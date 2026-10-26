"""Telegram relay CLI: a skill's bot finds, resumes and steers its own
conversations (#1639).

Run by a relay-enabled Telegram bot session (``"relay": true`` in
``config/channel_profiles.json``), never by hand. The launch put everything it
needs in that session's environment: ``LAUNCHER_RELAY_URL`` (this PC's
webapp), ``LAUNCHER_RELAY_PROFILE`` and ``LAUNCHER_RELAY_TOKEN``, and a
permission rule that allows exactly ``$LAUNCHER_RELAY_COMMAND``, so the
command must be typed as that text plus a subcommand, with no pipe,
redirection or ``cd`` around it.

    find [QUERY ...] [--limit N]   the skill's conversations, newest first, or the hits for QUERY
    resume [PATH] [--chat KEY]     resume PATH (from find), or the newest resumable one
    new [--chat KEY]               start a fresh saved conversation of the skill (then ask it)
    steer SESSION_ID TEXT ...      send TEXT once (or --file FILE)
    read SESSION_ID                the session's last reply
    ask SESSION_ID TEXT ...        steer, then wait for the reply (--timeout SECS)
    stop SESSION_ID                close a conversation resume or new opened, once the chat is done
    status [--chat KEY]            what is open for this bot, and what was closed since last asked

``--chat`` is the chat's own key (the Telegram chat id): each chat keeps one
conversation the relay opened (reopened or new), and ``resume`` or ``new`` in a
chat that has one answers ``already_open`` with it instead of starting another.

Output: the first line is always ``STATE=<state>``, then ``KEY=value`` lines,
``CONVERSATION`` and ``STOP`` rows, then (for a reply) a ``---`` line and the
reply text. A found row carries ``tier`` (0 title, 1 topic, 2 digest, 3 full
text only) when the search reports it. Exit 0 for an outcome (found, none,
resumed, opened, started, already_open, delivered, queued, unconfirmed, reply, stopped,
open), 1 for a refusal or failure, 2 when the relay is not set up in this session. The
states, and what each means for the chat:

    not_found         no such session or conversation
    not_this_skill    the session is not one of this skill's conversations
    refused           the relay refused the call (off-PC, bad token, bot not running)
    relay_off         relay is off for this profile
    not_resumable     the conversation can't be resumed (MESSAGE says why)
    stranded          the message was written but not taken; do NOT send it again
    gone              the target session ended
    host_unreachable  the session host is down; nothing was done
    unknown           the launcher could not be reached; delivery is unknown
    unavailable       the reply could not be read (REASON says why); not old text
    no_reply_yet      sent, but no reply within the wait; read again later
    started           new: a fresh conversation is running (SESSION_ID); send the opening message with ask
    already_open      this chat already has a conversation the relay opened (SESSION_ID); nothing launched
    stopped           the conversation was closed, its capture already saved
    not_relay_owned   stop: the relay did not reopen that session for this bot; left alone
    busy              stop: the conversation is mid-turn or waiting on an answer; nothing done
    capture_unconfirmed  stop: its last turn is not confirmed saved yet; nothing done, try later
    stop_unconfirmed  stop: quit sent but not confirmed closed; status says whether it is still open
    open / none       status: whether this bot has reopened conversations running

Stdlib only, so it runs on the launcher's venv interpreter with no imports
from the repo.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

_OK_STATES = frozenset({
    "found", "none", "resumed", "opened", "started", "already_open", "delivered", "queued",
    "unconfirmed", "reply", "stopped", "open",
})
_ROW_FIELDS = ("path", "conversation", "title", "topic", "last_interaction", "resumable",
               "tier", "session_id", "chat", "idle_minutes", "minutes_to_close")
_STOP_FIELDS = ("session_id", "conversation", "title", "chat", "reason", "stopped_at")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_POLL_SECONDS = 2.0
# Output still arriving this recently means the reply may be mid-stream, when
# the Board cannot say whether the target is working.
_QUIET_SECONDS = 5.0


class _Setup(Exception):
    """The relay environment is missing or wrong."""


def _env() -> Tuple[str, str, str]:
    url = os.environ.get("LAUNCHER_RELAY_URL", "").rstrip("/")
    profile = os.environ.get("LAUNCHER_RELAY_PROFILE", "")
    token = os.environ.get("LAUNCHER_RELAY_TOKEN", "")
    if not (url and profile and token):
        raise _Setup("relay is not set up in this session (its LAUNCHER_RELAY_* "
                     "environment is missing); the bot needs relay on and a restart")
    if urllib.parse.urlparse(url).hostname not in _LOOPBACK_HOSTS:
        raise _Setup("LAUNCHER_RELAY_URL is not a loopback address")
    return url, profile, token


def _call(method: str, path: str, *, query: Optional[Dict[str, str]] = None,
          body: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
    """One relay request. Always returns a dict with a ``state``."""
    url, profile, token = _env()
    target = f"{url}/api/life-os/channels/{urllib.parse.quote(profile)}/relay/{path}"
    if query:
        target += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"X-Relay-Token": token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(target, data=data, method=method, headers=headers)
    context = None
    if target.startswith("https://"):
        # Loopback only (checked above): the cert names the tailnet host.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("detail")
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, dict) and detail.get("state"):
            return {"state": str(detail["state"]), "message": str(detail.get("message") or "")}
        return {"state": "unknown", "message": f"HTTP {exc.code}: {str(detail or exc.reason)[:300]}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"state": "unknown",
                "message": f"the launcher could not be reached ({exc}); whether anything "
                           "happened is unknown"}
    try:
        reply = json.loads(raw) if raw else {}
    except ValueError:
        return {"state": "unknown", "message": "the launcher's reply was unreadable"}
    return reply if isinstance(reply, dict) and reply.get("state") else {
        "state": "unknown", "message": "the launcher's reply had no state"}


def _print(result: Dict[str, Any], *, text: Optional[str] = None) -> int:
    print(f"STATE={result['state']}")
    for key, value in result.items():
        if key in ("state", "text", "conversations", "stops") or value in (None, ""):
            continue
        print(f"{key.upper()}={value}")
    for label, rows, keys in (("CONVERSATION", result.get("conversations"), _ROW_FIELDS),
                              ("STOP", result.get("stops"), _STOP_FIELDS)):
        for row in rows or []:
            fields = " | ".join(f"{key}={str(row.get(key)).strip()}" for key in keys
                                if row.get(key) not in (None, ""))
            print(f"{label} {fields}")
    reply = text if text is not None else result.get("text")
    if reply is not None:
        print("---")
        print(reply)
    return 0 if result["state"] in _OK_STATES else 1


def _message(args: argparse.Namespace) -> str:
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            return handle.read()
    return " ".join(args.text)


def _when(stamp: Any) -> Optional[datetime]:
    """An ISO timestamp (a transcript's) or epoch seconds (the session host's
    ``last_output_at``) as an aware datetime; ``None`` when it is neither."""
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _settled(read: Dict[str, Any]) -> bool:
    """Whether the reply in ``read`` is finished rather than mid-turn."""
    status = read.get("status")
    if status:
        return status != "working"
    last = _when(read.get("last_output_at"))
    return last is not None and (datetime.now(timezone.utc) - last).total_seconds() >= _QUIET_SECONDS


def cmd_find(args: argparse.Namespace) -> int:
    query = {"limit": str(args.limit)}
    if args.query:
        query["q"] = " ".join(args.query)
    return _print(_call("GET", "conversations", query=query))


def cmd_resume(args: argparse.Namespace) -> int:
    body = {key: value for key, value in (("path", args.path), ("chat", args.chat)) if value}
    return _print(_call("POST", "resume", body=body))


def cmd_new(args: argparse.Namespace) -> int:
    return _print(_call("POST", "new", body={"chat": args.chat} if args.chat else {},
                        timeout=60.0))


def cmd_stop(args: argparse.Namespace) -> int:
    # The launcher waits up to 15 s for the quit to land before it answers.
    return _print(_call("POST", "stop", body={"session_id": args.session_id}, timeout=60.0))


def cmd_status(args: argparse.Namespace) -> int:
    return _print(_call("GET", "status", query={"chat": args.chat} if args.chat else None))


def cmd_steer(args: argparse.Namespace) -> int:
    return _print(_call("POST", "steer", body={"session_id": args.session_id,
                                               "data": _message(args)}))


def cmd_read(args: argparse.Namespace) -> int:
    return _print(_call("GET", "read", query={"session_id": args.session_id}))


def cmd_ask(args: argparse.Namespace) -> int:
    text = _message(args)
    before = _call("GET", "read", query={"session_id": args.session_id})
    if before["state"] not in ("reply", "unavailable"):
        return _print(before)  # a refusal: nothing was sent
    baseline = _when(before.get("timestamp")) if before["state"] == "reply" else None
    sent_at = datetime.now(timezone.utc)
    steer = _call("POST", "steer", body={"session_id": args.session_id, "data": text})
    if steer["state"] not in _OK_STATES:
        return _print(steer)
    if steer["state"] == "queued":
        # The target is mid-turn: the next reply it finishes is the turn it was
        # already on, not an answer to this message. Say so instead of
        # passing that one off as the answer.
        return _print({**steer, "message": "the conversation was busy; the message is "
                                           "queued and will be submitted when it settles. "
                                           "Run read later; do not send it again"})
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
        read = _call("GET", "read", query={"session_id": args.session_id})
        if read["state"] not in ("reply", "unavailable"):
            # The target went away, or the relay stopped answering mid-wait.
            return _print({**read, "steer": steer["state"]})
        if read["state"] != "reply":
            continue
        stamp = _when(read.get("timestamp"))
        newer = stamp is not None and (stamp > baseline if baseline else stamp >= sent_at)
        if newer and _settled(read):
            return _print({**read, "steer": steer["state"]})
    return _print({"state": "no_reply_yet", "steer": steer["state"],
                   "session_id": args.session_id,
                   "message": f"sent once, no finished reply within {args.timeout:.0f}s; "
                              "run read later and do not send it again"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    find = sub.add_parser("find")
    find.add_argument("query", nargs="*")
    find.add_argument("--limit", type=int, default=10)
    find.set_defaults(func=cmd_find)
    resume = sub.add_parser("resume")
    resume.add_argument("path", nargs="?", default="")
    resume.add_argument("--chat", default="")
    resume.set_defaults(func=cmd_resume)
    new = sub.add_parser("new")
    new.add_argument("--chat", default="")
    new.set_defaults(func=cmd_new)
    stop = sub.add_parser("stop")
    stop.add_argument("session_id")
    stop.set_defaults(func=cmd_stop)
    status = sub.add_parser("status")
    status.add_argument("--chat", default="")
    status.set_defaults(func=cmd_status)
    for name, func in (("steer", cmd_steer), ("ask", cmd_ask)):
        cmd = sub.add_parser(name)
        cmd.add_argument("session_id")
        cmd.add_argument("text", nargs="*")
        cmd.add_argument("--file")
        if name == "ask":
            cmd.add_argument("--timeout", type=float, default=120.0)
        cmd.set_defaults(func=func)
    read = sub.add_parser("read")
    read.add_argument("session_id")
    read.set_defaults(func=cmd_read)
    return parser


def main(argv: Optional[list] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = _parser().parse_args(argv)
    if args.command in ("steer", "ask") and not (args.file or args.text):
        return _print({"state": "refused", "message": "nothing to send"})
    try:
        return args.func(args)
    except _Setup as exc:
        print("STATE=refused")
        print(f"MESSAGE={exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
