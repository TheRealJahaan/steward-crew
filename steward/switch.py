"""A small client for Switch's agent protocol, so the Ledger can sit in a room.

The Ledger is a custom agent: plain Python, no model. It registers once,
opens the agent WebSocket for the maintainers room, answers the server's
pings, reads messages, and posts replies through the agent API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

log = logging.getLogger("steward.switch")

# Switch Console keeps its data under "switchdash" (its original name).
CONSOLE_DIRS = [
    Path.home() / "Library" / "Application Support" / "switchdash" / "local-switch-server",
    Path.home() / "Library" / "Application Support" / "Switch Console" / "local-switch-server",
]


class SwitchError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Switch {status}: {detail}")
        self.status = status


def _console_dir() -> Path:
    if os.environ.get("SWITCH_CONSOLE_DIR"):
        return Path(os.environ["SWITCH_CONSOLE_DIR"])
    for path in CONSOLE_DIRS:
        if path.exists():
            return path
    return CONSOLE_DIRS[0]


def read_console_env() -> dict[str, str]:
    """What Switch Console knows about the server it runs on this computer.

    Console runs the server either in Docker (with a .env file) or directly on
    the Mac (settings passed to the process). Read whichever is there: the
    .env, the ports.json beside it, and, for the registration key, the running
    switch-core process's own environment, which macOS shows to its owner.
    """
    root = _console_dir()
    out: dict[str, str] = {}
    env_file = root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip().strip('"').strip("'")
    ports = root / "ports.json"
    if "API_HOST_PORT" not in out and ports.exists():
        try:
            data = json.loads(ports.read_text())
            if data.get("api"):
                out["API_HOST_PORT"] = str(data["api"])
        except (ValueError, AttributeError):
            pass
    if "AGENT_REGISTRATION_TOKEN" not in out:
        token = _token_from_process()
        if token:
            out["AGENT_REGISTRATION_TOKEN"] = token
    return out


def _token_from_process() -> str:
    """macOS `ps -E` shows the environment of your own processes."""
    import subprocess

    try:
        listing = subprocess.run(
            ["ps", "-axE", "-ww", "-o", "command"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in listing.splitlines():
        if "AGENT_REGISTRATION_TOKEN=" in line and "switch" in line.lower():
            match = re.search(r"AGENT_REGISTRATION_TOKEN=(\S+)", line)
            if match:
                return match.group(1)
    return ""


def local_api_url(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else read_console_env()
    port = env.get("API_HOST_PORT") or "8000"
    return f"http://localhost:{port}"


def _http(method: str, url: str, token: str, body: Any = None, headers: dict[str, str] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        raise SwitchError(err.code, err.read().decode(errors="replace")[:400]) from err


def health(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def register(
    base_url: str,
    registration_token: str,
    *,
    name: str = "ledger",
    display_name: str = "Ledger",
    description: str = "Steward Crew's Ledger: acts on GitHub only after a maintainer approves, and keeps the audit log.",
    overwrite: bool = False,
) -> tuple[str, str]:
    body = {
        "name": name,
        "display_name": display_name,
        "description": description,
        "connector_type": "steward-ledger",
        "integration_profile": {
            "connection_model": "always_on",
            "message_exchange": True,
            "pre_invocation_mediation": [],
            "post_invocation_mediation": [],
            "event_reporting": [],
        },
        "overwrite": overwrite,
    }
    result = _http("POST", f"{base_url.rstrip('/')}/agents", registration_token, body)
    return result["id"], result["api_key"]


@dataclass
class Message:
    room_id: str
    sender: str
    sender_name: str
    body: str
    message_id: str
    addressed: bool
    thread_id: str | None


Handler = Callable[[Message], Awaitable[None]]


class SwitchAgent:
    def __init__(self, base_url: str, agent_id: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.api_key = api_key
        self.connection_id = str(uuid.uuid4())
        self.cursor = 0
        self.generation: int | None = None

    # ── REST ─────────────────────────────────────────────────────────────────

    def op(self, operation: str, args: dict[str, Any] | None = None, *, with_connection: bool = False) -> Any:
        headers = {"X-Switch-Connection-Id": self.connection_id} if with_connection else None
        result = _http(
            "POST", f"{self.base_url}/agents/{self.agent_id}/ops/{operation}", self.api_key, args or {}, headers
        )
        return result.get("result") if isinstance(result, dict) and "result" in result else result

    def rooms(self) -> list[dict[str, Any]]:
        return self.op("list_rooms") or []

    def find_room(self, name: str) -> dict[str, Any] | None:
        wanted = _norm(name)
        rooms = self.rooms()
        for room in rooms:
            if _norm(room.get("name", "")) == wanted:
                return room
        for room in rooms:
            if wanted and wanted in _norm(room.get("name", "")):
                return room
        return None

    def post(self, room_id: str, content: str) -> str:
        result = _http(
            "POST", f"{self.base_url}/agents/{self.agent_id}/message", self.api_key, {"room_id": room_id, "content": content}
        )
        return str((result or {}).get("event_id", ""))

    # ── WebSocket ────────────────────────────────────────────────────────────

    def _ws_url(self, rooms: list[str]) -> str:
        params: dict[str, str] = {"connection_id": self.connection_id, "filter": "all"}
        if rooms:
            params["rooms"] = ",".join(rooms)
        if self.cursor:
            params["start_from"] = str(self.cursor)
        if self.generation is not None:
            params["expected_generation"] = str(self.generation)
        params["client"] = "steward-ledger"
        base = self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/agents/{self.agent_id}/connection/ws?{urlencode(params)}"

    async def listen(self, rooms: list[str], handler: Handler, stop: asyncio.Event) -> None:
        """Stay connected, hand every message to `handler`, reconnect on drops."""
        import websockets

        delay = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(
                    self._ws_url(rooms),
                    additional_headers={"Authorization": f"Bearer {self.api_key}"},
                    max_size=8 * 1024 * 1024,
                ) as sock:
                    delay = 1.0
                    async for raw in sock:
                        frame = json.loads(raw)
                        kind = frame.get("event") or frame.get("type")
                        data = frame.get("data") or {}
                        if kind == "ping":
                            # Answer before doing anything else: a late pong evicts us.
                            await sock.send(json.dumps({"type": "pong", "cursor": self.cursor}))
                            continue
                        if kind == "connection_state":
                            self.generation = data.get("generation", self.generation)
                            self.cursor = int(data.get("cursor") or self.cursor)
                            log.info("connected to Switch, rooms %s", data.get("rooms"))
                            continue
                        if kind == "gap":
                            self.cursor = int(data.get("resumed_at") or self.cursor)
                            continue
                        if kind == "evicted":
                            code = data.get("code")
                            log.warning("evicted by Switch: %s (%s)", code, data.get("reason"))
                            if code in ("taken_over", "closed"):
                                stop.set()
                            break
                        if kind == "refused":
                            raise SwitchError(int(data.get("status") or 0), str(data.get("detail")))
                        if frame.get("id") is not None:
                            self.cursor = int(frame["id"])
                        if kind == "message":
                            payload = data.get("payload") or {}
                            msg = Message(
                                room_id=data.get("room_id", ""),
                                sender=str(payload.get("sender", "")),
                                sender_name=str(payload.get("sender_name", "")),
                                body=str(payload.get("body", "")),
                                message_id=str(payload.get("message_id", "")),
                                addressed=bool(payload.get("addressed")),
                                thread_id=payload.get("thread_id"),
                            )
                            try:
                                await handler(msg)
                            except Exception:  # noqa: BLE001 - one bad message must not drop the socket
                                log.exception("handler failed on message %s", msg.message_id)
            except SwitchError as err:
                log.error("Switch refused the connection: %s", err)
                if err.status in (400, 401, 403):
                    stop.set()
                    raise
            except (OSError, asyncio.TimeoutError, Exception) as err:  # noqa: BLE001
                if stop.is_set():
                    break
                log.warning("connection to Switch lost (%s); retrying in %.0fs", err, delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 30.0)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())
