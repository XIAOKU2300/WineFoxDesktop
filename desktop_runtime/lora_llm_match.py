"""LoRA LLM 本地匹配服务（AstrBot-free 移植）。

让 LLM 从本地 LoRA 候选中挑出与用户 prompt 最匹配的一项，带容错解析与磁盘缓存。
prompt 与候选只进内存/LLM，绝不落日志；缓存只存公开安全字段。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .llm_client import OpenAICompatibleLLMClient, resolve_provider

logger = logging.getLogger("winefox.runtime.lora_llm_match")

_MATCH_CACHE_VERSION = 3
_CONFIDENCE_THRESHOLD = 0.92
_CACHE_DIR_NAME = "lora_llm_cache"
_FAMILIES = frozenset({"anima", "illustrious"})

_SYSTEM_PROMPT = (
    "Match a user's image request to the best installed LoRA from the provided candidates. "
    "Use ONLY the provided candidates. Match ONLY when the user explicitly names that character, one of its aliases, a search_alias from a previously selected import, a trigger word, or a clear romanization/translation of a provided name. "
    "Do NOT infer from franchise/source words, art style, gender tags, generic visual tags, or the fact that a candidate exists. "
    "If the request mentions a different character that is not in the candidates, return no match. "
    "Return ONLY JSON: {\"key\": string, \"confidence\": number, \"matched_text\": string, \"reason\": string}. "
    "key must be the exact key of one candidate. "
    "matched_text must be the exact phrase from the user prompt that names the character. "
    "If no candidate is explicitly mentioned, return {\"key\": \"\", \"confidence\": 0, \"matched_text\": \"\", \"reason\": \"\"}."
)

_GENERIC_TERMS = frozenset({
    "1girl", "1boy", "solo", "girl", "boy", "male", "female", "character",
    "masterpiece", "bestquality", "highquality", "anime", "animeillustration",
    "blueeyes", "redeyes", "blackhair", "whitehair", "longhair", "shorthair",
    "blondehair", "silverhair", "lookingatviewer", "smile", "standing",
    "fullbody", "upperbody", "portrait", "official", "preview", "base", "v1",
    "v2", "illustrious", "anima", "pony", "sdxl", "lora", "locon", "lycoris",
    "cute", "kawaii", "loli", "shota", "萝莉", "少女", "少年",
})


def _bounded_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _safe_error(value: Any) -> str:
    text = str(value or "LoRA 匹配失败")
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


def _safe_text(value: Any, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if re.search(r"(?i)https?://|www\.|ftp://|file://|token=|api[_-]?key|authorization|cookie", text):
        return ""
    if re.search(r"(?i)(?:[a-z]:[\\/]|\\\\|^[\\/]|\.\.|//)", text):
        return ""
    return text[:limit]


class LoraLlmMatchService:
    """本地 LoRA 候选的 LLM 匹配服务。"""

    def __init__(
        self,
        *,
        config: Any,
        data_root: Path,
        llm_client_factory: Callable[..., Any] | None = None,
        max_cache_entries: int = 512,
        confidence_threshold: float = _CONFIDENCE_THRESHOLD,
    ) -> None:
        self.config = config
        self.data_root = Path(data_root)
        self.llm_client_factory = llm_client_factory or OpenAICompatibleLLMClient
        self.max_cache_entries = max(1, int(max_cache_entries or 512))
        self.confidence_threshold = max(0.0, min(float(confidence_threshold), 1.0))
        self._llm_client_cache: Any = None
        self._llm_model = ""
        self._llm_checked = False
        self._cache_lock = asyncio.Lock()

    # ------------------------------------------------------------------ LLM 配置
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
            logger.warning("LoRA 匹配 LLM 不可用: %s", _safe_error(exc))
            client = None
        self._llm_client_cache = client
        return client

    def llm_enabled(self) -> bool:
        return self._resolve_llm() is not None

    def invalidate(self) -> None:
        """档案变化后清空磁盘匹配缓存（保守策略，编辑频率低）。"""
        try:
            for path in self._cache_dir().glob("*.json"):
                path.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ 候选净化
    @staticmethod
    def _normalize_family(family: str) -> str:
        value = str(family or "illustrious").strip().lower()
        return value if value in _FAMILIES else "illustrious"

    def _normalize_profile_match_key(self, value: str) -> str:
        return _cached_normalize_profile_match_key(str(value or ""))

    def _is_generic_match_term(self, term: str) -> bool:
        raw = str(term or "").strip().lower()
        norm = self._normalize_profile_match_key(raw)
        if not norm:
            return True
        if norm in _GENERIC_TERMS:
            return True
        if re.fullmatch(r"\d+(girl|boy|girls|boys|person|people)", norm):
            return True
        if re.fullmatch(r"(black|white|blue|red|green|gold|blonde|silver|pink|purple|brown)(hair|eyes)", norm):
            return True
        has_cjk = re.search(r"[぀-ヿ㐀-鿿]", raw)
        if has_cjk:
            return len(norm) < 2
        return len(norm) < 4

    def _candidate_public_payload(self, candidate: dict) -> dict:
        if not isinstance(candidate, dict):
            return {}

        def safe_list(values: Any, limit: int = 80) -> list:
            out = []
            for value in values or []:
                safe = _safe_text(value, limit)
                if safe:
                    out.append(safe)
            return out

        return {
            "character": _safe_text(candidate.get("character") or candidate.get("name") or "", 120),
            "name": _safe_text(candidate.get("name") or candidate.get("character") or "", 120),
            "title": _safe_text(candidate.get("title") or "", 120),
            "aliases": safe_list(candidate.get("aliases")),
            "match_terms": safe_list(candidate.get("match_terms")),
            "search_aliases": safe_list(candidate.get("search_aliases")),
            "trigger_words": safe_list(candidate.get("trigger_words") or candidate.get("trained_words")),
            "tags": safe_list(candidate.get("tags")),
            "lora": _safe_text(candidate.get("lora") or candidate.get("lora_name") or "", 120),
        }

    def _candidate_fingerprint(self, candidate: dict) -> str:
        payload = self._candidate_public_payload(candidate)
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _candidate_stable_key(self, candidate: dict, fingerprint: str) -> str:
        if not isinstance(candidate, dict):
            return fingerprint
        raw = str(candidate.get("key") or candidate.get("id") or "").strip()
        safe = _safe_text(raw, 120)
        return safe or fingerprint

    def _public_candidate(self, candidate: dict) -> dict:
        payload = self._candidate_public_payload(candidate)
        fingerprint = self._candidate_fingerprint(candidate)
        pub = dict(payload)
        pub["key"] = self._candidate_stable_key(candidate, fingerprint)
        pub["fingerprint"] = fingerprint
        return pub

    def _find_candidate(self, candidates: list, public_candidates: list, result_key: str):
        key = str(result_key or "").strip()
        if not key:
            return None
        for cand, pub in zip(candidates, public_candidates):
            if key == pub.get("key") or key == pub.get("fingerprint"):
                return cand
        return None

    # ------------------------------------------------------------------ LLM 调用
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

    async def _call_llm_json(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 140) -> dict:
        client = self._resolve_llm()
        if client is None or not self._llm_model:
            return {}
        attempts = [
            (system_prompt, max(128, int(max_tokens or 140))),
            (
                system_prompt
                + "\nReturn exactly one complete minified JSON object. No markdown, no comments, no trailing text.",
                max(int(max_tokens or 140) * 2, 256),
            ),
        ]
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
            except asyncio.TimeoutError:
                logger.info("LoRA 匹配 LLM 请求超时(第 %s 次)", attempt)
                continue
            except Exception as exc:
                logger.info("LoRA 匹配 LLM 请求失败(第 %s 次): %s", attempt, _safe_error(exc))
                continue
            if not content:
                continue
            parsed = self._parse_llm_json_content(content)
            if isinstance(parsed, dict):
                return parsed
        return {}

    # ------------------------------------------------------------------ 磁盘缓存
    def _cache_dir(self) -> Path:
        return self.data_root / _CACHE_DIR_NAME

    def _cache_path(self, key: str) -> Path:
        return self._cache_dir() / f"{key}.json"

    def _cache_key(self, prompt: str, family: str, fingerprints: list, revision: int = 0) -> str:
        raw = json.dumps(
            {
                "matcher_version": _MATCH_CACHE_VERSION,
                "family": family,
                "revision": int(revision or 0),
                "prompt": prompt,
                "fingerprints": sorted(fingerprints),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _read_cache(self, key: str) -> dict | None:
        path = self._cache_path(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or int(data.get("version") or 0) != _MATCH_CACHE_VERSION:
            return None
        return data

    def _write_cache(self, key: str, payload: dict) -> None:
        try:
            directory = self._cache_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = self._cache_path(key)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("LoRA 匹配缓存写入失败: %s", type(exc).__name__)
            return
        self._evict_oldest()

    def _evict_oldest(self) -> None:
        try:
            files = [p for p in self._cache_dir().glob("*.json")]
        except OSError:
            return
        if len(files) <= self.max_cache_entries:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for stale in files[: len(files) - self.max_cache_entries]:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ 匹配校验
    def _accept_matched_text(self, prompt: str, matched_text: str, candidate: dict) -> bool:
        marker = self._normalize_profile_match_key(matched_text)
        if not marker or self._is_generic_match_term(matched_text):
            return False
        prompt_marker = self._normalize_profile_match_key(prompt)
        if marker in prompt_marker:
            return True
        for term in self._candidate_prompt_terms(candidate):
            term_marker = self._normalize_profile_match_key(term)
            if term_marker and term_marker in prompt_marker:
                return True
        return False

    @staticmethod
    def _candidate_prompt_terms(candidate: dict) -> list:
        if not isinstance(candidate, dict):
            return []
        terms = [
            candidate.get("key"), candidate.get("id"), candidate.get("character"),
            candidate.get("name"), candidate.get("title"),
            candidate.get("lora"), candidate.get("lora_name"),
        ]
        for list_key in ("aliases", "match_terms", "search_aliases",
                         "trigger_words", "trained_words", "tags"):
            terms.extend(candidate.get(list_key) or [])
        return [str(x) for x in terms if str(x or "").strip()]

    async def match(self, *, prompt: str, candidates: list, family: str = "illustrious", limit: int = 5, revision: int = 0) -> dict | None:
        """LLM 匹配最贴近 prompt 的本地 LoRA 候选，返回公开结果。

        revision 为当前 family 档案聚合版本，纳入缓存键；档案被编辑后
        即使候选内容未变也不会命中旧缓存。
        """
        prompt = str(prompt or "").strip()
        family = self._normalize_family(family)
        if not self.llm_enabled():
            return {"matched": None, "reason": "LLM 未配置", "used_cache": False}
        if not prompt:
            return {"matched": None, "reason": "prompt 为空", "used_cache": False}
        if not candidates:
            return {"matched": None, "reason": "无候选", "used_cache": False}
        public_candidates = [self._public_candidate(c) for c in candidates[:limit]]
        if not public_candidates:
            return {"matched": None, "reason": "候选净化后为空", "used_cache": False}
        fingerprints = [p.get("fingerprint") or "" for p in public_candidates]
        cache_key = self._cache_key(prompt, family, fingerprints, revision)

        async with self._cache_lock:
            cached = self._read_cache(cache_key)
            if cached is not None:
                matched_key = str(cached.get("key") or "").strip()
                if not matched_key:
                    return {"matched": None, "reason": str(cached.get("reason") or "缓存无匹配"), "used_cache": True}
                matched = self._find_candidate(candidates, public_candidates, matched_key)
                if matched is not None:
                    try:
                        cached_confidence = float(cached.get("confidence") or 0)
                    except (TypeError, ValueError):
                        cached_confidence = 0.0
                    return {
                        "matched": matched,
                        "reason": str(cached.get("reason") or "命中缓存"),
                        "confidence": max(0.0, min(cached_confidence, 1.0)),
                        "used_cache": True,
                    }

        payload = json.dumps(
            {"user_prompt": prompt, "candidates": public_candidates},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await self._call_llm_json(_SYSTEM_PROMPT, payload, max_tokens=140)
        result_key = str(result.get("key") or "").strip()
        try:
            confidence = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        matched_text = str(result.get("matched_text") or "").strip()
        llm_reason = _safe_text(result.get("reason"), 200)

        matched = self._find_candidate(candidates, public_candidates, result_key)
        if matched is None or confidence < self.confidence_threshold:
            async with self._cache_lock:
                self._write_cache(cache_key, {
                    "version": _MATCH_CACHE_VERSION,
                    "family": family,
                    "prompt": _safe_text(prompt, 500),
                    "key": "",
                    "reason": "LLM 未匹配到合适候选",
                    "time": time.time(),
                })
            return {"matched": None, "reason": "LLM 未匹配到合适候选", "used_cache": False}

        if not self._accept_matched_text(prompt, matched_text, matched):
            async with self._cache_lock:
                self._write_cache(cache_key, {
                    "version": _MATCH_CACHE_VERSION,
                    "family": family,
                    "prompt": _safe_text(prompt, 500),
                    "key": "",
                    "reason": "LLM 匹配文本未通过校验",
                    "time": time.time(),
                })
            return {"matched": None, "reason": "LLM 匹配文本未通过校验", "used_cache": False}

        matched_key = self._candidate_stable_key(matched, self._candidate_fingerprint(matched))
        reason = llm_reason or f"LLM 匹配到 {matched_key}"
        async with self._cache_lock:
            self._write_cache(cache_key, {
                "version": _MATCH_CACHE_VERSION,
                "family": family,
                "prompt": _safe_text(prompt, 500),
                "key": matched_key,
                "reason": reason,
                "confidence": max(0.0, min(confidence, 1.0)),
                "time": time.time(),
            })
        logger.info("LoRA LLM 匹配: %s confidence=%.2f", matched_key, confidence)
        return {
            "matched": matched,
            "reason": reason,
            "confidence": max(0.0, min(confidence, 1.0)),
            "used_cache": False,
        }


__all__ = ["LoraLlmMatchService"]
