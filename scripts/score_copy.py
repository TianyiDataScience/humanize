from __future__ import annotations

import argparse
import json
from pathlib import Path

from runtime_common import reexec_into_runtime

reexec_into_runtime()

from scoring_core import dump_score_json, load_yaml, read_text, score_candidate  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Score one candidate draft.")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON output path. Defaults to <candidate>.score.json",
    )
    args = parser.parse_args()

    spec = load_yaml(args.spec)
    candidate_text = read_text(args.candidate)
    source_text = read_text(args.source)
    score = score_candidate(spec, candidate_text, source_text)

    payload = {
        "candidate_path": str(args.candidate.resolve()),
        "source_path": str(args.source.resolve()) if args.source else "",
        "spec_path": str(args.spec.resolve()),
        **score.as_dict(),
    }
    output_path = args.output or args.candidate.with_suffix(".score.json")
    dump_score_json(output_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
