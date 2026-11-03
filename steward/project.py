"""A read-only copy of the project's code, for the agents to read.

The Ledger keeps it current. The agents read it with their Read and Grep
tools, or with `steward code`. The GitHub token is passed to git for one
command at a time and never written into the checkout, so an agent reading
`.git/config` finds nothing to borrow.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from . import config


def code_dir() -> Path:
    return config.home() / "project"


def _auth(token: str) -> list[str]:
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


def _git(args: list[str], token: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(["git", *_auth(token), *args], capture_output=True, text=True, timeout=120)


def checkout(repo: str, token: str = "", dest: Path | None = None) -> str:
    """Clone the repository, or bring the copy up to date. Returns what happened."""
    dest = dest or code_dir()
    if not repo:
        return "no repository in policy.yaml"
    url = f"https://github.com/{repo}.git"
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = _git(["clone", "--depth", "1", "--quiet", url, str(dest)], token)
        if out.returncode != 0:
            return f"could not clone {repo}: {_clean(out.stderr, token)}"
        return f"cloned {repo} into {dest}"
    out = _git(["-C", str(dest), "fetch", "--depth", "1", "--quiet", "origin"], token)
    if out.returncode != 0:
        return f"could not update {repo}: {_clean(out.stderr, token)}"
    out = _git(["-C", str(dest), "reset", "--hard", "--quiet", "FETCH_HEAD"])
    if out.returncode != 0:
        return f"could not update {repo}: {_clean(out.stderr, token)}"
    return f"updated {repo}"


def _clean(text: str, token: str) -> str:
    text = (text or "").strip().splitlines()[-1:] or ["unknown error"]
    line = text[0]
    return line.replace(token, "***") if token else line
