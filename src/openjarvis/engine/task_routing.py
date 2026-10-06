"""Explicit task assignments gated by revision-bound diagnostic measurements."""

from __future__ import annotations

import json
import time
import uuid

from openjarvis.engine.configured_models import ConfiguredModelEngine, model_id
from openjarvis.engine.connection_store import ConnectionConflict

TASKS = ("general", "coding", "analysis", "vision")
SUITE_VERSION = "behavior-v2"


class TaskRoutingStore:
    def __init__(self, connections):
        self.connections = connections

    @staticmethod
    def _public(row):
        value = dict(row)
        if "enabled" in value:
            value["enabled"] = bool(value["enabled"])
        if "passed" in value:
            value["passed"] = bool(value["passed"])
            value["details"] = json.loads(value["details"])
        return value

    def rules(self):
        with self.connections.connection() as db:
            return [
                self._public(r)
                for r in db.execute("SELECT * FROM model_task_rules ORDER BY task")
            ]

    def benchmarks(self):
        with self.connections.connection() as db:
            return [
                self._public(r)
                for r in db.execute(
                    "SELECT * FROM model_benchmarks ORDER BY seq DESC LIMIT 100"
                )
            ]

    def record(self, connection, serving_id, task, result, actor):
        if task not in TASKS:
            raise ValueError("Unknown routing task")
        with self.connections.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # A stale run never validates a changed endpoint or catalog.
            self.connections._row(db, connection["id"], connection["revision"])
            identity = uuid.uuid4().hex
            db.execute(
                "INSERT INTO model_benchmarks "
                "(id,connection_id,connection_revision,model_id,task,suite_version,"
                "passed,details,elapsed_ms,tokens,actor,timestamp) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    connection["id"],
                    connection["revision"],
                    model_id(connection["id"], serving_id),
                    task,
                    SUITE_VERSION,
                    int(result["passed"]),
                    json.dumps(result["details"], allow_nan=False),
                    result["elapsed_ms"],
                    result["tokens"],
                    actor,
                    time.time(),
                ),
            )
            return self._public(
                db.execute(
                    "SELECT * FROM model_benchmarks WHERE id=?", (identity,)
                ).fetchone()
            )

    def _valid(self, db, rule):
        bench = db.execute(
            "SELECT * FROM model_benchmarks WHERE id=?", (rule["benchmark_id"],)
        ).fetchone()
        if (
            bench is None
            or not bench["passed"]
            or bench["task"] != rule["task"]
            or bench["model_id"] != rule["model_id"]
            or bench["suite_version"] != SUITE_VERSION
        ):
            raise ValueError(
                "Assign a passing diagnostic benchmark for this task/model"
            )
        latest = db.execute(
            "SELECT id FROM model_benchmarks WHERE model_id=? AND task=? "
            "ORDER BY seq DESC LIMIT 1",
            (rule["model_id"], rule["task"]),
        ).fetchone()
        if latest["id"] != bench["id"]:
            raise ValueError("Benchmark was superseded. Review the latest result")
        row = self.connections._row(
            db, bench["connection_id"], bench["connection_revision"]
        )
        if (
            not row["enabled"]
            or row["discovery_state"] != "discovered"
            or row["adapter_id"] != "ollama"
            or row["config_version"] != 1
        ):
            raise ValueError("Assigned model connection is disabled or unsupported")
        catalog = json.loads(row["catalog"])
        entry = next(
            (
                m
                for m in catalog
                if model_id(row["id"], m["serving_id"]) == rule["model_id"]
            ),
            {},
        )
        required = (
            {"completion", "vision"} if rule["task"] == "vision" else {"completion"}
        )
        if entry.get("capability_state") != "reported" or not required.issubset(
            entry.get("capabilities", [])
        ):
            raise ValueError("Assigned model no longer reports required capabilities")
        return self._public(bench), self.connections.public(row)

    def update(
        self,
        task,
        revision,
        enabled,
        selected,
        benchmark_id,
        actor,
        fallback_model_id="",
        fallback_benchmark_id="",
    ):
        if task not in TASKS:
            raise ValueError("Choose general, coding, analysis or vision")
        with self.connections.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM model_task_rules WHERE task=?", (task,)
            ).fetchone()
            if row["revision"] != revision:
                raise ConnectionConflict(
                    "Task assignment changed. Reload and try again"
                )
            candidate = {
                "task": task,
                "model_id": selected,
                "benchmark_id": benchmark_id,
            }
            if enabled:
                self._valid(db, candidate)
                if fallback_model_id:
                    if fallback_model_id == selected:
                        raise ValueError("Fallback must be a different model identity")
                    self._valid(
                        db,
                        {
                            "task": task,
                            "model_id": fallback_model_id,
                            "benchmark_id": fallback_benchmark_id,
                        },
                    )
                elif fallback_benchmark_id:
                    raise ValueError("Choose a fallback model for its benchmark")
            db.execute(
                "UPDATE model_task_rules SET revision=revision+1,enabled=?,"
                "model_id=?,benchmark_id=?,actor=?,updated_at=?,"
                "fallback_model_id=?,fallback_benchmark_id=? WHERE task=?",
                (
                    int(enabled),
                    selected,
                    benchmark_id,
                    actor,
                    time.time(),
                    fallback_model_id,
                    fallback_benchmark_id,
                    task,
                ),
            )
            db.execute(
                "INSERT INTO model_routing_audit (task,revision,event,actor,timestamp) "
                "VALUES (?,?,?,?,?)",
                (
                    task,
                    revision + 1,
                    "enabled" if enabled else "disabled",
                    actor,
                    time.time(),
                ),
            )
        return next(r for r in self.rules() if r["task"] == task)

    def resolve(self, task, *, tools=False, images=False, live=False):
        if task not in TASKS:
            raise ValueError("Unknown routing task")
        with self.connections.connection() as db:
            db.execute("BEGIN")
            rule = db.execute(
                "SELECT * FROM model_task_rules WHERE task=?", (task,)
            ).fetchone()
            if not rule["enabled"]:
                raise ValueError(
                    "Task assignment is disabled. Choose Manual model or "
                    "ask an administrator to configure this task"
                )
            bench, row = self._valid(db, rule)
            details = bench["details"]
            if tools and not details.get("tools_passed"):
                raise ValueError(
                    "Assigned model has no passing tool-call diagnostic. "
                    "Run benchmarks or choose Manual model"
                )
            if images and rule["task"] != "vision":
                raise ValueError("Use the Vision assignment or Manual model for images")
            decision = {
                "mode": "task",
                "task": task,
                "rule_revision": rule["revision"],
                "model": rule["model_id"],
                "benchmark_id": bench["id"],
                "connection_revision": row["revision"],
                "suite_version": SUITE_VERSION,
                "reason": f"Explicit {task} assignment with a passing diagnostic; "
                "live capabilities remain required",
            }
        # Bind precisely to the connection revision validated in the transaction.
        engine = ConfiguredModelEngine(self.connections, decision["model"])
        if engine.connection["revision"] != row["revision"]:
            raise ConnectionConflict("Assigned model changed during routing. Try again")
        engine.validate_binding = lambda: self.validate_decision(decision)
        engine.diagnostic_tools_passed = bool(bench["details"].get("tools_passed"))
        engine._row()
        if live:
            from openjarvis.engine.configured_models import ConfiguredModelUnavailable

            try:
                engine.check(tools=tools, images=images, live=True)
            except ConfiguredModelUnavailable:
                if not rule["fallback_model_id"]:
                    raise
                with self.connections.connection() as db:
                    db.execute("BEGIN")
                    fallback, fallback_row = self._valid(
                        db,
                        {
                            "task": task,
                            "model_id": rule["fallback_model_id"],
                            "benchmark_id": rule["fallback_benchmark_id"],
                        },
                    )
                decision.update(
                    {
                        "primary_model": rule["model_id"],
                        "fallback_used": True,
                        "model": rule["fallback_model_id"],
                        "selected_benchmark_id": fallback["id"],
                        "connection_revision": fallback_row["revision"],
                        "reason": "Explicit fallback: primary transport unavailable "
                        "before inference; one reviewed alternative selected",
                    }
                )
                engine = ConfiguredModelEngine(self.connections, decision["model"])
                if engine.connection["revision"] != fallback_row["revision"]:
                    raise ConnectionConflict(
                        "Fallback connection changed during routing"
                    )
                engine.diagnostic_tools_passed = bool(
                    fallback["details"].get("tools_passed")
                )
                engine.validate_binding = lambda: self.validate_decision(decision)
                engine.check(tools=tools, images=images, live=True)
        return decision, engine

    def validate_decision(self, decision):
        from openjarvis.engine.configured_models import ConfiguredModelError

        try:
            with self.connections.connection() as db:
                db.execute("BEGIN")
                rule = db.execute(
                    "SELECT * FROM model_task_rules WHERE task=?", (decision["task"],)
                ).fetchone()
                if (
                    not rule["enabled"]
                    or rule["revision"] != decision["rule_revision"]
                    or rule["benchmark_id"] != decision["benchmark_id"]
                ):
                    raise ValueError("Task assignment changed")
                self._valid(db, rule)
                if decision.get("fallback_used"):
                    if (
                        rule["fallback_model_id"] != decision["model"]
                        or rule["fallback_benchmark_id"]
                        != decision["selected_benchmark_id"]
                    ):
                        raise ValueError("Fallback assignment changed")
                    self._valid(
                        db,
                        {
                            "task": rule["task"],
                            "model_id": rule["fallback_model_id"],
                            "benchmark_id": rule["fallback_benchmark_id"],
                        },
                    )
        except (KeyError, ValueError):
            raise ConfiguredModelError(
                "Task assignment or diagnostic changed. Review the task or "
                "choose Manual model; this run cannot switch servers.",
            ) from None

    def audit(self):
        with self.connections.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM model_routing_audit ORDER BY seq DESC LIMIT 100"
                )
            ]
