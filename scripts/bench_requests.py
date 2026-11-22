"""Benchmark the requests that make app-launcher feel slow, against a running webapp.

The number every speed fix is measured against: p50 and p95 for the Board, the
session list, a transcript open, a live transcript tick and git status, the
median queue and worker time the server reports for each, and the highest
in-flight count the webapp saw while the run lasted.

    .venv\\Scripts\\python.exe -m scripts.bench_requests
    .venv\\Scripts\\python.exe -m scripts.bench_requests --base https://127.0.0.1:8445 -n 20

Read-only by default: GETs only, one at a time, so it never loads the server
harder than one open phone does. Sending is the one request that writes, so it
is measured only when asked: ``--send-session <sid>`` posts ``--send-text`` to
that session, which should be a throwaway one. Each report is also written as
JSON under ``webapp/bench/`` so two runs (before and after a change) can be
compared.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from scripts.probe_endpoint_timing import _percentile
from src import runtime_files

logger = logging.getLogger(__name__)

REPORT_DIR = Path(__file__).resolve().parent.parent / "webapp" / "bench"
_TIMING_PART = re.compile(r"(queue|worker|app);dur=([\d.]+)")

Sample = Tuple[float, Dict[str, float]]  # client ms, server-reported ms by part


def server_parts(header: Optional[str]) -> Dict[str, float]:
    """``queue``, ``worker`` and ``app`` milliseconds from a ``Server-Timing`` value."""
    return {name: float(ms) for name, ms in _TIMING_PART.findall(header or "")}


def summarise(samples: List[Sample]) -> Dict[str, Any]:
    """p50, p95 and max of the client's time, and the median server queue and worker time."""
    totals = [ms for ms, _ in samples]
    queue = [parts["queue"] for _, parts in samples if "queue" in parts]
    worker = [parts["worker"] for _, parts in samples if "worker" in parts]
    return {
        "samples": len(totals),
        "p50_ms": round(statistics.median(totals), 1),
        "p95_ms": round(_percentile(totals, 95), 1),
        "max_ms": round(max(totals), 1),
        "queue_p50_ms": round(statistics.median(queue), 1) if queue else None,
        "worker_p50_ms": round(statistics.median(worker), 1) if worker else None,
    }


def _timed(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Tuple[httpx.Response, Sample]:
    start = time.perf_counter()
    resp = client.request(method, path, **kwargs)
    elapsed_ms = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    return resp, (elapsed_ms, server_parts(resp.headers.get("server-timing")))


def _first_session(client: httpx.Client) -> Optional[str]:
    """The id of a live session to read a transcript from, or ``None`` when none is running."""
    body = client.get("/api/claude-code/sessions").json()
    for row in body.get("sessions", []):
        sid = row.get("id") or row.get("session_id")
        if sid:
            return str(sid)
    return None


def _repeat(client: httpx.Client, method: str, path: str, count: int, interval_s: float,
            **kwargs: Any) -> List[Sample]:
    samples: List[Sample] = []
    for _ in range(count):
        samples.append(_timed(client, method, path, **kwargs)[1])
        time.sleep(interval_s)
    return samples


def run(base: str, count: int, interval_s: float, session: Optional[str],
        send_session: Optional[str], send_text: str) -> Dict[str, Any]:
    """Time every probe ``count`` times; a probe that cannot run is reported with why."""
    started = time.monotonic()
    results: Dict[str, Any] = {}
    with httpx.Client(base_url=base, verify=False, timeout=60.0) as client:
        for name, path in (("board", "/api/board"), ("session-list", "/api/claude-code/sessions"),
                           ("git-status", "/api/claude-code/git-status")):
            _timed(client, "GET", path)  # warm: the first request builds what the rest read
            results[name] = summarise(_repeat(client, "GET", path, count, interval_s))

        sid = session or _first_session(client)
        if sid is None:
            for name in ("transcript-open", "transcript-tick"):
                results[name] = {"skipped": "no live session; pass --session"}
        else:
            transcript = f"/api/claude-code/sessions/{sid}/transcript"
            first, _ = _timed(client, "GET", transcript)
            results["transcript-open"] = summarise(_repeat(client, "GET", transcript, count, interval_s))
            size = first.json().get("size")
            if isinstance(size, int):
                results["transcript-tick"] = summarise(
                    _repeat(client, "GET", transcript, count, interval_s, params={"after": size, "size": size})
                )
            else:
                results["transcript-tick"] = {"skipped": "the transcript page carried no size"}

        if send_session:
            results["send"] = summarise(_repeat(
                client, "POST", f"/api/claude-code/sessions/{send_session}/input", count, interval_s,
                json={"data": send_text, "submit": False},
            ))
        else:
            results["send"] = {"skipped": "writes into a session; pass --send-session <sid> to measure it"}

        window_s = max(10.0, time.monotonic() - started + 5.0)
        # A webapp that predates /api/request-stats has no in-flight figure: unknown, never 0.
        reply = client.get("/api/request-stats", params={"window_s": window_s})
        stats = reply.json() if reply.status_code == 200 else {}
    return {
        "base": base,
        "taken": datetime.now().isoformat(timespec="seconds"),
        "max_inflight": stats.get("max_inflight", "unknown (no /api/request-stats)"),
        "budgets": stats.get("budgets", {}),
        "results": results,
    }


def _ms(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value} ms"


def render(report: Dict[str, Any]) -> List[str]:
    lines = [f"Benchmark {report['taken']} against {report['base']}"]
    for name, row in report["results"].items():
        if "skipped" in row:
            lines.append(f"  {name:<16} skipped: {row['skipped']}")
            continue
        hard = (report["budgets"].get(name) or {}).get("hard_s")
        mark = "" if hard is None else (" OVER" if row["p95_ms"] > hard * 1000 else " ok")
        lines.append(
            f"  {name:<16} p50 {row['p50_ms']:>7} ms  p95 {row['p95_ms']:>7} ms  max {row['max_ms']:>7} ms  "
            f"queue {_ms(row['queue_p50_ms'])}  worker {_ms(row['worker_p50_ms'])}  n={row['samples']}{mark}"
        )
    lines.append(f"  max in flight: {report['max_inflight']}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="https://127.0.0.1:8445")
    parser.add_argument("-n", "--samples", type=int, default=20)
    parser.add_argument("--interval", type=float, default=0.5,
                        help="seconds between requests (default 0.5)")
    parser.add_argument("--session", help="session id to read the transcript from (default: the first live one)")
    parser.add_argument("--send-session", help="measure send against this throwaway session (writes to it)")
    parser.add_argument("--send-text", default="benchmark", help="text --send-session posts, unsubmitted")
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        report = run(args.base, args.samples, args.interval, args.session,
                     args.send_session, args.send_text)
    except httpx.HTTPError as exc:
        logger.error(f"❌ benchmark failed: {type(exc).__name__}: {exc}")
        return 1
    runtime_files.make_dirs(REPORT_DIR)
    target = REPORT_DIR / f"bench-{datetime.now():%Y%m%d-%H%M%S}.json"
    runtime_files.save_text(target, json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for line in render(report):
            logger.info(line)
        logger.info(f"✅ report saved to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
