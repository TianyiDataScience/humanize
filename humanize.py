from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path


def build_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text.strip()

    original = args.original
    if not original and args.baseline_text and not args.challenger_text:
        original = args.baseline_text

    lines = ["用 humanize 帮我生成并优化一条中文沟通消息。"]
    if args.task:
        lines.append(f"任务：{args.task.strip()}")
    if args.constraints:
        constraint_parts = [args.constraints.strip()]
    else:
        constraint_parts = []
    if args.must_include:
        constraint_parts.append("保留" + "、".join(f"“{item.strip()}”" for item in args.must_include if item.strip()))
    if args.banned_phrase:
        constraint_parts.append("避免" + "、".join(f"“{item.strip()}”" for item in args.banned_phrase if item.strip()))
    if args.min_chars is not None:
        constraint_parts.append(f"不少于{args.min_chars}字")
    if args.max_chars is not None:
        constraint_parts.append(f"控制在{args.max_chars}字内")
    if constraint_parts:
        lines.append("约束：" + "，".join(constraint_parts))
    if original:
        lines.append(f"原文：{original.strip()}")
    return "\n".join(line for line in lines if line.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Top-level wrapper for the humanize skill.",
    )
    parser.add_argument("--text", default=None)
    parser.add_argument("--task", default=None)
    parser.add_argument("--constraints", default=None)
    parser.add_argument("--must-include", action="append", default=[])
    parser.add_argument("--banned-phrase", action="append", default=[])
    parser.add_argument("--min-chars", type=int, default=None)
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--original", "--original-draft", dest="original", default=None)
    parser.add_argument("--baseline-text", default=None)
    parser.add_argument("--challenger-text", default=None)
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--output-root", default="./runs")
    args = parser.parse_args()

    brief = build_text(args)
    if not brief.strip():
        raise ValueError("Provide --text or at least --task")

    script_path = Path(__file__).resolve().parent / "scripts" / "run_from_brief.py"
    sys.path.insert(0, str(script_path.parent))
    sys.argv = [
        str(script_path),
        "--text",
        brief,
        "--output-root",
        args.output_root,
    ]
    baseline_override = args.baseline_text if (args.original and args.baseline_text) else None
    if baseline_override:
        sys.argv.extend(["--baseline-text", args.baseline_text])
    if args.challenger_text:
        sys.argv.extend(["--challenger-text", args.challenger_text])
    if args.run_dir:
        sys.argv.extend(["--run-dir", args.run_dir])
    runpy.run_path(str(script_path), run_name="__main__")


if __name__ == "__main__":
    main()
