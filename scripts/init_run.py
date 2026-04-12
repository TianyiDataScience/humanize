from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path


def slugify(text: str) -> str:
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text.strip().lower())
    compact = re.sub(r"-{2,}", "-", compact).strip("-")
    return compact[:40] or "run"


def extract_task_from_yaml_text(text: str) -> str:
    match = re.search(r"(?m)^task:\s*(.+?)\s*$", text)
    if not match:
        return ""
    value = match.group(1).strip()
    value = value.strip("'").strip('"')
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new optimization run folder.")
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("./runs"),
    )
    args = parser.parse_args()

    spec_text = args.spec.read_text(encoding="utf-8")
    task = extract_task_from_yaml_text(spec_text)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slugify(task)
    run_dir = args.output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "rounds").mkdir()

    (run_dir / "spec.yaml").write_text(
        spec_text,
        encoding="utf-8",
    )
    if args.source and args.source.exists():
        (run_dir / "source.txt").write_text(
            args.source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    else:
        (run_dir / "source.txt").write_text("", encoding="utf-8")

    payload = {
        "run_dir": str(run_dir.resolve()),
        "spec_path": str((run_dir / "spec.yaml").resolve()),
        "source_path": str((run_dir / "source.txt").resolve()),
        "rounds_dir": str((run_dir / "rounds").resolve()),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
