from __future__ import annotations

import argparse
import json
from pathlib import Path

from runtime_common import reexec_into_runtime

reexec_into_runtime()

from scoring_core import dump_score_json, load_yaml, read_text, score_candidate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare baseline vs challenger and output keep/discard.",
    )
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--challenger", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument(
        "--margin",
        type=float,
        default=None,
        help="Improvement margin. Defaults to spec.evaluator.minimum_improvement or 0.015.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSON path. Defaults to compare-result.json next to baseline.",
    )
    args = parser.parse_args()

    spec = load_yaml(args.spec)
    source_text = read_text(args.source)
    baseline_score = score_candidate(spec, read_text(args.baseline), source_text)
    challenger_score = score_candidate(
        spec,
        read_text(args.challenger),
        source_text,
    )

    default_margin = (
        ((spec.get("evaluator") or {}).get("minimum_improvement")) or 0.015
    )
    margin = args.margin if args.margin is not None else float(default_margin)
    delta = challenger_score.final_score - baseline_score.final_score

    if challenger_score.hard_fail:
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

    payload = {
        "decision": decision,
        "winner": winner,
        "reason": reason,
        "margin": margin,
        "delta": round(delta, 6),
        "spec_path": str(args.spec.resolve()),
        "source_path": str(args.source.resolve()) if args.source else "",
        "baseline": {
            "path": str(args.baseline.resolve()),
            **baseline_score.as_dict(),
        },
        "challenger": {
            "path": str(args.challenger.resolve()),
            **challenger_score.as_dict(),
        },
    }

    output_path = args.output or args.baseline.parent / "compare-result.json"
    dump_score_json(output_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
