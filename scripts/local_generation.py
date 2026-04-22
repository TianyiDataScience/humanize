from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from runtime_common import DEFAULT_GOAL, copaw_working_dir
from strategy_state import PROFILE_LIBRARY


def _http_get_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_post_json(url: str, payload: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _copaw_python() -> Path:
    wd = copaw_working_dir()
    if __import__("os").name == "nt":
        return wd / "venv" / "Scripts" / "python.exe"
    return wd / "venv" / "bin" / "python"


def _copaw_bridge_script() -> Path:
    return Path(__file__).resolve().parent / "copaw_active_generate.py"


def _active_generation_timeout() -> float:
    raw = os.environ.get("HUMANIZE_ACTIVE_MODEL_TIMEOUT", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return 60.0


def _subprocess_json(args: list[str], timeout: float = 30.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or str(exc)
        raise RuntimeError(f"Subprocess failed: {detail}") from exc
    stdout = (proc.stdout or "").strip()
    if not stdout:
        raise RuntimeError("Subprocess returned empty stdout")
    return json.loads(stdout)


def _call_copaw_active_model(
    *,
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    python_path = _copaw_python()
    script_path = _copaw_bridge_script()
    if not python_path.exists():
        raise RuntimeError(f"CoPaw python not found at {python_path}")
    if not script_path.exists():
        raise RuntimeError(f"CoPaw bridge script not found at {script_path}")
    payload = _subprocess_json(
        [
            str(python_path),
            str(script_path),
            "--system-prompt",
            system_prompt,
            "--user-prompt",
            user_prompt,
            "--temperature",
            str(temperature),
            "--max-tokens",
            str(max_tokens),
        ],
        timeout=_active_generation_timeout(),
    )
    text = str(payload.get("text") or "").strip()
    thinking = str(payload.get("thinking") or "").strip()
    return {
        "backend": "copaw-active",
        "provider_id": str(payload.get("provider_id") or ""),
        "model": str(payload.get("model") or ""),
        "choices": [
            {
                "message": {
                    "content": text,
                    "reasoning_content": thinking,
                },
            },
        ],
    }


def discover_generation_backend() -> dict[str, str]:
    mode = __import__("os").environ.get("HUMANIZE_GENERATION_BACKEND", "").strip().lower()
    prefer_copaw = mode not in {"local", "local-http", "http"}
    if prefer_copaw:
        try:
            payload = _subprocess_json(
                [
                    str(_copaw_python()),
                    str(_copaw_bridge_script()),
                    "--probe",
                ],
                timeout=20.0,
            )
            provider_id = str(payload.get("provider_id") or "").strip()
            model = str(payload.get("model") or "").strip()
            if provider_id and model:
                return {
                    "kind": "copaw-active",
                    "base_url": f"copaw-active://{provider_id}",
                    "model": f"{provider_id}/{model}",
                }
        except Exception:
            if mode in {"copaw", "copaw-active", "active"}:
                raise

    base_url = discover_base_url()
    ensure_endpoint_ready(base_url)
    model = discover_model(base_url)
    return {
        "kind": "local-http",
        "base_url": base_url,
        "model": model,
    }


def discover_base_url() -> str:
    base_url = __import__("os").environ.get("HUMANIZE_LLM_BASE_URL", "").strip()
    if base_url:
        return base_url.rstrip("/")

    candidates: list[str] = []
    log_paths = [
        copaw_working_dir() / "copaw.log",
        copaw_working_dir() / "qwenpaw.log",
        Path.home() / ".qwenpaw" / "qwenpaw-app.log",
    ]
    for log_path in log_paths:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            for port in re.findall(r"http://127\.0\.0\.1:(\d+)/health", text):
                candidates.append(f"http://127.0.0.1:{port}/v1")
            for port in re.findall(r"llama\.cpp server started on port (\d+)", text):
                candidates.append(f"http://127.0.0.1:{port}/v1")

    provider_dir = Path.home() / ".copaw.secret" / "providers" / "builtin"
    for provider_id in ("qwenpaw-local", "copaw-local"):
        provider_path = provider_dir / f"{provider_id}.json"
        if not provider_path.exists():
            continue
        try:
            payload = json.loads(provider_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        provider_base_url = str(payload.get("base_url") or "").strip()
        if provider_base_url:
            candidates.append(provider_base_url.rstrip("/"))

    seen: set[str] = set()
    for candidate in reversed(candidates):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            ensure_endpoint_ready(candidate)
            return candidate
        except Exception:
            continue

    return "http://127.0.0.1:54841/v1"


def ensure_endpoint_ready(base_url: str) -> None:
    root_url = base_url.removesuffix("/v1")
    checks = [
        (
            root_url + "/health",
            lambda payload: payload.get("status") == "ok",
            "health",
        ),
        (
            base_url.rstrip("/") + "/models",
            lambda payload: bool(payload.get("data") or payload.get("models")),
            "models",
        ),
        (
            root_url + "/api/tags",
            lambda payload: bool(payload.get("models")),
            "api_tags",
        ),
    ]
    errors: list[str] = []
    for url, validator, label in checks:
        try:
            payload = _http_get_json(url, timeout=5.0)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            continue
        if validator(payload):
            return
        errors.append(f"{label}: unexpected payload {payload}")
    joined = "; ".join(errors) if errors else "unknown error"
    raise RuntimeError(f"Local generation endpoint is unavailable at {base_url}: {joined}")


def discover_model(base_url: str) -> str:
    env_model = __import__("os").environ.get("HUMANIZE_LLM_MODEL", "").strip()
    if env_model:
        return env_model
    payload = _http_get_json(base_url.rstrip("/") + "/models", timeout=10.0)
    models = payload.get("data") or payload.get("models") or []
    if not models:
        raise RuntimeError(f"No models returned by local CoPaw endpoint at {base_url}")
    first = models[0]
    return str(first.get("id") or first.get("model") or first.get("name") or "").strip()


def call_chat(
    *,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 300,
) -> dict[str, Any]:
    if base_url.startswith("copaw-active://"):
        return _call_copaw_active_model(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    reasoning_effort = os.environ.get("HUMANIZE_LLM_REASONING_EFFORT", "").strip().lower()
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    try:
        return _http_post_json(base_url.rstrip("/") + "/chat/completions", payload, timeout=120.0)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Local CoPaw generation failed: {exc.code} {detail}") from exc


def extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError(f"Local CoPaw generation returned no choices: {response}")
    message = choices[0].get("message") or {}
    content = str(message.get("content") or "").strip()
    if content:
        marker_matches = re.findall(r"FINAL_CANDIDATE:\s*(.+)", content)
        if marker_matches:
            return marker_matches[-1].strip(" “\"'”")
        return content

    reasoning = str(message.get("reasoning_content") or "").strip()
    if not reasoning:
        raise RuntimeError(f"Local CoPaw generation returned empty content: {response}")

    tag_match = re.search(r"<final>\s*(.+?)\s*</final>", reasoning, flags=re.IGNORECASE | re.DOTALL)
    if tag_match:
        return tag_match.group(1).strip()

    marker_match = re.search(r"FINAL_CANDIDATE:\s*(.+)", reasoning)
    if marker_match:
        return marker_match.group(1).strip(" “\"'”")

    labeled_match = re.search(
        r"(?:最终文案|最终回复|最终候选|候选文案|草稿|可能的草稿|建议文案)\s*[：:]\s*(.+)",
        reasoning,
    )
    if labeled_match:
        candidate = labeled_match.group(1).strip(" “\"'”")
        quoted = re.findall(r"[“\"「『]([^”\"」』\n]{8,120})[”\"」』]", candidate)
        if quoted:
            return quoted[-1].strip()
        if candidate:
            return candidate

    line_candidates = []
    for line in reasoning.splitlines():
        stripped = line.strip(" -•\t")
        if not stripped:
            continue
        if stripped.startswith(("比如", "例如")):
            quoted = re.findall(r"[“\"「『]([^”\"」』\n]{8,120})[”\"」』]", stripped)
            if quoted:
                return quoted[-1].strip()
        if any(prefix in stripped for prefix in ("最终选择", "草拟文案", "更简洁", "最终文案", "候选文案")) and "：" in stripped:
            candidate = stripped.split("：", 1)[1].strip(" “\"'”")
            quoted = re.findall(r"[“\"「『]([^”\"」』\n]{8,120})[”\"」』]", candidate)
            if quoted:
                return quoted[-1].strip()
            if candidate:
                line_candidates.append(candidate)
    if line_candidates:
        return line_candidates[-1]

    raise RuntimeError(f"Local CoPaw generation returned empty content and no recoverable final candidate: {response}")


def _constraint_lines(hard_constraints: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    max_chars = hard_constraints.get("max_chars")
    min_chars = hard_constraints.get("min_chars")
    must_include = hard_constraints.get("must_include") or []
    banned = hard_constraints.get("banned_phrases") or []
    if max_chars is not None:
        lines.append(f"- 不超过 {max_chars} 字")
    if min_chars is not None:
        lines.append(f"- 不少于 {min_chars} 字")
    if must_include:
        lines.append("- 必须包含：" + "、".join(str(x) for x in must_include))
    if banned:
        lines.append("- 不要出现：" + "、".join(str(x) for x in banned))
    return lines


def _audience_lines(task: str) -> list[str]:
    lines: list[str] = []
    if "客户" in task:
        lines.extend(
            [
                "- 这是发给客户的消息，不是发给财务或内部同事的消息。",
                "- 不要把客户当成被提问对象，不要反过来向客户索要内部确认。",
            ]
        )
    if "上级" in task or "老板" in task:
        lines.append("- 这是发给上级的消息，不是发给同级或客户的消息。")
    if "面试" in task:
        lines.append("- 这是发给招聘方或面试官的消息，不是发给朋友的消息。")
    if "邮件" in task or "email" in task.lower():
        lines.extend(
            [
                "- 这是邮件回复，不是微信一句话短消息。",
                "- 至少写成完整邮件正文：称呼 + 当前进展 + 下一步时间点。",
                "- 语气要专业、自然、清楚，但不要模板腔。",
            ]
        )
    return lines


def _guidance_lines(
    *,
    profile_name: str,
    strategy_directives: list[str] | None,
    failure_tags: list[str] | None,
) -> list[str]:
    lines: list[str] = []
    profile_desc = PROFILE_LIBRARY.get(profile_name, "").strip()
    if profile_name:
        if profile_desc:
            lines.append(f"- 当前候选风格：{profile_name}。{profile_desc}")
        else:
            lines.append(f"- 当前候选风格：{profile_name}")
    if strategy_directives:
        lines.append("- 当前策略要求：")
        lines.extend(f"  {item}" for item in strategy_directives if item.strip())
    if failure_tags:
        lines.append("- 上一轮失败标签：" + "、".join(failure_tags))
    return lines


def _compact_constraints_for_direct(hard_constraints: dict[str, Any]) -> str:
    lines = _constraint_lines(hard_constraints)
    if not lines:
        return ""
    return "硬约束：\n" + "\n".join(lines) + "\n"


def build_direct_rewrite_prompt(
    *,
    task: str,
    hard_constraints: dict[str, Any],
    source_text: str,
) -> tuple[str, str]:
    system = (
        "你是中文文案直改器。\n"
        "只输出改写后的正文，不要解释，不要标题，不要项目符号。\n"
        "如果底层模型会把内容拆成 reasoning_content，最后一行必须写成 FINAL_CANDIDATE: <改写后的正文>。"
    )
    constraints = _compact_constraints_for_direct(hard_constraints)
    user = (
        f"任务：{task}\n"
        f"{constraints}"
        "原文：\n"
        f"{source_text}\n\n"
        "你只需要把这段文本改得更像真人会写的话。\n"
        "保留原意和必要事实。\n"
        "减少模板腔、客服腔、公告腔、AI 味。\n"
        "只输出改写后的正文。"
    )
    return system, user


def build_direct_repair_prompt(
    *,
    task: str,
    hard_constraints: dict[str, Any],
    source_text: str,
    current_best_text: str,
    failure_tags: list[str] | None = None,
) -> tuple[str, str]:
    system = (
        "你是中文文案修复器。\n"
        "只输出修复后的正文，不要解释，不要标题，不要项目符号。\n"
        "如果底层模型会把内容拆成 reasoning_content，最后一行必须写成 FINAL_CANDIDATE: <修复后的正文>。"
    )
    constraints = _compact_constraints_for_direct(hard_constraints)
    failure_line = "残留问题：" + "、".join(failure_tags or []) + "\n" if failure_tags else ""
    user = (
        f"任务：{task}\n"
        f"{constraints}"
        f"{failure_line}"
        "原文只作为事实参照，不要从原文重写：\n"
        f"{source_text}\n\n"
        "当前最佳版本：\n"
        f"{current_best_text}\n\n"
        "这是当前最佳版本，不要从原文重写。\n"
        "只修残留问题。\n"
        "保留已经自然的表达。\n"
        "减少残留的模板腔、客服腔、公告腔、AI 味。\n"
        "保留原意和必要事实。\n"
        "只输出修复后的正文。"
    )
    return system, user


def build_generation_prompts(
    *,
    task: str,
    hard_constraints: dict[str, Any],
    original: str,
    mode: str,
    baseline_profile: str = "steady",
    challenger_profile: str = "natural",
    strategy_directives: list[str] | None = None,
    failure_tags: list[str] | None = None,
    revision_mode: str = "rewrite",
) -> tuple[tuple[str, str], tuple[str, str]]:
    constraints = "\n".join(_constraint_lines(hard_constraints)) or "- 无额外硬约束"
    audience = "\n".join(_audience_lines(task))
    baseline_guidance = "\n".join(
        _guidance_lines(
            profile_name=baseline_profile,
            strategy_directives=strategy_directives,
            failure_tags=[],
        ),
    )
    challenger_guidance = "\n".join(
        _guidance_lines(
            profile_name=challenger_profile,
            strategy_directives=strategy_directives,
            failure_tags=failure_tags,
        ),
    )
    system = (
        "你是 humanize 的文案生成器。\n"
        "你的职责是生成中文沟通消息，语气自然、像真人发送、少模板腔。\n"
        "只输出最终文案本身，不要解释，不要标题，不要思考过程，不要项目符号。\n"
        "如果底层模型仍然会输出思考过程，把它压缩到最短，并在最后明确给出唯一最终文案。\n"
        "如果底层模型会把内容拆成 reasoning_content，那么最后一行必须写成 FINAL_CANDIDATE: <最终文案>。"
    )
    baseline_user = (
        f"任务：{task}\n"
        f"默认优化方向：{DEFAULT_GOAL}\n"
        f"硬约束：\n{constraints}\n"
        f"{audience + chr(10) if audience else ''}"
        f"{baseline_guidance + chr(10) if baseline_guidance else ''}"
        "请先生成一个 baseline 版本。\n"
        "要求：\n"
        "- 先求稳，信息完整，语气自然。\n"
        "- 不要过度润色，不要模板腔。\n"
        f"{'- 如果是邮件场景，写成完整邮件回复正文，不要退化成一句短消息。'+ chr(10) if ('邮件' in task or 'email' in task.lower()) else ''}"
        "- 只输出最终文案。\n"
        "- 如果出现思考过程，最后一行必须是 FINAL_CANDIDATE: <最终文案>。"
    )
    if mode == "rewrite":
        baseline_user = (
            f"任务：{task}\n"
            f"默认优化方向：{DEFAULT_GOAL}\n"
            f"硬约束：\n{constraints}\n"
            f"{audience + chr(10) if audience else ''}"
            f"原文：{original}\n"
            "原文已经是 baseline，不需要改写。\n"
            "请原样输出原文，不要添加解释。\n"
            "如果出现思考过程，最后一行必须是 FINAL_CANDIDATE: <最终文案>。"
        )
    if mode == "rewrite" and revision_mode == "repair":
        challenger_user = (
            f"任务：{task}\n"
            f"默认优化方向：{DEFAULT_GOAL}\n"
            f"硬约束：\n{constraints}\n"
            f"{audience + chr(10) if audience else ''}"
            f"{challenger_guidance + chr(10) if challenger_guidance else ''}"
            f"{'原始文本：' + original + chr(10) if original else ''}"
            "这是当前最佳版本（best-so-far），不是原始文本：\n"
            "{baseline}\n"
            "请基于当前最佳版本继续修复，生成下一个 challenger 版本。\n"
            "要求：\n"
            "- 必须满足所有硬约束。\n"
            "- 重点修复上一轮残留问题，不要从原始文本重新整篇改写。\n"
            "- 尽量保留当前最佳版本里已经自然、已经改好的表达。\n"
            "- 如果某一段已经自然，就不要为了改而改。\n"
            "- 不要把已经成型的完整文案压缩成一句空话。\n"
            "- 保留关键信息，不要改变任务语义。\n"
            f"{'- 如果任务是邮件回复，保持完整邮件正文形态，不要回退成聊天短句。'+ chr(10) if ('邮件' in task or 'email' in task.lower()) else ''}"
            "- 只输出最终文案。\n"
            "- 如果出现思考过程，最后一行必须是 FINAL_CANDIDATE: <最终文案>。"
        )
    else:
        challenger_user = (
            f"任务：{task}\n"
            f"默认优化方向：{DEFAULT_GOAL}\n"
            f"硬约束：\n{constraints}\n"
            f"{audience + chr(10) if audience else ''}"
            f"{challenger_guidance + chr(10) if challenger_guidance else ''}"
            f"{'原始文本：' + original + chr(10) if original else ''}"
            "这是当前 baseline：\n"
            "{baseline}\n"
            "请生成一个 challenger 版本。\n"
            "要求：\n"
            "- 必须满足所有硬约束。\n"
            "- 比 baseline 更像真人自然发送的中文沟通消息。\n"
            "- 保留关键信息，不要改变任务语义。\n"
            "- 与 baseline 要有明确差异，不要只改一两个字。\n"
            f"{'- 如果任务是邮件回复，保持邮件正文形态，至少包含称呼、进展、下一步时间点，不要缩成一句话。'+ chr(10) if ('邮件' in task or 'email' in task.lower()) else ''}"
            "- 只输出最终文案。\n"
            "- 如果出现思考过程，最后一行必须是 FINAL_CANDIDATE: <最终文案>。"
        )
    return (system, baseline_user), (system, challenger_user)
