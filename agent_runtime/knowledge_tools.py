"""Bind the scene knowledge service to the active Agent turn."""

from typing import cast

from agent_core.tools import JSONValue, Tool, ToolDefinition, ToolExecutionContext
from rade_tasks.knowledge import EXPECTED_VALUE_EFFECTS, ReflectionProposal, ReflectionSource

from .scene_tool import SceneToolContext


class _KnowledgeTool(Tool):
    def available(self, context: ToolExecutionContext) -> bool:
        return (
            isinstance(context, SceneToolContext)
            and context.knowledge is not None and context.scene is not None and context.experiments is not None
        )


class KnowledgeQueryTool(_KnowledgeTool):
    name = "knowledge_query"
    concurrent = True

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Find verified strategy profile comparisons for scenes in the same worker capacity and "
                "candidate density categories. Same-scale evidence ranks first. Comparisons from the other "
                "task scale are candidates for testing on the current scene. Observations describe the "
                "effect of the alternative relative to the reference."
            ),
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext) -> JSONValue:
        assert isinstance(context, SceneToolContext) and context.knowledge is not None and context.scene is not None
        if arguments.keys() - {"limit"}:
            raise ValueError("knowledge_query accepts only limit")
        limit = cast(int, arguments.get("limit", 5))
        return {"knowledge": context.knowledge.query(context.scene.state, limit=limit)}


class ReflectionProposeTool(_KnowledgeTool):
    name = "reflection_propose"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Submit a hypothesis and conclusion after comparing completed experiments from this scene task. "
                "Provide two distinct strategy profiles and the measured effect on expected value of "
                "experiment_id relative to reference_experiment_id. The Runtime verifies the allocations and scores."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "conclusion": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "reference_experiment_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "experiment_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "expected_value_effect": {"type": "string", "enum": list(EXPECTED_VALUE_EFFECTS)},
                },
                "required": [
                    "hypothesis", "conclusion", "reference_experiment_id", "experiment_id", "expected_value_effect",
                ],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext) -> JSONValue:
        assert isinstance(context, SceneToolContext) and context.knowledge is not None
        assert context.scene is not None and context.experiments is not None
        task_id = context.session.current_turn_id
        if task_id is None:
            raise ValueError("reflection_propose requires an active scene turn")
        scene = context.scene
        source = ReflectionSource(context.session.session_id, task_id, scene.filename, scene.digest, scene.source_path)
        return context.knowledge.propose(
            ReflectionProposal(**arguments), source, scene.state, context.experiments.evidence,
        )


class KnowledgeExplainTool(_KnowledgeTool):
    name = "knowledge_explain"
    concurrent = True

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Inspect a knowledge revision or reflection, its validation status and source experiments.",
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "version": {"type": "integer", "minimum": 1},
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext) -> JSONValue:
        assert isinstance(context, SceneToolContext) and context.knowledge is not None
        if arguments.keys() - {"id", "version"}:
            raise ValueError("knowledge_explain accepts only id and version")
        identifier, version = arguments.get("id"), arguments.get("version")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("id must be a non-empty string")
        if "version" in arguments and (type(version) is not int or version < 1):
            raise ValueError("version must be a positive integer")
        return context.knowledge.explain(identifier.strip(), version=version)
