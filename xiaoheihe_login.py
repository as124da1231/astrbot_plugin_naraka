"""小黑盒扫码登录模块"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

import aiohttp
import qrcode

# 签名算法与固定请求参数统一由 heybox_api 提供，这里只做兼容性再导出
from .heybox_api import (  # noqa: F401
    WEB_CLIENT_PARAMS,
    generate_hkey,
    sign_params as _sign_params,
)

API_BASE_URL = "https://api.xiaoheihe.cn"

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
    """小黑盒 App 扫码登录 HTTP 客户端"""

    # 主流程轮询间隔（小黑盒每次都要重新发起短轮询）
    poll_delay = 3.0

    def __init__(self, device_id: str = ""):
        # device_id 的策略由调用方决定（见 main 的 device_id_mode）：
        # 传空字符串 = 本次请求不带 device_id 参数
        self.device_id = device_id
        self._headers = {
            "Accept": "application/json",
            "Referer": "https://www.xiaoheihe.cn/",
            "User-Agent": _DEFAULT_UA,
        }

    async def qr_image(self, qr_session: QRSession) -> bytes:
        """小黑盒给的是二维码内容，本地渲染成 PNG。"""
        return generate_qr_png(qr_session.qr_content)

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


# ==================== 微信扫码登录 ====================


class WechatLoginClient:
    """微信扫码登录小黑盒（SSO 换 cookie）。"""

    WEIXIN_APP_ID = "wxced0cbce486f737e"
    LOGIN_REDIRECT = "https://api.xiaoheihe.cn/account/wechat/login_redirect/v2/web_sso/"
    CALLBACK_URL = "http://127.0.0.1/heybox-login-callback"

    # 微信是长轮询，返回很快，不需要再等 3 秒
    poll_delay = 0.5

    _UUID_RE = re.compile(r"connect/qrcode/([A-Za-z0-9_\-]{8,})")
    _ERRCODE_RE = re.compile(r"wx_errcode=(\d+)")
    _WXCODE_RE = re.compile(r"window\.wx_code='([^']*)'")

    def __init__(self, device_id: str = ""):
        # 同 XiaoheiheLoginClient：策略由调用方决定
        self.device_id = device_id
        self._headers = {
            "User-Agent": _DEFAULT_UA,
            "Referer": "https://open.weixin.qq.com/",
        }

    def _redirect_uri(self) -> str:
        return f"{self.LOGIN_REDIRECT}?redirect_url={quote(self.CALLBACK_URL, safe='')}"

    async def request_qr(self) -> QRSession:
        """请求微信登录二维码（只拿到 uuid，图片另行下载）。"""
        params = {
            "appid": self.WEIXIN_APP_ID,
            "redirect_uri": self._redirect_uri(),
            "response_type": "code",
            "scope": "snsapi_login",
            "state": "xiaoheihe",
        }
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://open.weixin.qq.com/connect/qrconnect",
                params=params,
                headers=self._headers,
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()

        match = self._UUID_RE.search(html)
        if match is None:
            raise ValueError("微信登录：未能从页面取到二维码 uuid")

        uuid = match.group(1)
        return QRSession(
            qr_content=f"https://open.weixin.qq.com/connect/qrcode/{uuid}",
            poll_params={"uuid": uuid},
            expires_at=time.time() + 120,
        )

    async def qr_image(self, qr_session: QRSession) -> bytes:
        """微信直接提供二维码图片，下载即可。"""
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(qr_session.qr_content, headers=self._headers) as resp:
                resp.raise_for_status()
                return await resp.read()

    async def check_qr(self, qr_session: QRSession) -> LoginResult:
        """长轮询扫码状态；已确认时直接完成登录交换。"""
        uuid = str(qr_session.poll_params.get("uuid") or "")
        if not uuid:
            return LoginResult(LoginState.FAILED, "缺少微信二维码 uuid")

        params = {"uuid": uuid, "_": str(int(time.time() * 1000))}
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://long.open.weixin.qq.com/connect/l/qrconnect",
                    params=params,
                    headers=self._headers,
                ) as resp:
                    body = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # 长轮询超时是正常的，继续等
            return LoginResult(LoginState.WAITING_SCAN, "")

        errcode_match = self._ERRCODE_RE.search(body)
        errcode = errcode_match.group(1) if errcode_match else ""
        if errcode == "404":
            return LoginResult(LoginState.SCANNED_WAITING_CONFIRM, "已扫码，请在微信中确认")
        if errcode in ("402", "403"):
            return LoginResult(LoginState.EXPIRED, "二维码已过期")
        if errcode != "405":
            return LoginResult(LoginState.WAITING_SCAN, "")

        wxcode_match = self._WXCODE_RE.search(body)
        if wxcode_match is None or not wxcode_match.group(1):
            return LoginResult(LoginState.FAILED, "微信已确认，但未取到 code")
        return await self._exchange(wxcode_match.group(1))

    async def _exchange(self, wx_code: str) -> LoginResult:
        """用 wx_code 换取小黑盒的 pkey / heybox_id。"""
        url = (
            f"{self.LOGIN_REDIRECT}?redirect_url={quote(self.CALLBACK_URL, safe='')}"
            f"&code={quote(wx_code, safe='')}&state=xiaoheihe"
        )
        timeout = aiohttp.ClientTimeout(total=25)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers=self._headers, allow_redirects=False
                ) as resp:
                    cookies = _read_set_cookies(resp)
                    location = str(resp.headers.get("Location") or "")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return LoginResult(LoginState.FAILED, f"登录交换失败：{type(exc).__name__}")

        query = dict(parse_qsl(urlsplit(location).query, keep_blank_values=True))
        uid = (
            cookies.get("user_heybox_id")
            or cookies.get("heybox_id")
            or query.get("heybox_id")
            or ""
        )
        pkey = cookies.get("user_pkey") or cookies.get("pkey") or query.get("pkey") or ""
        if not uid or not pkey:
            return LoginResult(LoginState.FAILED, "登录交换未返回 pkey / heybox_id")

        cookies.setdefault("pkey", pkey)
        cookies.setdefault("user_pkey", pkey)
        cookies.setdefault("heybox_id", uid)
        cookies.setdefault("user_heybox_id", uid)
        return LoginResult(LoginState.SUCCESS, "登录成功", cookies=cookies, uid=uid)


def _read_set_cookies(response: aiohttp.ClientResponse) -> dict[str, str]:
    """从响应头直接读取 Set-Cookie（不依赖 cookie jar）。"""
    result: dict[str, str] = {}
    headers = response.headers
    values = headers.getall("Set-Cookie", []) if hasattr(headers, "getall") else []
    for raw in values:
        first = raw.split(";")[0]
        key, sep, value = first.partition("=")
        if sep and key.strip():
            result[key.strip()] = value.strip()
    return result


LOGIN_CLIENTS = {
    "heybox": XiaoheiheLoginClient,
    "wechat": WechatLoginClient,
}


def create_login_client(mode: str, device_id: str = ""):
    """按数据模式创建登录客户端；none 模式返回 None（不发二维码）。"""
    factory = LOGIN_CLIENTS.get(str(mode or "").strip().lower())
    return factory(device_id) if factory else None
