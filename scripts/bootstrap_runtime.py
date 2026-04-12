from __future__ import annotations

import argparse
import json

from runtime_common import (
    DEFAULT_MODEL_ID,
    download_model_snapshot,
    reexec_into_runtime,
    runtime_summary,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap local runtime and download scorer model.",
    )
    parser.add_argument(
        "--force-reinstall",
        action="store_true",
        help="Recreate the dedicated runtime venv before continuing.",
    )
    parser.add_argument(
        "--force-model-download",
        action="store_true",
        help="Redownload the scorer model snapshot.",
    )
    parser.add_argument(
        "--skip-model",
        action="store_true",
        help="Only prepare the runtime venv.",
    )
    args = parser.parse_args()

    reexec_into_runtime(force_reinstall=args.force_reinstall)

    payload = runtime_summary()
    if args.skip_model:
        payload["model_downloaded"] = "false"
    else:
        downloaded = download_model_snapshot(
            model_id=DEFAULT_MODEL_ID,
            force=args.force_model_download,
        )
        payload["model_downloaded"] = "true"
        payload["model_dir"] = str(downloaded)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
