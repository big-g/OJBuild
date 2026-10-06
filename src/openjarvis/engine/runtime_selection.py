"""One explicit selection contract for chat, managed ticks and scheduled queries."""

from openjarvis.engine.configured_models import ConfiguredModelEngine, preserve_wrappers
from openjarvis.engine.connection_store import ModelConnectionStore
from openjarvis.engine.task_routing import TASKS, TaskRoutingStore


def connection_store(runtime):
    existing = getattr(runtime, "model_connection_store", None)
    if existing is not None:
        return existing
    config = getattr(runtime, "config", None)
    security = getattr(config, "security", None)
    path = getattr(security, "model_connections_db_path", "")
    if not path:
        raise ValueError("Configured model storage is unavailable")
    return ModelConnectionStore(path)


def resolve_selection(runtime, engine, selected, *, tools=False, images=False):
    if selected.startswith("task/"):
        task = selected[5:]
        if task not in TASKS:
            raise ValueError("Choose a known task assignment")
        decision, bound = TaskRoutingStore(connection_store(runtime)).resolve(
            task,
            tools=tools,
            images=images,
            live=True,
        )
    elif selected.startswith("oj/"):
        bound = ConfiguredModelEngine(connection_store(runtime), selected)
        bound.check(tools=tools, images=images, live=True)
        decision = {
            "mode": "manual",
            "model": selected,
            "connection_revision": bound.connection["revision"],
            "reason": "Explicit server-bound selection; fallback disabled",
        }
    else:
        return engine, selected, None
    return preserve_wrappers(engine, bound), decision["model"], decision
