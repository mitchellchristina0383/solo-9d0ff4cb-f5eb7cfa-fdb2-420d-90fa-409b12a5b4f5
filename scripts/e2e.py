#!/usr/bin/env python3
"""端到端自测脚本（仅用标准库）：对运行中的服务栈做完整场景核对。

覆盖：超长丢包二分测量、探测次数对数级、切线即撤建议、断线无法判定、
测量中切线作废、切回原线路也只认切换后的测量、/docs 可用，以及阈值在线调整：
探测收发间改阈值、改回原值、备用线隔离、配置生效后响应超时。

用法：
    python3 scripts/e2e.py --base http://127.0.0.1:8000 \
        --ctrl-a http://127.0.0.1:9101 --ctrl-b http://127.0.0.1:9102
退出码非 0 表示有场景未通过。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

RESULTS: list[tuple[bool, str, str]] = []


def api(base: str, method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw.decode(errors="replace")


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def wait_done(base: str, mid: str, timeout_s: float = 60.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _, rec = api(base, "GET", f"/api/measurements/{mid}")
        if rec["status"] != "running":
            return rec
        time.sleep(0.2)
    raise TimeoutError(f"测量 {mid} 超时未结束")


def measure(base: str, lower: int, upper: int, timeout_ms: int = 300) -> dict:
    status, rec = api(
        base,
        "POST",
        "/api/measure",
        {"lower": lower, "upper": upper, "timeout_ms": timeout_ms, "attempts": 3},
    )
    assert status == 202, f"发起测量失败: {status} {rec}"
    return wait_done(base, rec["id"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--ctrl-a", default="http://127.0.0.1:9101")
    parser.add_argument("--ctrl-b", default="http://127.0.0.1:9102")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    ctrl = {"a": args.ctrl_a.rstrip("/"), "b": args.ctrl_b.rstrip("/")}
    print(f"目标服务: {base}\n控制口: a={ctrl['a']} b={ctrl['b']}\n")

    print("== 0. 服务与文档可用 ==")
    status, _ = api(base, "GET", "/")
    check("GET / 返回 200", status == 200)
    req = urllib.request.Request(base + "/docs")
    with urllib.request.urlopen(req, timeout=10) as resp:
        docs_ok = resp.status == 200 and b"swagger" in resp.read().lower()
    check("GET /docs 返回 Swagger UI", docs_ok)

    print("== 1. 初始化两条线路的模拟参数 ==")
    status, cfg_a = api(base, "POST", "/api/lines/a/config",
                        {"max_size": 1500, "delay_ms": 30, "down": False})
    check("线路 a 配置 max_size=1500", status == 200 and cfg_a["max_size"] == 1500)
    status, cfg_b = api(base, "POST", "/api/lines/b/config",
                        {"max_size": 4000, "delay_ms": 80, "down": False})
    check("线路 b 配置 max_size=4000", status == 200 and cfg_b["max_size"] == 4000)
    _, snap = api(base, "GET", "/api/line")
    check("默认激活线路为 a", snap["active_line"] == "a")

    print("== 2. 超长丢包：线路 a 上二分测量 [512, 9000] ==")
    rec = measure(base, 512, 9000)
    check("测量完成", rec["status"] == "completed", rec["detail"])
    check("测出阈值 1500", rec["max_payload"] == 1500,
          f"max_payload={rec['max_payload']}")
    check("探测次数为对数级 (<200，范围宽 8489)", rec["probes_sent"] < 200,
          f"probes_sent={rec['probes_sent']}")
    _, snap = api(base, "GET", "/api/recommendation")
    check("建议已发布 1500",
          snap["recommendation"] and snap["recommendation"]["max_payload"] == 1500)

    print("== 3. 切线：建议立即撤下，换线后重新测量 ==")
    _, snap = api(base, "POST", "/api/line/switch", {"line": "b"})
    check("切到 b 后建议为 null", snap["recommendation"] is None)
    _, snap = api(base, "GET", "/api/recommendation")
    check("撤下立即可见", snap["recommendation"] is None)
    rec = measure(base, 512, 9000)
    check("线路 b 测出阈值 4000", rec["max_payload"] == 4000,
          f"max_payload={rec['max_payload']}")

    print("== 4. 断线：最短包也超时 → 无法判定，本轮不发布建议 ==")
    api(base, "POST", "/api/lines/b/config", {"down": True})
    rec = measure(base, 512, 9000)
    check("状态为 undetermined", rec["status"] == "undetermined", rec["detail"])
    _, snap = api(base, "GET", "/api/recommendation")
    check("旧建议(4000)保留、未发布新建议",
          snap["recommendation"] and snap["recommendation"]["max_payload"] == 4000)
    api(base, "POST", "/api/lines/b/config", {"down": False})

    print("== 5. 测量中切线：旧测量作废，建议立即撤下 ==")
    status, rec = api(base, "POST", "/api/measure",
                      {"lower": 512, "upper": 65507, "timeout_ms": 300, "attempts": 3})
    mid = rec["id"]
    time.sleep(1.0)  # 让测量跑起来
    _, snap = api(base, "POST", "/api/line/switch", {"line": "a"})
    check("切线响应中建议已为 null", snap["recommendation"] is None)
    rec = wait_done(base, mid)
    check("旧测量作废(aborted/discarded)", rec["status"] in ("aborted", "discarded"),
          f"status={rec['status']}")
    _, snap = api(base, "GET", "/api/recommendation")
    check("建议未被旧测量发布", snap["recommendation"] is None)

    print("== 6. 切回原线路：也只接受切换后开始的测量 ==")
    status, rec = api(base, "POST", "/api/measure",
                      {"lower": 512, "upper": 65507, "timeout_ms": 300, "attempts": 3})
    mid = rec["id"]
    time.sleep(1.0)
    api(base, "POST", "/api/line/switch", {"line": "b"})
    api(base, "POST", "/api/line/switch", {"line": "a"})  # 立刻切回 a
    rec = wait_done(base, mid)
    check("切回后旧测量仍作废", rec["status"] in ("aborted", "discarded"),
          f"status={rec['status']}")
    _, snap = api(base, "GET", "/api/recommendation")
    check("建议仍为 null", snap["recommendation"] is None)
    rec = measure(base, 512, 9000)
    check("切回后重新测量有效 (1500)", rec["max_payload"] == 1500)

    print("== 7. 线路统计佐证超长丢包真实发生 ==")
    _, lines = api(base, "GET", "/api/lines")
    by_name = {l["name"]: l for l in lines}
    check("线路 a 有超长丢包计数",
          by_name["a"]["reachable"] and by_name["a"]["stats"]["dropped_oversize"] > 0,
          f"dropped_oversize={by_name['a']['stats']['dropped_oversize']}")
    check("线路 b 有断线丢包计数",
          by_name["b"]["reachable"] and by_name["b"]["stats"]["dropped_down"] > 0,
          f"dropped_down={by_name['b']['stats']['dropped_down']}")

    # ---- 阈值在线调整 ----
    print("== 8. 探测收发间改阈值：旧建议立即撤下，在途测量作废 ==")
    _, snap = api(base, "GET", "/api/line")
    if snap["active_line"] != "a":
        api(base, "POST", "/api/line/switch", {"line": "a"})
    rec = measure(base, 512, 9000)
    check("线路 a 当前建议 1500", rec["max_payload"] == 1500,
          f"max_payload={rec['max_payload']}")

    status, rec = api(base, "POST", "/api/measure",
                      {"lower": 512, "upper": 65507, "timeout_ms": 300, "attempts": 3})
    mid = rec["id"]
    time.sleep(0.8)  # 让测量进入收发探测阶段
    _, mid_rec = api(base, "GET", f"/api/measurements/{mid}")
    check("改阈值时测量仍在收发", mid_rec["status"] == "running",
          f"status={mid_rec['status']}")
    status, cfg = api(base, "POST", "/api/lines/a/config", {"max_size": 2000})
    check("配置响应标明阈值实际变化", cfg["threshold_changed"] is True and
          cfg["max_size"] == 2000, f"threshold_changed={cfg['threshold_changed']}")
    _, snap = api(base, "GET", "/api/recommendation")
    check("改阈值响应后旧建议已撤下（不等测量结束）", snap["recommendation"] is None)
    rec = wait_done(base, mid)
    check("改动前启动的测量不发布 (aborted/discarded)",
          rec["status"] in ("aborted", "discarded") and rec["published"] is False,
          f"status={rec['status']} detail={rec['detail']}")
    rec = measure(base, 512, 9000)
    check("改动生效后重测得到新阈值 2000", rec["max_payload"] == 2000,
          f"max_payload={rec['max_payload']}")

    print("== 9. 改回原值：仍算实际变过，旧建议不恢复、旧测量仍作废 ==")
    status, rec = api(base, "POST", "/api/measure",
                      {"lower": 512, "upper": 65507, "timeout_ms": 300, "attempts": 3})
    mid = rec["id"]
    time.sleep(0.8)
    _, back1 = api(base, "POST", "/api/lines/a/config", {"max_size": 1500})
    _, back2 = api(base, "POST", "/api/lines/a/config", {"max_size": 2000})
    check("改回原值也推进阈值版本",
          back1["threshold_changed"] is True and back2["threshold_changed"] is True)
    _, snap = api(base, "GET", "/api/recommendation")
    check("改回后建议仍为 null", snap["recommendation"] is None)
    rec = wait_done(base, mid)
    check("改动前启动的测量仍不发布",
          rec["status"] in ("aborted", "discarded") and rec["published"] is False,
          f"status={rec['status']}")
    rec = measure(base, 512, 9000)
    check("再次重测得到 2000", rec["max_payload"] == 2000,
          f"max_payload={rec['max_payload']}")

    print("== 10. 调整备用线不影响现用线 ==")
    status, rec = api(base, "POST", "/api/measure",
                      {"lower": 512, "upper": 65507, "timeout_ms": 300, "attempts": 3})
    mid = rec["id"]
    time.sleep(0.8)
    status, cfg_b = api(base, "POST", "/api/lines/b/config", {"max_size": 5000})
    check("备用线 b 阈值调整生效", cfg_b["max_size"] == 5000 and
          cfg_b["threshold_changed"] is True)
    _, snap = api(base, "GET", "/api/recommendation")
    check("现用线 a 建议仍在",
          snap["recommendation"] is not None and
          snap["recommendation"]["line"] == "a" and
          snap["recommendation"]["max_payload"] == 2000)
    rec = wait_done(base, mid)
    check("现用线 a 在途测量正常发布",
          rec["status"] == "completed" and rec["published"] is True,
          f"status={rec['status']}")

    print("== 11. 配置生效后响应超时：按回显端实际生效情况处理 ==")
    api(ctrl["a"], "POST", "/fault", {"post_applies_then_delay_ms": 4000,
                                      "get_delay_ms": 0})
    try:
        # 回显端先让 max_size 生效，再把响应拖到主服务 3s 超时之后。
        status, cfg = api(base, "POST", "/api/lines/a/config", {"max_size": 2400})
        check("配置请求超时后按实况对账成功", status == 200,
              f"status={status} body={cfg}")
        check("按实际生效状态撤下旧建议并推进版本",
              isinstance(cfg, dict) and cfg.get("outcome") == "reconciled" and
              cfg.get("threshold_changed") is True and cfg.get("max_size") == 2400,
              f"outcome={cfg.get('outcome') if isinstance(cfg, dict) else cfg}")
        _, snap = api(base, "GET", "/api/recommendation")
        check("超时后旧建议已撤下", snap["recommendation"] is None)
        _, line_a = api(base, "GET", "/api/lines")
        a_cfg = {l["name"]: l for l in line_a}["a"]["config"]
        check("回显端实际阈值确为 2400", a_cfg["max_size"] == 2400)
        rec = measure(base, 512, 9000)
        check("按新阈值重测得到 2400", rec["max_payload"] == 2400,
              f"max_payload={rec['max_payload']}")
    finally:
        api(ctrl["a"], "POST", "/fault", {"post_applies_then_delay_ms": 0,
                                          "get_delay_ms": 0})

    print("== 12. 配置与控制口都超时：未确认期间暂停提供建议 ==")
    api(ctrl["a"], "POST", "/fault", {"post_applies_then_delay_ms": 4000,
                                      "get_delay_ms": 4000})
    try:
        status, body = api(base, "POST", "/api/lines/a/config", {"max_size": 2600})
        check("配置结果未确认返回 503", status == 503, f"status={status}")
        _, snap = api(base, "GET", "/api/recommendation")
        check("未确认期间该线路暂停提供建议",
              snap["recommendation"] is None and
              snap.get("recommendation_status") == "config_unconfirmed",
              f"status={snap.get('recommendation_status')}")
        _, snap_ab = api(base, "GET", "/api/recommendation")
        check("调整备用线 b 仍可正常查询（b 未确认状态不影响 a）",
              snap_ab["line_config"]["b"]["confirmed"] is True)
    finally:
        api(ctrl["a"], "POST", "/fault", {"post_applies_then_delay_ms": 0,
                                          "get_delay_ms": 0})
    # 恢复后按回显端实况处理：阈值确已变成 2600 → 旧建议作废。
    _, snap = api(base, "GET", "/api/recommendation")
    check("恢复确认后建议仍不沿用旧值", snap["recommendation"] is None,
          f"recommendation={snap['recommendation']}")
    rec = measure(base, 512, 9000)
    check("恢复后重测得到 2600", rec["max_payload"] == 2600,
          f"max_payload={rec['max_payload']}")

    failed = [item for item in RESULTS if not item[0]]
    print(f"\n汇总: {len(RESULTS) - len(failed)}/{len(RESULTS)} 项通过")
    for _, name, detail in failed:
        print(f"  未通过: {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
