# -*- coding: utf-8 -*-
"""LoRA 身份/别名提取引擎：纯函数计算层，无网络与文件 IO。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

COMPACT_RE = re.compile(r"[^0-9a-z぀-ヿ㐀-鿿]+")

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

# 原插件 COMMON_CHARACTER_ALIASES 为空 dict；此处保留结构并内置常用角色别名样例。
COMMON_CHARACTER_ALIASES: dict[str, list[str]] = {
    "hatsune miku": ["初音ミク", "初音未来", "Miku"],
    "初音ミク": ["Hatsune Miku", "初音未来", "Miku"],
    "初音未来": ["Hatsune Miku", "初音ミク", "Miku"],
    "雷电将军": ["Raiden Shogun", "Ei", "影"],
    "raiden shogun": ["雷电将军", "Ei", "影"],
    "八重神子": ["Yae Miko", "Yae"],
    "yae miko": ["八重神子", "Yae"],
    "神里绫华": ["Kamisato Ayaka", "Ayaka"],
    "甘雨": ["Ganyu"],
    "胡桃": ["Hu Tao"],
    "刻晴": ["Keqing"],
    "芙宁娜": ["Furina"],
    "那维莱特": ["Neuvillette"],
    "阿蕾奇诺": ["Arlecchino"],
    "纳西妲": ["Nahida"],
    "夜兰": ["Yelan"],
    "绫波丽": ["Rei Ayanami", "Ayanami Rei", "綾波レイ"],
    "明日香": ["Asuka Langley Soryu", "Asuka"],
    "鹿目圆": ["Madoka Kaname", "Madoka"],
    "阿尔托莉雅": ["Artoria Pendragon", "Saber"],
    "玛修": ["Mash Kyrielight", "Mash"],
    "后藤一里": ["Hitori Gotoh", "Bocchi"],
    "喜多郁代": ["Ikuyo Kita", "Kita"],
    "四宫辉夜": ["Kaguya Shinomiya", "Kaguya"],
    "玛奇玛": ["Makima"],
    "五条悟": ["Satoru Gojo", "Gojo"],
    "祢豆子": ["Nezuko"],
    "灶门炭治郎": ["Tanjiro Kamado", "Tanjiro"],
    "高坂穗乃果": ["Honoka Kosaka", "Honoka"],
    "南小鸟": ["Kotori Minami", "Kotori"],
}

# 来源别名词表：从原插件 lora_source_aliases.json 节选的常用作品/IP 词条，用于标题解析时识别来源片段。
SOURCE_ALIAS_TERMS: frozenset[str] = frozenset({
    "鸣潮", "wuthering waves", "wuwa", "鳴潮",
    "原神", "genshin impact",
    "崩坏", "崩坏3", "honkai impact 3rd", "崩坏学园2", "guns girl z", "ggz",
    "崩坏星穹铁道", "崩坏：星穹铁道", "honkai star rail", "hsr", "星穹铁道",
    "绝区零", "zenless zone zero", "zzz",
    "异环", "neverness to everness", "nte",
    "二重螺旋", "duet night abyss",
    "蔚蓝档案", "blue archive", "碧蓝档案", "ブルーアーカイブ", "ba",
    "明日方舟", "arknights", "アークナイツ", "方舟",
    "明日方舟：终末地", "arknights: endfield", "终末地",
    "少女前线", "girls' frontline", "gfl", "少前",
    "少女前线2：追放", "girls' frontline 2: exilium", "gfl2",
    "尘白禁区", "snowbreak: containment zone", "snowbreak",
    "赛马娘", "umamusume", "uma musume pretty derby", "马娘",
    "命运冠位指定", "fate grand order", "fgo", "fate", "fate stay night",
    "fate/grand order",
    "蓝色星原：旅谣", "azur promilia",
    "幻兽帕鲁", "palworld",
    "绝地潜兵2", "helldivers 2",
    "永劫无间", "naraka: bladepoint",
    "我的世界", "minecraft",
    "剑网3", "jx3",
    "逆水寒", "justice online",
    "物华弥新", "resonance of the ocean",
    "边狱公司", "limbus company",
    "主播女孩重度依赖", "needy streamer overload", "超天酱",
    "葬送的芙莉莲", "sousou no frieren", "frieren beyond journey's end", "葬送のフリーレン", "葬送的芙莉蓮",
    "赛博朋克2077", "cyberpunk 2077", "赛博朋克", "cyberpunk",
    "赛博朋克边缘行者", "cyberpunk edgerunners",
    "可塑性记忆", "plastic memories",
    "clannad",
    "美少女万华镜", "bishoujo mangekyou",
    "沙耶之歌", "saya no uta",
    "寒蝉鸣泣之时", "higurashi when they cry",
    "海猫鸣泣之时", "umineko when they cry",
    "魔法使之夜", "witch on the holy night",
    "月姬", "tsukihime",
    "空之境界", "kara no kyoukai", "the garden of sinners",
    "传颂之物", "utawarerumono",
    "刀剑神域", "sword art online", "sao",
    "进击的巨人", "attack on titan",
    "我的英雄学院", "my hero academia",
    "鬼灭之刃", "demon slayer", "kimetsu no yaiba",
    "间谍过家家", "spy x family",
    "re从零开始", "re:zero", "re zero", "re:从零开始的异世界生活",
    "无职转生", "mushoku tensei",
    "孤独摇滚", "bocchi the rock",
    "莉可丽丝", "lycoris recoil",
    "辉夜大小姐", "kaguya-sama",
    "电锯人", "chainsaw man",
    "咒术回战", "jujutsu kaisen",
    "物语系列", "monogatari",
    "公主连结", "princess connect", "priconne",
    "碧蓝航线", "azur lane",
    "少女歌剧", "revue starlight",
    "nikke", "goddess of victory nikke",
    "尼尔", "nier automata", "nier",
    "战双帕弥什", "punishing: gray raven", "pgr",
    "深空之眼", "aether gazer",
    "无期迷途", "path to nowhere",
    "重返未来1999", "reverse: 1999",
    "第七史诗", "epic seven",
    "碧蓝幻想", "granblue fantasy", "gbf",
    "少女乐团派对", "bang dream! girls band party!", "bang dream!", "邦邦",
    "世界计划", "project sekai: colorful stage! feat. hatsune miku", "pjsk", "世界计划 彩色舞台 feat. 初音未来",
    "初音未来", "hatsune miku", "miku",
    "vocaloid",
    "东方project", "touhou project", "东方",
    "舰队collection", "kancolle", "舰队收藏",
    "刀剑乱舞", "touken ranbu",
    "我推的孩子", "oshi no ko", "推しの子",
    "药屋少女的呢喃", "the apothecary diaries",
    "迷宫饭", "delicious in dungeon", "dungeon meshi",
    "更衣人偶坠入爱河", "my dress-up darling",
    "魔法少女小圆", "puella magi madoka magica", "madoka magica",
    "新世纪福音战士", "neon genesis evangelion", "eva",
    "紫罗兰永恒花园", "violet evergarden",
    "你的名字。", "your name.",
    "天气之子", "weathering with you",
    "轻音少女", "k-on!",
    "凉宫春日的忧郁", "the melancholy of haruhi suzumiya",
    "冰菓", "hyouka",
    "吹响吧上低音号", "sound! euphonium",
    "日常", "nichijou",
    "幸运星", "lucky star",
    "龙与虎", "toradora!",
    "某科学的超电磁炮", "a certain scientific railgun",
    "魔法禁书目录", "a certain magical index",
    "加速世界", "accel world",
    "游戏人生", "no game no life",
    "黑执事", "black butler",
    "夏目友人帐", "natsume's book of friends",
    "狼与香辛料", "spice and wolf",
    "命运石之门", "steins;gate",
    "来自深渊", "made in abyss",
    "为美好的世界献上祝福", "konosuba",
    "不死者之王", "overlord",
    "约会大作战", "date a live",
    "魔法科高中的劣等生", "the irregular at magic high school",
    "darling in the franxx",
    "天元突破", "gurren lagann",
    "斩服少女", "kill la kill",
    "机动战士高达", "mobile suit gundam", "高达",
    "时光代理人", "link click",
    "死亡笔记", "death note",
    "叛逆的鲁路修", "code geass", "鲁路修",
    "钢之炼金术师", "fullmetal alchemist",
    "银魂", "gintama",
    "jojo的奇妙冒险", "jojo's bizarre adventure", "jojo",
    "海贼王", "one piece",
    "火影忍者", "naruto",
    "死神", "bleach",
    "龙珠", "dragon ball",
    "名侦探柯南", "detective conan",
    "灌篮高手", "slam dunk",
    "游戏王", "yu-gi-oh!",
    "全职猎人", "hunter x hunter",
    "浪客剑心", "rurouni kenshin",
    "塞尔达传说", "the legend of zelda",
    "宝可梦", "pokémon",
    "超级马里奥", "super mario",
    "怪物猎人", "monster hunter",
    "最终幻想", "final fantasy",
    "勇者斗恶龙", "dragon quest",
    "王国之心", "kingdom hearts",
    "女神异闻录", "persona",
    "女神异闻录5", "persona 5",
    "如龙", "yakuza", "like a dragon",
    "生化危机", "resident evil",
    "街头霸王", "street fighter",
    "拳皇", "the king of fighters",
    "鬼泣", "devil may cry",
    "只狼：影逝二度", "sekiro: shadows die twice",
    "艾尔登法环", "elden ring",
    "黑暗之魂", "dark souls",
    "血源诅咒", "bloodborne",
    "对马岛之魂", "ghost of tsushima",
    "巫师", "the witcher",
    "刺客信条", "assassin's creed",
    "黑神话：悟空", "black myth: wukong",
    "英雄联盟", "league of legends",
    "无畏契约", "valorant",
    "apex英雄", "apex legends",
    "阴阳师", "onmyoji",
    "第五人格", "identity v",
    "无限暖暖", "infinity nikki",
    "恋与深空", "love and deepspace",
    "卡拉彼丘", "strinova",
    "雀魂", "mahjong soul",
    "星际战甲", "warframe",
    "少女与战车", "girls und panzer",
    "强袭魔女", "strike witches",
    "星际牛仔", "cowboy bebop",
    "攻壳机动队", "ghost in the shell",
    "千与千寻", "spirited away",
    "love live!", "love live! school idol project",
    "偶像大师", "the idolm@ster",
    "少女终末旅行", "girls' last tour",
    "摇曳露营", "yuru camp",
})


@dataclass
class IdentityCandidates:
    """身份候选结果：主角色、别名、置信度与打分依据。"""
    character: str
    aliases: list[str]
    confidence: float
    reasons: list[str]


def normalize_key(text: str) -> str:
    """归一化关键词：小写并压缩空白。"""
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def is_generic_alias_word(text: str) -> bool:
    """判断是否为通用/无信息量的别名词。"""
    raw = normalize_key(text)
    compact = COMPACT_RE.sub("", raw)
    generic = {COMPACT_RE.sub("", x) for x in GENERIC_ALIAS_WORDS}
    if not compact or compact in generic:
        return True
    if re.fullmatch(r"\d+(girl|boy|girls|boys|person|people)", compact):
        return True
    if re.fullmatch(r"(black|white|blue|red|green|gold|blonde|silver|pink|purple|brown)(hair|eyes)", compact):
        return True
    if re.search(r"[぀-ヿ㐀-鿿]", raw):
        return len(compact) < 2
    return len(compact) < 4


def _looks_like_title_source_fragment(value: str) -> bool:
    text = normalize_key(value)
    compact = COMPACT_RE.sub("", text)
    for marker in SOURCE_ALIAS_TERMS:
        marker_norm = normalize_key(marker)
        marker_compact = COMPACT_RE.sub("", marker_norm)
        if marker_norm and marker_norm in text:
            return True
        if marker_compact and marker_compact == compact:
            return True
    return False


def _strip_title_descriptors(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""

    def strip_bracket(match):
        inner = str(match.group(1) or "").strip()
        lowered = inner.lower()
        if (
            _looks_like_title_source_fragment(inner)
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


def _is_bad_alias_candidate(item: str, original_title: str = "") -> bool:
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
    compact = COMPACT_RE.sub("", lowered)
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


def _dedupe_values(values: list) -> list:
    result = []
    seen = set()
    for item in values or []:
        item = str(item or "").strip()
        if not item:
            continue
        marker = COMPACT_RE.sub("", item.lower())
        if not marker or marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _alias_looks_source_prefixed(value: str) -> bool:
    raw = re.sub(r"\s+", " ", str(value or "")).strip(" -_|｜丨／/⁄:：")
    pieces = [x.strip(" -_/\\／⁄:：") for x in re.split(r"_+", raw) if x.strip(" -_/\\／⁄:：")]
    return len(pieces) >= 2 and _looks_like_title_source_fragment(" ".join(pieces[:-1]))


def _title_part_character_candidates(value: str, original_title: str = "") -> list:
    raw = _strip_title_descriptors(value)
    if not raw:
        return []
    pieces = [x.strip(" -_/\\／⁄:：") for x in re.split(r"_+", raw) if x.strip(" -_/\\／⁄:：")]
    candidates = []
    if len(pieces) >= 2:
        prefix = " ".join(pieces[:-1])
        tail = pieces[-1]
        if _looks_like_title_source_fragment(prefix):
            candidates.append(tail)
        else:
            candidates.extend([raw, tail])
    else:
        candidates.append(raw)
    result = []
    for item in candidates:
        item = re.sub(r"\s+", " ", str(item or "")).strip()
        if item and not _is_bad_alias_candidate(item, original_title=original_title):
            result.append(item[:80])
    return result


def _title_character_candidates(title: str) -> list:
    text = str(title or "").strip()
    if not text:
        return []
    text = re.sub(r"^[\s【\[][^\]】]{0,80}[\]】]\s*", "", text)
    text = _strip_title_descriptors(text)
    text = re.sub(r"\s+", " ", text).strip(" -_|｜丨／/⁄:：")
    candidates = []
    for part_index, part in enumerate(re.split(r"\s*(?:[|｜丨／/⁄]|\s+-\s+|\s+—\s+|\s+–\s+)\s*", text)):
        part = part.strip(" -_|｜丨／/⁄:：")
        if not part:
            continue
        if re.fullmatch(r"(?:[0-9A-Za-z]+)?[―—].+[―—]", part.strip()):
            continue
        if candidates and part_index > 0:
            if _alias_looks_source_prefixed(part):
                pass
            elif _looks_like_title_source_fragment(part):
                continue
            elif re.search(r"[㐀-鿿぀-ヿ]", part):
                pass
            else:
                continue
        subparts = [x.strip(" -_/\\／⁄:：") for x in re.split(r"\s*[／/⁄]\s*", part) if x.strip()]
        for sub in subparts or [part]:
            candidates.extend(_title_part_character_candidates(sub, original_title=title))
    return _dedupe_values(candidates)


def _title_character_candidate(title: str) -> str:
    candidates = _title_character_candidates(title)
    return candidates[0] if candidates else ""


def _to_summary(profile: dict) -> dict:
    trained_words = profile.get("trained_words") or []
    if isinstance(trained_words, str):
        trained_words = re.split(r"[,，;；\n\r]+", trained_words)
    return {
        "model_name": str(profile.get("name") or profile.get("model_name") or ""),
        "version_name": str(profile.get("version_name") or ""),
        "file_name": str(profile.get("file_name") or ""),
        "trained_words": list(trained_words),
        "character_name": str(profile.get("character_name") or profile.get("character") or ""),
        "aliases": list(profile.get("aliases") or []),
        "search_query": str(profile.get("search_query") or ""),
    }


def _identity_candidates(summary: dict, alias: str = "") -> list:
    model_name = str(summary.get("model_name") or "").strip()
    version_name = str(summary.get("version_name") or "").strip()
    file_name = str(summary.get("file_name") or "").strip()
    trained_words = summary.get("trained_words") or []
    raw_candidates = [
        ("llm_character", summary.get("character_name"), 100),
        ("model_title", _title_character_candidate(model_name), 85),
        ("version_title", _title_character_candidate(version_name), 70),
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
        if _is_bad_alias_candidate(value, original_title=model_name):
            continue
        marker = COMPACT_RE.sub("", value.lower())
        if not marker or marker in seen:
            continue
        seen.add(marker)
        result.append({"source": source, "value": value, "score": score})
    return result


def clean_alias(name: str) -> str:
    """清洗别名：去引号/括号内容/版本后缀/多余空白，空串返回空。"""
    text = str(name or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[\'\"]+|[\'\"]+$", "", text)
    text = _strip_title_descriptors(text)
    text = re.sub(r"\s+(?:ver(?:sion)?\s*\d+(?:\.\d+)*|v\d+(?:\.\d+)*|ver(?:sion)?)\s*$", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -_|｜丨／/⁄:：")


def extract_identity_candidates(profile: dict) -> IdentityCandidates:
    """提取身份候选：输出主角色、别名、置信度与打分依据。"""
    summary = _to_summary(profile or {})
    candidates = _identity_candidates(summary)
    if not candidates:
        return IdentityCandidates(character="", aliases=[], confidence=0.0, reasons=[])
    character = clean_alias(candidates[0]["value"])
    alias_values = [clean_alias(x) for x in ([candidates[0]["value"]] + [c["value"] for c in candidates])]
    aliases = _dedupe_values([a for a in alias_values if a])
    confidence = min(1.0, (candidates[0]["score"] + 5.0 * max(0, len(candidates) - 1)) / 100.0)
    reasons = [f"{c['source']}: {c['value']} ({c['score']})" for c in candidates]
    return IdentityCandidates(
        character=character,
        aliases=aliases,
        confidence=round(confidence, 3),
        reasons=reasons,
    )


def _expand_aliases(aliases: list) -> list:
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


def expand_aliases(aliases: list[str], profile_name: str, trained_words: list[str]) -> list[str]:
    """展开别名：合并别名、角色别名表扩展与非平凡触发词，去重。"""
    seed = [str(a or "").strip() for a in (aliases or [])]
    if profile_name:
        seed.extend(_title_character_candidates(profile_name))
    trained = trained_words or []
    if isinstance(trained, str):
        trained = re.split(r"[,，;；\n\r]+", trained)
    for word in trained:
        word = str(word or "").strip()
        if word and not is_generic_alias_word(word):
            seed.append(word)
    return _expand_aliases(seed)


def _profile_identity_score(profile: dict) -> float:
    evidence = profile.get("identity_evidence") or []
    if isinstance(evidence, list):
        scores = []
        for entry in evidence:
            if isinstance(entry, dict) and entry.get("score") is not None:
                try:
                    scores.append(float(entry["score"]))
                except (TypeError, ValueError):
                    pass
        if scores:
            return max(scores)
    if profile.get("character"):
        return 45.0
    return -1.0


def _confidence_from_candidates(candidates: list) -> float:
    if not candidates:
        return 0.0
    top = candidates[0]["score"]
    return min(1.0, (top + 5.0 * max(0, len(candidates) - 1)) / 100.0)


def upsert_merge(base: dict, incoming: dict) -> dict:
    """合并身份档案：身份打分更高者胜出。"""
    base_profile = dict(base or {})
    incoming = dict(incoming or {})
    summary = _to_summary(incoming)
    candidates = _identity_candidates(summary)
    incoming_top = candidates[0]["score"] if candidates else -1
    base_top = _profile_identity_score(base_profile)
    incoming_wins = incoming_top > base_top

    if incoming_wins:
        character = clean_alias(candidates[0]["value"]) if candidates else ""
        evidence = candidates[:6]
        confidence = _confidence_from_candidates(candidates)
    else:
        character = str(base_profile.get("character") or "").strip()
        evidence = base_profile.get("identity_evidence") or []
        if isinstance(evidence, list):
            evidence = evidence[:6]
        confidence = base_profile.get("confidence")
        if confidence is None:
            confidence = max(0.0, base_top / 100.0) if base_top > 0 else 0.0
        else:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.0

    alias_seed = list(base_profile.get("aliases") or []) + list(summary.get("aliases") or [])
    if incoming_wins:
        alias_seed += [c["value"] for c in candidates]
    if character:
        alias_seed.append(character)
    aliases = [a for a in _dedupe_values([clean_alias(a) for a in alias_seed]) if a]

    model_name = summary["model_name"] or str(base_profile.get("source_title") or base_profile.get("name") or "")
    version_name = summary["version_name"] or str(base_profile.get("source_version_name") or "")

    return {
        "lora": summary["file_name"] or str(base_profile.get("lora") or ""),
        "character": character,
        "aliases": aliases,
        "identity_evidence": evidence,
        "confidence": round(min(1.0, max(0.0, confidence)), 3),
        "source_title": model_name,
        "source_version_name": version_name,
        "source": str(incoming.get("source") or base_profile.get("source") or ""),
        "name": model_name,
    }
