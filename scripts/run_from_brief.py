from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

from runtime_common import reexec_into_runtime

reexec_into_runtime()

from local_generation import (  # noqa: E402
    build_generation_prompts,
    call_chat,
    discover_generation_backend,
    extract_content,
)
from parse_user_brief import build_payload  # noqa: E402
from prepare_run import create_run_dir  # noqa: E402
from render_run_report import build_html, build_markdown  # noqa: E402
from scoring_core import DEFAULT_TEMPLATE_PHRASES, dump_score_json, score_candidate  # noqa: E402
from strategy_state import (  # noqa: E402
    choose_profiles,
    evolve_after_attempts,
    extract_failure_tags,
    load_state,
    save_state,
    snapshot_state,
    state_directives,
)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def candidate_rank_key(candidate: dict[str, Any]) -> tuple[int, float, float]:
    score = candidate.get("score") or {}
    return (
        1 if not score.get("hard_fail") else 0,
        float(score.get("final_score") or 0.0),
        float(score.get("model_score") or 0.0),
    )


def derive_run_budget(task: str, session_mode: str) -> dict[str, int]:
    lowered = task.lower()
    is_email = "邮件" in task or "email" in lowered
    is_short_chat = "微信" in task or "wechat" in lowered or any(
        marker in task for marker in ("App", "APP", "推送", "直播课", "消息", "飞书")
    )
    if session_mode == "rewrite" and is_email:
        return {"challenger_count": 1, "max_rounds": 2}
    if session_mode == "rewrite":
        return {"challenger_count": 2, "max_rounds": 2}
    if is_email:
        return {"challenger_count": 2, "max_rounds": 1}
    if is_short_chat:
        return {"challenger_count": 2, "max_rounds": 1}
    return {"challenger_count": 2, "max_rounds": 2}


def pick_best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [item for item in candidates if item.get("score") and not item["score"].get("hard_fail")]
    pool = valid or [item for item in candidates if item.get("score")]
    if not pool:
        raise RuntimeError("No candidate produced a scorable output")
    return max(pool, key=candidate_rank_key)


def aggregate_failure_tags(candidates: list[dict[str, Any]], selected: dict[str, Any]) -> list[str]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        for tag in candidate.get("failure_tags") or []:
            counts[tag] += 1
    ordered: list[str] = []
    for tag in selected.get("failure_tags") or []:
        if tag not in ordered:
            ordered.append(tag)
    for tag, _ in counts.most_common():
        if tag not in ordered:
            ordered.append(tag)
    return ordered


QUALITY_GATE_RETRY_TAGS = {
    "too_similar",
    "copied_baseline",
    "source_template_carryover",
    "placeholder_output",
    "bad_splice",
    "overcompressed",
}


def quality_gate_tags(candidate: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for tag in candidate.get("failure_tags") or []:
        if tag in QUALITY_GATE_RETRY_TAGS and tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return ordered


def should_continue_refinement(
    *,
    selected: dict[str, Any],
    delta: float,
    margin: float,
    round_number: int,
    max_rounds: int,
) -> tuple[bool, list[str]]:
    blockers = quality_gate_tags(selected)
    if round_number >= max_rounds:
        return False, blockers
    score = selected.get("score") or {}
    if bool(score.get("hard_fail")) or delta < margin:
        return False, blockers
    return bool(blockers), blockers


def build_trace_markdown(trace_payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Evolution Trace")
    lines.append("")
    lines.append(f"- Session mode: `{trace_payload['session_mode']}`")
    lines.append(f"- Input mode: `{trace_payload['input_mode']}`")
    lines.append(f"- Total rounds: `{len(trace_payload['rounds'])}`")
    lines.append(f"- Improved any round: `{trace_payload['improved_any']}`")
    lines.append("")
    for round_payload in trace_payload["rounds"]:
        lines.append(f"## Round {round_payload['round']}")
        lines.append("")
        lines.append(f"- Baseline score at round start: `{round_payload['baseline_score']['final_score']}`")
        lines.append(
            f"- Revision mode: `{round_payload.get('revision_mode', 'rewrite')}` "
            f"({localize_revision_mode(round_payload.get('revision_mode', 'rewrite'))})",
        )
        lines.append(
            f"- Base text: `{round_payload.get('base_text_kind', 'source')}` "
            f"({localize_base_text_kind(round_payload.get('base_text_kind', 'source'))})",
        )
        lines.append(f"- Profiles tried: `{', '.join(round_payload['profiles'])}`")
        lines.append(f"- Incoming failure tags: `{', '.join(round_payload['failure_tags_in']) or 'none'}`")
        if round_payload["strategy_directives"]:
            lines.append("- Strategy directives:")
            lines.extend(f"  - {item}" for item in round_payload["strategy_directives"])
        lines.append(f"- Decision: `{round_payload['decision']}`")
        lines.append(f"- Reason: {round_payload['reason']}")
        if round_payload.get("quality_gate_tags"):
            lines.append(
                f"- Quality gate: blocked by `{', '.join(round_payload['quality_gate_tags'])}`",
            )
        else:
            lines.append("- Quality gate: passed")
        lines.append(f"- Next step: `{round_payload.get('next_step', 'stop')}`")
        lines.append(f"- Delta: `{round_payload['delta']}`")
        lines.append("")
        lines.append("| Candidate | Profile | Final | Rules | Hard Fail | Failure Tags |")
        lines.append("| --- | --- | ---: | ---: | --- | --- |")
        for candidate in round_payload["candidates"]:
            score = candidate.get("score") or {}
            lines.append(
                "| "
                f"{candidate['candidate_index']} | "
                f"{candidate['profile']} | "
                f"{float(score.get('final_score') or 0.0):.6f} | "
                f"{float(score.get('rule_score') or 0.0):.6f} | "
                f"{bool(score.get('hard_fail', False))} | "
                f"{', '.join(candidate.get('failure_tags') or []) or 'none'} |",
            )
        selected = round_payload["selected_candidate"]
        lines.append("")
        lines.append(f"### Selected Candidate: {selected['profile']}")
        lines.append("")
        lines.append("```text")
        lines.append(selected["text"])
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def localize_decision(decision: str) -> str:
    mapping = {
        "keep": "保留 challenger",
        "continue": "继续优化，进入下一轮",
        "discard": "丢弃 challenger，保留 baseline",
    }
    return mapping.get(decision, decision)


def localize_winner(winner: str) -> str:
    mapping = {
        "baseline": "基线版本",
        "challenger": "挑战版本",
    }
    return mapping.get(winner, winner)


def localize_reason(reason: str) -> str:
    mapping = {
        "challenger improved beyond threshold": "challenger 超过阈值并明显更优",
        "selected challenger did not improve enough": "选中的 challenger 提升不足，未达到保留阈值",
        "selected challenger improved and passed quality gate": "选中的 challenger 提升明显，且已经通过结果质量门",
        "selected challenger improved, but quality gate requested another round": "选中的 challenger 虽然提升明显，但还残留结构性问题，所以继续下一轮修复",
        "selected challenger improved, but round budget was exhausted": "选中的 challenger 已经提升，但轮次预算已用完，只能保留当前最优版本",
        "improvement below threshold": "提升幅度低于阈值",
        "challenger failed hard constraints": "challenger 未通过硬约束",
    }
    return mapping.get(reason, reason)


def localize_profile(profile: str) -> str:
    mapping = {
        "repair": "修复",
        "steady": "稳健",
        "reassuring": "安抚",
        "natural": "自然",
        "direct": "直接",
        "heuristic-natural": "启发式-自然",
        "heuristic-balanced": "启发式-平衡",
    }
    return mapping.get(profile, profile)


def localize_revision_mode(mode: str) -> str:
    mapping = {
        "rewrite": "从原文重写",
        "repair": "基于上一轮最佳版本继续修复",
    }
    return mapping.get(mode, mode)


def localize_base_text_kind(kind: str) -> str:
    mapping = {
        "source": "原文",
        "best_so_far": "上一轮最佳版本",
    }
    return mapping.get(kind, kind)


def localize_failure_tag(tag: str) -> str:
    mapping = {
        "template_tone": "模板腔",
        "hard_fail": "硬约束失败",
        "copied_baseline": "直接复用 baseline",
        "generation_error": "生成失败",
        "no_improvement": "没有提升",
        "wrong_audience": "对象错误",
        "too_short": "过短",
        "too_similar": "改写不明显",
        "source_template_carryover": "还保留了原文套话",
        "bad_splice": "句子拼接有问题",
        "placeholder_output": "像占位文本",
        "overcompressed": "压缩过头了",
        "regressed_from_best": "相对当前最优稿回退了",
        "too_vague": "过于空泛",
        "missing_must_include": "缺少必含信息",
        "none": "无",
    }
    return mapping.get(tag, tag)


def localize_failure_tags(tags: list[str]) -> str:
    if not tags:
        return "无"
    return "、".join(localize_failure_tag(tag) for tag in tags)


def localize_note(note: str) -> str:
    mapping = {
        "generation error: recovered candidate was too short": "生成失败：回收得到的候选文本过短",
        "generation error: recovered candidate did not satisfy hard constraints": "生成失败：回收得到的候选文本未满足硬约束",
        "generation error: recovered placeholder candidate": "生成失败：回收得到的是占位候选文本",
        "generation error: candidate recovery failed": "生成失败：候选文本恢复失败",
        "severe template carryover from source": "严重沿用了原文里的模板腔",
        "rewrite change is too small": "改写幅度太小，和原文仍然过近",
        "sentence structure is broken by phrase collision": "句子结构被重复尾句或连接语拼坏了",
        "candidate still looks like placeholder text": "候选文本看起来还是占位式内容",
        "rewrite removed too much source content": "改写时删掉了过多原文有效内容",
        "email reply could use clearer business context": "邮件正文的业务上下文还可以更明确",
        "too short for an email reply": "作为邮件回复偏短",
        "too short and vague for a complete reply": "作为完整文案偏短、偏泛",
        "could use clearer action or time detail": "动作或时间信息还可以更明确",
        "missing email-style greeting": "缺少邮件式称呼",
        "reads like a chat reply, not a full email body": "更像聊天回复，不像完整邮件正文",
        "email reply lacks clear body structure": "邮件正文结构不够清晰",
    }
    if note in mapping:
        return mapping[note]
    if note.startswith("contains template phrases: "):
        return "包含模板短语：" + note.split(": ", 1)[1]
    if note.startswith("retains source template phrases: "):
        return "保留了原文模板短语：" + note.split(": ", 1)[1]
    if note.startswith("rewrite too similar to source"):
        return "改写和原文过于相似"
    if note.startswith("rewrite still very close to source"):
        return "改写仍然和原文非常接近"
    if note.startswith("rewrite remains close to source"):
        return "改写和原文仍然偏近"
    if note.startswith("rewrite drops too much source detail"):
        return "改写删掉了太多原文细节"
    if note.startswith("rewrite is over-compressed for the source length"):
        return "相对原文长度来说，改写压缩得过头了"
    if note.startswith("rewrite is quite compressed compared with the source"):
        return "改写相对原文压缩得偏多"
    if note.startswith("contains placeholder-style ellipsis"):
        return "出现了省略号式占位表达"
    if note.startswith("contains placeholder-style content"):
        return "出现了占位式内容"
    if note.startswith("sentence splice issue: repeated lead-in connectors"):
        return "句子里重复出现引导连接语，读起来像拼接"
    if note.startswith("sentence splice issue: collided closing phrases"):
        return "一句话里叠了两个结尾短语，读起来像硬拼"
    if note.startswith("candidate reintroduces more template phrases than current best"):
        return "这个候选把当前最优稿已经压下去的模板短语又带回来了"
    if note.startswith("candidate drifts back toward source wording"):
        return "这个候选的措辞又往原文模板方向回去了"
    if note.startswith("candidate compresses current best too aggressively"):
        return "这个候选把当前最优稿压缩得太狠了"
    if note.startswith("candidate collapses paragraph structure compared with current best"):
        return "这个候选把当前最优稿的段落结构压塌了"
    return note


def build_user_visible_summary(
    *,
    task: str,
    compare_payload: dict[str, Any],
    baseline_text: str,
    challenger_text: str,
    session_trace: list[dict[str, Any]],
    report_html_path: str,
    trace_path: str,
) -> str:
    lines: list[str] = []
    lines.append("# Humanize 优化摘要")
    lines.append("")
    lines.append(f"- 任务：{task}")
    lines.append(
        f"- 最终决策：`{compare_payload['decision']}`（{localize_decision(compare_payload['decision'])}）",
    )
    lines.append(
        f"- 胜出版本：`{compare_payload['winner']}`（{localize_winner(compare_payload['winner'])}）",
    )
    lines.append(f"- 原因：{localize_reason(compare_payload['reason'])}")
    lines.append(f"- 分数变化：`{compare_payload['delta']}`")
    lines.append(f"- 保留阈值：`{compare_payload['margin']}`")
    lines.append("")
    winner_text = challenger_text if compare_payload.get("winner") == "challenger" else baseline_text
    lines.append("## 最终结果")
    lines.append("")
    lines.append("```text")
    lines.append(winner_text)
    lines.append("```")
    lines.append("")
    lines.append("## 基线版本")
    lines.append("")
    lines.append("```text")
    lines.append(baseline_text)
    lines.append("```")
    lines.append("")
    lines.append("## 最终挑战版本")
    lines.append("")
    lines.append("```text")
    lines.append(challenger_text)
    lines.append("```")
    lines.append("")
    lines.append("## 优化过程")
    lines.append("")
    for round_payload in session_trace:
        candidate_profiles = []
        for candidate in round_payload["candidates"]:
            profile = candidate.get("profile")
            if profile and profile not in candidate_profiles:
                candidate_profiles.append(profile)
        lines.append(f"### 第 {round_payload['round']} 轮")
        lines.append("")
        lines.append(
            f"- 本轮开始时的 baseline 分数：`{round_payload['baseline_score']['final_score']}`",
        )
        lines.append(
            f"- 修订模式：`{round_payload.get('revision_mode', 'rewrite')}`（{localize_revision_mode(round_payload.get('revision_mode', 'rewrite'))}）",
        )
        lines.append(
            f"- 本轮修订来源：`{round_payload.get('base_text_kind', 'source')}`（{localize_base_text_kind(round_payload.get('base_text_kind', 'source'))}）",
        )
        lines.append(
            f"- 本轮决策：`{round_payload['decision']}`（{localize_decision(round_payload['decision'])}）",
        )
        lines.append(f"- 原因：{localize_reason(round_payload['reason'])}")
        if round_payload.get("quality_gate_tags"):
            lines.append(
                f"- 结果质量门：未通过（残留问题：{localize_failure_tags(round_payload['quality_gate_tags'])}）",
            )
        else:
            lines.append("- 结果质量门：通过")
        next_step = str(round_payload.get("next_step") or "stop")
        next_step_text = "进入下一轮继续修复" if next_step == "continue" else "在本轮停止"
        lines.append(f"- 下一步：{next_step_text}")
        lines.append(f"- 分数变化：`{round_payload['delta']}`")
        lines.append(
            f"- 尝试的 profiles：`{'、'.join(localize_profile(profile) for profile in candidate_profiles)}`",
        )
        lines.append(
            f"- 继承的失败标签：`{localize_failure_tags(round_payload['failure_tags_in'])}`",
        )
        lines.append("")
        lines.append("| 候选 | Profile | 总分 | 规则分 | 硬失败 | 失败标签 |")
        lines.append("| --- | --- | ---: | ---: | --- | --- |")
        for candidate in round_payload["candidates"]:
            score = candidate.get("score") or {}
            lines.append(
                "| "
                f"{candidate['candidate_index']} | "
                f"{localize_profile(candidate['profile'])} | "
                f"{float(score.get('final_score') or 0.0):.6f} | "
                f"{float(score.get('rule_score') or 0.0):.6f} | "
                f"{bool(score.get('hard_fail', False))} | "
                f"{localize_failure_tags(candidate.get('failure_tags') or [])} |",
            )
        lines.append("")
        for candidate in round_payload["candidates"]:
            candidate_score = candidate.get("score") or {}
            lines.append(
                f"候选 {candidate['candidate_index']} · `{localize_profile(candidate['profile'])}`",
            )
            lines.append("")
            candidate_text = candidate.get("text") or ""
            if candidate_text.strip():
                lines.append("```text")
                lines.append(candidate_text)
                lines.append("```")
            else:
                lines.append("_该候选没有产出可用文本。_")
            if candidate.get("failure_tags"):
                lines.append("")
                lines.append("失败标签：" + localize_failure_tags(candidate["failure_tags"]))
            if (candidate_score.get("notes") or []):
                lines.append("")
                lines.append("备注：")
                lines.extend(f"- {localize_note(compact_note(note))}" for note in candidate_score["notes"])
            lines.append("")
        selected = round_payload["selected_candidate"]
        lines.append(
            "本轮选中候选："
            f"`{localize_profile(selected['profile'])}`（候选 {selected['candidate_index']}）"
        )
        lines.append("")
    lines.append("## 文件")
    lines.append("")
    lines.append(f"- 完整报告：`{report_html_path}`")
    lines.append(f"- 完整追踪 JSON：`{trace_path}`")
    lines.append("")
    return "\n".join(lines)


def compact_note(note: str) -> str:
    normalized = " ".join(str(note).split())
    if normalized.startswith("generation error:"):
        lowered = normalized.lower()
        if "too short" in lowered:
            return "generation error: recovered candidate was too short"
        if "hard constraints" in lowered:
            return "generation error: recovered candidate did not satisfy hard constraints"
        if "placeholder candidate" in lowered:
            return "generation error: recovered placeholder candidate"
        return "generation error: candidate recovery failed"
    if len(normalized) > 180:
        return normalized[:177] + "..."
    return normalized


def build_user_visible_html(markdown_text: str) -> str:
    escaped = (
        markdown_text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Humanize 优化摘要</title>
  <style>
    body {{
      margin: 0;
      padding: 28px;
      background: #f5f1e8;
      color: #1a1a17;
      font: 15px/1.6 "Iowan Old Style", "Palatino Linotype", serif;
    }}
    .shell {{
      max-width: 1100px;
      margin: 0 auto;
      background: rgba(255,250,240,0.94);
      border: 1px solid #d9d1c0;
      border-radius: 18px;
      box-shadow: 0 14px 34px rgba(24, 28, 34, 0.08);
      padding: 24px;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      font: 14px/1.65 ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
  </style>
</head>
<body>
  <main class="shell">
    <pre>{escaped}</pre>
  </main>
</body>
</html>
"""


def compare_payload_local(
    *,
    spec_path: Path,
    source_path: Path,
    baseline_path: Path,
    challenger_path: Path,
    baseline_score: dict[str, Any],
    challenger_score: dict[str, Any],
    margin: float,
) -> dict[str, Any]:
    delta = float(challenger_score.get("final_score") or 0.0) - float(baseline_score.get("final_score") or 0.0)
    if challenger_score.get("hard_fail"):
        decision = "discard"
        winner = "baseline"
        reason = "challenger failed hard constraints"
    elif delta >= margin:
        decision = "keep"
        winner = "challenger"
        reason = "challenger improved beyond threshold"
    else:
        decision = "discard"
        winner = "baseline"
        reason = "improvement below threshold"

    return {
        "decision": decision,
        "winner": winner,
        "reason": reason,
        "margin": margin,
        "delta": round(delta, 6),
        "spec_path": str(spec_path.resolve()),
        "source_path": str(source_path.resolve()) if source_path.exists() else "",
        "baseline": {
            "path": str(baseline_path.resolve()),
            **baseline_score,
        },
        "challenger": {
            "path": str(challenger_path.resolve()),
            **challenger_score,
        },
    }


def write_round_log_local(run_dir: Path, compare_payload: dict[str, Any]) -> None:
    payload = dict(compare_payload)
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    log_path = run_dir / "rounds.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    rounds_dir = run_dir / "rounds"
    rounds_dir.mkdir(exist_ok=True)
    index = len(list(rounds_dir.glob("round-*.json"))) + 1
    (rounds_dir / f"round-{index:03d}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def recover_candidate_from_response(response: dict[str, Any], hard_constraints: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    reasoning = str(message.get("reasoning_content") or "").strip()
    if not reasoning:
        return ""

    must_include = [str(item).strip() for item in hard_constraints.get("must_include") or [] if str(item).strip()]
    max_chars = hard_constraints.get("max_chars")

    def valid(text: str) -> bool:
        compact = text.replace(" ", "").replace("\n", "")
        if max_chars is not None and len(compact) > int(max_chars):
            return False
        if must_include and any(item not in text for item in must_include):
            return False
        return len(compact) >= 10

    quoted = [
        item.strip()
        for item in __import__("re").findall(r"[“\"「『]([^”\"」』\n]{8,140})[”\"」』]", reasoning)
        if item.strip()
    ]
    valid_quoted = [item for item in quoted if valid(item)]
    if valid_quoted:
        return max(valid_quoted, key=len)

    labeled = __import__("re").findall(
        r"(?:最终文案|最终回复|最终候选|候选文案|草稿|建议文案)\s*[：:]\s*([^\n]{8,160})",
        reasoning,
    )
    valid_labeled = [item.strip(" “\"'”") for item in labeled if valid(item.strip(" “\"'”"))]
    if valid_labeled:
        return max(valid_labeled, key=len)

    return ""


def is_placeholder_candidate(text: str) -> bool:
    stripped = text.strip().strip("。.!！?？")
    if not stripped:
        return True
    if "<最终文案>" in stripped or "FINAL_CANDIDATE" in stripped:
        return True
    if stripped in {"最终文案", "候选文案", "<最终文案>"}:
        return True
    return False


def satisfies_hard_constraints(text: str, hard_constraints: dict[str, Any]) -> bool:
    max_chars = hard_constraints.get("max_chars")
    must_include = [str(item).strip() for item in hard_constraints.get("must_include") or [] if str(item).strip()]
    compact = text.replace(" ", "").replace("\n", "")
    if max_chars is not None and len(compact) > int(max_chars):
        return False
    if must_include and any(item not in text for item in must_include):
        return False
    return True


def infer_progress_context(task: str) -> tuple[str, str]:
    issue = ""
    action = ""
    issue_match = re.search(r"卡在([^，。；;]+)", task)
    if issue_match:
        issue = issue_match.group(1).strip()
    action_match = re.search(r"(今天(?:已经|已)?[^，。；;]*(?:处理|跟进|确认|沟通))", task)
    if action_match:
        action = action_match.group(1).strip()
    return issue, action


def is_social_decline_task(task: str) -> bool:
    return (
        any(marker in task for marker in ("不要太冷淡", "别太冷淡", "婉拒", "聚餐", "约饭", "邀约"))
        or (
            "朋友" in task
            and "朋友圈" not in task
            and any(marker in task for marker in ("拒绝", "不去", "去不了", "今晚", "聚餐", "约饭", "邀约"))
        )
    )


COPY_TASK_MARKERS = (
    "差评",
    "评价",
    "评论",
    "公告",
    "社群",
    "自媒体",
    "小红书",
    "朋友圈",
    "文案",
    "口播",
    "种草",
    "招聘",
    "岗位",
    "JD",
    "jd",
    "商品详情",
    "电商",
    "直播",
    "课程",
    "退款",
    "客服",
    "售后",
    "App",
    "APP",
    "推送",
)


def build_fallback_baseline(task: str, hard_constraints: dict[str, Any]) -> str:
    must_include = [str(item).strip() for item in hard_constraints.get("must_include") or [] if str(item).strip()]
    max_chars = hard_constraints.get("max_chars")
    is_email = "邮件" in task or "email" in task.lower()
    time_term = infer_time_term(must_include, task)
    deadline = deadline_term(time_term)
    other_terms = [item for item in must_include if item != time_term]
    joined_terms = "、".join(other_terms)
    primary_term = other_terms[0] if other_terms else ""
    tail_terms = "、".join(other_terms[1:]) if len(other_terms) > 1 else ""
    is_social_decline = is_social_decline_task(task)
    is_service = any(marker in task for marker in ("售后", "投诉", "安抚", "破损", "退款", "退换"))
    is_app_push = any(marker in task for marker in ("App", "APP", "推送", "直播课")) and any(
        marker in task for marker in ("直播", "课程", "课")
    )
    is_repair_request = any(marker in task for marker in ("房东", "维修", "报修", "空调", "不制冷"))
    is_peer_sync = (
        any(marker in task for marker in ("同事", "飞书", "测试环境", "接口字段"))
        and any(marker in task for marker in ("同步", "调整", "变更"))
    )
    issue, action = infer_progress_context(task)

    if is_app_push:
        candidate = f"{time_term}直播课开始，记得来听。"
    elif is_email and any(marker in task for marker in ("合作方", "合同附件", "催资料", "项目排期")):
        candidate = f"您好，关于{primary_term or '合同编号'}的相关附件还没收到，会影响项目排期，麻烦{deadline}补一下，谢谢。"
    elif is_email and "客户" in task:
        candidate = (
            f"您好，\n\n目前这件事还在推进中，我们会围绕{joined_terms or '相关事项'}继续确认，"
            f"预计{time_term}给您更明确的回复。\n\n谢谢您的耐心等待。"
        )
    elif is_email:
        candidate = (
            f"您好，\n\n目前这件事还在推进中，我们会围绕{joined_terms or '相关事项'}继续确认，"
            f"预计{time_term}向您同步更明确的进展。\n\n谢谢。"
        )
    elif any(marker in task for marker in ("老板", "上级", "领导")):
        if issue or action:
            candidate = f"老板，{issue or joined_terms or '这件事'}{action or '今天已经跟进'}，{time_term}同步。"
        else:
            candidate = (
                f"老板，今天和{primary_term or '相关方'}已经沟通过了，{time_term}我把"
                f"{tail_terms or '进展'}整理好发您。"
            )
    elif is_social_decline:
        candidate = f"{time_term}不去了，今天要赶项目，下次吧。"
    elif is_repair_request:
        issue_text = "空调不制冷" if "空调" in task and "不制冷" in task else (primary_term or "这边")
        candidate = f"您好，{issue_text}，想约{time_term}维修，您看方便安排吗？"
    elif is_peer_sync:
        candidate = f"接口字段今天有调整，麻烦{deadline}同步到测试环境，谢谢。"
    elif is_service:
        candidate = f"您好，{joined_terms or '这件事'}这边在跟进，{time_term}给您回复。"
    elif "客户" in task:
        candidate = f"您好，{joined_terms or '这件事'}这边还在跟进，预计{time_term}给您明确回复。"
    elif len(must_include) >= 2:
        candidate = f"目前正在围绕{joined_terms or '相关事项'}继续确认，预计{time_term}给您明确回复。"
    elif len(must_include) == 1:
        candidate = f"目前正在推进中，预计{time_term}给您明确回复。"
    else:
        candidate = "您好，这件事我正在跟进，稍后给您同步最新进展。"

    compact = candidate.replace(" ", "").replace("\n", "")
    if max_chars is not None and len(compact) > int(max_chars):
        candidate = candidate[: int(max_chars)].rstrip("，。； ") + "。"
    return candidate


def call_and_extract_candidate(
    *,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens_sequence: list[int],
    hard_constraints: dict[str, Any],
    require_hard_constraints: bool,
    min_chars_hint: int,
) -> tuple[str, dict[str, Any]]:
    last_exc: Exception | None = None
    last_response: dict[str, Any] | None = None
    for index, max_tokens in enumerate(max_tokens_sequence):
        prompt = user_prompt
        if index > 0:
            prompt += "\n补充要求：不要继续展开分析，直接在最后给出 FINAL_CANDIDATE: <最终文案>。"
        response = call_chat(
            base_url=base_url,
            model=model,
            system_prompt=system_prompt,
            user_prompt=prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        last_response = response
        try:
            candidate = extract_content(response)
        except Exception as exc:
            recovered = recover_candidate_from_response(response, hard_constraints)
            if recovered:
                candidate = recovered
            else:
                last_exc = exc
                continue
        if is_placeholder_candidate(candidate):
            last_exc = RuntimeError(f"Recovered placeholder candidate: {candidate}")
            continue
        if require_hard_constraints and not satisfies_hard_constraints(candidate, hard_constraints):
            last_exc = RuntimeError(f"Recovered candidate does not satisfy hard constraints: {candidate}")
            continue
        compact = candidate.replace(" ", "").replace("\n", "")
        if min_chars_hint > 0 and len(compact) < min_chars_hint:
            last_exc = RuntimeError(f"Recovered candidate is too short: {candidate}")
            continue
        return candidate, response
    raise RuntimeError(
        "Failed to recover a final candidate from local generation"
        + (f": {last_exc}" if last_exc else "")
        + (f". Last response: {last_response}" if last_response else ""),
    )


def generate_candidate(
    *,
    run_dir: Path,
    spec: dict[str, Any],
    session_mode: str,
    task: str,
    hard_constraints: dict[str, Any],
    source_text: str,
    current_best_text: str,
    base_url: str,
    model: str,
    profile: str,
    strategy_directives: list[str],
    failure_tags: list[str],
    revision_mode: str,
    round_number: int,
    candidate_index: int,
    current_best_score_value: float,
    enforce_continuity: bool,
) -> dict[str, Any]:
    (_, _), (challenger_system, challenger_user) = build_generation_prompts(
        task=task,
        hard_constraints=hard_constraints,
        original=source_text if session_mode == "rewrite" else "",
        mode=session_mode,
        challenger_profile=profile,
        strategy_directives=strategy_directives,
        failure_tags=failure_tags,
        revision_mode=revision_mode,
    )
    candidate_text, response = call_and_extract_candidate(
        base_url=base_url,
        model=model,
        system_prompt=challenger_system,
        user_prompt=challenger_user.format(baseline=current_best_text),
        temperature=0.35,
        max_tokens_sequence=[220, 320, 420],
        hard_constraints=hard_constraints,
        require_hard_constraints=False,
        min_chars_hint=32 if ("邮件" in task or "email" in task.lower()) else (16 if "客户" in task else 12),
    )

    stem = f"round-{round_number:03d}-candidate-{candidate_index:02d}"
    generation_path = run_dir / "rounds" / f"{stem}.generation.json"
    candidate_path = run_dir / "rounds" / f"{stem}.txt"
    score_path = run_dir / "rounds" / f"{stem}.score.json"

    write_json(generation_path, response)
    write_text(candidate_path, candidate_text)

    score_obj = score_candidate(
        spec,
        candidate_text,
        source_text,
    )
    score_payload = score_obj.as_dict()
    failure_tags_out = extract_failure_tags(
        task,
        candidate_text,
        score_payload,
        baseline_score=current_best_score_value,
    )
    if candidate_text.strip() == current_best_text.strip() and "copied_baseline" not in failure_tags_out:
        failure_tags_out.append("copied_baseline")
    score_payload, failure_tags_out = apply_best_so_far_guardrails(
        candidate_text=candidate_text,
        current_best_text=current_best_text,
        source_text=source_text,
        score_payload=score_payload,
        failure_tags_out=failure_tags_out,
        enforce_continuity=enforce_continuity,
    )
    write_json(
        score_path,
        {
            "candidate_path": str(candidate_path.resolve()),
            "generation_path": str(generation_path.resolve()),
            **score_payload,
        },
    )

    return {
        "candidate_index": candidate_index,
        "profile": profile,
        "text": candidate_text,
        "candidate_path": str(candidate_path.resolve()),
        "generation_path": str(generation_path.resolve()),
        "score_path": str(score_path.resolve()),
        "score": score_payload,
        "failure_tags": failure_tags_out,
    }


def infer_time_phrase(text: str) -> str:
    match = re.search(r"周[一二三四五六日天](?:早上|上午|中午|下午|晚上)?", text)
    if match:
        return match.group(0)
    for phrase in ["明天下午", "明天上午", "今天下午", "今天上午", "明早", "明晚", "今早", "今晚", "今天", "本周内", "稍后"]:
        if phrase in text:
            return phrase
    return "稍后"


def infer_time_term(must_include: list[str], source_text: str) -> str:
    for item in must_include:
        if any(token in item for token in ("今天", "明天", "明早", "明晚", "今早", "今晚", "本周", "稍后")):
            return item
    return infer_time_phrase(source_text)


def deadline_term(time_term: str) -> str:
    if time_term.endswith(("前", "内")):
        return time_term
    return f"{time_term}前"


def looks_like_short_message_task(task: str, hard_constraints: dict[str, Any] | None = None) -> bool:
    lowered = task.lower()
    if "邮件" in task or "email" in lowered:
        return False
    markers = (
        "客户",
        "微信",
        "飞书",
        "同事",
        "回复",
        "消息",
        "沟通",
        "老板",
        "上级",
        "领导",
        "聚餐",
        "约饭",
        "邀约",
        "婉拒",
        "拒绝",
        "售后",
        "投诉",
        "安抚",
        "App",
        "APP",
        "推送",
        "直播课",
    )
    if any(marker in task for marker in markers) or is_social_decline_task(task):
        return True
    max_chars = (hard_constraints or {}).get("max_chars")
    return bool(max_chars and int(max_chars) <= 120 and len(task) <= 80)


def looks_like_professional_email(task: str, source_text: str) -> bool:
    lowered_task = task.lower()
    if ("微信" in task or "wechat" in lowered_task or "消息" in task) and not ("邮件" in task or "email" in lowered_task):
        return False
    if any(marker in task for marker in COPY_TASK_MARKERS) and not ("邮件" in task or "email" in lowered_task):
        return False
    if "邮件" in task or "email" in lowered_task:
        return True
    markers = (
        "尊敬的",
        "贵公司",
        "进一步通知",
        "岗位职责",
        "有序处理中",
        "竭诚为您服务",
        "感谢您的时间与安排",
        "如您后续还需要我补充",
    )
    return any(marker in source_text for marker in markers)


def _normalize_compare_text(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    return re.sub(r"[，。！？；：、,.!?:;\"'“”‘’（）()《》【】\\[\\]<>·—…-]", "", compact)


def _similarity_ratio(left: str, right: str) -> float:
    left_norm = _normalize_compare_text(left)
    right_norm = _normalize_compare_text(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _template_phrase_hits(text: str) -> list[str]:
    return [phrase for phrase in DEFAULT_TEMPLATE_PHRASES if phrase in text]


def _compact_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _generic_template_repair_replacements(*, natural: bool) -> list[tuple[str, str]]:
    replacements = [
        (r"成长这件事的底层逻辑", "成长这件事到底该怎么想"),
        (r"底层逻辑", "更核心的问题" if not natural else "根子上的问题"),
        (r"认知升级系统", "更适合自己的思考方式"),
        (r"行动迭代机制", "行动节奏"),
        (r"成长闭环", "一套能持续转起来的方法" if not natural else "一套能转起来的方法"),
        (r"低质量勤奋", "表面上的忙" if not natural else "瞎忙"),
        (r"系统化交流", "一起展开交流" if not natural else "一起好好聊聊"),
        (r"高质量学习型社群", "更愿意认真交流的社群" if not natural else "更愿意认真交流的群"),
        (r"系统化理解", "真正理解"),
        (r"高价值直播分享", "直播分享" if not natural else "直播"),
        (r"可持续增长", "更稳的增长"),
        (r"结构化表达", "更有条理地表达"),
        (r"影响力输出能力", "把重点说清楚的能力"),
        (r"个人竞争力跃迁", "个人状态往前走一截"),
        (r"释放增长潜能", "把增长往前推一推"),
        (r"深度拆解", "尽量讲具体"),
        (r"全面提升", "慢慢提上来"),
        (r"数字化浪潮", "现在这波数字化变化"),
        (r"数字化转型", "数字化升级"),
        (r"多维度提升", "一起往前提"),
        (r"组织协同体系", "团队协作方式"),
        (r"感谢您的理解与支持", "谢谢您的理解"),
        (r"感谢您的耐心等待", "感谢您的耐心"),
        (r"欢迎随时与我们联系", "欢迎随时联系我"),
        (r"我们将竭诚为您服务", "我会继续跟进"),
    ]
    return replacements


def _apply_generic_template_repairs(text: str, *, natural: bool) -> str:
    return _apply_replacements(text, _generic_template_repair_replacements(natural=natural))


def rewrite_professional_email(
    source_text: str,
    *,
    natural: bool,
    repair: bool = False,
    source_reference: str = "",
    failure_tags: list[str] | None = None,
) -> str:
    text = _compact_copy(source_text)
    failure_tags = failure_tags or []
    replacements: list[tuple[str, str]] = [
        (r"^尊敬的[^，。]*，您好。?", "您好。"),
        (r"尊敬的客户，您好。?", "您好。"),
        (r"尊敬的招聘负责人，您好。?", "您好。"),
        (r"您好，冒昧打扰。?", "您好，打扰了。"),
        (r"非常感谢贵公司日前为我提供本次面试机会。", "感谢您安排这次面试。"),
        (r"非常感谢贵司此前给予我的面试机会。?", "感谢您前面给我的面试机会。"),
        (r"我想就此前参与的([^。]*?)与您进行一次礼貌性的跟进确认", r"想跟您跟进一下此前参与的\1"),
        (r"我想就([^。]*?)与您进行一次礼貌性的跟进确认", r"想跟您跟进一下\1"),
        (r"通过本次沟通，我对([^。]+)有了更加全面且深入的理解，也进一步增强了我希望加入贵公司的意愿。", r"这次沟通后，我对\1有了更具体的了解，也更希望有机会加入贵公司。"),
        (r"结合本次面试交流内容，我更加确信自身过往经历与该岗位需求之间具有较高的匹配度，也期待后续能够有机会继续推进相关流程。", "这次交流也让我更确认，自己的经历和岗位需求比较匹配，也期待后续能继续推进。"),
        (r"面试结束后，我对该岗位的理解进一步加深，也更加期待能够加入团队贡献自己的价值。?", "面试结束后，我对这个岗位的理解更具体了一些，也更希望有机会加入团队。"),
        (r"冒昧来信是想礼貌性跟进一下当前招聘流程的最新进展，?", "这封邮件主要是想跟进一下目前的招聘进度。"),
        (r"若后续还需要我补充任何信息或材料，我都会第一时间配合提供。?", "如果后面还需要我补充材料或信息，我也可以马上配合。"),
        (r"感谢您的时间与关注，期待您的回复。?", "谢谢您，期待您的回复。"),
        (r"针对您当前咨询的([^。]*?)事项", r"关于您咨询的\1"),
        (r"关于您当前咨询的([^。]*?)事项", r"关于您咨询的\1"),
        (r"我方已经同步协调", "我们已经和"),
        (r"我司已第一时间协调", "这边已经联系"),
        (r"相关财务同事进行进一步核实与推进", "财务同事确认并继续跟进"),
        (r"相关同事进行进一步核实与推进", "相关同事确认并继续跟进"),
        (r"相关部门进行核查处理", "相关同事核实处理"),
        (r"礼貌性的跟进确认", "跟进一下"),
        (r"进一步核实与推进", "继续确认和跟进"),
        (r"现阶段整体流程正在有序处理中", "目前这件事还在处理中"),
        (r"目前物流链路正在持续推进中", "目前物流这边还在跟进"),
        (r"预计会在([^，。]+)为您提供更加清晰和完整的反馈说明", r"预计\1给您更明确的回复"),
        (r"预计将在24小时内同步最新进展", "预计24小时内给您同步最新进展"),
        (r"如目前流程仍在推进中，我将持续保持关注，并期待后续有进一步消息。?", "如果流程还在推进，也想麻烦您有消息时告诉我一声。"),
        (r"如您后续还需要我补充任何材料、信息或案例，欢迎随时与我联系，我将第一时间积极配合。", "如果后续还需要我补充材料、信息或案例，欢迎随时联系我，我会第一时间配合。"),
        (r"再次感谢您的时间与安排，期待您的进一步通知。?", "再次感谢您的时间和安排，期待您的回复。"),
        (r"感谢您的耐心等待与理解支持", "感谢您的耐心"),
        (r"感谢您在百忙之中查阅此邮件", "感谢您抽空看这封邮件"),
        (r"感谢您的理解与支持", "谢谢您的理解"),
        (r"欢迎随时与我们联系，我们将竭诚为您服务。?", "如有需要，欢迎随时联系我。"),
        (r"如后续您有任何疑问，欢迎随时与我们联系，我?们?将竭诚为您服务。?", "如有其他问题，欢迎随时联系我。"),
        (r"我们注意到贵公司当前正处于数字化转型和业务增长的关键阶段，因此希望向您推荐我司全新升级的一站式智能营销解决方案。?", "看到贵公司最近可能也在关注增长和数字化这块，所以想跟您简单介绍一下我们的智能营销方案。"),
        (r"该方案能够从多维度提升线索获取效率、客户触达质量以及转化链路表现，帮助企业在复杂多变的市场环境中构建更加稳健和可持续的增长体系。?", "它主要是帮团队把线索获取、客户触达和转化跟进这几件事做得更顺一点。"),
        (r"如您方便，我们期待与您安排一次简短沟通，以便进一步介绍相关能力与合作价值。?", "如果您方便，我想约个十几分钟，简单看看这块对您现在的业务有没有帮助。"),
    ]
    if natural:
        replacements.extend(
            [
                (r"您好。", "您好，"),
                (r"这次沟通后，我对([^。]+)有了更具体的了解，也更希望有机会加入贵公司。", r"和您沟通之后，我对\1的理解更具体了，也更希望有机会加入贵公司。"),
                (r"关于您咨询的([^。]+)，我们已经和", r"关于您咨询的\1，这边我们已经和"),
                (r"想跟您简单介绍一下", "想先跟您简单聊一下"),
            ],
        )
    text = _apply_replacements(text, replacements)
    if repair:
        if {"template_tone", "source_template_carryover", "too_similar"} & set(failure_tags):
            text = _apply_generic_template_repairs(text, natural=natural)
        if "bad_splice" in failure_tags:
            text = cleanup_common_phrase_collisions(text)
        if "overcompressed" in failure_tags and source_reference:
            source_reference = _compact_copy(source_reference)
            if _compact_len(text) < _compact_len(source_reference) * 0.72:
                source_sentences = [part.strip() for part in re.split(r"[。！？]", source_reference) if part.strip()]
                text_sentences = [part.strip() for part in re.split(r"[。！？]", text) if part.strip()]
                for sentence in source_sentences:
                    if sentence and all(_similarity_ratio(sentence, existing) < 0.72 for existing in text_sentences):
                        text = text.rstrip("。") + "。\n\n" + sentence + "。"
                        break
    text = re.sub(r"([。！？])(?=[^\n])", r"\1\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def build_email_rewrite_heuristics(
    task: str,
    source_text: str,
    current_best_text: str,
    failure_tags: list[str],
    *,
    repair: bool,
) -> list[tuple[str, str]]:
    if not looks_like_professional_email(task, source_text):
        return []
    base_text = current_best_text if repair else source_text
    return [
        (
            "heuristic-natural",
            rewrite_professional_email(
                base_text,
                natural=True,
                repair=repair,
                source_reference=source_text,
                failure_tags=failure_tags,
            ),
        ),
        (
            "heuristic-balanced",
            rewrite_professional_email(
                base_text,
                natural=False,
                repair=repair,
                source_reference=source_text,
                failure_tags=failure_tags,
            ),
        ),
    ]


def _compact_copy(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"[ \t]+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_like_longform_rewrite(task: str, source_text: str) -> bool:
    compact = re.sub(r"\s+", "", source_text)
    is_copy_task = any(marker in task for marker in COPY_TASK_MARKERS) or "更像真人" in task
    ai_copy_markers = (
        "底层逻辑",
        "认知升级",
        "行动迭代",
        "成长闭环",
        "高效修护体验",
        "多重植萃",
        "肌肤底层",
        "高度自驱力",
        "优秀沟通协作能力",
        "复杂问题拆解能力",
        "业务需求洞察",
        "产品方案设计",
        "跨部门资源协调",
        "项目落地推进",
        "高品质亲肤面料",
        "人体工学剪裁",
        "时尚表现力",
        "系统化理解",
        "内容增长方法论",
        "高价值直播",
        "转化路径",
        "可持续增长",
        "系统化表达训练",
        "全面提升",
        "竞争力跃迁",
        "退款诉求",
        "规定时效",
        "相关部门进行核实处理",
    )
    source_looks_template = any(marker in source_text for marker in ai_copy_markers)
    generic_longform_shape = len(compact) >= 90 and source_text.count("。") >= 1
    min_chars = 45 if is_copy_task else 80
    return len(compact) >= min_chars and (
        is_copy_task
        or source_looks_template
        or "评论区" in source_text
        or "大家" in source_text[:80]
        or generic_longform_shape
    )


def _apply_replacements(text: str, replacements: list[tuple[str, str]]) -> str:
    updated = text
    for pattern, repl in replacements:
        updated = re.sub(pattern, repl, updated)
    return updated


def rewrite_longform_copy(
    source_text: str,
    *,
    natural: bool,
    repair: bool = False,
    source_reference: str = "",
    failure_tags: list[str] | None = None,
) -> str:
    text = _compact_copy(source_text)
    failure_tags = failure_tags or []
    replacements: list[tuple[str, str]] = [
        (r"在当下这个充满不确定性却又蕴含巨大机会的时代", "这几年变化真的很快"),
        (r"每一个普通人都比以往任何时候更需要重新思考", "普通人真的得重新想想"),
        (r"很多人表面上看起来很努力，也投入了大量时间与精力，但最终却没有获得预期中的结果。", "很多人明明也很努力，时间精力都花了，但结果还是不理想。"),
        (r"为什么会这样？我认为，核心原因并不在于", "为什么会这样？问题往往不在于"),
        (r"有没有建立起一套真正适合自己的认知升级系统和行动迭代机制", "有没有形成一套适合自己的思考方式和行动节奏"),
        (r"一个人真正拉开差距的，从来都不是短期的爆发，而是长期稳定的自我优化能力。", "真正拉开差距的，往往不是一时爆发，而是能不能长期调整自己。"),
        (r"那些能够持续复盘、持续修正、持续精进的人，往往更容易在复杂环境中找到自己的节奏，并不断放大自己的优势。", "那些愿意持续复盘、修正、往前走的人，更容易在变化里找到自己的节奏，把优势慢慢做出来。"),
        (r"相反，如果一个人只是重复旧有路径、依赖惯性前进，那么即便看起来很忙，也很可能只是停留在低质量勤奋的循环之中。", "如果一个人只是按老路子走，靠惯性往前推，那看起来再忙，也可能只是在低质量勤奋里打转。"),
        (r"所以我想告诉大家的是，", "所以我想说的是，"),
        (r"未来真正重要的能力，不只是执行力，不只是学习力，也不只是单点突破的能力，而是把认知、方法、反馈和行动连接起来，形成一个真正可以持续运转的成长闭环。", "以后真正重要的，不只是执行力、学习力这些单点能力，更重要的是能不能把认知、方法、反馈和行动连起来，形成自己的成长闭环。"),
        (r"只有这样，一个人才有可能在变化中不断完成自我更新，并在长期竞争中获得更大的确定性。", "这样你才有机会在变化里不断更新自己，也更容易在长期里站稳。"),
        (r"如果你最近也正在经历迷茫、焦虑或者迟迟找不到突破口的阶段，", "如果你最近也有点迷茫、焦虑，或者一直找不到突破口，"),
        (r"我真心希望你可以认真想一想：", "不妨停下来想一想："),
        (r"你现在所坚持的东西，到底是在帮助你走向更高质量的成长，还是只是在消耗你的时间和注意力。", "你现在坚持的东西，到底真的在帮你成长，还是只是在消耗你的时间和注意力。"),
        (r"希望今天这段分享，能够给你带来一点新的思考和启发。", "希望这段话能给你一点启发。"),
        (r"如果你也有类似感受，欢迎在评论区分享你的看法。", "如果你也有类似感受，评论区聊聊。"),
        (r"很多人之所以一直没有办法真正建立起稳定的自律体系，核心原因并不是缺少目标感，而是没有找到一套能够持续运行的行为触发机制。?", "很多人一直没办法稳定自律，可能不是因为目标不够清楚，而是没有找到一个能让自己坚持下去的具体做法。"),
        (r"真正有效的自律，从来不是依靠短期情绪驱动，而是通过环境设计、即时反馈和低成本启动，逐步形成一种可以被长期复用的生活系统。?", "真正能坚持下来的自律，不能只靠一时兴起。更现实的做法是把环境调整好，及时给自己一点反馈，再从很小的动作开始，慢慢变成不用太费劲也能持续的习惯。"),
        (r"在这个快节奏的时代，我们总是在不断追逐目标，却常常忽略了生活本身真正值得被感知的细节。?", "最近越来越觉得，人一忙起来，就很容易忘了好好感受生活。"),
        (r"或许真正的松弛感，并不是逃离当下，而是在每一个普通瞬间里重新找回与自己相处的能力。?", "松弛感好像也不是非要逃到哪里去，而是在普通的一天里，还能留一点时间跟自己待一会儿。"),
        (r"在数字化浪潮持续深入企业经营全流程的今天，?", "现在很多团队都在用各种数字化工具。"),
        (r"越来越多团队开始意识到，单一工具已经无法满足复杂业务场景下的协同提效需求。?", "但用着用着大家会发现，只靠一个单点工具，很难把复杂协作真正跑顺。"),
        (r"我们的智能协作平台通过多模块能力整合、自动化流程编排以及数据驱动的决策支持，?", "我们这个智能协作平台，主要是把模块能力、流程自动化和数据分析放在一起，"),
        (r"帮助企业构建更加高效、稳定、可持续的组织协同体系，?", "帮团队少一点来回沟通，多一点清楚的推进节奏，"),
        (r"从而在激烈的市场竞争中持续释放增长潜能。?", "最后把协作效率和业务增长都往前推一推。"),
        (r"在快节奏生活方式不断升级的当下，?", "最近护肤步骤一多，我反而更在意有没有真的省心、好用。"),
        (r"越来越多精致女性开始关注日常护肤流程中的高效修护体验。?", "像精华这种每天都会用的东西，我会更看重修护感和稳定度。"),
        (r"这款精华通过多重植萃成分协同作用，深入肌肤底层，帮助改善干燥、暗沉与屏障脆弱等多维度肌肤问题，让肌肤在持续使用中焕发自然光彩。?", "这支用下来比较明显的是保湿感不错，干燥和暗沉的时候上脸也不会有负担。坚持用一段时间，皮肤状态会更稳一点。"),
        (r"我们正在寻找一位具备高度自驱力、优秀沟通协作能力以及复杂问题拆解能力的([^。]*?)候选人。?", r"我们想找一位\1，最好是比较主动、沟通顺畅，也能把复杂问题拆清楚的人。"),
        (r"你将深度参与业务需求洞察、产品方案设计、跨部门资源协调与项目落地推进，并通过数据分析与用户反馈持续优化产品体验，推动业务目标实现。?", "你会一起看业务需求、做产品方案，也会和不同团队对齐推进。后面还需要结合数据和用户反馈，把产品体验继续打磨好。"),
        (r"本产品采用高品质亲肤面料，结合人体工学剪裁设计，兼具舒适性、透气性与时尚表现力。?", "这件衣服用的是比较亲肤的面料，版型也做得比较贴合日常穿着。"),
        (r"这款产品采用高品质原材料与先进工艺打造，兼顾功能性、舒适性与审美表现。?", "这款东西用料和做工都比较扎实，功能性、舒适度和外观也都兼顾到了。"),
        (r"无论是日常通勤、休闲出行还是居家穿搭，都能够为用户提供更加自在、轻盈且高级的穿着体验。?", "平时通勤、出门或者在家穿都可以，整体是那种轻松、不紧绷，也不太挑场景的感觉。"),
        (r"无论是在日常通勤、家庭使用还是多场景切换中，都能够为用户提供稳定、轻松且高效率的使用体验。?", "平时通勤、在家用，或者不同场景切换的时候，用起来都比较顺手，不会让人觉得麻烦。"),
        (r"我们希望通过更加细致的设计与持续优化的细节打磨，为用户带来真正意义上的品质升级。?", "我们更想把细节慢慢做好，让你用的时候能明显感觉到体验更顺一点。"),
        (r"为了帮助大家系统化理解短视频内容增长方法论，我们将在今晚八点开启一场高价值直播分享。?", "今晚八点我会开一场直播，聊聊短视频内容到底怎么做增长。"),
        (r"本次直播将围绕账号定位、内容选题、流量承接与转化路径展开深度拆解，帮助大家构建可持续增长的内容运营体系。?", "会讲账号定位、选题、流量怎么接住，以及后面怎么转化。尽量讲得具体一点，别只停留在概念上。"),
        (r"为了帮助大家[^。！？]{0,120}?我们将在今晚八点开启一场高价值直播分享，?", "今晚八点我会开一场直播，想和大家聊聊短视频到底怎么做增长。"),
        (r"围绕账号定位、内容策划、流量获取与转化路径展开深度讲解，欢迎大家准时进入直播间共同学习成长。?", "会讲账号定位、内容怎么策划、流量和转化这些问题，感兴趣的话可以来直播间聊聊。"),
        (r"如果你正在经历表达能力不足、公众演讲紧张以及职场沟通效率低下等问题，?", "如果你一到发言就紧张，或者在职场里总觉得自己说不清楚，"),
        (r"那么这套系统化表达训练课程将帮助你从底层逻辑出发，全面提升结构化表达、临场反应和影响力输出能力，真正实现个人竞争力跃迁。?", "这套表达课会从怎么组织内容、怎么临场回应、怎么把话说得更有重点这几块练起，让你在沟通和汇报时更稳一点。"),
        (r"尊敬的客户您好，?", "您好，"),
        (r"您好，针对您反馈的订单延迟配送问题，?", "您好，您反馈的订单配送慢这个问题，"),
        (r"针对您反馈的订单延迟配送问题，?", "您反馈的订单配送慢这个问题，"),
        (r"关于您反馈的物流延迟问题，我们已第一时间联系相关承运方进行核实处理。?", "您反馈的物流慢这个问题，我这边已经联系承运方核实了。"),
        (r"我司已第一时间协调相关部门进行核查处理。?", "我这边已经联系相关同事核实了。"),
        (r"目前物流链路正在持续推进中，预计将在24小时内同步最新进展，感谢您的理解与支持。?", "目前物流这边还在跟进，预计24小时内给您同步最新进展，也谢谢您的理解。"),
        (r"目前包裹正在加急转运途中，具体送达时效以物流页面更新为准。?", "目前包裹还在加急转运，具体什么时候送到，还要以物流更新为准。"),
        (r"给您带来的不便我们深表歉意，感谢您的理解与支持，如后续您有任何疑问，欢迎随时与我们联系。?", "给您添麻烦了，确实不好意思。后面如果还有问题，随时找我。"),
        (r"关于您反馈的退款诉求，我们已经第一时间提交相关部门进行核实处理。?", "您反馈的退款问题我们已经提交处理了。"),
        (r"后续将在规定时效内为您同步最新进展，请您耐心等待。?", "这边有进展我会及时同步给您，您不用反复催。"),
        (r"感谢您的理解与支持，祝您生活愉快。?", "也谢谢您理解。"),
        (r"亲爱的各位伙伴，大家好。?", "大家好，"),
        (r"亲爱的朋友们大家好！?", "大家好，"),
        (r"为了进一步提升社群整体互动质量与内容传播效率，我们将于([^，。]+)正式开展一次主题分享活动。?", r"\1我们会做一场主题分享。"),
        (r"本次活动将围绕个人成长、认知升级与行动闭环展开系统化交流，希望大家积极参与，共同打造高质量学习型社群。?", "这次主要聊个人成长和行动复盘，感兴趣的朋友可以一起参加，现场也欢迎大家多交流。"),
        (r"本次活动将围绕([^，。]+)展开系统化交流，希望大家积极参与，共同打造高质量学习型社群。?", r"这次主要聊\1，感兴趣的朋友可以一起参加，现场也欢迎大家多交流。"),
        (r"围绕([^，。]+)展开系统化交流", r"聊聊\1"),
        (r"共同打造高质量学习型社群", "也让群里的交流更实在一点"),
        (r"尊敬的用户您好，?", "您好，"),
        (r"非常抱歉本次服务体验未能达到您的预期。?", "这次体验没做好，先跟您说声抱歉。"),
        (r"针对您反馈的问题，我们已第一时间高度重视并同步相关部门进行全面排查与优化。?", "您反馈的问题我们已经记下来了，也会尽快和相关同事一起看具体原因。"),
        (r"后续我们也将持续完善服务流程，努力为每一位用户提供更加优质、高效、贴心的服务体验。?", "后面我们会把这个环节再梳理一下，避免类似情况反复出现。"),
        (r"感谢您的宝贵反馈，祝您生活愉快。?", "也谢谢您愿意把问题说出来。"),
        (r"很多人之所以一直没有办法真正建立起([^，。]+)，核心原因并不是([^，。]+)，而是([^。]+)。?", r"很多人一直没能真正建立起\1，不一定是因为\2，更多是因为\3。"),
        (r"真正有效的([^，。]+)，从来不是依靠([^，。]+)，而是通过([^，。]+)，逐步形成([^。]+)。?", r"真正有效的\1，靠的不是\2，而是\3，慢慢形成\4。"),
    ]
    if natural:
        replacements.extend(
            [
                (r"这几年变化真的很快，普通人真的得重新想想", "这几年变化真的很快，我越来越觉得，普通人得重新想想"),
                (r"所以我想说的是，", "说实话，我越来越觉得，"),
                (r"很多人一直没办法稳定自律，可能不是因为目标不够清楚，而是没有找到一个能让自己坚持下去的具体做法。", "说白了，很多人不是不想自律，也不是完全没目标，而是一直没找到一个真的能坚持下去的方法。"),
                (r"真正能坚持下来的自律，不能只靠一时兴起。更现实的做法是把环境调整好，及时给自己一点反馈，再从很小的动作开始，慢慢变成不用太费劲也能持续的习惯。", "真正能坚持下来的自律，靠的也不是突然打鸡血。更有用的办法，反而是把环境先调整好，给自己一点及时反馈，再从特别小的动作开始。这样时间久了，它才会慢慢变成一种没那么费劲的习惯。"),
                (r"最近越来越觉得，人一忙起来，就很容易忘了好好感受生活。", "最近越来越觉得，人一忙起来，真的很容易忘了好好感受生活。"),
                (r"松弛感好像也不是非要逃到哪里去，而是在普通的一天里，还能留一点时间跟自己待一会儿。", "所谓松弛感，好像也不是非要逃到哪里去。更多时候，就是在很普通的一天里，还能留一点时间跟自己待一会儿。"),
                (r"我们这个智能协作平台，主要是把模块能力、流程自动化和数据分析放在一起，", "我们这个智能协作平台，简单说就是把模块能力、流程自动化和数据分析放在一起，"),
                (r"帮团队少一点来回沟通，多一点清楚的推进节奏，", "让团队少一点来回沟通，多一点清楚的推进节奏，"),
                (r"最近护肤步骤一多，我反而更在意有没有真的省心、好用。", "最近护肤步骤一多，我反而更在意这东西到底省不省心、好不好用。"),
                (r"像精华这种每天都会用的东西，我会更看重修护感和稳定度。", "像精华这种每天都会用的东西，我最看重的还是修护感和稳定度。"),
                (r"你会一起看业务需求、做产品方案，也会和不同团队对齐推进。", "你会一起看业务需求、做产品方案，也要和不同团队对齐推进。"),
                (r"这件衣服用的是比较亲肤的面料，版型也做得比较贴合日常穿着。", "这件衣服摸起来比较舒服，版型也更适合日常穿。"),
                (r"今晚八点我会开一场直播，聊聊短视频内容到底怎么做增长。", "今晚八点我会开一场直播，聊聊短视频到底怎么做增长。"),
                (r"这套表达课会从怎么组织内容、怎么临场回应、怎么把话说得更有重点这几块练起，让你在沟通和汇报时更稳一点。", "这套表达课会从组织内容、临场回应、把重点说清楚这几块练起，让你沟通和汇报时更稳一点。"),
                (r"您反馈的退款问题我们已经提交处理了。", "您反馈的退款问题，我这边已经提交处理了。"),
                (r"大家好，", "大家好，跟大家同步一下，"),
                (r"这次主要聊个人成长和行动复盘，感兴趣的朋友可以一起参加，现场也欢迎大家多交流。", "这次主要聊个人成长和行动复盘，不会太正式，感兴趣的朋友可以一起来，现场也欢迎大家多聊聊。"),
                (r"这次主要聊([^，。]+)，感兴趣的朋友可以一起参加，现场也欢迎大家多交流。", r"这次主要聊\1，不会太正式，感兴趣的朋友可以一起来，现场也欢迎大家多聊聊。"),
                (r"这次体验没做好，先跟您说声抱歉。", "这次体验没做好，先跟您说声抱歉。"),
                (r"您反馈的问题我们已经记下来了，也会尽快和相关同事一起看具体原因。", "您反馈的问题我们已经记下来了，我会尽快和相关同事一起看具体原因。"),
            ],
        )
    text = _apply_replacements(text, replacements)
    if repair:
        if {"template_tone", "source_template_carryover", "too_similar"} & set(failure_tags):
            text = _apply_generic_template_repairs(text, natural=natural)
            text = _apply_replacements(
                text,
                [
                    (r"重新想想成长这件事的根子上的问题", "重新想想成长这件事到底该怎么想"),
                    (r"成长这件事的更核心的问题", "成长这件事到底该怎么想"),
                    (r"低质量勤奋里打转", "只是在瞎忙" if natural else "只是在表面上忙"),
                    (r"形成自己的一套能转起来的方法", "形成一套适合自己的方法"),
                    (r"形成自己的一套能持续转起来的方法", "形成一套适合自己的方法"),
                    (r"把认知、方法、反馈和行动连起来", "把想法、方法、反馈和行动串起来"),
                    (r"把想法、方法、反馈和行动连起来", "把想法、方法、反馈和行动串起来"),
                    (r"说实话，我越来越觉得，", "我这段时间越来越觉得，" if natural else "说到底，"),
                ],
            )
        if "bad_splice" in failure_tags:
            text = cleanup_common_phrase_collisions(text)
        if "overcompressed" in failure_tags and source_reference:
            source_reference = _compact_copy(source_reference)
            if _compact_len(text) < _compact_len(source_reference) * 0.7:
                source_paragraphs = [part.strip() for part in source_reference.split("\n\n") if part.strip()]
                text_paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
                for paragraph in source_paragraphs:
                    if paragraph and all(_similarity_ratio(paragraph, existing) < 0.72 for existing in text_paragraphs):
                        patched = rewrite_longform_copy(
                            paragraph,
                            natural=natural,
                            repair=False,
                        )
                        text = text.rstrip() + "\n\n" + patched.strip()
                        break
    text = re.sub(r"不只是([^，。]+)，不只是([^，。]+)，也不只是([^，。]+)，而是", r"不只是\1、\2、\3，更重要的是", text)
    text = re.sub(r"([。！？])(?=[^\n])", r"\1\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def build_longform_rewrite_heuristics(
    task: str,
    source_text: str,
    current_best_text: str,
    failure_tags: list[str],
    *,
    repair: bool,
) -> list[tuple[str, str]]:
    if not looks_like_longform_rewrite(task, source_text):
        return []
    base_text = current_best_text if repair else source_text
    natural = rewrite_longform_copy(
        base_text,
        natural=True,
        repair=repair,
        source_reference=source_text,
        failure_tags=failure_tags,
    )
    balanced = rewrite_longform_copy(
        base_text,
        natural=False,
        repair=repair,
        source_reference=source_text,
        failure_tags=failure_tags,
    )
    return [
        ("heuristic-natural", natural),
        ("heuristic-balanced", balanced),
    ]


def build_short_reply_heuristics(
    task: str,
    source_text: str,
    hard_constraints: dict[str, Any],
) -> list[tuple[str, str]]:
    lowered = task.lower()
    if "邮件" in task or "email" in lowered:
        return []
    if not looks_like_short_message_task(task, hard_constraints):
        return []
    must_include = [str(item).strip() for item in hard_constraints.get("must_include") or [] if str(item).strip()]
    max_chars = hard_constraints.get("max_chars")
    time_term = infer_time_term(must_include, f"{task} {source_text}")
    deadline = deadline_term(time_term)
    required_terms = [item for item in must_include if item != time_term]
    is_internal = any(marker in task for marker in ("老板", "上级", "领导"))
    is_service = any(marker in task for marker in ("售后", "投诉", "安抚"))
    is_app_push = any(marker in task for marker in ("App", "APP", "推送", "直播课")) and any(
        marker in task for marker in ("直播", "课程", "课")
    )
    is_repair_request = any(marker in task for marker in ("房东", "维修", "报修", "空调", "不制冷"))
    is_peer_sync = (
        any(marker in task for marker in ("同事", "飞书", "测试环境", "接口字段"))
        and any(marker in task for marker in ("同步", "调整", "变更"))
    )
    is_social_decline = is_social_decline_task(task)
    joined_terms = "、".join(required_terms)
    first_term = required_terms[0] if required_terms else ""
    tail_terms = "、".join(required_terms[1:]) if len(required_terms) > 1 else ""
    issue, action = infer_progress_context(task)

    if is_app_push:
        variants = [
            ("heuristic-natural", f"{time_term}直播课开讲，记得来听。"),
            ("heuristic-balanced", f"{time_term}有直播课，记得来听。"),
        ]
    elif is_social_decline:
        variants = [
            (
                "heuristic-natural",
                f"{time_term}我先不去啦，今天得赶项目，实在有点抽不开。你们先吃，下次我补上。"
                if "项目" in task
                else f"{time_term}我先不去啦，今天这边实在有点抽不开。你们先玩，下次我补上。"
            ),
            (
                "heuristic-balanced",
                f"{time_term}我可能去不了了，今天项目得赶一下。你们先好好吃，下次我再约。"
                if "项目" in task
                else f"{time_term}我可能去不了了，今天确实有点抽不开。你们先玩，下次我再约。"
            ),
        ]
    elif is_internal:
        if issue or action:
            variants = [
                (
                    "heuristic-natural",
                    f"老板，{issue or joined_terms or '这件事'}这块{action or '今天已经跟进'}，{time_term}我再同步进展。"
                ),
                (
                    "heuristic-balanced",
                    f"老板，{issue or joined_terms or '这件事'}目前还在处理，{action or '今天已经跟进'}，{time_term}给您同步结果。"
                ),
            ]
        else:
            variants = [
                (
                    "heuristic-natural",
                    f"老板，今天和{first_term}已经对过了，{time_term}我把{tail_terms or '进展'}整理好发您。"
                    if first_term
                    else f"老板，今天这件事我已经跟进过了，{time_term}我把更明确的进展发您。"
                ),
                (
                    "heuristic-balanced",
                    f"老板，今天和{first_term}这边已经沟通过了，{time_term}我会把{tail_terms or '进展'}整理后发您。"
                    if first_term
                    else f"老板，今天这件事我已经对过一轮了，{time_term}给您同步更明确的进展。"
                ),
            ]
    elif is_service:
        variants = [
            (
                "heuristic-natural",
                f"您好，您的问题已经登记，{time_term}会有{joined_terms.replace('已经登记、', '').replace('、已经登记', '') or '专员'}继续跟进处理，我这边也会同步您。"
            ),
            (
                "heuristic-balanced",
                f"您好，这边已经登记，{time_term}会安排{joined_terms.replace('已经登记、', '').replace('、已经登记', '') or '专员'}联系您处理，我也会持续跟进。"
            ),
        ]
    elif is_repair_request:
        issue_text = "空调不制冷" if "空调" in task and "不制冷" in task else (joined_terms or "这边")
        variants = [
            (
                "heuristic-natural",
                f"您好，家里{issue_text}，想问下{time_term}方便安排师傅过来维修吗？"
            ),
            (
                "heuristic-balanced",
                f"您好，{issue_text}，想约{time_term}维修，您看这个时间方便安排吗？"
            ),
        ]
    elif is_peer_sync:
        variants = [
            (
                "heuristic-natural",
                f"接口字段今天有调整，麻烦{deadline}同步到测试环境，我这边后续按这个版本对。"
            ),
            (
                "heuristic-balanced",
                f"接口字段刚调整过，麻烦{deadline}帮忙同步到测试环境，我这边后面按新版本测。"
            ),
        ]
    elif "客户" in task:
        variants = [
            (
                "heuristic-natural",
                f"您好，这边和{joined_terms or '这件事'}相关的内容还在跟进，预计{time_term}给您更明确的回复。"
            ),
            (
                "heuristic-balanced",
                f"您好，关于{joined_terms or '这件事'}我这边还在确认，{deadline}给您同步更明确的进展。"
            ),
        ]
    else:
        variants = [
            (
                "heuristic-natural",
                f"您好，{joined_terms or '这件事'}我这边还在跟进，预计{time_term}给您明确回复。"
            ),
            (
                "heuristic-balanced",
                f"您好，目前{joined_terms or '这件事'}还在确认中，{deadline}给您同步更明确的进展。"
            ),
        ]

    filtered: list[tuple[str, str]] = []
    for profile, text in variants:
        candidate = text.strip()
        if max_chars is not None and len(candidate.replace(" ", "").replace("\n", "")) > int(max_chars):
            continue
        if must_include and any(item not in candidate for item in must_include):
            continue
        filtered.append((profile, candidate))
    return filtered


def build_generate_heuristics(
    task: str,
    hard_constraints: dict[str, Any],
    baseline_text: str,
) -> list[tuple[str, str]]:
    must_include = [str(item).strip() for item in hard_constraints.get("must_include") or [] if str(item).strip()]
    max_chars = hard_constraints.get("max_chars")
    is_email = "邮件" in task or "email" in task.lower()
    if not is_email:
        short_variants = build_short_reply_heuristics(task, "", hard_constraints)
        baseline_compact = baseline_text.strip()
        return [
            (profile, candidate)
            for profile, candidate in short_variants
            if candidate.strip() != baseline_compact
        ]

    time_term = infer_time_term(must_include, task)
    deadline = deadline_term(time_term)
    other_terms = [item for item in must_include if item != time_term]
    primary_term = other_terms[0] if other_terms else ""
    joined_terms = "、".join(other_terms)
    variants: list[tuple[str, str]]

    if any(marker in task for marker in ("合作方", "合同附件", "催资料", "项目排期")):
        target = primary_term or "合同编号"
        variants = [
            (
                "heuristic-natural",
                f"您好，{target}对应的附件这边还没收到，项目排期会受影响。麻烦您{deadline}补一下，谢谢。",
            ),
            (
                "heuristic-balanced",
                f"您好，{target}相关附件目前还没收到，可能影响项目排期。麻烦{deadline}补充给我们，谢谢。",
            ),
        ]
    elif "客户" in task:
        variants = [
            (
                "heuristic-natural",
                f"您好，关于{joined_terms or '这件事'}我这边还在确认，预计{time_term}给您一个更明确的回复。",
            ),
            (
                "heuristic-balanced",
                f"您好，{joined_terms or '相关事项'}目前还在推进中，预计{deadline}给您同步更明确的进展。",
            ),
        ]
    else:
        variants = [
            (
                "heuristic-natural",
                f"您好，关于{joined_terms or '这件事'}我这边还在跟进，预计{deadline}给您同步明确进展。",
            ),
            (
                "heuristic-balanced",
                f"您好，{joined_terms or '相关事项'}目前还在确认，预计{deadline}给您回复，谢谢。",
            ),
        ]

    filtered: list[tuple[str, str]] = []
    baseline_compact = baseline_text.strip()
    for profile, text in variants:
        candidate = text.strip()
        if candidate == baseline_compact:
            continue
        if max_chars is not None and len(candidate.replace(" ", "").replace("\n", "")) > int(max_chars):
            continue
        if must_include and any(item not in candidate for item in must_include):
            continue
        filtered.append((profile, candidate))
    return filtered


def build_rewrite_heuristics(
    task: str,
    source_text: str,
    current_best_text: str,
    hard_constraints: dict[str, Any],
    failure_tags: list[str],
    *,
    revision_mode: str,
) -> list[tuple[str, str]]:
    repair = revision_mode == "repair"
    if looks_like_longform_rewrite(task, source_text):
        if any(marker in task for marker in COPY_TASK_MARKERS):
            heuristics = build_longform_rewrite_heuristics(
                task,
                source_text,
                current_best_text,
                failure_tags,
                repair=repair,
            )
            if heuristics:
                return heuristics
    heuristics = build_email_rewrite_heuristics(
        task,
        source_text,
        current_best_text,
        failure_tags,
        repair=repair,
    )
    if heuristics:
        return heuristics
    heuristics = build_longform_rewrite_heuristics(
        task,
        source_text,
        current_best_text,
        failure_tags,
        repair=repair,
    )
    if heuristics:
        return heuristics
    base_text = current_best_text if repair else source_text
    return build_short_reply_heuristics(task, base_text, hard_constraints)


def cleanup_common_phrase_collisions(text: str) -> str:
    cleaned = text
    replacements = [
        (
            r"感谢您的耐心，如后续[^。！？]*如有需要，欢迎随时联系我。?",
            "感谢您的耐心，如有其他问题，欢迎随时联系我。",
        ),
        (
            r"感谢您的耐心，如后续[^。！？]*欢迎随时联系我。?",
            "感谢您的耐心，如有其他问题，欢迎随时联系我。",
        ),
        (
            r"如后续[^。！？]*如有需要，欢迎随时联系我。?",
            "如有其他问题，欢迎随时联系我。",
        ),
        (
            r"如后续[^。！？]*欢迎随时联系我。?",
            "如有其他问题，欢迎随时联系我。",
        ),
        (
            r"如后续[^。！？]*如有需要，欢迎随时联系。?",
            "如有其他问题，欢迎随时联系。",
        ),
    ]
    for pattern, repl in replacements:
        cleaned = re.sub(pattern, repl, cleaned)
    return cleaned


def should_force_model_retry(
    *,
    session_mode: str,
    round_number: int,
    max_rounds: int,
    round_candidates: list[dict[str, Any]],
    heuristic_variants: list[tuple[str, str]],
) -> bool:
    if session_mode != "rewrite":
        return False
    if round_number >= max_rounds:
        return False
    if not heuristic_variants or not round_candidates:
        return False
    all_hard_fail = all(bool((candidate.get("score") or {}).get("hard_fail")) for candidate in round_candidates)
    if not all_hard_fail:
        return False
    seen_tags = {
        tag
        for candidate in round_candidates
        for tag in (candidate.get("failure_tags") or [])
    }
    return bool(seen_tags.intersection(QUALITY_GATE_RETRY_TAGS))


def apply_best_so_far_guardrails(
    *,
    candidate_text: str,
    current_best_text: str,
    source_text: str,
    score_payload: dict[str, Any],
    failure_tags_out: list[str],
    enforce_continuity: bool,
) -> tuple[dict[str, Any], list[str]]:
    if not enforce_continuity:
        score_payload = dict(score_payload)
        score_payload["notes"] = list(score_payload.get("notes") or [])
        unique_tags: list[str] = []
        seen_tags: set[str] = set()
        for tag in failure_tags_out:
            if tag not in seen_tags:
                seen_tags.add(tag)
                unique_tags.append(tag)
        return score_payload, unique_tags

    notes = list(score_payload.get("notes") or [])
    penalty = 0.0

    current_best_templates = _template_phrase_hits(current_best_text)
    candidate_templates = _template_phrase_hits(candidate_text)
    if len(candidate_templates) > len(current_best_templates):
        notes.append("candidate reintroduces more template phrases than current best")
        failure_tags_out.append("regressed_from_best")
        penalty += 0.08

    source_to_best = _similarity_ratio(source_text, current_best_text)
    source_to_candidate = _similarity_ratio(source_text, candidate_text)
    if source_to_candidate >= source_to_best + 0.035 and source_to_candidate >= 0.78:
        notes.append(
            f"candidate drifts back toward source wording (ratio={source_to_candidate:.3f} > best={source_to_best:.3f})",
        )
        failure_tags_out.append("regressed_from_best")
        penalty += 0.1

    current_best_len = _compact_len(current_best_text)
    candidate_len = _compact_len(candidate_text)
    if current_best_len >= 50 and candidate_len < current_best_len * 0.72:
        notes.append(f"candidate compresses current best too aggressively (ratio={candidate_len / max(current_best_len, 1):.3f})")
        if "overcompressed" not in failure_tags_out:
            failure_tags_out.append("overcompressed")
        failure_tags_out.append("regressed_from_best")
        penalty += 0.1

    best_paragraphs = [part.strip() for part in current_best_text.split("\n\n") if part.strip()]
    candidate_paragraphs = [part.strip() for part in candidate_text.split("\n\n") if part.strip()]
    if len(best_paragraphs) >= 3 and len(candidate_paragraphs) + 1 < len(best_paragraphs):
        notes.append("candidate collapses paragraph structure compared with current best")
        failure_tags_out.append("regressed_from_best")
        penalty += 0.06

    unique_tags: list[str] = []
    seen_tags: set[str] = set()
    for tag in failure_tags_out:
        if tag not in seen_tags:
            seen_tags.add(tag)
            unique_tags.append(tag)

    if penalty > 0:
        score_payload = dict(score_payload)
        score_payload["notes"] = notes
        score_payload["final_score"] = round(max(0.0, float(score_payload.get("final_score") or 0.0) - penalty), 6)
    else:
        score_payload = dict(score_payload)
        score_payload["notes"] = notes
    return score_payload, unique_tags


def build_heuristic_candidate(
    *,
    run_dir: Path,
    spec: dict[str, Any],
    task: str,
    source_text: str,
    current_best_text: str,
    current_best_score_value: float,
    enforce_continuity: bool,
    round_number: int,
    candidate_index: int,
    profile: str,
    candidate_text: str,
) -> dict[str, Any]:
    stem = f"round-{round_number:03d}-candidate-{candidate_index:02d}"
    candidate_path = run_dir / "rounds" / f"{stem}.txt"
    score_path = run_dir / "rounds" / f"{stem}.score.json"
    cleaned_text = cleanup_common_phrase_collisions(candidate_text.strip())
    write_text(candidate_path, cleaned_text)
    score_obj = score_candidate(spec, cleaned_text, source_text)
    score_payload = score_obj.as_dict()
    failure_tags_out = extract_failure_tags(
        task,
        cleaned_text,
        score_payload,
        baseline_score=current_best_score_value,
    )
    if cleaned_text.strip() == current_best_text.strip() and "copied_baseline" not in failure_tags_out:
        failure_tags_out.append("copied_baseline")
    score_payload, failure_tags_out = apply_best_so_far_guardrails(
        candidate_text=cleaned_text,
        current_best_text=current_best_text,
        source_text=source_text,
        score_payload=score_payload,
        failure_tags_out=failure_tags_out,
        enforce_continuity=enforce_continuity,
    )
    write_json(
        score_path,
        {
            "candidate_path": str(candidate_path.resolve()),
            "generation_path": "",
            "heuristic": True,
            **score_payload,
        },
    )
    return {
        "candidate_index": candidate_index,
        "profile": profile,
        "text": cleaned_text,
        "candidate_path": str(candidate_path.resolve()),
        "generation_path": "",
        "score_path": str(score_path.resolve()),
        "score": score_payload,
        "failure_tags": failure_tags_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a run from a raw brief and execute a visible multi-candidate humanize loop.",
    )
    parser.add_argument("--text", default=None)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--baseline-text", default=None)
    parser.add_argument("--challenger-text", default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=Path("./runs"))
    args = parser.parse_args()

    if args.text is None and args.input is None:
        raise ValueError("Provide --text or --input")

    raw_text = args.text if args.text is not None else args.input.read_text(encoding="utf-8")
    payload = build_payload(raw_text)
    spec = payload["spec"]
    task = str(payload["parsed"]["task"] or "run")
    run_dir = args.run_dir.resolve() if args.run_dir else create_run_dir(args.output_root.resolve(), task)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "rounds").mkdir(exist_ok=True)

    spec_path = run_dir / "spec.yaml"
    source_path = run_dir / "source.txt"
    parse_path = run_dir / "parse-result.json"
    brief_path = run_dir / "user-brief.txt"

    write_text(
        spec_path,
        yaml.safe_dump(
            spec,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
    )
    write_text(source_path, payload["parsed"]["original"])
    write_json(parse_path, payload)
    write_text(brief_path, raw_text.strip() + "\n")

    session_mode = payload["session_mode"]
    generation_backend_error = ""
    try:
        generation_backend = discover_generation_backend()
    except Exception as exc:
        generation_backend_error = str(exc)
        generation_backend = {
            "kind": "heuristic-only",
            "base_url": "heuristic-only://unavailable",
            "model": "heuristic-only",
        }
    base_url = generation_backend["base_url"]
    model = generation_backend["model"]
    generation_available = generation_backend.get("kind") != "heuristic-only"

    state = load_state()
    state_before = snapshot_state(state)
    state_before_path = run_dir / "strategy-state.before.json"
    state_after_path = run_dir / "strategy-state.after.json"
    write_json(state_before_path, state_before)

    original = payload["parsed"]["original"]
    hard_constraints = payload["parsed"]["hard_constraints"]
    margin = float(((spec.get("evaluator") or {}).get("minimum_improvement")) or 0.015)

    baseline_override = (args.baseline_text or "").strip()
    challenger_override = (args.challenger_text or "").strip()
    last_success_profile = str(state.get("last_success_profile") or "steady") or "steady"
    baseline_profile = last_success_profile if last_success_profile != "repair" else "steady"
    initial_directives = state_directives(state, [])
    (baseline_system, baseline_user), _ = build_generation_prompts(
        task=task,
        hard_constraints=hard_constraints,
        original=original,
        mode=session_mode,
        baseline_profile=baseline_profile,
        strategy_directives=initial_directives,
    )

    if baseline_override:
        baseline_text = baseline_override
    elif session_mode == "rewrite":
        baseline_text = original
    elif looks_like_short_message_task(task, hard_constraints):
        baseline_text = build_fallback_baseline(task, hard_constraints)
        write_json(
            run_dir / "baseline.generation.json",
            {
                "heuristic": True,
                "reason": "short message baseline",
                "baseline_text": baseline_text,
            },
        )
    elif not generation_available:
        baseline_text = build_fallback_baseline(task, hard_constraints)
        write_json(
            run_dir / "baseline.generation.json",
            {
                "fallback": True,
                "reason": f"generation backend unavailable: {generation_backend_error}",
                "baseline_text": baseline_text,
            },
        )
    else:
        try:
            baseline_text, baseline_response = call_and_extract_candidate(
                base_url=base_url,
                model=model,
                system_prompt=baseline_system,
                user_prompt=baseline_user,
                temperature=0.25,
                max_tokens_sequence=[220, 320, 420],
                hard_constraints=hard_constraints,
                require_hard_constraints=True,
                min_chars_hint=40 if ("邮件" in task or "email" in task.lower()) else (18 if "客户" in task else 14),
            )
            write_json(run_dir / "baseline.generation.json", baseline_response)
        except Exception as exc:
            baseline_text = build_fallback_baseline(task, hard_constraints)
            write_json(
                run_dir / "baseline.generation.json",
                {
                    "fallback": True,
                    "reason": str(exc),
                    "baseline_text": baseline_text,
                },
            )

    initial_baseline_text = baseline_text
    source_text = original or initial_baseline_text
    baseline_score_obj = score_candidate(spec, baseline_text, source_text)
    baseline_score_payload = baseline_score_obj.as_dict()
    write_json(
        run_dir / "baseline.initial.score.json",
        {
            "candidate_path": str((run_dir / "baseline.txt").resolve()),
            "source_text": source_text,
            **baseline_score_payload,
        },
    )

    current_best_text = baseline_text
    current_best_score = baseline_score_obj
    current_failure_tags = extract_failure_tags(task, baseline_text, baseline_score_payload)
    session_trace: list[dict[str, Any]] = []
    selected_candidates: list[dict[str, Any]] = []
    improved_any = False
    run_budget = derive_run_budget(task, session_mode)
    max_rounds = int(run_budget["max_rounds"])
    challenger_count = int(run_budget["challenger_count"])

    final_kept_candidate: dict[str, Any] | None = None

    if challenger_override:
        override_score = score_candidate(spec, challenger_override, source_text)
        override_record = {
            "candidate_index": 1,
            "profile": "manual-override",
            "text": challenger_override,
            "candidate_path": "",
            "generation_path": "",
            "score_path": "",
            "score": override_score.as_dict(),
            "failure_tags": extract_failure_tags(
                task,
                challenger_override,
                override_score.as_dict(),
                baseline_score=current_best_score.final_score,
            ),
        }
        selected_candidates.append(override_record)
        delta = override_score.final_score - current_best_score.final_score
        improved = (not override_score.hard_fail) and delta >= margin
        session_trace.append(
            {
                "round": 1,
                "profiles": ["manual-override"],
                "failure_tags_in": [],
                "strategy_directives": initial_directives,
                "baseline_text": current_best_text,
                "baseline_score": current_best_score.as_dict(),
                "candidates": [override_record],
                "selected_candidate": override_record,
                "delta": round(delta, 6),
                "decision": "keep" if improved else "discard",
                "reason": "manual challenger override" if improved else "manual challenger override did not improve enough",
                "revision_mode": "rewrite",
                "base_text_kind": "source",
            },
        )
        if improved:
            improved_any = True
            current_best_text = challenger_override
            current_best_score = override_score
            final_kept_candidate = override_record
    else:
        for round_index in range(max_rounds):
            round_number = round_index + 1
            profiles = choose_profiles(state, current_failure_tags, round_index)[:challenger_count]
            directives = state_directives(state, current_failure_tags)
            round_candidates: list[dict[str, Any]] = []
            revision_mode = "repair" if session_mode == "rewrite" and improved_any else "rewrite"
            base_text_kind = "best_so_far" if revision_mode == "repair" else "source"
            enforce_continuity = revision_mode == "repair"
            if session_mode == "generate":
                heuristic_variants = build_generate_heuristics(task, hard_constraints, current_best_text)
            else:
                heuristic_variants = build_rewrite_heuristics(
                    task,
                    source_text,
                    current_best_text,
                    hard_constraints,
                    current_failure_tags,
                    revision_mode=revision_mode,
                )
            max_chars = int(hard_constraints.get("max_chars") or 0)
            is_short_chat = looks_like_short_message_task(task, hard_constraints) and max_chars and max_chars <= 120
            force_model_candidates = round_number > 1 and any(
                tag in current_failure_tags for tag in QUALITY_GATE_RETRY_TAGS
            )
            skip_model_candidates = (not force_model_candidates) and bool(heuristic_variants) and (
                session_mode == "generate"
                or looks_like_longform_rewrite(task, source_text)
                or is_short_chat
                or looks_like_professional_email(task, source_text)
            )

            if not generation_available:
                write_json(
                    run_dir / f"round-{round_number:03d}-generation-backend.json",
                    {
                        "kind": "heuristic-only",
                        "reason": generation_backend_error,
                        "message": "No generation backend was available; using heuristic candidates only.",
                    },
                )
            elif not skip_model_candidates:
                for candidate_index, profile in enumerate(profiles, start=1):
                    try:
                        candidate = generate_candidate(
                            run_dir=run_dir,
                            spec=spec,
                            session_mode=session_mode,
                            task=task,
                            hard_constraints=hard_constraints,
                            source_text=source_text,
                            current_best_text=current_best_text,
                            base_url=base_url,
                            model=model,
                            profile=profile,
                            strategy_directives=directives,
                            failure_tags=current_failure_tags,
                            revision_mode=revision_mode,
                            round_number=round_number,
                            candidate_index=candidate_index,
                            current_best_score_value=current_best_score.final_score,
                            enforce_continuity=enforce_continuity,
                        )
                    except Exception as exc:
                        candidate = {
                            "candidate_index": candidate_index,
                            "profile": profile,
                            "text": "",
                            "candidate_path": "",
                            "generation_path": "",
                            "score_path": "",
                            "score": {
                                "final_score": 0.0,
                                "model_score": 0.0,
                                "rule_score": 0.0,
                                "hard_fail": True,
                                "char_count": 0,
                                "query": "",
                                "rule_breakdown": {
                                    "length": 0.0,
                                    "must_include": 0.0,
                                    "banned_phrases": 0.0,
                                    "template_tone": 0.0,
                                    "formatting": 0.0,
                                    "detailfulness": 0.0,
                                    "email_shape": 0.0,
                                    "anti_repetition": 0.0,
                                },
                                "notes": [f"generation error: {exc}"],
                            },
                            "failure_tags": ["generation_error"],
                            "error": str(exc),
                        }
                    round_candidates.append(candidate)

            for heuristic_offset, (profile, candidate_text) in enumerate(heuristic_variants, start=len(round_candidates) + 1):
                round_candidates.append(
                    build_heuristic_candidate(
                        run_dir=run_dir,
                        spec=spec,
                        task=task,
                        source_text=source_text,
                        current_best_text=current_best_text,
                        current_best_score_value=current_best_score.final_score,
                        enforce_continuity=enforce_continuity,
                        round_number=round_number,
                        candidate_index=heuristic_offset,
                        profile=profile,
                        candidate_text=candidate_text,
                    ),
                )

            selected = pick_best_candidate(round_candidates)
            selected_candidates.append(selected)
            selected_score = selected["score"]
            delta = float(selected_score["final_score"]) - float(current_best_score.final_score)
            improved = (not selected_score["hard_fail"]) and delta >= margin
            continue_requested, quality_gate_tags_out = should_continue_refinement(
                selected=selected,
                delta=delta,
                margin=margin,
                round_number=round_number,
                max_rounds=max_rounds,
            )
            quality_gate_exhausted = bool(quality_gate_tags_out) and not continue_requested
            if improved and continue_requested:
                decision = "continue"
                reason = "selected challenger improved, but quality gate requested another round"
                next_failure_tags = quality_gate_tags_out
                next_step = "continue"
            elif improved and quality_gate_exhausted:
                decision = "keep"
                reason = "selected challenger improved, but round budget was exhausted"
                next_failure_tags = quality_gate_tags_out
                next_step = "stop"
            elif improved:
                decision = "keep"
                reason = "selected challenger improved and passed quality gate"
                next_failure_tags = []
                next_step = "stop"
            else:
                decision = "discard"
                reason = "selected challenger did not improve enough"
                next_failure_tags = aggregate_failure_tags(round_candidates, selected)
                next_step = "stop"
            force_retry_with_model = should_force_model_retry(
                session_mode=session_mode,
                round_number=round_number,
                max_rounds=max_rounds,
                round_candidates=round_candidates,
                heuristic_variants=heuristic_variants,
            )

            round_payload = {
                "round": round_number,
                "profiles": profiles,
                "failure_tags_in": current_failure_tags,
                "strategy_directives": directives,
                "force_model_candidates": force_model_candidates,
                "force_retry_with_model": force_retry_with_model,
                "baseline_text": current_best_text,
                "baseline_score": current_best_score.as_dict(),
                "candidates": round_candidates,
                "selected_candidate": selected,
                "delta": round(delta, 6),
                "decision": decision,
                "reason": reason,
                "quality_gate_tags": quality_gate_tags_out,
                "next_step": next_step,
                "revision_mode": revision_mode,
                "base_text_kind": base_text_kind,
            }
            session_trace.append(round_payload)

            state = evolve_after_attempts(
                state,
                task=task,
                chosen_profile=selected["profile"],
                failure_tags=next_failure_tags,
                improved=improved and not quality_gate_tags_out,
                baseline_text=current_best_text,
                challenger_text=selected["text"],
                delta=delta,
            )

            if improved:
                improved_any = True
                current_best_text = selected["text"]
                current_best_score = score_candidate(spec, current_best_text, source_text)
                final_kept_candidate = selected
                if continue_requested:
                    current_failure_tags = next_failure_tags
                    continue
                current_failure_tags = []
                break

            current_failure_tags = next_failure_tags
            if improved_any or round_number >= max_rounds:
                break
            if force_retry_with_model:
                continue
            if heuristic_variants and (not next_failure_tags or set(next_failure_tags).issubset({"no_improvement"})):
                break

    save_state(state)
    write_json(state_after_path, state)

    if improved_any and final_kept_candidate:
        final_candidate = final_kept_candidate
    elif selected_candidates:
        final_candidate = max(selected_candidates, key=candidate_rank_key)
    else:
        final_candidate = {
            "candidate_index": 0,
            "profile": "baseline",
            "text": initial_baseline_text,
            "candidate_path": "",
            "generation_path": "",
            "score_path": "",
            "score": baseline_score_payload,
            "failure_tags": [],
        }

    final_compare_text = current_best_text if improved_any else final_candidate["text"]
    if not final_compare_text.strip():
        final_compare_text = initial_baseline_text

    final_challenger_score = (
        current_best_score.as_dict()
        if improved_any
        else dict(final_candidate.get("score") or baseline_score_payload)
    )
    baseline_final_score = baseline_score_payload

    baseline_path = run_dir / "baseline.txt"
    challenger_path = run_dir / "challenger.txt"
    baseline_score_path = run_dir / "baseline.score.json"
    challenger_score_path = run_dir / "challenger.score.json"
    compare_path = run_dir / "compare-result.json"
    best_path = run_dir / "best.txt"
    report_md_path = run_dir / "report.md"
    report_html_path = run_dir / "report.html"

    write_text(baseline_path, initial_baseline_text)
    write_text(challenger_path, final_compare_text)
    dump_score_json(
        baseline_score_path,
        {
            "candidate_path": str(baseline_path.resolve()),
            "source_path": str(source_path.resolve()),
            "spec_path": str(spec_path.resolve()),
            **baseline_final_score,
        },
    )
    dump_score_json(
        challenger_score_path,
        {
            "candidate_path": str(challenger_path.resolve()),
            "source_path": str(source_path.resolve()),
            "spec_path": str(spec_path.resolve()),
            **final_challenger_score,
        },
    )
    compare_payload = compare_payload_local(
        spec_path=spec_path,
        source_path=source_path,
        baseline_path=baseline_path,
        challenger_path=challenger_path,
        baseline_score=baseline_final_score,
        challenger_score=final_challenger_score,
        margin=margin,
    )
    dump_score_json(compare_path, compare_payload)
    write_round_log_local(run_dir, compare_payload)
    write_text(
        best_path,
        final_compare_text if compare_payload["winner"] == "challenger" else initial_baseline_text,
    )
    report_md_text = build_markdown(compare_payload, initial_baseline_text, final_compare_text)
    write_text(report_md_path, report_md_text)
    write_text(report_html_path, build_html(compare_payload, initial_baseline_text, final_compare_text))

    session_trace_payload = {
        "session_mode": session_mode,
        "input_mode": payload["input_mode"],
        "run_budget": run_budget,
        "improved_any": improved_any,
        "state_before_path": str(state_before_path.resolve()),
        "state_after_path": str(state_after_path.resolve()),
        "rounds": session_trace,
    }
    session_trace_path = run_dir / "session-trace.json"
    session_trace_md_path = run_dir / "session-trace.md"
    write_json(session_trace_path, session_trace_payload)
    session_trace_markdown = build_trace_markdown(session_trace_payload)
    write_text(session_trace_md_path, session_trace_markdown)

    user_visible_md_path = run_dir / "user-visible.md"
    user_visible_html_path = run_dir / "user-visible.html"
    user_visible_summary = build_user_visible_summary(
        task=task,
        compare_payload=compare_payload,
        baseline_text=initial_baseline_text,
        challenger_text=final_compare_text,
        session_trace=session_trace,
        report_html_path=str(report_html_path.resolve()),
        trace_path=str(session_trace_path.resolve()),
    )
    write_text(user_visible_md_path, user_visible_summary)
    write_text(user_visible_html_path, build_user_visible_html(user_visible_summary))

    payload_out = {
        "run_dir": str(run_dir.resolve()),
        "decision": compare_payload["decision"],
        "winner": compare_payload["winner"],
        "delta": compare_payload["delta"],
        "baseline_score": compare_payload["baseline"]["final_score"],
        "challenger_score": compare_payload["challenger"]["final_score"],
        "best_path": str(best_path.resolve()),
        "compare_result": str(compare_path.resolve()),
        "markdown_report": str(report_md_path.resolve()),
        "html_report": str(report_html_path.resolve()),
        "session_mode": session_mode,
        "input_mode": payload["input_mode"],
        "parse_result": str(parse_path.resolve()),
        "user_brief": str(brief_path.resolve()),
        "llm_base_url": base_url,
        "llm_model": model,
        "llm_backend_error": generation_backend_error,
        "baseline_text": initial_baseline_text,
        "challenger_text": final_compare_text,
        "baseline_generation": str((run_dir / "baseline.generation.json").resolve()) if (run_dir / "baseline.generation.json").exists() else "",
        "challenger_generation": final_candidate.get("generation_path", ""),
        "score_summary": {
        "decision": compare_payload["decision"],
        "reason": compare_payload["reason"],
        "delta": compare_payload["delta"],
        "margin": compare_payload["margin"],
        "baseline": {
            "final_score": compare_payload["baseline"]["final_score"],
            "model_score": compare_payload["baseline"]["model_score"],
            "rule_score": compare_payload["baseline"]["rule_score"],
            "hard_fail": compare_payload["baseline"]["hard_fail"],
            "notes": compare_payload["baseline"]["notes"],
        },
        "challenger": {
            "final_score": compare_payload["challenger"]["final_score"],
            "model_score": compare_payload["challenger"]["model_score"],
            "rule_score": compare_payload["challenger"]["rule_score"],
            "hard_fail": compare_payload["challenger"]["hard_fail"],
            "notes": compare_payload["challenger"]["notes"],
        },
        },
        "report_markdown_text": report_md_text,
        "session_trace": session_trace,
        "session_trace_path": str(session_trace_path.resolve()),
        "session_trace_markdown": session_trace_markdown,
        "user_visible_summary_markdown": user_visible_summary,
        "user_visible_markdown_path": str(user_visible_md_path.resolve()),
        "user_visible_html_path": str(user_visible_html_path.resolve()),
        "strategy_state_before": str(state_before_path.resolve()),
        "strategy_state_after": str(state_after_path.resolve()),
    }
    result_json_path = run_dir / "result.json"
    write_json(result_json_path, payload_out)
    print("=== HUMANIZE_FINAL_RESPONSE_BEGIN ===")
    print(user_visible_summary)
    print("=== HUMANIZE_FINAL_RESPONSE_END ===")


if __name__ == "__main__":
    main()
