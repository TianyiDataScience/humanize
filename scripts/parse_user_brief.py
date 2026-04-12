from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import yaml

from runtime_common import DEFAULT_GOAL

LABEL_MAP = {
    "task": ("任务", "task"),
    "constraints": ("约束", "constraints"),
    "original": ("原文", "原稿", "正文", "draft", "message"),
    "goal": ("目标", "goal"),
}
INLINE_LABELS = (
    ("任务", "task"),
    ("约束", "constraints"),
    ("原文", "original"),
    ("原稿", "original"),
    ("正文", "original"),
    ("目标", "goal"),
    ("task", "task"),
    ("constraints", "constraints"),
    ("goal", "goal"),
    ("draft", "original"),
    ("message", "original"),
)

GENERIC_TASK = "优化这条中文沟通消息"
SCENARIO_KEYWORDS = (
    "回复",
    "微信",
    "消息",
    "邮件",
    "沟通",
    "客户",
    "上级",
    "老板",
    "面试",
    "售后",
    "文案",
    "自媒体",
    "小红书",
    "公众号",
    "朋友圈",
    "公告",
    "社群",
    "差评",
    "评价",
    "口播",
    "脚本",
    "短视频",
)
MESSAGE_HINTS = ("您好", "你好", "这边", "这次", "辛苦", "麻烦", "谢谢", "感谢", "明天下午", "财务", "方便", "抱歉")
INSTRUCTION_HINTS = ("任务", "约束", "目标", "保留", "控制在", "不超过", "生成", "优化", "更像真人", "别太", "不要")


def clean_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text.strip())


def split_lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def is_skill_invocation(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    return lowered.startswith("用 humanize") or lowered.startswith("humanize ") or "用 humanize 帮我" in lowered


def looks_like_message(text: str) -> bool:
    stripped = clean_text(text)
    if not stripped:
        return False
    if any(hint in stripped for hint in MESSAGE_HINTS):
        return True
    if sum(1 for char in "，。！？；" if char in stripped) >= 1 and len(stripped) >= 18:
        return True
    if any(hint in stripped for hint in INSTRUCTION_HINTS):
        return False
    return False


def label_key(line: str) -> tuple[str | None, str]:
    stripped = line.strip()
    for key, labels in LABEL_MAP.items():
        for label in labels:
            match = re.match(rf"^{re.escape(label)}\s*[:：]?\s*(.*)$", stripped, flags=re.IGNORECASE)
            if match:
                return key, match.group(1).strip()
    return None, stripped


def parse_labeled_sections(text: str) -> tuple[dict[str, str], list[str]]:
    sections: dict[str, list[str]] = {}
    unclaimed: list[str] = []
    current: str | None = None

    for raw_line in split_lines(text):
        stripped = raw_line.strip()
        if not stripped:
            if current and sections.get(current):
                sections[current].append("")
            continue
        key, remainder = label_key(raw_line)
        if key:
            current = key
            sections.setdefault(key, [])
            if remainder:
                sections[key].append(remainder)
            continue
        if current:
            sections.setdefault(current, []).append(stripped)
        else:
            unclaimed.append(stripped)

    flattened = {
        key: clean_text("\n".join(value))
        for key, value in sections.items()
        if clean_text("\n".join(value))
    }
    return flattened, unclaimed


def parse_inline_sections(text: str) -> dict[str, str]:
    pattern = re.compile(
        r"(任务|约束|原文|原稿|正文|目标|task|constraints|goal|draft|message)\s*[:：]",
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return {}

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        raw_label = match.group(1).lower()
        key = None
        for label, canonical in INLINE_LABELS:
            if raw_label == label.lower():
                key = canonical
                break
        if key is None:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = clean_text(text[start:end].strip(" \n\t。；;"))
        if value and key not in sections:
            sections[key] = value
    return sections


def extract_quoted_terms(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.findall(r"[“\"'「『](.+?)[”\"'」』]", text)
        if item.strip()
    ]


def split_plain_constraint_terms(text: str) -> list[str]:
    fragment = re.sub(r"(控制在|限制在|不超过|别超过|最多|至少|不要|别用|避免).*$", "", text).strip()
    fragment = fragment.strip(" ，。；;:：")
    if not fragment:
        return []
    fragment = re.sub(r"^(这些|这几个|以下|关键词|词语)", "", fragment).strip()
    candidates = re.split(r"[、，,和及与/]\s*", fragment)
    output: list[str] = []
    for item in candidates:
        value = item.strip(" “\"'”‘’()（）")
        if not value:
            continue
        if len(value) > 16:
            continue
        if any(token in value for token in ("保留", "必须", "包含", "控制在", "不要", "避免")):
            continue
        output.append(value)
    return unique_keep_order(output)


def is_soft_constraint_term(value: str) -> bool:
    soft_markers = (
        "核心观点",
        "逻辑结构",
        "结构",
        "原文",
        "语气",
        "口语",
        "口语化",
        "书面语",
        "减少书面语",
        "说教感",
        "ai腔",
        "AI腔",
        "模板腔",
        "自然",
        "风格",
        "观点",
        "内容",
        "真人会说",
    )
    return any(marker in value for marker in soft_markers)


def unique_keep_order(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def infer_max_chars(text: str) -> int | None:
    patterns = [
        r"(?:控制在|限制在|不超过|别超过|最多)\s*(\d+)\s*字",
        r"(\d+)\s*字内",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def infer_must_include(text: str) -> list[str]:
    matches: list[str] = []
    for match in re.finditer(r"(?:保留|带上|包含|必须提到)(.{0,100})", text):
        fragment = match.group(1)
        quoted = extract_quoted_terms(fragment)
        matches.extend(quoted or split_plain_constraint_terms(fragment))
    return unique_keep_order([item for item in matches if not is_soft_constraint_term(item)])


def infer_banned_phrases(text: str) -> list[str]:
    matches: list[str] = []
    for match in re.finditer(r"(?:不要|别用|不要出现|避免|别写)(.{0,100})", text):
        fragment = match.group(1)
        quoted = extract_quoted_terms(fragment)
        matches.extend(quoted or split_plain_constraint_terms(fragment))
    return unique_keep_order(matches)


def infer_task(unclaimed: list[str], original: str, text: str) -> str:
    for line in unclaimed:
        stripped = line.strip()
        if not stripped or is_skill_invocation(stripped):
            continue
        if any(keyword in stripped for keyword in SCENARIO_KEYWORDS):
            return stripped.rstrip("：:")
    for line in split_lines(text):
        stripped = line.strip()
        if not stripped or not is_skill_invocation(stripped):
            continue
        normalized = re.sub(r"^用\s*humanize\s*帮我", "", stripped, flags=re.IGNORECASE).strip(" ：:")
        normalized = re.split(
            r"(?:原文|原稿|正文|约束|要求|需求|目标|task|constraints|goal|draft|message)\s*[:：]",
            normalized,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" ：:")
        humanize_suffix = r"改得更像真人(?:会?(?:说|写|发))?(?:的话|的|话)?"
        capture = re.match(
            rf"^把这(?:段|封|篇|条)?(.+?)(?:{humanize_suffix}|改得更像真人一点|改得更自然(?:一点)?|改得更像人话|优化一下|润色一下)$",
            normalized,
        )
        if capture:
            normalized = capture.group(1).strip(" ，。,:：")
        normalized = re.sub(
            rf"({humanize_suffix}|改得更像真人一点|改得更自然(?:一点)?|改得更像人话|优化一下|优化这条中文沟通消息|生成并优化一条中文沟通消息|润色一下)",
            "",
            normalized,
        )
        normalized = re.sub(r"[，,。]\s*(?:要求|需求)?\s*(保留|带上|包含|必须提到|控制在|限制在|不超过|别超过|最多).*$", "", normalized)
        normalized = re.sub(r"\s*(?:要求|需求)?\s*(保留|带上|包含|必须提到|控制在|限制在|不超过|别超过|最多).*$", "", normalized)
        normalized = re.sub(r"[，,。]?\s*(?:要求|需求)$", "", normalized)
        normalized = normalized.strip(" ，。,:：")
        if normalized and any(keyword in normalized for keyword in SCENARIO_KEYWORDS):
            return normalized
    if "客户" in original:
        return "给客户发送中文沟通回复"
    if "面试" in original:
        return "撰写中文面试沟通回复"
    return GENERIC_TASK


def infer_original(text: str, sections: dict[str, str], unclaimed: list[str]) -> str:
    explicit = sections.get("original", "").strip()
    if explicit:
        return explicit
    if sections:
        return ""

    lines = split_lines(text)
    for line in lines:
        stripped = line.strip()
        if not stripped or not is_skill_invocation(stripped):
            continue
        if "：" in stripped or ":" in stripped:
            head, tail = re.split(r"[:：]", stripped, maxsplit=1)
            if any(token in head for token in ("背景", "要求", "约束", "任务", "保留", "控制在", "限制在")):
                continue
            if "背景" in stripped and any(token in stripped for token in ("要求", "保留", "控制在", "限制在")):
                continue
            tail = tail.strip()
            if tail and looks_like_message(tail):
                return tail

    residual: list[str] = []
    for item in unclaimed:
        stripped = item.strip()
        if not stripped or is_skill_invocation(stripped):
            continue
        if re.search(r"(?:\d+\s*字内|控制在|不超过|保留|避免|不要)", stripped):
            continue
        residual.append(stripped)

    if residual:
        joined = clean_text("\n".join(residual))
        if looks_like_message(joined):
            return joined

    return ""


def build_payload(raw_text: str) -> dict[str, Any]:
    text = clean_text(raw_text)
    sections, unclaimed = parse_labeled_sections(text)
    inline_sections = parse_inline_sections(text)
    for key, value in inline_sections.items():
        sections.setdefault(key, value)
    original = infer_original(text, sections, unclaimed)
    constraints_text = sections.get("constraints", "")
    goal = clean_text(sections.get("goal", ""))
    task = clean_text(sections.get("task", "")) or infer_task(unclaimed, original, text)

    infer_text = "\n".join([constraints_text, *unclaimed]).strip()
    max_chars = infer_max_chars(infer_text)
    must_include = infer_must_include(infer_text)
    banned_phrases = infer_banned_phrases(infer_text)

    hard_constraints: dict[str, Any] = {}
    if max_chars is not None:
        hard_constraints["max_chars"] = max_chars
    if must_include:
        hard_constraints["must_include"] = must_include
    if banned_phrases:
        hard_constraints["banned_phrases"] = banned_phrases

    input_mode = "structured" if sections.get("original") or sections.get("task") or sections.get("constraints") else "loose"
    session_mode = "rewrite" if original else "generate"
    spec: dict[str, Any] = {
        "task": task or GENERIC_TASK,
        "hard_constraints": hard_constraints,
        "evaluator": {
            "minimum_improvement": 0.015,
        },
    }
    if goal and goal != DEFAULT_GOAL:
        spec["goal"] = goal

    return {
        "input_mode": input_mode,
        "session_mode": session_mode,
        "parsed": {
            "task": spec["task"],
            "goal": spec.get("goal", ""),
            "hard_constraints": hard_constraints,
            "original": original,
        },
        "spec": spec,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize user input into a humanize spec and original draft.",
    )
    parser.add_argument("--text", default=None)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--json-output", type=Path, default=None)
    parser.add_argument("--spec-output", type=Path, default=None)
    parser.add_argument("--source-output", type=Path, default=None)
    args = parser.parse_args()

    if args.text is None and args.input is None:
        raise ValueError("Provide --text or --input")
    raw_text = args.text if args.text is not None else args.input.read_text(encoding="utf-8")
    payload = build_payload(raw_text)

    if args.spec_output:
        args.spec_output.parent.mkdir(parents=True, exist_ok=True)
        args.spec_output.write_text(
            yaml.safe_dump(
                payload["spec"],
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
    if args.source_output:
        args.source_output.parent.mkdir(parents=True, exist_ok=True)
        args.source_output.write_text(payload["parsed"]["original"], encoding="utf-8")
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
