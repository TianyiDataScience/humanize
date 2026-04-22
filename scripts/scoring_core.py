from __future__ import annotations

from difflib import SequenceMatcher
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
import yaml
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from runtime_common import DEFAULT_GOAL, DEFAULT_MODEL_ID, model_dir

DEFAULT_TEMPLATE_PHRASES = [
    "在当下这个充满不确定性却又蕴含巨大机会的时代",
    "底层逻辑",
    "认知升级",
    "行动迭代",
    "成长闭环",
    "系统化交流",
    "行为触发机制",
    "长期复用",
    "生活系统",
    "快节奏的时代",
    "不断追逐目标",
    "生活本身真正值得被感知的细节",
    "真正的松弛感",
    "重新找回与自己相处的能力",
    "低质量勤奋",
    "单点突破",
    "更大的确定性",
    "数字化转型",
    "数字化浪潮",
    "企业经营全流程",
    "复杂业务场景",
    "协同提效需求",
    "多模块能力整合",
    "自动化流程编排",
    "数据驱动的决策支持",
    "组织协同体系",
    "释放增长潜能",
    "快节奏生活方式不断升级",
    "精致女性",
    "高效修护体验",
    "多重植萃成分协同作用",
    "深入肌肤底层",
    "多维度肌肤问题",
    "焕发自然光彩",
    "高度自驱力",
    "优秀沟通协作能力",
    "复杂问题拆解能力",
    "深度参与",
    "业务需求洞察",
    "产品方案设计",
    "跨部门资源协调",
    "项目落地推进",
    "推动业务目标实现",
    "高品质亲肤面料",
    "人体工学剪裁设计",
    "兼具舒适性、透气性与时尚表现力",
    "更加自在、轻盈且高级",
    "穿着体验",
    "系统化理解短视频内容增长方法论",
    "高价值直播分享",
    "账号定位",
    "流量承接",
    "转化路径",
    "深度拆解",
    "可持续增长的内容运营体系",
    "系统化表达训练课程",
    "从底层逻辑出发",
    "全面提升结构化表达",
    "临场反应和影响力输出能力",
    "个人竞争力跃迁",
    "退款诉求",
    "规定时效内",
    "提交相关部门进行核实处理",
    "业务增长的关键阶段",
    "一站式智能营销解决方案",
    "多维度提升",
    "线索获取效率",
    "客户触达质量",
    "转化链路表现",
    "复杂多变的市场环境",
    "稳健和可持续的增长体系",
    "相关能力与合作价值",
    "亲爱的各位伙伴",
    "进一步提升社群整体互动质量",
    "内容传播效率",
    "正式开展",
    "高质量学习型社群",
    "未能达到您的预期",
    "高度重视",
    "第一时间高度重视",
    "同步相关部门",
    "综合核实",
    "有序推进",
    "完整处理结果",
    "全面排查与优化",
    "持续完善服务流程",
    "持续为您提供优质服务",
    "更加优质、高效、贴心",
    "宝贵反馈",
    "日前为我提供本次面试机会",
    "更加全面且深入",
    "进一步增强",
    "更加确信",
    "具有较高的匹配度",
    "期待您的进一步通知",
    "第一时间积极配合",
    "感谢您的理解与支持",
    "如有任何问题请随时联系",
    "给您带来的不便，敬请谅解",
    "祝您生活愉快",
    "希望以上信息对您有所帮助",
    "我们将竭诚为您服务",
    "感谢您的耐心等待",
    "请您耐心等待",
    "期待与您合作",
]

FORMAT_PENALTIES = [
    (re.compile(r"(?m)^\s*[-*]\s+"), 0.2, "contains bullet-list formatting"),
    (re.compile(r"(?m)^\s*\d+\.\s+"), 0.2, "contains numbered-list formatting"),
    (re.compile(r"[!！]{2,}"), 0.08, "contains repeated exclamation"),
    (re.compile(r"[?？]{2,}"), 0.06, "contains repeated question marks"),
    (re.compile(r"\n{3,}"), 0.1, "contains too many blank lines"),
]

_MODEL_BUNDLE: dict[str, Any] = {}


@dataclass
class CandidateScore:
    final_score: float
    model_score: float
    rule_score: float
    hard_fail: bool
    char_count: int
    query: str
    rule_breakdown: dict[str, float]
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "final_score": round(self.final_score, 6),
            "model_score": round(self.model_score, 6),
            "rule_score": round(self.rule_score, 6),
            "hard_fail": self.hard_fail,
            "char_count": self.char_count,
            "query": self.query,
            "rule_breakdown": {
                key: round(value, 6)
                for key, value in self.rule_breakdown.items()
            },
            "notes": self.notes,
        }


def load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Spec must be a YAML object: {path}")
    return payload


def read_text(path: Path | None) -> str:
    if not path:
        return ""
    return path.read_text(encoding="utf-8").strip()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def compact_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def build_query(spec: dict[str, Any], source_text: str) -> str:
    task = str(spec.get("task", "")).strip()
    goal = clean_text(str(spec.get("goal", "") or "")) or DEFAULT_GOAL
    notes = spec.get("style_notes") or []
    hard_constraints = spec.get("hard_constraints") or {}
    must_include = hard_constraints.get("must_include") or []
    banned = hard_constraints.get("banned_phrases") or []

    parts = [
        "请判断这条候选中文沟通消息，是否更符合目标中的真人感和自然度要求。",
    ]
    if task:
        parts.append(f"沟通任务：{task}")
    if source_text:
        parts.append(f"原始信息：{clean_text(source_text)[:280]}")
    if goal:
        parts.append(f"优化目标：{goal}")
    parts.append("通用要求：像真人会发出的中文消息，避免模板腔、客服腔、公告腔和过度AI润色感。")
    if notes:
        parts.append("风格备注：" + "；".join(clean_text(str(x)) for x in notes))
    if must_include:
        parts.append("必须保留：" + "；".join(clean_text(str(x)) for x in must_include))
    if banned:
        parts.append("尽量避免：" + "；".join(clean_text(str(x)) for x in banned))
    return "\n".join(parts)


def default_device() -> torch.device:
    requested = os.environ.get("HUMANIZE_SCORER_DEVICE", "").strip().lower()
    if requested in {"cpu", "mps"}:
        if requested == "mps" and torch.backends.mps.is_available():
            return torch.device("mps")
        if requested == "cpu":
            return torch.device("cpu")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def load_model_bundle(model_path: Path | None = None) -> dict[str, Any]:
    key = str(model_path or model_dir())
    if key in _MODEL_BUNDLE:
        return _MODEL_BUNDLE[key]

    resolved = str(model_path or model_dir())
    if not Path(resolved).exists():
        resolved = DEFAULT_MODEL_ID

    tokenizer = AutoTokenizer.from_pretrained(resolved)
    model = AutoModelForSequenceClassification.from_pretrained(resolved)
    device = default_device()
    model.to(device)
    getattr(model, "eval")()
    bundle = {"tokenizer": tokenizer, "model": model, "device": device}
    _MODEL_BUNDLE[key] = bundle
    return bundle


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def model_score(query: str, candidate: str) -> float:
    bundle = load_model_bundle()
    tokenizer = bundle["tokenizer"]
    model = bundle["model"]
    device = bundle["device"]
    inputs = tokenizer(
        [query],
        [candidate],
        padding=True,
        truncation=True,
        max_length=1024,
        return_tensors="pt",
    )
    inputs = {key: value.to(device) for key, value in inputs.items()}
    with torch.no_grad():
        logits = model(**inputs).logits.view(-1).float().cpu().tolist()
    return sigmoid(logits[0])


def length_score(spec: dict[str, Any], char_count: int, notes: list[str]) -> float:
    hard_constraints = spec.get("hard_constraints") or {}
    min_chars = hard_constraints.get("min_chars")
    max_chars = hard_constraints.get("max_chars")
    if min_chars is None and max_chars is None:
        return 1.0

    score = 1.0
    if min_chars is not None and char_count < int(min_chars):
        gap = int(min_chars) - char_count
        score -= min(0.7, gap / max(int(min_chars), 1))
        notes.append(f"shorter than min_chars ({char_count} < {min_chars})")
    if max_chars is not None and char_count > int(max_chars):
        gap = char_count - int(max_chars)
        score -= min(0.85, gap / max(int(max_chars), 1))
        notes.append(f"longer than max_chars ({char_count} > {max_chars})")
    return max(0.0, score)


def must_include_score(spec: dict[str, Any], candidate: str, notes: list[str]) -> tuple[float, bool]:
    hard_constraints = spec.get("hard_constraints") or {}
    must_include = [clean_text(str(x)) for x in hard_constraints.get("must_include") or [] if str(x).strip()]
    if not must_include:
        return 1.0, False

    hits = 0
    missing: list[str] = []
    for item in must_include:
        if item in candidate:
            hits += 1
        else:
            missing.append(item)
    if missing:
        notes.append("missing must_include: " + " / ".join(missing))
    return hits / len(must_include), bool(missing)


def phrase_penalty_score(spec: dict[str, Any], candidate: str, notes: list[str]) -> tuple[float, float]:
    hard_constraints = spec.get("hard_constraints") or {}
    user_banned = [clean_text(str(x)) for x in hard_constraints.get("banned_phrases") or [] if str(x).strip()]
    banned_hits = [item for item in user_banned if item and item in candidate]
    template_hits = [item for item in DEFAULT_TEMPLATE_PHRASES if item in candidate]
    if banned_hits:
        notes.append("contains banned phrases: " + " / ".join(banned_hits))
    if template_hits:
        notes.append("contains template phrases: " + " / ".join(template_hits))
    banned_score = max(0.0, 1.0 - 0.32 * len(banned_hits))
    template_score = max(0.0, 1.0 - 0.14 * len(template_hits))
    return banned_score, template_score


def source_template_reduction_score(source_text: str, candidate: str, notes: list[str]) -> float:
    source_hits = [item for item in DEFAULT_TEMPLATE_PHRASES if item in source_text]
    if not source_hits:
        return 1.0
    carried = [item for item in source_hits if item in candidate]
    if carried:
        notes.append("retains source template phrases: " + " / ".join(carried))
    ratio = len(carried) / max(len(source_hits), 1)
    return max(0.0, 1.0 - ratio)


def formatting_score(candidate: str, notes: list[str]) -> float:
    score = 1.0
    for pattern, penalty, message in FORMAT_PENALTIES:
        if pattern.search(candidate):
            score -= penalty
            notes.append(message)
    return max(0.0, score)


def repeated_ngram_penalty(candidate: str, notes: list[str]) -> float:
    compact = re.sub(r"\s+", "", candidate)
    if len(compact) < 10:
        return 1.0
    seen: dict[str, int] = {}
    for idx in range(0, max(0, len(compact) - 3)):
        gram = compact[idx : idx + 4]
        seen[gram] = seen.get(gram, 0) + 1
    repeated = [gram for gram, count in seen.items() if count >= 3]
    if repeated:
        notes.append("repeated 4-gram fragments: " + " / ".join(repeated[:5]))
    penalty = min(0.25, 0.06 * len(repeated))
    return max(0.0, 1.0 - penalty)


def _normalize_similarity_text(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    return re.sub(r"[，。！？；：、,.!?:;\"'“”‘’（）()《》【】\\[\\]<>·—…-]", "", compact)


def rewrite_similarity_score(source_text: str, candidate: str, notes: list[str]) -> float:
    source_norm = _normalize_similarity_text(source_text)
    candidate_norm = _normalize_similarity_text(candidate)
    if len(source_norm) < 50 or len(candidate_norm) < 35:
        return 1.0

    ratio = SequenceMatcher(None, source_norm, candidate_norm).ratio()
    if ratio >= 0.9:
        notes.append(f"rewrite too similar to source (ratio={ratio:.3f})")
        return 0.0
    if ratio >= 0.84:
        notes.append(f"rewrite still very close to source (ratio={ratio:.3f})")
        return 0.35
    if ratio >= 0.78:
        notes.append(f"rewrite remains close to source (ratio={ratio:.3f})")
        return 0.68
    return 1.0


def sentence_splice_score(candidate: str, notes: list[str]) -> float:
    sentences = [part.strip() for part in re.split(r"[。！？\n]+", candidate) if part.strip()]
    condition_markers = ("如有需要", "如有任何问题", "如后续", "如果后续", "后续如有")
    contact_markers = ("欢迎随时联系", "欢迎联系", "随时联系我", "随时联系我们", "联系我", "联系我们")

    for sentence in sentences:
        compact = re.sub(r"\s+", "", sentence)
        if re.search(r"如[^，。！？]{0,24}，如", compact):
            notes.append("sentence splice issue: repeated lead-in connectors")
            return 0.0
        if re.search(r"稍后前(?=给|向|为|把|会|同步|回复)", compact):
            notes.append("sentence splice issue: invalid time connector")
            return 0.0
        has_condition = sum(1 for item in condition_markers if item in compact)
        has_contact = sum(1 for item in contact_markers if item in compact)
        if has_condition >= 2 or (has_condition >= 1 and has_contact >= 1 and "，" in sentence):
            notes.append("sentence splice issue: collided closing phrases")
            return 0.0
    return 1.0


def placeholder_output_score(candidate: str, notes: list[str]) -> float:
    compact = re.sub(r"\s+", "", candidate)
    if not compact:
        notes.append("candidate is empty")
        return 0.0
    if re.search(r"(……|\.{3,}|。。。)", compact):
        notes.append("contains placeholder-style ellipsis")
        return 0.0
    if compact in {"同上", "略", "待补充", "待确认"}:
        notes.append("contains placeholder-style content")
        return 0.0
    return 1.0


def rewrite_coverage_score(source_text: str, candidate: str, notes: list[str]) -> float:
    source_count = compact_char_count(source_text)
    candidate_count = compact_char_count(candidate)
    if source_count < 60:
        return 1.0
    ratio = candidate_count / max(source_count, 1)
    if ratio < 0.15:
        notes.append(f"rewrite drops too much source detail (ratio={ratio:.3f})")
        return 0.0
    if ratio < 0.24:
        notes.append(f"rewrite is over-compressed for the source length (ratio={ratio:.3f})")
        return 0.35
    if ratio < 0.32:
        notes.append(f"rewrite is quite compressed compared with the source (ratio={ratio:.3f})")
        return 0.72
    return 1.0


def detail_score(spec: dict[str, Any], char_count: int, candidate: str, notes: list[str]) -> float:
    task = str(spec.get("task") or "")
    compact = re.sub(r"\s+", "", candidate)
    has_progress_shape = any(
        token in candidate
        for token in ("正在", "已经", "这边", "目前", "预计", "会", "给您", "回复", "开始", "开讲", "记得", "直播课")
    )
    if char_count < 10:
        notes.append("too short for a believable communication reply")
        return 0.28
    if char_count < 14:
        notes.append("too short and vague for a complete reply")
        return 0.5
    if any(token in task for token in ("客户", "上级", "老板", "面试", "售后")) and char_count < 18:
        notes.append("light on concrete progress detail")
        return 0.72 if has_progress_shape else 0.62
    if not has_progress_shape and char_count < 22:
        notes.append("could use clearer action or time detail")
        return 0.82
    return 1.0


def email_shape_score(spec: dict[str, Any], char_count: int, candidate: str, notes: list[str]) -> float:
    task = str(spec.get("task") or "")
    lowered = task.lower()
    if "邮件" not in task and "email" not in lowered:
        return 1.0
    hard_constraints = spec.get("hard_constraints") or {}
    max_chars = hard_constraints.get("max_chars")
    is_tight_short_email = max_chars is not None and int(max_chars) <= 140

    score = 1.0
    has_greeting = any(token in candidate for token in ("您好", "尊敬", "Hi", "Hello", "Dear"))
    sentence_like = len([part for part in re.split(r"[。！？!?]\s*", candidate) if part.strip()])
    has_body_shape = "\n\n" in candidate or sentence_like >= 3

    min_full_email_chars = 32 if is_tight_short_email else 45
    if char_count < min_full_email_chars:
        notes.append("too short for an email reply")
        score -= 0.55
    elif char_count < 60 and not is_tight_short_email:
        notes.append("email reply is on the short side")
        score -= 0.12

    if not has_greeting:
        notes.append("missing email-style greeting")
        score -= 0.32
    business_context_tokens = ("同步", "回复", "说明", "确认", "进展", "感谢", "谢谢", "附件", "排期", "合同")
    has_business_context = any(token in candidate for token in business_context_tokens)
    if sentence_like < 2:
        if is_tight_short_email and has_greeting and has_business_context:
            # Explicit short emails often read as one compact sentence; do not force
            # a multi-paragraph email shape when the user asked for a tight limit.
            pass
        else:
            notes.append("reads like a chat reply, not a full email body")
            score -= 0.38
    if not has_body_shape and not is_tight_short_email:
        notes.append("email reply lacks clear body structure")
        score -= 0.14
    if not has_business_context:
        notes.append("email reply could use clearer business context")
        score -= 0.12
    return max(0.0, score)


def audience_fit_score(spec: dict[str, Any], candidate: str, notes: list[str]) -> float:
    task = str(spec.get("task") or "")
    score = 1.0

    if "客户" in task:
        if any(token in candidate for token in ("XX总", "x总", "老板", "总，您好", "总您好")):
            notes.append("wrong audience: reads like a manager-facing salutation")
            score -= 0.55
        if "XX" in candidate or "某某" in candidate:
            notes.append("contains placeholder-style salutation")
            score -= 0.25
        if any(token in task for token in ("售后", "投诉", "破损", "退款", "退换", "登记")):
            if "登记" in task and "登记" not in candidate:
                notes.append("support reply misses registration status")
                score -= 0.65
            if any(token in task for token in ("联系", "处理", "安排")) and not any(token in candidate for token in ("联系", "处理", "安排")):
                notes.append("support reply misses next handling action")
                score -= 0.45

    if any(token in task for token in ("老板", "上级", "经理")):
        if "客户" in candidate or "尊敬的客户" in candidate:
            notes.append("wrong audience: reads like a customer-facing reply")
            score -= 0.55
        if not any(token in candidate for token in ("我", "给您", "发您", "同步进展", "同步结果")):
            notes.append("manager update could use clearer ownership")
            score -= 0.45

    social_decline = (
        any(token in task for token in ("不要太冷淡", "别太冷淡", "婉拒", "聚餐", "约饭", "邀约"))
        or (
            "朋友" in task
            and "朋友圈" not in task
            and any(token in task for token in ("拒绝", "不去", "去不了", "今晚", "聚餐", "约饭", "邀约"))
        )
    )
    if social_decline:
        warm_tokens = ("啦", "实在", "你们先", "好好吃", "补上", "不好意思", "抱歉", "可能", "下次我")
        if not any(token in candidate for token in warm_tokens):
            notes.append("tone is a bit cold for a friendly decline")
            score -= 0.55

    return max(0.0, score)


def task_fact_score(spec: dict[str, Any], candidate: str, notes: list[str]) -> float:
    task = str(spec.get("task") or "")
    if not task:
        return 1.0

    checks: list[tuple[str, tuple[str, ...]]] = []
    for match in re.finditer(
        r"(周[一二三四五六日天](?:早上|上午|中午|下午|晚上)?|明天(?:早上|上午|中午|下午|晚上)?|明早|明晚|今天(?:早上|上午|中午|下午|晚上)?|今晚|本周内)",
        task,
    ):
        value = match.group(1)
        checks.append((value, (value,)))

    fact_groups = [
        ("空调", ("空调",)),
        ("不制冷", ("不制冷", "制冷")),
        ("维修", ("维修", "修")),
        ("破损", ("破损", "损坏", "坏")),
        ("退款", ("退款", "退")),
        ("退换", ("退换", "退货", "换货")),
        ("发票", ("发票",)),
        ("合同", ("合同",)),
        ("附件", ("附件",)),
        ("项目排期", ("项目排期", "排期")),
        ("报价", ("报价", "价格")),
        ("面试", ("面试",)),
        ("财务", ("财务",)),
        ("接口字段", ("接口字段", "字段")),
        ("测试环境", ("测试环境",)),
        ("权限", ("权限",)),
        ("运维", ("运维",)),
        ("专员", ("专员",)),
    ]
    for marker, alternatives in fact_groups:
        if marker in task:
            checks.append((marker, alternatives))

    if "测试环境" in task and "同步" in task:
        if not re.search(r"(同步|更新|发到|放到).{0,8}测试环境|测试环境.{0,8}(同步|更新)", candidate):
            checks.append(("同步测试环境", ("__missing_sync_test_env__",)))

    if not checks:
        return 1.0

    missing: list[str] = []
    hits = 0
    seen: set[str] = set()
    for label, alternatives in checks:
        if label in seen:
            continue
        seen.add(label)
        if any(value in candidate for value in alternatives):
            hits += 1
        else:
            missing.append(label)
    if missing:
        notes.append("missing task facts: " + " / ".join(missing))
    return hits / max(len(seen), 1)


def weighted_average(parts: list[tuple[str, float, float]]) -> tuple[float, dict[str, float]]:
    total_weight = sum(weight for _, _, weight in parts if weight > 0)
    if total_weight <= 0:
        return 0.0, {name: score for name, score, _ in parts}
    value = sum(score * weight for _, score, weight in parts if weight > 0) / total_weight
    return value, {name: score for name, score, _ in parts}


def score_candidate(spec: dict[str, Any], candidate: str, source_text: str = "") -> CandidateScore:
    candidate = candidate.strip()
    source_text = source_text.strip()
    notes: list[str] = []
    query = build_query(spec, source_text)
    char_count = compact_char_count(candidate)

    m_score = model_score(query, candidate)
    l_score = length_score(spec, char_count, notes)
    keep_score, missing_must_include = must_include_score(spec, candidate, notes)
    banned_score, template_score = phrase_penalty_score(spec, candidate, notes)
    source_reduction_score = source_template_reduction_score(source_text, candidate, notes)
    similarity_score = rewrite_similarity_score(source_text, candidate, notes)
    splice_score = sentence_splice_score(candidate, notes)
    placeholder_score = placeholder_output_score(candidate, notes)
    coverage_score = rewrite_coverage_score(source_text, candidate, notes)
    fmt_score = formatting_score(candidate, notes)
    repeat_score = repeated_ngram_penalty(candidate, notes)
    d_score = detail_score(spec, char_count, candidate, notes)
    e_score = email_shape_score(spec, char_count, candidate, notes)
    audience_score = audience_fit_score(spec, candidate, notes)
    fact_score = task_fact_score(spec, candidate, notes)

    r_score, breakdown = weighted_average(
        [
            ("length", l_score, 0.10),
            ("must_include", keep_score, 0.22),
            ("banned_phrases", banned_score, 0.12),
            ("template_tone", template_score, 0.14),
            ("source_template_reduction", source_reduction_score, 0.22),
            ("rewrite_similarity", similarity_score, 0.14),
            ("sentence_splice", splice_score, 0.10),
            ("placeholder_output", placeholder_score, 0.12),
            ("rewrite_coverage", coverage_score, 0.12),
            ("formatting", fmt_score, 0.04),
            ("detailfulness", d_score, 0.08),
            ("email_shape", e_score, 0.05),
            ("audience_fit", audience_score, 0.10),
            ("task_facts", fact_score, 0.12),
            ("anti_repetition", repeat_score, 0.03),
        ],
    )

    final = 0.64 * m_score + 0.36 * r_score
    task = str(spec.get("task") or "")
    lowered_task = task.lower()
    is_email = "邮件" in task or "email" in lowered_task
    email_shape_hard_fail = False
    if is_email:
        hard_constraints = spec.get("hard_constraints") or {}
        max_chars = hard_constraints.get("max_chars")
        is_tight_short_email = max_chars is not None and int(max_chars) <= 140
        if is_tight_short_email:
            email_shape_hard_fail = "missing email-style greeting" in notes or e_score < 0.5
        else:
            email_shape_hard_fail = (
                "missing email-style greeting" in notes
                or "reads like a chat reply, not a full email body" in notes
                or e_score < 0.75
            )
    severe_template_carryover = (
        source_reduction_score <= 0.05
        and template_score <= 0.75
        and any(note.startswith("retains source template phrases:") for note in notes)
    )
    severe_similarity = similarity_score <= 0.35
    severe_splice = splice_score <= 0.0
    severe_placeholder = placeholder_score <= 0.0
    severe_coverage = coverage_score <= 0.35
    if severe_template_carryover:
        notes.append("severe template carryover from source")
    if severe_similarity:
        notes.append("rewrite change is too small")
    if severe_splice:
        notes.append("sentence structure is broken by phrase collision")
    if severe_placeholder:
        notes.append("candidate still looks like placeholder text")
    if severe_coverage:
        notes.append("rewrite removed too much source content")
    audience_hard_fail = audience_score < 0.65 or any(
        marker in notes
        for marker in (
            "wrong audience: reads like a manager-facing salutation",
            "wrong audience: reads like a customer-facing reply",
        )
    )

    hard_fail = (
        missing_must_include
        or banned_score <= 0.36
        or l_score <= 0.2
        or d_score <= 0.3
        or e_score <= 0.32
        or fact_score < 0.55
        or email_shape_hard_fail
        or audience_hard_fail
        or severe_template_carryover
        or severe_similarity
        or severe_splice
        or severe_placeholder
        or severe_coverage
    )
    if hard_fail:
        final *= 0.82

    return CandidateScore(
        final_score=max(0.0, min(1.0, final)),
        model_score=max(0.0, min(1.0, m_score)),
        rule_score=max(0.0, min(1.0, r_score)),
        hard_fail=hard_fail,
        char_count=char_count,
        query=query,
        rule_breakdown=breakdown,
        notes=notes,
    )


def dump_score_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
