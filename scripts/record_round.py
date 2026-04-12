from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Append one compare result to a run log.")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()

    result = json.loads(args.result.read_text(encoding="utf-8"))
    result["recorded_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    log_path = args.run_dir / "rounds.jsonl"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")

    round_dir = args.run_dir / "rounds"
    round_dir.mkdir(exist_ok=True)
    index = len(list(round_dir.glob("round-*.json"))) + 1
    snapshot_path = round_dir / f"round-{index:03d}.json"
    snapshot_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "log_path": str(log_path.resolve()),
                "snapshot_path": str(snapshot_path.resolve()),
                "round_index": index,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


if __name__ == "__main__":
    main()
