"""Dashboard cache coalesces concurrent requests without crossing app/session keys."""

import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from openjarvis.server.telemetry_snapshot import summary


def test_snapshot_singleflight_expiry_and_scope(monkeypatch):
    calls = []

    class Aggregator:
        def __init__(self, path):
            self.path = path

        def summary(self, **kwargs):
            assert kwargs["include_breakdowns"] is False
            calls.append((self.path, kwargs["since"]))
            time.sleep(0.01)
            return object()

        def close(self):
            pass

    monkeypatch.setattr(
        "openjarvis.telemetry.aggregator.TelemetryAggregator", Aggregator
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: summary(request, "a.db", 1), range(8)))
    assert len(calls) == 1
    assert all(value is values[0] for value in values)
    request.app.state._telemetry_snapshot["until"] = 0
    assert summary(request, "a.db", 1) is not values[0]
    summary(request, "b.db", 1)
    summary(request, "b.db", 2)
    other = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    summary(other, "b.db", 2)
    assert len(calls) == 5
