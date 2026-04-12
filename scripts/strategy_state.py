from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from runtime_common import runtime_root

PROFILE_LIBRARY: dict[str, str] = {
    "steady": "稳健完整，优先保证信息清楚、可信、不过度修饰。",
    "natural": "更像微信里真人会发的话，少模板感，但不要失分寸。",
    "reassuring": "更强调安抚感和可预期性，但不要空泛。",
    "direct": "更直接、简短、利落，减少铺垫和套话。",
    "concise": "尽量短，但不能丢事实、时间点和对象感。",
    "repair": "专门修复上一轮失败原因，优先满足 must_include、对象、长度等硬约束。",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def state_path() -> Path:
    return runtime_root() / "strategy-state.json"


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _timestamp(),
        "generation": {
            "challenger_count": 2,
            "retry_rounds": 1,
            "preferred_profiles": ["steady", "natural", "reassuring", "direct", "concise"],
        },
        "prompt_policies": {
            "must_include_strength": "hard",
            "audience_guardrail": "explicit",
            "min_detail": "medium",
            "avoid_template_tone": "high",
            "self_check": True,
        },
        "failure_tally": {},
        "profile_stats": {
            name: {"wins": 0, "losses": 0}
            for name in PROFILE_LIBRARY
        },
        "last_failure_tags": [],
        "last_success_profile": "",
        "history": [],
    }


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        state = default_state()
        save_state(state)
        return state
    payload = json.loads(path.read_text(encoding="utf-8"))
    state = default_state()
    state.update({k: v for k, v in payload.items() if k in state})
    if isinstance(payload.get("generation"), dict):
        state["generation"].update(payload["generation"])
    if isinstance(payload.get("prompt_policies"), dict):
        state["prompt_policies"].update(payload["prompt_policies"])
    if isinstance(payload.get("failure_tally"), dict):
        state["failure_tally"].update(payload["failure_tally"])
    if isinstance(payload.get("profile_stats"), dict):
        for name, stats in payload["profile_stats"].items():
            if name not in state["profile_stats"] or not isinstance(stats, dict):
                continue
            state["profile_stats"][name].update(stats)
    if isinstance(payload.get("history"), list):
        state["history"] = payload["history"][-20:]
    if isinstance(payload.get("last_failure_tags"), list):
        state["last_failure_tags"] = payload["last_failure_tags"]
    if isinstance(payload.get("last_success_profile"), str):
        state["last_success_profile"] = payload["last_success_profile"]
    state["updated_at"] = payload.get("updated_at") or state["updated_at"]
    generation = state.get("generation") or {}
    generation["challenger_count"] = 2
    generation["retry_rounds"] = min(1, max(0, int(generation.get("retry_rounds") or 1)))
    return state


def save_state(state: dict[str, Any]) -> Path:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _timestamp()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def snapshot_state(state: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(state)


def choose_profiles(
    state: dict[str, Any],
    failure_tags: list[str],
    round_index: int,
) -> list[str]:
    preferred = list(state.get("generation", {}).get("preferred_profiles") or [])
    count = int(state.get("generation", {}).get("challenger_count") or 4)
    scored_profiles: list[tuple[float, int, str]] = []
    for index, name in enumerate(preferred):
        stats = (state.get("profile_stats") or {}).get(name) or {}
        wins = int(stats.get("wins") or 0)
        losses = int(stats.get("losses") or 0)
        scored_profiles.append((wins - losses * 0.6, -index, name))
    scored_profiles.sort(reverse=True)
    ordered = [name for _, _, name in scored_profiles] or preferred or ["steady", "natural", "direct", "concise"]

    chosen: list[str] = []
    if round_index > 0 or failure_tags:
        chosen.append("repair")
    if "too_short" in failure_tags or "too_vague" in failure_tags:
        chosen.append("steady")
        chosen.append("reassuring")
    if "template_tone" in failure_tags:
        chosen.append("natural")
        chosen.append("direct")
    if "wrong_audience" in failure_tags:
        chosen.append("steady")
    for name in ordered:
        if name not in chosen:
            chosen.append(name)
    return chosen[:count]


def extract_failure_tags(task: str, candidate_text: str, score_payload: dict[str, Any], baseline_score: float | None = None) -> list[str]:
    tags: list[str] = []
    notes = score_payload.get("notes") or []
    lowered_notes = [str(item).lower() for item in notes]

    if any("missing must_include" in item for item in lowered_notes):
        tags.append("missing_must_include")
    if any("contains template phrases" in item for item in lowered_notes):
        tags.append("template_tone")
    if any("retains source template phrases:" in item for item in lowered_notes):
        tags.append("source_template_carryover")
    if any("contains banned phrases" in item for item in lowered_notes):
        tags.append("banned_phrase")
    if any("rewrite too similar to source" in item or "rewrite still very close to source" in item for item in lowered_notes):
        tags.append("too_similar")
    if any("sentence splice issue" in item or "sentence structure is broken by phrase collision" in item for item in lowered_notes):
        tags.append("bad_splice")
    if any("contains placeholder-style" in item or "candidate still looks like placeholder text" in item for item in lowered_notes):
        tags.append("placeholder_output")
    if any("rewrite drops too much source detail" in item or "rewrite is over-compressed" in item or "rewrite removed too much source content" in item for item in lowered_notes):
        tags.append("overcompressed")
    if any("longer than max_chars" in item for item in lowered_notes):
        tags.append("too_long")
    if any("shorter than min_chars" in item for item in lowered_notes):
        tags.append("too_short")
    if bool(score_payload.get("hard_fail")):
        tags.append("hard_fail")

    char_count = int(score_payload.get("char_count") or 0)
    if char_count and char_count < 14:
        tags.append("too_short")
    if char_count and char_count < 18 and "。" not in candidate_text and "，" not in candidate_text:
        tags.append("too_vague")

    if "客户" in task:
        if ("财务" in candidate_text and any(mark in candidate_text for mark in ("吗", "？", "?")) and "您" not in candidate_text):
            tags.append("wrong_audience")
        if candidate_text.strip().startswith(("财务那边", "财务同事", "财务部门")) and "您" not in candidate_text:
            tags.append("wrong_audience")

    final_score = float(score_payload.get("final_score") or 0.0)
    if baseline_score is not None and final_score < float(baseline_score):
        tags.append("no_improvement")

    unique: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            unique.append(tag)
    return unique


def state_directives(state: dict[str, Any], failure_tags: list[str]) -> list[str]:
    policies = state.get("prompt_policies") or {}
    directives: list[str] = []
    if policies.get("must_include_strength") == "hard":
        directives.append("提交前逐字自检所有 must_include 是否完整保留，任何一个缺失都不允许提交。")
    if policies.get("audience_guardrail") in {"explicit", "hard"}:
        directives.append("先确认消息接收对象，再写文案；不要把客户、内部同事、上级混淆。")
    if policies.get("min_detail") == "high":
        directives.append("不要过短，不要只写一句空泛回应；至少交代当前动作和下一步时间点。")
    elif policies.get("min_detail") == "medium":
        directives.append("不要过短，要让对方知道你现在在做什么、下一步什么时候给结果。")
    if policies.get("avoid_template_tone") in {"high", "very_high"}:
        directives.append("避免客服模板腔、公告腔和过度工整的套话。")
    if policies.get("self_check"):
        directives.append("生成后先自检硬约束、对象、时间点，再输出。")

    if "missing_must_include" in failure_tags:
        directives.append("上一轮漏了 must_include，这一轮必须先把关键短语写进去再组织句子。")
    if "wrong_audience" in failure_tags:
        directives.append("上一轮对象写错了，这一轮明确写成发给目标对象的外部回复。")
    if "too_short" in failure_tags or "too_vague" in failure_tags:
        directives.append("上一轮太短或太虚，这一轮要补足动作、状态和时间承诺。")
    if "template_tone" in failure_tags:
        directives.append("上一轮太模板，这一轮用更自然、更像真人微信的表达。")
    if "source_template_carryover" in failure_tags:
        directives.append("上一轮还保留了原文里的套话，这一轮把这些高频模板词换成新的真人表达。")
    if "too_similar" in failure_tags:
        directives.append("上一轮和原文太像了，这一轮要明显改写表达方式，不要只替换几个词。")
    if "bad_splice" in failure_tags:
        directives.append("上一轮有明显拼接痕迹，这一轮把相关句子重写，不要把两个结尾或引导语拼在一起。")
    if "placeholder_output" in failure_tags:
        directives.append("上一轮像占位文本，这一轮不要用省略号、空洞概括或占位词顶替真实内容。")
    if "overcompressed" in failure_tags:
        directives.append("上一轮压缩过头了，这一轮保留更多原文里的有效信息，不要只剩一句概括。")
    if "copied_baseline" in failure_tags:
        directives.append("上一轮只是复读 baseline，这一轮必须明显改写，不能原样重复。")
    return directives


def evolve_after_attempts(
    state: dict[str, Any],
    *,
    task: str,
    chosen_profile: str,
    failure_tags: list[str],
    improved: bool,
    baseline_text: str,
    challenger_text: str,
    delta: float,
) -> dict[str, Any]:
    next_state = snapshot_state(state)
    profile_stats = next_state.setdefault("profile_stats", {}).setdefault(chosen_profile, {"wins": 0, "losses": 0})
    if improved:
        profile_stats["wins"] = int(profile_stats.get("wins") or 0) + 1
        next_state["last_success_profile"] = chosen_profile
        next_state["last_failure_tags"] = []
        preferred = list(next_state.get("generation", {}).get("preferred_profiles") or [])
        if chosen_profile in preferred:
            preferred.remove(chosen_profile)
        preferred.insert(0, chosen_profile)
        next_state["generation"]["preferred_profiles"] = preferred
    else:
        profile_stats["losses"] = int(profile_stats.get("losses") or 0) + 1
        next_state["last_failure_tags"] = list(failure_tags)
        tally = next_state.setdefault("failure_tally", {})
        for tag in failure_tags:
            tally[tag] = int(tally.get(tag) or 0) + 1
        policies = next_state.setdefault("prompt_policies", {})
        if "missing_must_include" in failure_tags:
            policies["must_include_strength"] = "hard"
        if "wrong_audience" in failure_tags:
            policies["audience_guardrail"] = "hard"
        if "too_short" in failure_tags or "too_vague" in failure_tags:
            policies["min_detail"] = "high"
        if "template_tone" in failure_tags:
            policies["avoid_template_tone"] = "very_high"
        if "source_template_carryover" in failure_tags:
            policies["avoid_template_tone"] = "very_high"
            policies["self_check"] = True
        if "too_similar" in failure_tags:
            policies["min_detail"] = "high"
            policies["self_check"] = True
        if "bad_splice" in failure_tags:
            policies["self_check"] = True
        if "placeholder_output" in failure_tags or "overcompressed" in failure_tags:
            policies["min_detail"] = "high"
            policies["self_check"] = True
        if "copied_baseline" in failure_tags:
            policies["min_detail"] = "high"
            policies["self_check"] = True

    history = next_state.setdefault("history", [])
    history.append(
        {
            "recorded_at": _timestamp(),
            "task": task,
            "chosen_profile": chosen_profile,
            "failure_tags": failure_tags,
            "improved": improved,
            "delta": round(delta, 6),
            "baseline_preview": baseline_text[:120],
            "challenger_preview": challenger_text[:120],
        }
    )
    next_state["history"] = history[-20:]
    return next_state
