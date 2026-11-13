"""Fleet-wide GitHub reads for the Board tab, via the ``gh`` CLI.

Wraps the exact cross-repo queries the ``/issue-triage`` skill runs — open
issues, open PRs, and today's merged/closed items across every repo the
configured owner has — behind a module-level cache. The contract mirrors the
Coding tab's ⎇ git-status button: **``gh`` is a subprocess per call and is
never invoked on a poll** — :func:`refresh` runs the searches only when the
user asks (or once on first Board activation), while :func:`snapshot` is a
pure memory read the 5s board poll can hit for free.

Shared ownership note: this module is the single fetch layer —
the future Issues tab builds its rich inbox on these same functions
instead of growing a second ``gh`` wrapper.

The one write is :func:`create_issue`, behind the Board's issue-filing route.

Errors degrade, never break the board: a failed refresh keeps the previous
snapshot's data and surfaces ``error`` so the UI can badge it.

The cache is process memory, so every webapp restart starts it empty with
``fetched_at: None`` — "never fetched", which is *not* "fetched, nothing
open". Readers must consult ``fetched_at`` before counting the lists.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.subprocess_flags import NO_WINDOW

logger = logging.getLogger(__name__)

_GH_TIMEOUT_SECONDS = 20.0
# The command a throwaway instance runs instead of the real ``gh``:
# a JSON list of argv, e.g. the design-review synthetic instance's fake
# (``scripts/synthetic_gh.py``). Unset everywhere real.
GH_CMD_ENV = "LAUNCHER_GH_CMD"
_ISSUE_LIMIT = 100
# The open-issue search feeds the Board's per-issue partition, so it must hold
# every open issue: GitHub's search API serves at most 1000 results, and a
# fetch that fills it is flagged ``truncated`` rather than read as complete.
_OPEN_ISSUE_LIMIT = 1000
_PR_LIMIT = 50

# Fields common to issue and PR search rows.
_SEARCH_FIELDS = "repository,number,title,url,updatedAt"


class GhError(RuntimeError):
    """``gh`` missing, timed out, unauthenticated, or non-zero exit."""


def _gh_argv() -> List[str]:
    """``["gh"]``, or the argv :data:`GH_CMD_ENV` names.

    A malformed value is a :class:`GhError`, never a quiet fall-back to the
    real ``gh``: the instance that set it meant to stay off real GitHub.
    """
    raw = os.environ.get(GH_CMD_ENV, "").strip()
    if not raw:
        return ["gh"]
    try:
        argv = json.loads(raw)
    except ValueError:
        argv = None
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
        raise GhError(f"{GH_CMD_ENV} must be a JSON list of strings")
    return argv


def _run_gh(
    args: Sequence[str],
    *,
    timeout: float = _GH_TIMEOUT_SECONDS,
    stdin_text: Optional[str] = None,
) -> str:
    """Run ``gh <args>`` and return stdout; :class:`GhError` on any failure."""
    argv = _gh_argv()
    try:
        proc = subprocess.run(
            [*argv, *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GhError(f"gh {' '.join(args[:2])}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else "no output"
        raise GhError(f"gh {' '.join(args[:2])} exited {proc.returncode}: {first}")
    return proc.stdout


def _search_json(args: Sequence[str]) -> List[Dict[str, Any]]:
    out = _run_gh(args)
    try:
        data = json.loads(out or "[]")
    except ValueError as exc:
        raise GhError(f"gh returned unparseable JSON: {exc}") from exc
    return data if isinstance(data, list) else []


def _repo_name(row: Dict[str, Any]) -> str:
    repo = row.get("repository") or {}
    full = str(repo.get("nameWithOwner") or repo.get("name") or "")
    return full.split("/")[-1]


def _norm_issue(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "issue",
        "repo": _repo_name(row),
        "number": row.get("number"),
        "title": row.get("title"),
        "url": row.get("url"),
        "updated_at": row.get("updatedAt"),
        # Only the done search asks for ``closedAt``; None elsewhere.
        "closed_at": row.get("closedAt"),
        "labels": [
            lab.get("name")
            for lab in (row.get("labels") or [])
            if isinstance(lab, dict) and lab.get("name")
        ],
    }


def _norm_pr(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "pr",
        "repo": _repo_name(row),
        "number": row.get("number"),
        "title": row.get("title"),
        "url": row.get("url"),
        "updated_at": row.get("updatedAt"),
        "is_draft": bool(row.get("isDraft")),
        "labels": [
            lab.get("name")
            for lab in (row.get("labels") or [])
            if isinstance(lab, dict) and lab.get("name")
        ],
    }


# The fleet's ``/codebase-audit`` skill files ledger/decision-log issues under
# this label ("codebase-audit ledger / metadata — not actionable work") —
# real bookkeeping, not dispatchable work, so the Board hides them entirely.
_NON_ACTIONABLE_LABELS = {"audit-meta"}


def _is_actionable(labels: Sequence[str]) -> bool:
    return not any(str(lab).lower() in _NON_ACTIONABLE_LABELS for lab in labels)


def _search_open_issue_rows(owner: str) -> List[Dict[str, Any]]:
    rows = _search_json([
        "search", "issues",
        "--owner", owner,
        "--state", "open",
        "--sort", "updated",
        "--limit", str(_OPEN_ISSUE_LIMIT),
        "--json", _SEARCH_FIELDS + ",labels",
    ])
    return [_norm_issue(r) for r in rows]


def _split_records(
    issues: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(actionable, records)``: machine records are bookkeeping, not work."""
    actionable = [i for i in issues if _is_actionable(i["labels"])]
    records = [i for i in issues if not _is_actionable(i["labels"])]
    return actionable, records


def search_open_prs(owner: str) -> List[Dict[str, Any]]:
    rows = _search_json([
        "search", "prs",
        "--owner", owner,
        "--state", "open",
        "--sort", "updated",
        "--limit", str(_PR_LIMIT),
        "--json", _SEARCH_FIELDS + ",isDraft,labels",
    ])
    prs = [_norm_pr(r) for r in rows]
    return [p for p in prs if _is_actionable(p["labels"])]


def local_day_window(
    day: date, tz: Optional[tzinfo] = None
) -> Tuple[datetime, datetime]:
    """The first and last second of the local calendar day ``day``, in UTC.

    GitHub stamps and searches in UTC while the owner reads days on this
    host's clock, so a "day" is converted instead of assuming 24 h from UTC
    midnight: the window opens at local midnight and closes one second before
    the next local midnight, which makes the day 23 h or 25 h long across a DST
    change. ``tz`` stands in for the host's zone (tests); ``None`` is the host's.
    """
    def midnight_utc(d: date) -> datetime:
        return datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)

    start = midnight_utc(day)
    end = midnight_utc(day + timedelta(days=1)) - timedelta(seconds=1)
    return start, end


def _gh_stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


# GitHub's own closing keywords; "Part of #N" / "Progresses #N" do not close.
_CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+#(\d+)", re.IGNORECASE)
_VISUAL_PROOF = re.compile(r"\bVISUAL_PROOF=([A-Za-z/]+)")


def search_merged_on(
    owner: str, day: date, tz: Optional[tzinfo] = None
) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """The PR that closed each issue, for PRs merged on the local day ``day``.

    Keyed ``(repo, issue number)``; the value is the PR's ``pr`` number and the
    state its body gives to ``VISUAL_PROOF=`` (``None`` when it has no such
    line). A body names an issue with a GitHub closing keyword. Where two PRs
    close one issue the later one (higher number) wins. Reads the PR body for
    that line and the keyword only; no report content is involved.
    """
    start, end = local_day_window(day, tz)
    merged = _search_json([
        "search", "prs",
        "--owner", owner,
        "--merged",
        "--merged-at", f"{_gh_stamp(start)}..{_gh_stamp(end)}",
        "--limit", str(_ISSUE_LIMIT),
        "--json", "repository,number,body,url",
    ])
    closers: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for row in merged:
        number = row.get("number")
        if not isinstance(number, int):
            continue
        body = str(row.get("body") or "")
        state = _VISUAL_PROOF.search(body)
        for issue in _CLOSES.findall(body):
            key = (_repo_name(row), int(issue))
            if key not in closers or number > closers[key]["pr"]:
                closers[key] = {
                    "pr": number, "pr_url": str(row.get("url") or ""),
                    "visual_proof": state.group(1).lower() if state else None,
                }
    return closers


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# A PR body names an issue it only contributes to ("Part of #N") as well as
# the ones it closes; either links the PR to a hand-closed issue.
_REFERENCES = (
    r"\b(?:part\s+of|progress(?:es|ed)?|close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+#{n}\b"
)
_NOT_PLANNED = "NOT_PLANNED"


def _lookup_query(owner: str, issues: Sequence[Tuple[str, int]]) -> str:
    """One GraphQL document asking close reason and referencing PRs per issue."""
    blocks = []
    for idx, (repo, number) in enumerate(issues):
        blocks.append(
            f'i{idx}: repository(owner: "{owner}", name: "{repo}") {{ issue(number: {number}) {{ '
            "stateReason timelineItems(itemTypes: [CROSS_REFERENCED_EVENT], last: 30) { nodes { "
            "... on CrossReferencedEvent { source { ... on PullRequest { "
            "number url merged mergedAt body repository { name } } } } } } } }"
        )
    return "query { " + " ".join(blocks) + " }"


def lookup_unpaired(
    owner: str, issues: Sequence[Tuple[str, int]]
) -> Dict[Tuple[str, int], Dict[str, Any]]:
    """Close reason and latest referencing merged PR for closed issues, in one call.

    ``issues`` are ``(repo, number)`` pairs no same-day closing PR paired.
    The value holds ``not_planned`` and, when a merged PR of the same repo
    names the issue in its body with "Part of"/"Progresses"/a closing keyword
    (on any merge day), ``pr``/``pr_url`` of the latest such PR. One GraphQL
    request covers the whole page, so Done costs no per-card ``gh`` call.
    """
    safe = [(r, n) for r, n in issues if _SAFE_NAME.match(r) and n > 0]
    if not safe or not _SAFE_NAME.match(owner):
        return {}
    out = _run_gh(["api", "graphql", "-f", f"query={_lookup_query(owner, safe)}"])
    try:
        data = (json.loads(out or "{}").get("data")) or {}
    except (ValueError, AttributeError) as exc:
        raise GhError(f"gh returned unparseable JSON: {exc}") from exc
    found: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for idx, (repo, number) in enumerate(safe):
        issue = ((data.get(f"i{idx}") or {}).get("issue")) or {}
        if not issue:
            continue
        mention = re.compile(_REFERENCES.format(n=number), re.IGNORECASE)
        best: Optional[Dict[str, Any]] = None
        for node in (issue.get("timelineItems") or {}).get("nodes") or []:
            pr = (node or {}).get("source") or {}
            if not (pr.get("merged") and isinstance(pr.get("number"), int)):
                continue
            if str((pr.get("repository") or {}).get("name") or "") != repo:
                continue
            if not mention.search(str(pr.get("body") or "")):
                continue
            if best is None or str(pr.get("mergedAt") or "") > str(best.get("mergedAt") or ""):
                best = pr
        row: Dict[str, Any] = {"not_planned": issue.get("stateReason") == _NOT_PLANNED}
        if best:
            row.update(pr=best["number"], pr_url=str(best.get("url") or ""))
        found[(repo, number)] = row
    return found


def search_done_on(
    owner: str, day: date, tz: Optional[tzinfo] = None
) -> List[Dict[str, Any]]:
    """Issues closed on the local day ``day``, latest close first.

    Merged PRs no longer get their own Done card — a PR that closed an issue
    is already reflected by that issue showing closed, so this is just the
    closed-issues search with no pairing/dedup step. The search is bounded by
    the day's exact UTC window (:func:`local_day_window`), so no row from a
    neighbouring day needs filtering out afterwards. Each row carries the PR
    that closed it (``pr``) and that PR's ``visual_proof`` state, from one more
    search over the same window (:func:`search_merged_on`); issues that search
    leaves unpaired are resolved by :func:`lookup_unpaired`.
    """
    start, end = local_day_window(day, tz)
    closed = _search_json([
        "search", "issues",
        "--owner", owner,
        "--state", "closed",
        "--closed", f"{_gh_stamp(start)}..{_gh_stamp(end)}",
        "--sort", "updated",
        "--limit", str(_ISSUE_LIMIT),
        "--json", _SEARCH_FIELDS + ",closedAt,labels",
    ])
    # Which PR closed each issue, so the Board can link its proof report. A
    # failed read leaves the cards without a PR: Done still shows, with no
    # proof state to claim either way.
    try:
        closers = search_merged_on(owner, day, tz)
    except GhError as exc:
        logger.warning("⚠️ gh merged-PR search for %s failed: %s", day.isoformat(), exc)
        closers = {}
    issues = [i for i in map(_norm_issue, closed) if _is_actionable(i["labels"])]
    # Issues no same-day closer paired (a "Part of" PR, an earlier merge, a
    # not-planned close) get one batched lookup; a failure leaves them bare.
    unpaired = [
        (i["repo"], int(i["number"] or 0)) for i in issues
        if (i["repo"], int(i["number"] or 0)) not in closers
    ]
    try:
        extra = lookup_unpaired(owner, unpaired)
    except GhError as exc:
        logger.warning("⚠️ gh PR lookup for %s failed: %s", day.isoformat(), exc)
        extra = {}
    done: List[Dict[str, Any]] = []
    for issue in issues:
        key = (issue["repo"], int(issue["number"] or 0))
        closer = closers.get(key)
        found = extra.get(key) or {}
        done.append({
            **issue, "state": "closed", "labels": [],
            "pr": closer["pr"] if closer else found.get("pr"),
            "pr_url": closer["pr_url"] if closer else found.get("pr_url", ""),
            "visual_proof": closer["visual_proof"] if closer else None,
            "not_planned": bool(found.get("not_planned")),
        })
    # By close time: a comment after the close bumps ``updatedAt``, so
    # ``updated_at`` only stands in for a row that has no close time.
    done.sort(
        key=lambda item: str(item.get("closed_at") or item.get("updated_at") or ""),
        reverse=True,
    )
    return done


def search_done_today(owner: str) -> List[Dict[str, Any]]:
    """Issues closed since local midnight today."""
    return search_done_on(owner, date.today())


# ----------------------------------------------------------- repo history

_HISTORY_PAGE = 100
# A guard against a connection that never reports its last page: 400 pages is
# 40,000 commits, far past any repo of the fleet.
_HISTORY_MAX_PAGES = 400

# One GraphQL connection per kind of event. ``c`` commits, ``p`` merged PRs,
# ``i`` issues; the alias doubles as the prefix of its cursor variable.
_HISTORY_BLOCKS = {
    "c": (
        "$csince: GitTimestamp, $cafter: String",
        "c: defaultBranchRef { target { ... on Commit { history("
        f"first: {_HISTORY_PAGE}, since: $csince, after: $cafter) {{ "
        "pageInfo { hasNextPage endCursor } nodes { committedDate additions deletions } } } } }",
    ),
    "p": (
        "$pafter: String",
        f"p: pullRequests(first: {_HISTORY_PAGE}, states: MERGED, "
        "orderBy: {field: UPDATED_AT, direction: DESC}, after: $pafter) { "
        "pageInfo { hasNextPage endCursor } nodes { mergedAt updatedAt } }",
    ),
    "i": (
        "$isince: DateTime, $iafter: String",
        f"i: issues(first: {_HISTORY_PAGE}, orderBy: {{field: UPDATED_AT, direction: DESC}}, "
        "filterBy: {since: $isince}, after: $iafter) { "
        "pageInfo { hasNextPage endCursor } nodes { createdAt closedAt } }",
    ),
}


def _history_query(active: Sequence[str]) -> str:
    """One GraphQL document for ``owner/name`` asking only the connections in ``active``.

    An unused variable is a GraphQL error, so the declarations follow the
    blocks that are present.
    """
    decls = ["$owner: String!", "$name: String!"]
    blocks = []
    for key in active:
        decl, block = _HISTORY_BLOCKS[key]
        decls.append(decl)
        blocks.append(block)
    return (
        f"query({', '.join(decls)}) {{ rateLimit {{ cost remaining }} "
        f"repository(owner: $owner, name: $name) {{ {' '.join(blocks)} }} }}"
    )


def _connection_page(connection: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """``(nodes, next cursor)`` of a GraphQL connection; the cursor is None on the last page."""
    conn = connection if isinstance(connection, dict) else {}
    nodes = [n for n in (conn.get("nodes") or []) if isinstance(n, dict)]
    info = conn.get("pageInfo") or {}
    cursor = info.get("endCursor") if info.get("hasNextPage") else None
    return nodes, (str(cursor) if cursor else None)


def repo_history(
    owner: str, name: str, since: Optional[datetime] = None
) -> Dict[str, Any]:
    """Every commit, merged PR and issue event of ``owner/name`` since ``since``.

    ``since=None`` reads the whole history (the backfill). Returns timestamps
    in GitHub's UTC form, ungrouped: ``commits`` as ``(committed, additions,
    deletions)``, ``merged`` PR merge times, ``opened`` and ``closed`` issue
    times, plus ``remaining`` (GraphQL points left, or None when unreported).
    Issues and PRs are read most-recently-updated first and stop at ``since``,
    so a refresh costs about one point per kind however old the repo is.
    Never ``gh search``: its index lags behind a merge. :class:`GhError` on any
    failure; a partial read is never returned.
    """
    if not (_SAFE_NAME.match(owner) and _SAFE_NAME.match(name)):
        raise GhError(f"unsafe repository name {owner}/{name}")
    since_text = _gh_stamp(since) if since else None
    result: Dict[str, Any] = {
        "commits": [], "merged": [], "opened": [], "closed": [], "remaining": None,
    }
    cursors: Dict[str, Optional[str]] = {"c": None, "p": None, "i": None}
    active = ["c", "p", "i"]
    for _ in range(_HISTORY_MAX_PAGES):
        if not active:
            return result
        args = ["api", "graphql", "-f", f"query={_history_query(active)}",
                "-f", f"owner={owner}", "-f", f"name={name}"]
        if since_text and "c" in active:
            args += ["-f", f"csince={since_text}"]
        if since_text and "i" in active:
            args += ["-f", f"isince={since_text}"]
        for key in active:
            if cursors[key]:
                args += ["-f", f"{key}after={cursors[key]}"]
        try:
            body = json.loads(_run_gh(args) or "{}")
        except ValueError as exc:
            raise GhError(f"gh returned unparseable JSON: {exc}") from exc
        data = (body.get("data") if isinstance(body, dict) else None) or {}
        repo = data.get("repository")
        if not isinstance(repo, dict):
            raise GhError(f"{owner}/{name} is not readable with this token")
        points = (data.get("rateLimit") or {}).get("remaining")
        result["remaining"] = points if isinstance(points, int) else result["remaining"]
        still: List[str] = []
        for key in active:
            if key == "c":
                head = (((repo.get("c") or {}).get("target")) or {}).get("history")
                nodes, cursors["c"] = _connection_page(head)
                result["commits"] += [
                    (n["committedDate"], int(n.get("additions") or 0), int(n.get("deletions") or 0))
                    for n in nodes if n.get("committedDate")
                ]
            elif key == "p":
                nodes, cursors["p"] = _connection_page(repo.get("p"))
                result["merged"] += [n["mergedAt"] for n in nodes if n.get("mergedAt")]
                # Newest-updated first: a page ending before ``since`` is the last that matters.
                if since_text and nodes and str(nodes[-1].get("updatedAt") or "") < since_text:
                    cursors["p"] = None
            else:
                nodes, cursors["i"] = _connection_page(repo.get("i"))
                result["opened"] += [n["createdAt"] for n in nodes if n.get("createdAt")]
                result["closed"] += [n["closedAt"] for n in nodes if n.get("closedAt")]
            if cursors[key]:
                still.append(key)
        active = still
    raise GhError(f"{owner}/{name}: history exceeded {_HISTORY_MAX_PAGES} pages")


# ------------------------------------------------------------------ cache

_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "fetched_at": None,
    "issues": [],
    "records": [],
    "truncated": False,
    "prs": [],
    "done": [],
    "error": None,
}


def create_issue(owner: str, repo: str, title: str, body: str) -> str:
    """File one issue on ``owner/repo`` assigned to the owner; returns its URL.

    The body travels on stdin (``--body-file -``), so its size and content
    never meet the Windows command line.
    """
    out = _run_gh(
        ["issue", "create", "--repo", f"{owner}/{repo}", "--title", title,
         "--body-file", "-", "--assignee", "@me"],
        stdin_text=body,
    )
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        raise GhError("gh issue create printed no issue URL")
    return lines[-1]


def issue_body(owner: str, repo: str, number: int) -> str:
    """The body text of ``owner/repo#number``; :class:`GhError` when it did not read."""
    out = _run_gh(["issue", "view", str(number), "--repo", f"{owner}/{repo}", "--json", "body"])
    try:
        data = json.loads(out or "{}")
    except ValueError as exc:
        raise GhError(f"gh returned unparseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GhError("gh issue view printed no object")
    return str(data.get("body") or "")


def issue_title(owner: str, repo: str, number: int, *, timeout: float = 4.0) -> str:
    """The title of issue or pull request ``owner/repo#number`` (the API serves a
    PR under its issue number); :class:`GhError` when it did not read."""
    out = _run_gh(["api", f"repos/{owner}/{repo}/issues/{number}", "--jq", ".title"], timeout=timeout)
    return out.strip()


def comment_issue(owner: str, repo: str, number: int, body: str) -> None:
    """Post one comment on ``owner/repo#number``; the body travels on stdin."""
    _run_gh(
        ["issue", "comment", str(number), "--repo", f"{owner}/{repo}", "--body-file", "-"],
        stdin_text=body,
    )


#: Enough runs on one branch to see each workflow's latest result and the
#: start of its red streak, in one call.
_RUNS_PER_PAGE = 50


def workflow_runs(owner: str, repo: str, branch: str) -> List[Dict[str, Any]]:
    """The latest Actions runs on ``branch``, newest first, in one ``gh api`` call.

    Pull-request runs are excluded: they never describe the branch itself.
    """
    if not (_SAFE_NAME.match(repo) and _SAFE_NAME.match(owner)):
        raise GhError(f"unsafe repo name {owner}/{repo}")
    query = urllib.parse.urlencode(
        {"branch": branch, "per_page": _RUNS_PER_PAGE, "exclude_pull_requests": "true"}
    )
    out = _run_gh(["api", f"repos/{owner}/{repo}/actions/runs?{query}"])
    try:
        data = json.loads(out or "{}")
    except ValueError as exc:
        raise GhError(f"gh api runs for {repo}: unparseable JSON: {exc}") from exc
    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        raise GhError(f"gh api runs for {repo}: no workflow_runs list")
    return [r for r in runs if isinstance(r, dict)]


# Matches the owner-check and CI sweeps' own cooldowns.
AUTO_REFRESH_COOLDOWN_SECONDS = 60


def recently_filled(seconds: float = AUTO_REFRESH_COOLDOWN_SECONDS) -> bool:
    """Whether a refresh landed inside ``seconds``; never-filled or unreadable is no."""
    try:
        stamp = datetime.fromisoformat(str(snapshot().get("fetched_at")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - stamp).total_seconds() < seconds


def snapshot() -> Dict[str, Any]:
    """The cached GitHub view — a pure memory read, safe on every poll."""
    with _lock:
        return dict(_cache)


def refresh(owner: str) -> Dict[str, Any]:
    """Run the ``gh`` searches now and replace the cache.

    Subprocess-heavy (three ``gh`` calls) — callers invoke this on explicit
    user demand only, never on a poll. On failure the previous data is kept
    and only ``error`` is updated, so a flaky ``gh`` degrades to a badge
    instead of an empty board.

    *Every* failure records ``error``, not just :class:`GhError` — a
    malformed row the normalisers choke on used to escape as a 500 and leave
    ``error: None`` behind, so the next reader saw no trace of it.
    """
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        # One search: the Board places the records in their own folded
        # bucket, counted in the Backlog total.
        rows = _search_open_issue_rows(owner)
        issues, records = _split_records(rows)
        prs = search_open_prs(owner)
        done = search_done_today(owner)
    except GhError as exc:
        logger.warning("⚠️ gh refresh failed: %s", exc)
        return _record_error(str(exc))
    except Exception as exc:
        logger.exception("❌ gh refresh failed unexpectedly")
        return _record_error(f"refresh failed unexpectedly: {type(exc).__name__}: {exc}")
    with _lock:
        _cache.update(
            fetched_at=fetched_at, issues=issues, records=records,
            truncated=len(rows) >= _OPEN_ISSUE_LIMIT, prs=prs, done=done, error=None,
        )
        logger.info(
            "✅ gh refresh: %d open issues (%d records), %d PRs, %d done today",
            len(rows), len(records), len(prs), len(done),
        )
        return dict(_cache)


def _record_error(error: str) -> Dict[str, Any]:
    """Keep the previous data, surface ``error`` to every later reader."""
    with _lock:
        _cache["error"] = error
        return dict(_cache)


def reset_cache() -> None:
    """Test helper — restore the pristine empty cache."""
    with _lock:
        _cache.update(
            fetched_at=None, issues=[], records=[], truncated=False, prs=[], done=[], error=None,
        )
