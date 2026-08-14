"""Civitai/HuggingFace LoRA 搜索编排服务（AstrBot-free 移植）。

移植自原插件的 Civitai 搜索编排算法，形成固定查询流水线：
清洗 -> 本地种子（含回调档案种子） -> 来源解析 -> 拼写变体 -> 有界并发搜索
-> 别名发现 -> 规则兜底 -> LLM 补救。搜索结果只暴露公开安全字段，任何真实
URL 都不允许出现在返回项里。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

from civitai_api import CivitaiClient, CivitaiRedClient, HuggingFaceClient

from .llm_client import OpenAICompatibleLLMClient, resolve_provider

logger = logging.getLogger("winefox.runtime.lora_search")

_SOURCE_ALIAS_FILE = Path(__file__).resolve().parent.parent / "lora_source_aliases.json"
_FAMILY_BASE_MODELS = {
    "anima": ["Anima"],
    "illustrious": ["Illustrious"],
}
_FAMILIES = frozenset({"anima", "illustrious"})
_DEFAULT_SOURCES = ["civitai", "huggingface"]
_LLM_CACHE_TTL = 60 * 60
_LLM_CACHE_MAX = 500


def _bounded_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _public_text(value: Any, limit: int = 200) -> str:
    """脱敏公开文本：命中 URL/凭据/绝对路径/穿越/网络路径模式的字符串一律置空。"""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if re.search(r"(?i)https?://|www\.|ftp://|file://|token=|api[_-]?key|authorization|cookie", text):
        return ""
    if re.search(r"(?i)(?:[a-z]:[\\/]|\\\\|^[\\/]|\.\.|//)", text):
        return ""
    return text[:limit]


def _safe_error(value: Any) -> str:
    text = str(value or "LoRA 搜索失败")
    text = re.sub(r"(?i)(token=)[^&\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(authorization|cookie)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
    text = re.sub(r"(?i)https?://[^\s,;]+", "[URL]", text)
    text = re.sub(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n,;]+", "[LOCAL_PATH]", text)
    return _bounded_text(text, 600)


@lru_cache(maxsize=8192)
def _cached_normalize_profile_match_key(text: str) -> str:
    text = str(text or "").strip().lower()
    text = re.sub(r"\.(safetensors|ckpt|pt|bin)$", "", text, flags=re.I)
    return re.sub(r"[^0-9a-z぀-ヿ㐀-鿿]+", "", text)


class LoraSearchService:
    """无 AstrBot 依赖的 LoRA 多源搜索编排服务。"""

    def __init__(
        self,
        *,
        config: Any,
        data_root: Path,
        client_factory: Callable[..., Any] = CivitaiClient,
        red_client_factory: Callable[..., Any] = CivitaiRedClient,
        hf_client_factory: Callable[..., Any] = HuggingFaceClient,
        llm_client_factory: Callable[..., Any] = OpenAICompatibleLLMClient,
        profile_seed_fn: Callable[[str, str], list[str]] | None = None,
        preview_service: Any = None,
    ) -> None:
        self.config = config
        self.data_root = Path(data_root)
        self.client_factory = client_factory
        self.red_client_factory = red_client_factory
        self.hf_client_factory = hf_client_factory
        self.llm_client_factory = llm_client_factory
        self.profile_seed_fn = profile_seed_fn
        self.preview_service = preview_service
        self._clients: dict[str, Any] = {}
        self._search_semaphore: asyncio.Semaphore | None = None
        self._lora_source_alias_map_cache: dict | None = None
        self._source_alias_bundle_cache: dict | None = None
        self._lora_search_llm_cache: dict[str, dict] = {}
        self._llm_client_cache: Any = None
        self._llm_model = ""
        self._llm_checked = False

    # ------------------------------------------------------------------ 配置
    def _lora_download_settings(self) -> dict[str, Any]:
        try:
            snapshot = self.config.snapshot()
        except Exception:
            return {}
        value = snapshot.get("lora_download")
        return dict(value) if isinstance(value, Mapping) else {}

    def _client_kwargs(self) -> dict[str, Any]:
        settings = self._lora_download_settings()
        return {
            "proxy": str(settings.get("proxy") or "").strip(),
            "timeout": max(10, min(int(settings.get("request_timeout") or 60), 180)),
            "split": max(1, min(int(settings.get("split") or 4), 8)),
            "network_max_retries": 2,
        }

    def _source_client(self, source: str) -> Any:
        source = str(source or "civitai").strip().lower()
        client = self._clients.get(source)
        if client is not None:
            return client
        kwargs = self._client_kwargs()
        if source == "civitai_red":
            client = self.red_client_factory(
                # civitai.red 是独立来源，绝不能把 civitai.com 的凭据转发过去。
                api_token="",
                cookie="",
                **kwargs,
            )
        elif source == "huggingface":
            client = self.hf_client_factory(
                api_token=self.config.get_secret("huggingface_api_token") or "",
                **kwargs,
            )
        else:
            client = self.client_factory(
                api_token=self.config.get_secret("civitai_api_token") or "",
                cookie=self.config.get_secret("civitai_cookie") or "",
                downloader="multithread",
                **kwargs,
            )
        self._clients[source] = client
        return client

    def _resolve_llm(self) -> Any:
        if self._llm_checked:
            return self._llm_client_cache
        self._llm_checked = True
        client: Any = None
        try:
            snapshot = self.config.snapshot()
            resolved = resolve_provider(snapshot, "text")
            secret = self.config.get_secret(resolved.secret_name) or ""
            if resolved.enabled and resolved.base_url and resolved.model and secret:
                client = self.llm_client_factory(
                    resolved.base_url,
                    api_key=secret,
                    timeout_seconds=resolved.timeout_seconds,
                )
                self._llm_model = resolved.model
        except Exception as exc:
            logger.warning("LoRA 搜索 LLM 不可用: %s", _safe_error(exc))
            client = None
        self._llm_client_cache = client
        return client

    def _llm_enabled(self) -> bool:
        return self._resolve_llm() is not None

    # ------------------------------------------------------------------ 公开接口
    async def search(
        self,
        query: str,
        *,
        family: str = "illustrious",
        limit: int = 12,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """执行固定查询流水线并返回公开安全的结果。"""
        query = self._normalize_civitai_search_term(query)
        if not query:
            return {
                "query": "",
                "family": self._normalize_family(family),
                "sources": [],
                "items": [],
                "searched_terms": [],
                "alias": "",
                "errors": [],
            }
        family = self._normalize_family(family)
        choice_limit = max(1, min(int(limit or 12), 20))
        source_list = self._parse_lora_search_sources(
            sources if sources is not None else _DEFAULT_SOURCES
        )
        initial_terms = self._dedupe_civitai_search_terms(
            [query] + self._local_lora_search_seed_terms(query) + self._profile_seed_terms(query, family)
        )[:6]
        search_terms = list(initial_terms or [query])
        query_source_matches = self._lora_source_alias_matches_in_text(query, limit=4)
        alias = self._default_alias_for_search_query(query)
        base_models = self._current_civitai_base_models(family)
        candidates: list[dict[str, Any]] = []
        reserve_candidates: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[str] = set()
        reserve_seen: set[str] = set()
        discovered_terms: list[str] = []
        discovered_term_keys = {self._normalize_profile_match_key(x) for x in search_terms}
        trusted_cjk_title_term_keys = {
            self._normalize_profile_match_key(term)
            for term in (initial_terms or [query])
            if re.search(r"[㐀-鿿぀-ヿ]", str(term or ""))
            and self._normalize_profile_match_key(term)
        }
        source_title_markers = sorted(
            self._civitai_title_source_markers(),
            key=lambda value: len(self._normalize_profile_match_key(value)),
            reverse=True,
        )

        def remember_discovered_term(term: str) -> None:
            term = self._normalize_civitai_search_term(term)
            key = self._normalize_profile_match_key(term)
            if (
                not term
                or not key
                or key in discovered_term_keys
                or self._lora_search_term_is_source_only(term)
            ):
                return
            discovered_term_keys.add(key)
            discovered_terms.append(term)

        def discover_terms_from_raw_item(item: dict, term: str) -> None:
            if not isinstance(item, dict):
                return
            query_key = self._normalize_profile_match_key(query)
            term_key = self._normalize_profile_match_key(term)
            haystack = " ".join([
                str(item.get("name") or ""),
                str(item.get("version_name") or ""),
                " ".join(str(x) for x in (item.get("trained_words") or [])),
                " ".join(str(x) for x in (item.get("tags") or [])),
            ])
            haystack_key = self._normalize_profile_match_key(haystack)
            if query_key and query_key not in haystack_key and term_key and term_key not in haystack_key:
                return
            source_hint = ""
            for source_name in source_title_markers:
                source_key = self._normalize_profile_match_key(source_name)
                if len(source_key) >= 2 and source_key in haystack_key:
                    source_hint = source_name
                    break
            raw_candidates: list[str] = []
            raw_candidates.extend(self._civitai_title_character_candidates(str(item.get("name") or "")))
            raw_candidates.extend(self._civitai_title_character_candidates(str(item.get("version_name") or "")))
            query_tokens = self._identity_token_set([query])
            for candidate in raw_candidates:
                candidate = self._normalize_civitai_search_term(candidate)
                if not candidate or self._lora_search_term_is_source_only(candidate):
                    continue
                candidate_tokens = self._identity_token_set([candidate])
                if not candidate_tokens or not (candidate_tokens & query_tokens):
                    if not self._identity_values_related([candidate], [query]):
                        continue
                if not re.search(r"[A-Za-z]", candidate):
                    continue
                words = re.findall(r"[A-Za-z][A-Za-z0-9']*", candidate)
                if not words or len(words) > 3:
                    continue
                candidate = " ".join(words)
                if len(self._normalize_profile_match_key(candidate)) < 4:
                    continue
                remember_discovered_term(f"{candidate} {source_hint}" if source_hint else candidate)
                remember_discovered_term(candidate)
                if len(discovered_terms) >= 4:
                    return

        async def run_one_search(source: str, term: str, index: int, source_index: int):
            search_semaphore = self._search_semaphore
            if str(source or "").strip().lower() in {"civitai", "civitai_red"}:
                if search_semaphore is None:
                    search_semaphore = asyncio.Semaphore(4)
                    self._search_semaphore = search_semaphore

            async def execute_search():
                fetch_limit = max(choice_limit * 3, 12)
                client = self._source_client(source)
                return source, term, index, source_index, await client.search_loras(
                    term, limit=fetch_limit, base_models=base_models
                )

            try:
                if search_semaphore is not None:
                    async with search_semaphore:
                        return await execute_search()
                return await execute_search()
            except Exception as e:
                err = _safe_error(e)
                label = self._lora_source_label(source)
                errors.append(f"{label}/{term}: {err}")
                logger.warning("%s 搜索失败 term=%s: %s", label, term, _bounded_text(err, 200))
                return source, term, index, source_index, []

        def absorb_items(source: str, term: str, index: int, source_index: int, items: list) -> None:
            for item in items:
                discover_terms_from_raw_item(item, term)
                source = str(item.get("source") or source or "civitai").strip().lower()
                if (
                    str(item.get('type') or '').lower() not in {'lora', 'locon', 'lycoris'}
                    or not self._is_base_model_compatible_with_active_family(item.get('base_model') or '', family)
                    or not item.get('page_url')
                    or not self._is_civitai_search_candidate_allowed(item)
                ):
                    continue
                key = str(item.get('version_id') or item.get('page_url') or '').strip()
                if key in seen:
                    continue
                enriched = dict(item)
                enriched['source'] = source
                enriched['_source_query'] = term
                enriched['_source_index'] = int(index or 0) * max(len(source_list), 1) + int(source_index or 0)
                enriched['_relevance'] = self._civitai_candidate_relevance(enriched, [query, term])
                if query_source_matches:
                    source_haystack = self._civitai_candidate_source_haystack(enriched)
                    if self._civitai_item_matches_query_source(enriched, query_source_matches, haystack=source_haystack):
                        enriched['_source_context_match'] = True
                        enriched['_relevance'] = max(enriched['_relevance'], 5)
                    elif self._civitai_item_has_conflicting_source(enriched, query_source_matches, haystack=source_haystack):
                        logger.info(
                            "跳过来源冲突 LoRA 搜索结果: %s | term=%s query_sources=%s",
                            item.get("name"),
                            term,
                            ",".join(str(x.get('matched_term') or x.get('source') or '') for x in query_source_matches),
                        )
                        continue
                    else:
                        enriched['_source_context_missing'] = True
                term_key = self._normalize_profile_match_key(term)
                cjk_title_relevance = 0
                if term_key in trusted_cjk_title_term_keys:
                    cjk_title_relevance = self._civitai_cjk_title_match_relevance(enriched, term)
                if cjk_title_relevance:
                    enriched['_relevance'] = max(enriched['_relevance'], cjk_title_relevance)
                    enriched['_trusted_cjk_title_match'] = True
                if enriched['_relevance'] <= 0:
                    if self._lora_search_term_can_keep_low_relevance(term) and key not in reserve_seen:
                        reserve_seen.add(key)
                        reserve = dict(enriched)
                        reserve['_relevance'] = 1
                        reserve['_low_relevance'] = True
                        reserve_candidates.append(reserve)
                    logger.info("跳过低相关 LoRA 搜索结果: %s | source=%s term=%s", item.get("name"), source, term)
                    continue
                seen.add(key)
                candidates.append(enriched)

        llm_plan: dict[str, Any] = {}
        first_results = await asyncio.gather(
            *[
                run_one_search(source, term, term_idx, source_idx)
                for term_idx, term in enumerate(initial_terms or [query])
                for source_idx, source in enumerate(source_list)
            ]
        )
        for source, term, index, source_idx, items in first_results:
            absorb_items(source, term, index, source_idx, items)

        if discovered_terms:
            for term in discovered_terms[:6]:
                if term.lower() not in {x.lower() for x in search_terms}:
                    search_terms.append(term)
            logger.info("LoRA 搜索从候选证据发现别名: %s", ", ".join(discovered_terms[:6]))

        should_expand = self._should_expand_lora_search_with_llm(query, candidates, source_list, choice_limit)
        if should_expand:
            try:
                llm_plan = await self._llm_civitai_search_terms(query, family, sources=source_list)
            except Exception as exc:
                logger.warning("Civitai 搜索词 LLM 扩展失败: %s", _safe_error(exc))
        llm_terms = self._dedupe_civitai_search_terms(llm_plan.get("queries") or [])[:6]
        if llm_plan.get("alias"):
            alias = str(llm_plan.get("alias") or "").strip()
        if llm_terms:
            logger.info("LoRA 搜索词 LLM 扩展: %s => %s", query, ", ".join(llm_terms))
        elif should_expand:
            logger.info("LoRA 搜索词 LLM 没有可用扩展，本次只使用原始关键词: %s", query)
        for term in llm_terms:
            if term.lower() not in {x.lower() for x in search_terms}:
                search_terms.append(term)

        logger.info(
            "LoRA 搜索源: %s | 搜索词: %s",
            ", ".join(self._lora_source_label(x) for x in source_list),
            ", ".join(search_terms[:6]),
        )
        searched_count = len(initial_terms or [query])
        tasks = [
            run_one_search(source, term, idx, source_idx)
            for idx, term in enumerate(search_terms[searched_count:6], searched_count)
            for source_idx, source in enumerate(source_list)
        ]
        if tasks:
            for source, term, index, source_idx, items in await asyncio.gather(*tasks):
                absorb_items(source, term, index, source_idx, items)
        if not candidates:
            fallback_terms = self._lora_search_fallback_terms(query, search_terms, llm_plan)
            if fallback_terms:
                fallback_start = len(search_terms)
                for term in fallback_terms:
                    if term.lower() not in {x.lower() for x in search_terms}:
                        search_terms.append(term)
                logger.info("LoRA 首轮无结果，追加兜底搜索: %s", ", ".join(fallback_terms))
                fallback_tasks = [
                    run_one_search(source, term, fallback_start + idx, source_idx)
                    for idx, term in enumerate(fallback_terms)
                    for source_idx, source in enumerate(source_list)
                ]
                for source, term, index, source_idx, items in await asyncio.gather(*fallback_tasks):
                    absorb_items(source, term, index, source_idx, items)
        if not candidates:
            rescue_terms = await self._llm_civitai_rescue_search_terms(query, family, search_terms, sources=source_list)
            if rescue_terms:
                rescue_start = len(search_terms)
                for term in rescue_terms:
                    if term.lower() not in {x.lower() for x in search_terms}:
                        search_terms.append(term)
                logger.info("LoRA 无强相关结果，LLM 追加补救搜索: %s", ", ".join(rescue_terms))
                rescue_tasks = [
                    run_one_search(source, term, rescue_start + idx, source_idx)
                    for idx, term in enumerate(rescue_terms)
                    for source_idx, source in enumerate(source_list)
                ]
                for source, term, index, source_idx, items in await asyncio.gather(*rescue_tasks):
                    absorb_items(source, term, index, source_idx, items)
        if not candidates and reserve_candidates:
            logger.info("LoRA 搜索无强相关结果，保留低置信候选 %s 个", len(reserve_candidates))
            for item in reserve_candidates:
                key = str(item.get('version_id') or item.get('page_url') or '').strip()
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(item)
                if len(candidates) >= choice_limit:
                    break
        for item in candidates:
            original_relevance = int(item.get('_relevance') or 0)
            recalculated = self._civitai_candidate_relevance(item, search_terms)
            item['_relevance'] = max(original_relevance, recalculated)
            if item.get('_low_relevance') and item.get('_relevance', 0) <= 0:
                item['_relevance'] = 1
            item['_score'] = self._score_civitai_candidate(item, search_terms, int(item.get('_source_index') or 0))
        candidates.sort(key=lambda item: item.get('_score', 0), reverse=True)
        items = [self._public_item(item, family) for item in candidates[:choice_limit]]
        return {
            "query": query,
            "family": family,
            "sources": source_list,
            "items": items,
            "searched_terms": search_terms[:8],
            "alias": alias or "",
            "errors": errors[:5],
        }

    def _public_item(self, item: dict, family: str) -> dict[str, Any]:
        source = str(item.get("source") or "civitai").strip().lower()
        preview_url = str(item.get("preview_url") or "").strip()
        preview_token = ""
        if preview_url and self.preview_service is not None:
            try:
                preview_token = str(
                    self.preview_service.register("", preview_url, source=source) or ""
                )
            except Exception:
                preview_token = ""
        return {
            "id": uuid.uuid4().hex,
            "source": source,
            "family": family,
            "model_id": _bounded_text(item.get("model_id"), 80),
            "version_id": _bounded_text(item.get("version_id"), 80),
            "file_name": _public_text(item.get("file_name"), 200),
            "name": _public_text(item.get("name") or "Civitai LoRA", 160),
            "version_name": _public_text(item.get("version_name"), 100),
            "base_model": _public_text(item.get("base_model"), 80),
            "trained_words": [
                _public_text(x, 100)
                for x in (item.get("trained_words") or [])[:12]
                if _public_text(x, 100)
            ],
            "tags": [
                _public_text(x, 60)
                for x in (item.get("tags") or [])[:12]
                if _public_text(x, 60)
            ],
            "download_count": int(item.get("download_count") or 0),
            "heart_count": int(item.get("heart_count") or 0),
            "nsfw": bool(item.get("nsfw")),
            "score": round(float(item.get("_score") or 0), 2),
            "relevance": int(item.get("_relevance") or 0),
            "preview_token": preview_token,
        }

    def _profile_seed_terms(self, query: str, family: str) -> list[str]:
        fn = self.profile_seed_fn
        if fn is None:
            return []
        try:
            values = fn(query, family)
        except Exception as exc:
            logger.warning("profile_seed_fn 失败: %s", _safe_error(exc))
            return []
        if not isinstance(values, list):
            return []
        return [str(x) for x in values if str(x or "").strip()]

    # ------------------------------------------------------------------ 基础清洗
    @staticmethod
    def _normalize_family(family: str) -> str:
        value = str(family or "illustrious").strip().lower()
        return value if value in _FAMILIES else "illustrious"

    def _default_alias_for_search_query(self, query: str) -> str:
        return ''

    def _normalize_civitai_search_term(self, term: str) -> str:
        term = str(term or '').strip()
        term = re.sub(r'<[^>]+>', ' ', term)
        term = re.sub(r'^(query|keyword|name|character|角色|关键词)\s*[:：]\s*', '', term, flags=re.IGNORECASE)
        term = term.strip('`"\'“”')
        term = (
            term.replace("（", "(")
            .replace("）", ")")
            .replace("【", "[")
            .replace("】", "]")
        )
        if term.count("(") != term.count(")"):
            term = term.replace("(", " ").replace(")", " ")
        if term.count("[") != term.count("]"):
            term = term.replace("[", " ").replace("]", " ")
        if re.fullmatch(r"\([^()]{1,80}\)", term) or re.fullmatch(r"\[[^\[\]]{1,80}\]", term):
            term = term[1:-1]
        term = re.sub(r"[\[\]{}]", " ", term)
        term = re.sub(r'\s+', ' ', term).strip()
        term = term.strip(" ，,、:：;；|｜")
        if not term or 'civitai.com' in term.lower() or len(term) > 80:
            return ''
        return term

    def _dedupe_civitai_search_terms(self, terms: list) -> list:
        seen = set()
        cleaned = []
        for term in terms:
            term = self._normalize_civitai_search_term(term)
            if not term:
                continue
            key = re.sub(r'\s+', ' ', term).strip().lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(term)
        return cleaned[:10]

    def _lora_search_spelling_variants(self, term: str) -> list:
        term = self._normalize_civitai_search_term(term)
        if not term or self._lora_search_term_is_source_only(term):
            return []
        if re.search(r"[㐀-鿿぀-ヿ]", term):
            return []
        parts = re.findall(r"[A-Za-z][A-Za-z0-9']*", term)
        if len(parts) < 2 or len(parts) > 2:
            return []
        compact = "".join(parts)
        if len(compact) < 4 or len(compact) > 28:
            return []
        variants = [
            compact,
            "".join(part[:1].upper() + part[1:] for part in parts),
        ]
        return [
            item for item in self._dedupe_civitai_search_terms(variants)
            if self._normalize_profile_match_key(item) != self._normalize_profile_match_key(term)
        ][:2]

    def _clean_lora_search_piece(self, text: str) -> str:
        text = str(text or "").strip()
        text = re.sub(r"(?:^|[\s，,、])(?:游戏|手游|端游|动漫|动画|番剧|作品|角色|人物|里面|里边|里的|中的|中|出自|来自|同人|官方|lora|LoRA|模型)(?:$|[\s，,、])", " ", text)
        text = re.sub(r"^(?:的|游戏|手游|端游|动漫|动画|番剧|作品|角色|人物|里面|里边|里的|中的|中|出自|来自|同人|官方)+", "", text)
        text = re.sub(r"(?:的|角色|人物|lora|LoRA|模型)+$", "", text)
        return re.sub(r"\s+", " ", text).strip(" ，,、:：")

    def _civitai_meaningful_parts(self, term: str) -> list:
        generic = {
            "lora", "locon", "lycoris", "model", "anime", "anima", "sd", "sdxl",
            "stable", "diffusion", "style", "styles", "character", "official",
        }
        parts = []
        for p in re.split(r'[\s:_/\\|,，()（）\[\].-]+', str(term or '').lower()):
            p = p.strip()
            if not p or p in generic:
                continue
            if re.search(r'[一-鿿぀-ヿ]', p):
                if len(p) >= 2:
                    parts.append(p)
            elif len(p) >= 3:
                parts.append(p)
        return parts

    # ------------------------------------------------------------------ 来源解析
    def _parse_lora_search_sources(self, raw) -> list:
        if isinstance(raw, (list, tuple, set)):
            parts = list(raw)
        else:
            parts = re.split(r"[,，;；\s]+", str(raw or ""))
        alias_map = {
            "civitai_red": "civitai_red", "civitai-red": "civitai_red", "civitaired": "civitai_red",
            "red": "civitai_red", "r18": "civitai_red", "r18+": "civitai_red", "18+": "civitai_red",
            "adult": "civitai_red", "nsfw": "civitai_red", "红站": "civitai_red", "成人": "civitai_red",
            "civitai": "civitai", "cv": "civitai", "站内": "civitai",
            "hf": "huggingface", "huggingface": "huggingface", "hugging": "huggingface",
            "hugging-face": "huggingface", "hugging_face": "huggingface", "抱脸": "huggingface",
        }
        result = []
        seen = set()
        for item in parts:
            key = alias_map.get(str(item or "").strip().lower())
            if key and key not in seen:
                seen.add(key)
                result.append(key)
        return result or ["civitai"]

    def _lora_source_label(self, source: str) -> str:
        return {
            "civitai": "Civitai",
            "civitai_red": "Civitai Red (R18+)",
            "huggingface": "Hugging Face",
        }.get(str(source or "").strip().lower(), str(source or "未知来源"))

    # ------------------------------------------------------------------ source-alias bundle
    def _lora_source_alias_json_paths(self) -> list:
        paths = [_SOURCE_ALIAS_FILE]
        data_path = self.data_root / "lora_source_aliases.json"
        if str(data_path) != str(paths[0]):
            paths.append(data_path)
        return paths

    def _normalize_lora_source_alias_map(self, data) -> dict:
        if isinstance(data, dict) and isinstance(data.get("sources"), dict):
            data = data.get("sources")
        if not isinstance(data, dict):
            return {}
        result = {}
        for source, aliases in data.items():
            source = self._normalize_civitai_search_term(source)
            if not source:
                continue
            if isinstance(aliases, str):
                aliases = re.split(r"[,，;；\n\r]+", aliases)
            elif not isinstance(aliases, (list, tuple, set)):
                aliases = []
            cleaned = []
            seen = {self._normalize_profile_match_key(source)}
            for alias in aliases:
                alias = self._normalize_civitai_search_term(alias)
                marker = self._normalize_profile_match_key(alias)
                if alias and marker and marker not in seen:
                    seen.add(marker)
                    cleaned.append(alias)
            result[source] = cleaned
        return result

    @staticmethod
    def _merge_lora_source_alias_json_pairs(pairs: list) -> dict:
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

    def _load_lora_source_alias_map(self) -> dict:
        paths = self._lora_source_alias_json_paths()
        signature = []
        for path in paths:
            try:
                path = Path(path)
                signature.append((str(path), path.stat().st_mtime_ns if path.exists() else -1))
            except Exception:
                signature.append((str(path), -1))
        cached = self._lora_source_alias_map_cache
        if isinstance(cached, dict) and cached.get("signature") == signature:
            return dict(cached.get("aliases") or {})
        merged = {}
        for path in paths:
            try:
                path = Path(path)
                if not path.exists():
                    continue
                data = json.loads(
                    path.read_text(encoding="utf-8"),
                    object_pairs_hook=self._merge_lora_source_alias_json_pairs,
                )
                for source, aliases in self._normalize_lora_source_alias_map(data).items():
                    current = merged.setdefault(source, [])
                    seen = {self._normalize_profile_match_key(source)}
                    seen.update(self._normalize_profile_match_key(item) for item in current)
                    for alias in aliases:
                        marker = self._normalize_profile_match_key(alias)
                        if marker and marker not in seen:
                            seen.add(marker)
                            current.append(alias)
            except Exception as exc:
                logger.warning("读取 LoRA 来源别名表失败 %s: %s", path, _safe_error(exc))
        self._lora_source_alias_map_cache = {"signature": signature, "aliases": dict(merged)}
        return merged

    def _lora_search_source_alias_map(self) -> dict:
        return self._load_lora_source_alias_map()

    def _source_alias_signature(self) -> tuple:
        signature = []
        for path in self._lora_source_alias_json_paths():
            try:
                path = Path(path)
                signature.append((str(path), path.stat().st_mtime_ns if path.exists() else -1))
            except Exception:
                signature.append((str(path), -1))
        return tuple(signature)

    def _source_alias_bundle(self) -> dict:
        signature = self._source_alias_signature()
        cached = self._source_alias_bundle_cache
        if isinstance(cached, dict) and cached.get("signature") == signature:
            return cached

        alias_map = self._load_lora_source_alias_map()
        source_only_keys = set()
        source_markers = set()
        for source, aliases in alias_map.items():
            source = str(source or "").strip()
            source_key = self._normalize_profile_match_key(source)
            if source_key:
                source_only_keys.add(source_key)
            if source:
                source_markers.add(source)
            for alias in aliases or []:
                alias = str(alias or "").strip()
                alias_key = self._normalize_profile_match_key(alias)
                if alias_key:
                    source_only_keys.add(alias_key)
                if alias:
                    source_markers.add(alias)

        term_patterns: dict[str, Any] = {}

        def get_compiled_pattern(term: str):
            if term in term_patterns:
                return term_patterns[term]
            if not self._lora_source_alias_term_is_matchable(term):
                term_patterns[term] = None
                return None
            pattern = self._lora_source_alias_term_pattern(term)
            compiled = None
            if pattern:
                try:
                    compiled = re.compile(pattern, flags=re.I)
                except re.error:
                    compiled = None
            term_patterns[term] = compiled
            return compiled

        entries = []
        for source, aliases in alias_map.items():
            terms = self._dedupe_lora_source_alias_terms([source] + list(aliases or []))
            match_terms = [term for term in terms if self._lora_source_alias_term_is_matchable(term)]
            if not match_terms:
                continue
            for term in match_terms:
                get_compiled_pattern(term)
            source_key = self._normalize_profile_match_key(source)
            entries.append({
                "source": source,
                "aliases": [
                    term for term in terms
                    if self._normalize_profile_match_key(term) != source_key
                ],
                "terms": terms,
                "match_terms": sorted(
                    match_terms,
                    key=lambda term: len(self._normalize_profile_match_key(term)),
                    reverse=True,
                ),
                "source_key": source_key,
                "term_keys": frozenset(
                    self._normalize_profile_match_key(term)
                    for term in terms
                    if self._normalize_profile_match_key(term)
                ),
            })
        entries.sort(
            key=lambda entry: max(
                (len(self._normalize_profile_match_key(term)) for term in entry.get("match_terms") or [""]),
                default=0,
            ),
            reverse=True,
        )
        marker_compact_set = frozenset(
            self._normalize_profile_match_key(marker)
            for marker in source_markers
            if self._normalize_profile_match_key(marker)
        )
        marker_lower_set = frozenset(
            re.sub(r"\s+", " ", str(marker or "").strip().lower())
            for marker in source_markers
            if str(marker or "").strip()
            and (
                len(self._normalize_profile_match_key(marker)) >= 3
                or (
                    re.search(r"[぀-ヿ㐀-鿿]", str(marker or ""))
                    and len(self._normalize_profile_match_key(marker)) >= 2
                )
            )
        )
        bundle = {
            "signature": signature,
            "alias_map": dict(alias_map),
            "source_only_keys": frozenset(source_only_keys),
            "source_markers": frozenset(source_markers),
            "marker_compact_set": marker_compact_set,
            "marker_lower_set": marker_lower_set,
            "entries": entries,
            "term_patterns": term_patterns,
        }
        self._source_alias_bundle_cache = bundle
        return bundle

    def _lora_search_source_only_keys(self) -> set:
        return self._source_alias_bundle().get("source_only_keys", frozenset())

    def _dedupe_lora_source_alias_terms(self, terms: list) -> list:
        seen = set()
        cleaned = []
        for term in terms or []:
            term = self._normalize_civitai_search_term(term)
            marker = self._normalize_profile_match_key(term)
            if not term or not marker or marker in seen:
                continue
            seen.add(marker)
            cleaned.append(term)
        return cleaned

    def _lora_source_alias_term_is_matchable(self, term: str) -> bool:
        marker = self._normalize_profile_match_key(term)
        if not marker:
            return False
        if re.fullmatch(r"[a-z]{1,2}", marker):
            return False
        return len(marker) >= 2

    def _lora_source_alias_term_pattern(self, term: str) -> str:
        term = str(term or "").strip()
        if not term:
            return ""
        parts = re.findall(r"[0-9A-Za-z]+|[぀-ヿ㐀-鿿]+", term)
        if not parts:
            return ""
        separator = r"[^0-9A-Za-z぀-ヿ㐀-鿿]*"
        body = separator.join(re.escape(part) for part in parts)
        prefix = r"(?<![0-9A-Za-z])" if re.match(r"[0-9A-Za-z]", parts[0]) else ""
        suffix = r"(?![0-9A-Za-z])" if re.search(r"[0-9A-Za-z]$", parts[-1]) else ""
        return f"{prefix}{body}{suffix}"

    def _lora_source_alias_term_in_text(self, text: str, term: str) -> bool:
        if not self._lora_source_alias_term_is_matchable(term):
            return False
        text = str(text or "")
        if not text.strip():
            return False
        bundle = self._source_alias_bundle()
        term_patterns = bundle.setdefault("term_patterns", {})
        compiled = term_patterns.get(term)
        if compiled is None and term not in term_patterns:
            pattern = self._lora_source_alias_term_pattern(term)
            if pattern:
                try:
                    compiled = re.compile(pattern, flags=re.I)
                except re.error:
                    compiled = None
            term_patterns[term] = compiled
        if compiled is not None and compiled.search(text):
            return True
        marker = self._normalize_profile_match_key(term)
        if len(marker) >= 4 and marker in self._normalize_profile_match_key(text):
            return True
        return False

    def _lora_source_alias_entries(self) -> list:
        return self._source_alias_bundle().get("entries", [])

    def _lora_source_alias_matches_in_text(self, text: str, limit: int = 4) -> list:
        matches = []
        seen = set()
        for entry in self._lora_source_alias_entries():
            matched_term = ""
            for term in entry.get("match_terms") or []:
                if self._lora_source_alias_term_in_text(text, term):
                    matched_term = term
                    break
            if not matched_term:
                continue
            source_key = entry.get("source_key") or self._normalize_profile_match_key(entry.get("source") or "")
            if source_key in seen:
                continue
            seen.add(source_key)
            item = dict(entry)
            item["matched_term"] = matched_term
            matches.append(item)
            if len(matches) >= max(1, int(limit or 4)):
                break
        return matches

    def _lora_source_alias_matches_related(self, left: dict, right: dict) -> bool:
        left_keys = set(left.get("term_keys") or [])
        right_keys = set(right.get("term_keys") or [])
        if not left_keys or not right_keys:
            return False
        return bool(left_keys & right_keys)

    def _lora_source_terms_for_match(self, match: dict) -> list:
        if not isinstance(match, dict):
            return []
        matched = str(match.get("matched_term") or "").strip()
        raw_terms = [matched, match.get("source") or ""]
        raw_terms.extend(match.get("aliases") or [])

        def rank(term: str) -> tuple:
            term = str(term or "")
            if matched and self._normalize_profile_match_key(term) == self._normalize_profile_match_key(matched):
                return (0, -len(self._normalize_profile_match_key(term)))
            if re.search(r"[A-Za-z]", term) and len(self._normalize_profile_match_key(term)) >= 4:
                return (1, -len(self._normalize_profile_match_key(term)))
            if re.fullmatch(r"[A-Za-z0-9]{3,}", self._normalize_profile_match_key(term)):
                return (2, -len(self._normalize_profile_match_key(term)))
            return (3, -len(self._normalize_profile_match_key(term)))

        return sorted(self._dedupe_lora_source_alias_terms(raw_terms), key=rank)[:4]

    def _lora_source_aliases_for_llm_context(self, query: str) -> dict:
        context = {}
        for match in self._lora_source_alias_matches_in_text(query, limit=4):
            source = str(match.get("source") or "").strip()
            if source:
                context[source] = self._lora_source_terms_for_match(match)
        return context

    def _remove_lora_source_alias_terms_from_query(self, text: str, matches: list) -> str:
        result = str(text or "")
        terms = []
        for match in matches or []:
            terms.extend(match.get("terms") or [])
        for term in sorted(
            self._dedupe_lora_source_alias_terms(terms),
            key=lambda item: len(self._normalize_profile_match_key(item)),
            reverse=True,
        ):
            pattern = self._lora_source_alias_term_pattern(term)
            if pattern:
                result = re.sub(pattern, " ", result, flags=re.I)
        return self._clean_lora_search_piece(result)

    def _lora_search_term_is_source_only(self, term: str) -> bool:
        text = self._normalize_civitai_search_term(term)
        if not text:
            return False
        key = self._normalize_profile_match_key(text)
        source_keys = self._lora_search_source_only_keys()
        if key in source_keys:
            return True
        weak_parts = {
            "game", "mobile", "anime", "series", "source", "franchise", "character",
            "model", "lora", "style", "official",
        }
        parts = self._civitai_meaningful_parts(text)
        if parts and all(part in weak_parts or part in source_keys for part in parts):
            return True
        return False

    # ------------------------------------------------------------------ 本地种子/兜底
    def _known_lora_character_search_terms(self, query: str) -> list:
        query_key = self._normalize_profile_match_key(query)
        if not query_key:
            return []
        known = {
            "爱弥斯": ["Aemeath Wuthering Waves", "Aemeath"],
            "艾弥斯": ["Aemeath Wuthering Waves", "Aemeath"],
            "弗洛洛": ["Phrolova Wuthering Waves", "Phrolova", "Frolova Wuthering Waves", "Frolova"],
            "弗诺诺": ["Phrolova Wuthering Waves", "Phrolova", "Frolova Wuthering Waves", "Frolova"],
        }
        terms = []
        for alias, values in known.items():
            alias_key = self._normalize_profile_match_key(alias)
            if alias_key and alias_key in query_key:
                terms.extend(values)
        return [
            term for term in self._dedupe_civitai_search_terms(terms)
            if self._normalize_profile_match_key(term) != query_key
        ][:4]

    def _local_lora_search_seed_terms(self, query: str) -> list:
        """纯文本本地种子：已知角色表 + 来源关系正则 + 移除来源别名。

        本地档案匹配是阶段四，种子改由外部 profile_seed_fn 回调提供。
        """
        query = self._normalize_civitai_search_term(query)
        if not query:
            return []
        terms = self._known_lora_character_search_terms(query)
        source_matches = self._lora_source_alias_matches_in_text(query, limit=4)

        def add_character_source_terms(character: str, source_match) -> None:
            character = self._clean_lora_search_piece(character)
            if not character:
                return
            if isinstance(source_match, dict):
                source_terms = self._lora_source_terms_for_match(source_match)
            else:
                source = self._clean_lora_search_piece(source_match)
                source_terms = [source] if source else []
            for idx, source_term in enumerate(source_terms):
                terms.append(f"{character} {source_term}")
                if idx <= 1:
                    terms.append(f"{source_term} {character}")

        relation_patterns = [
            r"^(?P<source>.+?)(?:里面的|里边的|里的|中的|中那个|中|出自|来自|from)\s*(?P<character>.+)$",
            r"^(?P<source>.+?)(?:角色|人物)\s*(?P<character>.+)$",
        ]
        relation_matched = False
        for pattern in relation_patterns:
            m = re.search(pattern, query, flags=re.I)
            if m:
                source_part = m.group("source")
                matched_sources = self._lora_source_alias_matches_in_text(source_part, limit=2) or source_matches[:2]
                if matched_sources:
                    for source_match in matched_sources:
                        add_character_source_terms(m.group("character"), source_match)
                else:
                    add_character_source_terms(m.group("character"), source_part)
                relation_matched = True

        query_key = self._normalize_profile_match_key(query)
        if source_matches and not relation_matched:
            character = self._remove_lora_source_alias_terms_from_query(query, source_matches)
            for source_match in source_matches[:2]:
                if character:
                    add_character_source_terms(character, source_match)
                else:
                    terms.extend(self._lora_source_terms_for_match(source_match))
        return [
            term
            for term in self._dedupe_civitai_search_terms(terms)
            if self._normalize_profile_match_key(term) != query_key
        ][:8]

    def _lora_search_fallback_terms(self, query: str, searched_terms: list, llm_plan: dict = None) -> list:
        raw_terms = [query]
        if isinstance(llm_plan, dict):
            raw_terms.append(llm_plan.get("alias") or "")
            raw_terms.extend(llm_plan.get("queries") or [])
        raw_terms.extend(searched_terms or [])
        raw_terms.extend(self._local_lora_search_seed_terms(query))
        expanded = []
        for term in raw_terms:
            term = self._normalize_civitai_search_term(term)
            if not term or self._lora_search_term_is_source_only(term):
                continue
            expanded.append(term)
            expanded.extend(self._lora_search_spelling_variants(term))
        searched_keys = {
            self._normalize_profile_match_key(term)
            for term in (searched_terms or [])
            if self._normalize_profile_match_key(term)
        }
        return [
            term for term in self._dedupe_civitai_search_terms(expanded)
            if self._normalize_profile_match_key(term) not in searched_keys
        ][:4]

    def _lora_search_term_can_keep_low_relevance(self, term: str) -> bool:
        text = self._normalize_civitai_search_term(term)
        if not text or self._lora_search_term_is_source_only(text):
            return False
        lowered = text.lower()
        if re.search(r"(游戏|手游|端游|动漫|动画|番剧|作品|角色|人物|里面|里边|里的|中的|同人|官方|风格|画风)", text):
            return False
        if re.search(r"\b(?:game|anime|series|source|franchise|character|style|official|model|lora)\b", lowered):
            return False
        compact = self._normalize_profile_match_key(text)
        if re.search(r"[㐀-鿿぀-ヿ]", text):
            return 2 <= len(compact) <= 12
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9'\-]{3,32}", text))

    # ------------------------------------------------------------------ identity 辅助
    def _normalize_profile_match_key(self, value: str) -> str:
        return _cached_normalize_profile_match_key(str(value or ""))

    def _is_generic_profile_match_term(self, term: str) -> bool:
        raw = str(term or "").strip().lower()
        norm = self._normalize_profile_match_key(raw)
        if not norm:
            return True
        generic = {
            "1girl", "1boy", "solo", "girl", "boy", "male", "female", "character",
            "masterpiece", "bestquality", "highquality", "anime", "animeillustration",
            "blueeyes", "redeyes", "blackhair", "whitehair", "longhair", "shorthair",
            "blondehair", "silverhair", "lookingatviewer", "smile", "standing",
            "black", "white", "blue", "red", "green", "yellow", "gold", "blonde",
            "silver", "pink", "purple", "brown", "gray", "grey", "orange",
            "fullbody", "upperbody", "portrait", "official", "preview", "base", "v1",
            "v2", "illustrious", "anima", "pony", "sdxl", "lora", "locon", "lycoris",
            "girls", "boys", "woman", "women", "man", "men", "lady", "cute", "kawaii",
            "young", "child", "children", "loli", "lolita", "shota",
            "萝莉", "幼女", "少女", "少年",
            "wutheringwaves", "wuwa", "genshinimpact", "honkaiimpact", "honkaistarrail",
            "cyberpunk", "cyberpunkedgerunners", "edgerunners",
            "cyberpunk2077", "plasticmemories", "clannad", "bishoujomangekyou",
            "鸣潮", "原神", "崩坏", "崩坏星穹铁道", "赛博朋克",
        }
        if norm in generic:
            return True
        if re.fullmatch(r"\d+(girl|boy|girls|boys|person|people)", norm):
            return True
        if re.fullmatch(r"(black|white|blue|red|green|gold|blonde|silver|pink|purple|brown)(hair|eyes)", norm):
            return True
        has_cjk = re.search(r"[぀-ヿ㐀-鿿]", raw)
        if has_cjk:
            return len(norm) < 2
        return len(norm) < 4

    def _identity_token_set(self, values: list) -> set:
        generic = {
            "air", "v1", "v2", "v3", "v4", "v5", "v10", "base", "model", "models",
            "lora", "locon", "lycoris", "illustrious", "illust", "illu", "anima",
            "sdxl", "pony", "noob", "noobai", "preview", "official", "character",
            "collection", "style", "quality", "artist", "artstyle", "anime", "girl",
            "girls", "solo", "hair", "eyes", "wuwa", "wuthering", "waves", "genshin",
            "honkai", "star", "rail", "impact", "version", "ww", "wwil", "il",
        }
        tokens = set()
        for value in values or []:
            text = str(value or "").strip().lower()
            if not text:
                continue
            for chunk in re.findall(r"[㐀-鿿]{2,}", text):
                if not self._is_generic_profile_match_term(chunk):
                    tokens.add(chunk)
            for token in re.findall(r"[a-z][a-z0-9']{2,}", text):
                token = re.sub(r"\d+$", "", token.strip("'"))
                if len(token) < 4 or token in generic or self._is_generic_profile_match_term(token):
                    continue
                tokens.add(token)
        return tokens

    def _identity_values_related(self, claimed_values: list, evidence_values: list) -> bool:
        claimed = [str(x or "").strip() for x in (claimed_values or []) if str(x or "").strip()]
        evidence = [str(x or "").strip() for x in (evidence_values or []) if str(x or "").strip()]
        if not claimed or not evidence:
            return False
        claimed_norms = [self._normalize_profile_match_key(x) for x in claimed]
        evidence_norms = [self._normalize_profile_match_key(x) for x in evidence]
        for c_norm in claimed_norms:
            if not c_norm or self._is_generic_profile_match_term(c_norm):
                continue
            for e_norm in evidence_norms:
                if not e_norm or self._is_generic_profile_match_term(e_norm):
                    continue
                if c_norm == e_norm:
                    return True
                if len(c_norm) >= 4 and c_norm in e_norm:
                    return True
                if len(e_norm) >= 4 and e_norm in c_norm:
                    return True
        return bool(self._identity_token_set(claimed) & self._identity_token_set(evidence))

    def _current_civitai_base_models(self, family: str = None) -> list:
        return _FAMILY_BASE_MODELS.get(self._normalize_family(family), ["Anima"])

    def _is_base_model_compatible_with_active_family(self, base_model: str, family: str = None) -> bool:
        base = str(base_model or "").strip().lower()
        return any(str(item).lower() in base for item in self._current_civitai_base_models(family))

    def _family_label(self, family: str = None) -> str:
        return "Illustrious" if self._normalize_family(family) == "illustrious" else "Anima"

    # ------------------------------------------------------------------ 标题角色候选
    def _civitai_title_character_candidate(self, title: str) -> str:
        candidates = self._civitai_title_character_candidates(title)
        return candidates[0] if candidates else ""

    def _civitai_title_source_markers(self) -> set:
        return self._source_alias_bundle().get("source_markers", frozenset())

    def _civitai_title_looks_like_source_fragment(self, value: str) -> bool:
        return self._civitai_source_fragment_in_text(value)

    def _civitai_source_fragment_in_text(self, value: str, bundle: dict = None) -> bool:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if not text:
            return False
        compact = self._normalize_profile_match_key(text)
        bundle = bundle or self._source_alias_bundle()
        if compact and compact in bundle.get("marker_compact_set", frozenset()):
            return True
        for marker_lower in bundle.get("marker_lower_set", frozenset()):
            if marker_lower and marker_lower in text:
                return True
        return False

    def _strip_civitai_title_descriptors(self, value: str) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return ""
        bundle = self._source_alias_bundle()

        def strip_bracket(match):
            inner = str(match.group(1) or "").strip()
            lowered = inner.lower()
            if (
                self._civitai_source_fragment_in_text(inner, bundle=bundle)
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
        text = re.sub(r"\s+", " ", text).strip(" -_|｜丨／/⁄:：")
        return text

    def _civitai_title_part_character_candidates(self, value: str, original_title: str = "") -> list:
        raw = self._strip_civitai_title_descriptors(value)
        if not raw:
            return []
        pieces = [x.strip(" -_/\\／⁄:：") for x in re.split(r"_+", raw) if x.strip(" -_/\\／⁄:：")]
        candidates = []
        if len(pieces) >= 2:
            prefix = " ".join(pieces[:-1])
            tail = pieces[-1]
            if self._civitai_title_looks_like_source_fragment(prefix):
                candidates.append(tail)
            else:
                candidates.extend([raw, tail])
        else:
            candidates.append(raw)
        result = []
        for item in candidates:
            item = re.sub(r"\s+", " ", str(item or "")).strip()
            if item and not self._is_bad_civitai_profile_alias(item, original_title=original_title):
                result.append(item[:80])
        return self._dedupe_profile_terms(result, original_title=original_title)

    def _civitai_alias_looks_source_prefixed(self, value: str) -> bool:
        raw = re.sub(r"\s+", " ", str(value or "")).strip(" -_|｜丨／/⁄:：")
        pieces = [x.strip(" -_/\\／⁄:：") for x in re.split(r"_+", raw) if x.strip(" -_/\\／⁄:：")]
        return len(pieces) >= 2 and self._civitai_title_looks_like_source_fragment(" ".join(pieces[:-1]))

    def _civitai_title_character_candidates(self, title: str) -> list:
        text = str(title or "").strip()
        if not text:
            return []
        text = re.sub(r"^[\s【\[][^\]】]{0,80}[\]】]\s*", "", text)
        text = self._strip_civitai_title_descriptors(text)
        text = re.sub(r"\s+", " ", text).strip(" -_|｜丨／/⁄:：")
        candidates = []
        for part_index, part in enumerate(re.split(r"\s*(?:[|｜丨／/⁄]|\s+-\s+|\s+—\s+|\s+–\s+)\s*", text)):
            part = part.strip(" -_|｜丨／/⁄:：")
            if not part:
                continue
            if re.fullmatch(r"(?:[0-9A-Za-z]+)?[―—].+[―—]", part.strip()):
                continue
            if candidates and part_index > 0:
                if self._civitai_alias_looks_source_prefixed(part):
                    pass
                elif self._civitai_title_looks_like_source_fragment(part):
                    continue
                elif re.search(r"[㐀-鿿぀-ヿ]", part):
                    pass
                else:
                    continue
            subparts = [x.strip(" -_/\\／⁄:：") for x in re.split(r"\s*[／/⁄]\s*", part) if x.strip()]
            for sub in subparts or [part]:
                candidates.extend(self._civitai_title_part_character_candidates(sub, original_title=title))
        return self._dedupe_profile_terms(candidates, original_title=title)

    def _is_bad_civitai_profile_alias(self, value: str, original_title: str = "") -> bool:
        text = str(value or "").strip()
        if not text or self._is_generic_profile_match_term(text):
            return True
        lowered = text.lower()
        if self._is_lora_profile_bad_match_term(text):
            return True
        if self._lora_search_term_is_source_only(text):
            return True
        if any(word in lowered for word in ("illustrious", "noob", "pony", "sdxl", "lora", "locon", "lycoris")):
            return True
        if text == str(original_title or "").strip() and re.search(r"[|｜丨／/⁄]|\s+-\s+|\s+—\s+|\s+–\s+", text):
            return True
        return len(text) > 80

    def _dedupe_profile_terms(self, values: list, original_title: str = "") -> list:
        terms = []
        seen = set()
        title_norm = self._normalize_profile_match_key(original_title)
        for item in values or []:
            item = str(item or "").strip()
            if not item or self._is_generic_profile_match_term(item) or self._is_lora_profile_bad_match_term(item):
                continue
            marker = self._normalize_profile_match_key(item)
            if not marker or marker in seen:
                continue
            if title_norm and marker == title_norm and len(marker) > 16:
                continue
            seen.add(marker)
            terms.append(item)
        return terms

    def _is_lora_profile_bad_match_term(self, term: str) -> bool:
        text = str(term or "").strip()
        if not text:
            return True
        lowered = text.lower()
        compact = self._normalize_profile_match_key(text)
        if self._is_civitai_page_junk_trigger(text):
            return True
        if re.search(r"(?:^|[\s_\-])(?:illu|illuxl|ixl|sdxl|xl|pony|noob|lora|locon|lycoris)(?:$|[\s_\-])", lowered):
            return True
        if re.search(r"(?:illu|illuxl|ixl|sdxl|pony|noob|lora)$", compact):
            return True
        if re.search(r"\[[^\]]*(?:outfits?|costumes?|clothes|version|v\d|illu|sdxl|lora)[^\]]*\]", lowered):
            return True
        if self._is_lora_quality_or_scene_tag(text):
            return True
        if self._is_lora_outfit_tag(text) or self._is_lora_appearance_tag(text):
            return True
        return False

    def _is_civitai_page_junk_trigger(self, tag: str) -> bool:
        text = str(tag or '').strip().strip('`"\'“”')
        if not text:
            return True
        lowered = text.lower()
        compact = self._normalize_profile_match_key(lowered)
        if lowered in {
            "air", "none", "null", "undefined", "n/a", "na",
            "trigger words", "trained words", "prompt", "lora prompt",
        }:
            return True
        if re.fullmatch(r"civitai:\d+@\d+", lowered):
            return True
        if re.search(r"https?://|www\.|civitai\.com|huggingface\.co", lowered):
            return True
        if "?" in text or "？" in text:
            return True
        if re.search(
            r"\b(?:what did you think|resource|review|comment|discussion|download|downloads|"
            r"creator|username|posted|published|model version|base model|trained words|trigger words|hash)\b",
            lowered,
        ):
            return True
        if len(text) > 72:
            return True
        ascii_words = re.findall(r"[A-Za-z][A-Za-z0-9']*", text)
        if len(ascii_words) > 6:
            return True
        if compact and re.fullmatch(r"(?:v|ver|version)?\d+(?:\d+|\.\d+)*", compact):
            return True
        return False

    def _is_lora_quality_or_scene_tag(self, tag: str) -> bool:
        text = str(tag or '').strip().lower()
        if not text:
            return True
        quality_words = {
            'masterpiece', 'best quality', 'ultra-detailed', 'ultra detailed', 'highres',
            '8k', '4k', 'sharp focus', 'anime style', 'cinematic composition',
            'detailed shading', 'vibrant colors', 'soft lighting', 'warm lighting',
            'atmospheric', 'atmospheric depth', 'background', 'cafe', 'wooden tables',
            'coffee cups', 'standing', 'sitting', 'looking at viewer', 'smile',
        }
        return any(word in text for word in quality_words)

    def _is_lora_outfit_tag(self, tag: str) -> bool:
        text = str(tag or '').strip().lower()
        outfit_words = {
            'outfit', 'clothes', 'costume', 'alternate costume', 'official alternate costume',
            'dress', 'robe', 'uniform', 'maid', 'apron', 'skirt',
            'shirt', 'jacket', 'coat', 'pants', 'shorts', 'sleeves', 'sleeve', 'gloves',
            'boots', 'shoes', 'sandals', 'stockings', 'thighhighs', 'pantyhose',
            'socks', 'sash', 'sashes', 'belt', 'top', 'crop top', 'midriff', 'navel',
            'bare shoulders', 'open jacket', 'open clothes', 'bra', 'bikini',
            'swimsuit', 'kimono', 'armor', 'hood', 'animal hood', 'hoodie', 'sweater', 'neckwear',
            'ascot', 'necktie', 'tie', 'collar', 'cape', 'capelet', 'cloak',
            'scarf', 'shawl', 'detached sleeves', 'wide sleeves', 'puffy sleeves',
            'sleeveless', 'frilled sleeves', 'ribbon-trimmed sleeves', 'trimmed sleeves',
            'mary janes', 'loafers', 'heels', 'pumps', 'sneakers', 'legwear',
        }
        return any(word in text for word in outfit_words)

    def _is_lora_appearance_tag(self, tag: str) -> bool:
        text = str(tag or '').strip().lower()
        appearance_words = {
            'hair', 'eyes', 'eye', 'face', 'features', 'skin', 'mole', 'eyelashes',
            'horns', 'ears', 'tail', 'wings', 'ahoge', 'ponytail', 'twintails',
            'braid', 'bangs', 'sidelocks', 'ribbons', 'ribbon', 'accessories',
            'accessory', 'ornament', 'earrings', 'earring', 'jewelry', 'necklace',
            'flower-shaped', 'flowers', 'flower', 'tassels', 'tassel',
        }
        return any(word in text for word in appearance_words)

    # ------------------------------------------------------------------ 相关度/评分
    def _civitai_candidate_relevance(self, item: dict, search_terms: list) -> int:
        haystack = " ".join([
            str(item.get('name') or ''),
            str(item.get('version_name') or ''),
            str(item.get('base_model') or ''),
            " ".join([str(x) for x in (item.get('trained_words') or [])]),
            " ".join([str(x) for x in (item.get('tags') or [])]),
        ]).lower()
        best = 0
        weak_context = {
            "genshin", "impact", "arknights", "endfield", "honkai", "star",
            "rail", "zenless", "zone", "zero", "project", "game",
        }
        for term in search_terms:
            norm = str(term or '').strip().lower()
            if not norm:
                continue
            cjk_chunks = re.findall(r'[㐀-鿿぀-ヿ]+', norm)
            if cjk_chunks:
                cjk_hit = False
                for chunk in cjk_chunks:
                    if len(chunk) >= 2 and chunk in haystack:
                        cjk_hit = True
                        best = max(best, len(chunk))
                if cjk_hit:
                    continue
                if all(len(chunk) < 2 for chunk in cjk_chunks):
                    continue
            parts = self._civitai_meaningful_parts(norm)
            if not parts:
                continue
            if norm and norm in haystack:
                best = max(best, len(parts) + 2)
            matched = [part for part in parts if part in haystack]
            strong_matched = [part for part in matched if part not in weak_context]
            if strong_matched:
                best = max(best, len(strong_matched))
            elif matched and matched[0] == parts[0] and matched[0] not in weak_context:
                best = max(best, len(matched))
            elif len(matched) >= 2:
                best = max(best, len(matched))
        return best

    def _civitai_cjk_title_match_relevance(self, item: dict, term: str) -> int:
        term = str(term or "").strip()
        if not re.search(r"[㐀-鿿぀-ヿ]", term):
            return 0
        chunks = [
            chunk for chunk in re.findall(r"[㐀-鿿぀-ヿ]+", term)
            if len(self._normalize_profile_match_key(chunk)) >= 2
        ]
        if not chunks:
            return 0
        title_key = self._normalize_profile_match_key(" ".join([
            str((item or {}).get("name") or ""),
            str((item or {}).get("version_name") or ""),
        ]))
        if not title_key:
            return 0
        matched = [
            chunk for chunk in chunks
            if self._normalize_profile_match_key(chunk) in title_key
        ]
        if len(matched) != len(chunks):
            return 0
        return max(4, sum(len(self._normalize_profile_match_key(chunk)) for chunk in matched))

    def _civitai_count(self, item: dict, key: str) -> int:
        try:
            return max(0, int(float((item or {}).get(key) or 0)))
        except Exception:
            return 0

    def _civitai_popularity_score(self, hearts: int, downloads: int, relevance: int = 1, source_index: int = 0) -> float:
        heart_part = 65.0 * math.log10(max(int(hearts or 0), 0) + 1)
        download_part = 35.0 * math.log10(max(int(downloads or 0), 0) + 1)
        relevance_factor = 1.0 + min(max(int(relevance or 0), 0), 6) * 0.12
        source_penalty = max(int(source_index or 0), 0) * 1.5
        return round((heart_part + download_part) * relevance_factor - source_penalty, 2)

    def _is_civitai_search_candidate_allowed(self, item: dict) -> bool:
        name = str(item.get('name') or '').lower()
        blocked = {
            "controlnet", "control net", "openpose", "depth", "sdpose", "sam2", "sam3",
            "segment", "segmentation", "bbox", "yolo", "detection", "preprocessor",
            "lllite", "t2i adapter", "adapter",
        }
        return not any(marker in name for marker in blocked)

    def _civitai_candidate_source_haystack(self, item: dict) -> str:
        return " ".join([
            str((item or {}).get("name") or ""),
            str((item or {}).get("version_name") or ""),
            str((item or {}).get("model_name") or ""),
            " ".join(str(x) for x in ((item or {}).get("trained_words") or [])),
            " ".join(str(x) for x in ((item or {}).get("tags") or [])),
        ])

    def _civitai_item_matches_query_source(self, item: dict, query_source_matches: list, haystack: str = None) -> bool:
        if not query_source_matches:
            return False
        haystack = haystack if haystack is not None else self._civitai_candidate_source_haystack(item)
        for source_match in query_source_matches:
            for term in source_match.get("terms") or []:
                if self._lora_source_alias_term_in_text(haystack, term):
                    return True
        return False

    def _civitai_item_has_conflicting_source(self, item: dict, query_source_matches: list, haystack: str = None) -> bool:
        if not query_source_matches:
            return False
        haystack = haystack if haystack is not None else self._civitai_candidate_source_haystack(item)
        item_sources = self._lora_source_alias_matches_in_text(haystack, limit=4)
        if not item_sources:
            return False
        for item_source in item_sources:
            if not any(self._lora_source_alias_matches_related(item_source, query_source) for query_source in query_source_matches):
                return True
        return False

    def _score_civitai_candidate(self, item: dict, search_terms: list, source_index: int) -> float:
        hearts = self._civitai_count(item, "heart_count")
        downloads = self._civitai_count(item, "download_count")
        relevance = int(item.get('_relevance') or self._civitai_candidate_relevance(item, search_terms) or 0)
        score = self._civitai_popularity_score(hearts, downloads, relevance=relevance, source_index=source_index)
        name_lower = str(item.get('name') or '').lower()
        for term in search_terms or []:
            term_norm = str(term or '').strip().lower()
            if not term_norm:
                continue
            if term_norm in name_lower:
                score += 80
                break
            cjk_text = ''.join(re.findall(r'[㐀-鿿぀-ヿ]+', term_norm))
            if len(cjk_text) >= 2 and cjk_text in name_lower:
                score += 60
                break
        if item.get("_trusted_cjk_title_match"):
            score += 160
        if item.get("_source_context_match"):
            score += 180
        if item.get("_source_context_missing"):
            score -= 45
        return round(score, 2)

    # ------------------------------------------------------------------ LLM 门控/补救
    def _should_expand_lora_search_with_llm(self, query: str, candidates: list, source_list: list, choice_limit: int) -> bool:
        if not self._llm_enabled():
            return False
        query = str(query or "").strip()
        if not query:
            return False
        if not candidates:
            return True
        if len(candidates) >= max(2, min(int(choice_limit or 6), 6)):
            return False
        if re.search(r"[㐀-鿿぀-ヿ]", query):
            return True
        if any(str(item.get("source") or "").lower() == "civitai" for item in candidates):
            return False
        return "civitai" in {str(x or "").lower() for x in (source_list or [])}

    @staticmethod
    def _llm_content(data: dict) -> str:
        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except Exception:
            return ""

    @staticmethod
    def _parse_llm_json_content(content: str):
        text = str(content or "").strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= start:
            text = text[start:end + 1]
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    async def _call_llm_json(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 700) -> dict:
        client = self._resolve_llm()
        if client is None or not self._llm_model:
            return {}
        attempts = [
            (system_prompt, max(128, int(max_tokens or 256))),
            (
                system_prompt
                + "\nReturn exactly one complete minified JSON object. No markdown, no comments, no trailing text.",
                max(int(max_tokens or 256) * 2, 768),
            ),
        ]
        last_content = ""
        for attempt, (attempt_prompt, attempt_tokens) in enumerate(attempts, 1):
            try:
                data = await client.complete(
                    [
                        {"role": "system", "content": attempt_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    model=self._llm_model,
                    max_tokens=attempt_tokens,
                    temperature=0.0,
                )
                content = self._llm_content(data)
            except Exception as exc:
                logger.info("LoRA 搜索 LLM JSON 请求失败(第 %s 次): %s", attempt, _safe_error(exc))
                continue
            if not content:
                continue
            parsed = self._parse_llm_json_content(content)
            if isinstance(parsed, dict):
                return parsed
            last_content = content
            logger.info("LoRA 搜索 LLM JSON 首响应无效，用更严格提示重试: %s", last_content[:80])
        if last_content:
            logger.info("LoRA 搜索 LLM JSON 解析失败: %s", last_content[:80])
        return {}

    def _normalize_lora_search_plan(self, plan, query: str = "") -> dict:
        if not isinstance(plan, dict):
            return {}
        alias = self._normalize_civitai_search_term(plan.get("alias") or plan.get("character") or "")
        raw_queries = plan.get("queries") or plan.get("keywords") or plan.get("names") or []
        if isinstance(raw_queries, str):
            raw_queries = re.split(r'[,，\n]+', raw_queries)
        character_terms = []
        source_terms = []
        if alias and not self._lora_search_term_is_source_only(alias):
            character_terms.append(alias)
        for value in raw_queries or []:
            if isinstance(value, str):
                term = self._normalize_civitai_search_term(value)
                if not term:
                    continue
                if self._lora_search_term_is_source_only(term):
                    source_terms.append(term)
                else:
                    character_terms.append(term)
        character_terms = self._dedupe_civitai_search_terms(character_terms)[:4]
        source_terms = self._dedupe_civitai_search_terms(source_terms)[:3]
        queries = []
        for character in character_terms[:2]:
            for source in source_terms[:2]:
                queries.append(f"{character} {source}")
        queries.extend(character_terms)
        for character in character_terms[:3]:
            queries.extend(self._lora_search_spelling_variants(character))
        query_key = self._normalize_profile_match_key(query)
        noise_words = {
            "echo", "retinal", "il", "xl", "sd", "ai", "art", "style",
            "anime", "model", "lora", "v1", "v2", "v3", "base", "test",
            "preview", "official", "character", "girl", "boy", "solo",
        }
        noise_keys = {self._normalize_profile_match_key(term) for term in noise_words}
        source_only_keys = self._lora_search_source_only_keys()

        def has_character_signal(term: str) -> bool:
            term_key = self._normalize_profile_match_key(term)
            if not term_key or term_key in noise_keys:
                return False
            parts = [self._normalize_profile_match_key(part) for part in self._civitai_meaningful_parts(term)]
            parts = [part for part in parts if part]
            return not (parts and all(part in noise_keys or part in source_only_keys for part in parts))

        if self._normalize_profile_match_key(alias) in noise_keys or len(alias.strip()) < 2:
            alias = ""
        queries = [
            term for term in self._dedupe_civitai_search_terms(queries)
            if not self._lora_search_term_is_source_only(term)
            and self._normalize_profile_match_key(term) != query_key
            and has_character_signal(term)
        ][:6]
        if not queries and character_terms:
            queries = [
                term for term in character_terms
                if has_character_signal(term)
            ][:4]
        return {
            "alias": alias if not self._lora_search_term_is_source_only(alias) else "",
            "queries": queries,
        }

    def _lora_search_plan_has_expansion(self, query: str, plan: dict) -> bool:
        if not isinstance(plan, dict):
            return False
        query_key = self._normalize_profile_match_key(query)
        for term in plan.get("queries") or []:
            term_key = self._normalize_profile_match_key(term)
            if term_key and term_key != query_key:
                return True
        alias_key = self._normalize_profile_match_key(plan.get("alias") or "")
        return bool(alias_key and alias_key != query_key)

    def _evict_cache_if_needed(self, cache: dict, max_size: int = None) -> None:
        if not isinstance(cache, dict):
            return
        max_size = int(max_size or _LLM_CACHE_MAX)
        if len(cache) <= max_size:
            return
        items = sorted(
            cache.items(),
            key=lambda item: float(((item[1] or {}) if isinstance(item[1], dict) else {}).get("time") or 0),
        )
        for key, _value in items[:max(1, len(items) // 2)]:
            cache.pop(key, None)

    async def _llm_civitai_search_terms(self, query: str, family: str, sources=None) -> dict:
        if not self._llm_enabled():
            return {}
        query = self._normalize_civitai_search_term(query)
        if not query:
            return {}
        source_list = self._parse_lora_search_sources(sources if sources is not None else _DEFAULT_SOURCES)
        local_seed_terms = self._local_lora_search_seed_terms(query)
        cache_key = hashlib.sha256(
            json.dumps(
                {
                    "planner_version": 11,
                    "family": family,
                    "query": query,
                    "local_seed_terms": local_seed_terms,
                    "matched_source_aliases": self._lora_source_aliases_for_llm_context(query),
                    "base_models": self._current_civitai_base_models(family),
                    "sources": source_list,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if _LLM_CACHE_TTL > 0:
            cached = self._lora_search_llm_cache.get(cache_key)
            if cached and time.time() - float(cached.get("time") or 0) <= _LLM_CACHE_TTL:
                plan = self._normalize_lora_search_plan(cached.get("plan"), query)
                if self._lora_search_plan_has_expansion(query, plan):
                    logger.info("LoRA 搜索词 LLM 扩展命中缓存: %s => %s", query, ", ".join(plan.get("queries") or []))
                    return plan
                self._lora_search_llm_cache.pop(cache_key, None)
                logger.info("已丢弃无效 LoRA 搜索词 LLM 缓存: %s", query)
        system_prompt = (
            "You prepare LoRA search keywords for Civitai and Hugging Face. "
            "Given a Chinese/English/Japanese character query, identify who the character is and where they are from. "
            "Treat matched_source_aliases as trusted source mapping when present; do not replace it with a different franchise. "
            "Infer official English name, official romanized name, common aliases, and source/franchise title when known. "
            "If the query is plain language like 'the character X from game/source Y', extract X as the character and Y as source; ignore filler words like game, character, from, inside. "
            "Return concise search queries likely to match LoRA model titles, repo names, tags, or trained words. "
            "Prefer '<English name> <source title>' when the source is clear, plus one bare official/romanized name. "
            "For anime/manga characters, include Japanese official title or kana name when known, plus romaji and English localized title variants. "
            "Never return a franchise/source title alone, such as only 'Wuthering Waves' or only 'Blue Archive'. "
            "For Chinese nicknames, translate them into the likely official name when you are confident; otherwise keep the original CJK query as one search term and leave alias empty. "
            "Use correct official romanization/localized names over literal pinyin guesses when you know them. "
            "If you are not confident about the official English/romanized name, do not guess pinyin-like romanizations; include the original CJK name and source-combined original CJK terms instead. "
            "Example: 岩泽雅美 is Iwasawa Masami from Angel Beats, so include 'Iwasawa Angel Beats' and 'Iwasawa Masami'. "
            "Example: 达尼亚 is Denia from Wuthering Waves, so include 'Denia Wuthering Waves' and 'Denia'. "
            "Example: 爱弥斯 is Aemeath from Wuthering Waves, so include 'Aemeath Wuthering Waves' and 'Aemeath'. "
            "For short ambiguous CJK names, include original CJK name and source-combined CJK terms; add romanized variants only when you are confident they are official/common, not literal guesses. "
            "Do not include style words, body descriptions, clothing requests, quality tags, or generic words like LoRA/anime/model. "
            "Every query must be a complete plain phrase with balanced punctuation; avoid raw parentheses unless both sides are present. "
            "Do not return the original CJK sentence as the only query. For a named character, always provide at least one non-CJK official/romanized query when possible. "
            "Return 3-6 queries when the name is ambiguous, ordered from most likely to least likely. "
            "Return ONLY JSON: {\"alias\": string, \"queries\": string[]}."
        )
        payload = json.dumps(
            {
                "query": query,
                "local_seed_terms": local_seed_terms,
                "matched_source_aliases": self._lora_source_aliases_for_llm_context(query),
                "active_model_family": self._family_label(family),
                "base_models": self._current_civitai_base_models(family),
                "sources": source_list,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await self._call_llm_json(system_prompt, payload, max_tokens=700)
        plan = self._normalize_lora_search_plan(result, query)
        if not self._lora_search_plan_has_expansion(query, plan):
            retry_prompt = (
                "Translate one LoRA character search query into search terms. "
                "Return valid minified JSON only: {\"alias\":\"official English or romanized character name\",\"queries\":[\"character source\",\"character\"]}. "
                "Extract the character name and source from the user's sentence. Ignore words such as game/source/inside/character/LoRA/model. "
                "For Chinese/Japanese names, provide official English/localized/romanized names if known, Japanese kana/title variants if known, plus original CJK. If unsure, keep alias empty and do not guess pinyin-like romanizations. "
                "Return only complete search phrases with balanced punctuation. "
                "Never return a franchise/source title alone. Never return fewer than 3 queries for a named character."
            )
            retry_result = await self._call_llm_json(retry_prompt, payload, max_tokens=700)
            retry_plan = self._normalize_lora_search_plan(retry_result, query)
            if self._lora_search_plan_has_expansion(query, retry_plan):
                plan = retry_plan
            elif local_seed_terms:
                plan = {"alias": "", "queries": local_seed_terms[:4]}
            else:
                logger.info("LoRA 搜索词 LLM 未返回有效扩展: %s", query)
                return {}
        if not self._lora_search_plan_has_expansion(query, plan):
            logger.info("LoRA 搜索词 LLM 未返回有效扩展: %s", query)
            return {}
        self._evict_cache_if_needed(self._lora_search_llm_cache)
        self._lora_search_llm_cache[cache_key] = {"time": time.time(), "plan": plan}
        return plan

    async def _llm_civitai_rescue_search_terms(self, query: str, family: str, searched_terms: list, sources=None) -> list:
        if not self._llm_enabled():
            return []
        query = self._normalize_civitai_search_term(query)
        if not query:
            return []
        source_list = self._parse_lora_search_sources(sources if sources is not None else _DEFAULT_SOURCES)
        searched_keys = {
            self._normalize_profile_match_key(term)
            for term in (searched_terms or [])
            if self._normalize_profile_match_key(term)
        }
        system_prompt = (
            "You are doing a last-resort LoRA search keyword rescue after earlier searches returned no strong relevant candidates. "
            "Return conservative additional search phrases only. Do not reuse failed_terms. "
            "Do not attach a franchise/source unless it is explicitly present in the user query or strongly indicated by matched_source_aliases. "
            "If you are unsure, prefer exact original CJK character/title fragments and source-combined CJK terms over guessed romanization. "
            "Avoid popular unrelated franchises and generic words. Return ONLY minified JSON: {\"queries\": string[]}."
        )
        payload = json.dumps(
            {
                "query": query,
                "failed_terms": list(searched_terms or [])[:12],
                "matched_source_aliases": self._lora_source_aliases_for_llm_context(query),
                "active_model_family": self._family_label(family),
                "base_models": self._current_civitai_base_models(family),
                "sources": source_list,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await self._call_llm_json(system_prompt, payload, max_tokens=500)
        raw_terms = []
        if isinstance(result, dict):
            raw_terms = result.get("queries") or result.get("keywords") or result.get("terms") or []
        if isinstance(raw_terms, str):
            raw_terms = re.split(r"[,，\n]+", raw_terms)
        terms = []
        for term in raw_terms or []:
            term = self._normalize_civitai_search_term(term)
            marker = self._normalize_profile_match_key(term)
            if (
                term
                and marker
                and marker not in searched_keys
                and not self._lora_search_term_is_source_only(term)
            ):
                searched_keys.add(marker)
                terms.append(term)
        return self._dedupe_civitai_search_terms(terms)[:4]


__all__ = ["LoraSearchService"]
