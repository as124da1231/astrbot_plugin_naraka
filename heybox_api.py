"""小黑盒接口层：请求签名 + 全部接口封装。

接口清单、参数与验证结论见仓库内 `API.md` / `三方实现接口对比.md` / `参数审计.md`。

关键事实（实测）：
* 签名 `hkey` 只取决于（路径, 秒级时间戳, nonce），与 query 参数、cookie 无关。
* `/account/*` 是 **GET-only**（POST 返回 405），且会校验 `_time` / `hkey` / `nonce` / `os_type` / `version` / `x_app`。
* `/game/*` 两种方法都接受。
* `/game/player_search/do` **不需要登录**，可拿到昵称 → role_id + 境界/总积分/场次。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from typing import Any

import aiohttp

BASE_URL = "https://api.xiaoheihe.cn"

# 小黑盒 Web 端固定请求参数
WEB_CLIENT_PARAMS = {
    "os_type": "web",
    "app": "web",
    "client_type": "web",
    "version": "999.0.4",
    "web_version": "2.5",
    "x_client_type": "web",
    "x_app": "heybox_website",
    "x_os_type": "Windows",
    "device_info": "Chrome",
    "_notip": "true",
}

HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.xiaoheihe.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

_HKEY_ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"  # gitleaks:allow

DEFAULT_SERVER = "163"
GAME_TYPE = "yjwj"


# ==================== 异常 ====================


class HeyboxError(Exception):
    """接口返回了业务错误。"""


class HeyboxLoginRequired(HeyboxError):
    """登录态失效或未登录。"""


class HeyboxRateLimited(HeyboxError):
    """被上游限流。"""


class HeyboxNetworkError(HeyboxError):
    """网络层异常。"""


# ==================== 请求签名 ====================


def _xtime(v: int) -> int:
    return (255 & ((v << 1) ^ 27)) if v & 128 else v << 1


def _mul3(v: int) -> int:
    return _xtime(v) ^ v


def _mul4(v: int) -> int:
    return _mul3(_xtime(v))


def _mul8(v: int) -> int:
    return _mul4(_mul3(_xtime(v)))


def _mul14(v: int) -> int:
    return _mul8(v) ^ _mul4(v) ^ _mul3(v)


def _mix_tail(values: list[int]) -> list[int]:
    a, b, c, d = values[:4]
    return [
        _mul14(a) ^ _mul8(b) ^ _mul4(c) ^ _mul3(d),
        _mul3(a) ^ _mul14(b) ^ _mul8(c) ^ _mul4(d),
        _mul4(a) ^ _mul3(b) ^ _mul14(c) ^ _mul8(d),
        _mul8(a) ^ _mul4(b) ^ _mul3(c) ^ _mul14(d),
        *values[4:],
    ]


def _map_to_alphabet(value: str, alphabet: str) -> str:
    return "".join(alphabet[ord(c) % len(alphabet)] for c in value)


def generate_hkey(path: str, timestamp: int, nonce: str) -> str:
    """生成上游要求的 hkey 签名参数。"""
    normalized_path = f"/{'/'.join(p for p in str(path).split('/') if p)}/"
    parts = (
        _map_to_alphabet(str(timestamp), _HKEY_ALPHABET[:-2]),
        _map_to_alphabet(normalized_path, _HKEY_ALPHABET),
        _map_to_alphabet(str(nonce), _HKEY_ALPHABET),
    )
    interleaved = "".join(
        part[i]
        for i in range(max(len(p) for p in parts))
        for part in parts
        if i < len(part)
    )[:20]
    digest = hashlib.md5(interleaved.encode(), usedforsecurity=False).hexdigest()
    mixed = _mix_tail([ord(c) for c in digest[-6:]])
    suffix = str(sum(mixed) % 100).zfill(2)
    prefix = _map_to_alphabet(digest[:5], _HKEY_ALPHABET[:-4])
    return f"{prefix}{suffix}"


def sign_params(
    path: str,
    base_params: dict[str, str] | None = None,
    device_id: str = "",
) -> dict[str, str]:
    """为指定路径添加 _time、nonce、hkey 签名参数。"""
    params = dict(base_params or {})
    timestamp = int(time.time())
    nonce = hashlib.md5(
        f"{timestamp}{secrets.token_hex(16)}".encode(), usedforsecurity=False
    ).hexdigest().upper()
    params["_time"] = str(timestamp)
    params["nonce"] = nonce
    params["hkey"] = generate_hkey(path, timestamp, nonce)
    if device_id:
        params["device_id"] = device_id
    return params


# ==================== 响应处理 ====================


def _rate_limited(message: str) -> bool:
    lowered = message.lower()
    return "频繁" in message or "429" in lowered or "rate limit" in lowered


def check_envelope(payload: Any, path: str = "") -> dict[str, Any]:
    """校验响应信封，返回 result 层。"""
    if not isinstance(payload, dict):
        raise HeyboxError("小黑盒未返回有效数据")

    status = str(payload.get("status") or "").strip().lower()
    message = str(payload.get("msg") or payload.get("message") or "")
    if status in ("login", "relogin"):
        raise HeyboxLoginRequired(message or "小黑盒登录已失效")
    if status not in ("", "ok", "success"):
        if _rate_limited(message):
            raise HeyboxRateLimited(message or "请求过于频繁")
        raise HeyboxError(message or f"接口返回错误（{status}）")

    body = payload.get("result")
    return body if isinstance(body, dict) else {}


# ==================== 客户端 ====================


class HeyboxClient:
    """小黑盒接口客户端。session 由调用方提供，便于统一节流。"""

    def __init__(
        self,
        cookies: dict[str, str] | None = None,
        device_id: str = "",
        timeout: float = 15.0,
    ):
        self.cookies = dict(cookies or {})
        self.device_id = device_id or ""
        self.timeout = timeout

    # ---------- 底层 ----------

    async def raw(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = True,
        with_cookies: bool = True,
    ) -> dict[str, Any]:
        """发一次请求，返回响应信封（不校验状态）。

        ``with_cookies=False`` 时**一个 cookie 都不带** —— 免登录通道必须用这个，
        否则从已登录状态切到无绑定模式后，请求里仍会夹带登录信息。
        """
        query: dict[str, Any] = {}
        if signed:
            query.update(WEB_CLIENT_PARAMS)
        for key, value in (params or {}).items():
            if value is not None and str(value) != "":
                query[key] = value
        if signed:
            query = sign_params(path, query, self.device_id)

        try:
            async with session.request(
                method.upper(),
                f"{BASE_URL}{path}",
                params=query,
                headers=HEADERS,
                cookies=(self.cookies or None) if with_cookies else None,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                text = await response.text()
                status_code = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise HeyboxNetworkError(f"{type(exc).__name__}") from exc

        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise HeyboxError(f"响应不是 JSON（HTTP {status_code}）") from exc
        if not isinstance(payload, dict):
            raise HeyboxError("响应格式已变化")
        return payload

    async def call(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = True,
    ) -> dict[str, Any]:
        """发一次请求并校验信封，返回 result 层。"""
        payload = await self.raw(session, method, path, params, signed=signed)
        return check_envelope(payload, path)

    async def call_lenient(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = True,
        with_cookies: bool = True,
    ) -> dict[str, Any]:
        """校验信封，但允许缺失 status（免登录搜索在无结果时就是这样）。"""
        payload = await self.raw(session, method, path, params, signed=signed,
                                 with_cookies=with_cookies)
        status = str(payload.get("status") or "").strip().lower()
        if status in ("login", "relogin"):
            raise HeyboxLoginRequired(str(payload.get("msg") or ""))
        if status not in ("", "ok", "success"):
            message = str(payload.get("msg") or "")
            if _rate_limited(message):
                raise HeyboxRateLimited(message)
            raise HeyboxError(message or f"接口返回错误（{status}）")
        body = payload.get("result")
        return body if isinstance(body, dict) else {}

    # ---------- 查询类接口 ----------

    async def search_users(self, session, name: str) -> list[dict]:
        """按昵称搜索（需登录）：/game/yjwj/search"""
        body = await self.call(session, "POST", "/game/yjwj/search", {"q": name})
        rows = body.get("user_list")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    async def search_public(self, session, name: str, limit: int = 10) -> list[dict]:
        """免登录搜索：/game/player_search/do

        * **不带签名、不带任何 cookie** —— 这是真正的"不登录"通道。
        * 只支持**昵称模糊搜索**；传完整 role_id 返回 0 条（实测）。
        * 返回规范化后的角色列表：[{role_id, role_name, server, level, score, games, columns, avatar, raw}]
        """
        body = await self.call_lenient(
            session,
            "GET",
            "/game/player_search/do",
            {"game_type": GAME_TYPE, "q": name, "offset": 0, "limit": limit},
            signed=False,
            with_cookies=False,
        )
        rows = body.get("player_list")
        if not isinstance(rows, list):
            return []
        header = body.get("header")
        titles = [
            str(item.get("text") or "")
            for item in header
            if isinstance(item, dict)
        ] if isinstance(header, list) else []

        players = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            cells = row.get("column_list")
            cells = [c for c in cells if isinstance(c, dict)] if isinstance(cells, list) else []
            columns: dict[str, str] = {}
            ordered: list[dict[str, str]] = []
            for index, cell in enumerate(cells):
                label = titles[index] if index < len(titles) else ""
                text = str(cell.get("text") or "")
                if label:
                    columns[label] = text
                # 完整保留上游给的每一列（含图片 URL），避免上游加列时被静默丢弃
                ordered.append({
                    "label": label,
                    "text": text,
                    "type": str(cell.get("type") or ""),
                    "img": str(cell.get("img") or ""),
                    "sub_text": str(cell.get("sub_text") or ""),
                })
            role_name = next(
                (str(c.get("text") or "") for c in cells if c.get("type") == "user_info"),
                "",
            )
            avatar = next(
                (str(c.get("img") or "") for c in cells if c.get("type") == "user_info"),
                "",
            )
            players.append({
                "role_id": str(row.get("game_id") or ""),
                "role_name": role_name,
                "avatar": avatar,
                "server": str(row.get("ext") or DEFAULT_SERVER),
                "level": columns.get("境界", ""),
                "score": columns.get("总积分", ""),
                "games": columns.get("场次", ""),
                "columns": ordered,
                "raw": row,
            })
        return players

    async def update(self, session, role_id: str, server: str = DEFAULT_SERVER) -> dict:
        """触发数据更新：/game/yjwj/update（state=ok/waiting/updating/failed）"""
        return await self.call(
            session, "POST", "/game/yjwj/update", {"server": server, "role_id": role_id}
        )

    async def home_data(
        self,
        session,
        role_id: str,
        *,
        server: str = DEFAULT_SERVER,
        season: str = "pre-01",
        battle_tid: str = "",
        heybox_id: str = "",
        user_id: str = "",
    ) -> dict:
        """战绩主页数据：/game/yjwj/home/data"""
        return await self.call(session, "POST", "/game/yjwj/home/data", {
            "server": server,
            "role_id": role_id,
            "season": season,
            "battle_tid": battle_tid,
            "heybox_id": heybox_id,
            "user_id": user_id,
        })

    async def match_list(
        self,
        session,
        role_id: str,
        *,
        server: str = DEFAULT_SERVER,
        page: int = 1,
        page_size: int = 30,
        limit: int | None = None,
        offset: int | None = None,
        pagination: str = "offset",
        battle_tid: str = "",
        season: str = "pre-01",
        heybox_id: str = "",
    ) -> dict:
        """对局列表：/game/yjwj/match/list

        分页参数两选一（``pagination``）：

        * ``offset``（默认）——发 ``limit`` / ``offset``
        * ``page``  ——发 ``page`` / ``page_size``（本插件 1.0.x 的旧用法，README 记录过
          "上游重复返回同一页"，疑似一直没生效，保留仅供对照验证）

        两者的窗口是对应的：``page = offset // page_size + 1``。
        """
        params: dict[str, Any] = {
            "server": server,
            "role_id": role_id,
            "battle_tid": battle_tid,
            "season": season,
            "heybox_id": heybox_id,
        }
        if str(pagination).strip().lower() == "page":
            params["page"] = page
            params["page_size"] = page_size
        else:
            params["limit"] = page_size if limit is None else limit
            params["offset"] = 0 if offset is None else offset
        return await self.call(session, "POST", "/game/yjwj/match/list", params)

    async def match_detail(
        self,
        session,
        match_id: str,
        *,
        role_id: str = "",
        server: str = DEFAULT_SERVER,
        battle_tid: str = "",
        scene: str = "",
        heybox_id: str = "",
    ) -> dict:
        """单局详情：/game/yjwj/match/detail"""
        return await self.call(session, "POST", "/game/yjwj/match/detail", {
            "match_id": match_id,
            "role_id": role_id,
            "server": server,
            "battle_tid": battle_tid,
            "scene": scene,
            "heybox_id": heybox_id,
        })

    # ---------- 绑定类接口（全部 GET-only） ----------

    async def bind_game(self, session, game_id: str, server_id: str = DEFAULT_SERVER) -> dict:
        """绑定角色：/account/bind_game_id/（game_id 传昵称）"""
        return await self.call(session, "GET", "/account/bind_game_id/", {
            "game_type": GAME_TYPE,
            "game_id": game_id,
            "os_type": "webinapp",
            "server_id": server_id,
        })

    async def bind_game_state(self, session, game_id: str) -> dict:
        """绑定结果轮询：/account/bind_game_state/"""
        return await self.call(session, "GET", "/account/bind_game_state/", {
            "game_type": GAME_TYPE,
            "game_id": game_id,
            "os_type": "webinapp",
        })

    async def unbind_game(self, session, game_id: str, server_id: str = DEFAULT_SERVER) -> dict:
        """解绑角色：/account/unbind_game_id/（game_id 与绑定时传同一个字符串）"""
        return await self.call(session, "GET", "/account/unbind_game_id/", {
            "game_type": GAME_TYPE,
            "game_id": game_id,
            "os_type": "webinapp",
            "server_id": server_id,
        })
