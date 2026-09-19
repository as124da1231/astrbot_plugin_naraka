"""角色绑定 / 解绑的生命周期管理。

设计要点（对应升级规格）：

* **一次只能绑定一个号**：全局互斥锁，覆盖「绑定 → 查询 → 解绑」整个过程。
  纯只读查询不占这把锁，所以不会被 40 多秒的兜底挡住。
* **无条件解绑**：只要发起过 `bind_game_id` 就在 `finally` 里解绑，
  **`waiting` 超时也算"可能已经绑上"**，必须去解。
* **解绑失败就重试**，重试仍失败 → 记入残留（落盘），由调用方提示用户手动执行 `战绩解绑`。
* **残留记录**还有两个用途：进程被 kill 后启动时补解绑；判断某角色"是不是我们绑的"。
* **负缓存**：同一角色绑定过仍然查不到数据，一段时间内不再重复绑定，避免写操作放大。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .heybox_api import (
    DEFAULT_SERVER,
    HeyboxError,
    HeyboxLoginRequired,
    HeyboxRateLimited,
    HeyboxClient,
)


@dataclass
class BindOutcome:
    """绑定结果。ok=False 时 message 是可以直接丢给用户的原因。"""

    ok: bool
    message: str = ""
    state: str = ""
    # 解绑结果（在 bound() 的 finally 里回填，块外可读）
    unbind_ok: bool = True
    unbind_error: str = ""


class _NullPace:
    """未注入节流器时的空实现。"""

    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc_info):
        return False


_NULL_PACE = _NullPace()


@dataclass
class Residual:
    nickname: str
    role_id: str = ""
    server: str = DEFAULT_SERVER
    at: float = field(default_factory=time.time)


class BindingManager:
    def __init__(
        self,
        api: HeyboxClient,
        store_path: Path,
        *,
        logger=None,
        state_interval: float = 2.0,
        state_attempts: int = 5,
        unbind_attempts: int = 3,
        unbind_interval: float = 2.0,
        negative_ttl: float = 600.0,
        pace=None,
    ):
        self.api = api
        self.store_path = Path(store_path)
        self.logger = logger
        self.state_interval = state_interval
        self.state_attempts = state_attempts
        self.unbind_attempts = unbind_attempts
        self.unbind_interval = unbind_interval
        self.negative_ttl = negative_ttl
        # 注入的节流器：绑定/解绑请求也按「单局详情间隔」排队
        self.pace = pace

        # 一次只能绑定一个号
        self.lock = asyncio.Lock()
        self._current: str | None = None
        self._residual: dict[str, Residual] = {}
        self._negative: dict[str, float] = {}
        self._recovery_done = False
        self._last_recovery_at = 0.0
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return
        if not isinstance(raw, dict):
            return
        residual = raw.get("residual")
        if isinstance(residual, dict):
            for name, info in residual.items():
                if isinstance(info, dict):
                    self._residual[str(name)] = Residual(
                        nickname=str(name),
                        role_id=str(info.get("role_id") or ""),
                        server=str(info.get("server") or DEFAULT_SERVER),
                        at=float(info.get("at") or time.time()),
                    )
        negative = raw.get("negative")
        if isinstance(negative, dict):
            for key, stamp in negative.items():
                try:
                    self._negative[str(key)] = float(stamp)
                except (TypeError, ValueError):
                    continue

    def _save(self) -> None:
        payload = {
            "residual": {
                name: {"role_id": item.role_id, "server": item.server, "at": item.at}
                for name, item in self._residual.items()
            },
            "negative": self._negative,
        }
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.store_path)
        except OSError as exc:  # 落盘失败不能影响查询
            if self.logger:
                self.logger.warning(f"[naraka] 绑定状态落盘失败: {type(exc).__name__}")

    # ---------- 状态查询 ----------

    @property
    def current(self) -> str | None:
        """当前正被我们绑着的昵称（没有则为 None）。"""
        return self._current

    def is_ours(self, nickname: str) -> bool:
        """这个昵称是不是我们绑的（含残留未解绑的）。"""
        return nickname == self._current or nickname in self._residual

    def residuals(self) -> list[Residual]:
        return sorted(self._residual.values(), key=lambda item: item.at)

    def negative_hit(self, key: str) -> bool:
        stamp = self._negative.get(key)
        if stamp is None:
            return False
        if time.time() - stamp > self.negative_ttl:
            self._negative.pop(key, None)
            self._save()
            return False
        return True

    def mark_negative(self, key: str) -> None:
        self._negative[key] = time.time()
        self._save()

    def clear_negative(self, key: str) -> None:
        if self._negative.pop(key, None) is not None:
            self._save()

    # ---------- 绑定 / 解绑 ----------

    def _pace(self):
        return self.pace() if self.pace is not None else _NULL_PACE

    async def _bind(self, session, nickname: str, server_id: str) -> BindOutcome:
        try:
            async with self._pace():
                body = await self.api.bind_game(session, nickname, server_id)
        except HeyboxLoginRequired:
            raise
        except HeyboxError as exc:
            # 接口层异常视为技术问题，不记为名字问题
            return BindOutcome(False, str(exc), "error")

        state = str(body.get("state") or "")
        if state == "ok":
            return BindOutcome(True, "", state)

        # waiting：官方前端每 2 秒轮询一次、最多 5 次
        for _ in range(self.state_attempts):
            await asyncio.sleep(self.state_interval)
            try:
                async with self._pace():
                    body = await self.api.bind_game_state(session, nickname)
            except HeyboxLoginRequired:
                raise
            except HeyboxError as exc:
                return BindOutcome(False, str(exc), state)
            state = str(body.get("state") or "")
            if state == "ok":
                return BindOutcome(True, "", state)
            if state != "waiting":
                return BindOutcome(False, self._state_reason(state, body), state)

        return BindOutcome(False, "绑定等待超时", "waiting")

    @staticmethod
    def _state_reason(state: str, body: dict) -> str:
        detail = str(body.get("btn_desc") or body.get("msg") or "").strip()
        if detail:
            return detail
        if state in ("failed", ""):
            return "英文请区分大小写，或绑定队列过长"
        return f"绑定状态异常（{state}）"

    async def _unbind(self, session, nickname: str, server_id: str) -> tuple[bool, str]:
        """重试解绑；失败则记入残留。"""
        last_error = ""
        for attempt in range(max(1, self.unbind_attempts)):
            try:
                async with self._pace():
                    await self.api.unbind_game(session, nickname, server_id)
                if self._residual.pop(nickname, None) is not None:
                    self._save()
                return True, ""
            except HeyboxRateLimited as exc:
                last_error = str(exc)
            except HeyboxError as exc:
                last_error = str(exc)
            if attempt + 1 < self.unbind_attempts:
                await asyncio.sleep(self.unbind_interval)

        self._residual[nickname] = Residual(nickname=nickname, server=server_id)
        self._save()
        return False, last_error or "解绑失败"

    @asynccontextmanager
    async def bound(self, session, nickname: str, server_id: str = DEFAULT_SERVER,
                    role_id: str = ""):
        """绑定 → 查询 → 自动解绑。

        用法：
            async with manager.bound(session, name, server) as outcome:
                if not outcome.ok:
                    ...  # outcome.message 直接给用户
        """
        async with self.lock:
            self._current = nickname
            # 发起绑定后必须尝试解绑
            outcome = BindOutcome(False, "绑定失败")
            try:
                outcome = await self._bind(session, nickname, server_id)
                yield outcome
            finally:
                if role_id and nickname in self._residual:
                    self._residual[nickname].role_id = role_id
                ok, reason = await self._unbind(session, nickname, server_id)
                outcome.unbind_ok = ok
                outcome.unbind_error = reason
                if not ok and self.logger:
                    self.logger.warning(
                        f"[naraka] 解绑失败 nickname={nickname!r} reason={reason}"
                    )
                self._current = None

    async def manual_unbind(self, session, nickname: str,
                            server_id: str = DEFAULT_SERVER) -> tuple[bool, str]:
        """手动解绑（`战绩解绑`）。拿同一把绑定锁，避免解掉别人正在用的角色。"""
        async with self.lock:
            return await self._unbind(session, nickname, server_id)

    async def retry_all(self, session) -> list[tuple[str, bool, str]]:
        """把当前所有残留逐个尝试解绑。"""
        results: list[tuple[str, bool, str]] = []
        for item in self.residuals():
            async with self.lock:
                ok, reason = await self._unbind(session, item.nickname, item.server)
            results.append((item.nickname, ok, reason))
            if self.logger:
                self.logger.info(
                    f"[naraka] 残留解绑 nickname={item.nickname!r} ok={ok} {reason}"
                )
        return results

    async def recover(self, session, interval: float = 3600.0, *, force: bool = False):
        """把残留绑定尝试解掉：启动后第一次查询 + 之后按 interval 周期重试。

        没有残留时只做一次标记，不会反复触发。
        """
        now = time.time()
        if not force:
            if self._recovery_done:
                if not self._residual or now - self._last_recovery_at < interval:
                    return []
            elif not self._residual:
                self._recovery_done = True
                self._last_recovery_at = now
                return []
        self._last_recovery_at = now
        results = await self.retry_all(session)
        self._recovery_done = True
        return results
