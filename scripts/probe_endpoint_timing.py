"""Time the launcher's hot GET endpoints against a running webapp (#1324).

The regression check for "Jobs, Board and the homepage in under a second":
``/api/jobs`` and ``/api/board`` must answer in < 100 ms p95 on a warm
server, ``GET /`` in < 50 ms and with a 304 on revalidation. Run it against
the live tray webapp (loopback is trusted, so no token is needed) or a
worktree's own:

    .venv\\Scripts\\python.exe -m scripts.probe_endpoint_timing
    .venv\\Scripts\\python.exe -m scripts.probe_endpoint_timing --base https://127.0.0.1:8839 -n 40

It sends GETs only, one at a time, at the tabs' own poll spacing by default,
so it never loads the server harder than one open phone does. The first
request per endpoint is reported separately as ``cold`` and left out of the
percentiles.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

ENDPOINTS = ("/api/jobs", "/api/board", "/")
# The acceptance budgets (#1324), for the PASS/OVER column.
BUDGET_P95_MS = {"/api/jobs": 100.0, "/api/board": 100.0, "/": 50.0}


def _percentile(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[rank]


def probe(base: str, samples: int, interval_s: float) -> Dict[str, Dict[str, object]]:
    """Time each endpoint ``samples`` times; return per-endpoint stats."""
    results: Dict[str, Dict[str, object]] = {}
    with httpx.Client(base_url=base, verify=False, timeout=60.0) as client:
        for path in ENDPOINTS:
            timings: List[float] = []
            cold: Optional[float] = None
            etag: Optional[str] = None
            for i in range(samples + 1):
                start = time.perf_counter()
                resp = client.get(path)
                elapsed_ms = (time.perf_counter() - start) * 1000
                resp.raise_for_status()
                if i == 0:
                    cold = elapsed_ms
                    etag = resp.headers.get("etag")
                else:
                    timings.append(elapsed_ms)
                time.sleep(interval_s)
            entry: Dict[str, object] = {
                "cold_ms": round(cold or 0.0, 1),
                "p50_ms": round(statistics.median(timings), 1),
                "p95_ms": round(_percentile(timings, 95), 1),
                "max_ms": round(max(timings), 1),
                "samples": len(timings),
            }
            if path == "/":
                # Revalidation: a browser holding the page sends its ETag back.
                status = None
                if etag:
                    status = client.get(path, headers={"If-None-Match": etag}).status_code
                entry["etag"] = bool(etag)
                entry["revalidate_status"] = status
            budget = BUDGET_P95_MS[path]
            entry["budget_p95_ms"] = budget
            entry["within_budget"] = entry["p95_ms"] <= budget
            results[path] = entry
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="https://127.0.0.1:8445")
    parser.add_argument("-n", "--samples", type=int, default=30)
    parser.add_argument("--interval", type=float, default=0.5,
                        help="seconds between requests (default 0.5)")
    parser.add_argument("--json", action="store_true", help="print JSON only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per GET otherwise
    sys.stdout.reconfigure(encoding="utf-8")
    results = probe(args.base, args.samples, args.interval)
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    for path, r in results.items():
        extra = ""
        if path == "/":
            extra = f"  etag={r['etag']} revalidate={r['revalidate_status']}"
        mark = "✅" if r["within_budget"] else "⚠️"
        logger.info(
            f"{mark} {path:<11} cold {r['cold_ms']:>7} ms  p50 {r['p50_ms']:>7} ms  "
            f"p95 {r['p95_ms']:>7} ms  max {r['max_ms']:>7} ms  "
            f"(budget p95 {r['budget_p95_ms']} ms, n={r['samples']}){extra}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
