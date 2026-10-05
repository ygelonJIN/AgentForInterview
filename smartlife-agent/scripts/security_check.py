"""扫描当前工作树和 Git 历史中的疑似密钥，不输出密钥内容。"""
import json
import re
import subprocess
import sys
from pathlib import Path


SECRET = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{20,}|(?:api[_-]?key|authorization|password|token)"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9_./+-]{24,})"
)
SKIP_DIRS = {".git", "venv312", "__pycache__", "chroma_db", "memory_db"}
IGNORED_SECRET_FILES = {
    "smartlife-agent/data/config.json",
    "smartlife-agent/.env",
}


def scan_worktree(root: Path) -> list[str]:
    hits = []
    tracked = subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()
    for relative in tracked:
        path = root / relative
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if SECRET.search(text):
            hits.append(str(path.relative_to(root)))
    return hits


def scan_ignored_local(root: Path) -> list[str]:
    hits = []
    for relative in IGNORED_SECRET_FILES:
        path = root / relative
        if path.is_file() and SECRET.search(path.read_text(encoding="utf-8", errors="ignore")):
            hits.append(relative)
    return hits


def scan_history(root: Path) -> list[str]:
    commits = subprocess.check_output(
        ["git", "log", "--format=%H", "--all"],
        cwd=root,
        text=True,
    ).splitlines()
    hits = []
    for commit in commits:
        for path in ("smartlife-agent/data/config.json", "smartlife-agent/.env"):
            try:
                raw = subprocess.check_output(
                    ["git", "show", f"{commit}:{path}"],
                    cwd=root,
                    stderr=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError:
                continue
            if SECRET.search(raw.decode("utf-8", errors="ignore")):
                hits.append(commit[:12])
                break
    return hits


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    current = scan_worktree(root)
    ignored_local = scan_ignored_local(root)
    history = scan_history(root)
    result = {
        "tracked_secret_like_files": current,
        "ignored_local_secret_files": ignored_local,
        "history_commits_with_secret_like_config": history,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if current or history else 0


if __name__ == "__main__":
    sys.exit(main())
