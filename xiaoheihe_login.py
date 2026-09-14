"""小黑盒扫码登录模块

基于 https://github.com/674537331/astrbot_plugin_xiaoheihe_adapter 的实现，
仅保留扫码登录所需的最小功能集：请求签名、请求二维码、轮询登录状态、提取凭证。

请求签名中的 hkey 算法独立移植自 MIT 许可的 heybox-core 实现（XiaHouSheng）。
MD5 仅用于满足上游兼容性要求，不用于任何本地安全决策。
"""

from __future__ import annotations

import base64
import hashlib
import io
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import aiohttp
import qrcode

API_BASE_URL = "https://api.xiaoheihe.cn"

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

_HKEY_ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"  # gitleaks:allow

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class LoginState(str, Enum):
    WAITING_SCAN = "waiting_scan"
    SCANNED_WAITING_CONFIRM = "scanned_waiting_confirm"
    SUCCESS = "success"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass
class QRSession:
    """二维码登录会话"""

    qr_content: str
    poll_params: dict[str, str] = field(default_factory=dict)
    expires_at: float = 0.0


@dataclass
class LoginResult:
    """单次轮询结果"""

    state: LoginState
    message: str
    cookies: dict[str, str] = field(default_factory=dict)
    uid: str = ""
    nickname: str = ""


# ==================== 请求签名 ====================
# 以下签名逻辑独立移植自 MIT 许可的 heybox-core / heybox-bot 实现。


def generate_device_id() -> str:
    """生成 32 字符的 Web 客户端标识"""
    return secrets.token_hex(16)


def generate_xhh_token_id(now: int | None = None) -> str:
    """构建非密钥的 Web 客户端 cookie"""
    timestamp = str(now if now is not None else int(time.time()))
    parts = tuple(secrets.token_hex(16) for _ in range(3))
    raw = b"".join(
        hashlib.md5(part.encode(), usedforsecurity=False).digest()
        for part in (timestamp, *parts)
    )
    return base64.b64encode(raw + b"\x00").decode("ascii")


def generate_hkey(path: str, timestamp: int, nonce: str) -> str:
    """生成上游要求的 hkey 签名参数"""
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


def _map_to_alphabet(value: str, alphabet: str) -> str:
    return "".join(alphabet[ord(c) % len(alphabet)] for c in value)


def _sign_params(
    path: str,
    base_params: dict[str, str] | None = None,
    device_id: str = "",
) -> dict[str, str]:
    """为指定路径添加 _time、nonce、hkey 签名参数"""
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


# ==================== 响应解析 ====================


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    """提取 result / data 层"""
    candidate = payload.get("result", payload.get("data", payload))
    if isinstance(candidate, dict):
        return candidate
    return {}


def parse_qr_response(payload: dict[str, Any]) -> QRSession:
    """解析二维码请求响应"""
    body = _data(payload)
    qr_content = str(
        body.get("qrcode")
        or body.get("qr_url")
        or body.get("url")
        or body.get("qr_content")
        or ""
    )
    if not qr_content:
        raise ValueError("二维码响应缺少 qrcode/qr_url 字段")

    # 从二维码 URL 的 query 参数中提取轮询所需的参数
    poll_params = {
        str(k): str(v)
        for k, v in parse_qsl(urlsplit(qr_content).query, keep_blank_values=True)
    }

    started = time.time()
    raw_expiry = body.get("expires_in", body.get("ttl", body.get("expire", 180)))
    try:
        expiry = float(raw_expiry)
    except (TypeError, ValueError):
        expiry = 180
    if expiry > 10_000_000_000:  # 毫秒时间戳
        expiry /= 1000
    ttl = expiry - started if expiry > 1_000_000_000 else expiry

    return QRSession(
        qr_content=qr_content,
        poll_params=poll_params,
        expires_at=started + max(10, min(ttl, 600)),
    )


def parse_login_state(payload: dict[str, Any]) -> tuple[LoginState, str]:
    """解析二维码扫码状态"""
    body = _data(payload)
    message = str(
        body.get("message")
        or body.get("msg")
        or body.get("error_msg")
        or body.get("err_msg")
        or ""
    )
    result_marker = str(body.get("error") or body.get("err") or "").strip().lower()
    raw_state = str(
        body.get("state") or body.get("status") or body.get("qr_state") or ""
    ).strip().lower()

    state_map = {
        "0": LoginState.WAITING_SCAN,
        "waiting": LoginState.WAITING_SCAN,
        "waiting_scan": LoginState.WAITING_SCAN,
        "1": LoginState.SCANNED_WAITING_CONFIRM,
        "scanned": LoginState.SCANNED_WAITING_CONFIRM,
        "confirm": LoginState.SCANNED_WAITING_CONFIRM,
        "2": LoginState.SUCCESS,
        "success": LoginState.SUCCESS,
        "confirmed": LoginState.SUCCESS,
        "3": LoginState.EXPIRED,
        "expired": LoginState.EXPIRED,
        "-1": LoginState.FAILED,
        "failed": LoginState.FAILED,
    }

    if raw_state in state_map:
        return state_map[raw_state], message
    if result_marker in {"ok", "success", "confirmed"}:
        return LoginState.SUCCESS, message
    if result_marker in {"ready", "scanned"}:
        return LoginState.SCANNED_WAITING_CONFIRM, message
    if result_marker in {"wait", "waiting"}:
        return LoginState.WAITING_SCAN, message

    hint = f"{result_marker} {message}".casefold()
    if any(t in hint for t in ("expired", "timeout", "过期", "失效", "超时")):
        return LoginState.EXPIRED, message
    if any(
        t in hint
        for t in ("scanned", "confirm", "已扫码", "已扫描", "待确认", "请确认", "确认")
    ):
        return LoginState.SCANNED_WAITING_CONFIRM, message

    # 上游在等待期间可能返回非 ok 的 error marker，不能因此终止有效会话
    return LoginState.WAITING_SCAN, message


def parse_login_credentials(
    payload: dict[str, Any], response_cookies: dict[str, str]
) -> tuple[str, str, dict[str, str]]:
    """从登录成功响应中提取 uid、nickname 和完整 cookie 字典。

    同时从 Set-Cookie 响应头和 JSON 正文内嵌字段中收集凭证。
    """
    body = _data(payload)
    containers: list[dict[str, Any]] = [body]
    for key in ("user", "account", "profile", "account_detail"):
        candidate = body.get(key)
        if isinstance(candidate, dict):
            containers.append(candidate)

    def _pick(*keys: str) -> str:
        for container in containers:
            for key in keys:
                value = container.get(key)
                if value is not None and str(value) != "":
                    return str(value)
        return ""

    uid = _pick(
        "uid", "heybox_id", "user_heybox_id", "heyboxid", "user_id", "userid", "id"
    )
    nickname = _pick("nickname", "username", "name")

    cookies = {str(k): str(v) for k, v in response_cookies.items()}

    # 正文中可能内嵌 cookie 字段
    embedded = body.get("cookies")
    if isinstance(embedded, dict):
        cookies.update({str(k): str(v) for k, v in embedded.items()})

    # 将关键字段写入 cookie 字典（若尚未由 Set-Cookie 提供）
    for cookie_name, aliases in [
        ("pkey", ("pkey", "user_pkey", "key")),
        ("heybox_id", ("heybox_id", "user_heybox_id", "heyboxid", "user_id", "userid")),
        ("x_xhh_tokenid", ("x_xhh_tokenid",)),
    ]:
        if not cookies.get(cookie_name):
            value = _pick(*aliases)
            if value:
                cookies[cookie_name] = value

    return uid, nickname, cookies


# ==================== 二维码图片 ====================


def generate_qr_png(content: str) -> bytes:
    """将文本生成为 PNG 格式的二维码图片字节"""
    img = qrcode.make(content)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ==================== 登录客户端 ====================


class XiaoheiheLoginClient:
    """小黑盒扫码登录 HTTP 客户端"""

    def __init__(self, device_id: str = ""):
        self.device_id = device_id or generate_device_id()
        self._headers = {
            "Accept": "application/json",
            "Referer": "https://www.xiaoheihe.cn/",
            "User-Agent": _DEFAULT_UA,
        }

    async def request_qr(self) -> QRSession:
        """请求登录二维码"""
        params = _sign_params(
            "/account/get_qrcode_url/",
            {**WEB_CLIENT_PARAMS},
            self.device_id,
        )
        url = f"{API_BASE_URL}/account/get_qrcode_url/"

        jar = aiohttp.CookieJar(unsafe=True)
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
            async with session.get(url, params=params, headers=self._headers) as resp:
                resp.raise_for_status()
                payload = await resp.json()

        if not isinstance(payload, dict):
            raise ValueError("二维码响应格式无效")

        return parse_qr_response(payload)

    async def check_qr(self, qr_session: QRSession) -> LoginResult:
        """轮询二维码扫码状态"""
        params = _sign_params(
            "/account/qr_state/",
            {**WEB_CLIENT_PARAMS, **qr_session.poll_params},
            self.device_id,
        )
        url = f"{API_BASE_URL}/account/qr_state/"

        jar = aiohttp.CookieJar(unsafe=True)
        timeout = aiohttp.ClientTimeout(total=20)
        cookies: dict[str, str] = {}

        async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
            async with session.get(url, params=params, headers=self._headers) as resp:
                resp.raise_for_status()
                payload = await resp.json()

            # 登录成功后 Set-Cookie 会携带认证 cookie
            for cookie in session.cookie_jar:
                cookies[str(cookie.key)] = str(cookie.value)

        if not isinstance(payload, dict):
            raise ValueError("登录状态响应格式无效")

        state, message = parse_login_state(payload)

        uid = ""
        nickname = ""
        if state is LoginState.SUCCESS:
            uid, nickname, cookies = parse_login_credentials(payload, cookies)

        return LoginResult(
            state=state,
            message=message,
            cookies=cookies,
            uid=uid,
            nickname=nickname,
        )
