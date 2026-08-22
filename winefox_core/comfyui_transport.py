# -*- coding: utf-8 -*-
"""WineFox Desktop — 平台无关 ComfyUI HTTP 传输层（Agent 05）。

职责边界（与旧插件 comfyui_api.py 的差异）：
- 旧插件把 HTTP 细节、认证、超时、重试散落在 ComfyUI 类各处，且默认
  server_ssl_verify=False（默认不验证 TLS）。本模块收敛为单一传输层：
  * TLS 默认验证（verify_tls=True），显式配置才可关闭（关闭时记 WARNING）；
  * Bearer / Basic / 自定义 Header / 自定义 CA 统一处理；
  * connect/read/total 三类超时；
  * GET（幂等）有限退避重试；POST 一律不重试；
  * JSON / 图片响应体大小上限；日志与异常自动脱敏；
  * 不返回服务器绝对路径，不自带 UI 文案。

不依赖 AstrBot，使用标准 logging。
"""
from __future__ import annotations

import asyncio
import logging
import re
import ssl
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .comfyui_types import (
    AuthMode,
    ComfyUIAuth,
    ComfyUIAuthError,
    ComfyUIConnectionError,
    ComfyUIError,
    ComfyUIPayloadTooLargeError,
    ComfyUIServerConfig,
    ComfyUITLSError,
    ComfyUITimeoutError,
    ErrorKind,
)

logger = logging.getLogger("winefox.comfyui.transport")

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_SENSITIVE_HEADER_RE = re.compile(
    r"(authorization|proxy-authorization|x-api-key|api-key|token|cookie|set-cookie)",
    re.IGNORECASE,
)
# 形如 Authorization: Bearer abc... 的通用脱敏模式（即使 secret 未知也能兜底）
_GENERIC_SECRET_RE = re.compile(
    r"(Bearer|Basic|Token|api[_-]?key)\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE
)


def _normalize_server_address(server_address: str) -> str:
    """规范化 ComfyUI 地址。

    允许 "127.0.0.1:8188" 或完整 URL；只接受 http/https；去掉尾部斜杠；
    剥离 URL 里内嵌的 userinfo（凭据不允许走 URL，必须走 auth 配置）。
    空值或非法 scheme 抛 ValueError。
    """
    raw = (server_address or "").strip()
    if not raw:
        raise ValueError("ComfyUI server_address 不能为空")
    if not _SCHEME_RE.match(raw):
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"ComfyUI 仅支持 http/https scheme，收到: {scheme!r}")
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"ComfyUI server_address 缺少主机名: {server_address!r}")
    port = parts.port
    if port is not None and not (1 <= port <= 65535):
        raise ValueError(f"ComfyUI server_address 端口非法: {port!r}")
    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    # 剥离 path/query/fragment，ComfyUI API 根就是 scheme://host:port
    return urlunsplit((scheme, netloc, "", "", ""))


def _redact_text(text: str, secrets: Tuple[str, ...] = ()) -> str:
    """日志脱敏：替换已知密钥值 + 兜底通用认证头模式。"""
    out = str(text or "")
    for secret in secrets:
        if secret and secret in out:
            out = out.replace(secret, "***")
    return _GENERIC_SECRET_RE.sub(lambda m: f"{m.group(1)} ***", out)


def _redact_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    return {
        k: ("***" if _SENSITIVE_HEADER_RE.search(k) else v)
        for k, v in (headers or {}).items()
    }


async def _cap_body(resp: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    """读取响应体并强制大小上限（防内存炸弹）。"""
    declared = resp.content_length
    if declared is not None and declared > max_bytes:
        raise ComfyUIPayloadTooLargeError(
            f"响应体超过大小上限（{declared} > {max_bytes} bytes）",
            kind=ErrorKind.PAYLOAD_TOO_LARGE,
            status=resp.status,
        )
    chunks: list[bytes] = []
    received = 0
    async for chunk in resp.content.iter_chunked(min(64 * 1024, max_bytes + 1)):
        received += len(chunk)
        if received > max_bytes:
            raise ComfyUIPayloadTooLargeError(
                f"响应体超过大小上限（>{max_bytes} bytes）",
                kind=ErrorKind.PAYLOAD_TOO_LARGE,
                status=resp.status,
            )
        chunks.append(chunk)
    return b"".join(chunks)


class ComfyUITransport:
    """aiohttp 会话生命周期 + 认证/TLS/超时/退避 的单一入口。

    用法（显式生命周期）::

        transport = ComfyUITransport(config)
        await transport.start()
        try:
            data = await transport.get_json("/object_info")
        finally:
            await transport.close()

    也支持 ``async with ComfyUITransport(config) as t:``。
    测试可注入现成 aiohttp session（``session=``），此时不负责关闭注入的会话。
    """

    def __init__(
        self,
        config: ComfyUIServerConfig,
        *,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        if config is None:
            raise ValueError("ComfyUITransport 需要 ComfyUIServerConfig")
        self._cfg = config
        self.base_url = _normalize_server_address(config.server_address)
        self._injected_session = session
        self._session: Optional[aiohttp.ClientSession] = None
        self._ssl_context: Optional[ssl.SSLContext] = None
        self._secrets: Tuple[str, ...] = config.auth.secret_values
        self._closed = True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> "ComfyUITransport":
        if self._session is not None:
            return self
        self._ssl_context = self._build_ssl_context()
        self._session = self._injected_session or aiohttp.ClientSession(
            headers=self._base_headers(),
            timeout=self._timeout(),
            connector=aiohttp.TCPConnector(ssl=self._ssl_context),
            raise_for_status=False,
        )
        self._closed = False
        logger.info(
            "[ComfyUITransport] 已连接 %s | tls_verify=%s ca_bundle=%s auth=%s | 超时 connect=%.1fs read=%.1fs total=%.1fs",
            self.base_url,
            self._cfg.verify_tls,
            bool(self._cfg.ca_bundle),
            self._cfg.auth.mode.value,
            self._cfg.timeout_connect,
            self._cfg.timeout_read,
            self._cfg.timeout_total,
        )
        return self

    async def close(self) -> None:
        if self._session is not None and not self._injected_session:
            await self._session.close()
        self._session = None
        self._closed = True

    async def __aenter__(self) -> "ComfyUITransport":
        return await self.start()

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("ComfyUITransport 未 start()；请先 await transport.start()")
        return self._session

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------
    # TLS / 认证 / 超时
    # ------------------------------------------------------------------
    def _build_ssl_context(self) -> Optional[ssl.SSLContext]:
        """默认验证；显式 verify_tls=False 才关闭（记录 WARNING）。"""
        if not self._cfg.verify_tls:
            logger.warning(
                "[ComfyUITransport] TLS 验证已显式关闭（verify_tls=False）— 仅限内网/调试，"
                "存在中间人风险，不推荐；请优先配置 ca_bundle 信任自签名证书"
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if self._cfg.ca_bundle:
            ca_path = Path(str(self._cfg.ca_bundle)).expanduser()
            if not ca_path.is_file():
                raise ComfyUITLSError(
                    f"自定义 CA 文件不存在: {ca_path.name}",
                    kind=ErrorKind.TLS,
                )
            ctx = ssl.create_default_context(cafile=str(ca_path))
            logger.info("[ComfyUITransport] 使用自定义 CA: %s", ca_path.name)
            return ctx
        return None  # aiohttp 默认验证

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            total=max(self._cfg.timeout_total, 0.0),
            connect=max(self._cfg.timeout_connect, 0.0),
            sock_connect=max(self._cfg.timeout_connect, 0.0),
            sock_read=max(self._cfg.timeout_read, 0.0),
        )

    def _base_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"User-Agent": self._cfg.user_agent}
        for key, value in (self._cfg.extra_headers or {}).items():
            headers[str(key)] = str(value)
        auth = self._cfg.auth
        if auth.mode == AuthMode.BEARER and auth.bearer_token:
            headers["Authorization"] = f"Bearer {auth.bearer_token}"
        elif auth.mode == AuthMode.BASIC:
            import base64

            token = base64.b64encode(
                f"{auth.username}:{auth.password}".encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        return headers

    def _url(self, path: str) -> str:
        if not str(path).startswith("/"):
            path = f"/{path}"
        return f"{self.base_url}{path}"

    # ------------------------------------------------------------------
    # 异常归一化
    # ------------------------------------------------------------------
    def _classify(
        self, exc: BaseException, method: str, url: str, status_unknown: bool = False
    ) -> ComfyUIError:
        """把 aiohttp/ssl 异常映射为结构化 ComfyUIError。

        status_unknown=True 表示写请求（POST）已发出但连接中断，无法确认
        服务器是否已收到 —— 调用方（executor）据此决定不得重试。
        """
        url_red = _redact_text(url, self._secrets)
        if isinstance(exc, ComfyUIError):
            return exc
        if isinstance(exc, asyncio.TimeoutError):
            return ComfyUITimeoutError(
                f"请求超时: {method} {url_red}（connect={self._cfg.timeout_connect}s "
                f"read={self._cfg.timeout_read}s total={self._cfg.timeout_total}s）",
                kind=ErrorKind.TIMEOUT,
                cause=exc,
            )
        # 证书验证失败：自签名/不受信 → 明确提示
        if isinstance(exc, aiohttp.ClientConnectorCertificateError) or isinstance(
            exc, ssl.SSLCertVerificationError
        ):
            reason = getattr(exc, "verify_message", None) or str(exc)
            return ComfyUITLSError(
                "ComfyUI TLS 证书验证失败（自签名或不受信证书）。"
                "请把服务器证书配置到 ca_bundle，或确认 verify_tls 已显式关闭；"
                "不建议在生产环境关闭 TLS 验证。",
                kind=ErrorKind.TLS,
                detail=_redact_text(str(reason), self._secrets),
                cause=exc,
            )
        # 连接建立前失败 → 明确 CONNECTION（可确定请求未发出）
        if isinstance(exc, (aiohttp.ClientConnectorError, aiohttp.ClientOSError)):
            # 连接建立前失败 → 确定请求未发出
            return ComfyUIConnectionError(
                f"无法连接 ComfyUI: {url_red}（{exc}）",
                kind=ErrorKind.CONNECTION,
                cause=exc,
            )
        if isinstance(exc, aiohttp.ClientError) or isinstance(exc, (ConnectionError, OSError)):
            return ComfyUIConnectionError(
                f"HTTP 客户端错误: {method} {url_red}（{exc}）",
                kind=ErrorKind.CONNECTION,
                cause=exc,
                status_unknown=status_unknown,
            )
        return ComfyUIError(
            f"请求失败: {method} {url_red}（{exc}）",
            kind=ErrorKind.CONNECTION,
            cause=exc,
            status_unknown=status_unknown,
        )

    def _is_retryable_http_status(self, status: int) -> bool:
        return status in (429,) or status >= 500

    def _backoff(self, attempt: int) -> float:
        base = max(float(self._cfg.get_backoff_base), 0.0)
        cap = max(float(self._cfg.get_backoff_max), base)
        return min(base * (2 ** max(attempt, 0)), cap) + (time.monotonic() % 0.25)

    # ------------------------------------------------------------------
    # 请求原语
    # ------------------------------------------------------------------
    async def get_json(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        retries: Optional[int] = None,
    ) -> Any:
        """GET JSON（幂等）：有限退避重试。

        仅对连接错误/超时/429/5xx 重试；401/403 立即抛 ComfyUIAuthError，
        其余 4xx 不重试。默认重试次数取配置 get_retries（含首次共 N 次尝试）。
        """
        url = self._url(path)
        attempts = max(int(retries if retries is not None else self._cfg.get_retries), 1)
        last_error: Optional[ComfyUIError] = None
        for attempt in range(attempts):
            try:
                async with self.session.get(
                    url, params=params, timeout=timeout or self._timeout()
                ) as resp:
                    if resp.status in (401, 403):
                        raise ComfyUIAuthError(
                            f"ComfyUI 认证失败（HTTP {resp.status}）：请检查 Bearer/Basic/自定义 Header 配置",
                            kind=ErrorKind.AUTH,
                            status=resp.status,
                        )
                    if self._is_retryable_http_status(resp.status):
                        if attempt + 1 < attempts:
                            delay = self._backoff(attempt)
                            logger.warning(
                                "[ComfyUITransport] GET %s 状态 %s，%.1fs 后重试 (%d/%d)",
                                _redact_text(url, self._secrets), resp.status, delay, attempt + 1, attempts,
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise ComfyUIError(
                            f"GET {_redact_text(url, self._secrets)} 返回 {resp.status}（重试 {attempts} 次后仍失败）",
                            kind=ErrorKind.CONNECTION,
                            status=resp.status,
                        )
                    if resp.status >= 400:
                        raise ComfyUIError(
                            f"GET {_redact_text(url, self._secrets)} 返回 {resp.status}",
                            kind=ErrorKind.CONNECTION,
                            status=resp.status,
                        )
                    body = await _cap_body(resp, self._cfg.max_json_bytes)
                    try:
                        return await _decode_json(body, self._cfg, url)
                    except ComfyUIError as exc:
                        raise exc
            except asyncio.TimeoutError as exc:
                last_error = self._classify(exc, "GET", url)
            except aiohttp.ClientConnectorError as exc:
                # 连接建立失败（拒绝/不可达）：重试必然同样失败，
                # 只会让离线探测白白拖上几十秒。立即抛出。
                raise self._classify(exc, "GET", url) from exc
            except aiohttp.ClientError as exc:
                last_error = self._classify(exc, "GET", url)
            except ssl.SSLCertVerificationError as exc:
                last_error = self._classify(exc, "GET", url)
            except ComfyUIError as exc:
                last_error = exc
                if exc.kind in (ErrorKind.AUTH, ErrorKind.PAYLOAD_TOO_LARGE):
                    raise exc  # 认证/超限不重试
            if attempt + 1 < attempts:
                delay = self._backoff(attempt)
                logger.debug(
                    "[ComfyUITransport] GET %s 失败: %s → %.1fs 后重试 (%d/%d)",
                    _redact_text(url, self._secrets), last_error, delay, attempt + 1, attempts,
                )
                await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout: Optional[aiohttp.ClientTimeout] = None,
    ) -> Any:
        """POST JSON。绝不重试。

        连接建立前失败 → CONNECTION；发送后连接断开/超时等状态不明 →
        CONNECTION（kind=connection，unknown=True 语义由调用方按路径解释，
        例如 executor 对 /prompt 包装为 QUEUE_SUBMIT_UNKNOWN）。
        """
        url = self._url(path)
        try:
            async with self.session.post(
                url, json=dict(payload), timeout=timeout or self._timeout()
            ) as resp:
                if resp.status in (401, 403):
                    raise ComfyUIAuthError(
                        f"ComfyUI 认证失败（HTTP {resp.status}）：请检查 Bearer/Basic/自定义 Header 配置",
                        kind=ErrorKind.AUTH,
                        status=resp.status,
                    )
                body = await _cap_body(resp, self._cfg.max_json_bytes)
                if resp.status >= 400:
                    body_text = body.decode("utf-8", errors="replace")[:4000]
                    raise ComfyUIError(
                        f"POST {_redact_text(url, self._secrets)} 返回 {resp.status}",
                        kind=ErrorKind.CONNECTION,
                        status=resp.status,
                        detail=body_text,
                    )
                try:
                    return await _decode_json(body, self._cfg, url)
                except ComfyUIError as exc:
                    raise exc
        except ComfyUIError as exc:
            raise exc
        except asyncio.TimeoutError as exc:
            raise self._classify(exc, "POST", url, status_unknown=True)
        except aiohttp.ClientError as exc:
            raise self._classify(exc, "POST", url, status_unknown=True)
        except ssl.SSLCertVerificationError as exc:
            raise self._classify(exc, "POST", url)

    async def post_multipart(
        self,
        path: str,
        fields: Mapping[str, Any],
        *,
        timeout: Optional[aiohttp.ClientTimeout] = None,
    ) -> Any:
        """multipart/form-data POST（上传），绝不重试。"""
        url = self._url(path)
        try:
            form = aiohttp.FormData()
            for key, value in (fields or {}).items():
                if isinstance(value, tuple) and len(value) == 3:
                    form.add_field(key, value[0], filename=value[1], content_type=value[2])
                else:
                    form.add_field(key, value)
            async with self.session.post(
                url, data=form, timeout=timeout or self._timeout()
            ) as resp:
                if resp.status in (401, 403):
                    raise ComfyUIAuthError(
                        f"ComfyUI 认证失败（HTTP {resp.status}）：请检查 Bearer/Basic/自定义 Header 配置",
                        kind=ErrorKind.AUTH,
                        status=resp.status,
                    )
                body = await _cap_body(resp, self._cfg.max_json_bytes)
                if resp.status >= 400:
                    body_text = body.decode("utf-8", errors="replace")[:4000]
                    raise ComfyUIError(
                        f"POST {_redact_text(url, self._secrets)} 返回 {resp.status}",
                        kind=ErrorKind.CONNECTION,
                        status=resp.status,
                        detail=body_text,
                    )
                try:
                    return await _decode_json(body, self._cfg, url)
                except ComfyUIError as exc:
                    raise exc
        except ComfyUIError as exc:
            raise exc
        except asyncio.TimeoutError as exc:
            raise self._classify(exc, "POST", url, status_unknown=True)
        except aiohttp.ClientError as exc:
            raise self._classify(exc, "POST", url, status_unknown=True)
        except ssl.SSLCertVerificationError as exc:
            raise self._classify(exc, "POST", url)

    async def get_bytes(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        max_bytes: Optional[int] = None,
        timeout: Optional[aiohttp.ClientTimeout] = None,
        retries: Optional[int] = None,
    ) -> bytes:
        """GET 二进制（/view），带大小上限与有限重试（幂等）。"""
        url = self._url(path)
        cap = int(max_bytes or self._cfg.max_image_bytes)
        attempts = max(int(retries if retries is not None else self._cfg.get_retries), 1)
        last_error: Optional[ComfyUIError] = None
        for attempt in range(attempts):
            try:
                async with self.session.get(
                    url, params=params, timeout=timeout or self._timeout()
                ) as resp:
                    if resp.status in (401, 403):
                        raise ComfyUIAuthError(
                            f"ComfyUI 认证失败（HTTP {resp.status}）：请检查 Bearer/Basic/自定义 Header 配置",
                            kind=ErrorKind.AUTH,
                            status=resp.status,
                        )
                    if self._is_retryable_http_status(resp.status):
                        if attempt + 1 < attempts:
                            await asyncio.sleep(self._backoff(attempt))
                            continue
                        raise ComfyUIError(
                            f"GET {_redact_text(url, self._secrets)} 返回 {resp.status}（重试 {attempts} 次后仍失败）",
                            kind=ErrorKind.CONNECTION,
                            status=resp.status,
                        )
                    if resp.status >= 400:
                        raise ComfyUIError(
                            f"GET {_redact_text(url, self._secrets)} 返回 {resp.status}",
                            kind=ErrorKind.CONNECTION,
                            status=resp.status,
                        )
                    return await _cap_body(resp, cap)
            except asyncio.TimeoutError as exc:
                last_error = self._classify(exc, "GET", url)
            except aiohttp.ClientError as exc:
                last_error = self._classify(exc, "GET", url)
            except ssl.SSLCertVerificationError as exc:
                last_error = self._classify(exc, "GET", url)
            except ComfyUIError as exc:
                last_error = exc
                if exc.kind in (ErrorKind.AUTH, ErrorKind.PAYLOAD_TOO_LARGE):
                    raise exc
            if attempt + 1 < attempts:
                await asyncio.sleep(self._backoff(attempt))
        assert last_error is not None
        raise last_error


async def _decode_json(body: bytes, cfg: ComfyUIServerConfig, url: str) -> Any:
    import json

    if len(body) > cfg.max_json_bytes:
        raise ComfyUIPayloadTooLargeError(
            f"JSON 响应超过大小上限（{len(body)} > {cfg.max_json_bytes} bytes）",
            kind=ErrorKind.PAYLOAD_TOO_LARGE,
        )
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise ComfyUIError(
            f"JSON 解析失败: {_redact_text(url, cfg.auth.secret_values)}",
            kind=ErrorKind.INTERNAL,
            cause=exc,
        )
