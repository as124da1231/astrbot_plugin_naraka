"""QQ commands for looking up NARAKA PC records through Xiaoheihe.

The upstream API is unofficial and requires a Xiaoheihe login.  Keep the
credentials in AstrBot's plugin data directory; never print them to chat.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
import astrbot.api.message_components as Comp

from .xiaoheihe_login import (
    LoginState,
    WEB_CLIENT_PARAMS,
    XiaoheiheLoginClient,
    _sign_params,
    generate_qr_png,
)
from .record_query import KNOWN_HERO_NAMES, MODES, RANKED_IDS, apply_hero_mappings, format_details, format_summary, make_record, recent_rows, resolve_mode, select_matches, split_detail_report

API = "https://api.xiaoheihe.cn"
PLUGIN = "astrbot_plugin_naraka"
HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.xiaoheihe.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}

def _is_rate_limited(value: object) -> bool:
    message = str(value).lower()
    return "频繁" in message or "429" in message or "rate limit" in message


def _result(payload: dict) -> dict:
    if payload.get("status") == "login":
        raise ValueError("小黑盒登录已失效，请管理员重新发送 /永劫登录")
    if payload.get("status") not in (None, "ok", "success"):
        raise ValueError(str(payload.get("msg") or "小黑盒接口返回错误"))
    body = payload.get("result")
    if not isinstance(body, dict):
        raise ValueError("小黑盒返回格式已变化")
    return body


def _rows(body: dict) -> list[dict]:
    items = body.get("user_list")
    if not isinstance(items, list):
        raise ValueError("玩家搜索返回格式已变化")
    return [row for row in items if isinstance(row, dict)]


def _text(value: object) -> str:
    return str(value) if value is not None else "—"


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


def _must_stop_after_error(exc: Exception) -> bool:
    return (
        _is_rate_limited(exc)
        or isinstance(exc, aiohttp.ClientResponseError) and exc.status in (401, 403, 429)
        or "登录" in str(exc)
    )


def _is_role_id(value: str) -> bool:
    """Recognize PC role IDs observed from different game accounts."""
    return value.isdigit() or bool(
        re.fullmatch(r"(?:g[0-9a-f]|uk[0-9a-z]|psrc[0-9a-z])[0-9a-z]{15,63}", value, re.I)
    )


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


@register(PLUGIN, "as124da1231", "永劫无间端游战绩查询", "1.0.9")
class NarakaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._path = Path(StarTools.get_data_dir(PLUGIN)) / "login.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._login_lock = asyncio.Lock()
        self._query_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_query_started_at: float | None = None
        self._last_detail_request_at: float | None = None
        self._detail_cache: dict[tuple[str, str], dict] = {}
        self._auth = self._load_auth()

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

    async def _fetch_recent_pool(self, common: dict[str, str], limit: int,
                                 session: aiohttp.ClientSession | None) -> list[dict]:
        """Read successive mixed pages; stop if upstream repeats a page."""
        seen: dict[str, dict] = {}
        page = 1
        while len(seen) < limit:
            body = await self._post("/game/yjwj/match/list", {
                **common, "battle_tid": "12", "season": "pre-01",
                "page": str(page), "page_size": "30",
            }, session)
            batch = recent_rows(body, limit)
            added = 0
            for row in batch:
                match_id = str(row.get("match_id") or "")
                if match_id and match_id not in seen:
                    seen[match_id] = row
                    added += 1
            if not added:
                break
            page += 1
        return recent_rows({"match_list": list(seen.values())}, limit)

    def _allowed_group(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat() or not self.config.get("enable_group_whitelist", False):
            return True
        allowed = self.config.get("group_whitelist", [])
        if not isinstance(allowed, list):
            return False
        return str(event.get_group_id() or "") in {str(item).strip() for item in allowed}

    def _reply(self, event: AstrMessageEvent, message: str, method: str | None = None):
        method = method or str(self.config.get("result_reply_method", "mention"))
        if method == "quote":
            message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
            if message_id is not None and str(message_id).strip():
                return event.chain_result([Comp.Reply(id=str(message_id)), Comp.Plain(message)])
        if not event.is_private_chat():
            sender_id = str(event.get_sender_id() or "")
            if sender_id:
                return event.chain_result([Comp.At(qq=sender_id), Comp.Plain("\u200b" + message)])
        return event.plain_result(message)

    async def _react_to_msg(self, event: AstrMessageEvent) -> None:
        """Attach a QQ reaction to the triggering message; never block the query."""
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
            if message_id is None or not str(message_id).strip():
                return
            await event.bot.api.call_action(
                "set_msg_emoji_like",
                message_id=message_id,
                emoji_id=str(self.config.get("react_emoji_id", "277") or "277"),
            )
        except Exception as exc:
            logger.warning(f"[naraka] QQ reaction failed: {type(exc).__name__}")

    async def _start_notice(self, event: AstrMessageEvent):
        if not self.config.get("enable_start_notice", False):
            return None
        text = str(self.config.get("start_notice_text", "正在查询战绩，请稍候…") or "").strip()
        method = str(self.config.get("start_notice_method", "quote"))
        if method == "reaction":
            await self._react_to_msg(event)
            return None
        return self._reply(event, text, method=method) if text else None

    def _load_auth(self) -> dict:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, ValueError):
            return {}

    def _save_auth(self, value: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)
        self._auth = value

    async def _post_raw(self, path: str, params: dict[str, str], session: aiohttp.ClientSession | None = None) -> dict:
        if not self._auth.get("cookies"):
            raise ValueError("尚未登录小黑盒，请管理员私聊发送 /永劫登录")
        client = XiaoheiheLoginClient(self._auth.get("device_id", ""))
        query = _sign_params(path, {**WEB_CLIENT_PARAMS, **params}, client.device_id)
        cookies = self._auth["cookies"]
        timeout = aiohttp.ClientTimeout(total=15)
        if session is None:
            async with aiohttp.ClientSession(timeout=timeout) as own_session:
                return await self._post_raw(path, params, own_session)
        async with self._request_lock:
            now = time.monotonic()
            remaining = 0.0
            is_detail = path == "/game/yjwj/match/detail"
            if is_detail and self._last_detail_request_at is not None:
                detail_interval = self._number_setting("detail_interval_seconds", 4.0, 0.0, 120.0)
                remaining = max(remaining, detail_interval - (now - self._last_detail_request_at))
            if remaining > 0:
                await asyncio.sleep(remaining)
            try:
                async with session.post(API + path, params=query, headers=HEADERS, cookies=cookies, timeout=timeout) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
            finally:
                if is_detail:
                    self._last_detail_request_at = time.monotonic()
        if not isinstance(payload, dict):
            raise ValueError("小黑盒未返回有效数据")
        return payload

    async def _post(self, path: str, params: dict[str, str], session: aiohttp.ClientSession | None = None) -> dict:
        return _result(await self._post_raw(path, params, session))

    async def _search(self, name: str, session: aiohttp.ClientSession | None = None) -> list[dict]:
        return _rows(await self._post("/game/yjwj/search", {"q": name}, session))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("永劫登录")
    async def login(self, event: AstrMessageEvent):
        if not event.is_private_chat() and not self.config.get("allow_group_login_qr", False):
            yield event.plain_result("请私聊机器人发送 /永劫登录；如需在群里扫码，请先开启「允许登录二维码发到群里」。")
            return
        if self._login_lock.locked():
            yield event.plain_result("已有登录流程正在等待扫码。")
            return
        async with self._login_lock:
            try:
                client = XiaoheiheLoginClient()
                qr = await client.request_qr()
                yield event.chain_result([
                    Comp.Plain("请用小黑盒 App 扫码并确认；二维码约 2 分钟后失效。"),
                    Comp.Image.fromBytes(generate_qr_png(qr.qr_content)),
                ])
                until = min(qr.expires_at, time.time() + 120)
                while time.time() < until:
                    await asyncio.sleep(3)
                    state = await client.check_qr(qr)
                    if state.state == LoginState.SUCCESS:
                        if not state.cookies:
                            raise ValueError("扫码成功，但没有收到登录凭证")
                        self._save_auth({
                            "cookies": state.cookies,
                            "uid": state.uid,
                            "nickname": state.nickname,
                            "device_id": client.device_id,
                        })
                        yield event.plain_result("小黑盒登录成功。现在可以发送 /永劫搜索 <昵称>。")
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
        self._path.unlink(missing_ok=True)
        yield event.plain_result("已清除本地小黑盒登录信息。")

    @filter.command("永劫搜索")
    async def search(self, event: AstrMessageEvent, nickname: str = ""):
        if not self._allowed_group(event):
            return
        if not nickname:
            yield self._reply(event, "用法：永劫搜索 玩家昵称")
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield self._reply(event, f"查询过于频繁，请 {cooldown} 秒后再试。")
            return
        try:
            rows = await self._search(nickname)
            if not rows:
                yield self._reply(event, f"没有找到「{nickname}」。")
                return
            lines = [f"搜索「{nickname}」："]
            for row in rows[:10]:
                lines.append(
                    f"{_text(row.get('role_name'))}｜角色ID {_text(row.get('role_id'))}｜"
                    f"等级 {_text(row.get('level'))}｜分数 {_text(row.get('rank_score'))}"
                )
            lines.append(f"查近期对局：战绩查询 {nickname}（同名玩家请填角色ID）")
            yield self._reply(event, "\n".join(lines))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            yield self._reply(event, f"查询失败：{exc}")

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
                    yield self._reply(event, "用法：永劫赛季 昵称或角色ID [模式] [当前/全部/赛季名]")
                    return
                async for message in self.stats(event, *args):
                    yield message
                return
        if text.startswith("/"):
            text = text[1:].lstrip()
        match = re.fullmatch(r"(战绩查询|详细查询|永劫战绩)(?:\s+(.*))?", text, re.S)
        if match is None:
            return
        if not self._allowed_group(event):
            return
        args = (match.group(2) or "").split()
        if len(args) > 2:
            yield self._reply(event, "用法：战绩查询或详细查询 昵称或角色ID [模式]")
            return
        player = args[0] if args else ""
        mode = args[1] if len(args) == 2 else ""
        async for message in self._query_records(event, player, mode, detailed=match.group(1) == "详细查询"):
            yield message

    async def query_records(self, event: AstrMessageEvent, player: str = "", mode: str = ""):
        async for message in self._query_records(event, player, mode):
            yield message

    async def query_records_alias(self, event: AstrMessageEvent, player: str = "", mode: str = ""):
        async for message in self._query_records(event, player, mode):
            yield message

    async def query_match_details(self, event: AstrMessageEvent, player: str = "", mode: str = ""):
        async for message in self._query_records(event, player, mode, detailed=True):
            yield message

    async def _query_records(self, event: AstrMessageEvent, player: str, mode: str, detailed: bool = False):
        if not self._allowed_group(event):
            return
        if not player:
            command = "详细查询" if detailed else "战绩查询"
            yield self._reply(event, f"用法：{command} <昵称或角色ID> [模式]；不填模式时自动选择最新一场排位对局的模式。")
            return
        if mode:
            try:
                resolve_mode(mode)
            except ValueError as exc:
                yield self._reply(event, str(exc))
                return
        if self._query_lock.locked():
            yield self._reply(event, "已有一份战绩正在查询，请稍后再试。")
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield self._reply(event, f"查询过于频繁，请 {cooldown} 秒后再试。")
            return
        async with self._query_lock:
            notice = await self._start_notice(event)
            if notice is not None:
                yield notice
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
                    if _is_role_id(player):
                        selected = {"role_id": player, "role_name": player, "server": "163"}
                    else:
                        found = await self._search(player, session)
                        exact = [row for row in found if row.get("role_name") == player]
                        candidates = exact or found
                        if len(candidates) != 1:
                            yield self._reply(event, "未找到唯一玩家，请先用 /永劫搜索 昵称 查看角色ID。")
                            return
                        selected = candidates[0]
                    role_id = str(selected.get("role_id") or "")
                    if not role_id:
                        raise ValueError("玩家搜索结果缺少角色ID")
                    common = {
                        "server": str(selected.get("server") or "163"),
                        "role_id": role_id,
                        "heybox_id": str(self._auth.get("uid") or self._auth.get("cookies", {}).get("heybox_id") or ""),
                    }
                    query_limit = self._count_setting("query_match_count", 30)
                    selection_limit = self._count_setting("selected_match_count", 10)
                    rows = await self._fetch_recent_pool(common, query_limit, session)
                    total = len(rows)
                    if total == 0:
                        yield self._reply(event, f"{selected.get('role_name') or player}：查询到对局 0 场。")
                        return
                    mode_id, matches = select_matches(rows, mode, selection_limit)
                    if mode_id is None:
                        yield self._reply(event, f"查询到对局 {total} 场；排除快速等非排位模式后，没有可自动选择的天人或天选对局。")
                        return
                    if not matches:
                        yield self._reply(event, f"查询到对局 {total} 场；其中没有{MODES[mode_id]}对局。")
                        return
                    records = []
                    failures = 0
                    stopped = False
                    for row in matches:
                        match_id = str(row["match_id"])
                        cache_key = (role_id, match_id)
                        detail = self._detail_cache.get(cache_key)
                        if detail is None:
                            try:
                                payload = await self._post_raw("/game/yjwj/match/detail", {
                                    **common,
                                    "match_id": match_id,
                                    "battle_tid": str(row.get("battle_tid") or ""),
                                    "scene": str(row.get("scene") or ""),
                                }, session)
                                detail = _result(payload)
                                if not isinstance(detail.get("data"), list):
                                    raise ValueError("单局详情缺少数据")
                                if len(self._detail_cache) >= 1000:
                                    self._detail_cache.pop(next(iter(self._detail_cache)))
                                self._detail_cache[cache_key] = detail
                            except Exception as exc:
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
                            home = await self._post("/game/yjwj/home/data", {
                                **common, "battle_tid": "12", "season": "pre-01",
                            }, session)
                            hero_names.update({
                                str(item["hero_id"]): str(item["name"])
                                for item in home.get("heroes", [])
                                if isinstance(item, dict) and item.get("hero_id") and item.get("name")
                            })
                            season_key, _ = _choose_season(home.get("seasons"), "当前")
                        except Exception as exc:
                            logger.warning(f"[naraka] hero catalog failed: {type(exc).__name__}")
                            season_key = "pre-01"
                            if _must_stop_after_error(exc):
                                stopped = True
                    if not detailed and not stopped and season_key != "pre-01":
                        for ranked_id in ("5000000", "12", "5000001", "4", "13", "5"):
                            try:
                                season_body = await self._post("/game/yjwj/home/data", {
                                    **common, "battle_tid": ranked_id, "season": season_key,
                                }, session)
                                rating = str((season_body.get("player_info") or {}).get("rating") or "")
                                if rating.isdigit() and int(rating) > 0:
                                    scores[ranked_id] = rating
                            except Exception as exc:
                                logger.warning(f"[naraka] season mode failed: {type(exc).__name__}")
                                if _must_stop_after_error(exc):
                                    stopped = True
                                    break
                hero_names = apply_hero_mappings(hero_names, records, self.config.get("hero_mappings", []))
                name = str(selected.get("role_name") or player)
                if detailed:
                    report = format_details(name, total, mode_id, len(matches), records, hero_names, failures)
                else:
                    report = format_summary(name, total, mode_id, len(matches), records, scores, hero_names, failures)
                if stopped:
                    report += "\n小黑盒要求重新登录或限制请求，已停止后续查询。"
                if detailed:
                    for chunk in split_detail_report(report):
                        yield self._reply(event, chunk)
                else:
                    yield self._reply(event, report)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                logger.warning(f"[naraka] record query failed: {type(exc).__name__}")
                if isinstance(exc, ValueError):
                    yield self._reply(event, f"查询失败：{exc}")
                else:
                    yield self._reply(event, "查询失败：小黑盒网络请求异常，请稍后重试。")

    @filter.command("永劫赛季")
    async def stats(self, event: AstrMessageEvent, player: str = "", mode: str = "", season: str = "当前"):
        if not self._allowed_group(event):
            return
        if not player:
            yield self._reply(event, "用法：永劫赛季 昵称或角色ID [天人单排/双排/三排或天选单排/双排/三排] [当前/全部/赛季名]")
            return
        if mode in ("单排", "双排", "三排"):
            mode = "天选" + mode
        mode_id = None
        if mode:
            try:
                mode_id = resolve_mode(mode)
            except ValueError as exc:
                yield self._reply(event, str(exc))
                return
            if mode_id not in RANKED_IDS:
                yield self._reply(event, "赛季总览只支持天人和天选的单排、双排、三排。")
                return
        if self._query_lock.locked():
            yield self._reply(event, "已有一份战绩正在查询，请稍后再试。")
            return
        cooldown = self._start_query_cooldown()
        if cooldown:
            yield self._reply(event, f"查询过于频繁，请 {cooldown} 秒后再试。")
            return
        async with self._query_lock:
            notice = await self._start_notice(event)
            if notice is not None:
                yield notice
            async for message in self._season_report(event, player, mode_id, season):
                yield message

    async def _season_report(self, event: AstrMessageEvent, player: str, mode_id: str | None, season: str):
        try:
            if _is_role_id(player):
                role_id, name, server = player, player, "163"
            else:
                matches = await self._search(player)
                exact_matches = [r for r in matches if r.get("role_name") == player]
                if len(exact_matches) == 1:
                    exact = exact_matches[0]
                elif not exact_matches:
                    if len(matches) != 1:
                        yield self._reply(event, "未找到唯一玩家，请先用 /永劫搜索 昵称 查看角色ID。")
                        return
                    exact = matches[0]
                else:
                    yield self._reply(event, "存在同名玩家，请先用 /永劫搜索 昵称 查看角色ID。")
                    return
                role_id = str(exact.get("role_id") or "")
                name = str(exact.get("role_name") or player)
                server = str(exact.get("server") or "163")
            if not role_id:
                raise ValueError("搜索结果缺少角色ID")
            if mode_id is None:
                common = {
                    "server": server,
                    "role_id": role_id,
                    "heybox_id": str(self._auth.get("uid") or self._auth.get("cookies", {}).get("heybox_id") or ""),
                }
                rows = await self._fetch_recent_pool(common, self._count_setting("query_match_count", 30), None)
                mode_id, _ = select_matches(rows, "", 1)
                if mode_id is None:
                    yield self._reply(event, "近期对局中没有可自动选择的天人或天选模式，请指定模式后重试。")
                    return
            # The archived Xiaoheihe client identifies Tianxuan duo as 12.
            params = {
                "server": server,
                "role_id": role_id,
                "battle_tid": mode_id,
                "season": "pre-01",
                "heybox_id": str(self._auth.get("uid") or self._auth.get("cookies", {}).get("heybox_id") or ""),
            }
            all_seasons = await self._post("/game/yjwj/home/data", params)
            season_key, season_name = _choose_season(all_seasons.get("seasons"), season)
            if season_key == "pre-01":
                body = all_seasons
            else:
                body = await self._post("/game/yjwj/home/data", {**params, "season": season_key})
            info = body.get("player_info") or {}
            overview = body.get("overview") or []
            returned_mode = str(body.get("battle_tid") or mode_id)
            mode_name = MODES.get(returned_mode, f"模式 {returned_mode}")
            if isinstance(info, dict) and _has_value(info.get("name")):
                name = str(info["name"])
            lines = [f"{name}（角色ID {role_id}）", f"{mode_name}｜{season_name}"]
            if isinstance(info, dict):
                for key, label in (("rating", "分数"), ("level", "段位"), ("lv", "等级")):
                    if _has_value(info.get(key)):
                        lines.append(f"{label}：{info[key]}")
            if isinstance(overview, list):
                for item in overview:
                    if isinstance(item, dict) and _has_value(item.get("desc")) and _has_value(item.get("value")):
                        lines.append(f"{item['desc']}：{item['value']}")
            for chunk in _split_lines(lines):
                yield self._reply(event, chunk)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            yield self._reply(event, f"查询失败：{exc}")
