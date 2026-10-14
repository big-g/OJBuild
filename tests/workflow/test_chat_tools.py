"""Chat workflow access uses verified identity and existing operation approvals."""

import json

import pytest

from openjarvis.artifacts.store import ArtifactStore
from openjarvis.core.correlation import ExecutionIdentity, execution_scope
from openjarvis.tools.workflow import WorkflowChatTool
from openjarvis.workflow.jobs import WorkflowJobs


@pytest.fixture
def service(tmp_path, monkeypatch):
    jobs = WorkflowJobs(ArtifactStore(tmp_path / "files"))
    monkeypatch.setattr(jobs, "start_worker", lambda: None)
    yield jobs
    jobs.close()


def test_workflow_chat_owner_and_approval_boundaries(service):
    saved = service.store.save(
        "alice",
        {
            "version": 1,
            "name": "Inspect",
            "steps": [{"id": "inspect", "operation": "mesh_inspect", "input": "input"}],
        },
    )
    listing = WorkflowChatTool(service, "workflow_list")
    submit = WorkflowChatTool(service, "workflow_submit")
    status = WorkflowChatTool(service, "workflow_status")
    assert not listing.execute().success
    with execution_scope(ExecutionIdentity(user_id="alice")):
        assert (
            json.loads(listing.execute().content)["workflows"][0]["id"] == saved["id"]
        )
        args = {"workflow_id": saved["id"], "revision": saved["revision"]}
        assert not submit.execute(**args).success
        service.approve("admin", "mesh_inspect", True)
        assert not submit.execute(**args, owner="bob").success
        assert not submit.execute(**{**args, "revision": 99}).success
        run = submit.execute(**args)
        assert run.success
        run_id = json.loads(run.content)["id"]
        assert json.loads(status.execute(run_id=run_id).content)["status"] == "queued"
    with execution_scope(ExecutionIdentity(user_id="bob")):
        assert json.loads(listing.execute().content)["workflows"] == []
        assert not status.execute(run_id=run_id).success
        assert not submit.execute(**args).success


def test_disabled_user_cannot_read_or_submit(service):
    service.owner_check = lambda owner: False
    with execution_scope(ExecutionIdentity(user_id="disabled")):
        assert not WorkflowChatTool(service, "workflow_list").execute().success


def test_parallel_tool_dispatch_preserves_authenticated_owner(service):
    from unittest.mock import MagicMock

    from openjarvis.agents.orchestrator import OrchestratorAgent
    from openjarvis.tools.workflow import workflow_tools

    engine = MagicMock()
    engine.generate.side_effect = [
        {
            "content": "",
            "tool_calls": [
                {"id": str(i), "name": "workflow_list", "arguments": "{}"}
                for i in range(2)
            ],
        },
        {"content": "No workflows saved", "usage": {}},
    ]
    agent = OrchestratorAgent(
        engine, "test", tools=workflow_tools(service), parallel_tools=True
    )
    with execution_scope(ExecutionIdentity(user_id="alice")):
        result = agent.run("List my workflows")
    assert len(result.tool_results) == 2
    assert all(tool.success for tool in result.tool_results)


def test_failed_run_retains_safe_recovery_details_and_log_identity(
    service, monkeypatch, caplog
):
    from openjarvis.workflow.errors import GenerationError

    saved = service.store.save(
        "alice",
        {
            "version": 1,
            "name": "Inspect",
            "steps": [{"id": "inspect", "operation": "mesh_inspect", "input": "input"}],
        },
    )
    service.approve("admin", "mesh_inspect", True)

    def fail(*args):
        raise GenerationError(
            "Worker cleanup failed",
            "gpu_recovery_required",
            "Stop and verify the worker before recovery.",
        )

    monkeypatch.setattr(service, "perform", fail)
    run = service.submit("alice", saved["id"], saved["revision"], {})
    with service.store.db() as db:
        row = dict(db.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone())
    service._execute(row)
    result = service.store.run("alice", run["id"])
    assert result["status"] == "failed"
    assert result["steps"][0]["failure"]["category"] == "gpu_recovery_required"
    assert run["id"] in caplog.text


def test_runtime_binding_exposes_service_tools_and_restores_agent(service, tmp_path):
    from unittest.mock import MagicMock

    from openjarvis.agents.orchestrator import OrchestratorAgent
    from openjarvis.core.types import ToolCall
    from openjarvis.tools.runtime_manager import RuntimeToolManager

    manager = RuntimeToolManager(tmp_path / "runtime.db")
    manager.artifact_store = service.artifacts
    manager.workflow_jobs = service
    agent = OrchestratorAgent(MagicMock(), "test")
    with execution_scope(ExecutionIdentity(user_id="alice")):
        with manager.bind_agent(agent):
            assert "workflow_list" in agent._executor._tools
            result = agent._executor.execute(
                ToolCall(id="list", name="workflow_list", arguments="{}")
            )
            assert result.success
    assert "workflow_list" not in agent._executor._tools
