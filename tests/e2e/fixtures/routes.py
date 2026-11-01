"""Route handlers that outlive their test without failing the next one."""

from __future__ import annotations

from typing import Any, Callable

from playwright.sync_api import Error, Route


def _torn_down(exc: Error) -> bool:
    # Playwright words the same event three ways: "Response has been disposed"
    # (reading a fetched body), "Request context disposed" (fetching) and
    # "Target page, context or browser has been closed" (anything else).
    text = str(exc)
    return "disposed" in text or "has been closed" in text


def fulfill_edited_json(route: Route, edit: Callable[[Any], None]) -> None:
    """Fetch the real response, let ``edit`` change its JSON body, fulfill it.

    A handler still mid-fetch when its test ends has its response disposed by
    the context close. Playwright keeps that error and rethrows it from the
    next API call on the connection, which is the following test's fixture
    setup, far from the handler that caused it. The page is going away, so a
    handler that finds it so just stops; any other failure still raises.
    """
    try:
        resp = route.fetch()
        body = resp.json()
        edit(body)
        route.fulfill(response=resp, json=body)
    except Error as exc:
        if not _torn_down(exc):
            raise
