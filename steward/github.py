"""A small GitHub REST client. Only the Ledger creates one.

The agents never talk to GitHub: they read what the Ledger synced into
Steward's database, and they ask for changes by filing proposals.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

API = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status}: {message}")
        self.status = status


def parse_time(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class GitHub:
    def __init__(self, token: str, repo: str, api: str = API):
        if "/" not in repo:
            raise ValueError("repo must look like owner/name")
        self.token = token
        self.repo = repo
        self.api = api.rstrip("/")

    # ── transport ────────────────────────────────────────────────────────────

    def request(self, method: str, path: str, body: Any = None, params: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.api}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "steward-crew")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:300]
            raise GitHubError(err.code, detail) from err

    def paged(self, path: str, params: dict[str, Any] | None = None, limit: int = 500) -> list[Any]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out: list[Any] = []
        page = 1
        while len(out) < limit:
            params["page"] = page
            batch = self.request("GET", path, params=params)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < params["per_page"]:
                break
            page += 1
        return out[:limit]

    def _r(self, suffix: str) -> str:
        return f"/repos/{self.repo}{suffix}"

    # ── reads ────────────────────────────────────────────────────────────────

    def issues(self) -> list[dict[str, Any]]:
        raw = self.paged(self._r("/issues"), {"state": "open"})
        out = []
        for item in raw:
            if "pull_request" in item:
                continue
            out.append(
                {
                    "number": item["number"],
                    "title": item["title"],
                    "body": item.get("body") or "",
                    "labels": [label["name"] for label in item.get("labels", [])],
                    "state": item["state"],
                    "assignees": [a["login"] for a in item.get("assignees", [])],
                    "created_at": parse_time(item.get("created_at")),
                    "updated_at": parse_time(item.get("updated_at")),
                    "url": item.get("html_url", ""),
                }
            )
        return out

    def issue_comments(self, since: str | None = None) -> list[dict[str, Any]]:
        params = {"sort": "created", "direction": "asc"}
        if since:
            params["since"] = since
        raw = self.paged(self._r("/issues/comments"), params)
        out = []
        for item in raw:
            number = int(item["issue_url"].rstrip("/").rsplit("/", 1)[1])
            out.append(
                {
                    "id": item["id"],
                    "issue": number,
                    "login": item["user"]["login"],
                    "body": item.get("body") or "",
                    "created_at": parse_time(item.get("created_at")),
                    "is_bot": item["user"].get("type") == "Bot",
                }
            )
        return out

    def pulls(self, state: str = "open") -> list[dict[str, Any]]:
        raw = self.paged(self._r("/pulls"), {"state": state})
        return [
            {
                "number": item["number"],
                "title": item["title"],
                "body": item.get("body") or "",
                "author": item["user"]["login"],
                "state": "merged" if item.get("merged_at") else item["state"],
                "created_at": parse_time(item.get("created_at")),
                "updated_at": parse_time(item.get("updated_at")),
                "url": item.get("html_url", ""),
            }
            for item in raw
        ]

    def pull_files(self, number: int) -> list[dict[str, Any]]:
        raw = self.paged(self._r(f"/pulls/{number}/files"), limit=100)
        return [
            {
                "filename": f["filename"],
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "patch": (f.get("patch") or "")[:6000],
            }
            for f in raw
        ]

    def user_created_at(self, login: str) -> float | None:
        try:
            return parse_time(self.request("GET", f"/users/{login}").get("created_at"))
        except GitHubError:
            return None

    # ── writes (Ledger only, after a maintainer approves) ────────────────────

    def add_assignee(self, number: int, login: str) -> None:
        self.request("POST", self._r(f"/issues/{number}/assignees"), {"assignees": [login]})

    def remove_assignee(self, number: int, login: str) -> None:
        self.request("DELETE", self._r(f"/issues/{number}/assignees"), {"assignees": [login]})

    def comment(self, number: int, body: str) -> int:
        return int(self.request("POST", self._r(f"/issues/{number}/comments"), {"body": body})["id"])

    def delete_comment(self, comment_id: int) -> None:
        self.request("DELETE", self._r(f"/issues/comments/{comment_id}"))

    def add_label(self, number: int, label: str) -> None:
        self.request("POST", self._r(f"/issues/{number}/labels"), {"labels": [label]})

    def remove_label(self, number: int, label: str) -> None:
        self.request("DELETE", self._r(f"/issues/{number}/labels/{urllib.parse.quote(label)}"))

    def set_state(self, number: int, state: str) -> None:
        self.request("PATCH", self._r(f"/issues/{number}"), {"state": state})
