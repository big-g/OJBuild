"""Owner-scoped chat access to the existing approved workflow job service."""

import json
from datetime import datetime, timezone

from pydantic import Field

from openjarvis.artifacts.store import ArtifactError
from openjarvis.core.correlation import current_identity
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec
from openjarvis.workflow.jobs import StrictModel


class ListInputs(StrictModel):
    pass


class SubmitInputs(StrictModel):
    workflow_id: str = Field(min_length=1, max_length=36)
    revision: int = Field(ge=1)
    prompt: str = Field(default="", max_length=4000)
    artifact_id: str | None = Field(default=None, max_length=36)


class StatusInputs(StrictModel):
    run_id: str = Field(min_length=1, max_length=36)


CONTRACTS = {
    "workflow_list": (
        ListInputs,
        (
            "List the current user's saved workflows and revisions before "
            "selecting one to run."
        ),
    ),
    "workflow_submit": (
        SubmitInputs,
        (
            "Start a saved, approved workflow for the current user. Use the exact "
            "id and revision from workflow_list. Optional prompt or existing "
            "artifact_id supplies input. Returns a queued job, not a completed "
            "output. Do not resubmit after an uncertain response; check run "
            "history in Workflows."
        ),
    ),
    "workflow_status": (
        StatusInputs,
        (
            "Retrieve the current user's workflow run status, errors and output "
            "artifacts by run_id. Do not claim completion until status is "
            "completed. Files are available in Workflows and Files."
        ),
    ),
}


class WorkflowChatTool(BaseTool):
    def __init__(self, service=None, name=None):
        self.service = service
        if name is not None:
            self.tool_id = name

    @property
    def spec(self):
        model, description = CONTRACTS[self.tool_id]
        return ToolSpec(
            name=self.tool_id,
            description=description,
            parameters=model.model_json_schema(),
            required_capabilities=["file:read", "file:write"]
            if self.tool_id == "workflow_submit"
            else ["file:read"],
            evidence_kinds=["fact", "current", "external"],
        )

    def execute(self, **params):
        identity = current_identity()
        if identity is None or not identity.user_id:
            return ToolResult(
                tool_name=self.tool_id,
                success=False,
                content="Authenticated user required",
            )
        if self.service is None:
            return ToolResult(
                tool_name=self.tool_id,
                success=False,
                content="Workflow service unavailable",
            )
        owner = identity.user_id
        try:
            values = CONTRACTS[self.tool_id][0].model_validate(params).model_dump()
            self.service.check_owner(owner)
            if self.tool_id == "workflow_list":
                value = {"workflows": self.service.store.definitions(owner)}
            elif self.tool_id == "workflow_status":
                value = self.service.store.run(owner, values["run_id"])
            else:
                inputs = {"prompt": values["prompt"]}
                if values["artifact_id"]:
                    inputs["artifact_id"] = values["artifact_id"]
                value = self.service.submit(
                    owner, values["workflow_id"], values["revision"], inputs
                )
            content = json.dumps(value)
            return ToolResult(
                tool_name=self.tool_id,
                success=True,
                content=content,
                metadata={
                    "evidence": {
                        "provider": "workflow",
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "records": [
                            {"source": "workflow:" + self.tool_id, "content": content}
                        ],
                    }
                },
            )
        except KeyError:
            message = "Workflow, run or file not found for the current user"
        except (ValueError, ArtifactError):
            message = (
                "Workflow request rejected. Check the saved revision, "
                "input and operation approvals in Workflows."
            )
        return ToolResult(tool_name=self.tool_id, success=False, content=message)


@ToolRegistry.register("workflow_list")
class WorkflowListTool(WorkflowChatTool):
    tool_id = "workflow_list"


@ToolRegistry.register("workflow_submit")
class WorkflowSubmitTool(WorkflowChatTool):
    tool_id = "workflow_submit"


@ToolRegistry.register("workflow_status")
class WorkflowStatusTool(WorkflowChatTool):
    tool_id = "workflow_status"


def workflow_tools(service):
    return [
        cls(service)
        for cls in (WorkflowListTool, WorkflowSubmitTool, WorkflowStatusTool)
    ]
