"""Tools for discovering strategies, submitting experiments and reading evaluations."""

from agent_core.tools import JSONValue, Tool, ToolDefinition, ToolExecutionContext

from .scene_tool import SceneToolContext


class StrategyListTool(Tool):
    name = "strategy_list"
    concurrent = True

    def available(self, context: ToolExecutionContext) -> bool:
        return isinstance(context, SceneToolContext) and context.experiments is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="List available allocation strategies, configuration IDs, parameter schemas, seeds and time budgets.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext) -> JSONValue:
        assert isinstance(context, SceneToolContext) and context.experiments is not None
        if arguments:
            raise ValueError("strategy_list takes no arguments")
        return {"strategies": context.experiments.list_strategies()}


class ExperimentRunTool(Tool):
    name = "experiment_run"

    def available(self, context: ToolExecutionContext) -> bool:
        return (
            isinstance(context, SceneToolContext)
            and context.experiments is not None and context.scene is not None
        )

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Start a background allocation experiment for the active scene using IDs from strategy_list. "
                "Returns an experiment ID and submission status. After the job result arrives, use "
                "experiment_get to read the evaluation. The experiment produces an intermediate result."
            ),
            parameters={
                "type": "object",
                "properties": {"strategy_id": {"type": "string"}, "config_id": {"type": "string"}},
                "required": ["strategy_id", "config_id"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext) -> JSONValue:
        assert isinstance(context, SceneToolContext) and context.experiments is not None
        assert context.scene is not None
        if arguments.keys() - {"strategy_id", "config_id"}:
            raise ValueError("experiment_run accepts only strategy_id and config_id")
        strategy_id, config_id = arguments.get("strategy_id"), arguments.get("config_id")
        if not isinstance(strategy_id, str) or not strategy_id or not isinstance(config_id, str) or not config_id:
            raise ValueError("strategy_id and config_id must be non-empty strings")
        turn_id = context.session.current_turn_id
        if turn_id is None:
            raise ValueError("experiment_run requires an active scene turn")
        return context.experiments.submit(strategy_id, config_id, context.scene, turn_id)


class ExperimentGetTool(Tool):
    name = "experiment_get"
    concurrent = True

    def available(self, context: ToolExecutionContext) -> bool:
        return isinstance(context, SceneToolContext) and context.experiments is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Get an experiment's status, validity, metrics, diagnostic and result reference in this run. "
                "Pass baseline_experiment_id to also compare expected values for the same scene snapshot."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "experiment_id": {"type": "string"},
                    "baseline_experiment_id": {"type": "string"},
                },
                "required": ["experiment_id"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext) -> JSONValue:
        assert isinstance(context, SceneToolContext) and context.experiments is not None
        if arguments.keys() - {"experiment_id", "baseline_experiment_id"}:
            raise ValueError("experiment_get accepts only experiment_id and baseline_experiment_id")
        experiment_id = arguments.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise ValueError("experiment_id must be a non-empty string")
        baseline = arguments.get("baseline_experiment_id")
        if "baseline_experiment_id" in arguments and (not isinstance(baseline, str) or not baseline):
            raise ValueError("baseline_experiment_id must be a non-empty string")
        return context.experiments.get(experiment_id, baseline)
