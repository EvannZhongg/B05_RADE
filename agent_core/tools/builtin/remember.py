"""Submit a candidate to the Runtime's long-term memory manager."""

from ...memory import MemoryCandidate
from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


class RememberTool(Tool):
    name = "remember"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.memory is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Submit one durable preference, stable fact, or decision "
                "for the current workspace. The Runtime reconciles candidates "
                "with existing workspace memory after the turn succeeds. "
                "Correct outdated memory when verified information supersedes it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["preference", "fact", "decision"],
                    },
                    "content": {
                        "type": "string",
                        "description": "A concise, self-contained memory candidate.",
                    },
                },
                "required": ["kind", "content"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        if context.memory is None:
            raise RuntimeError("memory manager is unavailable")
        unknown = set(arguments) - {"kind", "content"}
        if unknown:
            raise ValueError(
                f"unknown remember argument(s): {', '.join(sorted(unknown))}"
            )
        kind = arguments.get("kind")
        content = arguments.get("content")
        if kind not in {"preference", "fact", "decision"}:
            raise ValueError("kind must be preference, fact, or decision")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        context.memory.remember(
            MemoryCandidate(kind=kind, content=content.strip())
        )
        return {"accepted": True, "kind": kind}
