from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from runtime_common import DEFAULT_GOAL


def append_clean(items: list[str], values: list[str] | None) -> None:
    if not values:
        return
    for value in values:
        text = str(value).strip()
        if text:
            items.append(text)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a YAML spec for humanize.",
    )
    parser.add_argument("--task", required=True)
    parser.add_argument("--goal")
    parser.add_argument("--style-note", action="append", default=[])
    parser.add_argument("--must-include", action="append", default=[])
    parser.add_argument("--banned-phrase", action="append", default=[])
    parser.add_argument("--min-chars", type=int, default=None)
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--minimum-improvement", type=float, default=0.015)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    style_notes: list[str] = []
    must_include: list[str] = []
    banned_phrases: list[str] = []
    append_clean(style_notes, args.style_note)
    append_clean(must_include, args.must_include)
    append_clean(banned_phrases, args.banned_phrase)

    hard_constraints: dict[str, object] = {}
    if args.min_chars is not None:
        hard_constraints["min_chars"] = int(args.min_chars)
    if args.max_chars is not None:
        hard_constraints["max_chars"] = int(args.max_chars)
    if must_include:
        hard_constraints["must_include"] = must_include
    if banned_phrases:
        hard_constraints["banned_phrases"] = banned_phrases

    payload = {
        "task": str(args.task).strip(),
        "hard_constraints": hard_constraints,
        "evaluator": {
            "minimum_improvement": float(args.minimum_improvement),
        },
    }
    goal_text = str(args.goal or "").strip()
    if goal_text and goal_text != DEFAULT_GOAL:
        payload["goal"] = goal_text
    if style_notes:
        payload["style_notes"] = style_notes

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    print(args.output.resolve())


if __name__ == "__main__":
    main()
