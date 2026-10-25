"""Bounded, resumable HTTP transfers shared by the LoRA sources.

Each range owns a separate file. Only verified complete ranges are assembled;
no preallocated holes or legacy progress markers can become a completed model.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

RETRY_BASE = .5
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class TransferError(RuntimeError):
    def __init__(self, message, *, retryable=False, delay=None):
        super().__init__(message)
        self.retryable = retryable
        self.delay = delay


class RangeUnavailable(TransferError):
    pass


def retry_delay(value, attempt):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = RETRY_BASE * 2 ** attempt
    return max(0, min(delay, 20))


def _origin(url):
    parsed = urlparse(str(url))
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


@asynccontextmanager
async def response(client, session, url, *, headers=None):
    """Validate redirects before connecting; bind provider credentials to origin."""
    current = str(url)
    for _ in range(9):
        request_headers = dict(client._headers())
        if _origin(current) != _origin(client.site_base):
            for key in ("Authorization", "Cookie", "Proxy-Authorization", "Referer"):
                request_headers.pop(key, None)
        request_headers.update(headers or {})
        request_headers["Accept-Encoding"] = "identity"
        async with session.get(current, headers=request_headers, proxy=client.proxy or None, allow_redirects=False) as result:
            if result.status in {301, 302, 303, 307, 308}:
                location = result.headers.get("Location")
                if not location:
                    raise TransferError("下载重定向缺少目标地址")
                target = urljoin(current, location)
                parsed = urlparse(target)
                if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
                    raise TransferError("下载重定向地址无效")
                if urlparse(current).scheme == "https" and parsed.scheme != "https":
                    raise TransferError("下载重定向不能降低 HTTPS 安全级别")
                if client.download_redirect_validator:
                    accepted = client.download_redirect_validator(target)
                    if inspect.isawaitable(accepted):
                        accepted = await accepted
                    if accepted is False:
                        raise TransferError("下载重定向地址未通过安全校验")
                current = target
                continue
            yield result
            return
    raise TransferError("下载重定向次数过多")


def _check_status(result, source):
    if result.status in {200, 206}:
        return
    status = result.status
    if status == 401:
        message = "来源要求登录或凭据已失效，请检查该来源的 Token / Cookie 和模型下载权限"
    elif status == 403:
        message = "来源拒绝下载，请检查模型访问权限、登录状态或站点防护；反复重试不会授予权限"
    elif status == 404:
        message = "模型文件不存在或已被移除，请重新搜索并选择版本"
    elif status == 429:
        message = "来源限流，已按 Retry-After 等待；请稍后重试"
    elif status in TRANSIENT_STATUSES:
        message = "来源服务暂时不可用，自动重试后仍未恢复"
    else:
        message = "来源返回了无法下载的响应"
    raise TransferError(f"{source} 下载失败 HTTP {status}：{message}", retryable=status in TRANSIENT_STATUSES, delay=result.headers.get("Retry-After"))


def _range(result, start, end, total=None):
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", result.headers.get("Content-Range", ""))
    if not match:
        raise TransferError("下载 Content-Range 范围缺失或无效")
    first, last, size = map(int, match.groups())
    if first != start or last != end or size <= last or (total is not None and size != total):
        raise TransferError("下载 Content-Range 范围与请求分片不一致")
    if result.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise TransferError("分片下载不接受压缩后的字节范围")
    return size


async def download(client, url, destination, *, parallel=True, progress_cb=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    timeout = aiohttp.ClientTimeout(total=None, connect=min(30, client.timeout), sock_read=min(120, max(15, client.timeout)))
    attempts = max(2, min(4, client.network_max_retries + 1))
    source = {"huggingface": "Hugging Face", "civitai_red": "Civitai Red"}.get(client.source_key, "Civitai")

    async def retry(operation):
        for attempt in range(attempts):
            try:
                return await operation()
            except TransferError as exc:
                if not exc.retryable or attempt + 1 == attempts:
                    raise
                await asyncio.sleep(retry_delay(exc.delay, attempt))
            except (aiohttp.ClientError, TimeoutError) as exc:
                if attempt + 1 == attempts:
                    # Do not include signed URLs, headers, or proxy passwords.
                    raise TransferError(f"{source} 下载连接中断或超时，已保留可恢复分片，请重试") from exc
                await asyncio.sleep(retry_delay(None, attempt))

    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False, trust_env=False) as session:
        async def probe():
            async with response(client, session, url, headers={"Range": "bytes=0-0"}) as result:
                _check_status(result, source)
                ranged = result.status == 206
                total = _range(result, 0, 0) if ranged else int(result.headers.get("Content-Length") or 0)
                etag = result.headers.get("ETag", "")
                # Modification dates can stay equal across distinct revisions;
                # only a strong entity tag authorizes reuse of previous bytes.
                validator = etag if etag and not etag.startswith("W/") else ""
                return total, ranged, validator

        total, ranged, validator = await retry(probe)
        if getattr(client, "_no_ranges", False):
            ranged, validator = False, ""
        split = min(client.split, max(1, total)) if parallel and ranged and total else 1
        if total:
            part_size = (total + split - 1) // split
            split = (total + part_size - 1) // part_size
        identity = hashlib.sha256(json.dumps([url, total, validator, split]).encode()).hexdigest()
        work = destination.parent / f".{destination.name}.transfer-{identity[:16]}"
        if work.is_symlink():
            raise TransferError("下载临时目录不能是符号链接")
        work.mkdir(exist_ok=True)
        if work.resolve().parent != destination.parent.resolve():
            raise TransferError("下载临时目录超出目标目录")
        manifest = work / "manifest.json"
        if manifest.is_symlink():
            raise TransferError("下载清单不能是符号链接")
        manifest.write_text(json.dumps({"identity": identity, "size": total, "parts": split}), encoding="utf-8")
        chunk_size = (total + split - 1) // split if total else 0
        paths = [work / f"part-{index}" for index in range(split)]
        for path in paths:
            if path.is_symlink():
                raise TransferError("下载临时文件不能是符号链接")
        counts = [0] * split
        last_report = 0.0

        async def report(force=False):
            nonlocal last_report
            now = time.monotonic()
            if progress_cb and (force or now - last_report >= .75):
                last_report = now
                await progress_cb(sum(counts), total)

        async def fetch(index):
            path = paths[index]
            start = index * chunk_size
            end = min(total - 1, start + chunk_size - 1) if total else None
            expected = end - start + 1 if end is not None else None
            if not validator or not ranged or (path.exists() and expected is not None and path.stat().st_size > expected):
                path.write_bytes(b"")
            async def transfer():
                completed = path.stat().st_size if path.exists() else 0
                counts[index] = completed
                if expected is not None and completed == expected:
                    return
                # Without an object validator, a retry starts from byte zero.
                if completed and not (validator and ranged):
                    path.write_bytes(b"")
                    completed = counts[index] = 0
                headers = {}
                use_range = split > 1 or completed > 0
                if use_range:
                    headers["Range"] = f"bytes={start + completed}-{end}"
                    if validator:
                        headers["If-Range"] = validator
                async with response(client, session, url, headers=headers) as result:
                    _check_status(result, source)
                    if use_range:
                        if result.status == 200:
                            raise RangeUnavailable("来源忽略 Range 或文件版本发生变化，改为完整下载")
                        _range(result, start + completed, end, total)
                    elif result.status != 200:
                        raise TransferError("完整下载返回了意外的分片响应")
                    if result.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise TransferError("模型下载不接受压缩传输")
                    with path.open("ab") as stream:
                        async for chunk in result.content.iter_chunked(256 * 1024):
                            if expected is not None and counts[index] + len(chunk) > expected:
                                raise TransferError("下载分片超过声明范围")
                            stream.write(chunk)
                            counts[index] += len(chunk)
                            await report()
                if expected is not None and counts[index] != expected:
                    raise TransferError("下载字节数不足，正在恢复", retryable=True)
            await retry(transfer)

        tasks = [asyncio.create_task(fetch(index), name=f"lora-transfer-{index}") for index in range(split)]
        try:
            await asyncio.gather(*tasks)
        except BaseException as exc:
            # gather alone leaves siblings writing after the first failure.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(exc, RangeUnavailable):
                # A new identity prevents a previous partial file being reused.
                client_single = _SingleClient(client)
                return await download(client_single, url, destination, parallel=False, progress_cb=progress_cb)
            raise
        if not sum(counts):
            raise TransferError("来源返回了空文件")
        assembled = work / "assembled"
        if assembled.is_symlink():
            raise TransferError("下载合并文件不能是符号链接")
        with assembled.open("wb") as output:
            for path in paths:
                with path.open("rb") as part:
                    while chunk := part.read(1024 * 1024):
                        output.write(chunk)
                        await asyncio.sleep(0)
        if total and assembled.stat().st_size != total:
            raise TransferError("下载合并后的文件大小不匹配")
        assembled.replace(destination)
        await report(force=True)
        # Remove only files this transfer owns; preserve unknown directory data.
        for path in [*paths, manifest]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            work.rmdir()
        except OSError:
            pass
        return destination


class _SingleClient:
    """Disable resuming when a server advertises ranges but then ignores them."""
    def __init__(self, client):
        self.__dict__.update(client.__dict__)
        self.split = 1
        self._headers = client._headers
        self.source_key = client.source_key
        self._no_ranges = True
