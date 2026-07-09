"""The ``http-check`` job-kind's actual probe (issue #70).

Invoked as ``python -m src.jobs_kinds.http_check_probe`` by
:class:`~src.jobs_kinds.http_check.HttpCheckKind`. Deliberately a plain
script with no dependency on the rest of the Jobs-tab machinery — its
stdout is captured verbatim into the run's ``output.log`` by the executor,
exactly like any other job's script output.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jobs-tab http-check probe")
    parser.add_argument("--url", required=True)
    parser.add_argument("--method", default="GET")
    parser.add_argument("--expect-status", type=int, default=200)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    started = time.monotonic()
    try:
        response = httpx.request(
            args.method, args.url, timeout=args.timeout, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        elapsed = time.monotonic() - started
        print(f"❌ {args.method} {args.url} failed after {elapsed:.2f}s: {exc}")
        return 1

    elapsed = time.monotonic() - started
    body_tail = response.text[-500:] if response.text else ""
    ok = response.status_code == args.expect_status
    marker = "✅" if ok else "❌"
    print(
        f"{marker} {args.method} {args.url} → {response.status_code} "
        f"(expected {args.expect_status}) in {elapsed:.2f}s"
    )
    if body_tail:
        print("--- body tail ---")
        print(body_tail)
    return 0 if ok else 1


if __name__ == "__main__":
    # A piped/redirected stdout (the executor's normal spawn — no console)
    # falls back to cp1252 on Windows, which throws UnicodeEncodeError on
    # the emoji markers above even though they print fine in a real
    # terminal. See global CLAUDE.md "Windows Python: UTF-8 stdout under
    # capture".
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
