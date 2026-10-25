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

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from .lora_binding import lora_path_key, text_list

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
    # anima 无钳制会让 Civitai 档案里的 1.2+ 强度原样生效，画面被单一 LoRA 吞掉
    MODEL_FAMILY_ANIMA: {"model": 0.95, "clip": 0.95},
    MODEL_FAMILY_ILLUSTRIOUS: {"model": 0.82, "clip": 0.72},
}

# ---------------------------------------------------------------------------
# 名称规范化（与 main.py _cached_normalize_* 保持一致，纯函数、确定性）
# ---------------------------------------------------------------------------

_LORA_SUFFIX_RE = re.compile(r"\.(safetensors|ckpt|pt|bin|sft)$", re.I)


def normalize_match_key(text: str) -> str:
    """档案匹配键：小写、去后缀、仅保留字母数字/日文假名/汉字。"""
    text = unicodedata.normalize("NFKC", str(text or "")).strip().casefold()
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
        alias_fields = (
            "match_terms",
            "official_aliases",
            "nickname_aliases",
            "aliases",
            "search_terms",
            "source_aliases",
        ) if not data.get("aliases_authoritative") else ("aliases",)
        for field_name in alias_fields:
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
        for field_name in (() if data.get("aliases_authoritative") else ("search_query",)):
            value = str(data.get(field_name) or "").strip()
            if value:
                aliases.append(value)

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
        raw_trigger = data["trigger_words"] if "trigger_words" in data else (
            data.get("profile_activation_tags")
            or data.get("trigger_prompt")
            or data.get("activation_tags")
            or data.get("trained_words")
            or data.get("trainedWords")
            or data.get("char_prompt")
            or []
        )
        trigger_words.extend(text_list(raw_trigger))

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
    connector: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": [m.to_dict() for m in self.matches],
            "multi": self.multi,
            "connector_found": self.connector_found,
            "connector": self.connector,
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
    "找", "搜", "搜索", "导入", "導入", "切换", "切換", "一个", "一個",
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
            if not before and not after:
                return True
            if before and (
                before[-1] in _CJK_BOUNDARY_CHARS
                or any(before.endswith(phrase) for phrase in _CJK_BOUNDARY_PHRASES)
            ):
                return True
            if after and after[0] in _CJK_BOUNDARY_CHARS:
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
# 多角色意图判定（移植插件 intent_utils.py）
# ---------------------------------------------------------------------------

_MCI_CJK_NAME_TOKEN = r"[㐀-鿿]{2,8}"
_MCI_LATIN_NAME_TOKEN = r"[A-Z][A-Za-z0-9_.-]{1,40}(?:\s+[A-Z][A-Za-z0-9_.-]{1,40}){0,2}"
_MCI_NAME_TOKEN = rf"(?:{_MCI_CJK_NAME_TOKEN}|{_MCI_LATIN_NAME_TOKEN})"
_MCI_PAIR_CONNECTOR = r"(?:还有|還有|以及|并|並|和|跟|与|與|及|同|、|，|,|&|\+|/|[Vv][Ss]\.?|[Aa][Nn][Dd]|[Ww][Ii][Tt][Hh])"

_MCI_CJK_NON_NAME_FRAGMENT = re.compile(
    r"(自己|自身|本人|一只|一隻|另一|另只|另隻|两只|兩隻|双|雙|"
    r"手|手臂|胳膊|手指|指尖|胸|乳|奶|嘴|口|头|頭|发|髮|腿|脚|腳|足|腰|臀|腹|"
    r"抱|抬|举|舉|咬|送|拿|扶|托|摸|抓|握|用力|亲|親|吻|贴|貼|靠|"
    r"的|了|着|著|把|被|给|給|到|在|向|从|從|下|上|边|邊|一边|一邊)"
)

_MCI_CJK_LITERAL_NON_NAME_FRAGMENT = re.compile(
    "(?:"
    "自己|自身|本人|单人|單人|"
    "独自|獨自|全身|半身|正面|"
    "背面|侧面|展示|完整|足底|"
    "脚底|脚掌|手|手指|手臂|"
    "胳膊|腿|小腿|大腿|胸|乳|"
    "头发|頭髮|帽子|海边|海邊|"
    "沙滩|沙灘|地牢|泳装|泳裝|"
    "比基尼|一只|一隻|另一只|"
    "另一隻|左手|右手|身体|身體"
    ")"
)

_MCI_LATIN_NON_NAME_WORDS = {
    "a", "an", "and", "another", "arm", "arms", "breast", "breasts", "body",
    "boy", "character", "characters", "face", "finger", "fingers", "foot",
    "girl", "girls", "hand", "hands", "hair", "head", "her", "herself", "him",
    "himself", "holding", "hugging", "left", "leg", "legs", "mouth", "nipple",
    "one", "other", "own", "person", "right", "self", "side", "the", "their",
    "them", "toward", "two", "with",
}

_MCI_EXPLICIT_MULTI_COUNT_RE = re.compile(
    r"\b(?:[2-9]\s*(?:girls?|boys?|people|persons?|characters?)|"
    r"(?:two|three|four|five|six|seven|eight|nine)\s+(?:girls?|boys?|people|persons?|characters?)|"
    r"(?:one|a)\s+(?:girl|boy|woman|man|person|character)\s+(?:and|with)\s+(?:one|a)\s+(?:girl|boy|woman|man|person|character)|"
    r"(?:girl|boy|woman|man|person|character)\s+(?:and|with)\s+(?:girl|boy|woman|man|person|character)|"
    r"multiple\s+(?:girls?|boys?|people|characters?)|group|crowd|duo|pair|couple|twins?)\b",
    re.I,
)

_MCI_CJK_EXPLICIT_MULTI_RE = re.compile(
    r"(多人|双人|雙人|俩人|倆人|俩个人|倆個人|仨人|仨个人|兩人|两人|三人|四人|五人|"
    r"多个角色|多個角色|多角色|双角色|雙角色|双女主|雙女主|双男主|雙男主|两女|兩女|二女|两男|兩男|二男|"
    r"群像|一群|同框|同屏|同画面|同畫面|合照|合影|双胞胎|雙胞胎|姐妹|兄弟|情侣|情侶|搭档|搭檔|二人组|二人組)"
)

_MCI_CJK_MULTI_COUNT_RE = re.compile(
    r"(?:"
    r"(?:2|3|4|5|6|7|8|9)\s*(?:个|個|位|名|只)?|"
    r"俩|倆|仨|两|兩|二|三|四|五|六|七|八|九|两个|兩個|二个|二個|两位|兩位|二位|两名|兩名|二名|"
    r"三个|三個|三位|三名|四个|四個|四位|四名"
    r").{0,4}(人|角色|人物|女孩|少女|男孩|女生|男生|女人|男人|女孩子|男孩子|girl|girls|boy|boys)"
)

_MCI_CJK_MIXED_GENDER_PAIR_RE = re.compile(
    r"(一男一女|一女一男|一男和一女|一女和一男|男生女生|女生男生|男女二人|男女两人|男女兩人|一对男女|一對男女)"
)

_MCI_CJK_PLURAL_ACTION_RE = re.compile(
    r"(她们|她們|他们|他們|TA们|ta们|两人|兩人|二人|双方|雙方|两位|兩位).{0,18}"
    r"(互动|互動|对视|對視|牵手|牽手|拥抱|擁抱|贴贴|貼貼|亲吻|親吻|接吻|靠在一起|抱在一起|并肩|並肩|背靠背|面对面|面對面)"
)

_MCI_ROLE_SLOT_RE = re.compile(
    r"(?:(?:角色|人物|人设|人設|role|character|char)\s*([A-Ca-c1-3一二三①②③])|"
    r"\b([A-Ca-c])\s*(?:角色|人物|role|character|char)\b|"
    r"(?:^|[\n,，;；])\s*(?:角色\s*)?([A-Ca-c1-3一二三①②③])\s*[:：])",
    re.I,
)

_MCI_SIDE_ROLE_RE = re.compile(
    r"("
    r"(?:左边|左側|左侧|左邊|left).{0,10}(?:角色|人物|女孩|少女|男孩|女生|男生|girl|boy|character|person)"
    r".{0,80}"
    r"(?:右边|右側|右侧|右邊|right).{0,10}(?:角色|人物|女孩|少女|男孩|女生|男生|girl|boy|character|person)"
    r"|"
    r"(?:右边|右側|右侧|右邊|right).{0,10}(?:角色|人物|女孩|少女|男孩|女生|男生|girl|boy|character|person)"
    r".{0,80}"
    r"(?:左边|左側|左侧|左邊|left).{0,10}(?:角色|人物|女孩|少女|男孩|女生|男生|girl|boy|character|person)"
    r")",
    re.I,
)

_MCI_SELF_SUBJECT_RE = re.compile(
    r"(自己|自身|本人|自分|\bown\b|\boneself\b|\bmyself\b|\bherself\b|\bhimself\b|\bitself\b|\bself[-\s])",
    re.I,
)

_MCI_EXPLICIT_OTHER_SUBJECT_RE = re.compile(
    r"(别人|別人|他人|另一(?:个|個|位)?(?:人|角色|女孩|少女|男孩|女生|男生)|"
    r"\b(?:another|other|second|third)\s+(?:girl|boy|person|character|woman|man)\b)",
    re.I,
)

_MCI_BODY_PART_PAIR_RE = re.compile(
    r"(?:一只|一隻|另一只|另一隻|左|右|left|right|one|other)?\s*"
    r"(?:手|手臂|胳膊|腿|脚|腳|眼|胸|乳|奶|hand|arm|leg|foot|eye|breast)\s*"
    rf"{_MCI_PAIR_CONNECTOR}\s*"
    r"(?:一只|一隻|另一只|另一隻|左|右|left|right|one|other)?\s*"
    r"(?:手|手臂|胳膊|腿|脚|腳|眼|胸|乳|奶|hand|arm|leg|foot|eye|breast)",
    re.I,
)

_MCI_RELATIONAL_MULTI_RE = re.compile(
    r"(和|跟|与|與|同).{1,24}(一起|同框|合照|并排|並排|互动|互動|拥抱|擁抱|牵手|牽手|聚餐|吃饭|吃飯|聊天|约会|約會)|"
    r"(一起|同框|合照|并排|並排|互动|互動|拥抱|擁抱|牵手|牽手|聚餐|吃饭|吃飯|聊天|约会|約會).{0,24}(和|跟|与|與|同)"
)

_MCI_GENERIC_PAIR_CONNECTOR_RE = re.compile(
    r"(?:\b(?:and|with|plus|vs\.?|versus|x)\b|[&+/,;]|"
    "，|、|；|和|跟|与|與|同|及|"
    "以及|还有|還有|并|並)",
    re.I,
)

_MCI_SUBJECT_NOUN_RE = re.compile(
    r"\b(?:girl|girls|boy|boys|woman|women|man|men|person|people|character|"
    r"characters|oc|ocs|role|roles|maid|idol|student|sister|brother|friend|"
    r"partner|companion|teammate|knight|princess|witch|angel|demon)\b|"
    "(?:女孩|少女|女生|男孩|少年|"
    "男生|女人|男人|人物|角色|"
    "人设|女仆|偶像|学生|姐姐|"
    "妹妹|哥哥|弟弟|朋友|伙伴|"
    "队友|骑士|公主|魔女|天使|"
    "恶魔|OC|oc)",
    re.I,
)

_MCI_APPEARANCE_WORD_RE = (
    r"(?:white[-\s]*hair(?:ed)?|black[-\s]*hair(?:ed)?|blonde|silver[-\s]*hair(?:ed)?|red[-\s]*hair(?:ed)?|"
    r"blue[-\s]*hair(?:ed)?|green[-\s]*hair(?:ed)?|pink[-\s]*hair(?:ed)?|purple[-\s]*hair(?:ed)?|long[-\s]*hair(?:ed)?|"
    r"short[-\s]*hair(?:ed)?|twintails?|braid|cat\s*ears?|rabbit\s*ears?|bunny\s*ears?|"
    r"fox\s*ears?|horns?|halo|wings?|tail|glasses|hat|ribbon|dress|uniform|"
    r"school\s*uniform|maid|armor|cloak)"
)
_MCI_CJK_APPEARANCE_WORD_RE = (
    "白发|黑发|金发|银发|红发|"
    "蓝发|绿发|粉发|紫发|长发|"
    "短发|双马尾|辫子|猫耳|"
    "兔耳|狐耳|角|光环|翅膀|"
    "尾巴|眼镜|帽|丝带|蝜蝶结|"
    "裙|制服|校服|女仆|盔甲|披风|"
    "左边|右边|左侧|右侧|另一个|"
    "第二个|前面|后面"
)
_MCI_APPEARANCE_SUBJECT_RE = re.compile(
    rf"(?:{_MCI_APPEARANCE_WORD_RE}[^\n\r,;\+&/，、；]{{0,32}}(?:girl|boy|woman|man|person|character|oc|role|maid|idol|student|friend|partner))|"
    f"(?:(?:{_MCI_CJK_APPEARANCE_WORD_RE})[^\n\r,;\\+&/，、；]{{0,18}}(?:女孩|少女|女生|男孩|少年|男生|人物|角色|人设|朋友|伙伴|OC|oc))",
    re.I,
)

_MCI_SPATIAL_ROLE_MARKER_RE = re.compile(
    r"\b(?:left|right|front|back|behind|beside|next\s+to|foreground|background|middle|center)\b|"
    "(?:左边|右边|左侧|右侧|左面|"
    "右面|前面|后面|旁边|身边|"
    "前景|背景|中间|中央|另一边)",
    re.I,
)

_MCI_SHARED_ACTION_RE = re.compile(
    r"\b(?:together|interacting|interaction|facing\s+each\s+other|looking\s+at\s+each\s+other|"
    r"holding\s+hands|hugging|embracing|cuddling|kissing|talking|chatting|sitting\s+together|"
    r"standing\s+together|posing\s+together|fighting|dancing|sharing|beside\s+each\s+other)\b|"
    "(?:一起|同框|同屏|合照|互动|"
    "互動|对视|對視|牵手|擁抱|"
    "拥抱|搂|贴贴|亲吻|接吻|"
    "并肩|並肩|面对面|面對面|"
    "聊天|聚餐|聚餐|约会|約會|"
    "打架|跳舞|对打|對打)",
    re.I,
)

_MCI_PLURAL_REFERENCE_RE = re.compile(
    r"\b(?:they|them|their|both|each\s+other|two\s+of\s+them|all\s+of\s+them)\b|"
    "(?:她们|她們|他们|他們|TA们|ta们|"
    "两人|兩人|二人|俩人|两位|"
    "兩位|双方|雙方|彼此)",
    re.I,
)

_MCI_SINGLE_SUBJECT_STRONG_RE = re.compile(
    r"\b(?:solo|alone|single\s+(?:person|character|subject)|one\s+(?:girl|boy|person|character)\s+only|1girl|1boy)\b|"
    "(?:单人|單人|独自|獨自|一个人|"
    "一個人|一人|单独|單獨)",
    re.I,
)

_MCI_LATIN_NAME_PAIR_RE = re.compile(
    r"\b(?P<left>[A-Za-z][A-Za-z0-9_.-]{2,40})\s+"
    r"(?:and|with|plus|vs\.?|versus|x|/|&|\+)\s+"
    r"(?P<right>[A-Za-z][A-Za-z0-9_.-]{2,40})\b",
    re.I,
)

_MCI_PROBABLE_NAME_STOP_WORDS = _MCI_LATIN_NON_NAME_WORDS | {
    "best", "quality", "masterpiece", "solo", "alone", "white", "black", "blue",
    "green", "red", "pink", "purple", "silver", "gold", "golden", "hair", "eyes",
    "dress", "skirt", "shirt", "uniform", "background", "foreground", "standing",
    "sitting", "lying", "smiling", "looking", "wearing", "holding", "hugging",
}


def _mci_is_cjk_character_name_candidate(token: str) -> bool:
    token = str(token or "").strip()
    return bool(
        re.fullmatch(_MCI_CJK_NAME_TOKEN, token)
        and not _MCI_CJK_NON_NAME_FRAGMENT.search(token)
        and not _MCI_CJK_LITERAL_NON_NAME_FRAGMENT.search(token)
    )


def _mci_is_latin_character_name_candidate(token: str) -> bool:
    token = str(token or "").strip()
    if not re.fullmatch(_MCI_LATIN_NAME_TOKEN, token):
        return False
    words = re.findall(r"[A-Za-z]+", token.lower())
    return bool(words) and not any(word in _MCI_LATIN_NON_NAME_WORDS for word in words)


def _mci_is_character_name_candidate(token: str) -> bool:
    return _mci_is_cjk_character_name_candidate(token) or _mci_is_latin_character_name_candidate(token)


def _mci_has_explicit_character_name_pair(text: str) -> bool:
    text = str(text or "")
    for match in re.finditer(rf"(?P<left>{_MCI_NAME_TOKEN})\s*{_MCI_PAIR_CONNECTOR}\s*(?P<right>{_MCI_NAME_TOKEN})", text):
        if _mci_is_character_name_candidate(match.group("left")) and _mci_is_character_name_candidate(match.group("right")):
            return True
    return False


def _mci_has_multi_role_slots(text: str) -> bool:
    labels = set()
    for match in _MCI_ROLE_SLOT_RE.finditer(str(text or "")):
        label = next((item for item in match.groups() if item), "")
        label = str(label or "").strip().lower()
        if label:
            labels.add(label)
    return len(labels) >= 2


def _mci_merged_spans(spans: list) -> list:
    spans = sorted((int(start), int(end)) for start, end in spans if int(end) > int(start))
    merged = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _mci_is_embedded_ascii_subject_span(text: str, start: int, end: int) -> bool:
    text = str(text or "")
    token = text[start:end]
    if not token or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\s-]*", token):
        return False
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return bool(
        (before and re.match(r"[A-Za-z0-9_]", before))
        or (after and re.match(r"[A-Za-z0-9_]", after))
    )


def _mci_subject_spans(text: str) -> list:
    text = str(text or "")
    spans = [
        (m.start(), m.end())
        for m in _MCI_SUBJECT_NOUN_RE.finditer(text)
        if not _mci_is_embedded_ascii_subject_span(text, m.start(), m.end())
    ]
    spans.extend(
        (m.start(), m.end())
        for m in _MCI_APPEARANCE_SUBJECT_RE.finditer(text)
        if not _mci_is_embedded_ascii_subject_span(text, m.start(), m.end())
    )
    return _mci_merged_spans(spans)


def _mci_subject_mention_count(text: str) -> int:
    return len(_mci_subject_spans(text))


def _mci_appearance_subject_count(text: str) -> int:
    text = str(text or "")
    return len(_mci_merged_spans([
        (m.start(), m.end())
        for m in _MCI_APPEARANCE_SUBJECT_RE.finditer(text)
        if not _mci_is_embedded_ascii_subject_span(text, m.start(), m.end())
    ]))


def _mci_has_subject_listing(text: str) -> bool:
    text = str(text or "")
    spans = _mci_subject_spans(text)
    if len(spans) < 2:
        return False
    for left, right in zip(spans, spans[1:]):
        between = text[left[1]:right[0]]
        if len(between) <= 64 and _MCI_GENERIC_PAIR_CONNECTOR_RE.search(between):
            return True
    return False


def _mci_has_probable_latin_name_pair(text: str) -> bool:
    text = str(text or "")
    for match in _MCI_LATIN_NAME_PAIR_RE.finditer(text):
        left = str(match.group("left") or "").lower()
        right = str(match.group("right") or "").lower()
        if left in _MCI_PROBABLE_NAME_STOP_WORDS or right in _MCI_PROBABLE_NAME_STOP_WORDS:
            continue
        if left == right:
            continue
        return True
    return False


def _mci_spatial_marker_count(text: str) -> int:
    markers = []
    for match in _MCI_SPATIAL_ROLE_MARKER_RE.finditer(str(text or "")):
        token = match.group(0).lower()
        if token in {"left", "左边", "左侧", "左面"}:
            token = "left"
        elif token in {"right", "右边", "右侧", "右面"}:
            token = "right"
        elif token in {"front", "foreground", "前面", "前景"}:
            token = "front"
        elif token in {"back", "behind", "background", "后面", "背景"}:
            token = "back"
        markers.append(token)
    return len(set(markers))


def _mci_multi_character_evidence_score(text: str) -> int:
    text = str(text or "")
    if not text:
        return 0
    score = 0
    subject_count = _mci_subject_mention_count(text)
    appearance_count = _mci_appearance_subject_count(text)
    has_shared_action = bool(_MCI_SHARED_ACTION_RE.search(text))
    has_plural_reference = bool(_MCI_PLURAL_REFERENCE_RE.search(text))

    if _mci_has_subject_listing(text):
        score += 3
    elif subject_count >= 2:
        score += 2

    if appearance_count >= 2:
        score += 2
    if _mci_spatial_marker_count(text) >= 2 and subject_count >= 1:
        score += 2
    if has_shared_action and subject_count >= 2:
        score += 2
    elif has_shared_action and (_mci_has_probable_latin_name_pair(text) or has_plural_reference):
        score += 2
    if has_plural_reference and (subject_count >= 1 or has_shared_action):
        score += 2
    if _mci_has_probable_latin_name_pair(text):
        score += 2 if has_shared_action else 1
    if _MCI_SINGLE_SUBJECT_STRONG_RE.search(text) and subject_count < 2 and not has_plural_reference:
        score -= 3
    return score


def _mci_looks_like_single_self_subject(text: str) -> bool:
    text = str(text or "")
    if not (_MCI_SELF_SUBJECT_RE.search(text) or _MCI_BODY_PART_PAIR_RE.search(text)):
        return False
    return not (
        _MCI_EXPLICIT_MULTI_COUNT_RE.search(text)
        or _MCI_CJK_EXPLICIT_MULTI_RE.search(text)
        or _MCI_CJK_MULTI_COUNT_RE.search(text)
        or _MCI_CJK_MIXED_GENDER_PAIR_RE.search(text)
        or _MCI_CJK_PLURAL_ACTION_RE.search(text)
        or _MCI_EXPLICIT_OTHER_SUBJECT_RE.search(text)
        or _mci_has_explicit_character_name_pair(text)
        or _mci_has_multi_role_slots(text)
        or _MCI_SIDE_ROLE_RE.search(text)
    )


def has_multi_character_intent(raw_prompt: str, prompt: str = "") -> bool:
    """多角色意图判定（移植插件 intent_utils.illustrious_has_multi_character_intent）。"""
    raw_text = str(raw_prompt or "").strip()
    prompt_text = str(prompt or "").strip()
    combined = raw_text if not prompt_text or prompt_text == raw_text else f"{raw_text}\n{prompt_text}".strip()
    if not combined:
        return False
    if (
        _MCI_EXPLICIT_MULTI_COUNT_RE.search(combined)
        or _MCI_CJK_EXPLICIT_MULTI_RE.search(combined)
        or _MCI_CJK_MULTI_COUNT_RE.search(combined)
        or _MCI_CJK_MIXED_GENDER_PAIR_RE.search(combined)
        or _MCI_CJK_PLURAL_ACTION_RE.search(combined)
        or _MCI_EXPLICIT_OTHER_SUBJECT_RE.search(combined)
        or _mci_has_explicit_character_name_pair(combined)
        or _mci_has_multi_role_slots(combined)
        or _MCI_SIDE_ROLE_RE.search(combined)
    ):
        return True
    if _mci_looks_like_single_self_subject(combined):
        return False
    if _mci_multi_character_evidence_score(combined) >= 3:
        return True
    if _MCI_SINGLE_SUBJECT_STRONG_RE.search(combined):
        return False
    return bool(_MCI_RELATIONAL_MULTI_RE.search(combined))


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

    @staticmethod
    def _explicit_single_cjk_alias(profile: LoraProfile, term: str) -> bool:
        """An authoritative named alias may be one CJK character, never a guessed tag."""
        text = unicodedata.normalize('NFKC', str(term or '')).strip()
        return bool(profile.extra.get('aliases_authoritative')) and bool(re.fullmatch(r'[\u3400-\u9fff]', text)) and any(
            text == unicodedata.normalize('NFKC', str(value or '')).strip()
            for value in (profile.character, *profile.aliases)
        )

    def profile_terms(self, profile: LoraProfile, include_triggers: bool = True) -> list[str]:
        """档案的全部匹配术语（key/角色/别名/角色词/触发词），过滤泛化词。"""
        raw = [profile.character, *profile.aliases, *profile.role_terms]
        if not profile.extra.get("aliases_authoritative"):
            raw.insert(0, profile.key)
        if include_triggers:
            raw.extend(profile.trigger_words)
        terms: list[str] = []
        seen: set[str] = set()
        for item in raw:
            item = str(item or "").strip()
            if not item or (is_generic_term(item) and not self._explicit_single_cjk_alias(profile,item)):
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
        """在指定模型族内匹配（移植原插件评分：exact>=58 / fuzzy>=78）。"""
        family = str(family or "").strip().lower() or self.default_family
        raw_text = str(text or "").strip()
        text_norm = normalize_match_key(raw_text)
        result = LoraMatchResult()
        if not raw_text or not text_norm:
            return result

        profiles = self.profiles_for(family)
        blocked = self._blocked_markers(profiles)
        scored: list[dict[str, Any]] = []
        for key, profile in profiles.items():
            match = self._score_lora_profile_match(raw_text, key, profile, blocked_markers=blocked)
            if not match:
                continue
            score = float(match.get("score") or 0)
            kind = str(match.get("match_kind") or "")
            term = str(match.get("term") or "")
            if kind == "exact":
                min_score = 58.0
            elif kind == "fuzzy":
                min_score = 78.0
            else:
                continue
            if not include_fuzzy and kind == "fuzzy":
                continue
            if score < min_score or (is_generic_term(term) and not self._explicit_single_cjk_alias(profile,term)) or self._is_bad_match_term(term):
                continue
            scored.append(match)

        scored.sort(key=self._lora_profile_match_sort_key, reverse=True)
        used_lora: set[str] = set()
        used_markers: set[str] = set()
        selected: list[dict[str, Any]] = []
        for match in scored:
            profile = match.get("profile")
            lora_key = lora_path_key(profile.lora_name)
            term_marker = normalize_match_key(match.get("term") or "")
            if not lora_key or lora_key in used_lora or (term_marker and term_marker in used_markers):
                continue
            used_lora.add(lora_key)
            if term_marker:
                used_markers.add(term_marker)
            selected.append(match)
            result.matches.append(self._build_selection_from_match(profile, match))
            if len(result.matches) >= max(1, int(limit or 3)):
                break

        # 多角色判定：>=2 命中且存在连接词证据或显式多角色意图
        connector = self._multi_lora_matches_have_connector(raw_text, selected)
        multi_intent = self.has_multi_character_intent(raw_text)
        result.multi = len(result.matches) >= 2 and (bool(connector) or multi_intent)
        result.connector_found = bool(connector)
        result.connector = connector or ""
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
        """当前族无明确命中时，返回其它族的临时建议（suggestion_only=True，不持久修改）。"""
        info = self.suggest_cross_family_switch(text, current_family=current_family)
        return info.get("selection") if info else None

    def suggest_cross_family_switch(self, text: str, current_family: Optional[str] = None) -> Optional[dict]:
        """跨族切换建议：返回切换目标族 + 涉及 profile + 恢复所需信息。

        只做建议，不实际修改任何 workflow/作业（编排由 API 层负责）。
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
            result = self.match(raw_text, family=family, limit=3)
            if not result.matches:
                continue
            best = result.matches[0]
            best.suggestion_only = True
            best.notice = f"跨模型族临时建议（{family}）：仅本次建议，不持久修改当前模型族设置"
            return {
                "from_family": current_family,
                "family": family,
                "key": best.key,
                "character": best.character or best.key,
                "lora": best.lora_name,
                "selection": best,
                "restore_family": current_family,
                "notice": f"本次临时用 {family} 族：{best.character or best.key or '命中角色 LoRA'}",
            }
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

    # -- 多角色 prompt 组装 ---------------------------------------------------------

    def resolve_multi_loras(
        self,
        selections: list[LoraSelection],
        family: Optional[str] = None,
        mode: str = "safe_chain",
    ) -> list[LoraSelection]:
        """组装多角色 LoRA：每个角色的有效触发词 + 多 LoRA 强度钳制。

        移植原插件 _prepare_multi_lora_profiles_prompt 的触发词/强度逻辑：
        按 count 应用 mode 对应的强度上限（safe_chain 默认 0.65/0.72，>2 时 0.58/0.64）。
        """
        results: list[LoraSelection] = []
        for selection in selections or []:
            profile = self._profiles.get(selection.family or family or self.default_family, {}).get(selection.key)
            if profile is not None:
                tags, _used_fallback = self._profile_effective_trigger_words(profile)
                routed = self._route_multi_character_trigger(tags, profile, selection.matched_term)
                selection.trigger_words = list(routed) or list(tags)
                if not selection.roles:
                    selection.roles = list(profile.role_terms)
            self.resolve_strengths(selection, family=selection.family or family)
            results.append(selection)
        count = len(results)
        if count >= 2:
            if mode == "staged_multi_lora":
                model_cap, clip_cap = (0.86, 0.86) if count == 2 else (0.82, 0.82)
            elif mode == "densediffusion":
                model_cap, clip_cap = (0.82, 0.82) if count == 2 else (0.75, 0.75)
            else:
                model_cap, clip_cap = (0.65, 0.72) if count == 2 else (0.58, 0.64)
            for selection in results:
                selection.strength_model = round(min(float(selection.strength_model or 0), model_cap), 4)
                selection.strength_clip = round(min(float(selection.strength_clip or 0), clip_cap), 4)
        return results

    # -- 本地文件同步 ---------------------------------------------------------------

    def sync_local_files(self, local_dir: str, family: Optional[str] = None) -> list[dict]:
        """扫描本地目录，返回每个 profile 的本地文件存在/匹配/缺失信息。

        local_dir 为空或不可用（不存在/不可读）时返回空列表，不抛错。
        """
        if not str(local_dir or "").strip():
            return []
        base_dir = Path(str(local_dir).strip())
        try:
            if not base_dir.exists() or not base_dir.is_dir():
                return []
        except Exception:
            return []
        result: list[dict[str, Any]] = []
        families = [family] if str(family or "").strip() else list(SUPPORTED_MODEL_FAMILIES)
        for fam in families:
            fam = str(fam or "").strip().lower()
            for key, profile in self.profiles_for(fam).items():
                if not str(profile.lora_name or "").strip():
                    continue
                found = self._find_local_lora_file(base_dir, profile.lora_name)
                result.append({
                    "key": key,
                    "character": profile.character,
                    "family": profile.family,
                    "lora_name": profile.lora_name,
                    "exists": found is not None,
                    "matched": str(found) if found else "",
                    "status": "present" if found else "missing",
                })
        return result

    # -- 多角色意图判定 -------------------------------------------------------------

    def has_multi_character_intent(self, raw_prompt: str, prompt: str = "") -> bool:
        """多角色意图判定（移植原插件 _default_illustrious_has_multi_character_intent）。"""
        return has_multi_character_intent(raw_prompt, prompt)

    # -- 内部 ---------------------------------------------------------------------

    def _build_selection_from_match(self, profile: LoraProfile, match: dict[str, Any]) -> LoraSelection:
        kind = str(match.get("match_kind") or "exact")
        term = str(match.get("term") or "")
        score = float(match.get("score") or 0)
        selection = LoraSelection(
            key=profile.key,
            lora_name=profile.lora_name,
            family=profile.family,
            character=profile.character or profile.key,
            matched_term=term,
            match_kind=kind,
            score=round(score, 2),
            trigger_words=list(profile.trigger_words),
            roles=list(profile.role_terms),
        )
        return self.resolve_strengths(selection, family=profile.family)

    # -- 匹配评分（移植原插件 _score_lora_profile_match / _best_lora_profile_match）--

    @staticmethod
    def _lora_profile_match_sort_key(match: dict[str, Any]) -> tuple:
        return (
            float(match.get("score") or 0),
            int(match.get("source_rank") or 0),
            int(match.get("marker_len") or 0),
            len(str(match.get("term") or "")),
        )

    @staticmethod
    def _profile_match_term_is_strong(term: str) -> bool:
        text = str(term or "").strip()
        marker = normalize_match_key(text)
        if not marker or is_generic_term(text):
            return False
        if re.fullmatch(r"\d+", marker):
            return False
        if re.fullmatch(r"v\d+(?:\d+)?", marker):
            return False
        if marker in {"il", "ixl", "xl", "sdxl", "ww", "wwil"}:
            return False
        if re.search(r"[㐀-鿿]", text):
            return len(marker) >= 2
        if re.search(r"[A-Za-z]", text):
            return len(marker) >= 4
        return len(marker) >= 4

    def _blocked_markers(self, profiles: dict[str, LoraProfile]) -> frozenset:
        """多个档案共享的非身份标记 → 屏蔽（避免别名歧义）。"""
        if not isinstance(profiles, dict) or not profiles:
            return frozenset()
        protected: set[str] = set()
        for key, profile in profiles.items():
            for value in (str(key), str(profile.character or "")):
                marker = normalize_match_key(value)
                if marker:
                    protected.add(marker)
        counts: dict[str, int] = {}
        for _key, profile in profiles.items():
            seen: set[str] = set()
            for term in self.profile_terms(profile, include_triggers=False):
                marker = normalize_match_key(term)
                if marker and marker not in seen:
                    seen.add(marker)
                    counts[marker] = counts.get(marker, 0) + 1
        return frozenset(marker for marker, count in counts.items() if count > 1 and (marker not in protected or len(marker)==1))

    def _profile_match_candidate_entries(
        self,
        key: str,
        profile: LoraProfile,
        blocked_markers: Optional[frozenset] = None,
    ) -> list[dict[str, Any]]:
        blocked_markers = blocked_markers or frozenset()
        identity_markers = {normalize_match_key(key), normalize_match_key(profile.character)}
        entries: dict[str, dict[str, Any]] = {}
        for term in self.profile_terms(profile, include_triggers=False):
            term = str(term or "").strip()
            marker = normalize_match_key(term)
            if not marker or marker in blocked_markers:
                continue
            if not self._profile_match_term_is_strong(term) and not self._explicit_single_cjk_alias(profile,term):
                continue
            source = "identity" if marker in identity_markers else "profile"
            source_rank = 4 if source == "identity" else 3
            current = entries.get(marker)
            if current and int(current.get("source_rank") or 0) >= source_rank:
                continue
            entries[marker] = {"term": term, "marker": marker, "source": source, "source_rank": source_rank}
        return list(entries.values())

    def _profile_match_marker_hits_text(self, marker: str, text_norm: str, raw_text: str, term: str) -> bool:
        if not marker or marker not in text_norm:
            return False
        if re.search(r"[㐀-鿿぀-ヿ]", str(term or "")):
            if len(marker)==1:
                # Require both token boundaries: 祀，... is explicit, 祭祀 is
                # an ordinary word and must never silently select this LoRA.
                raw = unicodedata.normalize('NFKC',str(raw_text or ''))
                return bool(re.search(rf'(?<!\w){re.escape(marker)}(?!\w)',raw))
            if len(marker) <= 2:
                return _short_cjk_marker_has_safe_boundary(marker, raw_text)
            return True
        term_ascii = re.sub(r"[^0-9a-z]+", " ", unicodedata.normalize("NFKC", str(term or "")).casefold()).strip()
        raw_ascii = re.sub(r"[^0-9a-z]+", " ", unicodedata.normalize("NFKC", str(raw_text or "")).casefold())
        if term_ascii and re.search(rf"(?<![0-9a-z]){re.escape(term_ascii)}(?![0-9a-z])", raw_ascii):
            return True
        return False  # An embedded substring is not an exact character mention.

    @staticmethod
    def _profile_ascii_prompt_fragments(text: str) -> set:
        ascii_text = re.sub(r"[^0-9a-z]+", " ", str(text or "").lower()).strip()
        tokens = [token for token in ascii_text.split() if len(token) >= 3]
        fragments = set(tokens)
        for size in (2, 3):
            if len(tokens) < size:
                continue
            for index in range(0, len(tokens) - size + 1):
                compact = "".join(tokens[index:index + size])
                spaced = " ".join(tokens[index:index + size])
                if 5 <= len(compact) <= 48:
                    fragments.add(compact)
                if 5 <= len(spaced) <= 56:
                    fragments.add(spaced.replace(" ", ""))
        return fragments

    def _profile_best_fuzzy_prompt_fragment(self, text: str, marker: str, term: str) -> tuple:
        marker = normalize_match_key(marker)
        if not marker:
            return 0.0, ""
        best_ratio = 0.0
        best_fragment = ""
        term_has_cjk = bool(re.search(r"[㐀-鿿぀-ヿ]", str(term or "")))
        if term_has_cjk:
            if len(marker) < 4:
                return 0.0, ""
            threshold = 0.74 if len(marker) <= 4 else 0.80
            for chunk in re.findall(r"[㐀-鿿぀-ヿ]+", str(text or "")):
                chunk_norm = normalize_match_key(chunk)
                if len(chunk_norm) < max(4, len(marker) - 1):
                    continue
                min_size = max(4, len(marker) - 1)
                max_size = min(len(chunk_norm), len(marker) + 1)
                for size in range(min_size, max_size + 1):
                    for index in range(0, len(chunk_norm) - size + 1):
                        fragment = chunk_norm[index:index + size]
                        ratio = difflib.SequenceMatcher(None, marker, fragment).ratio()
                        if ratio > best_ratio:
                            best_ratio = ratio
                            best_fragment = fragment
            return (best_ratio, best_fragment) if best_ratio >= threshold else (0.0, "")
        if len(marker) < 5:
            return 0.0, ""
        threshold = 0.88 if len(marker) >= 7 else 0.92
        max_len_delta = max(2, len(marker) // 4)
        for fragment in self._profile_ascii_prompt_fragments(text):
            if abs(len(fragment) - len(marker)) > max_len_delta:
                continue
            ratio = difflib.SequenceMatcher(None, marker, fragment).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_fragment = fragment
        return (best_ratio, best_fragment) if best_ratio >= threshold else (0.0, "")

    def _score_lora_profile_match_candidate(self, text: str, candidate: dict[str, Any]) -> dict[str, Any]:
        marker = str(candidate.get("marker") or "")
        term = str(candidate.get("term") or "")
        source = str(candidate.get("source") or "")
        source_rank = int(candidate.get("source_rank") or 0)
        text_norm = normalize_match_key(text)
        if not marker or not text_norm:
            return {}
        if self._profile_match_marker_hits_text(marker, text_norm, str(text or ""), term):
            return {
                "term": term,
                "source": source,
                "source_rank": source_rank,
                "match_kind": "exact",
                "score": float(100 + min(len(marker), 24) + source_rank),
                "marker_len": len(marker),
                "matched_fragment": term,
                "ratio": 1.0,
            }
        ratio, fragment = self._profile_best_fuzzy_prompt_fragment(str(text or ""), marker, term)
        if not ratio:
            return {}
        return {
            "term": term,
            "source": source,
            "source_rank": source_rank,
            "match_kind": "fuzzy",
            "score": float(76 + ratio * 12 + min(len(marker), 16) / 4 + source_rank),
            "marker_len": len(marker),
            "matched_fragment": fragment,
            "ratio": ratio,
        }

    def _score_lora_profile_match(
        self,
        text: str,
        key: str,
        profile: LoraProfile,
        blocked_markers: Optional[frozenset] = None,
    ) -> dict[str, Any]:
        best: dict[str, Any] = {}
        for candidate in self._profile_match_candidate_entries(key, profile, blocked_markers=blocked_markers):
            scored = self._score_lora_profile_match_candidate(text, candidate)
            if scored and (not best or self._lora_profile_match_sort_key(scored) > self._lora_profile_match_sort_key(best)):
                best = scored
        if best:
            best.update({"key": key, "profile": profile})
        return best

    # -- 坏词过滤（移植原插件 _is_lora_profile_bad_match_term 的轻量版）------------

    @staticmethod
    def _is_lora_quality_or_scene_tag(tag: str) -> bool:
        text = str(tag or "").strip().lower()
        words = {
            "masterpiece", "best quality", "ultra-detailed", "ultra detailed", "highres",
            "8k", "4k", "sharp focus", "anime style", "cinematic composition",
            "detailed shading", "vibrant colors", "soft lighting", "warm lighting",
            "atmospheric", "atmospheric depth", "background", "cafe", "wooden tables",
            "coffee cups", "standing", "sitting", "looking at viewer", "smile",
        }
        return any(word in text for word in words)

    @staticmethod
    def _is_lora_outfit_tag(tag: str) -> bool:
        text = str(tag or "").strip().lower()
        words = {
            "outfit", "clothes", "costume", "alternate costume", "official alternate costume",
            "dress", "robe", "uniform", "maid", "apron", "skirt",
            "shirt", "jacket", "coat", "pants", "shorts", "sleeves", "sleeve", "gloves",
            "boots", "shoes", "sandals", "stockings", "thighhighs", "pantyhose",
            "socks", "sash", "sashes", "belt", "top", "crop top", "midriff", "navel",
            "bare shoulders", "open jacket", "open clothes", "bra", "bikini",
            "swimsuit", "kimono", "armor", "hood", "animal hood", "hoodie", "sweater", "neckwear",
            "ascot", "necktie", "tie", "collar", "cape", "capelet", "cloak",
            "scarf", "shawl", "detached sleeves", "wide sleeves", "puffy sleeves",
            "sleeveless", "frilled sleeves", "ribbon-trimmed sleeves", "trimmed sleeves",
            "mary janes", "loafers", "heels", "pumps", "sneakers", "legwear",
        }
        return any(word in text for word in words)

    @staticmethod
    def _is_lora_appearance_tag(tag: str) -> bool:
        text = str(tag or "").strip().lower()
        words = {
            "hair", "eyes", "eye", "face", "features", "skin", "mole", "eyelashes",
            "horns", "ears", "tail", "wings", "ahoge", "ponytail", "twintails",
            "braid", "bangs", "sidelocks", "ribbons", "ribbon", "accessories",
            "accessory", "ornament", "earrings", "earring", "jewelry", "necklace",
            "flower-shaped", "flowers", "flower", "tassels", "tassel",
        }
        return any(word in text for word in words)

    def _is_bad_match_term(self, term: str) -> bool:
        text = str(term or "").strip()
        if not text:
            return True
        lowered = text.lower()
        compact = normalize_match_key(text)
        if re.search(r"(?:^|[\s_\-])(?:illu|illuxl|ixl|sdxl|xl|pony|noob|lora|locon|lycoris)(?:$|[\s_\-])", lowered):
            return True
        if re.search(r"(?:illu|illuxl|ixl|sdxl|pony|noob|lora)$", compact):
            return True
        if self._is_lora_quality_or_scene_tag(text) or self._is_lora_outfit_tag(text) or self._is_lora_appearance_tag(text):
            return True
        return False

    # -- 多角色连接符（移植原插件 _multi_lora_matches_have_connector）---------------

    @staticmethod
    def _multi_lora_matches_have_connector(text: str, matches: list) -> Optional[str]:
        """返回相邻命中之间的首个连接词文本；无则 None。"""
        if len(matches or []) < 2:
            return None
        raw = str(text or "")
        if not raw or not _CONNECTOR_RE.search(raw):
            return None
        spans = []
        for match in matches or []:
            profile = match.get("profile")
            candidates = [
                match.get("term"),
                profile.character if profile else "",
                match.get("key"),
            ]
            for candidate in candidates:
                term = str(candidate or "").strip()
                if not term:
                    continue
                found = re.search(re.escape(term), raw, re.I)
                if found:
                    spans.append((found.start(), found.end(), term))
                    break
        spans.sort(key=lambda item: item[0])
        for left, right in zip(spans, spans[1:]):
            between = raw[left[1]:right[0]]
            found = _CONNECTOR_RE.search(between)
            if found:
                return found.group(0)
        return None

    # -- 多角色触发词（移植原插件 _profile_effective_trigger_prompt / 路由）--------

    def _profile_effective_trigger_words(self, profile: LoraProfile) -> tuple:
        base = [str(x).strip() for x in profile.trigger_words if str(x or "").strip()]
        kept = [tag for tag in base if not is_generic_term(tag) and not self._is_bad_match_term(tag)]
        if kept:
            return kept, False
        fallback = [
            str(x).strip()
            for x in (profile.character, profile.key)
            if str(x or "").strip() and not is_generic_term(str(x or ""))
        ]
        return fallback, True

    def _profile_is_multi_character_pack(self, profile: LoraProfile) -> bool:
        extra = profile.extra or {}
        text = " ".join([
            str(profile.character or ""),
            str(extra.get("source_title") or ""),
            str(extra.get("source_version_title") or ""),
            str(extra.get("model_name") or ""),
            str(extra.get("version_name") or ""),
            Path(str(profile.lora_name or "")).stem,
        ])
        return bool(re.search(
            r"\b\d+\s*(?:in|x)\s*1\b|multi[-\s]*(?:character|char)|character\s*pack|"
            r"(?:character|char|lora)\s*collection|合集|多角色|角色包",
            text,
            flags=re.I,
        ))

    def _route_multi_character_trigger(self, trigger_tags: list, profile: LoraProfile, match_text: str = "") -> list:
        if not trigger_tags or not self._profile_is_multi_character_pack(profile):
            return trigger_tags
        tags = [t for t in trigger_tags if not is_generic_term(t) and not self._is_bad_match_term(t)]
        if len(tags) <= 1:
            return tags
        match_norm = normalize_match_key(match_text or "")
        best = None
        for tag in tags:
            marker = normalize_match_key(tag)
            if not marker or marker not in match_norm:
                continue
            if best is None or (len(marker), tag) > best:
                best = (len(marker), tag)
        return [best[1]] if best else tags

    # -- 本地文件搜索（移植原插件 _find_local_lora_file）---------------------------

    @staticmethod
    def _find_local_lora_file(base_dir: Path, lora_name: str) -> Optional[Path]:
        lora_name = str(lora_name or "").strip()
        if not lora_name:
            return None
        for candidate in (base_dir / lora_name, base_dir / Path(lora_name).name):
            try:
                if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
            except Exception:
                continue
        filename = Path(lora_name).name
        try:
            for item in base_dir.rglob(filename):
                try:
                    if item.is_file() and item.stat().st_size > 0:
                        return item
                except Exception:
                    continue
        except Exception:
            pass
        wanted = normalize_lora_key(lora_name)
        if wanted:
            best = None
            try:
                for item in base_dir.rglob("*.safetensors"):
                    try:
                        if not item.is_file() or item.stat().st_size <= 0:
                            continue
                        key = normalize_lora_key(item.name)
                        if not key or wanted not in key:
                            continue
                        rank = (0 if normalize_lora_key(item.stem) == wanted else 1, len(key))
                        if best is None or rank < best[0]:
                            best = (rank, item)
                    except Exception:
                        continue
            except Exception:
                pass
            if best is not None:
                return best[1]
        return None

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
