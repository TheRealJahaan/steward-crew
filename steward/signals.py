"""Plain rules that turn GitHub activity into facts the crew can talk about.

Nothing here is AI. A claim is a comment that matches a pattern; a quiet claim
is one with no activity for a set number of hours; a pull request check is a
list of signals, each with the evidence behind it. The agents explain these
facts to people; they do not invent them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import Policy

CLAIM_PATTERNS = [
    r"\bcan i (?:please )?(?:work on|take|pick|grab|try|have)\b",
    r"\bcould i (?:work on|take|pick|try)\b",
    r"\bmay i (?:work on|take|pick)\b",
    r"\bi(?:'d| would) (?:like|love) to (?:work on|take|pick|try|fix|contribute)",
    r"\bi want to (?:work on|take|fix|contribute)",
    r"\bassign (?:this |it )?(?:issue )?to me\b",
    r"\bplease assign me\b",
    r"\bi(?:'ll| will) (?:take|work on|pick up|fix) (?:this|it)\b",
    r"\bworking on (?:this|it)\b",
    r"\bclaim(?:ing)? (?:this|it)\b",
]
_CLAIM_RE = re.compile("|".join(CLAIM_PATTERNS), re.IGNORECASE)

_RELEASE_RE = re.compile(
    r"\b(?:i can(?:'t|not) (?:work on|continue|finish)|unassign me|no longer (?:working|able)|"
    r"someone else can (?:take|have)|dropping (?:this|it))\b",
    re.IGNORECASE,
)

_LINKED_ISSUE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)", re.IGNORECASE
)
_ANY_ISSUE_REF_RE = re.compile(r"(?<![\w/])#(\d+)\b")

# Phrases that often appear in pull request text pasted from a chatbot without
# being read. Each one alone means little; they are signals for a person.
_BOILERPLATE = [
    "as an ai",
    "i hope this helps",
    "certainly!",
    "here is the updated",
    "here's the updated",
    "this pull request enhances",
    "this pr enhances",
    "comprehensive solution",
    "robust and scalable",
    "seamless",
    "delve",
    "in conclusion",
    "improves overall code quality",
    "best practices",
]

DOC_SUFFIXES = (".md", ".rst", ".txt")


def is_claim(text: str) -> bool:
    return bool(_CLAIM_RE.search(text or ""))


def is_release(text: str) -> bool:
    return bool(_RELEASE_RE.search(text or ""))


def linked_issues(text: str) -> list[int]:
    linked = [int(n) for n in _LINKED_ISSUE_RE.findall(text or "")]
    return sorted(set(linked))


def referenced_issues(text: str) -> list[int]:
    return sorted({int(n) for n in _ANY_ISSUE_REF_RE.findall(text or "")})


def hours_since(ts: float, now: float) -> float:
    return max(0.0, (now - ts) / 3600.0)


def quiet_claims(claims: list[dict[str, Any]], policy: Policy, now: float) -> list[dict[str, Any]]:
    out = []
    for claim in claims:
        if claim["status"] not in ("open", "assigned"):
            continue
        idle = hours_since(claim["last_activity"], now)
        if idle >= policy.quiet_hours:
            out.append({**claim, "idle_hours": round(idle, 1)})
    return out


def contested_issues(claims: list[dict[str, Any]]) -> dict[int, list[str]]:
    by_issue: dict[int, list[str]] = {}
    for claim in claims:
        if claim["status"] in ("open", "assigned"):
            by_issue.setdefault(claim["issue"], []).append(claim["login"])
    return {issue: logins for issue, logins in by_issue.items() if len(logins) > 1}


@dataclass
class Signal:
    level: str  # "info", "warn" or "flag"
    code: str
    message: str


@dataclass
class PrCheck:
    number: int
    verdict: str
    signals: list[Signal] = field(default_factory=list)

    def as_text(self) -> str:
        lines = [f"PR #{self.number}: {self.verdict}"]
        for s in self.signals:
            lines.append(f"  [{s.level}] {s.code}: {s.message}")
        return "\n".join(lines)


def check_pr(
    pr: dict[str, Any],
    *,
    policy: Policy,
    issues: dict[int, dict[str, Any]],
    open_prs: list[dict[str, Any]],
    contributor: dict[str, Any] | None,
    now: float,
) -> PrCheck:
    """Collect the facts a maintainer would want before reading a pull request."""
    signals: list[Signal] = []
    text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
    files = pr.get("files") or []
    filenames = [f.get("filename", "") for f in files]
    changed = int(pr.get("additions", 0)) + int(pr.get("deletions", 0))

    linked = linked_issues(text)
    if not linked:
        refs = referenced_issues(text)
        if refs:
            signals.append(Signal("warn", "weak-link", f"mentions #{', #'.join(map(str, refs))} but never says 'Fixes #n'"))
        else:
            signals.append(Signal("flag", "no-linked-issue", "does not link any issue (CONTRIBUTING asks for 'Fixes #n')"))

    for number in linked:
        issue = issues.get(number)
        if issue is None:
            signals.append(Signal("warn", "unknown-issue", f"links #{number}, which Steward does not know"))
            continue
        assignees = [a.lower() for a in issue.get("assignees") or []]
        if assignees and pr["author"].lower() not in assignees:
            signals.append(
                Signal("flag", "assigned-to-someone-else", f"#{number} is assigned to @{', @'.join(issue['assignees'])}")
            )
        if "good first issue" in (issue.get("labels") or []) and changed > policy.large_change_lines:
            signals.append(
                Signal("warn", "large-for-first-issue", f"{changed} lines changed for a good first issue")
            )
        others = [
            p for p in open_prs
            if p["number"] != pr["number"] and number in linked_issues(f"{p.get('title', '')}\n{p.get('body', '')}")
        ]
        if others:
            names = ", ".join(f"#{p['number']} by @{p['author']}" for p in others)
            signals.append(Signal("warn", "duplicate", f"other open PRs for #{number}: {names}"))

    code_files = [f for f in filenames if not f.endswith(DOC_SUFFIXES)]
    test_files = [f for f in filenames if f.startswith("tests/") or "/test_" in f or f.startswith("test_")]
    if code_files and not test_files:
        signals.append(Signal("warn", "no-tests", "changes code but no tests"))
    if filenames and not code_files and any(w in text.lower() for w in ("fix", "bug")):
        signals.append(Signal("warn", "docs-only-fix", "says it fixes a bug but only touches docs"))

    lowered = text.lower()
    hits = [phrase for phrase in _BOILERPLATE if phrase in lowered]
    if len(hits) >= 2:
        signals.append(Signal("flag", "boilerplate", f"chatbot-style phrases: {', '.join(hits[:4])}"))
    elif hits:
        signals.append(Signal("info", "boilerplate", f"phrase: {hits[0]}"))

    if contributor:
        created = contributor.get("account_created_at")
        if created and (now - created) / 86400.0 < policy.new_account_days:
            days = int((now - created) / 86400.0)
            signals.append(Signal("info", "new-account", f"GitHub account is {days} days old"))
        if not contributor.get("merged_prs"):
            signals.append(Signal("info", "first-contribution", "no merged PRs here yet; a warm review helps"))

    flags = sum(1 for s in signals if s.level == "flag")
    warns = sum(1 for s in signals if s.level == "warn")
    if flags >= 2:
        verdict = "likely spam or unreviewed AI output: a maintainer should decide"
    elif flags or warns >= 2:
        verdict = "needs a careful look"
    else:
        verdict = "looks ordinary"
    return PrCheck(number=pr["number"], verdict=verdict, signals=signals)
