"""Data-availability watermark helper + its wiring into the presence tools.

Regression guard for the production incident where a coaching brief read an
agent's last recorded presence session as her "logout" while the Genesys
users/details watermark was hours behind, silently truncating her evening.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from genesys_mcp import _availability
from genesys_mcp._availability import presence_data_availability


@pytest.fixture(autouse=True)
def _passthrough_to_dict(monkeypatch):
    # The real to_dict routes through get_api().sanitize_for_serialization,
    # which needs an initialised SDK client. The fake API already returns
    # plain dicts, so pass them through.
    monkeypatch.setattr(_availability, "to_dict", lambda obj: obj)


class _FakeAvailabilityApi:
    """Minimal stand-in exposing only the availability endpoint."""

    def __init__(self, watermark: str | None = None, raise_exc: bool = False):
        self._watermark = watermark
        self._raise = raise_exc

    def get_analytics_users_details_jobs_availability(self):
        if self._raise:
            raise RuntimeError("boom")
        return {"dataAvailabilityDate": self._watermark} if self._watermark else {}


def _end(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


class TestPresenceDataAvailability:
    def test_complete_when_watermark_at_or_past_end(self):
        api = _FakeAvailabilityApi("2026-07-13T14:00:00Z")
        out = presence_data_availability(api, _end("2026-07-13T14:00:00"))
        assert out["complete"] is True
        assert out["lag_seconds"] == 0
        assert out["note"] is None
        assert out["data_available_until"] == "2026-07-13T14:00:00Z"

    def test_incomplete_when_watermark_behind_end(self):
        # The Deanna incident: watermark 07:12Z, window ends 14:00Z next-day close.
        api = _FakeAvailabilityApi("2026-07-13T07:12:12Z")
        out = presence_data_availability(api, _end("2026-07-13T14:00:00"))
        assert out["complete"] is False
        assert out["data_available_until"] == "2026-07-13T07:12:12Z"
        assert out["lag_seconds"] == pytest.approx(24468, abs=2)
        assert "not yet available" in out["note"]

    def test_unknown_when_endpoint_errors(self):
        api = _FakeAvailabilityApi(raise_exc=True)
        out = presence_data_availability(api, _end("2026-07-13T14:00:00"))
        assert out["complete"] is None
        assert out["data_available_until"] is None
        assert out["lag_seconds"] is None
        assert "Could not read" in out["note"]

    def test_unknown_when_field_missing(self):
        api = _FakeAvailabilityApi(watermark=None)
        out = presence_data_availability(api, _end("2026-07-13T14:00:00"))
        assert out["complete"] is None
