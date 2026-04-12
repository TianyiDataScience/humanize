from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def parse_frontmatter_name(skill_md: str) -> str:
    if not skill_md.startswith("---"):
        raise ValueError("SKILL.md is missing YAML frontmatter")
    parts = skill_md.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md frontmatter is malformed")
    frontmatter_text = parts[1]
    for line in frontmatter_text.splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    raise ValueError("SKILL.md frontmatter does not define name")


def skill_name() -> str:
    name = parse_frontmatter_name(
        (repo_root() / "SKILL.md").read_text(encoding="utf-8"),
    )
    if not name:
        raise ValueError("SKILL.md must define a non-empty frontmatter name")
    return name


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns(
        ".git",
        ".DS_Store",
        "__pycache__",
        "*.pyc",
        "*.pyo",
        ".pytest_cache",
        "runs",
    )
    shutil.copytree(src, dst, ignore=ignore)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install this skill into local CoPaw.")
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--enable", action="store_true")
    parser.add_argument(
        "--copaw-working-dir",
        type=Path,
        default=Path.home() / ".copaw",
    )
    args = parser.parse_args()

    root = repo_root()
    name = skill_name()
    wd = args.copaw_working_dir.expanduser().resolve()
    pool_dir = wd / "skill_pool" / name
    workspace_dir = wd / "workspaces" / args.workspace
    workspace_skill_dir = workspace_dir / "skills" / name

    copy_tree(root, pool_dir)
    copy_tree(root, workspace_skill_dir)

    if shutil.which("python3"):
        host_python = shutil.which("python3")
    else:
        host_python = shutil.which("python")
    if not host_python:
        raise RuntimeError("No python interpreter found for local install")

    copaw_python = wd / "venv" / "bin" / "python"
    if not copaw_python.exists():
        raise FileNotFoundError(f"CoPaw venv python not found: {copaw_python}")

    reconcile_script = f"""
from pathlib import Path
import json
from copaw.agents.skills_manager import reconcile_pool_manifest, reconcile_workspace_manifest, read_skill_manifest
wd = Path({json.dumps(str(wd))})
workspace_dir = Path({json.dumps(str(workspace_dir))})
name = {json.dumps(name)}
pool_manifest = reconcile_pool_manifest()
workspace_manifest = reconcile_workspace_manifest(workspace_dir)
path = workspace_dir / "skill.json"
payload = json.loads(path.read_text(encoding="utf-8"))
entry = payload["skills"].setdefault(name, {{}}
)
entry.setdefault("channels", ["all"])
entry["enabled"] = {repr(bool(args.enable))}
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({{
    "pool_contains_skill": name in pool_manifest.get("skills", {{}}),
    "workspace_enabled": payload["skills"].get(name, {{}}).get("enabled", False)
}}, ensure_ascii=False))
"""
    result = subprocess.check_output(
        [str(copaw_python), "-c", reconcile_script],
        text=True,
    )
    info = json.loads(result.strip())

    print(
        json.dumps(
            {
                "skill_name": name,
                "pool_dir": str(pool_dir),
                "workspace_skill_dir": str(workspace_skill_dir),
                "enabled": bool(info.get("workspace_enabled", False)),
                "pool_manifest_contains_skill": bool(info.get("pool_contains_skill", False)),
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


if __name__ == "__main__":
    main()
