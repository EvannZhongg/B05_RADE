import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from agent_core import (
    AgentCancelled,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    MemoryCandidate,
    MemoryDocument,
    MemoryManager,
    MemoryReconciler,
    MemoryStore,
    RememberTool,
    Session,
    ToolExecutionContext,
    Workspace,
)


class ReconcilerProvider(LLMProvider):
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.requests: list[LLMRequest] = []

    @property
    def max_context_tokens(self) -> int:
        return 10000

    def count_input_tokens(self, request: LLMRequest) -> int:
        return len(str(request.messages[0].content).split())

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.requests.append(request)
        content = next(self.responses)
        on_text_delta(content)
        return LLMResponse(content=content)


class MemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.run_id = "project"
        self.store = MemoryStore(self.root / "runs")
        self.store.initialize(self.run_id)

    def manager(self, provider: LLMProvider) -> MemoryManager:
        return MemoryManager(
            self.run_id,
            self.store,
            MemoryReconciler(
                provider,
                prompt="run prompt",
                max_tokens=3000,
            ),
        )

    def test_independent_store_waits_for_the_memory_lock(self) -> None:
        other = MemoryStore(self.store.runs_directory)
        attempted, finished = Event(), Event()
        expected = self.store.load(self.run_id)

        def load_memory():
            attempted.set()
            try:
                return other.load(self.run_id)
            finally:
                finished.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store._locked_files():
                result = pool.submit(load_memory)
                self.assertTrue(attempted.wait(2))
                self.assertFalse(finished.wait(0.1))
            self.assertEqual(result.result(timeout=5), expected)

    def test_remember_only_submits_a_candidate(self) -> None:
        provider = ReconcilerProvider([])
        manager = self.manager(provider)
        context = ToolExecutionContext(
            workspace=Workspace(self.workspace),
            session=Session(),
            memory=manager,
        )

        tool = RememberTool()
        self.assertNotIn("scope", tool.definition(context).parameters["properties"])
        with self.assertRaisesRegex(ValueError, "unknown remember argument"):
            tool.execute({"kind": "fact", "scope": "global", "content": "Unused."}, context)
        result = tool.execute(
            {
                "kind": "preference",
                "content": "Prefer concise answers.",
            },
            context,
        )

        self.assertEqual(result, {
            "accepted": True,
            "kind": "preference",
        })
        self.assertEqual(
            manager.pending,
            (MemoryCandidate("preference", "Prefer concise answers."),),
        )
        self.assertNotIn(
            "Prefer concise answers",
            self.store.run_path(self.run_id).read_text(encoding="utf-8"),
        )

    def test_batches_candidates_and_keeps_runs_isolated(self) -> None:
        provider = ReconcilerProvider([json.dumps({
            "entries": [
                {"kind": "preference", "content": "Prefer concise answers."},
                {"kind": "decision", "content": "Use Bridge as the only runtime channel."},
            ]
        })])
        manager = self.manager(provider)
        manager.remember(MemoryCandidate("preference", "Be concise."))
        manager.remember(MemoryCandidate("decision", "Use Bridge."))

        self.assertTrue(manager.reconcile_pending())

        self.assertEqual(len(provider.requests), 1)
        request = provider.requests[0]
        payload = json.loads(request.messages[0].content)
        self.assertEqual(payload, {
            "current_memory": [],
            "candidates": [
                {"kind": "preference", "content": "Be concise."},
                {"kind": "decision", "content": "Use Bridge."},
            ],
        })
        self.assertIsNone(request.max_generation_tokens)
        document = self.store.load(self.run_id)
        self.assertEqual(document.preferences, ("Prefer concise answers.",))
        self.assertEqual(document.decisions, ("Use Bridge as the only runtime channel.",))
        self.assertTrue(self.store.load("other").is_empty())

    def test_consecutive_reconciliations_use_the_latest_stored_memory(self) -> None:
        provider = ReconcilerProvider([
            json.dumps({
                "entries": [
                    {"kind": "fact", "content": "First durable fact."},
                ]
            }),
            json.dumps({
                "entries": [
                    {"kind": "fact", "content": "First durable fact."},
                    {"kind": "fact", "content": "Second durable fact."},
                ]
            }),
        ])
        manager = self.manager(provider)

        manager.remember(MemoryCandidate("fact", "First fact."))
        self.assertTrue(manager.reconcile_pending())
        manager.remember(MemoryCandidate("fact", "Second fact."))
        self.assertTrue(manager.reconcile_pending())

        second_payload = json.loads(provider.requests[1].messages[0].content)
        self.assertEqual(
            second_payload["current_memory"],
            [{"kind": "fact", "content": "First durable fact."}],
        )
        self.assertEqual(
            self.store.load(self.run_id).facts,
            ("First durable fact.", "Second durable fact."),
        )

    def test_parallel_managers_preserve_both_memory_updates(self) -> None:
        class MergingProvider(ReconcilerProvider):
            def __init__(self) -> None:
                super().__init__([])

            def stream(self, request, on_text_delta, on_reasoning_delta=None):
                payload = json.loads(request.messages[0].content)
                time.sleep(0.02)
                content = json.dumps({
                    "entries": [
                        *payload["current_memory"],
                        *payload["candidates"],
                    ],
                })
                return LLMResponse(content=content)

        provider = MergingProvider()
        managers = [
            MemoryManager(
                self.run_id,
                self.store,
                MemoryReconciler(
                    provider,
                    prompt="run prompt",
                    max_tokens=3000,
                ),
            )
            for _ in range(2)
        ]
        for index, manager in enumerate(managers):
            manager.remember(MemoryCandidate("fact", f"Fact {index}."))
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(
                list(pool.map(lambda manager: manager.reconcile_pending(), managers)),
                [True, True],
            )
        self.assertCountEqual(
            self.store.load(self.run_id).facts,
            ("Fact 0.", "Fact 1."),
        )

    def test_token_limits_are_only_rendered_as_prompt_guidance(self) -> None:
        provider = ReconcilerProvider([
            json.dumps({
                "entries": [
                    {"kind": "fact", "content": "A durable fact."},
                ]
            })
        ])
        reconciler = MemoryReconciler(
            provider,
            prompt=(
                "Keep this around {{max_tokens}} tokens."
            ),
            max_tokens=34,
        )

        reconciler.reconcile(
            MemoryDocument(),
            (MemoryCandidate("fact", "Remember this."),),
        )

        self.assertIn("around 34 tokens", provider.requests[0].system_prompt)
        self.assertIsNone(provider.requests[0].max_generation_tokens)

    def test_invalid_reconciliation_is_recorded(self) -> None:
        calls = []
        reconciler = MemoryReconciler(
            ReconcilerProvider(["not JSON"]),
            prompt="run",
            max_tokens=3000,
            on_auxiliary_call=calls.append,
        )
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            reconciler.reconcile(
                MemoryDocument(),
                (MemoryCandidate("fact", "A fact."),),
            )
        self.assertEqual((calls[0].task, calls[0].scope, calls[0].status),
                         ("memory", "run", "invalid_response"))

    def test_empty_provider_error_is_recorded_as_null(self) -> None:
        class FailingProvider(ReconcilerProvider):
            def stream(self, request, on_text_delta, on_reasoning_delta=None):
                raise RuntimeError()

        calls = []
        reconciler = MemoryReconciler(
            FailingProvider([]),
            prompt="run",
            max_tokens=3000,
            on_auxiliary_call=calls.append,
        )
        with self.assertRaises(RuntimeError):
            reconciler.reconcile(
                MemoryDocument(),
                (MemoryCandidate("fact", "A fact."),),
            )
        self.assertEqual(calls[0].status, "failed")
        self.assertIsNone(calls[0].error)

    def test_reconciliation_cancellation_is_recorded_and_propagated(self) -> None:
        for exception in (AgentCancelled, KeyboardInterrupt):
            with self.subTest(exception=exception):
                class CancellingProvider(ReconcilerProvider):
                    def stream(self, request, on_text_delta, on_reasoning_delta=None):
                        raise exception()

                calls = []
                reconciler = MemoryReconciler(
                    CancellingProvider([]),
                    prompt="run",
                    max_tokens=3000,
                    on_auxiliary_call=calls.append,
                )
                with self.assertRaises(exception):
                    reconciler.reconcile(
                        MemoryDocument(),
                        (MemoryCandidate("fact", "A fact."),),
                    )
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].status, "cancelled")

    def test_store_round_trips_stable_markdown(self) -> None:
        self.store.write(
            self.run_id,
            MemoryDocument(facts=("A stable fact.",), decisions=("A project decision.",)),
        )

        loaded = self.store.load(self.run_id)

        self.assertEqual(loaded.facts, ("A stable fact.",))
        self.assertEqual(
            loaded.decisions,
            ("A project decision.",),
        )
        self.assertIn(
            "# RADE Run Memory",
            self.store.run_path(self.run_id).read_text(),
        )
        self.assertIn(
            "A project decision.",
            self.store.run_path(self.run_id).read_text(),
        )

    def test_load_all_returns_every_run_memory(self) -> None:
        other = "other"
        self.store.write(
            self.run_id,
            MemoryDocument(facts=("First project.",)),
        )
        self.store.write(
            other,
            MemoryDocument(decisions=("Use SQLite.",)),
        )

        runs = self.store.load_all()

        self.assertEqual(
            runs[self.run_id].facts,
            ("First project.",),
        )
        self.assertEqual(
            runs[other].decisions,
            ("Use SQLite.",),
        )

    def test_each_run_uses_its_own_memory_file(self) -> None:
        other = "other"
        self.store.write(
            self.run_id,
            MemoryDocument(facts=("First project.",)),
        )
        self.store.write(
            other,
            MemoryDocument(facts=("Second project.",)),
        )

        first_path = self.store.run_path(self.run_id)
        second_path = self.store.run_path(other)

        self.assertNotEqual(first_path.parent, second_path.parent)
        self.assertEqual(first_path.name, "MEMORY.md")
        self.assertEqual(second_path.name, "MEMORY.md")
        self.assertIn("First project.", first_path.read_text(encoding="utf-8"))
        self.assertNotIn("Second project.", first_path.read_text(encoding="utf-8"))
        self.assertIn("Second project.", second_path.read_text(encoding="utf-8"))

    def test_prompt_states_memory_priority(self) -> None:
        self.store.write(
            self.run_id,
            MemoryDocument(preferences=("Use Chinese.",)),
        )

        section = self.store.load(self.run_id).prompt_section()

        self.assertIn("current request has highest priority", section)
        self.assertIn("run instructions and project rules come next", section)
        self.assertIn("Use Chinese.", section)

    def test_empty_memory_still_states_priority_when_enabled(self) -> None:
        section = self.store.load(self.run_id).prompt_section()

        self.assertIn("current request has highest priority", section)
        self.assertIn("No long-term memory is currently recorded", section)

    def test_rejects_a_stale_write_without_losing_new_memory(self) -> None:
        stale = self.store.load(self.run_id)
        self.store.write(
            self.run_id,
            MemoryDocument(facts=("Newer fact.",)),
        )

        with self.assertRaisesRegex(RuntimeError, "changed during reconciliation"):
            self.store.write(
                self.run_id,
                MemoryDocument(facts=("Stale fact.",)),
                expected=stale,
            )

        self.assertEqual(
            self.store.load(self.run_id).facts,
            ("Newer fact.",),
        )


if __name__ == "__main__":
    unittest.main()
