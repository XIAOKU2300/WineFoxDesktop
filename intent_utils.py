import re


_CJK_NAME_TOKEN = r"[\u3400-\u9fff]{2,8}"
_LATIN_NAME_TOKEN = r"[A-Z][A-Za-z0-9_.-]{1,40}(?:\s+[A-Z][A-Za-z0-9_.-]{1,40}){0,2}"
_NAME_TOKEN = rf"(?:{_CJK_NAME_TOKEN}|{_LATIN_NAME_TOKEN})"
_PAIR_CONNECTOR = r"(?:还有|還有|以及|并|並|和|跟|与|與|及|同|、|，|,|&|\+|/|[Vv][Ss]\.?|[Aa][Nn][Dd]|[Ww][Ii][Tt][Hh])"

_CJK_NON_NAME_FRAGMENT = re.compile(
    r"(自己|自身|本人|一只|一隻|另一|另只|另隻|两只|兩隻|双|雙|"
    r"手|手臂|胳膊|手指|指尖|胸|乳|奶|嘴|口|头|頭|发|髮|腿|脚|腳|足|腰|臀|腹|"
    r"抱|抬|举|舉|咬|送|拿|扶|托|摸|抓|握|用力|亲|親|吻|贴|貼|靠|"
    r"的|了|着|著|把|被|给|給|到|在|向|从|從|下|上|边|邊|一边|一邊)"
)

_CJK_LITERAL_NON_NAME_FRAGMENT = re.compile(
    "(?:"
    "\u81ea\u5df1|\u81ea\u8eab|\u672c\u4eba|\u5355\u4eba|\u55ae\u4eba|"
    "\u72ec\u81ea|\u7368\u81ea|\u5168\u8eab|\u534a\u8eab|\u6b63\u9762|"
    "\u80cc\u9762|\u4fa7\u9762|\u5c55\u793a|\u5b8c\u6574|\u8db3\u5e95|"
    "\u811a\u5e95|\u811a\u638c|\u624b|\u624b\u6307|\u624b\u81c2|"
    "\u80f3\u818a|\u817f|\u5c0f\u817f|\u5927\u817f|\u80f8|\u4e73|"
    "\u5934\u53d1|\u982d\u9aee|\u5e3d\u5b50|\u6d77\u8fb9|\u6d77\u908a|"
    "\u6c99\u6ee9|\u6c99\u7058|\u5730\u7262|\u6cf3\u88c5|\u6cf3\u88dd|"
    "\u6bd4\u57fa\u5c3c|\u4e00\u53ea|\u4e00\u96bb|\u53e6\u4e00\u53ea|"
    "\u53e6\u4e00\u96bb|\u5de6\u624b|\u53f3\u624b|\u8eab\u4f53|\u8eab\u9ad4"
    ")"
)

_LATIN_NON_NAME_WORDS = {
    "a", "an", "and", "another", "arm", "arms", "breast", "breasts", "body",
    "boy", "character", "characters", "face", "finger", "fingers", "foot",
    "girl", "girls", "hand", "hands", "hair", "head", "her", "herself", "him",
    "himself", "holding", "hugging", "left", "leg", "legs", "mouth", "nipple",
    "one", "other", "own", "person", "right", "self", "side", "the", "their",
    "them", "toward", "two", "with",
}

_EXPLICIT_MULTI_COUNT_RE = re.compile(
    r"\b(?:[2-9]\s*(?:girls?|boys?|people|persons?|characters?)|"
    r"(?:two|three|four|five|six|seven|eight|nine)\s+(?:girls?|boys?|people|persons?|characters?)|"
    r"(?:one|a)\s+(?:girl|boy|woman|man|person|character)\s+(?:and|with)\s+(?:one|a)\s+(?:girl|boy|woman|man|person|character)|"
    r"(?:girl|boy|woman|man|person|character)\s+(?:and|with)\s+(?:girl|boy|woman|man|person|character)|"
    r"multiple\s+(?:girls?|boys?|people|characters?)|group|crowd|duo|pair|couple|twins?)\b",
    re.I,
)

_CJK_EXPLICIT_MULTI_RE = re.compile(
    r"(多人|双人|雙人|俩人|倆人|俩个人|倆個人|仨人|仨个人|兩人|两人|三人|四人|五人|"
    r"多个角色|多個角色|多角色|双角色|雙角色|双女主|雙女主|双男主|雙男主|两女|兩女|二女|两男|兩男|二男|"
    r"群像|一群|同框|同屏|同画面|同畫面|合照|合影|双胞胎|雙胞胎|姐妹|兄弟|情侣|情侶|搭档|搭檔|二人组|二人組)"
)

_CJK_MULTI_COUNT_RE = re.compile(
    r"(?:"
    r"(?:2|3|4|5|6|7|8|9)\s*(?:个|個|位|名|只)?|"
    r"俩|倆|仨|两|兩|二|三|四|五|六|七|八|九|两个|兩個|二个|二個|两位|兩位|二位|两名|兩名|二名|"
    r"三个|三個|三位|三名|四个|四個|四位|四名"
    r").{0,4}(人|角色|人物|女孩|少女|男孩|女生|男生|女人|男人|女孩子|男孩子|girl|girls|boy|boys)"
)

_CJK_MIXED_GENDER_PAIR_RE = re.compile(
    r"(一男一女|一女一男|一男和一女|一女和一男|男生女生|女生男生|男女二人|男女两人|男女兩人|一对男女|一對男女)"
)

_CJK_PLURAL_ACTION_RE = re.compile(
    r"(她们|她們|他们|他們|TA们|ta们|两人|兩人|二人|双方|雙方|两位|兩位).{0,18}"
    r"(互动|互動|对视|對視|牵手|牽手|拥抱|擁抱|贴贴|貼貼|亲吻|親吻|接吻|靠在一起|抱在一起|并肩|並肩|背靠背|面对面|面對面)"
)

_ROLE_SLOT_RE = re.compile(
    r"(?:(?:角色|人物|人设|人設|role|character|char)\s*([A-Ca-c1-3一二三①②③])|"
    r"\b([A-Ca-c])\s*(?:角色|人物|role|character|char)\b|"
    r"(?:^|[\n,，;；])\s*(?:角色\s*)?([A-Ca-c1-3一二三①②③])\s*[:：])",
    re.I,
)

_SIDE_ROLE_RE = re.compile(
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

_SELF_SUBJECT_RE = re.compile(
    r"(自己|自身|本人|自分|\bown\b|\boneself\b|\bmyself\b|\bherself\b|\bhimself\b|\bitself\b|\bself[-\s])",
    re.I,
)

_EXPLICIT_OTHER_SUBJECT_RE = re.compile(
    r"(别人|別人|他人|另一(?:个|個|位)?(?:人|角色|女孩|少女|男孩|女生|男生)|"
    r"\b(?:another|other|second|third)\s+(?:girl|boy|person|character|woman|man)\b)",
    re.I,
)

_BODY_PART_PAIR_RE = re.compile(
    r"(?:一只|一隻|另一只|另一隻|左|右|left|right|one|other)?\s*"
    r"(?:手|手臂|胳膊|腿|脚|腳|眼|胸|乳|奶|hand|arm|leg|foot|eye|breast)\s*"
    rf"{_PAIR_CONNECTOR}\s*"
    r"(?:一只|一隻|另一只|另一隻|左|右|left|right|one|other)?\s*"
    r"(?:手|手臂|胳膊|腿|脚|腳|眼|胸|乳|奶|hand|arm|leg|foot|eye|breast)",
    re.I,
)

_RELATIONAL_MULTI_RE = re.compile(
    r"(和|跟|与|與|同).{1,24}(一起|同框|合照|并排|並排|互动|互動|拥抱|擁抱|牵手|牽手|聚餐|吃饭|吃飯|聊天|约会|約會)|"
    r"(一起|同框|合照|并排|並排|互动|互動|拥抱|擁抱|牵手|牽手|聚餐|吃饭|吃飯|聊天|约会|約會).{0,24}(和|跟|与|與|同)"
)

_GENERIC_PAIR_CONNECTOR_RE = re.compile(
    r"(?:\b(?:and|with|plus|vs\.?|versus|x)\b|[&+/,;]|"
    "\uff0c|\u3001|\uff1b|\u548c|\u8ddf|\u4e0e|\u8207|\u540c|\u53ca|"
    "\u4ee5\u53ca|\u8fd8\u6709|\u9084\u6709|\u5e76|\u4e26)",
    re.I,
)

_SUBJECT_NOUN_RE = re.compile(
    r"\b(?:girl|girls|boy|boys|woman|women|man|men|person|people|character|"
    r"characters|oc|ocs|role|roles|maid|idol|student|sister|brother|friend|"
    r"partner|companion|teammate|knight|princess|witch|angel|demon)\b|"
    "(?:\u5973\u5b69|\u5c11\u5973|\u5973\u751f|\u7537\u5b69|\u5c11\u5e74|"
    "\u7537\u751f|\u5973\u4eba|\u7537\u4eba|\u4eba\u7269|\u89d2\u8272|"
    "\u4eba\u8bbe|\u5973\u4ec6|\u5076\u50cf|\u5b66\u751f|\u59d0\u59d0|"
    "\u59b9\u59b9|\u54e5\u54e5|\u5f1f\u5f1f|\u670b\u53cb|\u4f19\u4f34|"
    "\u961f\u53cb|\u9a91\u58eb|\u516c\u4e3b|\u9b54\u5973|\u5929\u4f7f|"
    "\u6076\u9b54|OC|oc)",
    re.I,
)

_APPEARANCE_WORD_RE = (
    r"(?:white[-\s]*hair(?:ed)?|black[-\s]*hair(?:ed)?|blonde|silver[-\s]*hair(?:ed)?|red[-\s]*hair(?:ed)?|"
    r"blue[-\s]*hair(?:ed)?|green[-\s]*hair(?:ed)?|pink[-\s]*hair(?:ed)?|purple[-\s]*hair(?:ed)?|long[-\s]*hair(?:ed)?|"
    r"short[-\s]*hair(?:ed)?|twintails?|braid|cat\s*ears?|rabbit\s*ears?|bunny\s*ears?|"
    r"fox\s*ears?|horns?|halo|wings?|tail|glasses|hat|ribbon|dress|uniform|"
    r"school\s*uniform|maid|armor|cloak)"
)
_CJK_APPEARANCE_WORD_RE = (
    "\u767d\u53d1|\u9ed1\u53d1|\u91d1\u53d1|\u94f6\u53d1|\u7ea2\u53d1|"
    "\u84dd\u53d1|\u7eff\u53d1|\u7c89\u53d1|\u7d2b\u53d1|\u957f\u53d1|"
    "\u77ed\u53d1|\u53cc\u9a6c\u5c3e|\u8fab\u5b50|\u732b\u8033|"
    "\u5154\u8033|\u72d0\u8033|\u89d2|\u5149\u73af|\u7fc5\u8180|"
    "\u5c3e\u5df4|\u773c\u955c|\u5e3d|\u4e1d\u5e26|\u875c\u8776\u7ed3|"
    "\u88d9|\u5236\u670d|\u6821\u670d|\u5973\u4ec6|\u76d4\u7532|\u62ab\u98ce|"
    "\u5de6\u8fb9|\u53f3\u8fb9|\u5de6\u4fa7|\u53f3\u4fa7|\u53e6\u4e00\u4e2a|"
    "\u7b2c\u4e8c\u4e2a|\u524d\u9762|\u540e\u9762"
)
_APPEARANCE_SUBJECT_RE = re.compile(
    rf"(?:{_APPEARANCE_WORD_RE}[^\n\r,;\+&/\uff0c\u3001\uff1b]{{0,32}}(?:girl|boy|woman|man|person|character|oc|role|maid|idol|student|friend|partner))|"
    f"(?:(?:{_CJK_APPEARANCE_WORD_RE})[^\n\r,;\\+&/\uff0c\u3001\uff1b]{{0,18}}(?:\u5973\u5b69|\u5c11\u5973|\u5973\u751f|\u7537\u5b69|\u5c11\u5e74|\u7537\u751f|\u4eba\u7269|\u89d2\u8272|\u4eba\u8bbe|\u670b\u53cb|\u4f19\u4f34|OC|oc))",
    re.I,
)

_SPATIAL_ROLE_MARKER_RE = re.compile(
    r"\b(?:left|right|front|back|behind|beside|next\s+to|foreground|background|middle|center)\b|"
    "(?:\u5de6\u8fb9|\u53f3\u8fb9|\u5de6\u4fa7|\u53f3\u4fa7|\u5de6\u9762|"
    "\u53f3\u9762|\u524d\u9762|\u540e\u9762|\u65c1\u8fb9|\u8eab\u8fb9|"
    "\u524d\u666f|\u80cc\u666f|\u4e2d\u95f4|\u4e2d\u592e|\u53e6\u4e00\u8fb9)",
    re.I,
)

_SHARED_ACTION_RE = re.compile(
    r"\b(?:together|interacting|interaction|facing\s+each\s+other|looking\s+at\s+each\s+other|"
    r"holding\s+hands|hugging|embracing|cuddling|kissing|talking|chatting|sitting\s+together|"
    r"standing\s+together|posing\s+together|fighting|dancing|sharing|beside\s+each\s+other)\b|"
    "(?:\u4e00\u8d77|\u540c\u6846|\u540c\u5c4f|\u5408\u7167|\u4e92\u52a8|"
    "\u4e92\u52d5|\u5bf9\u89c6|\u5c0d\u8996|\u7275\u624b|\u64c1\u62b1|"
    "\u62e5\u62b1|\u6402|\u8d34\u8d34|\u4eb2\u543b|\u63a5\u543b|"
    "\u5e76\u80a9|\u4e26\u80a9|\u9762\u5bf9\u9762|\u9762\u5c0d\u9762|"
    "\u804a\u5929|\u805a\u9910|\u805a\u9910|\u7ea6\u4f1a|\u7d04\u6703|"
    "\u6253\u67b6|\u8df3\u821e|\u5bf9\u6253|\u5c0d\u6253)",
    re.I,
)

_PLURAL_REFERENCE_RE = re.compile(
    r"\b(?:they|them|their|both|each\s+other|two\s+of\s+them|all\s+of\s+them)\b|"
    "(?:\u5979\u4eec|\u5979\u5011|\u4ed6\u4eec|\u4ed6\u5011|TA\u4eec|ta\u4eec|"
    "\u4e24\u4eba|\u5169\u4eba|\u4e8c\u4eba|\u4fe9\u4eba|\u4e24\u4f4d|"
    "\u5169\u4f4d|\u53cc\u65b9|\u96d9\u65b9|\u5f7c\u6b64)",
    re.I,
)

_SINGLE_SUBJECT_STRONG_RE = re.compile(
    r"\b(?:solo|alone|single\s+(?:person|character|subject)|one\s+(?:girl|boy|person|character)\s+only|1girl|1boy)\b|"
    "(?:\u5355\u4eba|\u55ae\u4eba|\u72ec\u81ea|\u7368\u81ea|\u4e00\u4e2a\u4eba|"
    "\u4e00\u500b\u4eba|\u4e00\u4eba|\u5355\u72ec|\u55ae\u7368)",
    re.I,
)

_LATIN_NAME_PAIR_RE = re.compile(
    r"\b(?P<left>[A-Za-z][A-Za-z0-9_.-]{2,40})\s+"
    r"(?:and|with|plus|vs\.?|versus|x|/|&|\+)\s+"
    r"(?P<right>[A-Za-z][A-Za-z0-9_.-]{2,40})\b",
    re.I,
)

_PROBABLE_NAME_STOP_WORDS = _LATIN_NON_NAME_WORDS | {
    "best", "quality", "masterpiece", "solo", "alone", "white", "black", "blue",
    "green", "red", "pink", "purple", "silver", "gold", "golden", "hair", "eyes",
    "dress", "skirt", "shirt", "uniform", "background", "foreground", "standing",
    "sitting", "lying", "smiling", "looking", "wearing", "holding", "hugging",
}


def _is_cjk_character_name_candidate(token: str) -> bool:
    token = str(token or "").strip()
    return bool(
        re.fullmatch(_CJK_NAME_TOKEN, token)
        and not _CJK_NON_NAME_FRAGMENT.search(token)
        and not _CJK_LITERAL_NON_NAME_FRAGMENT.search(token)
    )


def _is_latin_character_name_candidate(token: str) -> bool:
    token = str(token or "").strip()
    if not re.fullmatch(_LATIN_NAME_TOKEN, token):
        return False
    words = re.findall(r"[A-Za-z]+", token.lower())
    return bool(words) and not any(word in _LATIN_NON_NAME_WORDS for word in words)


def _is_character_name_candidate(token: str) -> bool:
    return _is_cjk_character_name_candidate(token) or _is_latin_character_name_candidate(token)


def _has_explicit_character_name_pair(text: str) -> bool:
    text = str(text or "")
    for match in re.finditer(rf"(?P<left>{_NAME_TOKEN})\s*{_PAIR_CONNECTOR}\s*(?P<right>{_NAME_TOKEN})", text):
        if _is_character_name_candidate(match.group("left")) and _is_character_name_candidate(match.group("right")):
            return True
    return False


def _has_multi_role_slots(text: str) -> bool:
    labels = set()
    for match in _ROLE_SLOT_RE.finditer(str(text or "")):
        label = next((item for item in match.groups() if item), "")
        label = str(label or "").strip().lower()
        if label:
            labels.add(label)
    return len(labels) >= 2


def _merged_spans(spans: list) -> list:
    spans = sorted((int(start), int(end)) for start, end in spans if int(end) > int(start))
    merged = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _is_embedded_ascii_subject_span(text: str, start: int, end: int) -> bool:
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


def _subject_spans(text: str) -> list:
    text = str(text or "")
    spans = [
        (m.start(), m.end())
        for m in _SUBJECT_NOUN_RE.finditer(text)
        if not _is_embedded_ascii_subject_span(text, m.start(), m.end())
    ]
    spans.extend(
        (m.start(), m.end())
        for m in _APPEARANCE_SUBJECT_RE.finditer(text)
        if not _is_embedded_ascii_subject_span(text, m.start(), m.end())
    )
    return _merged_spans(spans)


def _subject_mention_count(text: str) -> int:
    return len(_subject_spans(text))


def _appearance_subject_count(text: str) -> int:
    text = str(text or "")
    return len(_merged_spans([
        (m.start(), m.end())
        for m in _APPEARANCE_SUBJECT_RE.finditer(text)
        if not _is_embedded_ascii_subject_span(text, m.start(), m.end())
    ]))


def _has_subject_listing(text: str) -> bool:
    text = str(text or "")
    spans = _subject_spans(text)
    if len(spans) < 2:
        return False
    for left, right in zip(spans, spans[1:]):
        between = text[left[1]:right[0]]
        if len(between) <= 64 and _GENERIC_PAIR_CONNECTOR_RE.search(between):
            return True
    return False


def _has_probable_latin_name_pair(text: str) -> bool:
    text = str(text or "")
    for match in _LATIN_NAME_PAIR_RE.finditer(text):
        left = str(match.group("left") or "").lower()
        right = str(match.group("right") or "").lower()
        if left in _PROBABLE_NAME_STOP_WORDS or right in _PROBABLE_NAME_STOP_WORDS:
            continue
        if left == right:
            continue
        return True
    return False


def _spatial_marker_count(text: str) -> int:
    markers = []
    for match in _SPATIAL_ROLE_MARKER_RE.finditer(str(text or "")):
        token = match.group(0).lower()
        if token in {"left", "\u5de6\u8fb9", "\u5de6\u4fa7", "\u5de6\u9762"}:
            token = "left"
        elif token in {"right", "\u53f3\u8fb9", "\u53f3\u4fa7", "\u53f3\u9762"}:
            token = "right"
        elif token in {"front", "foreground", "\u524d\u9762", "\u524d\u666f"}:
            token = "front"
        elif token in {"back", "behind", "background", "\u540e\u9762", "\u80cc\u666f"}:
            token = "back"
        markers.append(token)
    return len(set(markers))


def _multi_character_evidence_score(text: str) -> int:
    text = str(text or "")
    if not text:
        return 0
    score = 0
    subject_count = _subject_mention_count(text)
    appearance_count = _appearance_subject_count(text)
    has_shared_action = bool(_SHARED_ACTION_RE.search(text))
    has_plural_reference = bool(_PLURAL_REFERENCE_RE.search(text))

    if _has_subject_listing(text):
        score += 3
    elif subject_count >= 2:
        score += 2

    if appearance_count >= 2:
        score += 2
    if _spatial_marker_count(text) >= 2 and subject_count >= 1:
        score += 2
    if has_shared_action and subject_count >= 2:
        score += 2
    elif has_shared_action and (_has_probable_latin_name_pair(text) or has_plural_reference):
        score += 2
    if has_plural_reference and (subject_count >= 1 or has_shared_action):
        score += 2
    if _has_probable_latin_name_pair(text):
        score += 2 if has_shared_action else 1
    if _SINGLE_SUBJECT_STRONG_RE.search(text) and subject_count < 2 and not has_plural_reference:
        score -= 3
    return score


def _looks_like_single_self_subject(text: str) -> bool:
    text = str(text or "")
    if not (_SELF_SUBJECT_RE.search(text) or _BODY_PART_PAIR_RE.search(text)):
        return False
    return not (
        _EXPLICIT_MULTI_COUNT_RE.search(text)
        or _CJK_EXPLICIT_MULTI_RE.search(text)
        or _CJK_MULTI_COUNT_RE.search(text)
        or _CJK_MIXED_GENDER_PAIR_RE.search(text)
        or _CJK_PLURAL_ACTION_RE.search(text)
        or _EXPLICIT_OTHER_SUBJECT_RE.search(text)
        or _has_explicit_character_name_pair(text)
        or _has_multi_role_slots(text)
        or _SIDE_ROLE_RE.search(text)
    )


def illustrious_has_multi_character_intent(raw_prompt: str, prompt: str = "") -> bool:
    raw_text = str(raw_prompt or "").strip()
    prompt_text = str(prompt or "").strip()
    combined = raw_text if not prompt_text or prompt_text == raw_text else f"{raw_text}\n{prompt_text}".strip()
    if not combined:
        return False
    if (
        _EXPLICIT_MULTI_COUNT_RE.search(combined)
        or _CJK_EXPLICIT_MULTI_RE.search(combined)
        or _CJK_MULTI_COUNT_RE.search(combined)
        or _CJK_MIXED_GENDER_PAIR_RE.search(combined)
        or _CJK_PLURAL_ACTION_RE.search(combined)
        or _EXPLICIT_OTHER_SUBJECT_RE.search(combined)
        or _has_explicit_character_name_pair(combined)
        or _has_multi_role_slots(combined)
        or _SIDE_ROLE_RE.search(combined)
    ):
        return True
    if _looks_like_single_self_subject(combined):
        return False
    if _multi_character_evidence_score(combined) >= 3:
        return True
    if _SINGLE_SUBJECT_STRONG_RE.search(combined):
        return False
    return bool(_RELATIONAL_MULTI_RE.search(combined))
