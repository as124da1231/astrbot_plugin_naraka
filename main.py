"""QQ commands for looking up NARAKA PC records through Xiaoheihe.

数据模式（配置项 ``data_mode``）：

* ``wechat``  —— 微信扫码登录小黑盒（全量查询 + 可按需绑定角色）
* ``heybox``  —— 小黑盒 App 扫码登录（能力与 wechat 等价，只是扫码渠道不同）
* ``none``    —— 不登录，只走免登录接口（战绩查询退化为「角色名片」）

查询主流程：搜昵称 → 更新 → 取数据；搜不到走绑定 → 再搜 → 查询 → 解绑兜底。
绑定用用户原始输入；解绑失败提示发送 ``战绩解绑 <昵称>``。

上游为小黑盒非公开接口，凭证只保存在 AstrBot 的插件数据目录，不打印到聊天。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
import astrbot.api.message_components as Comp

from .binding import BindingManager
from .heybox_api import (
    DEFAULT_SERVER,
    HeyboxClient,
    HeyboxError,
    HeyboxLoginRequired,
    HeyboxRateLimited,
)
from .record_query import (
    KNOWN_HERO_NAMES,
    MODES,
    RANKED_IDS,
    apply_hero_mappings,
    format_details,
    format_identity,
    format_summary,
    make_record,
    recent_rows,
    resolve_mode,
    select_matches,
    short_role_id,
    split_detail_report,
)
from .xiaoheihe_login import LoginState, create_login_client, generate_device_id

PLUGIN = "astrbot_plugin_naraka"
LOGIN_MODES = ("wechat", "heybox")
MODE_LABEL = {"wechat": "微信", "heybox": "小黑盒 App"}


def _is_rate_limited(value: object) -> bool:
    message = str(value).lower()
    return "频繁" in message or "429" in message or "rate limit" in message


def _must_stop_after_error(exc: Exception) -> bool:
    return (
        isinstance(exc, (HeyboxRateLimited, HeyboxLoginRequired))
        or _is_rate_limited(exc)
        or "登录" in str(exc)
    )


def _has_value(value: object) -> bool:
    return value is not None and str(value).strip() != ""


def _split_lines(lines: list[str], limit: int = 1200) -> list[str]:
    """Keep a complete season report readable in QQ without dropping rows."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


# 角色主键的形态：22 字符主格式 + 长 hash + 历史前缀。
_ROLE_ID_SHAPES = (
    re.compile(r"[0-9A-Za-z]{4}\d{18}"),               # 主格式（22 字符）
    re.compile(r"[0-9A-Za-z]{64}"),                    # 长 hash 型
    re.compile(r"(?:uk|psrc)[0-9a-z]{15,63}", re.I),   # 历史前缀
)


def looks_like_role_id(value: str) -> bool:
    """输入看起来是不是角色ID。识别后给出明确提示，不作为昵称搜索。"""
    text = str(value or "").strip()
    return any(pattern.fullmatch(text) for pattern in _ROLE_ID_SHAPES)


def _choose_season(options: object, requested: str) -> tuple[str, str]:
    """Pick a season exposed by the API, without guessing its internal key."""
    entries = [x for x in options if isinstance(x, dict)] if isinstance(options, list) else []
    choices = [
        (str(x.get("key") or ""), str(x.get("value") or ""), x)
        for x in entries
        if x.get("key")
    ]
    if requested == "全部":
        return "pre-01", "全部赛季"
    if requested == "当前":
        current = next(
            (item for item in choices if item[0] != "pre-01" and any(item[2].get(k) for k in ("is_current", "current", "selected"))),
            None,
        )
        if current is None:
            # The live selector observed in September 2026 is oldest first.
            recent_first = [item for item in choices if item[0] != "pre-01"]
            current = recent_first[-1] if recent_first else None
        return (current[0], current[1]) if current else ("pre-01", "全部赛季")
    match = next(
        (item for item in choices if requested == item[0] or requested in item[1]),
        None,
    )
    if match is None:
        names = "、".join(x[1] for x in choices[:8]) or "暂无"
        raise ValueError(f"没有找到赛季「{requested}」。可用赛季：{names}")
    return match[0], match[1]


@dataclass
class _ReadResult:
    """一次只读查询的结果。"""

    ok: bool = False
    texts: list[str] = field(default_factory=list)
    needs_bind: bool = False          # 搜不到这个昵称 → 需要走绑定兜底
    nickname: str = ""                # 绑定用的字符串（= 用户原始输入）
    role_id: str = ""
    server: str = DEFAULT_SERVER
    reason: str = ""                  # 失败原因，可直接展示


@register(PLUGIN, "as124da1231", "永劫无间端游战绩查询", "1.5.0")
class NarakaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(StarTools.get_data_dir(PLUGIN))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._login_path = data_dir / "login.json"

        self._login_lock = asyncio.Lock()
        self._query_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_query_started_at: float | None = None
        self._last_request_at: float | None = None
        self._last_detail_request_at: float | None = None
        self._detail_cache: dict[tuple[str, str], dict] = {}

        self._auth = self._load_auth()
        self._api = HeyboxClient(
            self._auth.get("cookies") or {}, self._auth.get("device_id", "")
        )
        self._binding = BindingManager(
            self._api,
            data_dir / "bind_state.json",
            logger=logger,
            negative_ttl=self._bind_negative_seconds(),
            state_interval=self._bind_state_interval(),
            unbind_interval=self._unbind_retry_interval(),
            pace=self._bind_pace,
        )

    # ==================== 配置 ====================

    @property
    def data_mode(self) -> str:
        mode = str(self.config.get("data_mode", "heybox") or "heybox").strip().lower()
        return mode if mode in ("wechat", "heybox", "none") else "heybox"

    @property
    def logged_in(self) -> bool:
        return bool(self._auth.get("cookies"))

    def _number_setting(self, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(self.config.get(key, default))
            return max(minimum, min(maximum, value))
        except (TypeError, ValueError):
            return default

    def _count_setting(self, key: str, default: int) -> int:
        try:
            return max(1, int(self.config.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _bind_negative_seconds(self) -> float:
        minutes = self._number_setting("bind_negative_cache_minutes", 10.0, 0.0, 1440.0)
        return minutes * 60.0

    def _auto_bind(self) -> bool:
        return bool(self.config.get("auto_bind", True)) and self.data_mode in LOGIN_MODES

    def _refresh_enabled(self) -> bool:
        return bool(self.config.get("refresh_before_query", True))

    def _residual_interval_seconds(self) -> float:
        minutes = self._number_setting("residual_unbind_interval_minutes", 60.0, 1.0, 1440.0)
        return minutes * 60.0

    def _device_id_mode(self) -> str:
        mode = str(self.config.get("device_id_mode", "reuse") or "reuse").strip().lower()
        return mode if mode in ("reuse", "random", "off") else "reuse"

    def _resolve_device_id(self) -> str:
        """按配置决定本次登录用哪个 device_id。

        * ``reuse``（默认）：复用 login.json 里已有的；没有就生成一个并固定下来
        * ``random``        ：每次登录都换新的
        * ``off``           ：完全不发送 device_id 参数
        """
        mode = self._device_id_mode()
        if mode == "off":
            return ""
        existing = str(self._auth.get("device_id") or "")
        if mode == "random" or not existing:
            return generate_device_id()
        return existing

    def _login_search_fallback(self) -> bool:
        """免登录搜索没结果时，是否回退到带登录信息的搜索（默认关闭）。"""
        return bool(self.config.get("login_search_fallback", False))

    def _bind_state_interval(self) -> float:
        return self._number_setting("bind_state_interval_seconds", 2.0, 0.1, 120.0)

    def _unbind_retry_interval(self) -> float:
        return self._number_setting("unbind_retry_interval_seconds", 2.0, 0.1, 120.0)

    def _update_retry_interval(self) -> float:
        return self._number_setting("update_retry_interval_seconds", 3.0, 0.1, 120.0)

    def _pagination_mode(self) -> str:
        mode = str(self.config.get("match_list_pagination", "offset") or "offset").strip().lower()
        return mode if mode in ("offset", "page") else "offset"

    # ==================== 基础设施 ====================

    def _load_auth(self) -> dict:
        try:
            value = json.loads(self._login_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save_auth(self, value: dict) -> None:
        tmp = self._login_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._login_path)
        self._auth = value
        self._refresh_api()

    def _refresh_api(self) -> None:
        self._api = HeyboxClient(
            self._auth.get("cookies") or {}, self._auth.get("device_id", "")
        )
        self._binding.api = self._api

    @asynccontextmanager
    async def _session(self):
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            yield session

    def _interval_b(self) -> float:
        """B：机器人每一次上游请求之间的最小间隔（0 = 关闭）。"""
        return self._number_setting("request_interval_seconds", 0.0, 0.0, 120.0)

    def _interval_c(self) -> float:
        """C：仅单局查询之间互相的额外最小间隔（0 = 关闭）。"""
        return self._number_setting("detail_interval_seconds", 4.0, 0.0, 120.0)

    @asynccontextmanager
    async def _paced(self, *, detail: bool = False):
        """统一请求节流。

        * **B** ``request_interval_seconds``：机器人**每一次**请求之间的最小间隔
        * **C** ``detail_interval_seconds``：**仅单局详情之间**的额外最小间隔

        两者互不叠加，取较大值（B 包含 C）；任一为 0 即该项关闭。
        所有上游请求都必须经过这里，包括绑定/解绑/轮询/更新。
        """
        async with self._request_lock:
            now = time.monotonic()
            wait = 0.0
            interval_b = self._interval_b()
            if interval_b > 0 and self._last_request_at is not None:
                wait = max(wait, interval_b - (now - self._last_request_at))
            if detail:
                interval_c = self._interval_c()
                if interval_c > 0 and self._last_detail_request_at is not None:
                    wait = max(wait, interval_c - (now - self._last_detail_request_at))
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield
            finally:
                stamp = time.monotonic()
                self._last_request_at = stamp
                if detail:
                    self._last_detail_request_at = stamp

    def _bind_pace(self):
        """绑定/解绑相关请求按 **B** 节流（C 只管单局详情之间）。"""
        return self._paced()

    def _start_query_cooldown(self) -> int:
        """Reserve one user-initiated query and return remaining cooldown seconds."""
        now = time.monotonic()
        interval = self._number_setting("query_interval_seconds", 4.0, 0.0, 120.0)
        if self._last_query_started_at is not None:
            remaining = interval - (now - self._last_query_started_at)
            if remaining > 0:
                return max(1, int(remaining + 0.999))
        self._last_query_started_at = now
        return 0

    def _allowed_group(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat() or not self.config.get("enable_group_whitelist", False):
            return True
        allowed = self.config.get("group_whitelist", [])
        if not isinstance(allowed, list):
            return False
        return str(event.get_group_id() or "") in {str(item).strip() for item in allowed}

    @staticmethod
    def _message_id(event: AstrMessageEvent) -> str:
        value = getattr(getattr(event, "message_obj", None), "message_id", None)
        return str(value).strip() if value is not None else ""

    # 三个出口：_body 战绩正文（走 result_reply_method）／_note 其余回复（直接发）／_ack 回执

    async def _reply(self, event: AstrMessageEvent, message: str, method: str | None = None):
        """按 method 组装一条回复：引用 / 纯文本 / at。"""
        method = str(method or self.config.get("result_reply_method", "quote") or "quote")
        method = method.strip().lower()
        if method in ("at", "mention"):
            # at 模式：直接调底层 OneBot API 发原始消息段，成功后返回 None。
            if await self._send_at(event, message):
                return None
            method = "plain"  # 底层发送失败则退回纯文本
        if method == "quote":
            message_id = self._message_id(event)
            if message_id:
                return event.chain_result([Comp.Reply(id=message_id), Comp.Plain(message)])
        return event.plain_result(message)

    async def _send_at(self, event: AstrMessageEvent, message: str) -> bool:
        """at 模式：直接 event.bot.send(raw, [at段, text段])，任何失败返回 False。"""
        try:
            if event.get_platform_name() != "aiocqhttp":
                return False
            bot = getattr(event, "bot", None)
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            sender_id = str(event.get_sender_id() or "").strip()
            if bot is None or raw is None or not sender_id:
                return False
            await bot.send(raw, [
                {"type": "at", "data": {"qq": sender_id}},
                {"type": "text", "data": {"text": " " + message}},
            ])
            return True
        except Exception as exc:
            logger.warning(f"[naraka] at 模式底层发送失败，退回纯文本：{type(exc).__name__}")
            return False

    def result_method(self) -> str:
        """当前生效的战绩正文回复方式。"""
        return str(self.config.get("result_reply_method", "quote") or "quote").strip().lower()

    async def _body(self, event: AstrMessageEvent, message: str, first: bool = False):
        """战绩正文；first=False 时不 @（at 只加在第一条分条上）。"""
        method = self.result_method()
        if not first and method in ("at", "mention"):
            method = "plain"
        return await self._reply(event, message, method=method)

    @staticmethod
    def _note(event: AstrMessageEvent, message: str):
        """其余回复一律直接发。"""
        return event.plain_result(message)

    async def _react_to_msg(self, event: AstrMessageEvent) -> bool:
        """Attach a QQ reaction to the triggering message; never block the query."""
        try:
            if event.get_platform_name() != "aiocqhttp":
                return False
            message_id = self._message_id(event)
            if not message_id:
                return False
            await event.bot.api.call_action(
                "set_msg_emoji_like",
                message_id=message_id,
                emoji_id=str(self.config.get("react_emoji_id", "277") or "277"),
            )
            return True
        except Exception as exc:
            logger.warning(f"[naraka] QQ reaction failed: {type(exc).__name__}")
            return False

    async def _ack(self, event: AstrMessageEvent):
        """立刻回执：表明机器人已收到请求。

        必须在**冷却判定、登录检查、任何上游请求之前**发出 —— 无论后面走的是
        只读查询、绑定兜底，还是压根没登录，用户都要先看到这一条。
        """
        if not self.config.get("enable_start_notice", False):
            return None
        text = str(self.config.get("start_notice_text", "已收到请求，正在查询…") or "").strip()
        method = str(self.config.get("start_notice_method", "quote") or "quote").strip().lower()
        if method == "reaction":
            if await self._react_to_msg(event):
                return None
            method = "plain"  # 表情回应失败则退回文字回执
        if method in ("at", "mention"):
            method = "plain"  # 回执不提供 at
        return await self._reply(event, text, method=method) if text else None

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, HeyboxLoginRequired):
            return "小黑盒登录已失效，请管理员重新发送 /永劫登录"
        if isinstance(exc, HeyboxRateLimited):
            return "小黑盒要求降低请求频率，请稍后再试"
        return str(exc) or type(exc).__name__

    # ==================== 登录 / 退出 / 残留 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("永劫登录")
    async def login(self, event: AstrMessageEvent):
        mode = self.data_mode
        if mode == "none":
            yield event.plain_result("当前为「无绑定」模式，无需登录；如需登录请在插件配置里切换数据模式。")
            return
        if not event.is_private_chat() and not self.config.get("allow_group_login_qr", False):
            yield event.plain_result("请私聊机器人发送 /永劫登录；如需在群里扫码，请先开启「允许登录二维码发到群里」。")
            return
        if self._login_lock.locked():
            yield event.plain_result("已有登录流程正在等待扫码。")
            return

        label = MODE_LABEL.get(mode, mode)
        async with self._login_lock:
            try:
                client = create_login_client(mode, self._resolve_device_id())
                if client is None:
                    yield event.plain_result(f"未知的登录渠道：{mode}")
                    return
                qr = await client.request_qr()
                image = await client.qr_image(qr)
                yield event.chain_result([
                    Comp.Plain(f"请用{label}扫码并确认；二维码约 2 分钟后失效。"),
                    Comp.Image.fromBytes(image),
                ])
                until = min(qr.expires_at, time.time() + 120)
                while time.time() < until:
                    await asyncio.sleep(float(getattr(client, "poll_delay", 3.0) or 3.0))
                    state = await client.check_qr(qr)
                    if state.state is LoginState.SUCCESS:
                        if not state.cookies:
                            raise ValueError("扫码成功，但没有收到登录凭证")
                        self._save_auth({
                            "cookies": state.cookies,
                            "uid": state.uid,
                            "nickname": state.nickname,
                            "device_id": client.device_id,
                            "login_mode": mode,
                        })
                        yield event.plain_result("小黑盒登录成功。现在可以发送 战绩查询 <昵称>。")
                        return
                    if state.state in (LoginState.EXPIRED, LoginState.FAILED):
                        break
                yield event.plain_result("二维码已过期，请重新发送 /永劫登录")
            except Exception as exc:
                logger.warning(f"[naraka] login failed: {type(exc).__name__}")
                yield event.plain_result(f"登录失败：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("永劫退出")
    async def logout(self, event: AstrMessageEvent):
        self._auth = {}
        self._login_path.unlink(missing_ok=True)
        self._refresh_api()
        yield event.plain_result("已清除本地小黑盒登录信息。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("永劫残留")
    async def residuals(self, event: AstrMessageEvent):
        """查看并重试解绑残留（进程被 kill 后的兜底）。"""
        if self.data_mode == "none":
            yield event.plain_result("当前为「无绑定」模式，没有绑定记录。")
            return
        items = self._binding.residuals()
        if not items:
            yield event.plain_result("没有待解绑的残留绑定。")
            return
        lines = ["待解绑的残留绑定（正在重试）："]
        async with self._session() as session:
            for item in items:
                ok, reason = await self._binding.manual_unbind(session, item.nickname, item.server)
                lines.append(
                    f"{item.nickname}｜角色ID {item.role_id or '—'}｜"
                    f"{'已解绑' if ok else '仍失败：' + reason}"
                )
        for chunk in _split_lines(lines):
            yield event.plain_result(chunk)

    # ==================== 搜索 ====================

    @filter.command("永劫搜索")
    async def search(self, event: AstrMessageEvent, nickname: str = ""):
        """搜索统一走免登录通道：不带登录信息，未登录也能用。"""
        if not self._allowed_group(event):
            return
        if not nickname:
            yield self._note(event, "用法：永劫搜索 玩家昵称")
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield self._note(event, f"查询过于频繁，请 {cooldown} 秒后再试。")
            return
        async for message in self._public_search(event, nickname):
            yield message

    async def _public_search(self, event: AstrMessageEvent, nickname: str):
        """免登录搜索：唯一通道，不带任何登录信息。"""
        try:
            async with self._session() as session:
                rows = await self._public_search_rows(session, nickname, limit=10)
        except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            yield self._note(event, f"查询失败：{self._error_text(exc)}")
            return
        if not rows:
            yield self._note(event, f"没有找到「{nickname}」。")
            return
        lines = [f"搜索「{nickname}」："]
        for row in rows[:10]:
            lines.append(self._public_row_line(row))
        lines.append(f"查近期对局：战绩查询 {nickname}")
        yield self._note(event, "\n".join(lines))

    # ==================== 指令分发 ====================

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_message(self, event: AstrMessageEvent):
        """Use one listener for slash and plain-text queries to avoid duplicates."""
        text = str(event.message_str or "").strip()
        if not text.startswith("/"):
            search_match = re.fullmatch(r"永劫搜索(?:\s+(.*))?", text, re.S)
            if search_match is not None:
                async for message in self.search(event, (search_match.group(1) or "").strip()):
                    yield message
                return
            season_match = re.fullmatch(r"永劫赛季(?:\s+(.*))?", text, re.S)
            if season_match is not None:
                if not self._allowed_group(event):
                    return
                args = (season_match.group(1) or "").split()
                if len(args) > 3:
                    yield self._note(event, "用法：永劫赛季 昵称 [模式] [当前/全部/赛季名]")
                    return
                async for message in self.stats(event, *args):
                    yield message
                return
        if text.startswith("/"):
            text = text[1:].lstrip()
        match = re.fullmatch(r"(战绩查询|详细查询|战绩绑定|战绩解绑)(?:\s+(.*))?", text, re.S)
        if match is None:
            return
        if not self._allowed_group(event):
            return
        command = match.group(1)
        args = (match.group(2) or "").split()

        if command == "战绩解绑":
            if len(args) > 1:
                yield self._note(event, "用法：战绩解绑 昵称")
                return
            async for message in self._unbind(event, args[0] if args else ""):
                yield message
            return

        if len(args) > 2:
            yield self._note(event, f"用法：{command} 昵称 [模式]")
            return
        player = args[0] if args else ""
        mode = args[1] if len(args) == 2 else ""
        async for message in self._query_records(
            event, player, mode,
            detailed=command == "详细查询",
            force_bind=command == "战绩绑定",
        ):
            yield message

    # ==================== 解绑 ====================

    async def _unbind(self, event: AstrMessageEvent, nickname: str):
        if self.data_mode == "none":
            yield self._note(event, "当前为无绑定模式，无需解绑。")
            return
        if not nickname:
            yield self._note(event, "用法：战绩解绑 昵称")
            return
        if not self.logged_in:
            yield self._note(event, "尚未登录小黑盒，请管理员私聊发送 /永劫登录")
            return
        async with self._session() as session:
            ok, reason = await self._binding.manual_unbind(session, nickname, DEFAULT_SERVER)
        logger.info(
            f"[naraka] 手动解绑 sender={event.get_sender_id()} nickname={nickname!r} ok={ok}"
        )
        if ok:
            yield self._note(event, f"已解绑 {nickname}")
        else:
            yield self._note(event, f"{nickname} 解绑失败：{reason}")

    # ==================== 战绩查询 ====================

    async def _query_records(self, event: AstrMessageEvent, player: str, mode: str,
                             detailed: bool = False, force_bind: bool = False):
        if not self._allowed_group(event):
            return
        if not player:
            command = "详细查询" if detailed else "战绩查询"
            yield self._note(event, f"用法：{command} <昵称> [模式]；不填模式时自动选择最新一场排位对局的模式。")
            return
        if looks_like_role_id(player):
            yield self._note(
                event,
                "本插件不支持按角色ID查询，请改用昵称；发送 永劫搜索 昵称 可以核对是哪位玩家。",
            )
            return
        if mode:
            try:
                resolve_mode(mode)
            except ValueError as exc:
                yield self._note(event, str(exc))
                return

        # ★ 立刻回执：在冷却判定、登录检查、绑定兜底之前发出
        ack = await self._ack(event)
        if ack is not None:
            yield ack

        if self.data_mode == "none":
            if force_bind:
                yield self._note(event, "当前为无绑定模式，不支持战绩绑定；请在插件配置里切换数据模式。")
                return
            async for message in self._none_mode_query(event, player, detailed):
                yield message
            return

        if not self.logged_in:
            yield self._note(event, "尚未登录小黑盒，请管理员私聊发送 /永劫登录")
            return

        if self._query_lock.locked():
            yield self._note(event, "已有一份战绩正在查询，请稍后再试。")
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield self._note(event, f"查询过于频繁，请 {cooldown} 秒后再试。")
            return

        async with self._session() as session:
            try:
                await self._binding.recover(session, self._residual_interval_seconds())
            except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                logger.warning(f"[naraka] 残留恢复失败: {type(exc).__name__}")

        # ---- 阶段一：只读尝试（不占绑定锁）----
        report = _ReadResult(nickname=player)
        if not force_bind:
            async with self._query_lock:
                try:
                    report = await self._run_read(player, mode, detailed)
                except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    if _must_stop_after_error(exc):
                        yield self._note(event, f"查询失败：{self._error_text(exc)}")
                        return
                    report = _ReadResult(ok=False, reason=self._error_text(exc))
            if report.ok:
                for index, text in enumerate(report.texts):
                    yield await self._body(event, text, first=index == 0)
                return
            if not report.needs_bind:
                yield self._note(event, report.reason or "查询失败")
                return

        # ---- 阶段二：绑定 → 再搜 → 查询 → 解绑 ----
        nickname = report.nickname or player
        if not self._auto_bind():
            if force_bind:
                yield self._note(event, "自动绑定已关闭，请在插件配置里开启「自动绑定」后再试。")
            else:
                yield self._note(event, f"没有找到「{nickname}」，请用 永劫搜索 核对昵称。")
            return
        if self._binding.negative_hit(nickname.strip().lower()):
            yield self._note(
                event,
                f"「{nickname}」暂时没有可查数据，请稍后再试，或用 永劫搜索 核对昵称。",
            )
            return

        async with self._session() as session:
            async with self._binding.bound(session, nickname, DEFAULT_SERVER) as outcome:
                if not outcome.ok:
                    logger.info(
                        f"[naraka] 兜底绑定失败 nickname={nickname!r} "
                        f"state={outcome.state} reason={outcome.message}"
                    )
                    yield self._note(
                        event, self._bind_failure_text(nickname, outcome, force_bind)
                    )
                else:
                    try:
                        again = await self._run_read(player, mode, detailed)
                    except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                        again = _ReadResult(ok=False, reason=self._error_text(exc))
                    if again.ok:
                        for index, text in enumerate(again.texts):
                            yield await self._body(event, text, first=index == 0)
                    else:
                        self._binding.mark_negative(nickname.strip().lower())
                        reason = again.reason or "该昵称没有可展示的数据"
                        logger.info(
                            f"[naraka] 绑定后仍无数据 nickname={nickname!r} reason={reason}"
                        )
                        if force_bind:
                            yield self._note(event, f"绑定成功，查询失败：{reason}")
                        else:
                            yield self._note(
                                event,
                                f"搜索「{nickname}」成功，但没能取到数据，请稍后再试。",
                            )
            if not outcome.unbind_ok:
                yield self._note(event, f"{nickname} 解绑失败，请发送：战绩解绑 {nickname}")

    @staticmethod
    def _bind_failure_text(nickname: str, outcome, forced: bool) -> str:
        """绑定兜底失败时的提示文案。"""
        if forced:
            return f"绑定「{nickname}」失败：{outcome.message}，请稍后再试。"
        if outcome.state == "failed":
            return (
                f"没找到「{nickname}」。\n"
                f"· 昵称可能拼错，英文、符号要完全一致\n"
                f"· 也可能只是服务器排队繁忙\n"
                f"请发送 永劫搜索 {nickname} 核对昵称，或稍后再试。"
            )
        return f"搜索「{nickname}」失败，请稍后再试。"

    async def _none_mode_query(self, event: AstrMessageEvent, player: str, detailed: bool):
        """无绑定模式：只走免登录接口，战绩查询退化为「角色名片」。"""
        if detailed:
            yield self._note(event, "当前为无绑定模式，不支持单局详情；请在插件配置里切换数据模式。")
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield self._note(event, f"查询过于频繁，请 {cooldown} 秒后再试。")
            return
        try:
            async with self._session() as session:
                rows = await self._public_search_rows(session, player, limit=10)
        except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            yield self._note(event, f"查询失败：{self._error_text(exc)}")
            return
        if not rows:
            yield self._note(event, f"没有找到「{player}」，请用 永劫搜索 核对昵称。")
            return
        exact = [row for row in rows if row["role_name"] == player or row["role_id"] == player]
        candidates = exact or rows
        if len(candidates) != 1:
            yield self._note(event, f"未找到唯一玩家，请发送：永劫搜索 {player}")
            return
        row = candidates[0]
        yield await self._body(event, "\n".join(self._public_card_lines(row, player)), first=True)

    @staticmethod
    def _public_card_lines(row: dict, player: str) -> list[str]:
        """角色名片：昵称 + ID + 上游返回的全部列（不硬编码字段）。"""
        lines = [*format_identity(row.get("role_name") or player, row.get("role_id")),
                 f"服务器：{row.get('server') or '—'}"]
        for column in row.get("columns") or []:
            if column.get("type") == "user_info":
                continue  # 名字已经作为标题
            label = column.get("label") or "—"
            lines.append(f"{label}：{column.get('text') or '—'}")
        return lines

    @staticmethod
    def _public_row_line(row: dict) -> str:
        """免登录搜索结果的一行：昵称 + ID + 上游返回的全部列。"""
        extra = "｜".join(
            f"{c.get('label') or '—'} {c.get('text') or '—'}"
            for c in (row.get("columns") or [])
            if c.get("type") != "user_info"
        )
        return (f"{row.get('role_name') or '—'}｜ID {row.get('role_id') or '—'}"
                + (f"｜{extra}" if extra else ""))


    # ==================== 只读查询实现 ====================

    async def _public_search_rows(self, session, name: str, limit: int = 10) -> list[dict]:
        """免登录搜索（走统一节流，不带任何 cookie）。"""
        async with self._paced():
            return await self._api.search_public(session, name, limit=limit)

    async def _search(self, session, name: str) -> list[dict]:
        """登录态搜索（/game/yjwj/search）。只在免登录搜索没结果时作为回退。"""
        async with self._paced():
            return await self._api.search_users(session, name)

    async def _resolve_player(self, session, name: str) -> tuple[dict | None, str]:
        """昵称 → 角色。

        **优先走免登录搜索**（不带登录信息、不消耗账号额度），只有在它没能唯一确定角色时
        才回退到登录态搜索。返回值：

        * ``(row, "")``      —— 唯一确定，可直接使用
        * ``(None, "")``     —— 确实搜不到（调用方可以走绑定兜底）
        * ``(None, reason)`` —— 同名歧义等，``reason`` 直接展示给用户
        """
        def pick(rows: list[dict]) -> tuple[dict | None, bool]:
            exact = [row for row in rows if row.get("role_name") == name]
            if len(exact) == 1:
                return exact[0], False
            if not exact and len(rows) == 1:
                return rows[0], False
            return None, bool(rows)

        try:
            public_rows = await self._public_search_rows(session, name, limit=20)
        except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(f"[naraka] 免登录搜索失败: {type(exc).__name__}")
            public_rows = []

        row, ambiguous = pick(public_rows)
        if row is not None:
            return row, ""

        if self.logged_in:
            if not self._login_search_fallback():
                logger.info(
                    f"[naraka] 免登录搜索没命中 nickname={name!r}；"
                    f"登录搜索回退已关闭（严格不带登录信息）"
                )
            else:
                try:
                    found = await self._search(session, name)
                except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    logger.warning(f"[naraka] 登录搜索回退失败: {type(exc).__name__}")
                    found = []
                if found:
                    logger.info(
                        f"[naraka] 免登录搜索没命中、登录搜索命中 nickname={name!r} "
                        f"（说明两个索引不一致；本次未触发绑定）"
                    )
                    row, ambiguous = pick(found)
                    if row is not None:
                        return row, ""
                    return None, "未找到唯一玩家，请先用 永劫搜索 昵称 核对是哪位玩家。"

        if ambiguous:
            return None, "未找到唯一玩家，请先用 永劫搜索 昵称 核对是哪位玩家。"
        logger.info(f"[naraka] 两个搜索通道都没命中 nickname={name!r} → 触发绑定兜底")
        return None, ""

    async def _run_read(self, player: str, mode: str, detailed: bool) -> _ReadResult:
        """只读查询：定位角色 → 更新 → 取列表 → 取详情 → 出报告。"""
        async with self._session() as session:
            selected, reason = await self._resolve_player(session, player)
            if selected is None:
                if reason:
                    return _ReadResult(ok=False, reason=reason)
                return _ReadResult(ok=False, needs_bind=True, nickname=player)

            role_id = str(selected.get("role_id") or "")
            if not role_id:
                raise ValueError("玩家搜索结果缺少角色ID")
            server = str(selected.get("server") or DEFAULT_SERVER)
            name = str(selected.get("role_name") or player)
            common = {
                "server": server,
                "role_id": role_id,
                "heybox_id": str(
                    self._auth.get("uid")
                    or self._auth.get("cookies", {}).get("heybox_id")
                    or ""
                ),
            }

            if self._refresh_enabled():
                await self._refresh(session, role_id, server)

            query_limit = self._count_setting("query_match_count", 30)
            selection_limit = self._count_setting("selected_match_count", 10)
            rows = await self._fetch_recent_pool(session, common, query_limit)
            if not rows:
                return _ReadResult(ok=False, role_id=role_id, server=server, nickname=name,
                                   reason=f"{name}：查询到对局 0 场。")

            mode_id, matches = select_matches(rows, mode, selection_limit)
            if mode_id is None:
                return _ReadResult(
                    ok=False, role_id=role_id, server=server, nickname=name,
                    reason=f"查询到对局 {len(rows)} 场；排除快速等非排位模式后，没有可自动选择的天人或天选对局。",
                )
            if not matches:
                return _ReadResult(
                    ok=False, role_id=role_id, server=server, nickname=name,
                    reason=f"查询到对局 {len(rows)} 场；其中没有{MODES[mode_id]}对局。",
                )

            detail_enabled = self.config.get("enable_detail_query", True)
            records = []
            failures = 0
            stopped = False
            if not detail_enabled:
                # 详情开关关闭：不调单局详情接口，用空 detail 只展示列表级数据
                for row in matches:
                    records.append(make_record(row, {}))
            else:
                for row in matches:
                    match_id = str(row["match_id"])
                    cache_key = (role_id, match_id)
                    detail = self._detail_cache.get(cache_key)
                    if detail is None:
                        try:
                            detail = await self._fetch_detail(session, common, row)
                            if len(self._detail_cache) >= 1000:
                                self._detail_cache.pop(next(iter(self._detail_cache)))
                            self._detail_cache[cache_key] = detail
                        except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                            failures += 1
                            if _must_stop_after_error(exc):
                                stopped = True
                                break
                            logger.warning(f"[naraka] detail failed: {type(exc).__name__}")
                            continue
                    records.append(make_record(row, detail))

            hero_names: dict[str, str] = dict(KNOWN_HERO_NAMES)
            scores: dict[str, str] = {}
            if not stopped:
                try:
                    home = await self._home_data(session, common, "12", "pre-01")
                    hero_names.update({
                        str(item["hero_id"]): str(item["name"])
                        for item in home.get("heroes", [])
                        if isinstance(item, dict) and item.get("hero_id") and item.get("name")
                    })
                    season_key, _ = _choose_season(home.get("seasons"), "当前")
                except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    logger.warning(f"[naraka] hero catalog failed: {type(exc).__name__}")
                    season_key = "pre-01"
                    if _must_stop_after_error(exc):
                        stopped = True
                if not detailed and not stopped and season_key != "pre-01":
                    for ranked_id in ("5000000", "12", "5000001", "4", "13", "5"):
                        try:
                            season_body = await self._home_data(session, common, ranked_id, season_key)
                            rating = str((season_body.get("player_info") or {}).get("rating") or "")
                            if rating.isdigit() and int(rating) > 0:
                                scores[ranked_id] = rating
                        except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                            logger.warning(f"[naraka] season mode failed: {type(exc).__name__}")
                            if _must_stop_after_error(exc):
                                stopped = True
                                break

        hero_names = apply_hero_mappings(hero_names, records, self.config.get("hero_mappings", []))
        total = len(rows)
        if detailed and not self.config.get("enable_detail_query", True):
            return _ReadResult(
                ok=False, role_id=role_id, server=server, nickname=name,
                reason="单局详情查询已在插件配置中关闭。",
            )
        if detailed:
            report = format_details(name, total, mode_id, len(matches), records, hero_names,
                                    failures, role_id=role_id)
            texts = split_detail_report(report)
        else:
            report = format_summary(name, total, mode_id, len(matches), records, scores,
                                    hero_names, failures, role_id=role_id)
            texts = [report]
        if stopped:
            texts[-1] = texts[-1] + "\n小黑盒要求重新登录或限制请求，已停止后续查询。"
        return _ReadResult(ok=True, texts=texts, role_id=role_id, server=server, nickname=name)

    async def _refresh(self, session, role_id: str, server: str) -> None:
        """「更新数据」按钮：非致命，失败只记日志。"""
        try:
            async with self._paced():
                body = await self._api.update(session, role_id, server)
        except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning(f"[naraka] update failed: {type(exc).__name__}")
            return
        state = str(body.get("state") or "")
        for _ in range(3):
            if state not in ("waiting", "updating"):
                break
            await asyncio.sleep(self._update_retry_interval())
            try:
                async with self._paced():
                    body = await self._api.update(session, role_id, server)
            except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                logger.warning(f"[naraka] update retry failed: {type(exc).__name__}")
                return
            state = str(body.get("state") or "")
        if state == "failed":
            logger.info(f"[naraka] update reported failed: {body.get('btn_desc') or ''}")

    async def _fetch_recent_pool(self, session, common: dict, limit: int) -> list[dict]:
        """Read successive mixed pages; stop if upstream repeats a page.

        分页参数由配置「对局列表分页参数」决定：offset（limit/offset，默认）或 page（page/page_size）。
        """
        seen: dict[str, dict] = {}
        page_size = 30
        offset = 0
        pagination = self._pagination_mode()
        while len(seen) < limit:
            async with self._paced():
                body = await self._api.match_list(
                    session,
                    common["role_id"],
                    server=common["server"],
                    page=offset // page_size + 1,
                    page_size=page_size,
                    limit=page_size,
                    offset=offset,
                    pagination=pagination,
                    battle_tid="12",
                    season="pre-01",
                    heybox_id=common.get("heybox_id", ""),
                )
            batch = recent_rows(body, limit)
            added = 0
            for row in batch:
                match_id = str(row.get("match_id") or "")
                if match_id and match_id not in seen:
                    seen[match_id] = row
                    added += 1
            if not added:
                break
            offset += page_size
        return recent_rows({"match_list": list(seen.values())}, limit)

    async def _fetch_detail(self, session, common: dict, row: dict) -> dict:
        match_id = str(row["match_id"])
        async with self._paced(detail=True):
            payload = await self._api.match_detail(
                session,
                match_id,
                role_id=common["role_id"],
                server=common["server"],
                battle_tid=str(row.get("battle_tid") or ""),
                scene=str(row.get("scene") or ""),
                heybox_id=common.get("heybox_id", ""),
            )
        if not isinstance(payload.get("data"), list):
            raise ValueError("单局详情缺少数据")
        return payload

    async def _home_data(self, session, common: dict, battle_tid: str, season: str) -> dict:
        async with self._paced():
            return await self._api.home_data(
                session,
                common["role_id"],
                server=common["server"],
                season=season,
                battle_tid=battle_tid,
                heybox_id=common.get("heybox_id", ""),
            )

    # ==================== 赛季总览 ====================

    @filter.command("永劫赛季")
    async def stats(self, event: AstrMessageEvent, player: str = "", mode: str = "", season: str = "当前"):
        if not self._allowed_group(event):
            return
        if not player:
            yield self._note(event, "用法：永劫赛季 昵称 [模式] [当前/全部/赛季名]")
            return
        if looks_like_role_id(player):
            yield self._note(
                event,
                "本插件不支持按角色ID查询，请改用昵称；发送 永劫搜索 昵称 可以核对是哪位玩家。",
            )
            return
        mode_id = None
        if mode:
            try:
                mode_id = resolve_mode(mode)
            except ValueError as exc:
                yield self._note(event, str(exc))
                return
            if mode_id not in RANKED_IDS:
                yield self._note(event, "赛季总览只支持天人和天选的单排、双排、三排。")
                return
        # ★ 立刻回执：在模式/登录/锁/冷却判定之前发出
        ack = await self._ack(event)
        if ack is not None:
            yield ack
        if self.data_mode == "none":
            yield self._note(event, "当前为无绑定模式，不支持赛季数据；请在插件配置里切换数据模式。")
            return
        if not self.logged_in:
            yield self._note(event, "尚未登录小黑盒，请管理员私聊发送 /永劫登录")
            return
        if self._query_lock.locked():
            yield self._note(event, "已有一份战绩正在查询，请稍后再试。")
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield self._note(event, f"查询过于频繁，请 {cooldown} 秒后再试。")
            return
        async with self._query_lock:
            async for message in self._season_report(event, player, mode_id, season):
                yield message

    async def _season_report(self, event: AstrMessageEvent, player: str, mode_id: str | None, season: str):
        try:
            async with self._session() as session:
                selected, reason = await self._resolve_player(session, player)
                if selected is None:
                    yield self._note(event, reason or f"没有找到「{player}」。")
                    return
                role_id = str(selected.get("role_id") or "")
                name = str(selected.get("role_name") or player)
                server = str(selected.get("server") or DEFAULT_SERVER)
                if not role_id:
                    raise ValueError("搜索结果缺少角色ID")
                common = {
                    "server": server,
                    "role_id": role_id,
                    "heybox_id": str(
                        self._auth.get("uid")
                        or self._auth.get("cookies", {}).get("heybox_id")
                        or ""
                    ),
                }
                if mode_id is None:
                    rows = await self._fetch_recent_pool(
                        session, common, self._count_setting("query_match_count", 30)
                    )
                    mode_id, _ = select_matches(rows, "", 1)
                    if mode_id is None:
                        yield self._note(event, "近期对局中没有可自动选择的天人或天选模式，请指定模式后重试。")
                        return
                all_seasons = await self._home_data(session, common, mode_id, "pre-01")
                season_key, season_name = _choose_season(all_seasons.get("seasons"), season)
                if season_key == "pre-01":
                    body = all_seasons
                else:
                    body = await self._home_data(session, common, mode_id, season_key)

            info = body.get("player_info") or {}
            overview = body.get("overview") or []
            returned_mode = str(body.get("battle_tid") or mode_id)
            mode_name = MODES.get(returned_mode, f"模式 {returned_mode}")
            if isinstance(info, dict) and _has_value(info.get("name")):
                name = str(info["name"])
            lines = [*format_identity(name, role_id), f"{mode_name}｜{season_name}"]
            if isinstance(info, dict):
                for key, label in (("rating", "分数"), ("level", "段位"), ("lv", "等级")):
                    if _has_value(info.get(key)):
                        lines.append(f"{label}：{info[key]}")
            if isinstance(overview, list):
                for item in overview:
                    if isinstance(item, dict) and _has_value(item.get("desc")) and _has_value(item.get("value")):
                        lines.append(f"{item['desc']}：{item['value']}")
            for index, chunk in enumerate(_split_lines(lines)):
                yield await self._body(event, chunk, first=index == 0)
        except (HeyboxError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            yield self._note(event, f"查询失败：{self._error_text(exc)}")
        except ValueError as exc:
            yield self._note(event, f"查询失败：{exc}")
