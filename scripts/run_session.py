from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from runtime_common import reexec_into_runtime

reexec_into_runtime()

from render_run_report import build_html, build_markdown  # noqa: E402
from scoring_core import dump_score_json, load_yaml, read_text, score_candidate  # noqa: E402


def slugify(text: str) -> str:
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text.strip().lower())
    compact = re.sub(r"-{2,}", "-", compact).strip("-")
    return compact[:40] or "run"


def resolve_text(path: Path | None, inline: str | None) -> str:
    if inline is not None:
        return inline.strip()
    if path is None:
        return ""
    return read_text(path)


def create_run_dir(output_root: Path, task: str) -> Path:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slugify(task)
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "rounds").mkdir(exist_ok=True)
    return run_dir


def compare_payload(
    spec: dict[str, Any],
    spec_path: Path,
    source_path: Path,
    baseline_path: Path,
    challenger_path: Path,
    baseline: Any,
    challenger: Any,
    margin: float,
) -> dict[str, Any]:
    delta = challenger.final_score - baseline.final_score
    if challenger.hard_fail:
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
            **baseline.as_dict(),
        },
        "challenger": {
            "path": str(challenger_path.resolve()),
            **challenger.as_dict(),
        },
    }


def write_round_log(run_dir: Path, compare: dict[str, Any]) -> None:
    payload = dict(compare)
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create one full visible optimization run with reports.",
    )
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--baseline-text", default=None)
    parser.add_argument("--challenger", type=Path, default=None)
    parser.add_argument("--challenger-text", default=None)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--source-text", default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=Path("./runs"))
    parser.add_argument("--margin", type=float, default=None)
    args = parser.parse_args()

    spec = load_yaml(args.spec)
    task = str(spec.get("task", "") or "run")
    run_dir = args.run_dir.resolve() if args.run_dir else create_run_dir(args.output_root.resolve(), task)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "rounds").mkdir(exist_ok=True)

    spec_text = args.spec.read_text(encoding="utf-8")
    baseline_text = resolve_text(args.baseline, args.baseline_text)
    challenger_text = resolve_text(args.challenger, args.challenger_text)
    source_text = resolve_text(args.source, args.source_text) or baseline_text

    if not baseline_text:
        raise ValueError("A baseline draft is required")
    if not challenger_text:
        raise ValueError("A challenger draft is required")

    spec_path = run_dir / "spec.yaml"
    source_path = run_dir / "source.txt"
    baseline_path = run_dir / "baseline.txt"
    challenger_path = run_dir / "challenger.txt"
    baseline_score_path = run_dir / "baseline.score.json"
    challenger_score_path = run_dir / "challenger.score.json"
    compare_path = run_dir / "compare-result.json"
    best_path = run_dir / "best.txt"

    spec_path.write_text(spec_text, encoding="utf-8")
    source_path.write_text(source_text, encoding="utf-8")
    baseline_path.write_text(baseline_text, encoding="utf-8")
    challenger_path.write_text(challenger_text, encoding="utf-8")

    baseline_score = score_candidate(spec, baseline_text, source_text)
    challenger_score = score_candidate(spec, challenger_text, source_text)
    dump_score_json(
        baseline_score_path,
        {
            "candidate_path": str(baseline_path.resolve()),
            "source_path": str(source_path.resolve()),
            "spec_path": str(spec_path.resolve()),
            **baseline_score.as_dict(),
        },
    )
    dump_score_json(
        challenger_score_path,
        {
            "candidate_path": str(challenger_path.resolve()),
            "source_path": str(source_path.resolve()),
            "spec_path": str(spec_path.resolve()),
            **challenger_score.as_dict(),
        },
    )

    default_margin = (
        ((spec.get("evaluator") or {}).get("minimum_improvement")) or 0.015
    )
    margin = args.margin if args.margin is not None else float(default_margin)
    compare = compare_payload(
        spec,
        spec_path,
        source_path,
        baseline_path,
        challenger_path,
        baseline_score,
        challenger_score,
        margin,
    )
    dump_score_json(compare_path, compare)
    write_round_log(run_dir, compare)

    if compare["winner"] == "challenger":
        best_path.write_text(challenger_text, encoding="utf-8")
    else:
        best_path.write_text(baseline_text, encoding="utf-8")

    report_md = run_dir / "report.md"
    report_html = run_dir / "report.html"
    report_md.write_text(
        build_markdown(compare, baseline_text, challenger_text),
        encoding="utf-8",
    )
    report_html.write_text(
        build_html(compare, baseline_text, challenger_text),
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "decision": compare["decision"],
                "winner": compare["winner"],
                "delta": compare["delta"],
                "baseline_score": baseline_score.final_score,
                "challenger_score": challenger_score.final_score,
                "best_path": str(best_path),
                "compare_result": str(compare_path),
                "markdown_report": str(report_md),
                "html_report": str(report_html),
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


if __name__ == "__main__":
    main()
