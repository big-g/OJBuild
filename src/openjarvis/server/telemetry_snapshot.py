"""Short-lived, per-application telemetry summaries shared by dashboard routes."""

import threading
import time

_setup_lock = threading.Lock()


def summary(request, db_path, session_start):
    with _setup_lock:
        state = getattr(request.app.state, "_telemetry_snapshot", None)
        if state is None:
            state = {"lock": threading.Lock(), "key": None, "until": 0, "value": None}
            request.app.state._telemetry_snapshot = state
    key = (str(db_path), session_start)
    with state["lock"]:
        if state["key"] == key and time.monotonic() < state["until"]:
            return state["value"]
        from openjarvis.telemetry.aggregator import TelemetryAggregator

        aggregator = TelemetryAggregator(db_path)
        try:
            value = aggregator.summary(since=session_start, include_breakdowns=False)
        finally:
            aggregator.close()
        state.update(key=key, until=time.monotonic() + 3, value=value)
        return value
