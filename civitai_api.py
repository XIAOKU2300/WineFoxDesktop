import asyncio
import html
import json
import logging
import os
import random
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import aiohttp
from aiohttp import ClientConnectorError, ClientProxyConnectionError
try:
    from astrbot.api import logger
except ImportError:  # Desktop sidecar deliberately does not bundle AstrBot.
    logger = logging.getLogger("winefox.civitai")


CIVITAI_API_BASE = "https://civitai.com/api/v1"
CIVITAI_SEARCH_INDEX = "models_v9"
CIVITAI_SEARCH_PAGE = "https://civitai.com/search/models"
CIVITAI_SEARCH_FALLBACK_HOST = "https://search-new.civitai.com"
CIVITAI_SEARCH_FALLBACK_CLIENT_KEY = "8c46eb2508e21db1e9828a97968d91ab1ca1caa5f70a00e88a2ba1e286603b61"
HUGGINGFACE_API_BASE = "https://huggingface.co/api"
ALLOWED_MODEL_TYPES = {"lora", "locon", "lycoris"}
COMMON_CHARACTER_ALIASES = {}
GENERIC_ALIAS_WORDS = {
    "1girl", "1boy", "solo", "girl", "boy", "blue eyes", "red eyes", "black hair",
    "white hair", "long hair", "short hair", "looking at viewer", "masterpiece",
    "grey hair", "gray hair", "blonde hair", "silver hair", "crossed bangs",
    "side braid", "hair tubes", "ahoge", "bob cut", "air",
    "best quality", "high quality", "anime illustration", "wuthering waves", "wuwa",
    "genshin impact", "honkai impact", "honkai star rail", "cyberpunk",
    "cyberpunk 2077", "cyberpunk edgerunners", "edgerunners", "赛博朋克",
    "plastic memories", "clannad", "bishoujo mangekyou", "illustrious", "anima",
    "pony", "sdxl", "noob", "style", "styles", "lora", "model", "official",
    "character", "cute girl", "cute", "kawaii", "small girl", "little girl",
    "鸣潮", "原神", "崩坏", "崩坏星穹铁道",
}
LORA_PROFILE_SCHEMA_VERSION = 3
DEFAULT_HEADERS = {
    "User-Agent": "astrbot-plugin-comfyui-pro-UltraPlusMax/0.1.39 (+https://github.com/XIAOKU2300/astrbot_plugin_comfyui_pro_UltraPlusMax)"
}
ADAPTER_JSON_MAX_BYTES = 32 * 1024 * 1024
ADAPTER_TEXT_MAX_BYTES = 8 * 1024 * 1024


def safe_filename(name: str) -> str:
    name = str(name or "").strip() or "civitai_lora.safetensors"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180]


def normalize_key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


_SOURCE_ALIAS_CACHE = None
_SOURCE_TERMS_CACHE = None
_PROXY_CIRCUITS = {}


def _normalize_source_alias_map(data) -> dict:
    if isinstance(data, dict) and isinstance(data.get("sources"), dict):
        data = data.get("sources")
    if not isinstance(data, dict):
        return {}
    result = {}
    for source, aliases in data.items():
        source = normalize_key(source)
        if not source:
            continue
        if isinstance(aliases, str):
            aliases = re.split(r"[,，;；\n\r]+", aliases)
        elif not isinstance(aliases, (list, tuple, set)):
            aliases = []
        cleaned = []
        seen = {normalize_key(source)}
        for alias in aliases:
            alias = normalize_key(alias)
            marker = normalize_key(alias)
            if alias and marker and marker not in seen:
                seen.add(marker)
                cleaned.append(alias)
        result[source] = cleaned
    return result


def _merge_source_alias_json_pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs or []:
        if key not in result:
            result[key] = value
            continue
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged = dict(existing)
            for child_key, child_value in value.items():
                if child_key in merged:
                    old_value = merged.get(child_key)
                    old_list = old_value if isinstance(old_value, list) else [old_value]
                    new_list = child_value if isinstance(child_value, list) else [child_value]
                    merged[child_key] = old_list + new_list
                else:
                    merged[child_key] = child_value
            result[key] = merged
        else:
            old_list = existing if isinstance(existing, list) else [existing]
            new_list = value if isinstance(value, list) else [value]
            result[key] = old_list + new_list
    return result


def load_lora_source_alias_map(extra_paths=None) -> dict:
    global _SOURCE_ALIAS_CACHE, _SOURCE_TERMS_CACHE
    paths = [Path(__file__).with_name("lora_source_aliases.json")]
    for extra_path in extra_paths or []:
        try:
            extra_path = Path(extra_path)
        except Exception:
            continue
        if str(extra_path) not in {str(path) for path in paths}:
            paths.append(extra_path)
    signature = []
    for path in paths:
        try:
            signature.append((str(path), path.stat().st_mtime_ns if path.exists() else -1))
        except Exception:
            signature.append((str(path), -1))
    if isinstance(_SOURCE_ALIAS_CACHE, dict) and _SOURCE_ALIAS_CACHE.get("signature") == signature:
        return dict(_SOURCE_ALIAS_CACHE.get("aliases") or {})
    aliases = {}
    for path in paths:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_merge_source_alias_json_pairs)
            for source, values in _normalize_source_alias_map(data).items():
                current = aliases.setdefault(source, [])
                seen = {normalize_key(source)}
                seen.update(normalize_key(item) for item in current)
                for value in values:
                    marker = normalize_key(value)
                    if marker and marker not in seen:
                        seen.add(marker)
                        current.append(value)
        except Exception as e:
            logger.warning(f"[ComfyUI] 读取 LoRA 来源别名表失败 {path}: {e}")
    _SOURCE_ALIAS_CACHE = {"signature": signature, "aliases": dict(aliases)}
    _SOURCE_TERMS_CACHE = None
    return aliases


def lora_source_alias_terms(extra_paths=None) -> set:
    global _SOURCE_TERMS_CACHE
    alias_map = load_lora_source_alias_map(extra_paths=extra_paths)
    cache = _SOURCE_ALIAS_CACHE
    signature = cache.get("signature") if isinstance(cache, dict) else None
    if isinstance(_SOURCE_TERMS_CACHE, dict) and _SOURCE_TERMS_CACHE.get("signature") == signature:
        return set(_SOURCE_TERMS_CACHE.get("terms") or frozenset())
    values = set()
    for source, aliases in alias_map.items():
        values.add(str(source or "").strip())
        values.update(str(alias or "").strip() for alias in aliases or [])
    terms = frozenset(value for value in values if value)
    _SOURCE_TERMS_CACHE = {"signature": signature, "terms": terms}
    return set(terms)


def is_generic_alias_word(text: str) -> bool:
    raw = normalize_key(text)
    compact = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", raw)
    generic = {re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", x) for x in GENERIC_ALIAS_WORDS}
    if not compact or compact in generic:
        return True
    if re.fullmatch(r"\d+(girl|boy|girls|boys|person|people)", compact):
        return True
    if re.fullmatch(r"(black|white|blue|red|green|gold|blonde|silver|pink|purple|brown)(hair|eyes)", compact):
        return True
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", raw):
        return len(compact) < 2
    return len(compact) < 4


def _safe_int(value) -> int:
    try:
        if value is None or value == "":
            return 0
        return max(0, int(float(value)))
    except Exception:
        return 0


class CivitaiClient:
    def __init__(
        self,
        api_token: str = "",
        proxy: str = "",
        timeout: int = 60,
        cookie: str = "",
        downloader: str = "multithread",
        aria2c_path: str = "aria2c",
        split: int = 8,
        site_base: str = "https://civitai.com",
        source_key: str = "civitai",
        use_web_search_fallback: bool = True,
        request_adapter=None,
        proxy_circuit_breaker: bool = True,
        proxy_circuit_breaker_cooldown: int = 60,
        network_max_retries: int = 2,
    ):
        self.api_token = str(api_token or "").strip()
        self.proxy = str(proxy or "").strip()
        self.timeout = max(10, int(timeout or 60))
        self.cookie = str(cookie or "").strip()
        self.downloader = str(downloader or "aiohttp").strip().lower()
        self.aria2c_path = str(aria2c_path or "aria2c").strip() or "aria2c"
        self.split = max(1, min(int(split or 8), 16))
        self.site_base = str(site_base or "https://civitai.com").strip().rstrip("/") or "https://civitai.com"
        if not re.match(r"^https?://", self.site_base, re.I):
            self.site_base = "https://civitai.com"
        self.api_base = f"{self.site_base}/api/v1"
        self.search_page = f"{self.site_base}/search/models"
        self.source_key = str(source_key or "civitai").strip().lower() or "civitai"
        self.use_web_search_fallback = bool(use_web_search_fallback)
        self._web_search_config = None
        self.request_adapter = request_adapter
        self.proxy_circuit_breaker = bool(proxy_circuit_breaker)
        self.proxy_circuit_breaker_cooldown = max(5, int(proxy_circuit_breaker_cooldown or 60))
        self.network_max_retries = max(1, min(int(network_max_retries or 2), 5))

    def _headers(self):
        headers = dict(DEFAULT_HEADERS)
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        if self.cookie:
            headers["Cookie"] = self.cookie
            headers["Referer"] = f"{self.site_base}/"
        return headers

    def _model_page_url(self, model_id, version_id=None) -> str:
        url = f"{self.site_base}/models/{int(model_id)}"
        if version_id:
            url = f"{url}?modelVersionId={int(version_id)}"
        return url

    def _download_url_for_version(self, version_id) -> str:
        return f"{self.site_base}/api/download/models/{int(version_id)}"

    def _normalize_site_host_url(self, url: str) -> str:
        url = str(url or "").strip()
        if not url:
            return ""
        return re.sub(
            r"^https?://(?:www\.)?(?:civitai\.com|civitai\.red|civitaired\.com)",
            self.site_base,
            url,
            flags=re.I,
        )

    def _format_network_error(self, exc: Exception) -> RuntimeError:
        blocked = self._proxy_circuit_block_message()
        if blocked:
            return RuntimeError(blocked)
        if self.proxy and isinstance(exc, (ClientProxyConnectionError, ClientConnectorError, TimeoutError, OSError)):
            return RuntimeError(
                f"无法连接 Civitai 代理：{self.proxy}。请检查 Clash 是否开启 Allow LAN、Mixed Port 是否正确、"
                f"防火墙是否放行，以及 AstrBot/Docker 是否能访问该局域网 IP。原始错误：{exc}"
            )
        if isinstance(exc, TimeoutError):
            return RuntimeError(f"连接 Civitai 超时，请检查网络或配置 civitai_settings.proxy。原始错误：{exc}")
        return RuntimeError(str(exc))

    def _proxy_circuit_key(self) -> str:
        return str(self.proxy or "").strip()

    def _proxy_circuit_state(self) -> dict:
        key = self._proxy_circuit_key()
        if not key:
            return {}
        state = _PROXY_CIRCUITS.get(key)
        if not isinstance(state, dict):
            return {}
        if float(state.get("blocked_until") or 0) <= time.time():
            _PROXY_CIRCUITS.pop(key, None)
            return {}
        return state

    def _proxy_circuit_block_message(self) -> str:
        state = self._proxy_circuit_state()
        if not state:
            return ""
        remain = max(1, int(float(state.get("blocked_until") or time.time()) - time.time()))
        reason = str(state.get("reason") or "").strip()
        return (
            f"代理 {self.proxy} 暂不可用，已触发全局熔断，剩余约 {remain}s。"
            "请检查 Clash Allow LAN/Mixed Port、防火墙、Docker/服务器到该代理的网络；"
            f"原始错误：{reason}"
        )

    def _check_proxy_circuit(self):
        blocked = self._proxy_circuit_block_message()
        if blocked:
            raise RuntimeError(blocked)

    def _is_proxy_network_error(self, exc: Exception) -> bool:
        return bool(self.proxy and isinstance(exc, (ClientProxyConnectionError, ClientConnectorError, TimeoutError, OSError)))

    def _mark_proxy_failure(self, exc: Exception):
        key = self._proxy_circuit_key()
        if not key or not self.proxy_circuit_breaker:
            return
        _PROXY_CIRCUITS[key] = {
            "blocked_until": time.time() + self.proxy_circuit_breaker_cooldown,
            "reason": str(exc),
            "updated_at": time.time(),
        }

    def _mark_proxy_success(self):
        key = self._proxy_circuit_key()
        if key:
            _PROXY_CIRCUITS.pop(key, None)

    async def _adapter_request_text(
        self,
        method: str,
        url: str,
        payload: dict = None,
        headers: dict = None,
        target: str = "Civitai API",
        max_retries: int = None,
        max_bytes: int = None,
        fail_on_truncated: bool = False,
    ) -> str:
        if not self.request_adapter:
            raise RuntimeError("request adapter is not configured")
        retry_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
        attempts = max(1, min(int(max_retries or self.network_max_retries), 5))
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                data = await self.request_adapter(
                    method=str(method or "GET").upper(),
                    url=url,
                    headers=headers or self._headers(),
                    json_payload=payload,
                    target=target,
                    max_bytes=max_bytes,
                )
                if not isinstance(data, dict):
                    raise RuntimeError("Bridge fetch returned a non-JSON response")
                status = int(data.get("status") or 0)
                text = str(data.get("text") or "")
                if status == 200:
                    if fail_on_truncated and data.get("truncated"):
                        got = int(data.get("bytes") or len(text.encode("utf-8", errors="ignore")))
                        limit = int(max_bytes or got or 0)
                        raise RuntimeError(
                            f"{target} Bridge fetch response was truncated at {got} bytes"
                            + (f" (limit {limit} bytes)" if limit else "")
                        )
                    return text
                last_error = self._format_http_error(target, status, text)
                retryable_status = status in retry_statuses
                if status == 408 and "temporarily overloaded" in text.lower():
                    retryable_status = False
                if retryable_status and attempt < attempts:
                    response_headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
                    retry_after = response_headers.get("Retry-After") or response_headers.get("retry-after")
                    try:
                        delay = float(retry_after) if retry_after else min(1.2 * (2 ** (attempt - 1)), 8.0)
                    except Exception:
                        delay = min(1.2 * (2 ** (attempt - 1)), 8.0)
                    delay = max(0.5, min(delay + random.uniform(0, delay * 0.25), 10.0))
                    logger.info(f"[ComfyUI] {target} Bridge HTTP {status}, retry {attempt + 1}/{attempts} after {delay:.1f}s")
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(last_error)
            except RuntimeError as exc:
                last_error = str(exc)
                lowered = last_error.lower()
                non_retry_markers = (
                    "代理",
                    "proxy",
                    "熔断",
                    "circuit",
                    "cannot connect",
                    "temporarily overloaded",
                    "bridge 代理出网暂不可用",
                )
                if attempt < attempts and not any(marker in lowered or marker in last_error for marker in non_retry_markers):
                    delay = max(0.5, min(1.0 * attempt, 5.0))
                    logger.info(f"[ComfyUI] {target} Bridge fetch retry {attempt + 1}/{attempts} after {delay:.1f}s: {exc}")
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception as exc:
                last_error = str(exc)
                if attempt < attempts:
                    delay = max(0.5, min(1.0 * attempt, 5.0))
                    logger.info(f"[ComfyUI] {target} Bridge fetch retry {attempt + 1}/{attempts} after {delay:.1f}s: {exc}")
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(last_error) from exc
        raise RuntimeError(last_error or f"{target} Bridge fetch failed")

    def _format_json_decode_error(self, target: str, exc: Exception, text: str) -> str:
        size = len(str(text or "").encode("utf-8", errors="ignore"))
        position = getattr(exc, "pos", None)
        detail = str(exc or "").strip()
        if position is not None:
            detail = f"{detail} at char {position}"
        return (
            f"{target} 返回的 JSON 不完整或已损坏（{size} bytes；{detail}）。"
            "这通常是 Bridge/代理/网络把响应截断了，请重试；如果持续出现，请更新 ComfyUI Bridge 并检查代理。"
        )

    async def _loads_json_with_retries(
        self,
        fetch_once,
        target: str,
        max_retries: int = None,
    ):
        attempts = max(1, min(int(max_retries or self.network_max_retries), 5))
        last_error = None
        for attempt in range(1, attempts + 1):
            text = ""
            try:
                text = await fetch_once()
                return json.loads(text)
            except json.JSONDecodeError as exc:
                last_error = self._format_json_decode_error(target, exc, text)
                if attempt < attempts:
                    delay = max(0.5, min(1.0 * attempt, 5.0))
                    logger.info(f"[ComfyUI] {target} JSON 解析失败，{delay:.1f}s 后重试 {attempt + 1}/{attempts}: {last_error}")
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(last_error) from exc
            except RuntimeError as exc:
                last_error = str(exc)
                lowered = last_error.lower()
                retryable = "truncated" in lowered or "json" in lowered
                if retryable and attempt < attempts:
                    delay = max(0.5, min(1.0 * attempt, 5.0))
                    logger.info(f"[ComfyUI] {target} Bridge JSON 响应异常，{delay:.1f}s 后重试 {attempt + 1}/{attempts}: {last_error}")
                    await asyncio.sleep(delay)
                    continue
                raise
        raise RuntimeError(last_error or f"{target} JSON 解析失败")

    async def _get_json_via_adapter(self, url: str, target: str = "Civitai API", max_retries: int = None, max_bytes: int = None):
        async def fetch_once():
            return await self._adapter_request_text(
                "GET",
                url,
                target=target,
                max_retries=max_retries,
                max_bytes=max_bytes or ADAPTER_JSON_MAX_BYTES,
                fail_on_truncated=True,
            )

        return await self._loads_json_with_retries(fetch_once, target=target, max_retries=max_retries)

    async def _get_text_via_adapter(self, url: str, target: str = "Civitai page", max_retries: int = None, max_bytes: int = None) -> str:
        return await self._adapter_request_text(
            "GET",
            url,
            target=target,
            max_retries=max_retries,
            max_bytes=max_bytes or ADAPTER_TEXT_MAX_BYTES,
        )

    async def _post_json_via_adapter(
        self,
        url: str,
        payload: dict,
        headers: dict = None,
        target: str = "Civitai API",
        max_retries: int = None,
        max_bytes: int = None,
    ):
        async def fetch_once():
            return await self._adapter_request_text(
                "POST",
                url,
                payload=payload or {},
                headers=headers or self._headers(),
                target=target,
                max_retries=max_retries,
                max_bytes=max_bytes or ADAPTER_JSON_MAX_BYTES,
                fail_on_truncated=True,
            )

        return await self._loads_json_with_retries(fetch_once, target=target, max_retries=max_retries)

    def parse_reference(self, ref: str) -> dict:
        """解析 Civitai URL / model id / version id。"""
        ref = str(ref or "").strip().strip("<>")
        if not ref:
            raise ValueError("缺少 Civitai 链接或 ID")

        if ref.isdigit():
            return {"model_id": int(ref), "version_id": None}

        parsed = urlparse(ref)
        query = parse_qs(parsed.query or "")
        version = query.get("modelVersionId") or query.get("modelVersion")
        if version and str(version[0]).isdigit():
            return {"model_id": None, "version_id": int(version[0])}

        m = re.search(r"/models/(\d+)", parsed.path or "")
        if m:
            return {"model_id": int(m.group(1)), "version_id": None}

        m = re.search(r"/api/download/models/(\d+)", parsed.path or "")
        if m:
            return {"model_id": None, "version_id": int(m.group(1))}

        raise ValueError("无法识别 Civitai 链接，请使用模型页链接、下载链接、模型ID或版本ID")

    def _format_http_error(self, target: str, status: int, text: str) -> str:
        raw = str(text or "")
        plain = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        plain = re.sub(r"\s+", " ", plain).strip()
        if status in {502, 503, 504}:
            return f"{target} 暂时不可用（HTTP {status}），请稍后重试，或检查 civitai_settings.proxy。"
        if status == 429:
            return f"{target} 请求过于频繁（HTTP 429），请稍后重试。"
        if status in {401, 403}:
            return f"{target} 拒绝访问（HTTP {status}），如需登录权限请设置 civitai_settings.cookie。"
        if plain:
            return f"{target} 返回 HTTP {status}: {plain[:160]}"
        return f"{target} 返回 HTTP {status}"

    async def _get_json(self, url: str):
        if self.request_adapter:
            return await self._get_json_via_adapter(url, target="Civitai API", max_retries=self.network_max_retries)
        timeout = aiohttp.ClientTimeout(total=self.timeout, connect=min(30, self.timeout))
        retry_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
        max_retries = self.network_max_retries
        try:
            last_error = None
            async with aiohttp.ClientSession(headers=self._headers(), timeout=timeout) as session:
                for attempt in range(1, max_retries + 1):
                    try:
                        self._check_proxy_circuit()
                        async with session.get(url, proxy=self.proxy or None) as resp:
                            self._mark_proxy_success()
                            text = await resp.text()
                            if resp.status == 200:
                                try:
                                    return json.loads(text)
                                except json.JSONDecodeError as exc:
                                    last_error = self._format_json_decode_error("Civitai API", exc, text)
                                    if attempt < max_retries:
                                        delay = max(0.5, min(1.0 * attempt, 5.0))
                                        logger.info(f"[ComfyUI] Civitai API JSON 解析失败，{delay:.1f}s 后重试 {attempt + 1}/{max_retries}: {last_error}")
                                        await asyncio.sleep(delay)
                                        continue
                                    raise RuntimeError(last_error) from exc
                            last_error = self._format_http_error("Civitai API", resp.status, text)
                            if resp.status in retry_statuses and attempt < max_retries:
                                retry_after = resp.headers.get("Retry-After")
                                try:
                                    delay = float(retry_after) if retry_after else min(1.5 * (2 ** (attempt - 1)), 15.0)
                                except Exception:
                                    delay = min(1.5 * (2 ** (attempt - 1)), 15.0)
                                delay = max(0.5, min(delay + random.uniform(0, delay * 0.5), 18.0))
                                logger.info(f"[ComfyUI] Civitai API HTTP {resp.status}，等待 {delay:.1f}s 后重试 {attempt + 1}/{max_retries}")
                                await asyncio.sleep(delay)
                                continue
                            raise RuntimeError(last_error)
                    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                        last_error = self._format_network_error(exc)
                        if self._is_proxy_network_error(exc):
                            self._mark_proxy_failure(exc)
                            raise RuntimeError(last_error)
                        if attempt < max_retries:
                            base_delay = min(1.5 * (2 ** (attempt - 1)), 15.0)
                            delay = max(0.5, min(base_delay + random.uniform(0, base_delay * 0.5), 18.0))
                            logger.info(f"[ComfyUI] Civitai API 网络抖动，等待 {delay:.1f}s 后重试 {attempt + 1}/{max_retries}: {exc}")
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(last_error)
            raise RuntimeError(last_error or "Civitai API 请求失败")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(self._format_network_error(exc)) from exc

    async def _get_text(self, url: str) -> str:
        if self.request_adapter:
            return await self._get_text_via_adapter(url, target="Civitai page", max_retries=self.network_max_retries)
        timeout = aiohttp.ClientTimeout(total=self.timeout, connect=min(30, self.timeout))
        retry_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
        max_retries = self.network_max_retries
        try:
            last_error = None
            async with aiohttp.ClientSession(headers=self._headers(), timeout=timeout) as session:
                for attempt in range(1, max_retries + 1):
                    try:
                        self._check_proxy_circuit()
                        async with session.get(url, proxy=self.proxy or None) as resp:
                            self._mark_proxy_success()
                            text = await resp.text()
                            if resp.status == 200:
                                return text
                            last_error = self._format_http_error("Civitai 页面", resp.status, text)
                            if resp.status in retry_statuses and attempt < max_retries:
                                retry_after = resp.headers.get("Retry-After")
                                try:
                                    delay = float(retry_after) if retry_after else min(1.5 * (2 ** (attempt - 1)), 15.0)
                                except Exception:
                                    delay = min(1.5 * (2 ** (attempt - 1)), 15.0)
                                delay = max(0.5, min(delay + random.uniform(0, delay * 0.5), 18.0))
                                logger.info(f"[ComfyUI] Civitai 页面 HTTP {resp.status}，等待 {delay:.1f}s 后重试 {attempt + 1}/{max_retries}")
                                await asyncio.sleep(delay)
                                continue
                            raise RuntimeError(last_error)
                    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                        last_error = self._format_network_error(exc)
                        if self._is_proxy_network_error(exc):
                            self._mark_proxy_failure(exc)
                            raise RuntimeError(last_error)
                        if attempt < max_retries:
                            base_delay = min(1.5 * (2 ** (attempt - 1)), 15.0)
                            delay = max(0.5, min(base_delay + random.uniform(0, base_delay * 0.5), 18.0))
                            logger.info(f"[ComfyUI] Civitai 页面网络抖动，等待 {delay:.1f}s 后重试 {attempt + 1}/{max_retries}: {exc}")
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(last_error)
            raise RuntimeError(last_error or "Civitai 页面请求失败")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(self._format_network_error(exc)) from exc

    async def _post_json(self, url: str, payload: dict, headers: dict = None, target: str = "Civitai API"):
        if self.request_adapter:
            request_headers = dict(DEFAULT_HEADERS)
            request_headers.update(headers or {})
            return await self._post_json_via_adapter(
                url,
                payload or {},
                headers=request_headers,
                target=target,
                max_retries=self.network_max_retries,
            )
        timeout = aiohttp.ClientTimeout(total=self.timeout, connect=min(30, self.timeout))
        retry_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
        max_retries = self.network_max_retries
        request_headers = dict(DEFAULT_HEADERS)
        request_headers.update(headers or {})
        try:
            last_error = None
            async with aiohttp.ClientSession(headers=request_headers, timeout=timeout) as session:
                for attempt in range(1, max_retries + 1):
                    try:
                        self._check_proxy_circuit()
                        async with session.post(url, json=payload or {}, proxy=self.proxy or None) as resp:
                            self._mark_proxy_success()
                            text = await resp.text()
                            if resp.status == 200:
                                try:
                                    return json.loads(text)
                                except json.JSONDecodeError as exc:
                                    last_error = self._format_json_decode_error(target, exc, text)
                                    if attempt < max_retries:
                                        delay = max(0.5, min(1.0 * attempt, 5.0))
                                        logger.info(f"[ComfyUI] {target} JSON 解析失败，{delay:.1f}s 后重试 {attempt + 1}/{max_retries}: {last_error}")
                                        await asyncio.sleep(delay)
                                        continue
                                    raise RuntimeError(last_error) from exc
                            last_error = self._format_http_error(target, resp.status, text)
                            if resp.status in retry_statuses and attempt < max_retries:
                                retry_after = resp.headers.get("Retry-After")
                                try:
                                    delay = float(retry_after) if retry_after else min(1.2 * (2 ** (attempt - 1)), 12.0)
                                except Exception:
                                    delay = min(1.2 * (2 ** (attempt - 1)), 12.0)
                                delay = max(0.5, min(delay + random.uniform(0, delay * 0.5), 15.0))
                                logger.info(f"[ComfyUI] {target} HTTP {resp.status}，等待 {delay:.1f}s 后重试 {attempt + 1}/{max_retries}")
                                await asyncio.sleep(delay)
                                continue
                            raise RuntimeError(last_error)
                    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                        last_error = self._format_network_error(exc)
                        if self._is_proxy_network_error(exc):
                            self._mark_proxy_failure(exc)
                            raise RuntimeError(last_error)
                        if attempt < max_retries:
                            base_delay = min(1.2 * (2 ** (attempt - 1)), 12.0)
                            delay = max(0.5, min(base_delay + random.uniform(0, base_delay * 0.5), 15.0))
                            logger.info(f"[ComfyUI] {target} 网络抖动，等待 {delay:.1f}s 后重试 {attempt + 1}/{max_retries}: {exc}")
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(last_error)
            raise RuntimeError(last_error or f"{target} 请求失败")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(self._format_network_error(exc)) from exc

    async def test_connection(self) -> dict:
        """测试 Civitai API/代理连通性。"""
        data = await self._get_json(f"{self.api_base}/models?limit=1")
        return {
            "ok": True,
            "proxy": self.proxy,
            "items": len(data.get("items", []) if isinstance(data, dict) else []),
        }

    async def get_model(self, model_id: int) -> dict:
        return await self._get_json(f"{self.api_base}/models/{int(model_id)}")

    async def get_model_version(self, version_id: int) -> dict:
        return await self._get_json(f"{self.api_base}/model-versions/{int(version_id)}")

    def _extract_web_search_config_from_text(self, text: str) -> dict:
        text = str(text or "")
        if not text:
            return {}
        host_match = re.search(r'NEXT_PUBLIC_SEARCH_HOST\s*:\s*"([^"]+)"', text)
        key_match = re.search(r'NEXT_PUBLIC_SEARCH_CLIENT_KEY\s*:\s*"([^"]+)"', text)
        if not host_match or not key_match:
            return {}
        host = html.unescape(host_match.group(1)).strip().rstrip("/")
        key = html.unescape(key_match.group(1)).strip()
        if not host or not key or not re.match(r"^https?://", host, re.I):
            return {}
        return {"host": host, "key": key, "index": CIVITAI_SEARCH_INDEX, "source": "frontend"}

    async def _get_web_search_config(self) -> dict:
        cached = self._web_search_config if isinstance(self._web_search_config, dict) else None
        if cached and float(cached.get("expires_at") or 0) > time.time():
            return dict(cached)

        params = {"query": "lora", "sortBy": CIVITAI_SEARCH_INDEX, "type": "LORA"}
        search_url = f"{self.search_page}?{urlencode(params)}"
        config = {}
        try:
            html_text = await self._get_text(search_url)
            config = self._extract_web_search_config_from_text(html_text)
            if not config:
                scripts = re.findall(
                    r'<script\b[^>]+src=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']',
                    html_text,
                    flags=re.I,
                )
                scripts = list(dict.fromkeys(scripts))
                scripts.sort(key=lambda src: 0 if ("pages/_app" in src or "95369" in src or "_app" in src) else 1)
                for src in scripts[:28]:
                    try:
                        script_text = await self._get_text(urljoin(self.site_base, src))
                        config = self._extract_web_search_config_from_text(script_text)
                        if config:
                            break
                    except Exception:
                        continue
        except Exception as e:
            logger.info(f"[ComfyUI] Civitai 网页搜索配置发现失败，使用内置公开兜底: {e}")

        if not config and self.use_web_search_fallback:
            config = {
                "host": CIVITAI_SEARCH_FALLBACK_HOST,
                "key": CIVITAI_SEARCH_FALLBACK_CLIENT_KEY,
                "index": CIVITAI_SEARCH_INDEX,
                "source": "fallback",
            }
        if not config:
            return {}
        config["expires_at"] = time.time() + (6 * 3600 if config.get("source") == "frontend" else 1800)
        self._web_search_config = dict(config)
        return dict(config)

    def _meili_filter_value(self, value: str) -> str:
        value = str(value or "").strip()
        value = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{value}"'

    def _build_web_search_filters(self, base_models=None) -> list:
        filters = ["type = LORA", "availability != Private", "minor != true", "poi != true"]
        base_models = [str(x).strip() for x in (base_models or []) if str(x).strip()]
        if base_models:
            if len(base_models) == 1:
                filters.append(f"version.baseModel = {self._meili_filter_value(base_models[0])}")
            else:
                values = ", ".join(self._meili_filter_value(item) for item in base_models)
                filters.append(f"version.baseModel IN [{values}]")
        return filters

    async def _search_loras_via_meilisearch(self, query: str, limit: int, base_models=None) -> list:
        config = await self._get_web_search_config()
        host = str(config.get("host") or "").strip().rstrip("/")
        key = str(config.get("key") or "").strip()
        index = str(config.get("index") or CIVITAI_SEARCH_INDEX).strip() or CIVITAI_SEARCH_INDEX
        if not host or not key:
            return []
        fetch_limit = min(max(limit * 4, 50), 120)
        payload = {
            "q": query,
            "limit": fetch_limit,
            "offset": 0,
            "filter": self._build_web_search_filters(base_models=base_models),
            "attributesToHighlight": [],
            "matchingStrategy": "all",
            "showRankingScore": True,
        }
        data = await self._post_json(
            f"{host}/indexes/{index}/search",
            payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json; charset=utf-8",
                "Referer": f"{self.site_base}/",
            },
            target="Civitai 网页搜索",
        )
        hits = data.get("hits", []) if isinstance(data, dict) else []
        if not isinstance(hits, list) or not hits:
            return []
        results = await self._normalize_meili_lora_hits(hits, limit=limit, base_models=base_models)
        logger.info(
            f"[ComfyUI] Civitai Meilisearch 搜索完成: query={query} "
            f"hits={len(hits)} results={len(results)} source={config.get('source') or '-'}"
        )
        return results

    async def _normalize_meili_lora_hits(self, hits: list, limit: int, base_models=None) -> list:
        if not isinstance(hits, list) or not hits:
            return []
        detail_limit = min(len(hits), max(limit, 12), 24)
        detail_semaphore = asyncio.Semaphore(4)

        async def fetch_detail(hit: dict):
            model_id = _safe_int((hit or {}).get("id") or (hit or {}).get("modelId"))
            if not model_id:
                return None
            async with detail_semaphore:
                try:
                    detail = await self.get_model(model_id)
                    if isinstance(detail, dict):
                        version = hit.get("version") if isinstance(hit, dict) else {}
                        detail["_preferred_version_id"] = (version or {}).get("id") or hit.get("modelVersionId")
                        detail["_meili_ranking_score"] = hit.get("_rankingScore")
                        return detail
                except Exception as e:
                    logger.info(f"[ComfyUI] Civitai Meilisearch 详情补全失败 model={model_id}: {e}")
            return None

        detail_tasks = [fetch_detail(hit) for hit in hits[:detail_limit] if isinstance(hit, dict)]
        detail_items = await asyncio.gather(*detail_tasks, return_exceptions=True) if detail_tasks else []
        details_by_model = {}
        for item in detail_items:
            if isinstance(item, dict):
                model_id = str(item.get("id") or item.get("modelId") or "")
                if model_id:
                    details_by_model[model_id] = item

        results = []
        seen = set()
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            model_id = str(hit.get("id") or hit.get("modelId") or "")
            item = details_by_model.get(model_id) or hit
            normalized = self._normalize_web_search_item(item, base_models=base_models)
            if not normalized and item is not hit:
                normalized = self._normalize_web_search_item(hit, base_models=base_models)
            if not normalized:
                continue
            key = str(normalized.get("version_id") or normalized.get("model_id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            if hit.get("_rankingScore") is not None:
                normalized["_web_search_score"] = hit.get("_rankingScore")
            results.append(normalized)
            if len(results) >= limit:
                break
        return results

    async def search_loras(self, query: str = "lora", limit: int = 10, base_models=None) -> list:
        """Search Civitai LoRA models. CJK queries try web full-text search before REST API."""
        query = str(query or "lora").strip() or "lora"
        limit = max(1, min(int(limit or 10), 60))
        base_models = [str(x).strip() for x in (base_models or ["Anima"]) if str(x).strip()] or ["Anima"]
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", query):
            try:
                web_results = await self.search_loras_via_web(query=query, limit=limit, base_models=base_models)
                if web_results:
                    return web_results
            except Exception as e:
                logger.warning(f"[ComfyUI] Civitai 网页搜索异常，回退 REST API: {e}")
        return await self._search_loras_rest_api(query=query, limit=limit, base_models=base_models)

    async def search_loras_via_web(self, query: str = "lora", limit: int = 10, base_models=None) -> list:
        """Use Civitai's public web search backend for CJK-friendly full-text results."""
        query = str(query or "lora").strip() or "lora"
        limit = max(1, min(int(limit or 10), 60))
        base_models = [str(x).strip() for x in (base_models or []) if str(x).strip()]

        try:
            meili_results = await self._search_loras_via_meilisearch(query=query, limit=limit, base_models=base_models)
            if meili_results:
                return meili_results
            logger.info(f"[ComfyUI] Civitai Meilisearch 未命中可用 LoRA，尝试页面数据兜底: {query}")
        except Exception as e:
            self._web_search_config = None
            logger.warning(f"[ComfyUI] Civitai Meilisearch 搜索异常，尝试页面数据兜底: {e}")

        params = {
            "query": query,
            "sortBy": CIVITAI_SEARCH_INDEX,
            "type": "LORA",
        }
        if base_models:
            params["baseModel"] = base_models[0]
        search_url = f"{self.search_page}?{urlencode(params)}"
        try:
            html_text = await self._get_text(search_url)
        except Exception as e:
            logger.warning(f"[ComfyUI] Civitai 网页搜索请求失败，回退 REST API: {e}")
            return []
        raw_items = self._parse_next_data_search_results(html_text, base_models=base_models)
        if not raw_items:
            logger.info(f"[ComfyUI] Civitai 网页搜索未命中可用结果，回退 REST API: {query}")
            return []
        results = []
        seen = set()
        for item in raw_items:
            normalized = self._normalize_web_search_item(item, base_models=base_models)
            if not normalized:
                continue
            key = str(normalized.get("version_id") or normalized.get("model_id") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(normalized)
            if len(results) >= limit:
                break
        logger.info(f"[ComfyUI] Civitai 网页搜索完成: query={query} results={len(results)}")
        return results

    def _parse_next_data_search_results(self, html_text: str, base_models=None) -> list:
        html_text = str(html_text or "")
        if not html_text:
            return []
        match = re.search(
            r'<script\b(?=[^>]*\bid=["\']__NEXT_DATA__["\'])[^>]*>\s*(.*?)\s*</script>',
            html_text,
            flags=re.I | re.S,
        )
        if match:
            try:
                data = json.loads(html.unescape(match.group(1)))
                items = self._extract_models_from_next_data(data, base_models=base_models)
                if items:
                    return items
            except Exception as e:
                logger.warning(f"[ComfyUI] Civitai __NEXT_DATA__ 搜索结果解析失败: {e}")
        for pattern in (
            r'"items"\s*:\s*(\[\s*\{.{100,}?\}\s*\])',
            r'"models"\s*:\s*(\[\s*\{.{100,}?\}\s*\])',
            r'"hits"\s*:\s*(\[\s*\{.{100,}?\}\s*\])',
        ):
            match = re.search(pattern, html_text, flags=re.S)
            if not match:
                continue
            try:
                items = json.loads(html.unescape(match.group(1)))
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    return items
            except Exception:
                continue
        return []

    def _extract_models_from_next_data(self, data, base_models=None) -> list:
        candidates = []

        def looks_like_model(item: dict) -> bool:
            if not isinstance(item, dict):
                return False
            if not (item.get("id") or item.get("modelId")):
                return False
            if not (item.get("name") or item.get("title")):
                return False
            model_type = str(item.get("type") or item.get("modelType") or "").lower()
            return (
                model_type in {"lora", "locon", "lycoris"}
                or isinstance(item.get("modelVersions") or item.get("versions"), list)
            )

        def walk(obj, depth: int = 0):
            if depth > 12 or len(candidates) >= 240:
                return
            if isinstance(obj, dict):
                if looks_like_model(obj):
                    candidates.append(obj)
                    return
                for key in ("items", "models", "hits", "results", "data", "pages", "records"):
                    if key in obj:
                        walk(obj.get(key), depth + 1)
                for key, value in obj.items():
                    if key in {"items", "models", "hits", "results", "data", "pages", "records"}:
                        continue
                    walk(value, depth + 1)
            elif isinstance(obj, list):
                if obj and all(isinstance(item, dict) for item in obj[: min(len(obj), 3)]):
                    model_items = [item for item in obj if looks_like_model(item)]
                    if model_items:
                        candidates.extend(model_items)
                        return
                for item in obj:
                    walk(item, depth + 1)

        walk(data)
        seen = set()
        result = []
        for item in candidates:
            key = str(item.get("id") or item.get("modelId") or "")
            if key and key not in seen:
                seen.add(key)
                result.append(item)
        return result

    def _normalize_web_search_item(self, item: dict, base_models=None) -> dict:
        if not isinstance(item, dict):
            return {}
        versions = []
        current_version = item.get("version")
        if isinstance(current_version, dict):
            versions.append(current_version)
        raw_versions = item.get("modelVersions") or item.get("versions") or []
        if isinstance(raw_versions, list):
            for version in raw_versions:
                if not isinstance(version, dict):
                    continue
                version_id = str(version.get("id") or "")
                if version_id and any(str(existing.get("id") or "") == version_id for existing in versions):
                    continue
                versions.append(version)
        base_models = [str(x).strip() for x in (base_models or []) if str(x).strip()]

        def is_allowed_base(base: str) -> bool:
            if not base_models:
                return True
            base_norm = normalize_key(base)
            return any(normalize_key(model) in base_norm for model in base_models)

        selected_version = None
        preferred_version_id = _safe_int(
            item.get("_preferred_version_id")
            or item.get("modelVersionId")
            or item.get("versionId")
            or (current_version or {}).get("id")
        )
        if preferred_version_id:
            for version in versions:
                if (
                    isinstance(version, dict)
                    and _safe_int(version.get("id")) == preferred_version_id
                    and is_allowed_base(version.get("baseModel") or "")
                ):
                    selected_version = version
                    break
        for version in versions:
            if selected_version is None and isinstance(version, dict) and is_allowed_base(version.get("baseModel") or ""):
                selected_version = version
                break
        if selected_version is None and versions and not base_models:
            selected_version = versions[0] if isinstance(versions[0], dict) else {}
        if selected_version is None:
            return {}
        model_id = item.get("id") or item.get("modelId")
        version_id = selected_version.get("id") or item.get("modelVersionId") or item.get("versionId")
        if not model_id:
            return {}
        model_stats = item.get("stats") or item.get("metrics") or item.get("rank") or {}
        version_stats = selected_version.get("stats") or selected_version.get("metrics") or {}
        preview_url, preview_nsfw_level, preview_hidden_count = self._select_preview_image_from_web_item(item, selected_version)
        download_count = max(
            _safe_int(model_stats.get("downloadCount") or model_stats.get("downloads")),
            _safe_int(version_stats.get("downloadCount") or version_stats.get("downloads")),
        )
        heart_count = max(
            _safe_int(model_stats.get("thumbsUpCount") or model_stats.get("favoriteCount") or model_stats.get("likeCount") or model_stats.get("likes")),
            _safe_int(version_stats.get("thumbsUpCount") or version_stats.get("favoriteCount") or version_stats.get("likeCount") or version_stats.get("likes")),
        )
        return {
            "source": self.source_key,
            "model_id": model_id,
            "version_id": version_id,
            "version_name": selected_version.get("name") or "",
            "name": item.get("name") or item.get("title") or "Civitai LoRA",
            "type": item.get("type") or item.get("modelType") or "LORA",
            "base_model": selected_version.get("baseModel") or "",
            "trained_words": self._normalize_trained_words(selected_version.get("trainedWords") or item.get("triggerWords") or []),
            "stats": model_stats,
            "version_stats": version_stats,
            "download_count": download_count,
            "heart_count": heart_count,
            "preview_url": preview_url,
            "preview_nsfw_level": preview_nsfw_level,
            "preview_hidden_count": preview_hidden_count,
            "nsfw": bool(item.get("nsfw") or selected_version.get("nsfw")),
            "tags": self._normalize_web_tags(item.get("tags") or []),
            "page_url": self._model_page_url(model_id, version_id) if version_id else self._model_page_url(model_id),
        }

    def _normalize_web_tags(self, tags) -> list:
        result = []
        for tag in tags or []:
            if isinstance(tag, dict):
                value = tag.get("name") or tag.get("title") or tag.get("value")
            else:
                value = tag
            value = str(value or "").strip()
            if value:
                result.append(value)
        return result

    def _normalize_civitai_image_url(self, url: str, name: str = "") -> str:
        url = str(url or "").strip()
        if not url or re.match(r"^https?://", url, re.I):
            return url
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", url):
            safe_name = quote(str(name or "preview.jpeg").strip() or "preview.jpeg")
            return f"https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA/{url}/width=450/{safe_name}"
        return url

    def _select_preview_image_from_web_item(self, item: dict, version: dict) -> tuple:
        images = []
        if isinstance(version, dict):
            images.extend(version.get("images") or [])
        if isinstance(item, dict):
            images.extend(item.get("images") or [])
        fallback = ""
        fallback_level = 0
        hidden = 0
        for image in images:
            if not isinstance(image, dict):
                continue
            url = self._normalize_civitai_image_url(image.get("url") or "", image.get("name") or "")
            if not url:
                continue
            raw_level = image.get("nsfwLevel")
            if raw_level is None:
                raw_level = image.get("nsfw")
            if isinstance(raw_level, bool):
                level = 4 if raw_level else 1
            else:
                text = str(raw_level or "").strip().lower()
                if text in {"none", "false", "safe"}:
                    level = 1
                elif text in {"soft", "mature", "true", "nsfw", "x", "r"}:
                    level = 4
                else:
                    level = _safe_int(raw_level) or 1
            if not fallback:
                fallback = url
                fallback_level = level
            image_type = str(image.get("type") or "image").lower()
            if image_type and image_type != "image":
                hidden += 1
                continue
            if bool(image.get("minor")) or bool(image.get("poi")):
                hidden += 1
                continue
            return url, level, hidden
        return "", fallback_level, hidden + (1 if fallback else 0)

    async def _search_loras_rest_api(self, query: str = "lora", limit: int = 10, base_models=None) -> list:
        """Search Civitai LoRA models filtered by one or more baseModel names."""
        query = str(query or "lora").strip() or "lora"
        limit = max(1, min(int(limit or 10), 60))
        base_models = [str(x).strip() for x in (base_models or ["Anima"]) if str(x).strip()] or ["Anima"]

        def is_allowed_base(base: str) -> bool:
            base_norm = normalize_key(base)
            return any(normalize_key(item) in base_norm for item in base_models)

        def stat_value(stats: dict, *keys: str) -> int:
            stats = stats or {}
            values = [_safe_int(stats.get(key)) for key in keys]
            return max(values) if values else 0

        def select_preview_image(version: dict) -> tuple:
            images = version.get("images") or []
            fallback = ""
            fallback_level = 0
            hidden = 0

            def nsfw_level(image: dict) -> int:
                raw = image.get("nsfwLevel")
                if raw is None:
                    raw = image.get("nsfw")
                if isinstance(raw, bool):
                    return 4 if raw else 1
                text = str(raw or "").strip().lower()
                if text in {"none", "false", "safe"}:
                    return 1
                if text in {"soft", "mature", "true", "nsfw", "x", "r"}:
                    return 4
                return _safe_int(raw)

            for image in images:
                if not isinstance(image, dict):
                    continue
                url = str(image.get("url") or "").strip()
                if not url:
                    continue
                if not fallback:
                    fallback = url
                    fallback_level = nsfw_level(image)
                image_type = str(image.get("type") or "image").lower()
                level = nsfw_level(image)
                if image_type and image_type != "image":
                    hidden += 1
                    continue
                if bool(image.get("minor")) or bool(image.get("poi")):
                    hidden += 1
                    continue
                return url, level, hidden
            return "", fallback_level, hidden + (1 if fallback else 0)

        async def fetch_items(base_model: str, fetch_limit: int, sort: str):
            params = {
                "query": query,
                "types": "LORA",
                "sort": sort,
                "limit": max(1, min(int(fetch_limit or limit), 20)),
            }
            if base_model:
                params["baseModels"] = base_model
            data = await self._get_json(f"{self.api_base}/models?{urlencode(params)}")
            return data.get("items", []) if isinstance(data, dict) else []

        def append_results(items, results, seen):
            for item in items:
                if not isinstance(item, dict):
                    continue
                versions = item.get("modelVersions", []) or []
                selected_version = None
                for version in versions:
                    if is_allowed_base(version.get("baseModel") or ""):
                        selected_version = version
                        break
                if not selected_version:
                    continue
                key = str(selected_version.get("id") or item.get("id") or "")
                if key in seen:
                    continue
                seen.add(key)
                model_stats = item.get("stats") or {}
                version_stats = selected_version.get("stats") or {}
                download_count = max(
                    stat_value(model_stats, "downloadCount", "download_count", "downloads"),
                    stat_value(version_stats, "downloadCount", "download_count", "downloads"),
                )
                heart_count = max(
                    stat_value(model_stats, "thumbsUpCount", "favoriteCount", "likeCount", "likes"),
                    stat_value(version_stats, "thumbsUpCount", "favoriteCount", "likeCount", "likes"),
                )
                preview_url, preview_nsfw_level, preview_hidden_count = select_preview_image(selected_version)
                results.append({
                    "source": self.source_key,
                    "model_id": item.get("id"),
                    "version_id": selected_version.get("id"),
                    "version_name": selected_version.get("name") or "",
                    "name": item.get("name") or "Civitai LoRA",
                    "type": item.get("type") or "LORA",
                    "base_model": selected_version.get("baseModel") or "",
                    "trained_words": self._normalize_trained_words(selected_version.get("trainedWords") or []),
                    "stats": model_stats,
                    "version_stats": version_stats,
                    "download_count": download_count,
                    "heart_count": heart_count,
                    "preview_url": preview_url,
                    "preview_nsfw_level": preview_nsfw_level,
                    "preview_hidden_count": preview_hidden_count,
                    "nsfw": bool(item.get("nsfw") or selected_version.get("nsfw")),
                    "tags": [str(x).strip() for x in (item.get("tags") or []) if str(x).strip()],
                    "page_url": self._model_page_url(item.get('id'), selected_version.get('id')),
                })

        results = []
        discovery_results = []
        seen = set()
        discovery_seen = set()
        fetch_limit = max(limit, 12)
        sorts = ["Most Downloaded", "Highest Rated"]
        strict_results = await asyncio.gather(
            *[fetch_items(base, fetch_limit, sort) for base in base_models for sort in sorts],
            return_exceptions=True,
        )
        strict_items = []
        errors = []
        for item in strict_results:
            if isinstance(item, Exception):
                errors.append(item)
            else:
                strict_items.extend(item)
        loose_items = []
        if len(strict_items) < limit:
            loose_results = await asyncio.gather(
                *[fetch_items("", max(limit * 2, 12), sort) for sort in sorts],
                return_exceptions=True,
            )
            for item in loose_results:
                if isinstance(item, Exception):
                    errors.append(item)
                else:
                    loose_items.extend(item)
        if errors and not strict_items and not loose_items:
            raise errors[0]
        def append_discovery_items(items):
            for item in items:
                if not isinstance(item, dict):
                    continue
                versions = item.get("modelVersions", []) or []
                version = versions[0] if versions else {}
                key = str((version or {}).get("id") or item.get("id") or "")
                if not key or key in seen or key in discovery_seen:
                    continue
                discovery_seen.add(key)
                discovery_results.append({
                    "source": self.source_key,
                    "model_id": item.get("id"),
                    "version_id": (version or {}).get("id"),
                    "version_name": (version or {}).get("name") or "",
                    "name": item.get("name") or "Civitai LoRA",
                    "type": item.get("type") or "LORA",
                    "base_model": (version or {}).get("baseModel") or "",
                    "trained_words": self._normalize_trained_words((version or {}).get("trainedWords") or []),
                    "tags": [str(x).strip() for x in (item.get("tags") or []) if str(x).strip()],
                    "page_url": self._model_page_url(item.get('id'), (version or {}).get('id')) if (version or {}).get('id') else self._model_page_url(item.get('id')),
                    "_discovery_only": True,
                })

        append_results(strict_items, results, seen)
        append_discovery_items(strict_items)
        if len(results) < limit:
            append_results(loose_items, results, seen)
            append_discovery_items(loose_items)
        return results[:limit] + discovery_results[:limit]

    async def search_anima_loras(self, query: str = "lora", limit: int = 10) -> list:
        return await self.search_loras(query=query, limit=limit, base_models=["Anima"])

    async def resolve_model(self, ref: str, base_models=None) -> dict:
        parsed = self.parse_reference(ref)
        if parsed.get("version_id"):
            version = await self.get_model_version(parsed["version_id"])
            model_id = version.get("modelId") or version.get("model", {}).get("id")
            if not model_id:
                raise RuntimeError("版本信息缺少 modelId")
            model = await self.get_model(model_id)
            selected_version = self._find_version(model, int(parsed["version_id"])) or version
        else:
            model = await self.get_model(parsed["model_id"])
            selected_version = self._select_best_version(model, base_models=base_models)
        summary = self._build_summary(model, selected_version)
        if summary.get("trained_words"):
            return summary
        if not summary.get("page_text"):
            summary["page_text"] = await self._fetch_page_plain_text(summary.get("model_id"), summary.get("version_id"))
        page_words = self._extract_trigger_words_from_text(summary.get("page_text") or "") if summary.get("page_text") else []
        if page_words:
            logger.info(f"[ComfyUI] 从 Civitai 页面正文确认触发词：{', '.join(page_words[:8])}")
            summary["trained_words"] = page_words
            summary["trained_words_source"] = "page"
        elif not summary.get("trained_words"):
            page_words = await self._fetch_page_trigger_words(summary.get("model_id"), summary.get("version_id"))
            if page_words:
                summary["trained_words"] = page_words
                summary["trained_words_source"] = "page"
        return summary

    def _find_version(self, model: dict, version_id: int):
        for v in model.get("modelVersions", []) or []:
            if int(v.get("id") or 0) == int(version_id):
                return v
        return None

    def _select_best_version(self, model: dict, base_models=None):
        versions = model.get("modelVersions", []) or []
        if not versions:
            raise RuntimeError("该模型没有可用版本")
        base_models = [str(x).strip() for x in (base_models or ["Anima"]) if str(x).strip()] or ["Anima"]
        matched_versions = []
        for v in versions:
            base = normalize_key(v.get("baseModel") or "")
            if any(normalize_key(item) in base for item in base_models):
                matched_versions.append(v)
        return matched_versions[0] if matched_versions else versions[0]

    def _select_lora_file(self, version: dict):
        files = version.get("files", []) or []
        candidates = []
        for f in files:
            name = str(f.get("name") or "")
            ext_ok = name.lower().endswith((".safetensors", ".ckpt", ".pt"))
            ftype = normalize_key(f.get("type") or "")
            if ext_ok and ftype not in {"vae", "config"}:
                candidates.append(f)
        if not candidates:
            raise RuntimeError("该版本没有可下载的 LoRA 权重文件")
        safes = [f for f in candidates if str(f.get("name") or "").lower().endswith(".safetensors")]
        return safes[0] if safes else candidates[0]

    def _strip_html(self, text: str) -> str:
        text = html.unescape(str(text or ""))
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"</(p|div|li|h\d|section|article)>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()

    def _normalize_trained_words(self, values, limit: int = 32) -> list:
        words = []
        seen = set()
        for item in values or []:
            for part in re.split(r"[,，;；]", str(item or "")):
                word = self._clean_trigger_word(part)
                marker = normalize_key(word)
                if word and marker not in seen and self._is_valid_trigger_word(word):
                    seen.add(marker)
                    words.append(word)
                    if len(words) >= limit:
                        return words
        return words

    def _extract_jsonish_trained_words(self, text: str) -> list:
        raw = html.unescape(str(text or ""))
        candidates = []
        patterns = [
            r'"trainedWords"\s*:\s*(\[[^\]]{0,1600}\])',
            r'\\"trainedWords\\"\s*:\s*(\[[^\]]{0,1600}\])',
            r"'trainedWords'\s*:\s*(\[[^\]]{0,1600}\])",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, raw, flags=re.I):
                block = match.group(1).replace('\\"', '"')
                try:
                    values = json.loads(block)
                except Exception:
                    values = re.findall(r'["\']([^"\']{1,80})["\']', block)
                if isinstance(values, list):
                    candidates.extend(values)
        return self._normalize_trained_words(candidates)

    def _extract_trigger_words_from_text(self, text: str) -> list:
        json_words = self._extract_jsonish_trained_words(text)
        plain = self._strip_html(text)
        if not plain:
            return json_words[:32]
        section_words = self._extract_prompt_sections(plain)
        if section_words:
            return section_words[:32]
        if json_words:
            return json_words[:32]
        patterns = [
            r"(?:###\s*)?触发词\s*[:：]?\s*`([^`]+)`",
            r"(?:###\s*)?(?:lora|LoRA)\s*提示词\s*[:：]?\s*`([^`]+)`",
            r"(?:trigger\s*words?|trained\s*words?)\s*[:：]?\s*`([^`]+)`",
            r"(?:lora\s*)?prompt\s*[:：]?\s*`([^`]+)`",
            r"(?:###\s*)?触发词\s*[:：]?\s*([^#\n\r]+)",
            r"(?:###\s*)?(?:lora|LoRA)\s*提示词\s*[:：]?\s*([^#\n\r]+)",
            r"(?:trigger\s*words?|trained\s*words?)\s*[:：]?\s*([^#\n\r]+)",
            r"(?:lora\s*)?prompt\s*[:：]?\s*([^#\n\r]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, plain, flags=re.I)
            if not m:
                continue
            raw = self._collect_trigger_block(plain, m.end(), m.group(1))
            words = [self._clean_trigger_word(x) for x in re.split(r"[,，;；]", raw)]
            stop_words = {"触发词", "提示词", "lora提示词", "发饰", "服装", "二形态服装", "trigger words", "trained words", "lora prompt", "prompt", "outfit", "clothes"}
            words = [x for x in words if self._is_valid_trigger_word(x) and normalize_key(x) not in stop_words]
            if words:
                return words[:20]
        label = re.search(r"(?:lora\s*提示词|LoRA\s*prompt|lora\s*prompt|触发词|trigger\s*words?|trained\s*words?)\s*[:：]\s*", plain, flags=re.I)
        if label:
            raw = self._collect_trigger_block(plain, label.end(), "")
            words = [self._clean_trigger_word(x) for x in re.split(r"[,，;；]", raw)]
            stop_words = {"触发词", "提示词", "lora提示词", "trigger words", "trained words", "lora prompt", "prompt"}
            words = [x for x in words if self._is_valid_trigger_word(x) and normalize_key(x) not in stop_words]
            if words:
                return words[:20]
        return []

    def _extract_prompt_sections(self, plain: str) -> list:
        """优先读取 Civitai 正文里“触发词/发饰/服装”后面的反引号代码块，避免把说明文字混入。"""
        text = str(plain or "")
        text = re.split(r"\n\s*(?:##\s*)?Discussion\b", text, maxsplit=1, flags=re.I)[0]
        labels = r"(?:触发词|发饰|服装|二形态服装|trigger\s*words?|trained\s*words?|lora\s*prompt|prompt|outfit|clothes)"
        words = []
        for m in re.finditer(labels + r"\s*[:：]?\s*`([^`]{1,800})`", text, flags=re.I):
            block = m.group(1)
            for part in re.split(r"[,，;；\n\r]+", block):
                word = self._clean_trigger_word(part)
                if word and self._is_valid_trigger_word(word):
                    marker = normalize_key(word)
                    if marker and marker not in {normalize_key(x) for x in words}:
                        words.append(word)
            if len(words) >= 32:
                break
        return words

    def _clean_trigger_word(self, word: str) -> str:
        word = str(word or "").strip().strip("`'\"“”")
        word = re.sub(r"\\([()])", r"\1", word)
        word = re.sub(r"\s+", " ", word).strip()
        word = re.sub(r"(?:触发词|提示词|发饰|服装|二形态服装|outfit|clothes|prompt)\s*$", "", word, flags=re.I).strip()
        return word.strip("`'\"“”，,;；")

    def _collect_trigger_block(self, plain: str, start_pos: int, first_value: str = "") -> str:
        first_value = str(first_value or "").strip().strip("`'\"“”")
        lines = []
        if first_value:
            lines.append(first_value)
        tail = str(plain or "")[start_pos:]
        stop_pattern = re.compile(
            r"^(?:[-—_]{4,}|默认|推荐|网速|百度|夸克|迅雷|链接|提取码|this\s+lora|it\s+is\s+compatible|the\s+default|for\s+outfit|discussion|comments?|base\s*model|hash|download|published)",
            flags=re.I,
        )
        for raw_line in tail.splitlines():
            line = raw_line.strip().strip("`'\"“”")
            if not line:
                if lines:
                    continue
                continue
            if stop_pattern.search(line):
                break
            if re.search(r"(https?://|pan\.|网盘|copyright|license|licensed|compatible|strength|强度|模型|base\.safetensors)", line, flags=re.I):
                break
            if re.search(r"[:：]$", line) and not re.search(r",|，|;|；", line):
                break
            if self._is_valid_trigger_word(line):
                lines.append(line)
            if len(lines) >= 8:
                break
        return ", ".join(lines)

    def _is_valid_trigger_word(self, word: str) -> bool:
        word = self._clean_trigger_word(word)
        if not word or len(word) > 64:
            return False
        bad_fragments = ['":', '{', '}', '[', ']', 'null', 'basemodel', 'trainingstatus', 'availability', 'publishedat', 'files', 'metrics', 'http://', 'https://', '网盘', '链接', '提取码', '基于', '训练', '新角色', '希望', '朋友', '联系', '质量', '效果', '官方模型']
        low = word.lower()
        if any(x in low for x in bad_fragments):
            return False
        if re.search(r"[\u4e00-\u9fff]", word) and not re.fullmatch(r"[\u4e00-\u9fff\s\-·]{1,12}", word):
            return False
        return bool(re.search(r'[\w\u4e00-\u9fff\u3040-\u30ff]', word))

    async def _fetch_page_trigger_words(self, model_id: int, version_id: int = None) -> list:
        url = self._model_page_url(model_id, version_id)
        try:
            text = await self._get_text(url)
            words = self._extract_trigger_words_from_text(text)
            if words:
                logger.info(f"[ComfyUI] 从 Civitai 页面兜底提取触发词：{', '.join(words[:6])}")
            return words
        except Exception as exc:
            logger.warning(f"[ComfyUI] Civitai 页面触发词兜底失败: {exc}")
            return []

    async def _fetch_page_plain_text(self, model_id: int, version_id: int = None) -> str:
        url = self._model_page_url(model_id, version_id)
        try:
            text = await self._get_text(url)
            plain = self._strip_html(text)
            plain = re.sub(r"\n{3,}", "\n\n", plain)
            return plain[:20000]
        except Exception as exc:
            logger.warning(f"[ComfyUI] Civitai 页面正文获取失败: {exc}")
            return ""

    def _build_summary(self, model: dict, version: dict) -> dict:
        model_type = str(model.get("type") or "").strip()
        if model_type and normalize_key(model_type) not in ALLOWED_MODEL_TYPES:
            raise RuntimeError(f"模型类型是 {model_type}，不是 LoRA/LoCon/LyCORIS，已拒绝导入")

        file_info = self._select_lora_file(version)
        trained_words = self._normalize_trained_words(version.get("trainedWords") or [])
        trained_words_source = "api" if trained_words else ""
        if not trained_words:
            description_words = self._extract_trigger_words_from_text(version.get("description") or model.get("description") or "")
            if description_words:
                trained_words = description_words
                trained_words_source = "description"
        tags = [str(x).strip() for x in (model.get("tags") or []) if str(x).strip()]
        download_url = self._normalize_site_host_url(file_info.get("downloadUrl")) or self._download_url_for_version(version.get('id'))
        if self.api_token and "token=" not in download_url:
            sep = "&" if "?" in download_url else "?"
            download_url = f"{download_url}{sep}token={self.api_token}"

        return {
            "model_id": model.get("id"),
            "version_id": version.get("id"),
            "model_name": model.get("name") or "Civitai LoRA",
            "version_name": version.get("name") or "",
            "model_type": model_type or "LORA",
            "base_model": version.get("baseModel") or "",
            "trained_words": trained_words,
            "trained_words_source": trained_words_source,
            "tags": tags,
            "file_name": safe_filename(file_info.get("name") or f"civitai_{version.get('id')}.safetensors"),
            "download_url": download_url,
            "nsfw": bool(model.get("nsfw") or version.get("nsfw")),
            "creator": (model.get("creator") or {}).get("username", ""),
            "stats": model.get("stats") or {},
            "description": re.sub(r"<.*?>", "", str(model.get("description") or "")).strip()[:300],
            "page_text": "",
            "page_url": self._model_page_url(model.get('id'), version.get('id')),
            "source": self.source_key,
        }

    async def download_file(self, download_url: str, dest_path: Path, progress_cb=None) -> Path:
        if self.downloader in {"multithread", "multi", "idm", "builtin"}:
            try:
                return await self.download_file_multithread(download_url, dest_path, progress_cb=progress_cb)
            except RuntimeError as exc:
                if "401" in str(exc):
                    raise
                logger.warning(f"[ComfyUI] 内置多线程下载失败，回退单线程下载：{exc}")
            except Exception as exc:
                logger.warning(f"[ComfyUI] 内置多线程下载异常，回退单线程下载：{self._format_network_error(exc)}")
            return await self.download_file_single(download_url, dest_path, progress_cb=progress_cb)

        if self.downloader in {"aria2", "aria2c"}:
            return await self.download_file_aria2(download_url, dest_path, progress_cb=progress_cb)

        return await self.download_file_single(download_url, dest_path, progress_cb=progress_cb)

    async def download_file_single(self, download_url: str, dest_path: Path, progress_cb=None) -> Path:
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
        part_dir = dest_path.parent / f".{dest_path.name}.parts"
        timeout = aiohttp.ClientTimeout(total=None, connect=min(30, self.timeout), sock_read=120)
        headers = self._headers()
        last_report = 0
        downloaded = 0
        logger.info(f"[ComfyUI] 使用单线程下载：{dest_path.name}")
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                self._check_proxy_circuit()
                async with session.get(download_url, proxy=self.proxy or None) as resp:
                    self._mark_proxy_success()
                    if resp.status != 200:
                        text = await resp.text()
                        if resp.status == 401:
                            raise RuntimeError(
                                "下载失败 401：该模型作者要求登录后下载。"
                                "Civitai API Token 不一定等同网页登录态；请在 WebUI 的 civitai_settings.cookie "
                                "填入浏览器请求里的 Cookie，或确认 API Token 对该资源有下载权限。"
                            )
                        raise RuntimeError(f"下载失败 {resp.status}: {text[:160]}")
                    total = int(resp.headers.get("Content-Length") or 0)
                    with open(tmp_path, "wb") as fp:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            if not chunk:
                                continue
                            fp.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if progress_cb and now - last_report >= 8:
                                last_report = now
                                await progress_cb(downloaded, total)
        except RuntimeError:
            raise
        except Exception as exc:
            if self._is_proxy_network_error(exc):
                self._mark_proxy_failure(exc)
            raise RuntimeError(self._format_network_error(exc)) from exc
        tmp_path.replace(dest_path)
        try:
            shutil.rmtree(part_dir, ignore_errors=True)
        except Exception:
            pass
        return dest_path

    async def _probe_download(self, session: aiohttp.ClientSession, download_url: str):
        async def range_probe(original_error: Exception = None):
            req_headers = {"Range": "bytes=0-0"}
            self._check_proxy_circuit()
            async with session.get(download_url, headers=req_headers, proxy=self.proxy or None, allow_redirects=True) as resp:
                self._mark_proxy_success()
                if resp.status == 401:
                    raise RuntimeError(
                        "下载失败 401：该模型作者要求登录后下载。请设置 civitai_settings.cookie，"
                        "或确认 API Token 对该资源有下载权限。"
                    )
                if resp.status == 206:
                    content_range = str(resp.headers.get("Content-Range") or "")
                    match = re.search(r"/(\d+)\s*$", content_range)
                    total = int(match.group(1)) if match else 0
                    if total:
                        return total, True
                if resp.status >= 400:
                    text = await resp.text()
                    if original_error:
                        raise original_error
                    raise RuntimeError(f"下载 Range 探测失败 {resp.status}: {text[:160]}")
                length = int(resp.headers.get("Content-Length") or 0)
                return length, False

        self._check_proxy_circuit()
        async with session.head(download_url, proxy=self.proxy or None, allow_redirects=True) as resp:
            self._mark_proxy_success()
            if resp.status == 401:
                raise RuntimeError(
                    "下载失败 401：该模型作者要求登录后下载。请设置 civitai_settings.cookie，"
                    "或确认 API Token 对该资源有下载权限。"
                )
            if resp.status >= 400:
                text = await resp.text()
                return await range_probe(RuntimeError(f"下载探测失败 {resp.status}: {text[:160]}"))
            length = int(resp.headers.get("Content-Length") or 0)
            accept_ranges = "bytes" in str(resp.headers.get("Accept-Ranges") or "").lower()
            if not accept_ranges:
                probed_length, probed_ranges = await range_probe()
                if probed_ranges:
                    return probed_length, True
            return length, accept_ranges

    async def download_file_multithread(self, download_url: str, dest_path: Path, progress_cb=None) -> Path:
        """内置 Range 分片并发下载，类似 IDM 的多线程下载。"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
        timeout = aiohttp.ClientTimeout(total=None, connect=min(30, self.timeout), sock_read=120)
        headers = self._headers()

        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            total, accept_ranges = await self._probe_download(session, download_url)
            if not total or not accept_ranges or self.split <= 1:
                raise RuntimeError("服务器不支持 Range 分片或无法获取文件大小")

            if tmp_path.exists() and tmp_path.stat().st_size != total:
                tmp_path.unlink()
            if not tmp_path.exists():
                with open(tmp_path, "wb") as fp:
                    fp.truncate(total)

            part_dir = dest_path.parent / f".{dest_path.name}.parts"
            part_dir.mkdir(parents=True, exist_ok=True)
            chunk_size = (total + self.split - 1) // self.split
            ranges = []
            for idx in range(self.split):
                start = idx * chunk_size
                if start >= total:
                    break
                end = min(total - 1, start + chunk_size - 1)
                ranges.append((idx, start, end))

            downloaded_parts = {}
            resume_offsets = {}
            for idx, start, end in ranges:
                marker = part_dir / f"{idx}.done"
                if marker.exists():
                    try:
                        done = int(marker.read_text(encoding="utf-8") or "0")
                    except Exception:
                        done = 0
                    if done == end - start + 1:
                        downloaded_parts[idx] = done
                        continue
                progress_marker = part_dir / f"{idx}.progress"
                if progress_marker.exists():
                    try:
                        done = int(progress_marker.read_text(encoding="utf-8") or "0")
                    except Exception:
                        done = 0
                    expected = end - start + 1
                    if 0 < done < expected:
                        resume_offsets[idx] = done

            lock = asyncio.Lock()
            downloaded = sum(downloaded_parts.values()) + sum(resume_offsets.values())
            last_report = 0

            async def fetch_range(idx: int, start: int, end: int):
                nonlocal downloaded, last_report
                marker = part_dir / f"{idx}.done"
                if idx in downloaded_parts:
                    return
                progress_marker = part_dir / f"{idx}.progress"
                expected = end - start + 1
                completed = min(max(int(resume_offsets.get(idx) or 0), 0), expected)
                last_exc = None
                max_attempts = 3
                for attempt in range(max_attempts):
                    written = 0
                    attempt_base = completed
                    try:
                        if completed >= expected:
                            marker.write_text(str(expected), encoding="utf-8")
                            try:
                                progress_marker.unlink()
                            except Exception:
                                pass
                            return
                        req_headers = dict(headers)
                        req_headers["Range"] = f"bytes={start + completed}-{end}"
                        last_marker_write = 0
                        self._check_proxy_circuit()
                        async with session.get(download_url, headers=req_headers, proxy=self.proxy or None) as resp:
                            self._mark_proxy_success()
                            if resp.status == 401:
                                raise RuntimeError("下载失败 401：请设置 civitai_settings.cookie 登录态。")
                            if resp.status != 206:
                                text = await resp.text()
                                raise RuntimeError(f"分片 {idx} 下载失败 {resp.status}: {text[:160]}")
                            with open(tmp_path, "r+b") as fp:
                                async for chunk in resp.content.iter_chunked(1024 * 512):
                                    if not chunk:
                                        continue
                                    fp.seek(start + completed + written)
                                    fp.write(chunk)
                                    written += len(chunk)
                                    async with lock:
                                        downloaded += len(chunk)
                                        now = time.time()
                                        if progress_cb and now - last_report >= 8:
                                            last_report = now
                                            await progress_cb(downloaded, total)
                                    if written - last_marker_write >= 8 * 1024 * 1024:
                                        progress_marker.write_text(str(completed + written), encoding="utf-8")
                                        last_marker_write = written
                        completed += written
                        progress_marker.write_text(str(completed), encoding="utf-8")
                        if completed != expected:
                            raise RuntimeError(f"分片 {idx} 大小异常：{completed}/{expected}")
                        marker.write_text(str(expected), encoding="utf-8")
                        try:
                            progress_marker.unlink()
                        except Exception:
                            pass
                        return
                    except Exception as exc:
                        if self._is_proxy_network_error(exc):
                            self._mark_proxy_failure(exc)
                            raise
                        last_exc = exc
                        completed = min(max(completed, attempt_base + written), expected)
                        if completed:
                            try:
                                progress_marker.write_text(str(completed), encoding="utf-8")
                            except Exception:
                                pass
                        if "401" in str(exc):
                            raise
                        if attempt + 1 >= max_attempts:
                            break
                        logger.warning(
                            f"[ComfyUI] 分片 {idx} 下载失败，准备重试 {attempt + 2}/{max_attempts}: {self._format_network_error(exc)}"
                        )
                        await asyncio.sleep(1.2 * (attempt + 1))
                raise RuntimeError(self._format_network_error(last_exc or RuntimeError(f"分片 {idx} 下载失败")))

            logger.info(f"[ComfyUI] 使用内置多线程下载：{dest_path.name} split={len(ranges)} size={total} direct_range_write=1")
            await asyncio.gather(*(fetch_range(idx, start, end) for idx, start, end in ranges))

        if tmp_path.stat().st_size != total:
            raise RuntimeError(f"多线程下载文件大小异常：{tmp_path.stat().st_size}/{total}")
        for idx, start, end in ranges:
            marker = part_dir / f"{idx}.done"
            expected = end - start + 1
            try:
                done = int(marker.read_text(encoding="utf-8") or "0")
            except Exception:
                done = 0
            if done != expected:
                raise RuntimeError(f"分片缺失：{idx}.done")
        tmp_path.replace(dest_path)
        try:
            shutil.rmtree(part_dir, ignore_errors=True)
        except Exception:
            pass
        return dest_path

    async def download_file_aria2(self, download_url: str, dest_path: Path, progress_cb=None) -> Path:
        """使用 aria2c 多连接下载。需要系统已安装 aria2c。"""
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        aria2c = self.aria2c_path
        if not Path(aria2c).exists() and not shutil.which(aria2c):
            raise RuntimeError("未找到 aria2c。请安装 aria2，或将 civitai_settings.downloader 改回 aiohttp。")

        args = [
            aria2c,
            "--continue=true",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            f"--max-connection-per-server={self.split}",
            f"--split={self.split}",
            "--min-split-size=1M",
            "--file-allocation=none",
            "--summary-interval=5",
            "--console-log-level=warn",
            "--download-result=hide",
            "--check-certificate=false",
            "--dir", str(dest_path.parent),
            "--out", dest_path.name,
            "--header", f"User-Agent: {DEFAULT_HEADERS['User-Agent']}",
        ]
        if self.api_token:
            args.extend(["--header", f"Authorization: Bearer {self.api_token}"])
        if self.cookie:
            args.extend(["--header", f"Cookie: {self.cookie}"])
            args.extend(["--header", f"Referer: {self.site_base}/"])
        if self.proxy:
            args.extend(["--all-proxy", self.proxy])
        args.append(download_url)

        logger.info(f"[ComfyUI] 使用 aria2c 多连接下载：{dest_path.name} split={self.split}")
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_lines = []
        last_report = 0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").strip()
            if text:
                output_lines.append(text)
                output_lines = output_lines[-20:]
            now = time.time()
            if progress_cb and now - last_report >= 8 and dest_path.exists():
                last_report = now
                await progress_cb(dest_path.stat().st_size, 0)
        code = await proc.wait()
        if code != 0 or not dest_path.exists() or dest_path.stat().st_size <= 0:
            detail = "\n".join(output_lines[-8:])
            raise RuntimeError(f"aria2c 下载失败，退出码 {code}：{detail}")
        return dest_path


class CivitaiRedClient(CivitaiClient):
    def __init__(self, api_token: str = "", proxy: str = "", timeout: int = 60, cookie: str = "", downloader: str = "multithread", aria2c_path: str = "aria2c", split: int = 8, **kwargs):
        super().__init__(
            api_token=api_token,
            proxy=proxy,
            timeout=timeout,
            cookie=cookie,
            downloader=downloader,
            aria2c_path=aria2c_path,
            split=split,
            site_base="https://civitai.red",
            source_key="civitai_red",
            use_web_search_fallback=False,
            **kwargs,
        )


class HuggingFaceClient(CivitaiClient):
    def __init__(self, api_token: str = "", proxy: str = "", timeout: int = 60, downloader: str = "multithread", aria2c_path: str = "aria2c", split: int = 8, **kwargs):
        super().__init__(
            api_token=api_token,
            proxy=proxy,
            timeout=timeout,
            cookie="",
            downloader=downloader,
            aria2c_path=aria2c_path,
            split=split,
            source_key="huggingface",
            **kwargs,
        )

    def _headers(self):
        headers = dict(DEFAULT_HEADERS)
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _format_network_error(self, exc: Exception) -> RuntimeError:
        blocked = self._proxy_circuit_block_message()
        if blocked:
            return RuntimeError(blocked)
        if self.proxy and isinstance(exc, (ClientProxyConnectionError, ClientConnectorError, TimeoutError, OSError)):
            return RuntimeError(
                f"无法连接 Hugging Face 代理：{self.proxy}。请检查代理/防火墙/Docker 网络。原始错误：{exc}"
            )
        if isinstance(exc, TimeoutError):
            return RuntimeError(f"连接 Hugging Face 超时，请检查网络或代理。原始错误：{exc}")
        return RuntimeError(str(exc))

    async def _get_json(self, url: str):
        if self.request_adapter:
            return await self._get_json_via_adapter(url, target="Hugging Face API", max_retries=self.network_max_retries)
        timeout = aiohttp.ClientTimeout(total=self.timeout, connect=min(30, self.timeout))
        retry_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
        max_retries = self.network_max_retries
        try:
            last_error = None
            async with aiohttp.ClientSession(headers=self._headers(), timeout=timeout) as session:
                for attempt in range(max_retries):
                    try:
                        self._check_proxy_circuit()
                        async with session.get(url, proxy=self.proxy or None) as resp:
                            self._mark_proxy_success()
                            text = await resp.text()
                            if resp.status == 200:
                                try:
                                    return json.loads(text)
                                except json.JSONDecodeError as exc:
                                    last_error = self._format_json_decode_error("Hugging Face API", exc, text)
                                    if attempt + 1 < max_retries:
                                        delay = 1.2 * (attempt + 1)
                                        logger.info(f"[ComfyUI] Hugging Face API JSON 解析失败，{delay:.1f}s 后重试 {attempt + 2}/{max_retries}: {last_error}")
                                        await asyncio.sleep(max(0.5, min(delay, 8.0)))
                                        continue
                                    raise RuntimeError(last_error) from exc
                            last_error = self._format_http_error("Hugging Face API", resp.status, text)
                            if resp.status in retry_statuses and attempt + 1 < max_retries:
                                retry_after = resp.headers.get("Retry-After")
                                try:
                                    delay = float(retry_after) if retry_after else 1.2 * (attempt + 1)
                                except Exception:
                                    delay = 1.2 * (attempt + 1)
                                logger.info(f"[ComfyUI] Hugging Face API HTTP {resp.status}，等待 {delay:.1f}s 后重试 {attempt + 2}/3")
                                await asyncio.sleep(max(0.5, min(delay, 8.0)))
                                continue
                            raise RuntimeError(last_error)
                    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                        last_error = self._format_network_error(exc)
                        if self._is_proxy_network_error(exc):
                            self._mark_proxy_failure(exc)
                            raise RuntimeError(last_error)
                        if attempt + 1 < max_retries:
                            delay = 1.2 * (attempt + 1)
                            logger.info(f"[ComfyUI] Hugging Face API 网络抖动，等待 {delay:.1f}s 后重试 {attempt + 2}/3: {exc}")
                            await asyncio.sleep(delay)
                            continue
                        raise RuntimeError(last_error)
            raise RuntimeError(last_error or "Hugging Face API 请求失败")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(self._format_network_error(exc)) from exc

    async def _get_text(self, url: str) -> str:
        if self.request_adapter:
            return await self._get_text_via_adapter(url, target="Hugging Face page", max_retries=self.network_max_retries)
        timeout = aiohttp.ClientTimeout(total=self.timeout, connect=min(30, self.timeout))
        try:
            async with aiohttp.ClientSession(headers=self._headers(), timeout=timeout) as session:
                self._check_proxy_circuit()
                async with session.get(url, proxy=self.proxy or None) as resp:
                    self._mark_proxy_success()
                    text = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(self._format_http_error("Hugging Face 页面", resp.status, text))
                    return text
        except RuntimeError:
            raise
        except Exception as exc:
            if self._is_proxy_network_error(exc):
                self._mark_proxy_failure(exc)
            raise RuntimeError(self._format_network_error(exc)) from exc

    async def _probe_download(self, session: aiohttp.ClientSession, download_url: str):
        self._check_proxy_circuit()
        async with session.head(download_url, proxy=self.proxy or None, allow_redirects=True) as resp:
            self._mark_proxy_success()
            if resp.status == 401:
                raise RuntimeError("下载失败 401：该 Hugging Face 仓库需要权限，请设置 huggingface_api_token。")
            if resp.status == 403:
                raise RuntimeError("下载失败 403：该 Hugging Face 仓库可能是 gated/private，请确认账号已获授权并设置 huggingface_api_token。")
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"下载探测失败 {resp.status}: {text[:160]}")
            length = int(resp.headers.get("Content-Length") or 0)
            accept_ranges = "bytes" in str(resp.headers.get("Accept-Ranges") or "").lower()
            return length, accept_ranges

    def parse_reference(self, ref: str) -> dict:
        ref = str(ref or "").strip().strip("<>")
        if not ref:
            raise ValueError("缺少 Hugging Face 模型链接或 repo id")
        file_name = ""
        repo_id = ref
        parsed = urlparse(ref)
        if parsed.netloc and "huggingface.co" in parsed.netloc.lower():
            parts = [p for p in (parsed.path or "").split("/") if p]
            if len(parts) < 2:
                raise ValueError("无法识别 Hugging Face 链接，请使用模型页或文件链接")
            repo_id = "/".join(parts[:2])
            marker_parts = {"blob", "resolve", "raw"}
            if len(parts) >= 5 and parts[2] in marker_parts:
                file_name = "/".join(parts[4:])
            query = parse_qs(parsed.query or "")
            file_values = query.get("file") or query.get("filename")
            if file_values:
                file_name = str(file_values[0] or "").strip()
        elif "/" not in ref:
            raise ValueError("Hugging Face repo id 应为 owner/repo")
        return {"repo_id": repo_id.strip("/"), "file_name": file_name}

    async def get_model(self, repo_id: str) -> dict:
        return await self._get_json(f"{HUGGINGFACE_API_BASE}/models/{quote(str(repo_id).strip(), safe='/')}?blobs=false")

    def _is_lora_repo(self, item: dict) -> bool:
        tags = [str(x).lower() for x in (item.get("tags") or [])]
        haystack = " ".join([str(item.get("id") or item.get("modelId") or ""), str(item.get("pipeline_tag") or ""), *tags]).lower()
        if any(x in haystack for x in ("controlnet", "vae", "checkpoint", "textual inversion", "embedding")):
            return False
        if "lora" in haystack or "adapter" in haystack:
            return True
        return any(self._select_lora_file({"siblings": item.get("siblings") or []}, from_hf=True, required=False))

    def _select_lora_file(self, version: dict, from_hf: bool = False, required: bool = True):
        files = version.get("siblings") if from_hf else version.get("files")
        files = files or []
        candidates = []
        for f in files:
            name = str(f.get("rfilename") if from_hf else f.get("name") or "")
            low = name.lower()
            ext_ok = low.endswith((".safetensors", ".ckpt", ".pt"))
            if not ext_ok:
                continue
            if any(x in low for x in ("vae", "textual", "embedding", "controlnet", "control_net")):
                continue
            if not from_hf:
                ftype = normalize_key(f.get("type") or "")
                if ftype in {"vae", "config"}:
                    continue
            candidates.append(f)
        if not candidates:
            if required:
                raise RuntimeError("该版本没有可下载的 LoRA 权重文件")
            return []
        safes = [
            f for f in candidates
            if str(f.get("rfilename") if from_hf else f.get("name") or "").lower().endswith(".safetensors")
        ]
        return safes[0] if safes else candidates[0]

    def _infer_base_model(self, model: dict, readme: str = "", base_models=None) -> str:
        base_models = [str(x).strip() for x in (base_models or []) if str(x).strip()]
        tags = [str(x).strip() for x in (model.get("tags") or []) if str(x).strip()]
        values = []
        for tag in tags:
            if tag.lower().startswith("base_model:"):
                values.append(tag.split(":", 1)[1])
        haystack = " ".join([str(model.get("id") or model.get("modelId") or ""), *tags, str(readme or "")])
        hay_low = haystack.lower()
        for allowed in base_models:
            if allowed.lower() in hay_low:
                return allowed
        for value in values:
            for allowed in base_models:
                if allowed.lower() in value.lower():
                    return value
        return values[0] if values else ""

    def _hf_file_url(self, repo_id: str, filename: str) -> str:
        return f"https://huggingface.co/{repo_id}/resolve/main/{quote(str(filename), safe='/')}?download=true"

    def _hf_page_url(self, repo_id: str, filename: str = "") -> str:
        if filename:
            return f"https://huggingface.co/{repo_id}/blob/main/{quote(str(filename), safe='/')}"
        return f"https://huggingface.co/{repo_id}"

    async def _fetch_readme(self, repo_id: str) -> str:
        try:
            text = await self._get_text(f"https://huggingface.co/{repo_id}/raw/main/README.md")
            return text[:20000]
        except Exception:
            return ""

    async def search_loras(self, query: str = "lora", limit: int = 10, base_models=None) -> list:
        query = str(query or "lora").strip() or "lora"
        limit = max(1, min(int(limit or 10), 30))
        params = urlencode({"search": query, "limit": max(limit * 3, 12), "full": "true"})
        data = await self._get_json(f"{HUGGINGFACE_API_BASE}/models?{params}")
        items = data if isinstance(data, list) else []
        results = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            repo_id = str(item.get("id") or item.get("modelId") or "").strip()
            if not repo_id or repo_id in seen:
                continue
            if bool(item.get("private")):
                continue
            if not self._is_lora_repo(item):
                continue
            file_info = self._select_lora_file({"siblings": item.get("siblings") or []}, from_hf=True, required=False)
            if not file_info:
                continue
            file_name = str(file_info.get("rfilename") or "").strip()
            base_model = self._infer_base_model(item, base_models=base_models)
            if base_models and base_model and not any(str(x).lower() in base_model.lower() for x in base_models):
                continue
            if base_models and not base_model:
                text = " ".join([repo_id, file_name, " ".join(str(x) for x in (item.get("tags") or []))]).lower()
                matched_base = ""
                for bm in base_models:
                    if str(bm).lower() in text:
                        matched_base = str(bm)
                        break
                if not matched_base:
                    base_model = "未知"
                else:
                    base_model = matched_base
            seen.add(repo_id)
            tags = [str(x).strip() for x in (item.get("tags") or []) if str(x).strip()]
            results.append({
                "source": "huggingface",
                "model_id": repo_id,
                "version_id": str(item.get("sha") or "")[:12],
                "version_name": str(item.get("lastModified") or "")[:10],
                "name": repo_id,
                "type": "LORA",
                "base_model": base_model or "未知",
                "trained_words": [],
                "tags": tags,
                "file_name": safe_filename(Path(file_name).name),
                "download_count": _safe_int(item.get("downloads")),
                "heart_count": _safe_int(item.get("likes")),
                "preview_url": "",
                "preview_hidden_count": 0,
                "nsfw": any("nsfw" in x.lower() for x in tags),
                "page_url": self._hf_page_url(repo_id, file_name),
                "download_url": self._hf_file_url(repo_id, file_name),
            })
            if len(results) >= limit:
                break
        return results

    async def resolve_model(self, ref: str, base_models=None) -> dict:
        parsed = self.parse_reference(ref)
        repo_id = parsed["repo_id"]
        requested_file = parsed.get("file_name") or ""
        model = await self.get_model(repo_id)
        readme = await self._fetch_readme(repo_id)
        file_info = None
        if requested_file:
            for item in model.get("siblings") or []:
                if str(item.get("rfilename") or "") == requested_file:
                    file_info = item
                    break
            if not file_info:
                raise RuntimeError(f"Hugging Face repo 里找不到文件：{requested_file}")
        if not file_info:
            file_info = self._select_lora_file({"siblings": model.get("siblings") or []}, from_hf=True)
        file_name = str(file_info.get("rfilename") or "").strip()
        tags = [str(x).strip() for x in (model.get("tags") or []) if str(x).strip()]
        trained_words = self._extract_trigger_words_from_text(readme)
        base_model = self._infer_base_model(model, readme=readme, base_models=base_models)
        return {
            "source": "huggingface",
            "model_id": repo_id,
            "version_id": str(model.get("sha") or "")[:12],
            "model_name": repo_id,
            "version_name": Path(file_name).stem,
            "model_type": "LORA",
            "base_model": base_model or "",
            "trained_words": trained_words,
            "tags": tags,
            "file_name": safe_filename(Path(file_name).name),
            "download_url": self._hf_file_url(repo_id, file_name),
            "nsfw": any("nsfw" in x.lower() for x in tags),
            "creator": str(model.get("author") or repo_id.split("/", 1)[0]),
            "stats": {"downloadCount": _safe_int(model.get("downloads")), "thumbsUpCount": _safe_int(model.get("likes"))},
            "description": self._strip_html(readme)[:300],
            "page_text": self._strip_html(readme)[:20000],
            "page_url": self._hf_page_url(repo_id, file_name),
        }


class YodayoClient:
    def __init__(self, cookie: str = "", proxy: str = "", timeout: int = 60):
        self.cookie = str(cookie or "").strip()
        self.proxy = str(proxy or "").strip()
        self.timeout = max(10, int(timeout or 60))

    async def search_loras(self, query: str = "lora", limit: int = 10, base_models=None) -> list:
        raise RuntimeError(
            "Yodayo 目前没有公开稳定的 LoRA 下载 API，网页也有 Cloudflare 防护；"
            "已预留 yodayo_cookie 配置，等你提供可用接口或登录态后再启用下载导入。"
        )


class LoraProfileStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[ComfyUI] 读取 LoRA profiles 失败: {e}")
        return {}

    def save(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data or {}, ensure_ascii=False, indent=2)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _expand_aliases(self, aliases: list) -> list:
        result = []
        seen = set()
        pending = [str(x or "").strip() for x in (aliases or [])]
        for item in list(pending):
            n = normalize_key(item)
            for key, extra_aliases in COMMON_CHARACTER_ALIASES.items():
                if key in n:
                    pending.extend(extra_aliases)
        for item in pending:
            item = str(item or "").strip()
            if not item:
                continue
            marker = normalize_key(item)
            if marker and marker not in seen:
                seen.add(marker)
                result.append(item)
        return result

    def _title_character_candidate(self, title: str) -> str:
        candidates = self._title_character_candidates(title)
        return candidates[0] if candidates else ""

    def _title_source_markers(self) -> set:
        return lora_source_alias_terms(extra_paths=[self.path.with_name("lora_source_aliases.json")])

    def _looks_like_title_source_fragment(self, value: str) -> bool:
        text = normalize_key(value)
        compact = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", text)
        for marker in self._title_source_markers():
            marker_norm = normalize_key(marker)
            marker_compact = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", marker_norm)
            if marker_norm and marker_norm in text:
                return True
            if marker_compact and marker_compact == compact:
                return True
        return False

    def _title_part_character_candidates(self, value: str, original_title: str = "") -> list:
        raw = self._strip_title_descriptors(value)
        if not raw:
            return []
        pieces = [x.strip(" -_/\\／⁄:：") for x in re.split(r"_+", raw) if x.strip(" -_/\\／⁄:：")]
        candidates = []
        if len(pieces) >= 2:
            prefix = " ".join(pieces[:-1])
            tail = pieces[-1]
            if self._looks_like_title_source_fragment(prefix):
                candidates.append(tail)
            else:
                candidates.extend([raw, tail])
        else:
            candidates.append(raw)
        result = []
        for item in candidates:
            item = re.sub(r"\s+", " ", str(item or "")).strip()
            if item and not self._is_bad_alias_candidate(item, original_title=original_title):
                result.append(item[:80])
        return result

    def _alias_looks_source_prefixed(self, value: str) -> bool:
        raw = re.sub(r"\s+", " ", str(value or "")).strip(" -_|｜丨／/⁄:：")
        pieces = [x.strip(" -_/\\／⁄:：") for x in re.split(r"_+", raw) if x.strip(" -_/\\／⁄:：")]
        return len(pieces) >= 2 and self._looks_like_title_source_fragment(" ".join(pieces[:-1]))

    def _strip_title_descriptors(self, value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return ""

        def strip_bracket(match):
            inner = str(match.group(1) or "").strip()
            lowered = inner.lower()
            if (
                self._looks_like_title_source_fragment(inner)
                or re.search(
                    r"\b(?:\d+\s*)?(?:outfits?|costumes?|clothes|clothing|style|styles|"
                    r"version|ver|v\d+(?:\.\d+)*|illu|illustrious|sdxl|xl|pony|noob|anima|lora|locon|lycoris)\b",
                    lowered,
                )
            ):
                return " "
            return match.group(0)

        text = re.sub(r"[\[\(（【]([^\]\)）】]{1,80})[\]\)）】]", strip_bracket, text)
        text = re.sub(r"\b(?:illustrious|noobai?|pony|sdxl|xl|lora|locon|lycoris)\b", " ", text, flags=re.I)
        text = re.sub(r"\b(?:illu|illust)\s*v?\d*(?:\.\d+)?\b", " ", text, flags=re.I)
        return re.sub(r"\s+", " ", text).strip(" -_|｜丨／/⁄:：")

    def _title_character_candidates(self, title: str) -> list:
        text = str(title or "").strip()
        if not text:
            return []
        text = re.sub(r"^[\s【\[][^\]】]{0,80}[\]】]\s*", "", text)
        text = self._strip_title_descriptors(text)
        text = re.sub(r"\s+", " ", text).strip(" -_|｜丨／/⁄:：")
        candidates = []
        for part_index, part in enumerate(re.split(r"\s*(?:[|｜丨／/⁄]|\s+-\s+|\s+—\s+|\s+–\s+)\s*", text)):
            part = part.strip(" -_|｜丨／/⁄:：")
            if not part:
                continue
            if re.fullmatch(r"(?:[0-9A-Za-z]+)?[―—].+[―—]", part.strip()):
                continue
            if candidates and part_index > 0:
                if self._alias_looks_source_prefixed(part):
                    pass
                elif self._looks_like_title_source_fragment(part):
                    continue
                elif re.search(r"[\u3400-\u9fff\u3040-\u30ff]", part):
                    pass
                else:
                    continue
            subparts = [x.strip(" -_/\\／⁄:：") for x in re.split(r"\s*[／/⁄]\s*", part) if x.strip()]
            for sub in subparts or [part]:
                candidates.extend(self._title_part_character_candidates(sub, original_title=title))
        return self._dedupe_values(candidates)

    def _is_bad_alias_candidate(self, item: str, original_title: str = "") -> bool:
        item = str(item or "").strip()
        if not item or is_generic_alias_word(item):
            return True
        lowered = item.lower()
        if re.fullmatch(r"civitai:\d+@\d+", lowered) or "what did you think" in lowered or "?" in item:
            return True
        if re.search(r"\[[^\]]*(?:outfits?|costumes?|clothes|version|v\d|illu|sdxl|lora)[^\]]*\]", lowered):
            return True
        if re.search(
            r"\b(?:hair|eyes?|bangs|braid|ahoge|ribbon|camisole|sleeves?|dress|skirt|outfit|clothes|"
            r"masterpiece|best quality|high quality|prompt|resource|review|download|creator)\b",
            lowered,
        ):
            return True
        compact = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", lowered)
        if re.search(r"(?:^|[\s_\-])(?:illu|illuxl|ixl|sdxl|xl|pony|noob|lora|locon|lycoris)(?:$|[\s_\-])", lowered):
            return True
        if re.search(r"(?:illu|illuxl|ixl|sdxl|pony|noob|lora)$", compact):
            return True
        if any(word in lowered for word in ("illustrious", "noob", "pony", "sdxl", "lora", "locon", "lycoris")):
            return True
        if item == str(original_title or "").strip() and re.search(r"[|｜丨／/⁄]|\s+-\s+|\s+—\s+|\s+–\s+", item):
            return True
        if len(item) > 80:
            return True
        return False

    def _dedupe_values(self, values: list) -> list:
        result = []
        seen = set()
        for item in values or []:
            item = str(item or "").strip()
            if not item:
                continue
            marker = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", item.lower())
            if not marker or marker in seen:
                continue
            seen.add(marker)
            result.append(item)
        return result

    def _identity_candidates(self, summary: dict, alias: str = "") -> list:
        model_name = str(summary.get("model_name") or "").strip()
        version_name = str(summary.get("version_name") or "").strip()
        file_name = str(summary.get("file_name") or "").strip()
        trained_words = summary.get("trained_words") or []
        raw_candidates = [
            ("llm_character", summary.get("character_name"), 100),
            ("model_title", self._title_character_candidate(model_name), 85),
            ("version_title", self._title_character_candidate(version_name), 70),
            (
                "trigger_word",
                next((str(x).strip() for x in trained_words if str(x).strip() and not is_generic_alias_word(str(x))), ""),
                62,
            ),
            ("file_name", Path(file_name).stem, 45),
            ("user_alias", alias, 35),
        ]
        result = []
        seen = set()
        for source, value, score in raw_candidates:
            value = str(value or "").strip()
            if self._is_bad_alias_candidate(value, original_title=model_name):
                continue
            marker = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", value.lower())
            if not marker or marker in seen:
                continue
            seen.add(marker)
            result.append({"source": source, "value": value, "score": score})
        return result

    def upsert_from_civitai(self, summary: dict, alias: str = "", strength_model: float = 0.8, strength_clip: float = 0.8) -> dict:
        data = self.load()
        def collect_tags(*groups, limit: int = 40) -> list:
            merged = []
            seen = set()
            for group in groups:
                values = group
                if isinstance(values, str):
                    values = re.split(r"[,，;；\n\r]+", values)
                for value in values or []:
                    item = str(value or "").strip()
                    marker = normalize_key(item)
                    if item and marker and marker not in seen:
                        seen.add(marker)
                        merged.append(item)
                        if len(merged) >= limit:
                            return merged
            return merged

        raw_trained_words = summary.get("trained_words") or []
        activation_tags = collect_tags(summary.get("activation_tags") or [], limit=40)
        if not activation_tags:
            activation_tags = collect_tags(raw_trained_words, limit=40)
        identity_appearance = collect_tags(
            summary.get("identity_appearance") or [],
            summary.get("appearance_words") or [],
            limit=40,
        )
        default_outfit = collect_tags(
            summary.get("default_outfit") or [],
            summary.get("default_outfit_words") or [],
            limit=40,
        )
        trained_words = activation_tags
        model_name = str(summary.get("model_name") or "").strip()
        file_name = str(summary.get("file_name") or "").strip()
        version_name = str(summary.get("version_name") or "").strip()
        search_query = str(summary.get("search_query") or "").strip()
        identity_candidates = self._identity_candidates(summary, alias=alias)
        character_name = identity_candidates[0]["value"] if identity_candidates else ""
        clean_model_alias = self._title_character_candidate(model_name)
        model_title_aliases = self._title_character_candidates(model_name)
        version_title_aliases = self._title_character_candidates(version_name)
        trigger_key = next((str(x).strip() for x in trained_words if str(x).strip() and not is_generic_alias_word(str(x))), "")
        key_source = character_name or clean_model_alias or trigger_key or Path(file_name).stem
        key = normalize_key(key_source)
        key = key or normalize_key(Path(file_name).stem)
        aliases = []
        for item in [
            character_name,
            clean_model_alias,
            self._title_character_candidate(version_name),
            Path(file_name).stem,
        ] + model_title_aliases + version_title_aliases + (summary.get("aliases") or []) + [alias]:
            item = str(item or "").strip()
            if (
                item
                and not self._is_bad_alias_candidate(item, original_title=model_name)
                and (
                    normalize_key(item) != normalize_key(search_query)
                    or normalize_key(item) != normalize_key(character_name)
                )
                and item.lower() not in {x.lower() for x in aliases}
            ):
                aliases.append(item)
        for item in trained_words[:3]:
            item = str(item or "").strip()
            if (
                item
                and not is_generic_alias_word(item)
                and not self._is_bad_alias_candidate(item, original_title=model_name)
                and item.lower() not in {x.lower() for x in aliases}
            ):
                aliases.append(item)
        aliases = self._dedupe_values(self._expand_aliases(aliases))
        official_aliases = self._dedupe_values([
            item for item in [character_name, clean_model_alias] + model_title_aliases + version_title_aliases + (summary.get("aliases") or [])
            if str(item or "").strip() and not self._is_bad_alias_candidate(str(item), original_title=model_name)
        ])
        match_terms = self._dedupe_values([
            item for item in [character_name, clean_model_alias, Path(file_name).stem] + model_title_aliases + version_title_aliases + aliases
            if str(item or "").strip() and not self._is_bad_alias_candidate(str(item), original_title=model_name)
        ])
        for old_key, old_profile in list(data.items()):
            if old_key != key and isinstance(old_profile, dict) and str(old_profile.get("lora") or "") == file_name:
                data.pop(old_key, None)
        profile = {
            "profile_version": LORA_PROFILE_SCHEMA_VERSION,
            "lora": file_name,
            "aliases": aliases,
            "character": character_name or clean_model_alias or Path(file_name).stem,
            "search_query": search_query,
            "search_terms": [str(x).strip() for x in (summary.get("search_terms") or []) if str(x).strip()][:8],
            "search_aliases": [str(x).strip() for x in (summary.get("search_aliases") or []) if str(x).strip()][:32],
            "source_title": model_name,
            "source_version_title": version_name,
            "identity_evidence": identity_candidates[:6],
            "official_aliases": official_aliases,
            "nickname_aliases": [],
            "match_terms": match_terms,
            "activation_tags": activation_tags,
            "identity_appearance": identity_appearance,
            "default_outfit": default_outfit,
            "trigger_prompt": ", ".join(trained_words),
            "appearance_prompt": ", ".join(identity_appearance),
            "default_outfit_prompt": ", ".join(default_outfit),
            "recommended_prompt": str(summary.get("recommended_prompt") or ""),
            "negative_prompt": "",
            "source": summary.get("source") or "civitai",
            "model_id": summary.get("model_id"),
            "version_id": summary.get("version_id"),
            "base_model": summary.get("base_model") or "",
            "page_url": summary.get("page_url") or "",
            "strength_model": strength_model,
            "strength_clip": strength_clip,
            "updated_at": int(time.time()),
        }
        data[key] = profile
        self.save(data)
        return profile

    def match(self, text: str):
        text_norm = normalize_key(text)
        if not text_norm:
            return None, None
        data = self.load()
        best_key = None
        best_profile = None
        best_len = 0
        for key, profile in data.items():
            if not isinstance(profile, dict):
                continue
            names = [key, profile.get("character", ""), Path(str(profile.get("lora", ""))).stem]
            names.extend(profile.get("aliases", []) or [])
            names = self._expand_aliases(names)
            for name in names:
                n = normalize_key(name)
                if not n:
                    continue
                if n in text_norm and len(n) > best_len:
                    best_key, best_profile, best_len = key, profile, len(n)
        return best_key, best_profile
