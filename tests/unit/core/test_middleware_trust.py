"""The auth trust boundary: who counts as the PC itself, and who is on the tailnet.

Loopback trust skips every other auth leg, so these pin it directly on
``app.webapp.middleware.trust`` rather than only through the HTTP stack.
"""

import pytest

from app.webapp.middleware import trust


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_without_edge_headers_is_the_pc(host):
    assert trust.is_pc_itself(host, {}) is True


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
@pytest.mark.parametrize("edge_header", ["cf-ray", "cf-connecting-ip"])
def test_edge_forwarded_request_claiming_loopback_is_never_the_pc(host, edge_header):
    assert trust.via_cloudflare({edge_header: "x"}) is True
    assert trust.is_pc_itself(host, {edge_header: "x"}) is False


@pytest.mark.parametrize("host", ["100.64.0.1", "192.168.1.10", "testclient", ""])
def test_non_loopback_is_not_the_pc(host):
    assert trust.is_pc_itself(host, {}) is False


@pytest.mark.parametrize(
    ("host", "allowlist", "expected"),
    [
        ("127.0.0.1", [], True),
        ("::1", [], True),
        ("100.64.0.1", [], True),
        ("100.127.255.254", [], True),
        ("100.128.0.1", [], False),
        ("8.8.8.8", [], False),
        ("192.168.1.10", ["192.168.1.0/24"], True),
        ("192.168.2.10", ["192.168.1.0/24"], False),
        ("testclient", [], False),
        ("testclient", ["testclient"], False),
    ],
)
def test_client_in_tailnet(host, allowlist, expected):
    assert trust.client_in_tailnet(host, allowlist) is expected


def test_package_reexports_the_trust_names():
    from app.webapp import middleware

    assert middleware.is_pc_itself is trust.is_pc_itself
    assert middleware.client_in_tailnet is trust.client_in_tailnet
    assert middleware.LOOPBACK_HOSTS is trust.LOOPBACK_HOSTS
