"""GET /api/life-os/channels is served from a snapshot; a write must show on the next read."""

from __future__ import annotations

# The two-profile client and its recording spawn live beside the launch tests.
from tests.unit.launching.test_channel_profiles import _bypass_gate, channels  # noqa: F401


class TestChannelListSnapshot:
    """GET /api/life-os/channels is served from a snapshot; a write here
    must show on the very next read, not after the snapshot ages out."""

    def test_an_edit_shows_on_the_next_read(self, channels):
        before = channels.client.get("/api/life-os/channels").json()
        assert [p["label"] for p in before["profiles"]] == ["Health", "School"]
        resp = channels.client.patch(
            "/api/life-os/channels/health", json={"label": "Health bot"}
        )
        assert resp.status_code == 200, resp.text
        after = channels.client.get("/api/life-os/channels").json()
        assert [p["label"] for p in after["profiles"]] == ["Health bot", "School"]

    def test_a_launch_shows_as_running_on_the_next_read(self, channels):
        before = channels.client.get("/api/life-os/channels").json()
        assert [p["running"] for p in before["profiles"]] == [False, False]
        assert channels.client.post("/api/life-os/channels/health/launch", json={}).status_code == 200
        after = channels.client.get("/api/life-os/channels").json()
        assert [p["running"] for p in after["profiles"]] == [True, False]
