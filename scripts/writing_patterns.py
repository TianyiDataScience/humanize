from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable


CATEGORY_LABELS = {
    "inflated_significance": "夸大意义",
    "promotional_language": "宣传式措辞",
    "vague_attribution": "模糊归因",
    "formulaic_contrast": "公式化转折",
    "generic_conclusion": "泛化式结尾",
    "chatbot_artifact": "机器人式客套",
}

CATEGORY_REPAIR_GUIDANCE = {
    "inflated_significance": "把夸大意义的判断改成具体事实，不把普通进展写成里程碑。",
    "promotional_language": "删掉宣传式形容词，用具体功能、场景或结果说明。",
    "vague_attribution": "没有明确来源时，不要用“专家”或“业内人士”替结论背书。",
    "formulaic_contrast": "把公式化的“不仅……更……”拆成直接陈述，只保留真正需要的对比。",
    "generic_conclusion": "把泛化的期待式结尾换成具体下一步，或自然收束。",
    "chatbot_artifact": "去掉机器人式客套收尾，保留必要的联系或下一步信息。",
}

_PATTERN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "inflated_significance",
        re.compile(r"(?:标志着|意味着)[^。！？\n]{0,14}(?:重要|关键|历史性|全新)[^。！？\n]{0,4}(?:阶段|里程碑|突破|时刻)"),
    ),
    (
        "inflated_significance",
        re.compile(r"(?:历史性|划时代|前所未有)[^。！？\n]{0,8}(?:意义|突破|时刻)"),
    ),
    (
        "promotional_language",
        re.compile(r"令人(?:惊艳|惊叹|叹为观止)"),
    ),
    (
        "promotional_language",
        re.compile(r"(?:卓越|非凡|极致|颠覆性)[^。！？\n]{0,8}(?:体验|品质|表现|效果)"),
    ),
    (
        "promotional_language",
        re.compile(r"(?:焕发|尽享)[^。！？\n]{0,10}(?:光彩|新体验)"),
    ),
    (
        "vague_attribution",
        re.compile(r"(?:专家|业内人士|观察者|许多人|大家)(?:普遍)?(?:认为|指出|表示)"),
    ),
    (
        "formulaic_contrast",
        re.compile(r"不仅(?:仅)?[^。！？\n]{0,18}(?:更是|而且|还)[^。！？\n]{0,18}"),
    ),
    (
        "generic_conclusion",
        re.compile(r"(?:未来|接下来|让我们)[^。！？\n]{0,16}(?:值得期待|共同期待|拭目以待)"),
    ),
    (
        "generic_conclusion",
        re.compile(r"(?:迈向|开启)[^。！？\n]{0,12}新(?:的)?(?:篇章|起点)"),
    ),
    (
        "chatbot_artifact",
        re.compile(r"希望(?:以上|这些|这段|这)?(?:内容|信息)?对(?:你|您)有所帮助"),
    ),
    (
        "chatbot_artifact",
        re.compile(r"如有(?:任何)?问题(?:，|,)?(?:请|欢迎)[^。！？\n]{0,8}(?:告诉我|随时联系)"),
    ),
)

_QUOTED_SEGMENT_PATTERNS = (
    re.compile(r"\x60\x60\x60.*?\x60\x60\x60", re.DOTALL),
    re.compile(r"“[^”]*”"),
    re.compile(r"「[^」]*」"),
    re.compile(r"《[^》]*》"),
    re.compile(r'"[^"\n]*"'),
)


def _mask_quoted_segments(text: str) -> str:
    masked = text
    for pattern in _QUOTED_SEGMENT_PATTERNS:
        masked = pattern.sub(lambda match: " " * len(match.group(0)), masked)
    return masked


def category_ids(audit: dict[str, Any]) -> list[str]:
    categories = audit.get("categories") or []
    result: list[str] = []
    for item in categories:
        if not isinstance(item, dict):
            continue
        category = str(item.get("id") or "")
        if category in CATEGORY_LABELS and category not in result:
            result.append(category)
    return result


def category_labels(categories: Iterable[str]) -> list[str]:
    return [CATEGORY_LABELS[category] for category in categories if category in CATEGORY_LABELS]


def repair_directives(categories: Iterable[str]) -> list[str]:
    return [CATEGORY_REPAIR_GUIDANCE[category] for category in categories if category in CATEGORY_REPAIR_GUIDANCE]


def audit_writing_patterns(text: str) -> dict[str, Any]:
    """Return explainable, conservative writing-pattern signals for Chinese copy.

    A single match remains visible in the result but has no score effect. The
    score only falls when multiple signals form a cluster.
    """

    scan_text = _mask_quoted_segments(text or "")
    hits_by_category: dict[str, list[str]] = defaultdict(list)
    for category, pattern in _PATTERN_RULES:
        for match in pattern.finditer(scan_text):
            evidence = match.group(0).strip()
            if evidence and evidence not in hits_by_category[category]:
                hits_by_category[category].append(evidence)

    categories = [
        {
            "id": category,
            "label": CATEGORY_LABELS[category],
            "hits": hits,
        }
        for category in CATEGORY_LABELS
        if (hits := hits_by_category.get(category))
    ]
    hit_count = sum(len(item["hits"]) for item in categories)
    category_count = len(categories)
    if hit_count <= 1:
        score = 1.0
    else:
        penalty = min(0.24, 0.06 * (hit_count - 1) + 0.05 * (category_count - 1))
        score = max(0.0, 1.0 - penalty)

    return {
        "score": round(score, 6),
        "needs_repair": bool(category_count >= 2 or hit_count >= 3),
        "hit_count": hit_count,
        "category_count": category_count,
        "categories": categories,
    }
