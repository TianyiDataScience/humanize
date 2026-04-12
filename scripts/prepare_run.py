from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from parse_user_brief import build_payload


def slugify(text: str) -> str:
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text.strip().lower())
    compact = re.sub(r"-{2,}", "-", compact).strip("-")
    return compact[:40] or "run"


def create_run_dir(output_root: Path, task: str) -> Path:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slugify(task)
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "rounds").mkdir(exist_ok=True)
    return run_dir


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_session_plan(payload: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    parsed = payload["parsed"]
    session_mode = payload["session_mode"]
    if session_mode == "generate":
        summary = "No original draft was provided. Generate a baseline from task and constraints first, then generate a challenger and compare them."
    else:
        summary = "An original draft was provided. Use it as baseline, then generate one challenger and compare them."
    return {
        "session_mode": session_mode,
        "input_mode": payload["input_mode"],
        "task": parsed["task"],
        "hard_constraints": parsed["hard_constraints"],
        "goal_override": parsed["goal"],
        "original_provided": bool(parsed["original"]),
        "baseline_strategy": "use_original_as_baseline" if session_mode == "rewrite" else "generate_baseline_from_task",
        "run_dir": str(run_dir.resolve()),
        "spec_path": str((run_dir / "spec.yaml").resolve()),
        "source_path": str((run_dir / "source.txt").resolve()),
        "baseline_path": str((run_dir / "baseline.txt").resolve()),
        "challenger_path": str((run_dir / "challenger.txt").resolve()),
        "compare_result_path": str((run_dir / "compare-result.json").resolve()),
        "report_html_path": str((run_dir / "report.html").resolve()),
        "summary": summary,
        "next_actions": (
            [
                "Generate one baseline draft from the task and constraints.",
                "Generate one challenger draft that is intentionally different.",
                "Run scripts/run_session.py with the baseline and challenger.",
            ]
            if session_mode == "generate"
            else [
                "Treat source.txt as baseline.",
                "Generate one challenger draft that improves naturalness without breaking constraints.",
                "Run scripts/run_session.py with the baseline and challenger.",
            ]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a visible humanize run from raw user input.",
    )
    parser.add_argument("--text", default=None)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=Path("./runs"))
    args = parser.parse_args()

    if args.text is None and args.input is None:
        raise ValueError("Provide --text or --input")

    raw_text = args.text if args.text is not None else args.input.read_text(encoding="utf-8")
    payload = build_payload(raw_text)
    task = str(payload["parsed"]["task"] or "run")
    run_dir = args.run_dir.resolve() if args.run_dir else create_run_dir(args.output_root.resolve(), task)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "rounds").mkdir(exist_ok=True)

    write_text(
        run_dir / "spec.yaml",
        yaml.safe_dump(
            payload["spec"],
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
    )
    write_text(run_dir / "user-brief.txt", raw_text.strip() + "\n")
    write_text(run_dir / "source.txt", payload["parsed"]["original"])
    if payload["session_mode"] == "rewrite":
        write_text(run_dir / "baseline.txt", payload["parsed"]["original"])

    session_plan = build_session_plan(payload, run_dir)
    write_text(
        run_dir / "session-plan.json",
        json.dumps(session_plan, ensure_ascii=False, indent=2),
    )
    write_text(
        run_dir / "parse-result.json",
        json.dumps(payload, ensure_ascii=False, indent=2),
    )

    print(
        json.dumps(
            {
                "run_dir": str(run_dir.resolve()),
                "session_mode": payload["session_mode"],
                "input_mode": payload["input_mode"],
                "task": payload["parsed"]["task"],
                "source_path": str((run_dir / "source.txt").resolve()),
                "spec_path": str((run_dir / "spec.yaml").resolve()),
                "session_plan_path": str((run_dir / "session-plan.json").resolve()),
                "baseline_path": str((run_dir / "baseline.txt").resolve()) if payload["session_mode"] == "rewrite" else "",
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


if __name__ == "__main__":
    main()
