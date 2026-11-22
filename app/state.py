"""运行状态：当前线路、纪元(epoch)、建议值、测量任务生命周期、线路配置版本。

切线语义：
- 每次真实切线（目标线路 ≠ 当前线路）都会令 epoch +1，并**立即撤下旧建议**；
- 测量启动时记录当时的 epoch，测量过程中或发布前发现 epoch 变化即作废——
  即使又切回原线路，也只接受切换之后开始的测量结果。

阈值配置语义（每条线路独立）：
- 回显端用单调递增的 ``threshold_version`` 标记“阈值实际变过”（改回原值也算变过）；
- 主服务为每条线路维护 ``generation``：只要确认阈值版本变化就 +1，并撤下该线路
  旧建议；在途测量启动时记录 generation，变化后一律不得发布；
- 改阈值不必等测量结束：配置一确认，旧建议立即撤下，在途测量随即中止；
- 配置结果无法核实（例如配置请求超时且回显端也读不到）时 ``confirmed=False``：
  该线路暂停提供建议、暂停发布测量结果；之后确认阈值其实没变，原建议与在途测量
  可继续使用；确认变了则按上面的规则撤下/作废；
- 所有状态按线路隔离：调整备用线不影响现用线的建议和在途测量。
"""
from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque

import httpx

from .config import LineEndpoint
from .measurement import Aborted, Undetermined, find_max_payload
from .prober import UdpProber

_CONFIG_KEYS = ("max_size", "delay_ms", "down")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConfigUnconfirmed(Exception):
    """配置结果无法核实：该线路暂停提供/发布建议。"""

    def __init__(self, message: str, state: dict) -> None:
        super().__init__(message)
        self.state = state


class ConfigRejected(Exception):
    """回显端拒绝了配置请求（参数非法等），配置未变。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class LineConfigState:
    """一条线路相对回显端的配置认知。"""

    #: 已确认的回显端阈值版本；None 表示尚未建立基线。
    threshold_version: int | None = None
    #: 阈值实际变化的次数；测量启动时记录，变化后旧测量不得发布。
    generation: int = 0
    #: 配置状态是否已与回显端核实。False 时该线路暂停提供/发布建议。
    confirmed: bool = True
    #: 最近一次确认的完整配置（max_size/delay_ms/down）。
    config: dict | None = None
    detail: str = ""

    @property
    def baseline_known(self) -> bool:
        """是否已建立阈值基线（有版本号，或至少用实值建立过配置基线）。"""
        return self.threshold_version is not None or self.config is not None

    def view(self) -> dict:
        return {
            "confirmed": self.confirmed,
            "threshold_version": self.threshold_version,
            "generation": self.generation,
            "config": self.config,
            "detail": self.detail,
        }


@dataclass
class Recommendation:
    """一条已发布的建议：某条线路上可用的最大载荷字节数。"""

    line: str
    epoch: int
    generation: int
    max_payload: int
    lower: int
    upper: int
    probes_sent: int
    measurement_id: str
    published_at: datetime


class LineManager:
    """线路台账：当前激活线路、epoch、现行建议、每条线路的配置版本。"""

    def __init__(
        self,
        lines: dict[str, LineEndpoint],
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not lines:
            raise ValueError("至少需要一条线路")
        self.lines = lines
        self.http = http
        self.active: str = next(iter(lines))
        self.epoch = 0
        self.recommendation: Recommendation | None = None
        self.config_states: dict[str, LineConfigState] = {
            name: LineConfigState() for name in lines
        }
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 基础查询

    def config_state(self, name: str) -> LineConfigState:
        return self.config_states[name]

    async def begin_measurement_context(self) -> dict:
        """为即将开始的测量建立阈值基线并原子地取上下文。

        先尽力向回显端刷新一次阈值版本，尽量让“基线”反映测量启动那一刻的实况；
        建立基线需要访问控制口，期间可能发生切线/换线，因此每次等待后重新确认
        当前激活线路，确保 line 与 epoch 来自同一时刻。
        """
        while True:
            async with self._lock:
                line = self.active
            await self.refresh_line(line)
            async with self._lock:
                if self.active != line:
                    continue  # 期间切线，重新取当前线路
                st = self.config_states[line]
                return {
                    "line": line,
                    "epoch": self.epoch,
                    "generation": st.generation,
                    "confirmed": st.confirmed,
                    # 基线是否已建立：未建立则之后无法证明阈值没变。
                    "baseline_known": st.baseline_known,
                }

    async def switch(self, name: str) -> dict:
        """切线：立即撤下旧建议并推进 epoch。切到当前线路视为无操作。"""
        async with self._lock:
            changed = name != self.active
            if changed:
                self.active = name
                self.epoch += 1
                self.recommendation = None  # 立即撤下旧建议
            return self.snapshot(changed=changed)

    # ------------------------------------------------------------------ 配置对账

    async def _read_config(self, name: str) -> dict:
        if self.http is None:
            raise RuntimeError("未配置线路控制口客户端")
        ep = self.lines[name]
        resp = await self.http.get(f"{ep.ctrl_url}/config")
        resp.raise_for_status()
        return resp.json()

    def _invalidate_locked(self, name: str, detail: str) -> None:
        """阈值实际变过：推进 generation 并立即撤下该线路旧建议。"""
        st = self.config_states[name]
        st.generation += 1
        st.detail = detail
        if self.recommendation is not None and self.recommendation.line == name:
            self.recommendation = None

    def _apply_observed_locked(self, name: str, observed: dict) -> bool:
        """把回显端实际配置并入状态；返回阈值是否实际变过。

        首选回显端的单调 ``threshold_version``；缺失或复位（如回显端重启）时，
        再以最近确认的 ``max_size`` 实值兜底比较，避免漏判。
        """
        st = self.config_states[name]
        config = {k: observed.get(k) for k in _CONFIG_KEYS}
        version = observed.get("threshold_version")
        changed = False

        if version is not None:
            if st.threshold_version is None or version > st.threshold_version:
                if st.threshold_version is not None:
                    self._invalidate_locked(name, "回显端阈值实际变化，已撤下旧建议")
                    changed = True
                st.threshold_version = version  # 首次建立或推进基线
            elif version < st.threshold_version:
                # 版本回退通常是回显端进程重启（计数归零），改用实值比较兜底。
                st.threshold_version = version

        if not changed:
            previous = (st.config or {}).get("max_size")
            if previous is not None and previous != config.get("max_size"):
                self._invalidate_locked(name, "回显端 max_size 实际变化，已撤下旧建议")
                changed = True

        st.config = config
        if not st.confirmed:
            st.detail = "配置已重新确认"
        st.confirmed = True
        return changed

    def _mark_unconfirmed_locked(self, name: str, detail: str) -> None:
        st = self.config_states[name]
        st.confirmed = False
        st.detail = detail

    async def observe_config(self, name: str, observed: dict) -> bool:
        """被动观测（如 GET /api/lines）到回显端配置时并入状态。"""
        async with self._lock:
            return self._apply_observed_locked(name, observed)

    async def reconcile_line(self, name: str) -> bool:
        """主动向回显端核实配置；成功返回 True，失败标记未确认并返回 False。"""
        try:
            observed = await self._read_config(name)
        except Exception as exc:  # noqa: BLE001 - 控制口不可达即视为未确认
            async with self._lock:
                self._mark_unconfirmed_locked(name, f"配置状态未确认: {exc}")
            return False
        async with self._lock:
            self._apply_observed_locked(name, observed)
        return True

    async def refresh_line(self, name: str) -> bool:
        """尽力向回显端刷新一次配置；失败时**不**改变确认状态。

        用于发布测量结果前识别绕过本服务的带外阈值改动；控制口暂时不可达
        不应把一个本来正常的线路标记为“未确认”。
        """
        try:
            observed = await self._read_config(name)
        except Exception:  # noqa: BLE001
            return False
        async with self._lock:
            self._apply_observed_locked(name, observed)
        return True

    async def reconcile_unconfirmed(self) -> None:
        """查询建议/线路状态前，尝试恢复未确认的线路（仅影响这些线路）。"""
        pending = [name for name, st in self.config_states.items() if not st.confirmed]
        if pending:
            await asyncio.gather(*(self.reconcile_line(name) for name in pending))

    async def configure(self, name: str, patch: dict) -> dict:
        """下发配置并以回显端实际生效状态为准更新本地认知。

        - 请求成功：直接采用回显端返回的配置；
        - 请求超时/连接异常：再读一次回显端配置，按实际生效情况处理；
        - 回显端明确拒绝：配置未变，返回拒绝（状态不变）；
        - 连回显端也读不到：标记该线路未确认，暂停提供/发布建议。
        """
        ep = self.lines[name]
        post_error: str | None = None

        # 先尽力建立阈值版本基线（不改确认状态），否则无法判断这次调整是否“实际变过”。
        st = self.config_states[name]
        if st.threshold_version is None or not st.confirmed:
            await self.refresh_line(name)

        try:
            resp = await self.http.post(f"{ep.ctrl_url}/config", json=patch)
            resp.raise_for_status()
            observed = resp.json()
            async with self._lock:
                changed = self._apply_observed_locked(name, observed)
                st = self.config_states[name]
                return self._config_result(
                    name, st, observed, changed, outcome="applied"
                )
        except httpx.HTTPStatusError as exc:
            # 明确被拒：配置确定没有变，不推进版本；回显端可达，顺带刷新确认状态。
            await self.refresh_line(name)
            raise ConfigRejected(
                exc.response.status_code,
                f"回显端拒绝配置: {exc.response.text[:200]}",
            ) from exc
        except httpx.HTTPError as exc:
            # 含超时：请求是否生效未知，必须向回显端核实实际状态。
            post_error = f"配置请求未得到确认（{exc!r}），已按回显端实际配置核对"

        try:
            observed = await self._read_config(name)
        except Exception as exc:  # noqa: BLE001
            async with self._lock:
                detail = f"配置结果未确认: {post_error or exc}"
                self._mark_unconfirmed_locked(name, detail)
                st = self.config_states[name]
                view = self._config_result(
                    name, st, st.config or {"line": name}, False, outcome="unconfirmed"
                )
            raise ConfigUnconfirmed(detail, view) from exc

        async with self._lock:
            changed = self._apply_observed_locked(name, observed)
            st = self.config_states[name]
            return self._config_result(
                name,
                st,
                observed,
                changed,
                outcome="reconciled",
                detail_override=post_error,
            )

    def _config_result(
        self,
        name: str,
        st: LineConfigState,
        observed: dict,
        changed: bool,
        *,
        outcome: str,
        detail_override: str | None = None,
    ) -> dict:
        result = {
            "line": name,
            **{k: observed.get(k) for k in _CONFIG_KEYS},
            "threshold_version": observed.get(
                "threshold_version", st.threshold_version
            ),
            "confirmed": st.confirmed,
            "threshold_changed": changed,
            "generation": st.generation,
            "outcome": outcome,
            "detail": detail_override if detail_override is not None else st.detail,
        }
        return result

    # ------------------------------------------------------------------ 发布建议

    async def publish_if_current(
        self,
        epoch: int,
        line: str,
        generation: int,
        baseline_known: bool,
        rec: Recommendation,
    ) -> tuple[Recommendation | None, str]:
        """仅当测量启动时的 epoch/generation 仍有效且配置已确认才发布。"""
        async with self._lock:
            st = self.config_states[line]
            if epoch != self.epoch:
                return None, "epoch"
            if generation != st.generation:
                return None, "threshold"
            if not st.confirmed:
                return None, "unconfirmed"
            if not baseline_known or not st.baseline_known:
                # 测量启动时未能建立阈值基线，无法证明期间阈值没变，保守不发布。
                return None, "baseline"
            self.recommendation = rec
            return rec, "ok"

    # ------------------------------------------------------------------ 快照

    def _recommendation_view(self) -> tuple[dict | None, str]:
        rec = self.recommendation
        if rec is None:
            return None, "none"
        st = self.config_states.get(rec.line)
        if st is not None and not st.confirmed:
            # 配置结果未确认：该线路暂停提供建议（保留底层建议，待确认后恢复）。
            return None, "config_unconfirmed"
        return {
            "line": rec.line,
            "epoch": rec.epoch,
            "generation": rec.generation,
            "max_payload": rec.max_payload,
            "range": {"lower": rec.lower, "upper": rec.upper},
            "probes_sent": rec.probes_sent,
            "measurement_id": rec.measurement_id,
            "published_at": rec.published_at,
        }, "ok"

    def snapshot(self, changed: bool | None = None) -> dict:
        recommendation, status = self._recommendation_view()
        snap = {
            "active_line": self.active,
            "epoch": self.epoch,
            "known_lines": sorted(self.lines),
            "recommendation": recommendation,
            "recommendation_status": status,
            "line_config": {
                name: st.view() for name, st in self.config_states.items()
            },
        }
        if changed is not None:
            snap["changed"] = changed
        return snap


class MeasurementBusy(Exception):
    """已有测量在进行中。"""


@dataclass
class Measurement:
    id: str
    line: str
    epoch: int
    generation: int
    lower: int
    upper: int
    timeout_ms: int
    attempts: int
    status: str = "running"  # running/completed/undetermined/aborted/discarded/error
    max_payload: int | None = None
    probes_sent: int = 0
    published: bool = False
    baseline_known: bool = True
    detail: str = ""
    events: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=_utcnow)
    finished_at: datetime | None = None


class MeasurementService:
    """测量任务调度：单并发（物理线路共享），后台执行，结果按 epoch/配置版本认领。"""

    def __init__(self, manager: LineManager, history: int = 50) -> None:
        self.manager = manager
        self.records: dict[str, Measurement] = {}
        self._order: Deque[str] = deque(maxlen=history)
        self._run_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._run_lock.locked()

    async def start(
        self, lower: int, upper: int, timeout_ms: int, attempts: int
    ) -> Measurement:
        if self._run_lock.locked():
            raise MeasurementBusy()
        await self._run_lock.acquire()
        try:
            ctx = await self.manager.begin_measurement_context()
            rec = Measurement(
                id=uuid.uuid4().hex[:12],
                line=ctx["line"],
                epoch=ctx["epoch"],
                generation=ctx["generation"],
                baseline_known=ctx["baseline_known"],
                lower=lower,
                upper=upper,
                timeout_ms=timeout_ms,
                attempts=attempts,
            )
        except Exception:
            self._run_lock.release()
            raise
        self.records[rec.id] = rec
        self._order.append(rec.id)
        asyncio.create_task(self._run(rec))
        return rec

    def get(self, measurement_id: str) -> Measurement | None:
        return self.records.get(measurement_id)

    def list_recent(self) -> list[Measurement]:
        return [self.records[i] for i in reversed(self._order) if i in self.records]

    async def _run(self, rec: Measurement) -> None:
        ep = self.manager.lines[rec.line]
        prober = UdpProber(ep.udp_host, ep.udp_port, rec.timeout_ms / 1000)
        epoch_at_start = rec.epoch
        generation_at_start = rec.generation
        state = self.manager.config_state(rec.line)

        def should_abort() -> bool:
            # 切线或该线路阈值实际变化都立即中止，不必等本轮测量自然结束。
            return (
                self.manager.epoch != epoch_at_start
                or state.generation != generation_at_start
            )

        try:
            await prober.open()
            best = await find_max_payload(
                prober,
                rec.lower,
                rec.upper,
                should_abort,
                rec.events,
                attempts=rec.attempts,
            )
            rec.max_payload = best
            rec.probes_sent = prober.sent

            # 发布前再向回显端核实一次实际生效的阈值（尽力而为）：
            # 能发现绕过本服务直接改回显端的情况；读不到也不误判为未确认。
            await self.manager.refresh_line(rec.line)
            if not state.confirmed:
                await self.manager.reconcile_line(rec.line)

            published, reason = await self.manager.publish_if_current(
                epoch_at_start,
                rec.line,
                generation_at_start,
                rec.baseline_known,
                Recommendation(
                    line=rec.line,
                    epoch=epoch_at_start,
                    generation=generation_at_start,
                    max_payload=best,
                    lower=rec.lower,
                    upper=rec.upper,
                    probes_sent=prober.sent,
                    measurement_id=rec.id,
                    published_at=_utcnow(),
                ),
            )
            if published is not None:
                rec.status = "completed"
                rec.published = True
                rec.detail = f"测量完成，已发布建议: {best}B (线路 {rec.line})"
            else:
                rec.status = "discarded"
                if reason == "threshold":
                    rec.detail = "测量期间线路阈值配置实际变化，结果作废，未发布建议"
                elif reason == "unconfirmed":
                    rec.detail = "线路配置结果未确认，结果不发布"
                elif reason == "baseline":
                    rec.detail = "测量启动时未能确认线路阈值基线，结果不发布"
                else:
                    rec.detail = "测量期间发生切线，结果作废，未发布建议"
        except Undetermined as exc:
            rec.status = "undetermined"
            rec.probes_sent = prober.sent
            rec.detail = str(exc)
        except Aborted:
            rec.status = "aborted"
            rec.probes_sent = prober.sent
            if self.manager.epoch != epoch_at_start:
                rec.detail = "测量期间发生切线，已主动中止，结果作废"
            elif state.generation != generation_at_start:
                rec.detail = "测量期间线路阈值配置实际变化，已主动中止，结果作废"
            else:
                rec.detail = "测量已中止，结果作废"
        except Exception as exc:  # noqa: BLE001 - 兜底，保证锁一定释放
            rec.status = "error"
            rec.probes_sent = prober.sent
            rec.detail = f"测量内部错误: {exc!r}"
        finally:
            rec.finished_at = _utcnow()
            prober.close()
            self._run_lock.release()
