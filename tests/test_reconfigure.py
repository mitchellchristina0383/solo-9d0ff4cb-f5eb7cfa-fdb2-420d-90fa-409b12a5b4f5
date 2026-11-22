"""阈值在线调整语义测试：改阈值立即生效、旧建议撤下、在途测量作废。

覆盖：
- 测量收发期间改 ``max_size``：旧建议立即撤下，在途测量不得发布；
- 改回原值同样算“实际变过”，旧建议不恢复；
- 调整备用线不影响现用线；
- 配置请求超时：按回显端实际生效情况处理（生效则撤建议，未生效则保留）；
- 配置结果未确认：该线路暂停提供建议，确认阈值没变后原建议恢复。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx

from app.config import LineEndpoint
from app.main import create_app
from tests.echoutils import full_echo

MEASURE_BODY = {"lower": 512, "upper": 9000, "timeout_ms": 200, "attempts": 3}


class FaultyControlClient:
    """包装真实 httpx 客户端，按需注入控制口不可达或请求超时。"""

    def __init__(self, real: httpx.AsyncClient) -> None:
        self.real = real
        self.fail_get = False
        self.fail_post = False
        #: POST 超时前是否先真实送达回显端（模拟“生效了但响应丢了”）。
        self.apply_before_timeout = False

    async def post(self, url: str, **kwargs):
        if self.fail_post:
            if self.apply_before_timeout:
                await self.real.post(url, **kwargs)
            raise httpx.ReadTimeout("模拟配置请求超时", request=None)
        return await self.real.post(url, **kwargs)

    async def get(self, url: str, **kwargs):
        if self.fail_get:
            raise httpx.ConnectError("模拟控制口不可达", request=None)
        return await self.real.get(url, **kwargs)

    async def aclose(self) -> None:
        await self.real.aclose()


@asynccontextmanager
async def stack(
    a_delay_ms=10,
    b_delay_ms=10,
    a_max_size=1500,
    b_max_size=4000,
    http_timeout=3.0,
):
    async with full_echo("a", max_size=a_max_size, delay_ms=a_delay_ms) as ea, full_echo(
        "b", max_size=b_max_size, delay_ms=b_delay_ms
    ) as eb:
        lines = {
            "a": LineEndpoint("a", "127.0.0.1", ea.udp_port, ea.ctrl_url),
            "b": LineEndpoint("b", "127.0.0.1", eb.udp_port, eb.ctrl_url),
        }
        app = create_app(lines, http_timeout=http_timeout)
        manager = app.state.manager
        real_http = manager.http
        fault = FaultyControlClient(real_http)
        manager.http = fault  # type: ignore[assignment]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield client, ea, eb, app, fault
        finally:
            manager.http = real_http
            await real_http.aclose()


async def wait_done(client, measurement_id, timeout_s=30.0):
    for _ in range(int(timeout_s / 0.05)):
        body = (await client.get(f"/api/measurements/{measurement_id}")).json()
        if body["status"] != "running":
            return body
        await asyncio.sleep(0.05)
    raise TimeoutError(f"测量 {measurement_id} 超时未结束")


async def measure(client, **overrides):
    body = {**MEASURE_BODY, **overrides}
    resp = await client.post("/api/measure", json=body)
    assert resp.status_code == 202, resp.text
    return await wait_done(client, resp.json()["id"])


async def test_echo_threshold_version_only_changes_on_actual_change():
    """回显端阈值版本：设同值不变，改值 +1，改回原值仍 +1。"""
    async with stack() as (client, ea, _eb, _app, _fault):
        before = (await client.get("/api/lines")).json()
        version0 = {x["name"]: x["config"]["threshold_version"] for x in before}["a"]

        resp = await client.post("/api/lines/a/config", json={"max_size": 1500})
        assert resp.json()["threshold_version"] == version0
        assert resp.json()["threshold_changed"] is False

        resp = await client.post("/api/lines/a/config", json={"max_size": 2000})
        assert resp.json()["threshold_version"] == version0 + 1
        assert resp.json()["threshold_changed"] is True

        # 改回原值：同样是“实际变过”，版本继续 +1。
        resp = await client.post("/api/lines/a/config", json={"max_size": 1500})
        assert resp.json()["threshold_version"] == version0 + 2
        assert resp.json()["threshold_changed"] is True
        assert ea.cfg.max_size == 1500


async def test_change_threshold_mid_measurement_withdraws_and_discards():
    """探测收发间改阈值：旧建议立即撤下，在途测量作废；重测得到新阈值。"""
    async with stack(a_delay_ms=60) as (client, ea, _eb, _app, _fault):
        assert (await measure(client))["max_payload"] == 1500

        body = {**MEASURE_BODY, "upper": 65507, "timeout_ms": 400}
        mid = (await client.post("/api/measure", json=body)).json()["id"]
        await asyncio.sleep(0.25)  # 测量正在收发探测包
        assert (await client.get(f"/api/measurements/{mid}")).json()["status"] == "running"

        resp = await client.post("/api/lines/a/config", json={"max_size": 2000})
        assert resp.status_code == 200
        assert resp.json()["threshold_changed"] is True
        # 改阈值不用等测量结束：配置响应返回时旧建议已撤下。
        assert (await client.get("/api/recommendation")).json()["recommendation"] is None

        rec = await wait_done(client, mid)
        assert rec["status"] in ("aborted", "discarded")
        assert rec["published"] is False
        assert "阈值" in rec["detail"]
        assert (await client.get("/api/recommendation")).json()["recommendation"] is None

        # 改动生效后重新测量，发布新阈值；线路 a 的旧回包不会让结论超出新阈值。
        assert (await measure(client))["max_payload"] == 2000
        assert ea.cfg.max_size == 2000


async def test_change_back_to_original_still_invalidates():
    """改过再改回原值：旧建议不恢复，改动前启动的测量也不能发布。"""
    async with stack(a_delay_ms=60) as (client, _ea, _eb, app, _fault):
        assert (await measure(client))["max_payload"] == 1500

        body = {**MEASURE_BODY, "upper": 65507, "timeout_ms": 400}
        mid = (await client.post("/api/measure", json=body)).json()["id"]
        await asyncio.sleep(0.25)

        await client.post("/api/lines/a/config", json={"max_size": 2000})
        await client.post("/api/lines/a/config", json={"max_size": 1500})
        assert (await client.get("/api/recommendation")).json()["recommendation"] is None
        assert app.state.manager.config_state("a").generation >= 2

        rec = await wait_done(client, mid)
        assert rec["status"] in ("aborted", "discarded")
        assert rec["published"] is False
        assert (await client.get("/api/recommendation")).json()["recommendation"] is None

        # 只有改动之后开始的测量才能发布。
        assert (await measure(client))["max_payload"] == 1500


async def test_adjusting_standby_line_does_not_affect_active():
    """调整备用线：现用线建议与在途测量都不受影响。"""
    async with stack(a_delay_ms=60) as (client, _ea, _eb, app, _fault):
        assert (await measure(client))["max_payload"] == 1500

        body = {**MEASURE_BODY, "upper": 65507, "timeout_ms": 400}
        mid = (await client.post("/api/measure", json=body)).json()["id"]
        await asyncio.sleep(0.25)

        # 先读一次线路状态，为备用线 b 建立阈值版本基线
        await client.get("/api/lines")

        resp = await client.post("/api/lines/b/config", json={"max_size": 8000})
        assert resp.json()["line"] == "b"
        assert resp.json()["threshold_changed"] is True
        # 现用线 a 的建议仍在
        snap = (await client.get("/api/recommendation")).json()
        assert snap["recommendation"]["line"] == "a"
        assert snap["recommendation"]["max_payload"] == 1500
        # 现用线 a 的在途测量不受备用线调整影响
        rec = await wait_done(client, mid)
        assert rec["status"] == "completed"
        assert rec["published"] is True
        assert app.state.manager.config_state("b").generation == 1


async def test_config_timeout_uses_actual_echo_state_when_applied():
    """配置请求超时但回显端已生效：按实际状态撤下旧建议并推进版本。"""
    async with stack() as (client, ea, _eb, app, fault):
        assert (await measure(client))["max_payload"] == 1500

        fault.fail_post = True
        fault.apply_before_timeout = True
        resp = await client.post("/api/lines/a/config", json={"max_size": 2200})
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "reconciled"
        assert body["threshold_changed"] is True
        assert body["max_size"] == 2200
        assert ea.cfg.max_size == 2200
        assert (await client.get("/api/recommendation")).json()["recommendation"] is None
        assert app.state.manager.config_state("a").confirmed is True

        # 新阈值生效后重测
        assert (await measure(client))["max_payload"] == 2200


async def test_config_timeout_when_not_applied_keeps_recommendation():
    """配置请求超时但回显端实际没变：原建议与在途测量继续可用。"""
    async with stack(a_delay_ms=60) as (client, _ea, _eb, app, fault):
        assert (await measure(client))["max_payload"] == 1500

        body = {**MEASURE_BODY, "upper": 65507, "timeout_ms": 400}
        mid = (await client.post("/api/measure", json=body)).json()["id"]
        await asyncio.sleep(0.25)

        fault.fail_post = True
        fault.apply_before_timeout = False
        resp = await client.post("/api/lines/a/config", json={"max_size": 9999})
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "reconciled"
        assert resp.json()["threshold_changed"] is False
        assert resp.json()["max_size"] == 1500

        # 阈值没变：原建议保留，在途测量继续并正常发布。
        snap = (await client.get("/api/recommendation")).json()
        assert snap["recommendation"]["max_payload"] == 1500
        rec = await wait_done(client, mid)
        assert rec["status"] == "completed"
        assert rec["published"] is True
        assert app.state.manager.config_state("a").generation == 0


async def test_unconfirmed_config_pauses_recommendation_then_recovers():
    """配置结果未确认：该线路暂停提供建议；确认阈值没变后原建议恢复。"""
    async with stack() as (client, _ea, _eb, app, fault):
        assert (await measure(client))["max_payload"] == 1500

        # 配置与控制口都不可达 → 配置结果未确认
        fault.fail_post = True
        fault.fail_get = True
        resp = await client.post("/api/lines/a/config", json={"max_size": 3000})
        assert resp.status_code == 503
        assert resp.json()["detail"]["line_state"]["confirmed"] is False

        snap = (await client.get("/api/recommendation")).json()
        assert snap["recommendation"] is None
        assert snap["recommendation_status"] == "config_unconfirmed"
        # 底层建议仍在，只是暂停提供
        assert app.state.manager.recommendation is not None

        # 恢复控制口：回显端确认阈值一直没变 → 原建议恢复可用
        fault.fail_post = False
        fault.fail_get = False
        snap = (await client.get("/api/recommendation")).json()
        assert snap["recommendation_status"] == "ok"
        assert snap["recommendation"]["max_payload"] == 1500
        assert app.state.manager.config_state("a").confirmed is True


async def test_unconfirmed_config_pauses_publishing_new_measurement():
    """配置未确认期间完成的测量不得发布；恢复且阈值没变后新测量可发布。"""
    async with stack() as (client, _ea, _eb, app, fault):
        assert (await measure(client))["max_payload"] == 1500

        # 先制造未确认状态
        fault.fail_post = True
        fault.fail_get = True
        assert (
            await client.post("/api/lines/a/config", json={"max_size": 1800})
        ).status_code == 503

        # UDP 仍通，测量可以跑完，但发布前核实仍失败 → 不发布
        rec = await measure(client)
        assert rec["status"] == "discarded"
        assert rec["published"] is False
        assert (await client.get("/api/recommendation")).json()["recommendation"] is None

        # 恢复控制口，阈值其实没变：原建议恢复；新测量可正常发布
        fault.fail_post = False
        fault.fail_get = False
        assert (
            await client.get("/api/recommendation")
        ).json()["recommendation"]["max_payload"] == 1500
        rec = await measure(client)
        assert rec["status"] == "completed"
        assert rec["published"] is True


async def test_real_echo_delayed_response_after_apply():
    """真实回显端：配置已生效但响应拖到超时 → 按实际生效状态撤建议。"""
    async with stack(http_timeout=0.3) as (client, ea, _eb, app, _fault):
        assert (await measure(client))["max_payload"] == 1500

        # 让回显端在配置生效后把响应拖到 1s，主服务 0.3s 即超时。
        ea.fault.post_applies_then_delay_ms = 1000
        resp = await client.post("/api/lines/a/config", json={"max_size": 2100})
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == "reconciled"
        assert body["threshold_changed"] is True
        assert body["max_size"] == 2100
        assert ea.cfg.max_size == 2100
        assert app.state.manager.config_state("a").confirmed is True

        snap = (await client.get("/api/recommendation")).json()
        assert snap["recommendation"] is None
        assert (await measure(client))["max_payload"] == 2100


async def test_real_echo_control_unreachable_marks_unconfirmed():
    """真实回显端：配置与控制口读取都超时 → 未确认，暂停建议；恢复后按实况处理。"""
    async with stack(http_timeout=0.3) as (client, ea, _eb, app, _fault):
        assert (await measure(client))["max_payload"] == 1500

        ea.fault.post_applies_then_delay_ms = 1000
        ea.fault.get_delay_ms = 1000
        resp = await client.post("/api/lines/a/config", json={"max_size": 2600})
        assert resp.status_code == 503
        assert resp.json()["detail"]["line_state"]["confirmed"] is False
        assert (await client.get("/api/recommendation")).json()["recommendation"] is None

        # 恢复控制口：回显端显示阈值其实已变成 2600 → 旧建议作废，重测得到 2600。
        ea.fault.post_applies_then_delay_ms = 0
        ea.fault.get_delay_ms = 0
        snap = (await client.get("/api/recommendation")).json()
        # 阈值确已变化：旧建议已作废，状态不再是“暂停提供”。
        assert snap["recommendation_status"] in ("none", "ok")
        assert snap["recommendation"] is None
        assert app.state.manager.config_state("a").confirmed is True
        assert app.state.manager.config_state("a").generation >= 1
        assert (await measure(client))["max_payload"] == 2600


async def test_out_of_band_change_before_publish_is_detected():
    """绕过主服务直接改回显端阈值：发布前对账能发现，结果不发布。"""
    async with stack(a_delay_ms=60) as (client, ea, _eb, app, _fault):
        assert (await measure(client))["max_payload"] == 1500

        body = {**MEASURE_BODY, "upper": 65507, "timeout_ms": 400}
        mid = (await client.post("/api/measure", json=body)).json()["id"]
        await asyncio.sleep(0.25)
        # 直接改回显端（不经过主服务）
        ea.cfg.max_size = 2000
        ea.threshold.version += 1

        rec = await wait_done(client, mid)
        assert rec["status"] in ("aborted", "discarded")
        assert rec["published"] is False
        assert (await client.get("/api/recommendation")).json()["recommendation"] is None
        assert app.state.manager.config_state("a").generation >= 1
