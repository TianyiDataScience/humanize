from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("regression cases must be a JSON list")
    return payload


def sanitize_case_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value).strip("-") or "case"


def extract_final_response(stdout: str) -> str:
    begin = "=== HUMANIZE_FINAL_RESPONSE_BEGIN ==="
    end = "=== HUMANIZE_FINAL_RESPONSE_END ==="
    if begin not in stdout or end not in stdout:
        return ""
    return stdout.split(begin, 1)[1].split(end, 1)[0].strip()


def count_phrases(text: str, phrases: list[str]) -> dict[str, int]:
    return {phrase: text.count(phrase) for phrase in phrases}


def run_case(root: Path, python_bin: str, output_root: Path, case: dict[str, Any], timeout: int) -> dict[str, Any]:
    case_id = sanitize_case_id(str(case.get("id") or "case"))
    prompt = str(case.get("prompt") or "").strip()
    if not prompt:
        raise ValueError(f"case {case_id} is missing prompt")

    run_dir = output_root / case_id
    if run_dir.exists():
        subprocess.run(["rm", "-rf", str(run_dir)], check=True)

    cmd = [
        python_bin,
        str(root / "humanize.py"),
        "--text",
        prompt,
        "--run-dir",
        str(run_dir),
        "--output-root",
        str(output_root),
    ]
    started_at = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = round(time.time() - started_at, 3)
    result_path = run_dir / "result.json"
    result_payload = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    session_trace = result_payload.get("session_trace") or []
    initial_baseline_text = str(result_payload.get("baseline_text") or "").strip()
    final_text = (result_payload.get("challenger_text") or result_payload.get("baseline_text") or "").strip()
    continue_rounds = [
        round_payload.get("round")
        for round_payload in session_trace
        if round_payload.get("decision") == "continue"
    ]
    repair_rounds = [
        round_payload.get("round")
        for round_payload in session_trace
        if round_payload.get("revision_mode") == "repair"
    ]
    revision_modes = [str(round_payload.get("revision_mode") or "rewrite") for round_payload in session_trace]
    base_text_kinds = [str(round_payload.get("base_text_kind") or "source") for round_payload in session_trace]
    best_so_far = initial_baseline_text
    continuity_ok = True
    for round_payload in session_trace:
        selected_text = str((round_payload.get("selected_candidate") or {}).get("text") or "").strip()
        if round_payload.get("revision_mode") == "repair":
            baseline_text = str(round_payload.get("baseline_text") or "").strip()
            if baseline_text != best_so_far:
                continuity_ok = False
        if round_payload.get("decision") in {"keep", "continue"} and selected_text:
            best_so_far = selected_text
    final_matches_last_improved = final_text == best_so_far if final_text else False
    tracked_phrases = list(((case.get("expectations") or {}).get("tracked_phrases")) or [])
    phrase_counts: dict[str, Any] = {}
    if tracked_phrases:
        round1_selected = ""
        if session_trace:
            round1_selected = str((session_trace[0].get("selected_candidate") or {}).get("text") or "").strip()
        phrase_counts = {
            "source": count_phrases(initial_baseline_text, tracked_phrases),
            "round1_selected": count_phrases(round1_selected, tracked_phrases),
            "final": count_phrases(final_text, tracked_phrases),
        }

    expectations = case.get("expectations") or {}
    expectation_errors: list[str] = []
    if expectations.get("requires_continue_round") and not continue_rounds:
        expectation_errors.append("expected a continue round")
    if expectations.get("requires_repair_round") and not repair_rounds:
        expectation_errors.append("expected at least one repair round")
    if expectations.get("requires_best_so_far_repair") and not continuity_ok:
        expectation_errors.append("repair round did not use current best text as baseline")
    if expectations.get("requires_final_match_last_improved") and not final_matches_last_improved:
        expectation_errors.append("final text did not preserve the last improved best-so-far")
    if expectations.get("requires_tracked_phrases_removed") and phrase_counts:
        if any(count > 0 for count in phrase_counts.get("final", {}).values()):
            expectation_errors.append("tracked residual phrases were not removed in final text")

    return {
        "id": case_id,
        "elapsed_seconds": elapsed,
        "returncode": proc.returncode,
        "run_dir": str(run_dir),
        "decision": result_payload.get("decision"),
        "winner": result_payload.get("winner"),
        "delta": result_payload.get("delta"),
        "round_count": len(session_trace),
        "continue_rounds": continue_rounds,
        "has_continue_round": bool(continue_rounds),
        "repair_rounds": repair_rounds,
        "revision_modes": revision_modes,
        "base_text_kinds": base_text_kinds,
        "best_so_far_continuity_ok": continuity_ok,
        "final_matches_last_improved": final_matches_last_improved,
        "quality_gate_blocked_rounds": [
            {
                "round": round_payload.get("round"),
                "tags": round_payload.get("quality_gate_tags") or [],
            }
            for round_payload in session_trace
            if round_payload.get("quality_gate_tags")
        ],
        "tracked_phrase_counts": phrase_counts,
        "expectation_errors": expectation_errors,
        "final_text": final_text,
        "final_response": extract_final_response(proc.stdout),
        "stderr": proc.stderr.strip(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the humanize regression sample set.")
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "examples" / "regression_cases.json",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "runs" / "regression-suite",
    )
    parser.add_argument("--python-bin", default=sys.executable or "python3")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    cases = load_cases(args.cases)
    if args.limit > 0:
        cases = cases[: args.limit]

    args.output_root.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    for case in cases:
        summary.append(
            run_case(
                root=root,
                python_bin=args.python_bin,
                output_root=args.output_root,
                case=case,
                timeout=args.timeout,
            ),
        )

    summary_path = args.output_root / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    for item in summary:
        if item["expectation_errors"]:
            raise SystemExit(
                f"{item['id']} failed expectations: {'; '.join(item['expectation_errors'])}",
            )
        continue_text = ",".join(str(round_no) for round_no in item["continue_rounds"]) or "-"
        repair_text = ",".join(str(round_no) for round_no in item["repair_rounds"]) or "-"
        print(
            f"{item['id']}: decision={item['decision']} "
            f"winner={item['winner']} rounds={item['round_count']} continue={continue_text} repair={repair_text} "
            f"delta={item['delta']} elapsed={item['elapsed_seconds']}s",
        )
    print(f"summary: {summary_path}")


if __name__ == "__main__":
    main()
