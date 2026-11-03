"""The Ledger: the only part of Steward Crew that can change anything on GitHub.

It is not an AI. It sits in the maintainers room as a Switch agent, posts each
proposal the AI agents file, and acts only when a maintainer types `approve`.
Everything it does is written to the audit log and can be undone.

    steward-ledger setup        register with the Switch server on this computer
    steward-ledger rooms        list the rooms the Ledger is in
    steward-ledger say TEXT     post a test message in the maintainers room
    steward-ledger sync         read GitHub once into Steward's database
    steward-ledger code         fetch the project's code for the agents to read
    steward-ledger run          stay in the room and do the job
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import actions, config, db, project
from .cli import describe
from .config import Policy, load_policy
from .github import GitHub, GitHubError
from .switch import Message, SwitchAgent, SwitchError, health, local_api_url, read_console_env, register

log = logging.getLogger("steward.ledger")

KEYCHAIN_SERVICE = "steward-ledger-github"

HELP = """I'm the Ledger. I only act when a maintainer tells me to, and I log everything.
  approve 7          do proposal P7 (several: approve 7 8 10)
  decline 9 <why>    turn P9 down
  edit 8: <text>     change what P8 will post, then approve it
  undo 7             reverse what P7 did
  queue              what is waiting
  log                what I have done lately
  assign 12 @ben     assign directly, no proposal needed
  release 8 @ben     release a claim directly
  sync               read GitHub now"""


# ── credentials ───────────────────────────────────────────────────────────────


def state_path() -> Path:
    return config.home() / "ledger.json"


def load_state() -> dict[str, Any]:
    path = state_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.write_text(json.dumps(state, indent=2))
    os.chmod(path, 0o600)


def github_token() -> str:
    """The write token: from the environment, the macOS Keychain, or a 600 file."""
    token = os.environ.get("STEWARD_GITHUB_TOKEN", "").strip()
    if token:
        return token
    if shutil.which("security"):
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    path = config.home() / "github-token"
    if path.exists():
        return path.read_text().strip()
    return ""


# ── command parsing ───────────────────────────────────────────────────────────

_CMD = re.compile(r"^\s*(?:@?\S*ledger\S*[:,]?\s+)?(?P<verb>[a-z]+)\b(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
_IDS = re.compile(r"\bP?(\d+)\b", re.IGNORECASE)
VERBS = {"approve", "decline", "edit", "undo", "queue", "log", "assign", "release", "sync", "help"}


def parse_command(text: str) -> tuple[str, str] | None:
    text = re.sub(r"<[^>]+>", "", text or "").strip()  # drop chat-app markup such as <@U123>
    text = text.replace("*", "").replace("`", "")
    match = _CMD.match(text)
    if not match:
        return None
    verb = match.group("verb").lower()
    if verb not in VERBS:
        return None
    return verb, match.group("rest").strip()


class Ledger:
    def __init__(self, policy: Policy, agent: SwitchAgent | None, gh: GitHub | None, self_login: str = ""):
        self.policy = policy
        self.agent = agent
        self.gh = gh
        self.self_login = self_login
        self.room_id = ""
        self.my_names = {"ledger"}

    # ── posting ──────────────────────────────────────────────────────────────

    def say(self, text: str) -> None:
        if self.agent and self.room_id:
            self.agent.post(self.room_id, text)
        else:
            print(text)

    def card(self, p: dict[str, Any]) -> str:
        lines = [f"[P{p['id']}] {describe(p)}"]
        if p.get("reason"):
            lines.append(f"Why: {p['reason']}")
        if p.get("text") and p["kind"] != "label":
            preview = p["text"] if len(p["text"]) < 600 else p["text"][:600] + "…"
            lines.append(f"Will post on GitHub:\n> " + preview.replace("\n", "\n> "))
        lines.append(f"Proposed by {p.get('proposed_by') or 'an agent'}. Reply: approve {p['id']} · decline {p['id']} <why> · edit {p['id']}: <text>")
        return "\n".join(lines)

    # ── commands ─────────────────────────────────────────────────────────────

    def handle(self, sender_name: str, text: str) -> str | None:
        """Run one maintainer command. Returns the reply, or None if it was not a command."""
        parsed = parse_command(text)
        if not parsed:
            return None
        verb, rest = parsed
        if not self.policy.is_maintainer(sender_name):
            return f"Sorry {sender_name}, only maintainers listed in policy.yaml can tell me what to do."
        with db.session() as conn:
            if verb == "help":
                return HELP
            if verb == "queue":
                pending = db.pending_proposals(conn)
                if not pending:
                    return "Nothing is waiting for you."
                return "Waiting for you:\n" + "\n".join(f"[P{p['id']}] {describe(p)}: {p['reason']}" for p in pending)
            if verb == "log":
                rows = list(reversed(db.recent_audit(conn, 10)))
                if not rows:
                    return "I have not done anything yet."
                return "Lately:\n" + "\n".join(
                    f"{datetime.fromtimestamp(r['at']):%H:%M} {r['action']} {r['target']}"
                    + (f" [P{r['proposal_id']}]" if r["proposal_id"] else "")
                    + (f" by {r['actor']}" if r["actor"] else "")
                    for r in rows
                )
            if verb == "sync":
                return self.sync_once(conn)
            if verb == "approve":
                ids = [int(i) for i in _IDS.findall(rest)]
                if not ids:
                    return "Which one? For example: approve 7"
                return "\n".join(self.approve(conn, pid, sender_name) for pid in ids)
            if verb == "decline":
                m = _IDS.match(rest)
                if not m:
                    return "Which one? For example: decline 9 not a good fit"
                pid = int(m.group(1))
                why = rest[m.end():].strip(" :-") or "no reason given"
                p = db.get_proposal(conn, pid)
                if not p or p["status"] != "pending":
                    return f"P{pid} is not waiting for a decision."
                db.set_proposal_status(conn, pid, "declined", decided_by=sender_name, result=why)
                db.audit(conn, sender_name, "declined", f"#{p['target']}", proposal_id=pid, detail=why)
                return f"Declined P{pid}. Nothing was changed on GitHub."
            if verb == "edit":
                m = re.match(r"P?(\d+)\s*:?\s*(.*)", rest, re.DOTALL | re.IGNORECASE)
                if not m or not m.group(2).strip():
                    return "Usage: edit 8: <the new text>"
                pid, new_text = int(m.group(1)), m.group(2).strip()
                p = db.get_proposal(conn, pid)
                if not p or p["status"] != "pending":
                    return f"P{pid} is not waiting for a decision."
                db.set_proposal_text(conn, pid, new_text)
                db.audit(conn, sender_name, "edited", f"#{p['target']}", proposal_id=pid)
                return f"Updated P{pid}. Say approve {pid} to post it."
            if verb == "undo":
                m = _IDS.search(rest)
                if not m:
                    return "Which one? For example: undo 7"
                return self.undo(conn, int(m.group(1)), sender_name)
            if verb in ("assign", "release"):
                m = re.match(r"#?(\d+)\s+@?([A-Za-z0-9-]+)", rest)
                if not m:
                    return f"Usage: {verb} 12 @ben"
                number, login = int(m.group(1)), m.group(2)
                text = (
                    f"Hi @{login}, this one is yours. Shout in the issue if you get stuck."
                    if verb == "assign"
                    else f"Hi @{login}, I've released this so someone else can pick it up. Thanks for your interest, and you're welcome back any time."
                )
                pid = db.add_proposal(conn, verb, number, login=login, text=text, reason=f"direct order from {sender_name}", proposed_by=sender_name)
                db.mark_announced(conn, pid)
                return self.approve(conn, pid, sender_name)
        return None

    def approve(self, conn, pid: int, who: str) -> str:
        p = db.get_proposal(conn, pid)
        if not p:
            return f"There is no P{pid}."
        if p["status"] != "pending":
            return f"P{pid} is already {p['status']}."
        db.set_proposal_status(conn, pid, "approved", decided_by=who)
        try:
            outcome = actions.execute(conn, self.gh, p, demo_mode=self.policy.demo_mode)
        except GitHubError as err:
            db.set_proposal_status(conn, pid, "failed", decided_by=who, result=str(err))
            db.audit(conn, "ledger", "failed", f"#{p['target']}", proposal_id=pid, detail=str(err))
            return f"P{pid} failed on GitHub: {err}. Nothing else was changed."
        if not outcome.ok:
            db.set_proposal_status(conn, pid, "failed", decided_by=who, result=outcome.summary)
            return f"P{pid} not done: {outcome.summary}."
        db.set_proposal_status(conn, pid, "done", decided_by=who, result=outcome.summary, undo={"steps": outcome.undo})
        db.audit(conn, who, f"approved: {outcome.summary}", f"#{p['target']}", proposal_id=pid)
        return f"Done P{pid}: {outcome.summary}. (undo {pid} reverses it)"

    def undo(self, conn, pid: int, who: str) -> str:
        p = db.get_proposal(conn, pid)
        if not p or p["status"] != "done":
            return f"P{pid} has nothing to undo."
        steps = json.loads(p.get("undo") or "{}").get("steps", [])
        outcome = actions.reverse(conn, self.gh, {**p, "undo": steps})
        if not outcome.ok:
            return f"Could not undo P{pid}: {outcome.summary}."
        db.set_proposal_status(conn, pid, "undone", decided_by=who, result=outcome.summary)
        db.audit(conn, who, f"undid: {outcome.summary}", f"#{p['target']}", proposal_id=pid)
        return f"Undid P{pid}: {outcome.summary}."

    # ── background jobs ──────────────────────────────────────────────────────

    def sync_once(self, conn) -> str:
        if not self.gh:
            return "I have no GitHub token, so I can't read GitHub."
        from .sync import sync

        try:
            report = sync(conn, self.gh, self_login=self.self_login)
        except GitHubError as err:
            return f"GitHub sync failed: {err}"
        parts = [f"{report['issues']} open issues"]
        if report["new_claims"]:
            parts.append("new claims: " + ", ".join(f"#{n} @{l}" for n, l in report["new_claims"]))
        if report["new_prs"]:
            parts.append("new PRs: " + ", ".join(f"#{n}" for n in report["new_prs"]))
        return "Synced GitHub: " + "; ".join(parts) + "."

    def announce_new(self) -> int:
        with db.session() as conn:
            fresh = db.unannounced_proposals(conn)
            for p in fresh:
                self.say(self.card(p))
                db.mark_announced(conn, p["id"])
            return len(fresh)

    def digest_due(self, now: datetime) -> bool:
        hh, mm = (int(x) for x in str(self.policy.raw.get("digest_at", "09:00")).split(":"))
        if (now.hour, now.minute) < (hh, mm):
            return False
        with db.session() as conn:
            last = db.get_meta(conn, "digest_asked_on")
            if last == now.strftime("%Y-%m-%d"):
                return False
            db.set_meta(conn, "digest_asked_on", now.strftime("%Y-%m-%d"))
        return True

    def ask_for_digest(self) -> None:
        steward = self.policy.agent("steward")
        self.say(f"@{steward} good morning. Please post today's summary for the maintainers (run `steward digest --mark`).")

    async def on_message(self, msg: Message) -> None:
        if msg.room_id != self.room_id:
            return
        if msg.sender_name.strip().lower() in self.my_names or (self.agent and self.agent.agent_id in msg.sender):
            return
        if _looks_like_agent(msg.sender):
            return
        reply = await asyncio.to_thread(self.handle, msg.sender_name, msg.body)
        if reply:
            await asyncio.to_thread(self.say, reply)


def _looks_like_agent(sender: str) -> bool:
    return "switch-agent" in sender or sender.startswith("@switch-agent")


# ── entry points ──────────────────────────────────────────────────────────────


def _agent_from_state() -> SwitchAgent:
    state = load_state()
    if not state.get("agent_id"):
        sys.exit("The Ledger is not registered yet. Run: steward-ledger setup")
    return SwitchAgent(state["base_url"], state["agent_id"], state["api_key"])


def _github(policy: Policy) -> tuple[GitHub | None, str]:
    token = github_token()
    if not token or not policy.repo:
        return None, ""
    gh = GitHub(token, policy.repo)
    try:
        login = gh.request("GET", "/user").get("login", "")
    except GitHubError:
        login = ""
    return gh, login


def cmd_setup(args) -> int:
    env = read_console_env()
    base = args.url or local_api_url(env)
    if not health(base):
        print(f"Switch is not answering at {base}. Is Switch Console running with the local server on?")
        return 1
    token = args.registration_token or env.get("AGENT_REGISTRATION_TOKEN", "")
    if not token:
        print("No registration key found. Create one in the Switch Gateway (API Keys, type registration) and pass --registration-token.")
        return 1
    try:
        agent_id, api_key = register(base, token, name=args.name, overwrite=args.overwrite)
    except SwitchError as err:
        if err.status == 409:
            print(f"An agent called '{args.name}' already exists. Re-run with --overwrite to issue a new key.")
            return 1
        raise
    save_state({"base_url": base, "agent_id": agent_id, "api_key": api_key, "name": args.name})
    agent = SwitchAgent(base, agent_id, api_key)
    try:
        agent.op("update_agent_detail", {"agent_id": agent_id, "addressing": "owner_and_owner_agents"})
    except SwitchError as err:
        log.debug("could not set addressing: %s", err)
    print(f"Registered the Ledger with Switch at {base} as '{args.name}' (id {agent_id}).")
    print(f"Next: in Slack #maintainers type   /invite-agent @{args.name}   (or !invite-agent @{args.name})")
    return 0


def cmd_rooms(args) -> int:
    agent = _agent_from_state()
    rooms = agent.rooms()
    if not rooms:
        print("The Ledger is in no rooms yet. In Slack #maintainers: /invite-agent @ledger")
    for room in rooms:
        print(f"{room.get('room_id')}  {room.get('name')}")
    return 0


def cmd_say(args) -> int:
    policy = load_policy()
    agent = _agent_from_state()
    room = agent.find_room(args.room or policy.room("maintainers"))
    if not room:
        print("The Ledger is not in the maintainers room yet. In Slack #maintainers: /invite-agent @ledger")
        return 1
    agent.post(room["room_id"], " ".join(args.text))
    print(f"Posted in {room['name']}.")
    return 0


def cmd_sync(args) -> int:
    policy = load_policy()
    gh, login = _github(policy)
    if not gh:
        print("No GitHub token. Run scripts/setup.sh, or set STEWARD_GITHUB_TOKEN.")
        return 1
    ledger = Ledger(policy, None, gh, login)
    with db.session() as conn:
        print(ledger.sync_once(conn))
    return 0


def cmd_code(args) -> int:
    policy = load_policy()
    print(project.checkout(policy.repo, github_token()))
    return 0


async def _run(args) -> int:
    policy = load_policy()
    agent = _agent_from_state()
    room = agent.find_room(policy.room("maintainers"))
    if not room:
        print("The Ledger is not in the maintainers room yet. In Slack #maintainers: /invite-agent @ledger")
        return 1
    gh, login = _github(policy)
    ledger = Ledger(policy, agent, gh, login)
    ledger.room_id = room["room_id"]
    ledger.my_names |= {load_state().get("name", "ledger").lower()}
    stop = asyncio.Event()

    if not args.quiet_start:
        await asyncio.to_thread(
            ledger.say,
            "Ledger online. I act on GitHub only when a maintainer approves, and I log everything. Type `help` to see what I understand."
            + ("" if gh else "\n(No GitHub token yet: I can take decisions but cannot act on real issues.)"),
        )

    async def loop_announce() -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(ledger.announce_new)
            except Exception:  # noqa: BLE001
                log.exception("announcing proposals failed")
            await asyncio.sleep(3)

    async def loop_sync() -> None:
        if not gh:
            return
        every = max(20, int(policy.raw.get("sync_every_seconds", 60)))
        while not stop.is_set():
            def once() -> str:
                with db.session() as conn:
                    return ledger.sync_once(conn)
            try:
                log.info(await asyncio.to_thread(once))
            except Exception:  # noqa: BLE001
                log.exception("GitHub sync failed")
            await asyncio.sleep(every)

    async def loop_code() -> None:
        token = github_token()
        while not stop.is_set():
            try:
                log.info("code: %s", await asyncio.to_thread(project.checkout, policy.repo, token))
            except Exception:  # noqa: BLE001
                log.exception("updating the code checkout failed")
            await asyncio.sleep(600)

    async def loop_digest() -> None:
        while not stop.is_set():
            if ledger.digest_due(datetime.now()):
                await asyncio.to_thread(ledger.ask_for_digest)
            await asyncio.sleep(30)

    tasks = [
        asyncio.create_task(agent.listen([ledger.room_id], ledger.on_message, stop)),
        asyncio.create_task(loop_announce()),
        asyncio.create_task(loop_sync()),
        asyncio.create_task(loop_digest()),
        asyncio.create_task(loop_code()),
    ]
    print(f"Ledger running in '{room['name']}'. Press Ctrl+C to stop.")
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
    return 0


def cmd_run(args) -> int:
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nLedger stopped.")
        return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("STEWARD_LOG", "INFO"), format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(prog="steward-ledger", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("setup", help="register the Ledger with the Switch server on this computer")
    s.add_argument("--url", help="Switch API address (default: read from Switch Console)")
    s.add_argument("--registration-token", help="default: read from Switch Console's local server")
    s.add_argument("--name", default="ledger")
    s.add_argument("--overwrite", action="store_true", help="re-register and issue a new key")
    s.set_defaults(func=cmd_setup)

    sub.add_parser("rooms", help="rooms the Ledger is in").set_defaults(func=cmd_rooms)

    s = sub.add_parser("say", help="post a message in the maintainers room")
    s.add_argument("text", nargs="+")
    s.add_argument("--room")
    s.set_defaults(func=cmd_say)

    sub.add_parser("sync", help="read GitHub once").set_defaults(func=cmd_sync)
    sub.add_parser("code", help="fetch the project's code for the agents").set_defaults(func=cmd_code)

    s = sub.add_parser("run", help="stay in the room and work")
    s.add_argument("--quiet-start", action="store_true", help="don't post the hello message")
    s.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
