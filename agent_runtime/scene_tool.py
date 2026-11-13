"""Expose the active contest scene's statistics through the Tool Catalog."""

from dataclasses import dataclass
from typing import cast

from agent_core.tools import JSONValue, Tool, ToolDefinition, ToolExecutionContext
from rade_tasks.scene import SceneState


@dataclass(frozen=True)
class SceneToolContext(ToolExecutionContext):
    state: SceneState | None = None


class QuerySceneTool(Tool):
    name = "query_scene"
    concurrent = True

    def available(self, context: ToolExecutionContext) -> bool:
        return isinstance(context, SceneToolContext) and context.state is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        assert isinstance(context, SceneToolContext) and context.state is not None
        return ToolDefinition(
            name=self.name,
            description=(
                "Look up one statistic from the current task allocation scene. "
                "The user message already contains the basic scene summary. "
                "Use the candidate histograms to count tasks or workers by the "
                "number of positive-probability entries, including zero candidates. "
                "Use task or worker with an index to inspect one column or row of P."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "statistic": {"type": "string", "enum": list(context.state.statistics)},
                    "index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Required for task and worker only. Use the zero-based "
                            "column index for task or row index for worker in the original P."
                        ),
                    },
                },
                "required": ["statistic"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        assert isinstance(context, SceneToolContext) and context.state is not None
        if arguments.keys() - {"statistic", "index"}:
            raise ValueError("query_scene accepts only statistic and index")
        statistic = arguments.get("statistic")
        if not isinstance(statistic, str):
            raise ValueError("statistic must be a string")
        index = arguments.get("index")
        if "index" in arguments and type(index) is not int:
            raise ValueError("index must be an integer")
        value = context.state.query(statistic, index)
        return {"statistic": statistic, "value": cast(JSONValue, value)}
