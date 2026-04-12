from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

SKILL_NAME = "humanize"
DEFAULT_MODEL_ID = "BAAI/bge-reranker-v2-m3"
RUNTIME_ACTIVE_ENV = "HUMANIZE_RUNTIME_ACTIVE"
DEFAULT_GOAL = (
    "更像真人自然发送的中文沟通消息，减少模板腔、客服腔、公告腔和过度AI润色感。"
    "保持清楚、可信、有分寸。"
)


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def copaw_working_dir() -> Path:
    return Path(
        os.environ.get("COPAW_WORKING_DIR", "~/.copaw"),
    ).expanduser().resolve()


def runtime_root() -> Path:
    return copaw_working_dir() / "models" / SKILL_NAME


def venv_dir() -> Path:
    return runtime_root() / "runtime-venv"


def _venv_bin_dir() -> Path:
    if os.name == "nt":
        return venv_dir() / "Scripts"
    return venv_dir() / "bin"


def runtime_python() -> Path:
    if os.name == "nt":
        return _venv_bin_dir() / "python.exe"
    return _venv_bin_dir() / "python"


def model_dir() -> Path:
    return runtime_root() / "models" / "primary"


def hf_cache_dir() -> Path:
    return runtime_root() / "hf-cache"


def host_python_candidates() -> list[str]:
    candidates: list[str] = []
    wd = copaw_working_dir()
    if os.name == "nt":
        copaw_python = wd / "venv" / "Scripts" / "python.exe"
    else:
        copaw_python = wd / "venv" / "bin" / "python"
    if copaw_python.exists():
        candidates.append(str(copaw_python))
    candidates.append(sys.executable)
    for name in ("python3.12", "python3.11", "python3.10", "python3", "python"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(resolved)
    unique: list[str] = []
    seen = set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def pick_host_python() -> str:
    for candidate in host_python_candidates():
        try:
            out = subprocess.check_output(
                [
                    candidate,
                    "-c",
                    (
                        "import sys, json; "
                        "print(json.dumps({'major': sys.version_info[0], "
                        "'minor': sys.version_info[1]}))"
                    ),
                ],
                text=True,
            ).strip()
            info = json.loads(out)
            if (info["major"], info["minor"]) >= (3, 9):
                return candidate
        except Exception:
            continue
    raise RuntimeError("No usable Python >= 3.9 interpreter found for bootstrap")


def pip_install_args() -> list[str]:
    return [
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--upgrade",
        "pip",
        "setuptools",
        "wheel",
    ]


def _subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("HF_HOME", str(hf_cache_dir()))
    env.pop("TRANSFORMERS_CACHE", None)
    env.setdefault("HF_HUB_DISABLE_XET", "1")
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    if extra:
        env.update(extra)
    return env


def ensure_runtime(force_reinstall: bool = False) -> None:
    rt_root = runtime_root()
    rt_root.mkdir(parents=True, exist_ok=True)
    hf_cache_dir().mkdir(parents=True, exist_ok=True)
    if force_reinstall and venv_dir().exists():
        shutil.rmtree(venv_dir())
    if runtime_python().exists():
        return
    host_python = pick_host_python()
    subprocess.run(
        [host_python, "-m", "venv", str(venv_dir())],
        check=True,
    )
    subprocess.run(
        [str(runtime_python()), *pip_install_args()],
        check=True,
        env=_subprocess_env(),
    )
    subprocess.run(
        [
            str(runtime_python()),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(skill_root() / "requirements.lock.txt"),
        ],
        check=True,
        env=_subprocess_env(),
    )


def is_runtime_process() -> bool:
    return os.environ.get(RUNTIME_ACTIVE_ENV) == "1"


def reexec_into_runtime(force_reinstall: bool = False) -> None:
    if is_runtime_process() and Path(sys.executable).resolve() == runtime_python().resolve():
        return
    ensure_runtime(force_reinstall=force_reinstall)
    env = _subprocess_env(
        {
            RUNTIME_ACTIVE_ENV: "1",
            "ZH_COMM_EVOLVER_RUNTIME_ROOT": str(runtime_root()),
        },
    )
    result = subprocess.run(
        [str(runtime_python()), *sys.argv],
        env=env,
    )
    raise SystemExit(result.returncode)


def download_model_snapshot(
    model_id: str = DEFAULT_MODEL_ID,
    force: bool = False,
) -> Path:
    from huggingface_hub import snapshot_download

    target_dir = model_dir()
    if force and target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    marker = target_dir / ".download-complete.json"
    if marker.exists():
        return target_dir

    snapshot_download(
        repo_id=model_id,
        local_dir=str(target_dir),
        allow_patterns=[
            "*.json",
            "*.txt",
            "*.model",
            "*.py",
            "*.safetensors",
            "tokenizer*",
            "sentencepiece*",
            "special_tokens_map*",
            "vocab*",
        ],
    )
    marker.write_text(
        json.dumps({"model_id": model_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target_dir


def runtime_summary() -> dict[str, str]:
    return {
        "skill_name": SKILL_NAME,
        "model_id": DEFAULT_MODEL_ID,
        "copaw_working_dir": str(copaw_working_dir()),
        "runtime_root": str(runtime_root()),
        "venv_python": str(runtime_python()),
        "model_dir": str(model_dir()),
        "platform": platform.platform(),
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_command(command: Iterable[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(list(command), check=True, env=_subprocess_env(env))
