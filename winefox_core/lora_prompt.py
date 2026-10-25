"""Compose LoRA activation cues without overriding the user's requested edits.

Model-card trainedWords frequently mixes an identity token, appearance and the
training outfit. Keep that source data intact; decide what to inject per request.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping

from .lora_binding import text_list, trigger_key


_COLORS = r"black|white|blue|red|green|gold(?:en)?|yellow|silver|pink|purple|violet|brown|gr[ae]y|orange|aqua|teal|cyan|blond(?:e)?|rainbow|multicolou?red|two tone|gradient"
_RULES = (
    ('accessories', r'\b(?:earrings?|jewelry|necklace|bracelet|hair ornament|hairclip|hair ribbon|hair bow|hairband|hairpin|hat|headwear|glasses|eyepatch)\b|耳环|耳環|首饰|首飾|项链|項鏈|发饰|髮飾|眼镜|眼鏡'),
    ('outfit', r'\b(?:dress|dresses|sleeves?|sleeveless|cutout|clothing|clothes|outfit|costume|uniform|swimsuit|swimwear|bikini|leotard|bodysuit|suit|shirt|t shirt|jacket|coat|skirt|pants|trousers|shorts|jeans|leggings|stockings|pantyhose|thighhighs|socks|boots|shoes|footwear|heels|sandals|gloves|apron|kimono|yukata|robe|armor|armour|cape|scarf|tie|necktie|bra|panties|underwear|lingerie|cleavage|midriff|navel|bare shoulders|bare arms|bare legs|barefoot|nude|naked|topless|bottomless)\b|连衣裙|連衣裙|服装|服裝|衣服|裙子|袖|泳衣|泳装|泳裝|死库水|死庫水|内衣|內衣|丝袜|絲襪|裸肩'),
    ('hair_color', rf'\b(?:{_COLORS}) (?:hair|fur)\b|\b(?:hair|fur) colou?r\b|发色|髮色|毛色'),
    ('hair_length', r'\b(?:(?:very |extra )?(?:long|short)|medium|shoulder length|floor length|waist length) hair\b|长发|長髮|短发|短髮'),
    ('hair_style', r'\b(?:hair style|hairstyle|bangs|braids?|ponytail|twintails|pigtails|ahoge|hair bun|curly hair|straight hair|wavy hair)\b|发型|髮型|刘海|劉海|马尾|馬尾|辫子|辮子'),
    ('eye_color', rf'\b(?:{_COLORS}) eyes?\b|\b(?:heterochromia|eye colou?r|iris colou?r)\b|瞳色|眸色|眼睛颜色|眼睛顏色'),
    ('horns', r'\bhorns?\b|龙角|龍角|犄角'),
    ('ears', r'\b(?:pointy|pointed|animal|cat|fox|normal|human|long) ears\b|尖耳|兽耳|獸耳|猫耳|貓耳|狐耳'),
    ('tail', r'\btails?\b|尾巴|龙尾|龍尾'),
    ('wings', r'\bwings?\b|翅膀|羽翼'),
    ('skin', r'\b(?:skin|tan|tanned|colored extremities|pale|dark skinned)\b|肤色|膚色|皮肤|皮膚'),
    ('body', r'\b(?:breasts|bust|muscular|slender|chubby|body shape|body type)\b|胸部|胸围|胸圍|身材|肌肉'),
    ('expression', r'\b(?:smil(?:e|ing)|grin|shy|blush(?:ing)?|angry|sad|crying|laughing|expression|closed eyes|open mouth|serious)\b|表情|害羞|微笑|生气|生氣|哭泣'),
    ('pose', r'\b(?:sitting|standing|lying|kneeling|running|walking|dancing|jumping|holding|eating|drinking|pose|waving|hugging)\b|姿势|姿勢|动作|動作|坐着|站着|躺着|跪着|跳舞|跑步|拿着|抱着|吃着'),
    ('framing', r'\b(?:full body|upper body|cowboy shot|portrait|close up|from behind|from above|from below|wide shot)\b|全身|半身|特写|特寫|远景|遠景|俯视|仰视'),
    ('scene', r'\b(?:background|outdoors|indoors|beach|forest|bedroom|classroom|ocean|city|street|night|sunset|daylight)\b|背景|场景|場景|沙滩|沙灘|海边|海邊|森林|室内|室內'),
    ('style', r'\b(?:photorealistic|realistic|3d|render|watercolor|oil painting|pixel art|anime|sketch|lineart)\b|画风|畫風|写实|寫實|水彩|油画|油畫|像素风'),
    ('count', r'\b(?:[1-9]+(?:girls?|boys?)|solo|multiple girls|multiple boys|multiple people)\b|单人|單人|双人|雙人|两人|兩人|多人'),
)
_COMPILED = tuple((name, re.compile(pattern, re.I)) for name, pattern in _RULES)
_DEFAULT = re.compile(r'原(?:来|本)(?:的)?衣服|原服|原皮|原装|原裝|默认(?:衣服|服装)|默認(?:衣服|服裝)|官服|官方服装|原设服装|不要换衣服|\b(?:default|original|canon(?:ical)?|official|usual) outfit\b', re.I)
_NOT_DEFAULT = re.compile(r'(?:不要|不用|不穿|不是|去掉|脱掉|换掉|替换).{0,6}(?:原服|原皮|原装|默认|原来的衣服)|\b(?:no|not|without|replace|remove|don.t wear)\s+(?:the\s+)?(?:original|default|canon(?:ical)?)\b', re.I)


def _plain(text: str) -> str:
    return re.sub(r'\s+', ' ', trigger_key(text).replace('_', ' ').replace('-', ' ')).strip()


def tag_scope(tag: str) -> str:
    key = trigger_key(tag)
    # Preserve opaque learned tokens, e.g. special_dress_character_v2. Unlike
    # ordinary white_dress, their meaning cannot be recovered by word matching.
    if re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.:-]*', key) and re.search(r'\d', key):
        return 'activation'
    value = _plain(tag)
    return next((name for name, rule in _COMPILED if rule.search(value)), 'activation')


def override_scopes(raw: str, translated: str = '') -> set[str]:
    text = _plain(f'{raw}\n{translated}')
    scopes = {name for name, rule in _COMPILED if rule.search(text)}
    cn_color = r'(?:黑|白|红|紅|绿|綠|蓝|藍|金|银|銀|粉|紫|棕|灰|橙|彩)(?:色)?'
    if re.search(cn_color+r'(?:的)?(?:头发|頭髮|长发|短发|发(?!饰|夹|箍|带)|髮)|(?:头发|頭髮|发色|髮色).{0,12}'+cn_color, text):
        scopes.add('hair_color')
    if re.search(cn_color+r'(?:的)?(?:眼睛|眼|瞳|眸)|(?:眼睛|瞳色|眸色).{0,12}'+cn_color, text):
        scopes.add('eye_color')
    if re.search(r'变成普通人|变成人类|变成人類|普通人类|普通人類|\b(?:ordinary human|normal human|human form)\b', text):
        scopes.update({'horns','ears','tail','wings'})
    return scopes


def wants_default_outfit(raw: str, translated: str = '') -> bool:
    # Original user wording takes precedence over an inferred translation.
    text = _plain(raw or translated)
    if not _DEFAULT.search(text) or _NOT_DEFAULT.search(text):
        return False
    if re.search(r'(?:换成|换为|改成|改为|替换成|change.+to|replace.+with)', text) and not re.search(r'换回|恢复|恢復|回到|back to', text):
        return False
    return True


def profile_prompt_groups(words: Iterable[str]) -> dict[str, list[str]]:
    result = {'activation':[], 'appearance':[], 'outfit':[], 'details':[]}
    for tag in text_list(list(words)):
        scope = tag_scope(tag)
        group = 'activation' if scope == 'activation' else 'outfit' if scope == 'outfit' else 'details' if scope in {'pose','expression','framing','scene','style','count'} else 'appearance'
        result[group].append(tag)
    return result


def filter_reference_tags(tags: Iterable[str], raw: str, translated: str = '') -> LoraPromptPlan:
    """Image descriptions are hints; explicit user edits always take precedence."""
    overrides = override_scopes(raw, translated)
    applied, omitted = [], []
    for tag in text_list(list(tags)):
        scope = tag_scope(tag)
        if scope != 'activation' and scope in overrides:
            omitted.append({'tag': tag, 'scope': scope, 'reason': 'user_override'})
        else:
            applied.append(tag)
    return LoraPromptPlan(tuple(applied), tuple(omitted), bool(applied or omitted))


@dataclass(frozen=True)
class LoraPromptPlan:
    tags: tuple[str, ...]
    omitted: tuple[dict[str, str], ...]
    known_triggers: bool

    def to_dict(self) -> dict[str, Any]:
        return {'applied':list(self.tags),'omitted':list(self.omitted)}


def plan_lora_prompt(profiles: Iterable[Any], raw_prompt: str = '', translated_prompt: str = '') -> LoraPromptPlan:
    overrides = override_scopes(raw_prompt, translated_prompt)
    original_outfit = wants_default_outfit(raw_prompt, translated_prompt)
    tags, omitted, seen, known = [], [], set(), False
    for profile in profiles:
        source = text_list(profile.trigger_words)
        if not source:
            continue  # A user-cleared trigger list remains authoritative.
        known = True
        extra: Mapping[str, Any] = profile.extra or {}
        # Explicit current trigger_words wins over stale legacy structured data.
        if 'trigger_words' not in extra:
            source = text_list([*source, *text_list(extra.get('identity_appearance')), *text_list(extra.get('default_outfit'))])
        forced_outfit = {trigger_key(tag) for tag in text_list(extra.get('default_outfit'))}
        for tag in source:
            key = trigger_key(tag)
            if key in seen:
                continue
            seen.add(key)
            scope = 'outfit' if key in forced_outfit else tag_scope(tag)
            reason = ''
            if scope == 'outfit' and not original_outfit:
                reason = 'default_outfit_not_requested'
            elif scope != 'activation' and scope in overrides and not (scope == 'outfit' and original_outfit):
                reason = 'user_override'
            if reason:
                omitted.append({'tag':tag,'scope':scope,'reason':reason})
            else:
                tags.append(tag)
    return LoraPromptPlan(tuple(tags),tuple(omitted),known)
