"""Register strategies and validate the configurations exposed to the Agent."""

import json
from copy import deepcopy

from jsonschema import Draft202012Validator, ValidationError

from .base import Strategy, StrategyProfile


BUILTIN_STRATEGIES = (
    Strategy(
        strategy_id="marginal_greedy",
        version="1.0.0",
        description=(
            "Deterministic baseline. Visit processing units in their original row order "
            "and assign each to the task with the largest positive increase in expected value."
        ),
        entrypoint="rade_tasks.algorithms.implementations.marginal_greedy:solve",
        parameter_schema={"type": "object", "additionalProperties": False},
        profiles={"default": StrategyProfile(parameters={}, seed=0, timeout_seconds=30)},
    ),
)


class StrategyRegistry:
    def __init__(self, strategies: tuple[Strategy, ...], *, max_timeout_seconds: int) -> None:
        self._strategies: dict[str, Strategy] = {}
        self._max_timeout_seconds = max_timeout_seconds
        for strategy in strategies:
            strategy_id = strategy.strategy_id
            if not strategy_id or strategy_id in self._strategies:
                raise ValueError(f"Invalid or duplicate strategy_id: {strategy_id!r}")
            Draft202012Validator.check_schema(strategy.parameter_schema)
            self._strategies[strategy_id] = strategy

    def discover(self) -> list[dict]:
        strategies = []
        for strategy in self._strategies.values():
            configs = []
            for config_id, profile in strategy.profiles.items():
                try:
                    self._validate_profile(strategy, profile)
                except ValueError:
                    continue
                configs.append({
                    "config_id": config_id,
                    "parameters": deepcopy(profile.parameters),
                    "seed": profile.seed,
                    "timeout_seconds": profile.timeout_seconds,
                })
            strategies.append({
                "strategy_id": strategy.strategy_id,
                "version": strategy.version,
                "description": strategy.description,
                "parameter_schema": deepcopy(strategy.parameter_schema),
                "configs": configs,
            })
        return strategies

    def resolve(self, strategy_id: str, config_id: str) -> tuple[Strategy, StrategyProfile]:
        strategy = self._strategies.get(strategy_id)
        if strategy is None:
            raise ValueError(f"Unknown strategy_id: {strategy_id}")
        profile = strategy.profiles.get(config_id)
        if profile is None:
            raise ValueError(f"Unknown config_id for strategy {strategy_id}: {config_id}")
        try:
            self._validate_profile(strategy, profile)
        except ValueError as error:
            raise ValueError(f"Invalid profile {strategy_id}/{config_id}: {error}") from error
        return strategy, profile

    def _validate_profile(self, strategy: Strategy, profile: StrategyProfile) -> None:
        if type(profile.seed) is not int or profile.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        timeout = profile.timeout_seconds
        if type(timeout) is not int or not 1 <= timeout <= self._max_timeout_seconds:
            raise ValueError(f"timeout_seconds must be an integer in [1, {self._max_timeout_seconds}]")
        try:
            Draft202012Validator(strategy.parameter_schema).validate(profile.parameters)
            json.dumps(profile.parameters, allow_nan=False)
        except (ValidationError, TypeError) as error:
            raise ValueError(str(error)) from error
