"""Claude Code permission rules for the AI crew's working folders.

Switch runs each agent as Claude Code in a folder of its own, and Claude Code
reads `.claude/settings.json` in that folder. These rules are what stops the
agents from touching GitHub or Steward's secrets, enforced by Claude Code
itself rather than asked for in a prompt:

- allow: the `steward` command, and reading the project's code
- deny:  gh, git push and commit, curl, wget, the Keychain, deleting files,
         opening Steward's database directly, and reading the Ledger's files
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config

DENY = [
    "Bash(gh:*)",
    "Bash(git push:*)",
    "Bash(git commit:*)",
    "Bash(git remote:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(security:*)",
    "Bash(rm:*)",
    "Bash(sqlite3:*)",
    "Bash(steward-ledger:*)",
    "Bash(~/.steward/bin/steward-ledger:*)",
    "Read(~/.steward/ledger.json)",
    "Read(~/.steward/github-token)",
    "Read(~/.steward/steward.db*)",
    "Read(~/.ssh/**)",
    "Read(~/.config/gh/**)",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
]


def rules(home: Path | None = None) -> dict:
    home = home or Path.home()
    steward_home = config.home()
    steward_bin = steward_home / "bin" / "steward"
    project = steward_home / "project"
    return {
        "permissions": {
            "allow": [
                "Bash(~/.steward/bin/steward:*)",
                f"Bash({steward_bin}:*)",
                "Read(~/.steward/project/**)",
                f"Read({project}/**)",
            ],
            "deny": DENY,
            "additionalDirectories": [str(project)],
        }
    }


def agent_dirs(names: list[str], root: Path | None = None) -> dict[str, list[Path]]:
    """Each crew agent's working folder(s) under Switch Console's agents folder."""
    root = root or Path.home() / ".switch" / "agents"
    found: dict[str, list[Path]] = {name: [] for name in names}
    if not root.exists():
        return found
    for name in names:
        found[name] = sorted(p for p in root.glob(f"*/{name}") if p.is_dir())
    return found


def write_settings(directory: Path) -> Path:
    path = directory / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except ValueError:
            current = {}
    current["permissions"] = rules()["permissions"]
    path.write_text(json.dumps(current, indent=2) + "\n")
    return path
