# -*- coding: utf-8 -*-
"""PromptService —— 平台无关的提示词处理服务（WineFox Desktop）。

从 AstrBot 插件 main.py 提炼：种子解析、reasoning 污染清理、LLM 优化/翻译、
确定性 fallback、负向提示词与匹配信息。不依赖 AstrBot / HTTP / 数据库。

- raw_prompt 永远原样保留在结果中；
- LLM 通过构造注入的 ``LLMPort`` 调用（见 winefox_core._agent_stubs）；
- 未配置 LLM / 端口失败 / 空输出 / 无效输出 → 确定性 fallback；
- 提取 ``seed=`` / ``--seed`` / ``种子：`` 并从提示词中移除。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

# ---- 缺接口兜底：优先使用未提交的 _agent_stubs，缺失时使用内置同构实现 ----
try:  # pragma: no cover - 取决于工作区是否包含未提交桩文件
    from ._agent_stubs import LLMPort, LLMPortError, extract_llm_text
except Exception:  # pragma: no cover
    class LLMPortError(RuntimeError):  # type: ignore[no-redef]
        pass

    class LLMPort:  # type: ignore[no-redef]
        """占位协议：complete(messages, **options) -> dict"""

        async def complete(self, messages, **options):  # pragma: no cover
            raise LLMPortError("LLM port unavailable")

    def extract_llm_text(data) -> str:  # type: ignore[no-redef]
        parts = []

        def collect(value):
            if value is None:
                return
            if isinstance(value, str):
                if value.strip():
                    parts.append(value)
                return
            if isinstance(value, list):
                for item in value:
                    collect(item)
                return
            if isinstance(value, dict):
                for key in ("text", "content", "output_text"):
                    if key in value:
                        collect(value.get(key))

        if isinstance(data, dict):
            for choice in data.get("choices") or []:
                if isinstance(choice, dict):
                    collect((choice.get("message") or {}).get("content"))
                    collect((choice.get("delta") or {}).get("content"))
                    collect(choice.get("text"))
            collect(data.get("output_text"))
            collect(data.get("text"))
        return "\n".join(part for part in parts if str(part).strip()).strip()


# ---------------------------------------------------------------------------
# 常量（从 main.py / intent_utils.py 提炼，保持确定性）
# ---------------------------------------------------------------------------

GENERATION_SEED_LIMIT = 999999999999999

_SEED_PATTERNS = (
    r"(?i)(?:^|[\s,，;；])(?:--seed|seed)\s*[:=]?\s*(\d{1,18})(?=$|[\s,，;；])",
    r"(?:^|[\s,，;；])(?:随机种子|固定种子|种子)\s*[:：=]?\s*(\d{1,18})(?=$|[\s,，;；])",
)

#: reasoning / 思维链块
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_BLOCK_ALT_RE = re.compile(r"<thinking>.*?</thinking>", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

#: LLM 输出的自述/reasoning 小节标题（来自 main._strip_visual_reasoning_blocks）
_REASONING_HEADING_RE = re.compile(
    r"(\*\*\s*(?:defining|analyzing|refining|integrating|detailing|incorporating|"
    r"summarizing|finalizing|objective|scope|focus)[^*\n]{0,120}\*\*)",
    re.I,
)

_REFUSAL_PATTERNS = (
    r"\b(?:sorry|i\s+(?:can'?t|cannot|am\s+unable)\b)",
    r"作为\s*(?:ai|人工智能)|抱歉[，,]?\s*(?:我)?(?:无法|不能|不)",
    r"^\s*(?:ok(?:ay)?|好的)[，,。]?\s*$",
)

#: 拒答文本通常很短且不是标签列表；含 >=2 个逗号分隔标签则不视为拒答
_REFUSAL_MAX_LENGTH = 80

_DEFAULT_NEGATIVE_ILLUSTRIOUS = (
    "worst quality, low quality, normal quality, lowres, blurry, bad anatomy, bad hands, "
    "extra fingers, missing fingers, deformed, mutated, text, watermark, signature, jpeg artifacts"
)

_DEFAULT_NEGATIVE_ANIMA = (
    "worst quality, low quality, normal quality, lowres, blurry, bad anatomy, bad hands, "
    "extra fingers, missing fingers, deformed, mutated, text, watermark, signature, jpeg artifacts"
)

_TRANSLATE_SYSTEM_PROMPT = (
    "You are a precise Stable Diffusion prompt translator. "
    "The user request may be written in Chinese, English, or mixed; always understand it fully. "
    "Convert it into concise English comma-separated Danbooru-style tags. "
    "Every element the user explicitly described — character identity, hair, outfit, pose, "
    "expression, framing, camera angle, background, lighting and mood — MUST appear in the tags. "
    "Do not invent content that is not in the request, do not drop details, and do not change "
    "the art style the user asked for. "
    "Output only the final English prompt tags on a single line; "
    "no explanations, markdown, JSON, reasoning notes, or multiple lines."
)


@dataclass
class PromptMatch:
    """提示词匹配信息（角色 / LoRA / 意图等，由注入的 matcher 提供）。"""

    kind: str
    value: str
    score: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptResult:
    """PromptService 处理结果。raw_prompt 原样保留。"""

    raw_prompt: str
    cleaned_prompt: str
    final_prompt: str
    negative_prompt: str
    seed: Optional[int]
    matches: list[PromptMatch] = field(default_factory=list)
    source: str = "fallback"  # "llm" | "fallback"
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_prompt": self.raw_prompt,
            "cleaned_prompt": self.cleaned_prompt,
            "final_prompt": self.final_prompt,
            "negative_prompt": self.negative_prompt,
            "seed": self.seed,
            "matches": [m.__dict__ for m in self.matches],
            "source": self.source,
            "warnings": list(self.warnings),
            "meta": dict(self.meta),
        }


# ---------------------------------------------------------------------------
# 纯函数工具（平台无关、确定性）
# ---------------------------------------------------------------------------


def normalize_seed(value: Any) -> int:
    """归一化种子：非法 / 负数归 0，并截断到上限。"""
    try:
        if value is None:
            return 0
        seed = int(str(value).strip())
    except Exception:
        return 0
    if seed < 0:
        return 0
    return min(seed, GENERATION_SEED_LIMIT)


def extract_seed(text: str) -> tuple[str, Optional[int]]:
    """解析 ``seed=123`` / ``--seed 123`` / ``种子：123`` 并把它从提示词中移除。

    :returns: (移除 seed 后的文本, seed 或 None)
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return "", None
    seed: Optional[int] = None

    def remove_match(match: re.Match) -> str:
        nonlocal seed
        try:
            seed = normalize_seed(match.group(1))
        except Exception:
            seed = None
        raw = match.group(0)
        if raw[:1] in {",", "，", ";", "；"}:
            return " "
        return raw[: len(raw) - len(raw.lstrip())]

    for pattern in _SEED_PATTERNS:
        cleaned, count = re.subn(pattern, remove_match, cleaned, count=1)
        if count:
            break
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,，;；")
    return cleaned, seed


def strip_reasoning_content(text: str) -> str:
    """剥离 LLM 输出的思维链 / reasoning 污染（<think>、HTML 注释、自述小节）。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    # <think>...</think> / <thinking>...</thinking> / HTML 注释
    cleaned = _THINK_BLOCK_RE.sub("", cleaned)
    cleaned = _THINK_BLOCK_ALT_RE.sub("", cleaned)
    cleaned = _HTML_COMMENT_RE.sub(", ", cleaned)

    # "**analyzing ...**" 类自述小节：若前缀已 >=4 个标签则截断，否则删除该小节
    match = _REASONING_HEADING_RE.search(cleaned)
    if match:
        prefix = cleaned[: match.start()].strip(" ,\n\r\t")
        if len([p for p in prefix.split(",") if p.strip()]) >= 4:
            cleaned = prefix
        else:
            cleaned = _REASONING_HEADING_RE.sub(", ", cleaned)

    return re.sub(r"[ \t]+", " ", cleaned).strip(" ,\n\r\t")


def default_negative_prompt(family: str = "illustrious") -> str:
    """按模型族返回确定性负向提示词。"""
    family = str(family or "").strip().lower()
    if family in {"anima", "aura", "auraflow"}:
        return _DEFAULT_NEGATIVE_ANIMA
    return _DEFAULT_NEGATIVE_ILLUSTRIOUS


def dedupe_tags(text: str, sep: str = ",") -> str:
    """按出现顺序去重重复标签（确定性 fallback 的轻量增强）。"""
    tags = [tag.strip() for tag in str(text or "").split(sep)]
    seen: set[str] = set()
    kept: list[str] = []
    for tag in tags:
        if not tag:
            continue
        norm = tag.lower()
        if norm in seen:
            continue
        seen.add(norm)
        kept.append(tag)
    return sep.join(kept)


def validate_llm_output(text: str) -> tuple[bool, str]:
    """校验 LLM 输出是否可接受：非空、非拒答、无元文本、长度合理。"""
    cleaned = strip_reasoning_content(text)
    if not cleaned:
        return False, "empty output"
    if len(cleaned) > 4096:
        return False, "output too long"
    tag_count = len([tag for tag in cleaned.split(",") if tag.strip()])
    is_refusal = bool(
        re.search(_REFUSAL_PATTERNS[0], cleaned, re.I)
        or re.search(_REFUSAL_PATTERNS[1], cleaned)
    )
    if is_refusal and (len(cleaned) <= _REFUSAL_MAX_LENGTH or tag_count < 2):
        return False, "refusal-like output"
    if re.search(r"\b(json|markdown|```|```json)\b", cleaned, re.I) and re.search(r"\{|\[", cleaned):
        return False, "meta/json output"
    if re.match(r"^(ok|好的|done|完成|没问题|no problem)\s*[。.！!]?\s*$", cleaned, re.I):
        return False, "meta acknowledgement"
    return True, "ok"


# ---------------------------------------------------------------------------
# PromptService
# ---------------------------------------------------------------------------

#: 匹配回调：raw/cleaned -> 匹配信息列表（由 LoraService 等注入）
MatcherCallable = Callable[[str, str], list[PromptMatch] | list[dict[str, Any]]]
LLMCallable = Callable[..., Awaitable[Any]]


class PromptService:
    """平台无关的提示词处理管线。

    :param config: 纯字典配置，支持键：
        - ``family``: "anima" | "illustrious"（决定默认负向提示词）
        - ``negative_prompt``: 自定义负向提示词（优先于默认值）
        - ``llm.enabled``: 是否启用 LLM 优化
        - ``llm.model`` / ``llm.max_tokens`` / ``llm.temperature``
        - ``llm.system_prompt``: 自定义 system prompt（缺省用翻译模板）
    :param llm_port: 注入的 LLM 端口（未提供则永远走确定性 fallback）
    :param matcher: 注入的匹配回调，填充结果中的 matches（如 LoraService.match）
    """

    def __init__(
        self,
        config: Optional[dict[str, Any]] = None,
        llm_port: Optional[LLMCallable] = None,
        matcher: Optional[MatcherCallable] = None,
    ) -> None:
        self.config = dict(config or {})
        self.llm_port = llm_port
        self.matcher = matcher

    # -- 配置读取 -----------------------------------------------------------

    @property
    def family(self) -> str:
        return str(self.config.get("family") or "illustrious").strip().lower()

    def _llm_enabled(self) -> bool:
        llm_conf = self.config.get("llm") or {}
        if isinstance(llm_conf, dict):
            return bool(llm_conf.get("enabled", False)) and self.llm_port is not None
        return False

    def _llm_model(self) -> str:
        llm_conf = self.config.get("llm") or {}
        return str(llm_conf.get("model") or "") if isinstance(llm_conf, dict) else ""

    def _llm_max_tokens(self) -> int:
        llm_conf = self.config.get("llm") or {}
        if not isinstance(llm_conf, dict):
            return 450
        try:
            return max(128, min(int(llm_conf.get("max_tokens", 800)), 2000))
        except Exception:
            return 450

    def _llm_temperature(self) -> float:
        llm_conf = self.config.get("llm") or {}
        try:
            return float(llm_conf.get("temperature", 0.1))
        except Exception:
            return 0.1

    # -- 主入口 ---------------------------------------------------------------

    async def process(self, raw_prompt: str) -> PromptResult:
        """处理一条提示词：保留 raw，解析 seed，清理，可选 LLM，确定性 fallback。"""
        raw = str(raw_prompt or "").strip()
        warnings: list[str] = []

        # 1) 解析 seed 并从提示词中移除
        cleaned, seed = extract_seed(raw)

        # 2) 确定性清理（reasoning 污染等）
        cleaned = strip_reasoning_content(cleaned)
        cleaned = self._cleanup_markers(cleaned)

        # 3) 匹配信息（由注入的 matcher 提供，纯业务）
        matches = self._collect_matches(raw, cleaned)

        # 4) 负向提示词：配置优先，否则按模型族默认
        configured_negative = str(self.config.get("negative_prompt") or "").strip()
        negative = configured_negative or default_negative_prompt(self.family)

        # 5) final prompt：LLM 可用则尝试，任何失败都回退
        final_prompt, source = await self._resolve_final_prompt(raw, cleaned, warnings)

        return PromptResult(
            raw_prompt=raw,
            cleaned_prompt=cleaned,
            final_prompt=final_prompt,
            negative_prompt=negative,
            seed=seed,
            matches=matches,
            source=source,
            warnings=warnings,
            meta={
                "family": self.family,
                "llm_enabled": self._llm_enabled(),
            },
        )

    # -- 内部步骤 ---------------------------------------------------------------

    def _cleanup_markers(self, text: str) -> str:
        """移除残留的占位标记（平台迁移符号），不触碰业务内容。"""
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"__ASTRBOT_[A-Z0-9_]+__", "", cleaned)
        return re.sub(r"\s{2,}", " ", cleaned).strip(" ,，;；")

    def _collect_matches(self, raw: str, cleaned: str) -> list[PromptMatch]:
        if self.matcher is None:
            return []
        try:
            items = self.matcher(raw, cleaned) or []
        except Exception as exc:  # matcher 失败不影响主流程
            return [PromptMatch(kind="matcher_error", value=str(exc), score=0.0)]
        matches: list[PromptMatch] = []
        for item in items:
            if isinstance(item, PromptMatch):
                matches.append(item)
            elif isinstance(item, dict):
                matches.append(
                    PromptMatch(
                        kind=str(item.get("kind") or "match"),
                        value=str(item.get("value") or ""),
                        score=float(item.get("score") or 0.0),
                        detail=dict(item.get("detail") or {}),
                    )
                )
        return matches

    async def _resolve_final_prompt(
        self, raw: str, cleaned: str, warnings: list[str]
    ) -> tuple[str, str]:
        """返回 (final_prompt, source)。source ∈ {"llm", "fallback"}。"""
        if not self._llm_enabled():
            return cleaned, "fallback"

        system_prompt = str(self.config.get("llm", {}).get("system_prompt") or "").strip()
        if not system_prompt:
            system_prompt = _TRANSLATE_SYSTEM_PROMPT
        # 纯文本用户消息：JSON 包装会干扰小模型，导致它漏译或复读结构
        request_text = cleaned or raw
        user_content = (
            f"Model family: {self.family}\n"
            f"User request (translate faithfully into English SD tags):\n{request_text}"
        )
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            data = await self.llm_port.complete(
                messages,
                model=self._llm_model(),
                max_tokens=self._llm_max_tokens(),
                temperature=self._llm_temperature(),
            )
            content = extract_llm_text(data)
            ok, reason = validate_llm_output(content)
            if ok:
                return strip_reasoning_content(content), "llm"
            warnings.append(f"LLM 输出无效（{reason}），使用确定性 fallback")
        except LLMPortError as exc:
            warnings.append(f"LLM 端口不可用，使用确定性 fallback: {exc}")
        except Exception as exc:  # noqa: BLE001 - 任何失败都回退
            warnings.append(f"LLM 调用失败，使用确定性 fallback: {type(exc).__name__}")
        if re.search(r"[\u4e00-\u9fff]", cleaned):
            warnings.append(
                "转译失败：中文提示词未翻译，直接出图会导致画面严重偏离描述，请检查 AI 配置"
            )
        return cleaned, "fallback"


# 兼容导出
__all__ = [
    "PromptService",
    "PromptResult",
    "PromptMatch",
    "extract_seed",
    "normalize_seed",
    "strip_reasoning_content",
    "dedupe_tags",
    "default_negative_prompt",
    "validate_llm_output",
    "LLMPort",
    "LLMPortError",
    "extract_llm_text",
]
