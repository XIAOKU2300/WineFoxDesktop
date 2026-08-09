# -*- coding: utf-8 -*-
"""LoraService —— 平台无关的 LoRA 档案匹配服务（WineFox Desktop）。

从 AstrBot 插件 main.py 提炼：名称规范化、别名/角色/触发词匹配、
泛化词过滤、单/多角色、跨模型族临时建议。纯业务，不依赖 AstrBot/HTTP/DB。

关键约定：
- 只有明确证据（exact 别名 / 角色名 / 触发词命中）才匹配，泛化词永不当作别名；
- 跨模型族命中只返回 ``suggestion_only=True`` 的临时建议，不持久修改任何状态；
- 强度一律经过默认值 + 模型族强度上限钳制。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 模型族
# ---------------------------------------------------------------------------

MODEL_FAMILY_ANIMA = "anima"
MODEL_FAMILY_ILLUSTRIOUS = "illustrious"
SUPPORTED_MODEL_FAMILIES = (MODEL_FAMILY_ANIMA, MODEL_FAMILY_ILLUSTRIOUS)

#: 每个模型族的默认强度（model, clip）与强度上限（model, clip）
DEFAULT_STRENGTHS: dict[str, tuple[float, float]] = {
    MODEL_FAMILY_ANIMA: (0.85, 0.85),
    MODEL_FAMILY_ILLUSTRIOUS: (0.75, 0.70),
}
#: Illustrious 画风预设对角色 LoRA 的强度钳制（来自 main.py 的 lora_strength_cap）
DEFAULT_STRENGTH_CAPS: dict[str, dict[str, Optional[float]]] = {
    MODEL_FAMILY_ANIMA: {"model": None, "clip": None},
    MODEL_FAMILY_ILLUSTRIOUS: {"model": 0.82, "clip": 0.72},
}

# ---------------------------------------------------------------------------
# 名称规范化（与 main.py _cached_normalize_* 保持一致，纯函数、确定性）
# ---------------------------------------------------------------------------

_LORA_SUFFIX_RE = re.compile(r"\.(safetensors|ckpt|pt|bin|sft)$", re.I)


def normalize_match_key(text: str) -> str:
    """档案匹配键：小写、去后缀、仅保留字母数字/日文假名/汉字。"""
    text = str(text or "").strip().lower()
    text = _LORA_SUFFIX_RE.sub("", text)
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", text)


def normalize_lora_key(text: str) -> str:
    """LoRA 文件名键：取 basename、小写、去后缀、统一全角字符、去空白下划线点。"""
    text = str(text or "").strip().replace("\\", "/").split("/")[-1].lower()
    text = _LORA_SUFFIX_RE.sub("", text)
    text = text.replace("＿", "_").replace("－", "-").replace("—", "-").replace("–", "-")
    return re.sub(r"[\s_\-\.]+", "", text)


# ---------------------------------------------------------------------------
# 泛化词（绝不当作别名/角色证据）
# ---------------------------------------------------------------------------

#: 蒸馏自 main.py _is_generic_profile_match_term 与 intent_utils 停止词
_GENERIC_TERMS = {
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
    "wutheringwaves", "wuwa", "genshinimpact", "honkaiimpact", "honkaistarrail",
    "cyberpunk", "cyberpunkedgerunners", "edgerunners", "cyberpunk2077",
    "鸣潮", "原神", "崩坏", "崩坏星穹铁道", "赛博朋克", "少女", "少年",
    "自己", "自身", "本人", "女孩", "男孩", "女生", "男生", "角色", "人物",
    "白发", "黑发", "金发", "银发", "红发", "蓝发", "绿发", "粉发", "紫发",
    "长发", "短发", "双马尾", "辫子", "猫耳", "兔耳", "狐耳", "翅膀", "尾巴",
}

#: 泛化计数标签：Ngirl/Nboy/Nperson
_GENERIC_COUNT_RE = re.compile(r"^\d+(girl|boy|girls|boys|person|people)$")
#: 泛化外貌标签：颜色+hair/eyes
_GENERIC_APPEARANCE_RE = re.compile(
    r"^(black|white|blue|red|green|gold|blonde|silver|pink|purple|brown)(hair|eyes)$"
)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_CJK_TOKEN_RE = re.compile(r"[\u3400-\u9fff]")


def is_generic_term(term: str) -> bool:
    """泛化词判断：通用词、颜色/发型/计数标签、过短 token 均视为泛化。"""
    raw = str(term or "").strip().lower()
    norm = normalize_match_key(raw)
    if not norm:
        return True
    if norm in _GENERIC_TERMS:
        return True
    if _GENERIC_COUNT_RE.fullmatch(norm):
        return True
    if _GENERIC_APPEARANCE_RE.fullmatch(norm):
        return True
    if _CJK_RE.search(raw):
        return len(norm) < 2
    return len(norm) < 3


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class LoraProfile:
    """LoRA 档案（平台无关的纯数据）。"""

    key: str
    lora_name: str = ""
    family: str = MODEL_FAMILY_ILLUSTRIOUS
    character: str = ""
    aliases: list[str] = field(default_factory=list)
    role_terms: list[str] = field(default_factory=list)
    trigger_words: list[str] = field(default_factory=list)
    strength_model: Optional[float] = None
    strength_clip: Optional[float] = None
    source: str = "local"
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, key: str, data: dict[str, Any], family: str = "") -> "LoraProfile":
        """从档案字典构建。兼容插件 lora_profile JSON 的字段名。"""
        data = dict(data or {})
        aliases: list[str] = []
        for field_name in ("match_terms", "official_aliases", "nickname_aliases", "aliases"):
            raw = data.get(field_name) or []
            if isinstance(raw, str):
                raw = re.split(r"[,，;；\n\r]+", raw)
            elif isinstance(raw, list):
                # 列表元素可能是逗号拼接的字符串，逐一拆分
                raw = [
                    piece
                    for item in raw
                    for piece in re.split(r"[,，;；\n\r]+", str(item))
                ]
            aliases.extend(str(item).strip() for item in raw if str(item).strip())

        role_terms: list[str] = []
        raw_roles = data.get("role_terms") or data.get("roles") or []
        if isinstance(raw_roles, str):
            raw_roles = re.split(r"[,，;；\n\r]+", raw_roles)
        elif isinstance(raw_roles, list):
            raw_roles = [
                piece
                for item in raw_roles
                for piece in re.split(r"[,，;；\n\r]+", str(item))
            ]
        role_terms.extend(str(item).strip() for item in raw_roles if str(item).strip())

        trigger_words: list[str] = []
        raw_trigger = (
            data.get("trigger_words")
            or data.get("profile_activation_tags")
            or data.get("trigger_prompt")
            or []
        )
        if isinstance(raw_trigger, str):
            raw_trigger = re.split(r"[,，;；\n\r]+", raw_trigger)
        elif isinstance(raw_trigger, list):
            raw_trigger = [
                piece
                for item in raw_trigger
                for piece in re.split(r"[,，;；\n\r]+", str(item))
            ]
        trigger_words.extend(str(item).strip() for item in raw_trigger if str(item).strip())

        def _float_opt(name: str) -> Optional[float]:
            try:
                value = data.get(name)
                return float(value) if value is not None and str(value).strip() != "" else None
            except Exception:
                return None

        return cls(
            key=str(key or "").strip(),
            lora_name=str(data.get("lora") or data.get("lora_name") or "").strip(),
            family=str(data.get("family") or family or "").strip().lower() or MODEL_FAMILY_ILLUSTRIOUS,
            character=str(data.get("character") or "").strip(),
            aliases=[a for a in aliases if a],
            role_terms=[r for r in role_terms if r],
            trigger_words=[t for t in trigger_words if t],
            strength_model=_float_opt("strength_model"),
            strength_clip=_float_opt("strength_clip"),
            source=str(data.get("source") or "local").strip(),
            tags=[str(t).strip() for t in (data.get("tags") or []) if str(t).strip()],
            extra=data,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "lora_name": self.lora_name,
            "family": self.family,
            "character": self.character,
            "aliases": list(self.aliases),
            "role_terms": list(self.role_terms),
            "trigger_words": list(self.trigger_words),
            "strength_model": self.strength_model,
            "strength_clip": self.strength_clip,
            "source": self.source,
            "tags": list(self.tags),
        }


@dataclass
class LoraSelection:
    """一次明确的 LoRA 选择结果。"""

    key: str
    lora_name: str
    family: str
    character: str = ""
    matched_term: str = ""
    match_kind: str = "exact"  # "exact" | "fuzzy"
    score: float = 0.0
    strength_model: float = 0.0
    strength_clip: float = 0.0
    trigger_words: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    suggestion_only: bool = False  # True = 跨模型族临时建议，不持久修改
    notice: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "lora_name": self.lora_name,
            "family": self.family,
            "character": self.character,
            "matched_term": self.matched_term,
            "match_kind": self.match_kind,
            "score": self.score,
            "strength_model": self.strength_model,
            "strength_clip": self.strength_clip,
            "trigger_words": list(self.trigger_words),
            "roles": list(self.roles),
            "suggestion_only": self.suggestion_only,
            "notice": self.notice,
        }


@dataclass
class LoraMatchResult:
    """匹配结果集合：单/多角色判定与连接词证据。"""

    matches: list[LoraSelection] = field(default_factory=list)
    multi: bool = False
    connector_found: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": [m.to_dict() for m in self.matches],
            "multi": self.multi,
            "connector_found": self.connector_found,
        }


# ---------------------------------------------------------------------------
# 匹配工具
# ---------------------------------------------------------------------------

#: 多角色连接词（蒸馏自 main._multi_lora_matches_have_connector / intent_utils）
_CONNECTOR_RE = re.compile(
    r"(还有|還有|以及|并|並|和|跟|与|與|及|同|、|&|，|,|\+|\/|\bvs\.?\b|\band\b|\bwith\b)",
    re.I,
)

#: 英文整词边界
_WORD_BOUNDARY_RE = re.compile(r"(?<![A-Za-z0-9_])")

#: 短 CJK 术语两侧的"安全边界"字符（来自 main.py _profile_short_cjk_marker_has_safe_boundary）
_CJK_BOUNDARY_CHARS = set("的了着給给畫画绘繪看要想找搜选選用换換抽点點来來和跟与與及同")
_CJK_BOUNDARY_PHRASES = (
    "来点", "來點", "想看", "要看", "给我", "給我", "帮我", "幫我",
    "画个", "畫個", "画张", "畫張", "画一张", "畫一張", "生成",
    "找", "搜", "搜索", "导入", "導入", "切换", "切換",
)
_CJK_CHUNK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]+")


def _short_cjk_marker_has_safe_boundary(marker: str, raw_text: str) -> bool:
    """短 CJK 术语（<=2 字）必须位于 CJK 词块开头，或前接安全边界字符。

    例如 "画一个白露" / "白露和镜流" 安全；"小白露丝" 中的 "白露" 不安全。
    """
    marker = normalize_match_key(marker)
    if not marker or len(marker) > 2:
        return True
    for chunk_match in _CJK_CHUNK_RE.finditer(str(raw_text or "")):
        chunk = normalize_match_key(chunk_match.group(0))
        if not chunk:
            continue
        for hit in re.finditer(re.escape(marker), chunk):
            start, end = hit.span()
            before = chunk[:start]
            after = chunk[end:]
            if not before or not after:
                return True
            if before[-1] in _CJK_BOUNDARY_CHARS:
                return True
            if any(before.endswith(phrase) for phrase in _CJK_BOUNDARY_PHRASES):
                return True
    return False


def _term_hits_in_text(term: str, text_norm: str, raw_text: str) -> list[tuple[int, int, str]]:
    """返回术语在文本中的命中区间列表，带边界约束（明确证据）。"""
    term = str(term or "").strip()
    if not term:
        return []
    term_norm = normalize_match_key(term)
    if not term_norm or is_generic_term(term):
        return []
    text_norm = str(text_norm or "").strip()
    raw_text = str(raw_text or "")
    hits: list[tuple[int, int, str]] = []

    # 1) 完全相等（规范键）
    if term_norm == text_norm:
        return [(0, len(text_norm), "exact")]

    # 2) 英文：整词边界命中
    if re.fullmatch(r"[A-Za-z0-9_.\- ]+", term):
        pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])", re.I)
        for match in pattern.finditer(raw_text):
            hits.append((match.start(), match.end(), "exact"))
        return hits

    # 3) 中日韩：子串命中 + 安全边界（短术语不嵌在更长 CJK 名字中）
    has_cjk = bool(_CJK_RE.search(term))
    if not has_cjk:
        return hits
    if len(term_norm) <= 2 and not _short_cjk_marker_has_safe_boundary(term_norm, raw_text):
        return hits
    for match in re.finditer(re.escape(term), raw_text):
        hits.append((match.start(), match.end(), "exact"))
    return hits


def _fuzzy_hits(term: str, raw_text: str) -> list[tuple[int, int, str]]:
    """模糊命中：仅用于较长 CJK 术语（拉丁词必须整词边界，不做模糊）。"""
    term = str(term or "").strip()
    if not _CJK_RE.search(term):
        return []
    term_norm = normalize_match_key(term)
    if len(term_norm) < 4:
        return []
    text_norm = normalize_match_key(raw_text)
    if not text_norm or term_norm == text_norm:
        return []
    # 顺序子序列匹配：术语字符在 text_norm 中按序出现且总跨度受限
    start = text_norm.find(term_norm[:1])
    if start < 0:
        return []
    idx = start
    for ch in term_norm[1:]:
        found = text_norm.find(ch, idx)
        if found < 0:
            return []
        idx = found + 1
    if idx - start > len(term_norm) + 6:
        return []
    return [(start, idx, "fuzzy")]


# ---------------------------------------------------------------------------
# LoraService
# ---------------------------------------------------------------------------


class LoraService:
    """平台无关的 LoRA 档案匹配服务。

    :param profiles: ``{family: {key: profile_dict}}`` 或 ``{key: profile_dict}``
        （后者归入默认族）。
    :param default_strengths: 覆盖默认强度（model, clip）。
    :param strength_caps: 覆盖强度上限 ``{family: {"model": .., "clip": ..}}``。
    """

    def __init__(
        self,
        profiles: Optional[dict[str, Any]] = None,
        default_strengths: Optional[dict[str, tuple[float, float]]] = None,
        strength_caps: Optional[dict[str, dict[str, Optional[float]]]] = None,
        default_family: str = MODEL_FAMILY_ILLUSTRIOUS,
    ) -> None:
        self._profiles: dict[str, dict[str, LoraProfile]] = {
            MODEL_FAMILY_ANIMA: {},
            MODEL_FAMILY_ILLUSTRIOUS: {},
        }
        self.default_strengths = dict(default_strengths or DEFAULT_STRENGTHS)
        self.strength_caps = {
            family: dict(caps) for family, caps in (strength_caps or DEFAULT_STRENGTH_CAPS).items()
        }
        self.default_family = str(default_family or MODEL_FAMILY_ILLUSTRIOUS).strip().lower()
        if profiles:
            self.load_profiles(profiles)

    # -- 档案管理 ---------------------------------------------------------------

    def load_profiles(self, data: dict[str, Any]) -> int:
        """加载档案。支持 ``{family: {key: profile}}`` 与 ``{key: profile}`` 两种形态。"""
        count = 0
        if not isinstance(data, dict):
            return 0
        nested = any(
            str(key).strip().lower() in SUPPORTED_MODEL_FAMILIES
            and isinstance(value, dict)
            for key, value in data.items()
        )
        if nested:
            for family, items in data.items():
                family = str(family or "").strip().lower()
                if family not in SUPPORTED_MODEL_FAMILIES:
                    continue
                if isinstance(items, dict):
                    count += self._add_family(family, items)
        else:
            count += self._add_family(self.default_family, data)
        return count

    def _add_family(self, family: str, items: dict[str, Any]) -> int:
        count = 0
        for key, raw in items.items():
            if not isinstance(raw, dict):
                continue
            profile = LoraProfile.from_dict(str(key), raw, family=family)
            if not profile.key or not profile.lora_name:
                continue
            if profile.family not in SUPPORTED_MODEL_FAMILIES:
                profile.family = family
            self._profiles.setdefault(profile.family, {})[profile.key] = profile
            count += 1
        return count

    def add_profile(self, profile: LoraProfile) -> None:
        family = profile.family if profile.family in SUPPORTED_MODEL_FAMILIES else self.default_family
        self._profiles.setdefault(family, {})[profile.key] = profile

    def profiles_for(self, family: str) -> dict[str, LoraProfile]:
        return dict(self._profiles.get(str(family or "").strip().lower(), {}))

    # -- 术语提取 ---------------------------------------------------------------

    def profile_terms(self, profile: LoraProfile, include_triggers: bool = True) -> list[str]:
        """档案的全部匹配术语（key/角色/别名/角色词/触发词），过滤泛化词。"""
        raw = [profile.key, profile.character, *profile.aliases, *profile.role_terms]
        if include_triggers:
            raw.extend(profile.trigger_words)
        terms: list[str] = []
        seen: set[str] = set()
        for item in raw:
            item = str(item or "").strip()
            if not item or is_generic_term(item):
                continue
            norm = normalize_match_key(item)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            terms.append(item)
        return terms

    # -- 匹配主流程 ---------------------------------------------------------------

    def match(
        self,
        text: str,
        family: Optional[str] = None,
        limit: int = 3,
        include_fuzzy: bool = True,
    ) -> LoraMatchResult:
        """在指定模型族内匹配。只有明确证据才返回。"""
        family = str(family or "").strip().lower() or self.default_family
        raw_text = str(text or "").strip()
        text_norm = normalize_match_key(raw_text)
        result = LoraMatchResult()
        if not raw_text or not text_norm:
            return result

        scored: list[dict[str, Any]] = []
        used_terms: set[str] = set()
        for key, profile in self.profiles_for(family).items():
            for term in self.profile_terms(profile):
                norm = normalize_match_key(term)
                if not norm or norm in used_terms:
                    continue
                hits = _term_hits_in_text(term, text_norm, raw_text)
                kind = None
                span = None
                if hits:
                    kind, span = "exact", hits[0]
                elif include_fuzzy:
                    fuzzy = _fuzzy_hits(term, raw_text)
                    if fuzzy:
                        kind, span = "fuzzy", fuzzy[0]
                if kind is None:
                    continue
                score = 100.0 if kind == "exact" else 70.0
                # 术语越长证据越强
                score = min(100.0, score + min(6.0, len(norm) * 1.5))
                if kind == "fuzzy" and score < 75.0:
                    continue
                used_terms.add(norm)
                scored.append(
                    {
                        "profile": profile,
                        "term": term,
                        "kind": kind,
                        "score": score,
                        "span": span,
                    }
                )
                break  # 每个档案只取最强术语

        scored.sort(key=lambda item: (-item["score"], -len(normalize_match_key(item["term"])), item["profile"].key))
        used_lora: set[str] = set()
        spans: list[tuple[int, int]] = []
        for item in scored:
            lora_key = normalize_lora_key(item["profile"].lora_name)
            if not lora_key or lora_key in used_lora:
                continue
            used_lora.add(lora_key)
            selection = self._build_selection(item["profile"], item["term"], item["kind"], item["score"])
            result.matches.append(selection)
            spans.append((item["span"][0], item["span"][1]))
            if len(result.matches) >= max(1, int(limit or 3)):
                break

        # 多角色判定：>=2 个命中且存在连接词证据
        result.multi = self._has_connector_between(raw_text, spans) and len(result.matches) >= 2
        result.connector_found = result.multi
        return result

    def single_match(self, text: str, family: Optional[str] = None, min_score: float = 75.0) -> Optional[LoraSelection]:
        """单角色匹配：返回最强命中（分数低于阈值返回 None）。"""
        result = self.match(text, family=family, limit=1)
        for match in result.matches:
            if match.score >= min_score:
                return match
        return None

    # -- 跨模型族临时建议 -----------------------------------------------------------

    def suggest_cross_family(self, text: str, current_family: Optional[str] = None) -> Optional[LoraSelection]:
        """当前族无明确命中时，尝试其它族的档案。

        只返回 ``suggestion_only=True`` 的临时建议，绝不持久修改模型族状态。
        """
        current_family = str(current_family or "").strip().lower() or self.default_family
        raw_text = str(text or "").strip()
        if not raw_text:
            return None
        if self.single_match(raw_text, family=current_family, min_score=75.0):
            return None  # 当前族已有明确命中 → 不跨族
        for family in SUPPORTED_MODEL_FAMILIES:
            if family == current_family:
                continue
            selection = self.single_match(raw_text, family=family, min_score=80.0)
            if selection:
                selection.suggestion_only = True
                selection.notice = (
                    f"跨模型族临时建议（{family}）：仅本次建议，不持久修改当前模型族设置"
                )
                return selection
        return None

    # -- 强度解析 -----------------------------------------------------------------

    def resolve_strengths(
        self,
        selection: LoraSelection,
        family: Optional[str] = None,
    ) -> LoraSelection:
        """应用档案强度 → 默认强度 → 模型族上限钳制。"""
        family = str(family or selection.family or "").strip().lower() or self.default_family
        model_default, clip_default = self.default_strengths.get(family, (0.75, 0.70))
        profile = self._profiles.get(family, {}).get(selection.key)
        model = profile.strength_model if profile and profile.strength_model is not None else model_default
        clip = profile.strength_clip if profile and profile.strength_clip is not None else clip_default
        caps = self.strength_caps.get(family, {})
        cap_model = caps.get("model")
        cap_clip = caps.get("clip")
        if cap_model is not None:
            model = min(model, float(cap_model))
        if cap_clip is not None:
            clip = min(clip, float(cap_clip))
        selection.strength_model = round(float(model), 4)
        selection.strength_clip = round(float(clip), 4)
        return selection

    # -- 内部 ---------------------------------------------------------------------

    def _build_selection(self, profile: LoraProfile, term: str, kind: str, score: float) -> LoraSelection:
        selection = LoraSelection(
            key=profile.key,
            lora_name=profile.lora_name,
            family=profile.family,
            character=profile.character or profile.key,
            matched_term=term,
            match_kind=kind,
            score=round(float(score), 2),
            trigger_words=list(profile.trigger_words),
            roles=list(profile.role_terms),
        )
        return self.resolve_strengths(selection, family=profile.family)

    @staticmethod
    def _has_connector_between(text: str, spans: list[tuple[int, int]]) -> bool:
        if len(spans) < 2:
            return False
        ordered = sorted(spans)
        for left, right in zip(ordered, ordered[1:]):
            between = str(text or "")[left[1]:right[0]]
            if _CONNECTOR_RE.search(between):
                return True
        return False


__all__ = [
    "LoraService",
    "LoraProfile",
    "LoraSelection",
    "LoraMatchResult",
    "normalize_match_key",
    "normalize_lora_key",
    "is_generic_term",
    "MODEL_FAMILY_ANIMA",
    "MODEL_FAMILY_ILLUSTRIOUS",
    "SUPPORTED_MODEL_FAMILIES",
]
