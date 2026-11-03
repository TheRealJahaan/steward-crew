"""`steward`: the command the AI agents use to see the project and to propose.

Every command here reads Steward's database, except `propose`, which adds a
proposal for a maintainer to decide on. Nothing here can change GitHub: there
is no token in this process, and no code path that would use one.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, db, signals
from .config import load_policy


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "unknown"
    hours = (now - ts) / 3600.0
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def _demo(row: dict[str, Any]) -> str:
    return " [demo]" if row.get("source") == "demo" else ""


def _issues_map(conn) -> dict[int, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM issues").fetchall()
    return {r["number"]: db.row_dict(r) for r in rows}


def _check(conn, pr: dict[str, Any], policy, now: float) -> signals.PrCheck:
    return signals.check_pr(
        pr,
        policy=policy,
        issues=_issues_map(conn),
        open_prs=db.open_prs(conn),
        contributor=db.get_contributor(conn, pr["author"]),
        now=now,
    )


# ── commands ──────────────────────────────────────────────────────────────────


def cmd_status(args, conn, policy) -> int:
    now = db.now()
    open_issues = conn.execute("SELECT COUNT(*) FROM issues WHERE state = 'open'").fetchone()[0]
    active = db.claims(conn)
    quiet = signals.quiet_claims(active, policy, now)
    contested = signals.contested_issues(active)
    prs = db.open_prs(conn)
    pending = db.pending_proposals(conn)
    last = db.get_meta(conn, "last_sync")
    synced = _ago(json.loads(last)["at"], now) if last else "never"
    print(f"Repository: {policy.repo}  (last GitHub sync: {synced})")
    print(f"Open issues: {open_issues}")
    print(f"Active claims: {len(active)}  quiet: {len(quiet)}  contested issues: {len(contested)}")
    print(f"Open pull requests: {len(prs)}")
    print(f"Proposals waiting for a maintainer: {len(pending)}")
    return 0


def cmd_issues(args, conn, policy) -> int:
    rows = [db.row_dict(r) for r in conn.execute("SELECT * FROM issues WHERE state = 'open' ORDER BY number")]
    active = db.claims(conn)
    claimed = {}
    for c in active:
        claimed.setdefault(c["issue"], []).append(c["login"])
    shown = 0
    for issue in rows:
        labels = issue.get("labels") or []
        if args.good_first and "good first issue" not in labels:
            continue
        taken = issue.get("assignees") or claimed.get(issue["number"])
        if args.free and taken:
            continue
        who = ""
        if issue.get("assignees"):
            who = f"  assigned: @{', @'.join(issue['assignees'])}"
        elif claimed.get(issue["number"]):
            who = f"  claimed by: @{', @'.join(claimed[issue['number']])} (not assigned yet)"
        print(f"#{issue['number']:<3} {issue['title']}  [{', '.join(labels)}]{who}{_demo(issue)}")
        shown += 1
    if not shown:
        print("No issues match.")
    return 0


def cmd_issue(args, conn, policy) -> int:
    issue = db.get_issue(conn, args.number)
    if not issue:
        print(f"No issue #{args.number} in Steward's database. It may be closed or not synced yet.")
        return 1
    now = db.now()
    print(f"#{issue['number']} {issue['title']}{_demo(issue)}")
    print(f"State: {issue['state']}  Labels: {', '.join(issue.get('labels') or []) or 'none'}")
    print(f"Assigned: {', '.join('@' + a for a in issue.get('assignees') or []) or 'nobody'}")
    print(f"URL: {issue.get('url') or '-'}")
    print()
    print(textwrap.indent(issue.get("body") or "(no description)", "  "))
    claims = db.claims(conn, issue=args.number, active_only=False)
    if claims:
        print("\nClaims:")
        for c in claims:
            print(
                f"  @{c['login']}: {c['status']}, claimed {_ago(c['claimed_at'], now)},"
                f" last activity {_ago(c['last_activity'], now)} (via {c['via']}){_demo(c)}"
            )
    comments = conn.execute(
        "SELECT * FROM comments WHERE issue = ? ORDER BY created_at DESC LIMIT 8", (args.number,)
    ).fetchall()
    if comments:
        print("\nRecent comments (newest first):")
        for c in comments:
            body = " ".join((c["body"] or "").split())[:200]
            print(f"  @{c['login']} ({_ago(c['created_at'], now)}): {body}")
    return 0


def cmd_claims(args, conn, policy) -> int:
    now = db.now()
    active = db.claims(conn)
    if args.quiet:
        rows = signals.quiet_claims(active, policy, now)
        if not rows:
            print(f"No claims have been quiet for {policy.quiet_hours:g} hours or more.")
        for c in rows:
            print(f"#{c['issue']:<3} @{c['login']}: {c['status']}, quiet for {c['idle_hours']}h{_demo(c)}")
        return 0
    if args.contested:
        contested = signals.contested_issues(active)
        if not contested:
            print("No issue has more than one active claim.")
        for issue, logins in sorted(contested.items()):
            print(f"#{issue:<3} claimed by @{', @'.join(logins)}")
        return 0
    if not active:
        print("No active claims.")
    counts: dict[str, int] = {}
    for c in active:
        counts[c["login"]] = counts.get(c["login"], 0) + 1
    for c in active:
        extra = f"  (has {counts[c['login']]} open claims)" if counts[c["login"]] > policy.max_open_claims else ""
        print(
            f"#{c['issue']:<3} @{c['login']}: {c['status']}, claimed {_ago(c['claimed_at'], now)},"
            f" last activity {_ago(c['last_activity'], now)}{extra}{_demo(c)}"
        )
    return 0


def cmd_prs(args, conn, policy) -> int:
    now = db.now()
    prs = db.open_prs(conn)
    if not prs:
        print("No open pull requests.")
    for pr in prs:
        check = _check(conn, pr, policy, now)
        print(
            f"#{pr['number']:<3} {pr['title']}  by @{pr['author']}, +{pr['additions']}/-{pr['deletions']}"
            f"  -> {check.verdict}{_demo(pr)}"
        )
    return 0


def cmd_pr(args, conn, policy) -> int:
    pr = db.get_pr(conn, args.number)
    if not pr:
        print(f"No pull request #{args.number} in Steward's database.")
        return 1
    now = db.now()
    print(f"PR #{pr['number']} {pr['title']}{_demo(pr)}")
    print(f"Author: @{pr['author']}  State: {pr['state']}  +{pr['additions']}/-{pr['deletions']}")
    print(f"URL: {pr.get('url') or '-'}")
    print("\nDescription:")
    print(textwrap.indent(pr.get("body") or "(empty)", "  "))
    print("\nChecks (plain rules, not AI):")
    print(textwrap.indent(_check(conn, pr, policy, now).as_text(), "  "))
    print("\nFiles:")
    for f in pr.get("files") or []:
        print(f"  {f.get('filename')} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})")
        if args.diff and f.get("patch"):
            print(textwrap.indent(f["patch"], "    | "))
    if not args.diff:
        print("\n(Add --diff to see the changes.)")
    return 0


def cmd_contributor(args, conn, policy) -> int:
    login = args.login.lstrip("@")
    person = db.get_contributor(conn, login)
    now = db.now()
    if not person:
        print(f"Steward has not seen @{login} yet.")
        return 1
    created = person.get("account_created_at")
    age = f"{(now - created) / 86400:.0f} days" if created else "unknown"
    print(f"@{login}{_demo(person)}: GitHub account age {age}, merged PRs here: {person.get('merged_prs') or 0}")
    for c in db.claims(conn, active_only=False):
        if c["login"].lower() == login.lower():
            print(f"  claim #{c['issue']}: {c['status']}, last activity {_ago(c['last_activity'], now)}")
    for pr in conn.execute("SELECT number, title, state FROM prs WHERE lower(author) = lower(?)", (login,)):
        print(f"  PR #{pr['number']} {pr['title']} ({pr['state']})")
    return 0


def cmd_digest(args, conn, policy) -> int:
    """Everything Steward needs for the daily summary, as plain facts."""
    now = db.now()
    since = float(db.get_meta(conn, "digest_since", "0") or 0) or now - 86400
    active = db.claims(conn)
    new_claims = [c for c in active if c["claimed_at"] >= since]
    quiet = signals.quiet_claims(active, policy, now)
    contested = signals.contested_issues(active)
    counts: dict[str, int] = {}
    for c in active:
        counts[c["login"]] = counts.get(c["login"], 0) + 1
    over = {login: n for login, n in counts.items() if n > policy.max_open_claims}
    print(f"Daily facts for {policy.repo}, since {datetime.fromtimestamp(since):%Y-%m-%d %H:%M}")
    print(f"\nNew claims ({len(new_claims)}):")
    for c in new_claims:
        assigned = db.get_issue(conn, c["issue"]) or {}
        state = "assigned" if assigned.get("assignees") else "waiting for a maintainer"
        print(f"  #{c['issue']} @{c['login']} ({state}){_demo(c)}")
    print(f"\nContested issues ({len(contested)}):")
    for issue, logins in sorted(contested.items()):
        print(f"  #{issue}: @{', @'.join(logins)}")
    print(f"\nQuiet claims, no activity for {policy.quiet_hours:g}h+ ({len(quiet)}):")
    for c in quiet:
        print(f"  #{c['issue']} @{c['login']}, quiet {c['idle_hours']}h{_demo(c)}")
    print(f"\nPeople over the open-claim limit of {policy.max_open_claims} ({len(over)}):")
    for login, n in sorted(over.items()):
        print(f"  @{login}: {n} open claims")
    print("\nOpen pull requests:")
    prs = db.open_prs(conn)
    for pr in prs:
        check = _check(conn, pr, policy, now)
        codes = ", ".join(s.code for s in check.signals if s.level in ("flag", "warn")) or "none"
        print(f"  #{pr['number']} by @{pr['author']}: {check.verdict} (signals: {codes}){_demo(pr)}")
    if not prs:
        print("  none")
    pending = db.pending_proposals(conn)
    print(f"\nProposals already waiting for a maintainer ({len(pending)}):")
    for p in pending:
        print(f"  [P{p['id']}] {describe(p)}")
    if args.mark:
        db.set_meta(conn, "digest_since", str(now))
    return 0


def describe(p: dict[str, Any]) -> str:
    who = f"@{p['login']}" if p.get("login") else ""
    kind, target = p["kind"], p["target"]
    if kind == "assign":
        return f"Assign #{target} to {who}"
    if kind == "release":
        return f"Release {who}'s claim on #{target}"
    if kind == "comment":
        return f"Comment on #{target}" + (f" for {who}" if who else "")
    if kind == "label":
        return f"Label #{target} as '{p.get('text')}'"
    if kind == "review":
        return f"Post a review on PR #{target}"
    if kind == "close":
        return f"Close PR #{target}"
    return f"{kind} #{target}"


def cmd_propose(args, conn, policy) -> int:
    text = args.text or ""
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    if args.kind in ("assign", "release") and not args.login:
        print("assign and release need --login <github username>.", file=sys.stderr)
        return 2
    if args.kind in ("comment", "review", "close", "release", "label") and not text.strip():
        print(f"{args.kind} needs --text (what will be posted, or the label name).", file=sys.stderr)
        return 2
    if not args.reason.strip():
        print("Give a --reason a maintainer can check.", file=sys.stderr)
        return 2
    issue = db.get_issue(conn, args.target)
    pr = db.get_pr(conn, args.target)
    if not issue and not pr:
        print(f"#{args.target} is not an issue or pull request Steward knows.", file=sys.stderr)
        return 1
    pid = db.add_proposal(
        conn,
        args.kind,
        args.target,
        login=args.login or "",
        text=text.strip(),
        reason=args.reason.strip(),
        proposed_by=args.by or "agent",
    )
    proposal = db.get_proposal(conn, pid)
    print(f"P{pid}")
    print(f"Filed [P{pid}] {describe(proposal)}. The Ledger will post it for a maintainer to decide.")
    if proposal.get("source") == "demo":
        print("It involves demo data, so approving it is simulated: nothing is sent to GitHub.")
    return 0


def cmd_queue(args, conn, policy) -> int:
    pending = db.pending_proposals(conn)
    if not pending:
        print("Nothing is waiting for a maintainer.")
    for p in pending:
        print(f"[P{p['id']}] {describe(p)}  (by {p['proposed_by']}): {p['reason']}")
    return 0


def cmd_log(args, conn, policy) -> int:
    for row in reversed(db.recent_audit(conn, args.limit)):
        when = datetime.fromtimestamp(row["at"]).strftime("%m-%d %H:%M")
        ref = f" [P{row['proposal_id']}]" if row["proposal_id"] else ""
        print(f"{when} {row['actor']}: {row['action']} {row['target']}{ref} {row['detail']}".rstrip())
    return 0


def cmd_code(args, conn, policy) -> int:
    """The project's code, for agents: where it is, one file, or a search."""
    import subprocess

    from .project import code_dir

    root = code_dir()
    if not (root / ".git").exists():
        print("The code is not checked out yet. A maintainer runs: steward-ledger code")
        return 1
    if args.grep:
        out = subprocess.run(
            ["git", "-C", str(root), "grep", "-n", "-I", "-e", args.grep, "--", *(args.path or [])],
            capture_output=True,
            text=True,
        )
        print(out.stdout.rstrip() or f"No match for {args.grep!r}.")
        return 0
    if not args.path:
        print(f"The code of {policy.repo} is at {root}")
        print("Read files there with your Read and Grep tools, or: steward code <file>, steward code --grep <text>")
        files = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True).stdout
        print(files.rstrip())
        return 0
    for rel in args.path:
        target = (root / rel).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            print(f"{rel}: no such file in the project")
            continue
        print(f"── {rel}")
        for n, line in enumerate(target.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            print(f"{n:4} {line}")
    return 0


def cmd_demo(args, conn, policy) -> int:
    from . import demo

    if args.action == "reset":
        n = demo.reset(conn)
        print(f"Removed {n} demo records. Real GitHub data is untouched.")
        return 0
    path = Path(args.file) if args.file else None
    return demo.play(conn, path=path, speed=args.speed, quiet=args.silent)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="steward",
        description="Read the project and file proposals. Only a maintainer's approval makes anything happen.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="one-screen overview").set_defaults(func=cmd_status)

    s = sub.add_parser("issues", help="open issues with who has claimed them")
    s.add_argument("--free", action="store_true", help="only issues nobody has claimed")
    s.add_argument("--good-first", action="store_true", help="only 'good first issue'")
    s.set_defaults(func=cmd_issues)

    s = sub.add_parser("issue", help="one issue: text, claims, recent comments")
    s.add_argument("number", type=int)
    s.set_defaults(func=cmd_issue)

    s = sub.add_parser("claims", help="who is working on what")
    s.add_argument("--quiet", action="store_true", help="claims with no recent activity")
    s.add_argument("--contested", action="store_true", help="issues claimed by more than one person")
    s.set_defaults(func=cmd_claims)

    sub.add_parser("prs", help="open pull requests with rule-based checks").set_defaults(func=cmd_prs)

    s = sub.add_parser("pr", help="one pull request: description, checks, files")
    s.add_argument("number", type=int)
    s.add_argument("--diff", action="store_true", help="show the changes")
    s.set_defaults(func=cmd_pr)

    s = sub.add_parser("contributor", help="what Steward knows about a person")
    s.add_argument("login")
    s.set_defaults(func=cmd_contributor)

    s = sub.add_parser("digest", help="facts for the daily summary")
    s.add_argument("--mark", action="store_true", help="start the next digest from now")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("propose", help="ask a maintainer to approve an action")
    s.add_argument("kind", choices=db.PROPOSAL_KINDS)
    s.add_argument("target", type=int, help="issue or pull request number")
    s.add_argument("--login", help="GitHub username, for assign and release")
    s.add_argument("--text", help="what will be posted on GitHub (or the label, for label)")
    s.add_argument("--text-file", help="read --text from a file")
    s.add_argument("--reason", required=True, help="why, in one line a maintainer can check")
    s.add_argument("--by", help="your agent name")
    s.set_defaults(func=cmd_propose)

    sub.add_parser("queue", help="proposals waiting for a maintainer").set_defaults(func=cmd_queue)

    s = sub.add_parser("code", help="the project's code: its path, a file, or a search")
    s.add_argument("path", nargs="*", help="files to print, with line numbers")
    s.add_argument("--grep", help="search the code for this text")
    s.set_defaults(func=cmd_code)

    s = sub.add_parser("log", help="what the Ledger has done")
    s.add_argument("--limit", type=int, default=15)
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("demo", help="play or remove the demo contributor flood")
    s.add_argument("action", choices=("flood", "reset"))
    s.add_argument("--file", help="a flood file other than demo/flood.json")
    s.add_argument("--speed", type=float, default=1.0, help="2 plays twice as fast; 0 loads it all at once")
    s.add_argument("--silent", action="store_true")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = load_policy()
    with db.session() as conn:
        return args.func(args, conn, policy)


if __name__ == "__main__":
    raise SystemExit(main())
