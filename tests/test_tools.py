import json
import os
import subprocess
import threading
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.filesystem_fixtures import symlinks_available


from agent_core import (
    ApplyPatchTool,
    CommandExecutionResult,
    EditFileTool,
    ListDirectoryTool,
    ReadFileTool,
    SearchFilesTool,
    Session,
    ShellTool,
    Tool,
    ToolCall,
    ToolCatalog,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    Workspace,
    WriteFileTool,
    builtin_catalog,
)
from agent_core.turn_control import AgentCancelled, TurnControl
from agent_core.tools.budget import MAX_TOOL_RESULT_CHARS
from agent_core.tools.text import (
    MAX_FILE_SIZE_BYTES as MAX_READ_FILE_SIZE_BYTES,
)


SYMLINKS_AVAILABLE = symlinks_available()

# Tools that never touch the filesystem still need a workspace in their
# context; the test package directory is a stable, existing one.
_TMP_WORKSPACE = Workspace(Path(__file__).parent)


class _Bound:
    """A Tool plus the context a test calls it with.

    Production code reaches tools through a ToolSet; these per-tool tests
    exercise one implementation directly, so they bind a context once.
    """

    def __init__(self, tool, workspace, **fields):
        self._tool = tool
        self._context = context_for(workspace, **fields)

    @property
    def definition(self):
        return self._tool.definition(self._context)

    def execute(self, arguments):
        return self._tool.execute(arguments, self._context)


def context_for(workspace, **fields):
    return ToolExecutionContext(
        workspace=workspace,
        session=fields.pop("session", None) or Session(),
        **fields,
    )


class FailingTool(Tool):
    name = "failing"

    def definition(self, context) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Always fails.",
            parameters={"type": "object", "properties": {}},
        )

    def execute(self, arguments, context):
        raise ValueError("bad input")


class NeedsExecutorTool(Tool):
    name = "needs_executor"

    def available(self, context) -> bool:
        return context.command_executor is not None

    def definition(self, context) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Needs a command executor.",
            parameters={"type": "object", "properties": {}},
        )

    def execute(self, arguments, context):
        return "ran"


class ConcurrentTool(FailingTool):
    name = "concurrent"
    concurrent = True


class ToolCatalogTest(unittest.TestCase):
    def test_rejects_duplicate_tool_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "already registered"):
            ToolCatalog((FailingTool(), FailingTool()))

    def test_selects_only_the_names_a_role_asks_for(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = context_for(Workspace(Path(directory)))
            catalog = builtin_catalog()

            tools = catalog.select(("read_file", "list_directory"), context)

            self.assertEqual(
                [definition.name for definition in tools.definitions],
                ["read_file", "list_directory"],
            )

    def test_skips_names_the_catalog_does_not_know(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = context_for(Workspace(Path(directory)))

            tools = builtin_catalog().select(("read_file", "nope"), context)

            self.assertEqual(
                [definition.name for definition in tools.definitions],
                ["read_file"],
            )

    def test_omits_tools_whose_runtime_dependency_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            catalog = ToolCatalog((NeedsExecutorTool(),))

            without = catalog.select(
                ("needs_executor",), context_for(workspace)
            )
            with_executor = catalog.select(
                ("needs_executor",),
                context_for(
                    workspace,
                    command_executor=UnusedExecutor(),
                ),
            )

            self.assertEqual(without.definitions, ())
            self.assertEqual(
                [d.name for d in with_executor.definitions],
                ["needs_executor"],
            )

    def test_two_roles_share_one_tool_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = context_for(Workspace(Path(directory)))
            catalog = builtin_catalog()

            reader = catalog.select(("read_file", "list_directory"), context)
            writer = catalog.select(("read_file", "edit_file"), context)

            self.assertIs(
                reader._tools["read_file"],
                writer._tools["read_file"],
            )

    def test_extend_keeps_the_original_catalog_unchanged(self) -> None:
        base = ToolCatalog((FailingTool(),))

        extended = base.extend((NeedsExecutorTool(),))

        self.assertEqual(base.names, ("failing",))
        self.assertEqual(
            extended.names, ("failing", "needs_executor")
        )


class ToolSetTest(unittest.TestCase):
    def test_invalid_arguments_skip_policy_and_execution(self):
        policy = Mock()
        tool = FailingTool()
        tool.execute = Mock()
        tools = ToolCatalog((tool,)).select(
            (tool.name,), context_for(_TMP_WORKSPACE), policy=policy,
        )

        for raw in ('{"value" "broken"}', '[]', 'null', '"text"'):
            with self.subTest(raw=raw):
                call = ToolCall.from_raw_arguments("bad-call", tool.name, raw)
                result = tools.execute(call)
                self.assertEqual(result.tool_call_id, call.id)
                self.assertEqual(result.name, call.name)
                self.assertEqual(result.error, call.argument_error)
                self.assertEqual(json.loads(result.to_content())["error"]["type"], "invalid_tool_arguments")
        policy.authorize.assert_not_called()
        tool.execute.assert_not_called()

    def test_returns_structured_execution_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools = ToolCatalog((FailingTool(),)).select(
                ("failing",), context_for(Workspace(Path(directory)))
            )

            result = tools.execute(
                ToolCall(id="call-1", name="failing", arguments={})
            )

        self.assertEqual(
            json.loads(result.to_content()),
            {
                "ok": False,
                "error": {
                    "type": "ValueError",
                    "message": "bad input",
                },
            },
        )

    def test_returns_tool_not_found_for_an_unselected_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools = builtin_catalog().select(
                ("read_file",), context_for(Workspace(Path(directory)))
            )

            result = tools.execute(
                ToolCall(id="call-1", name="shell", arguments={})
            )

        self.assertEqual(
            json.loads(result.to_content())["error"]["type"],
            "tool_not_found",
        )

    def test_returns_structured_policy_error_before_tool_execution(
        self,
    ) -> None:
        executed = []

        class RecordingTool(Tool):
            name = "recording"

            def definition(self, context) -> ToolDefinition:
                return ToolDefinition(
                    name=self.name,
                    description="Record execution.",
                    parameters={"type": "object", "properties": {}},
                )

            def execute(self, arguments, context):
                executed.append(arguments)
                return None

        class DenyPolicy:
            def authorize(self, call, context):
                raise PermissionError(f"{call.name} was denied")

        with tempfile.TemporaryDirectory() as directory:
            tools = ToolCatalog((RecordingTool(),)).select(
                ("recording",),
                context_for(Workspace(Path(directory))),
                policy=DenyPolicy(),
            )

            result = tools.execute(
                ToolCall(id="call-1", name="recording", arguments={})
            )

        self.assertEqual(executed, [])
        self.assertEqual(
            json.loads(result.to_content()),
            {
                "ok": False,
                "error": {
                    "type": "PermissionError",
                    "message": "recording was denied",
                },
            },
        )

    def test_reports_concurrency_only_for_selected_concurrent_tools(self) -> None:
        tools = ToolCatalog((ConcurrentTool(), FailingTool())).select(
            ("concurrent", "failing"), context_for(_TMP_WORKSPACE)
        )

        self.assertTrue(tools.is_concurrent("concurrent"))
        self.assertFalse(tools.is_concurrent("failing"))
        self.assertFalse(tools.is_concurrent("unknown"))


class ToolStatelessnessTest(unittest.TestCase):
    """A shared Tool must keep nothing from one invocation to the next."""

    def test_no_builtin_tool_holds_instance_state(self) -> None:
        for tool in _catalog_tools(builtin_catalog()):
            with self.subTest(tool=tool.name):
                self.assertEqual(
                    vars(tool),
                    {},
                    f"{type(tool).__name__} stores per-instance state",
                )

    def test_one_instance_serves_two_workspaces_at_once(self) -> None:
        """A shared Tool reads its workspace from the call, not from self."""
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            (Path(first) / "note.txt").write_text("first", encoding="utf-8")
            (Path(second) / "note.txt").write_text("second", encoding="utf-8")
            catalog = builtin_catalog()
            sets = {
                name: catalog.select(
                    ("read_file",), context_for(Workspace(Path(root)))
                )
                for name, root in (("first", first), ("second", second))
            }

            results = {}
            barrier = threading.Barrier(2)

            def read(name):
                barrier.wait()
                for _ in range(20):
                    results[name] = sets[name].execute(
                        ToolCall(
                            id=name,
                            name="read_file",
                            arguments={"path": "note.txt"},
                        )
                    )

            threads = [
                threading.Thread(target=read, args=(name,))
                for name in sets
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertIn("1| first", results["first"].output["content"])
            self.assertIn("1| second", results["second"].output["content"])


def _catalog_tools(catalog):
    return [catalog._tools[name] for name in catalog.names]


class UnusedExecutor:
    def execute(self, command, timeout_seconds=60):
        raise AssertionError("executor should not be called")


class ReadFileToolTest(unittest.TestCase):
    def test_reads_utf8_file_with_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "你好，RADE。\n第二行\n",
                encoding="utf-8",
                newline="\n",
            )

            result = _Bound(ReadFileTool(), workspace).execute({"path": "notes.txt"})

            self.assertEqual(
                result,
                {
                    "path": "notes.txt",
                    "file_size_bytes": len(
                        "你好，RADE。\n第二行\n".encode("utf-8")
                    ),
                    "content": (
                        "1| 你好，RADE。\n"
                        "2| 第二行\n\n"
                        "(End of file — 2 lines total)"
                    ),
                },
            )

    def test_reads_requested_line_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join(f"line {number}" for number in range(1, 6)),
                encoding="utf-8",
                newline="\n",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "offset": 2,
                    "limit": 2,
                }
            )

            self.assertEqual(
                result,
                {
                    "path": "notes.txt",
                    "file_size_bytes": len(
                        "\n".join(
                            f"line {number}" for number in range(1, 6)
                        ).encode("utf-8")
                    ),
                    "content": (
                        "2| line 2\n"
                        "3| line 3\n\n"
                        "(Showing lines 2-3. "
                        "Use offset=4 to continue.)"
                    ),
                },
            )

    def test_streams_file_without_reading_all_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "first\nsecond\nthird\n",
                encoding="utf-8",
            )

            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError(
                    "read_file must not load the whole file"
                ),
            ):
                result = _Bound(ReadFileTool(), workspace).execute(
                    {
                        "path": "notes.txt",
                        "offset": 2,
                        "limit": 1,
                    }
                )

            self.assertEqual(
                result["content"],
                "2| second\n\n"
                "(Showing line 2. Use offset=3 to continue.)",
            )

    def test_stops_at_the_line_limit_when_the_budget_allows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join(f"line {number}" for number in range(1, 2002)),
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {"path": "notes.txt", "limit": 900}
            )

            content_lines = result["content"].splitlines()
            self.assertEqual(content_lines[0], "1| line 1")
            self.assertEqual(content_lines[899], "900| line 900")
            self.assertEqual(
                content_lines[-1],
                "(Showing lines 1-900. Use offset=901 to continue.)",
            )

    def test_returns_end_marker_when_offset_reaches_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "first\nsecond\nthird\n",
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "offset": 3,
                    "limit": 10,
                }
            )

            self.assertEqual(
                result["content"],
                "3| third\n\n(End of file — 3 lines total)",
            )

    def test_returns_end_marker_for_offset_past_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "only line\n",
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {"path": "notes.txt", "offset": 2}
            )

            self.assertEqual(
                result["content"],
                "(End of file — 1 lines total)",
            )

    def test_limits_lines_and_characters_at_the_same_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join(
                    f"line {number}: " + ("x" * 1000)
                    for number in range(1, 100)
                ),
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "offset": 1,
                    "limit": 80,
                }
            )

            self.assertLessEqual(
                len(
                    ToolResult(
                        tool_call_id="call-1",
                        name="read_file",
                        output=result,
                    ).to_content()
                ),
                MAX_TOOL_RESULT_CHARS,
            )
            self.assertIn("character limit reached", result["content"])
            self.assertIn("Use offset=", result["content"])
            numbered_lines = [
                line
                for line in result["content"].splitlines()
                if "| " in line
            ]
            self.assertLess(len(numbered_lines), 80)

    def test_defers_a_line_that_does_not_fit_to_the_next_page(self) -> None:
        """Paging must not drop the tail of a line at a page boundary.

        A line the current page cannot hold is handed to the next call
        whole, so reading a file page by page reproduces it exactly.
        """
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            lines = [f"line {number}: " + "abcdefgh" * 12 for number in range(400)]
            (workspace.path / "notes.txt").write_text(
                "\n".join(lines), encoding="utf-8"
            )
            tool = _Bound(ReadFileTool(), workspace)

            recovered: list[str] = []
            offset = 1
            for _ in range(100):
                result = tool.execute(
                    {"path": "notes.txt", "offset": offset}
                )
                body = result["content"].splitlines()
                recovered += [
                    line.split("| ", 1)[1]
                    for line in body
                    if "| " in line and line.split("| ", 1)[0].isdigit()
                ]
                status = body[-1]
                if "End of file" in status:
                    break
                offset = int(status.split("offset=")[1].split()[0])
            else:
                self.fail("paging never reached the end of the file")

            self.assertEqual(recovered, lines)

    def test_paging_advances_past_a_line_no_page_can_hold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "short a\n" + ("X" * 200000) + "\nshort b",
                encoding="utf-8",
            )
            tool = _Bound(ReadFileTool(), workspace)

            statuses = []
            offset = 1
            for _ in range(20):
                result = tool.execute(
                    {"path": "notes.txt", "offset": offset}
                )
                status = result["content"].splitlines()[-1]
                statuses.append(status)
                if "End of file" in status:
                    break
                following = int(status.split("offset=")[1].split()[0])
                self.assertGreater(following, offset)
                offset = following
            else:
                self.fail("paging never reached the end of the file")

            self.assertIn("End of file — 3 lines total", statuses[-1])

    def test_truncates_an_overlong_single_line_with_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "x" * (MAX_TOOL_RESULT_CHARS * 2),
                encoding="utf-8",
            )

            result = _Bound(ReadFileTool(), workspace).execute(
                {"path": "notes.txt"}
            )

            self.assertLessEqual(
                len(
                    ToolResult(
                        tool_call_id="call-1",
                        name="read_file",
                        output=result,
                    ).to_content()
                ),
                MAX_TOOL_RESULT_CHARS,
            )
            self.assertIn("该行被截断", result["content"])
            self.assertIn("character limit reached", result["content"])

    def test_rejects_file_larger_than_size_limit_and_reports_size(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "large.txt"
            file_path.write_bytes(b"x" * 11)

            with patch(
                "agent_core.tools.text."
                "MAX_FILE_SIZE_BYTES",
                10,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "size is 11 bytes.*maximum of 10 bytes",
                ):
                    _Bound(ReadFileTool(), workspace).execute(
                        {"path": "large.txt"}
                    )

            self.assertEqual(MAX_READ_FILE_SIZE_BYTES, 50 * 1024 * 1024)

    def test_rejects_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))

            with self.assertRaisesRegex(
                ValueError,
                "must stay within the workspace",
            ):
                _Bound(ReadFileTool(), workspace).execute({"path": "../outside.txt"})

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_rejects_symlink_to_file_outside_workspace(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            workspace = Workspace(Path(directory))
            outside_file = Path(outside_directory) / "outside.txt"
            outside_file.write_text("secret", encoding="utf-8")
            os.symlink(outside_file, workspace.path / "link.txt")

            with self.assertRaisesRegex(
                ValueError,
                "must stay within the workspace",
            ):
                _Bound(ReadFileTool(), workspace).execute({"path": "link.txt"})

    def test_validates_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(ReadFileTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "non-empty string"):
                tool.execute({})
            with self.assertRaisesRegex(
                ValueError,
                "'offset' to be a positive integer",
            ):
                tool.execute({"path": "notes.txt", "offset": 0})
            with self.assertRaisesRegex(
                ValueError,
                "'offset' to be a positive integer",
            ):
                tool.execute({"path": "notes.txt", "offset": True})
            with self.assertRaisesRegex(
                ValueError,
                "'limit' to be a positive integer",
            ):
                tool.execute({"path": "notes.txt", "limit": 0})
            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute({"path": "notes.txt", "extra": True})


class WriteFileToolTest(unittest.TestCase):
    def test_creates_missing_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))

            result = _Bound(WriteFileTool(), workspace).execute(
                {
                    "path": "src/foo/bar.py",
                    "content": "print('hello')\n",
                }
            )

            self.assertEqual(
                result,
                {
                    "path": "src/foo/bar.py",
                    "created": True,
                    "created_parent_dirs": ["src", "src/foo"],
                    "bytes_written": 15,
                },
            )
            self.assertEqual(
                (workspace.path / "src/foo/bar.py").read_text(encoding="utf-8"),
                "print('hello')\n",
            )

    def test_reports_only_parent_directories_created_by_the_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "src").mkdir()

            result = _Bound(WriteFileTool(), workspace).execute(
                {"path": "src/foo/bar.py", "content": "content"}
            )

            self.assertEqual(result["created_parent_dirs"], ["src/foo"])

    def test_reports_overwrite_as_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("old", encoding="utf-8")

            result = _Bound(WriteFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "content": "new",
                    "overwrite": True,
                }
            )

            self.assertEqual(
                result,
                {
                    "path": "notes.txt",
                    "created": False,
                    "created_parent_dirs": [],
                    "bytes_written": 3,
                },
            )
            self.assertEqual(file_path.read_text(encoding="utf-8"), "new")

    def test_rejects_file_in_parent_chain_before_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "a").mkdir()
            blocking_file = workspace.path / "a/b"
            blocking_file.write_text("file", encoding="utf-8")

            with self.assertRaises(NotADirectoryError):
                _Bound(WriteFileTool(), workspace).execute(
                    {"path": "a/b/c/d.txt", "content": "content"}
                )

            self.assertEqual(blocking_file.read_text(encoding="utf-8"), "file")
            self.assertFalse((workspace.path / "a/b/c").exists())

    def test_rejects_workspace_escape_without_creating_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            outside = workspace.path.parent / f"{workspace.path.name}-outside"

            with self.assertRaisesRegex(ValueError, "within the workspace"):
                _Bound(WriteFileTool(), workspace).execute(
                    {"path": f"new/../../{outside.name}/file.txt", "content": "x"}
                )

            self.assertFalse(workspace.path.joinpath("new").exists())
            self.assertFalse(outside.exists())

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_rejects_symlink_parent_outside_workspace_without_creating(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            workspace = Workspace(Path(directory))
            outside = Path(outside_directory)
            os.symlink(outside, workspace.path / "link")

            with self.assertRaisesRegex(ValueError, "within the workspace"):
                _Bound(WriteFileTool(), workspace).execute(
                    {"path": "link/missing/file.txt", "content": "x"}
                )

            self.assertFalse((outside / "missing").exists())


class EditFileToolTest(unittest.TestCase):
    def test_replaces_one_exact_text_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("hello world\n", encoding="utf-8")

            result = _Bound(EditFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "old_text": "world",
                    "new_text": "RADE",
                }
            )

            self.assertEqual(
                result,
                {
                    "path": "notes.txt",
                    "replacements": 1,
                },
            )
            self.assertEqual(
                file_path.read_text(encoding="utf-8"),
                "hello RADE\n",
            )

    def test_rejects_missing_old_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("hello\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "was not found"):
                _Bound(EditFileTool(), workspace).execute(
                    {
                        "path": "notes.txt",
                        "old_text": "missing",
                        "new_text": "replacement",
                    }
                )

            self.assertEqual(file_path.read_text(encoding="utf-8"), "hello\n")

    def test_rejects_non_unique_old_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("same same", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "appears 2 times"):
                _Bound(EditFileTool(), workspace).execute(
                    {
                        "path": "notes.txt",
                        "old_text": "same",
                        "new_text": "changed",
                    }
                )

            self.assertEqual(
                file_path.read_text(encoding="utf-8"),
                "same same",
            )

    def test_allows_deleting_old_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("remove me", encoding="utf-8")

            _Bound(EditFileTool(), workspace).execute(
                {
                    "path": "notes.txt",
                    "old_text": "remove",
                    "new_text": "",
                }
            )

            self.assertEqual(file_path.read_text(encoding="utf-8"), " me")

    def test_requires_exact_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(EditFileTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "non-empty string 'path'"):
                tool.execute({})
            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute(
                    {
                        "path": "notes.txt",
                        "old_text": "old",
                        "new_text": "new",
                        "extra": True,
                    }
                )


class ApplyPatchToolTest(unittest.TestCase):
    def test_adds_updates_and_deletes_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "first\nold\nlast\n", encoding="utf-8"
            )
            (workspace.path / "obsolete.txt").write_text(
                "remove", encoding="utf-8"
            )

            result = _Bound(ApplyPatchTool(), workspace).execute(
                {
                    "patch": """*** Begin Patch
*** Update File: notes.txt
@@
 first
-old
+new
 last
*** Add File: added.txt
+hello
+world
*** Delete File: obsolete.txt
*** End Patch"""
                }
            )

            self.assertEqual(
                result,
                {
                    "files": [
                        {"path": "notes.txt", "operation": "updated"},
                        {"path": "added.txt", "operation": "added"},
                        {"path": "obsolete.txt", "operation": "deleted"},
                    ]
                },
            )
            self.assertEqual(
                (workspace.path / "notes.txt").read_text(encoding="utf-8"),
                "first\nnew\nlast\n",
            )
            self.assertEqual(
                (workspace.path / "added.txt").read_text(encoding="utf-8"),
                "hello\nworld\n",
            )
            self.assertFalse((workspace.path / "obsolete.txt").exists())

    def test_validates_all_changes_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("old\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "requires existing file"):
                _Bound(ApplyPatchTool(), workspace).execute(
                    {
                        "patch": """*** Begin Patch
*** Update File: notes.txt
@@
-old
+new
*** Update File: missing.txt
@@
-missing
+replacement
*** End Patch"""
                    }
                )

            self.assertEqual(file_path.read_text(encoding="utf-8"), "old\n")

    def test_rejects_ambiguous_hunk_and_workspace_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "same\nother\nsame\n", encoding="utf-8"
            )
            tool = _Bound(ApplyPatchTool(), workspace)

            with self.assertRaisesRegex(ValueError, "ambiguous"):
                tool.execute(
                    {
                        "patch": """*** Begin Patch
*** Update File: notes.txt
@@
-same
+changed
*** End Patch"""
                    }
                )
            with self.assertRaisesRegex(ValueError, "within the workspace"):
                tool.execute(
                    {
                        "patch": """*** Begin Patch
*** Add File: ../outside.txt
+nope
*** End Patch"""
                    }
                )

    def test_validates_arguments_and_patch_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(ApplyPatchTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "non-empty string"):
                tool.execute({"patch": ""})
            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute({"patch": "x", "extra": True})
            with self.assertRaisesRegex(ValueError, "must start"):
                tool.execute({"patch": "*** End Patch"})

    def test_preserves_crlf_and_non_newline_unicode_separators(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            original = (
                "first\r\nold\r\nvertical\vform\ffoo\x85bar\u2028baz\r\n"
                "last\r\n"
            ).encode("utf-8")
            file_path.write_bytes(original)

            _Bound(ApplyPatchTool(), workspace).execute(
                {
                    "patch": """*** Begin Patch
*** Update File: notes.txt
@@ -2,1 +2,1 @@
-old
+new
*** End Patch"""
                }
            )

            self.assertEqual(
                file_path.read_bytes(),
                original.replace(b"old", b"new", 1),
            )

    def test_validates_numbered_hunk_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("one\ntwo\n", encoding="utf-8")
            tool = _Bound(ApplyPatchTool(), workspace)

            with self.assertRaisesRegex(ValueError, "old line count"):
                tool.execute(
                    {
                        "patch": """*** Begin Patch
*** Update File: notes.txt
@@ -1,2 +1,1 @@
-one
+changed
*** End Patch"""
                    }
                )
            with self.assertRaisesRegex(ValueError, "invalid hunk header"):
                tool.execute(
                    {
                        "patch": """*** Begin Patch
*** Update File: notes.txt
@@ ignored text
-one
+changed
*** End Patch"""
                    }
                )
            self.assertEqual(file_path.read_text(encoding="utf-8"), "one\ntwo\n")

    def test_allows_blank_lines_between_delete_and_end_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            file_path = workspace.path / "notes.txt"
            file_path.write_text("delete me", encoding="utf-8")

            _Bound(ApplyPatchTool(), workspace).execute(
                {
                    "patch": """*** Begin Patch
*** Delete File: notes.txt

*** End Patch
"""
                }
            )

            self.assertFalse(file_path.exists())


class ListDirectoryToolTest(unittest.TestCase):
    def test_rejects_cancelled_call_before_accessing_workspace(self) -> None:
        control = TurnControl()
        control.cancel()
        tool = _Bound(ListDirectoryTool(), _TMP_WORKSPACE, cancellation=control)
        with patch.object(Workspace, "resolve_path") as resolve:
            with self.assertRaises(AgentCancelled):
                tool.execute({"path": "."})
        resolve.assert_not_called()

    def test_cancels_during_enumeration_without_matching_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "a.txt").write_text("a", encoding="utf-8")
            control = TurnControl()
            tool = _Bound(ListDirectoryTool(), workspace, cancellation=control)
            with os.scandir(workspace.path) as iterator:
                entries = list(iterator)

            def cancelling_entries():
                control.cancel()
                yield from entries

            with patch("agent_core.tools.builtin.list_directory.os.scandir") as scandir:
                scandir.return_value.__enter__.return_value = cancelling_entries()
                with self.assertRaises(AgentCancelled):
                    tool.execute({"path": ".", "glob": "*.py", "recursive": True})
                scandir.return_value.__exit__.assert_called_once()

    def test_times_out_during_enumeration_without_matching_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "a.txt").write_text("a", encoding="utf-8")
            tool = _Bound(ListDirectoryTool(), workspace)
            schema = tool.definition.parameters["properties"]["timeout_seconds"]
            self.assertEqual(schema["default"], 60)
            self.assertEqual(schema["minimum"], 1)
            with os.scandir(workspace.path) as iterator:
                entries = list(iterator)
            for timeout in (None, 2):
                with self.subTest(timeout=timeout):
                    clock = SimpleNamespace(now=0.0)

                    def slow_entries():
                        clock.now = 60.0 if timeout is None else float(timeout)
                        yield from entries

                    arguments = {"path": ".", "glob": "*.py", "recursive": True}
                    if timeout is not None:
                        arguments["timeout_seconds"] = timeout
                    with (
                        patch("agent_core.tools.builtin.list_directory.os.scandir") as scandir,
                        patch(
                            "agent_core.tools.builtin.list_directory.time.monotonic",
                            side_effect=lambda: clock.now,
                        ),
                    ):
                        scandir.return_value.__enter__.return_value = slow_entries()
                        with self.assertRaisesRegex(TimeoutError, f"after {timeout or 60} seconds"):
                            tool.execute(arguments)

    def test_checks_budget_after_empty_enumeration_and_result_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            tool = _Bound(ListDirectoryTool(), workspace)
            clock = SimpleNamespace(now=0.0)

            def slow_empty_entries():
                clock.now = 60.0
                return iter(())

            with (
                patch("agent_core.tools.builtin.list_directory.os.scandir") as scandir,
                patch(
                    "agent_core.tools.builtin.list_directory.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                scandir.return_value.__enter__.side_effect = slow_empty_entries
                with self.assertRaises(TimeoutError):
                    tool.execute({"path": "."})

            (workspace.path / "a.txt").write_text("a", encoding="utf-8")
            clock.now = 0.0

            def slow_type(_entry):
                clock.now = 60.0
                return "file"

            with (
                patch("agent_core.tools.builtin.list_directory._entry_type", side_effect=slow_type),
                patch(
                    "agent_core.tools.builtin.list_directory.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                with self.assertRaises(TimeoutError):
                    tool.execute({"path": "."})

    def test_rejects_invalid_timeouts(self) -> None:
        tool = _Bound(ListDirectoryTool(), _TMP_WORKSPACE)
        for timeout in (0, -1, True, 1.5, "1", None):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout_seconds"):
                    tool.execute({"path": ".", "timeout_seconds": timeout})

    @unittest.skipUnless(os.name == "nt", "directory junctions are a Windows feature")
    def test_lists_junctions_without_traversing_cycles_or_external_targets(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside,
        ):
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (nested / "a.txt").write_text("a", encoding="utf-8")
            (Path(outside) / "outside.txt").write_text("outside", encoding="utf-8")
            links = [workspace.path / "alias", nested / "ancestor", workspace.path / "external"]
            created = []
            try:
                for link, target in zip(links, (nested, workspace.path, Path(outside))):
                    subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                        check=True, capture_output=True,
                    )
                    created.append(link)
                result = _Bound(ListDirectoryTool(), workspace).execute(
                    {"path": ".", "glob": "**/*", "recursive": True, "timeout_seconds": 2}
                )
                self.assertEqual(
                    [entry["name"] for entry in result["entries"]],
                    ["alias", "external", "nested", "nested/a.txt", "nested/ancestor"],
                )
                self.assertFalse(result["has_more"])
            finally:
                for link in created:
                    link.rmdir()

    def test_lists_immediate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "z.txt").write_text("z", encoding="utf-8")
            (workspace.path / "a").mkdir()

            result = _Bound(ListDirectoryTool(), workspace).execute({"path": "."})

            self.assertEqual(result["path"], ".")
            self.assertCountEqual(
                result["entries"],
                [
                    {"name": "a", "type": "directory"},
                    {"name": "z.txt", "type": "file"},
                ],
            )
            self.assertFalse(result["has_more"])
            self.assertIsNone(result["next_cursor"])

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_reports_symlink_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "z.txt").write_text("z", encoding="utf-8")
            os.symlink(
                workspace.path / "z.txt",
                workspace.path / "link.txt",
            )

            result = _Bound(ListDirectoryTool(), workspace).execute({"path": "."})

            self.assertCountEqual(
                result["entries"],
                [
                    {"name": "link.txt", "type": "symlink"},
                    {"name": "z.txt", "type": "file"},
                ],
            )

    def test_filters_recurses_and_paginates_without_consuming_all_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            paths = []
            for name in ("a.py", "b.txt", "c.py"):
                path = workspace.path / name
                path.write_text(name, encoding="utf-8")
                paths.append(path)
            child = nested / "child.py"
            child.write_text("child", encoding="utf-8")
            paths.append(child)
            with patch(
                "agent_core.tools.builtin.list_directory._iter_entries",
                return_value=iter(paths),
            ):
                first = _Bound(ListDirectoryTool(), workspace).execute(
                    {
                        "path": ".",
                        "glob": "**/*.py",
                        "recursive": True,
                        "limit": 1,
                    }
                )
            with patch(
                "agent_core.tools.builtin.list_directory._iter_entries",
                return_value=iter(paths),
            ):
                result = _Bound(ListDirectoryTool(), workspace).execute(
                    {
                        "path": ".",
                        "glob": "**/*.py",
                        "recursive": True,
                        "cursor": first["next_cursor"],
                        "limit": 1,
                    }
                )

            self.assertEqual(first["entries"], [{"name": "a.py", "type": "file"}])
            self.assertEqual(
                result,
                {
                    "path": ".",
                    "entries": [{"name": "c.py", "type": "file"}],
                    "has_more": True,
                    "next_cursor": "c.py",
                },
            )

    def test_glob_matches_complete_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (workspace.path / "root.py").write_text("root", encoding="utf-8")
            (nested / "child.py").write_text("child", encoding="utf-8")
            tool = _Bound(ListDirectoryTool(), workspace)

            shallow = tool.execute(
                {"path": ".", "glob": "*.py", "recursive": True}
            )
            recursive = tool.execute(
                {"path": ".", "glob": "**/*.py", "recursive": True}
            )

            self.assertEqual(
                [entry["name"] for entry in shallow["entries"]],
                ["root.py"],
            )
            self.assertCountEqual(
                [entry["name"] for entry in recursive["entries"]],
                ["nested/child.py", "root.py"],
            )

    def test_returns_stable_path_order_with_bounded_page_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            paths = []
            for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
                path = workspace.path / name
                path.write_text(name, encoding="utf-8")
                paths.append(path)
            with patch(
                "agent_core.tools.builtin.list_directory._iter_entries",
                return_value=iter(reversed(paths)),
            ):
                result = _Bound(ListDirectoryTool(), workspace).execute(
                    {"path": ".", "limit": 2}
                )

            self.assertEqual(len(result["entries"]), 2)
            self.assertEqual(
                [entry["name"] for entry in result["entries"]],
                ["a.txt", "b.txt"],
            )
            self.assertTrue(result["has_more"])
            self.assertEqual(result["next_cursor"], "b.txt")

    def test_cursor_pagination_is_stable_when_earlier_entries_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            for name in ("b.txt", "c.txt", "d.txt"):
                (workspace.path / name).write_text(name, encoding="utf-8")
            tool = _Bound(ListDirectoryTool(), workspace)
            first = tool.execute({"path": ".", "limit": 2})

            (workspace.path / "a.txt").write_text("a", encoding="utf-8")
            (workspace.path / "b.txt").unlink()
            second = tool.execute(
                {
                    "path": ".",
                    "cursor": first["next_cursor"],
                    "limit": 2,
                }
            )

            self.assertEqual(
                [entry["name"] for entry in first["entries"]],
                ["b.txt", "c.txt"],
            )
            self.assertEqual(
                [entry["name"] for entry in second["entries"]],
                ["d.txt"],
            )
            self.assertFalse(second["has_more"])
            self.assertIsNone(second["next_cursor"])

    def test_rejects_offset_and_invalid_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(ListDirectoryTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(ValueError, "accepts only"):
                tool.execute({"path": ".", "offset": 1})
            for cursor in ("", 1, True):
                with self.subTest(cursor=cursor):
                    with self.assertRaisesRegex(ValueError, "cursor"):
                        tool.execute({"path": ".", "cursor": cursor})

    def test_rejects_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "notes",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must be a directory"):
                _Bound(ListDirectoryTool(), workspace).execute({"path": "notes.txt"})

    def test_rejects_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(ListDirectoryTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(
                ValueError,
                "must stay within the workspace",
            ):
                tool.execute({"path": ".."})


class SearchFilesToolTest(unittest.TestCase):
    def test_searches_utf8_files_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (workspace.path / "root.txt").write_text(
                "first RADE\nsecond\n",
                encoding="utf-8",
            )
            (nested / "child.txt").write_text(
                "RADE child\nrade lowercase\n",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": r"RADE"}
            )

            self.assertEqual(
                result,
                {
                    "matches": [
                        {
                            "path": "nested/child.txt",
                            "line_number": 1,
                            "line": "RADE child",
                        },
                        {
                            "path": "root.txt",
                            "line_number": 1,
                            "line": "first RADE",
                        },
                    ],
                    "has_more": False,
                    "next_offset": None,
                    "scanned_files": 2,
                    "skipped_files": 0,
                },
            )

    def test_supports_regular_expressions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "item-12\nitem-x\n",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": r"item-\d+"}
            )

            self.assertEqual(len(result["matches"]), 1)
            self.assertEqual(result["matches"][0]["line"], "item-12")

    def test_supports_glob_case_insensitive_and_fixed_strings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (workspace.path / "root.py").write_text(
                "value = 'RADE.'\n",
                encoding="utf-8",
            )
            (nested / "child.py").write_text(
                "value = 'rade.'\n",
                encoding="utf-8",
            )
            (nested / "child.txt").write_text(
                "value = 'rade.'\n",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {
                    "path": ".",
                    "pattern": "rade.",
                    "glob": "**/*.py",
                    "case_insensitive": True,
                    "fixed_strings": True,
                }
            )

            self.assertEqual(
                [match["path"] for match in result["matches"]],
                ["nested/child.py", "root.py"],
            )
            self.assertEqual(result["scanned_files"], 2)

    def test_paginates_matches_with_offset_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join(f"RADE {index}" for index in range(5)),
                encoding="utf-8",
            )
            tool = _Bound(SearchFilesTool(), workspace)

            first_page = tool.execute(
                {
                    "path": ".",
                    "pattern": "RADE",
                    "offset": 0,
                    "limit": 2,
                }
            )
            second_page = tool.execute(
                {
                    "path": ".",
                    "pattern": "RADE",
                    "offset": first_page["next_offset"],
                    "limit": 2,
                }
            )

            self.assertEqual(
                [match["line_number"] for match in first_page["matches"]],
                [1, 2],
            )
            self.assertTrue(first_page["has_more"])
            self.assertEqual(first_page["next_offset"], 2)
            self.assertEqual(
                [match["line_number"] for match in second_page["matches"]],
                [3, 4],
            )
            self.assertTrue(second_page["has_more"])
            self.assertEqual(second_page["next_offset"], 4)

    def test_defaults_to_at_most_200_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "\n".join("RADE" for _ in range(201)),
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "RADE"}
            )

            self.assertEqual(len(result["matches"]), 200)
            self.assertTrue(result["has_more"])
            self.assertEqual(result["next_offset"], 200)

    def test_skips_common_dependency_and_build_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            for name in (
                "node_modules", ".venv", "build", ".pnpm-store", ".npm",
                ".cache", ".yarn", "pip-cache", "uv-cache",
            ):
                excluded = workspace.path / name
                excluded.mkdir()
                (excluded / "match.txt").write_text(
                    "RADE",
                    encoding="utf-8",
                )
            (workspace.path / "match.txt").write_text(
                "RADE",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "RADE"}
            )

            self.assertEqual(
                [match["path"] for match in result["matches"]],
                ["match.txt"],
            )
            self.assertEqual(result["scanned_files"], 1)

    def test_can_search_an_explicit_cache_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            cache = workspace.path / ".pnpm-store"
            cache.mkdir()
            (cache / "match.txt").write_text("RADE", encoding="utf-8")

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".pnpm-store", "pattern": "RADE"}
            )

            self.assertEqual(result["matches"][0]["path"], ".pnpm-store/match.txt")

    def test_resolves_only_the_search_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (nested / "match.txt").write_text("RADE", encoding="utf-8")
            tool = _Bound(SearchFilesTool(), workspace)
            resolved = []
            original_resolve = Path.resolve

            def resolve(path, *args, **kwargs):
                resolved.append(path)
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve):
                result = tool.execute({"path": ".", "pattern": "RADE"})

            self.assertEqual(result["scanned_files"], 1)
            self.assertEqual(resolved, [workspace.path])

    def test_times_out_during_directory_enumeration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "empty").mkdir()
            tool = _Bound(SearchFilesTool(), workspace)
            clock = SimpleNamespace(now=0.0)
            with os.scandir(workspace.path) as iterator:
                entries = list(iterator)

            def slow_entries():
                clock.now = 1.0
                yield from entries

            with (
                patch("agent_core.tools.builtin.search_files.os.scandir") as scandir,
                patch(
                    "agent_core.tools.builtin.search_files.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                scandir.return_value.__enter__.return_value = slow_entries()
                with self.assertRaisesRegex(TimeoutError, "after 1 seconds"):
                    tool.execute({"path": ".", "pattern": "x", "timeout_seconds": 1})

    def test_times_out_while_scanning_lines_without_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text("one\ntwo", encoding="utf-8")
            tool = _Bound(SearchFilesTool(), workspace)
            clock = SimpleNamespace(now=0.0)

            def slow_match(_value):
                clock.now = 2.0
                return False

            matcher = Mock(side_effect=slow_match)
            with (
                patch(
                    "agent_core.tools.builtin.search_files._create_matcher",
                    return_value=matcher,
                ),
                patch(
                    "agent_core.tools.builtin.search_files.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                with self.assertRaises(TimeoutError):
                    tool.execute({"path": ".", "pattern": "x", "timeout_seconds": 2})

            self.assertEqual(matcher.call_count, 1)

    def test_checks_deadline_after_last_file_in_files_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text("", encoding="utf-8")
            tool = _Bound(SearchFilesTool(), workspace)
            clock = SimpleNamespace(now=0.0)

            def slow_match(_value):
                clock.now = 60.0
                return False

            schema = tool.definition.parameters["properties"]["timeout_seconds"]
            self.assertEqual(schema["default"], 60)
            with (
                patch(
                    "agent_core.tools.builtin.search_files._create_matcher",
                    return_value=slow_match,
                ),
                patch(
                    "agent_core.tools.builtin.search_files.time.monotonic",
                    side_effect=lambda: clock.now,
                ),
            ):
                with self.assertRaisesRegex(TimeoutError, "after 60 seconds"):
                    tool.execute({"path": ".", "pattern": "x", "mode": "files"})

    def test_honors_cancellation_during_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text("one\ntwo", encoding="utf-8")
            control = TurnControl()
            tool = _Bound(SearchFilesTool(), workspace, cancellation=control)

            def cancel(_value):
                control.cancel()
                return False

            matcher = Mock(side_effect=cancel)
            with patch(
                "agent_core.tools.builtin.search_files._create_matcher",
                return_value=matcher,
            ):
                with self.assertRaises(AgentCancelled):
                    tool.execute({"path": ".", "pattern": "x"})

            self.assertEqual(matcher.call_count, 1)

    def test_skips_files_larger_than_scan_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "large.txt").write_text(
                "RADE",
                encoding="utf-8",
            )

            with patch(
                "agent_core.tools.builtin.search_files."
                "MAX_FILE_SIZE_BYTES",
                3,
            ):
                result = _Bound(SearchFilesTool(), workspace).execute(
                    {"path": ".", "pattern": "RADE"}
                )

            self.assertEqual(result["matches"], [])
            self.assertEqual(result["scanned_files"], 0)
            self.assertEqual(result["skipped_files"], 1)

    def test_scans_past_the_previous_path_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            no_match = workspace.path / "no-match.txt"
            no_match.write_text("nothing", encoding="utf-8")
            match = workspace.path / "match.txt"
            match.write_text("RADE", encoding="utf-8")

            contents = {no_match: b"nothing", match: b"RADE"}
            sizes = {path: path.stat() for path in contents}
            stat = Path.stat
            # This is a scan-count boundary, not a filesystem throughput test.
            with (
                patch("agent_core.tools.builtin.search_files._iter_files",
                      return_value=iter([no_match] * 10_000 + [match])),
                patch.object(Path, "stat", lambda path, *args, **kwargs:
                             sizes[path] if path in sizes else stat(path, *args, **kwargs)),
                patch.object(Path, "read_bytes", lambda path: contents[path]),
            ):
                result = _Bound(SearchFilesTool(), workspace).execute(
                    {"path": ".", "pattern": "RADE"}
                )

            self.assertEqual(
                [item["path"] for item in result["matches"]],
                ["match.txt"],
            )
            self.assertFalse(result["has_more"])
            self.assertEqual(result["scanned_files"], 10_001)

    def test_returns_context_around_content_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "one\ntwo\nRADE\nfour\nfive\n",
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {
                    "path": ".",
                    "pattern": "RADE",
                    "context_before": 2,
                    "context_after": 2,
                }
            )

            self.assertEqual(
                result["matches"],
                [
                    {
                        "path": "notes.txt",
                        "line_number": 3,
                        "line": "RADE",
                        "context_before": [
                            {"line_number": 1, "line": "one"},
                            {"line_number": 2, "line": "two"},
                        ],
                        "context_after": [
                            {"line_number": 4, "line": "four"},
                            {"line_number": 5, "line": "five"},
                        ],
                    }
                ],
            )

    def test_searches_file_paths_in_files_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (nested / "agent.py").write_text("binary-like: \ufffd", encoding="utf-8")
            (nested / "notes.txt").write_text("agent.py", encoding="utf-8")

            result = _Bound(SearchFilesTool(), workspace).execute(
                {
                    "path": ".",
                    "pattern": r"agent\.py$",
                    "mode": "files",
                }
            )

            self.assertEqual(result["matches"], [{"path": "nested/agent.py"}])
            self.assertEqual(result["scanned_files"], 2)

    def test_search_glob_matches_complete_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (workspace.path / "root.py").write_text("RADE", encoding="utf-8")
            (nested / "child.py").write_text("RADE", encoding="utf-8")
            tool = _Bound(SearchFilesTool(), workspace)

            shallow = tool.execute(
                {"path": ".", "pattern": "RADE", "glob": "*.py"}
            )
            recursive = tool.execute(
                {"path": ".", "pattern": "RADE", "glob": "**/*.py"}
            )

            self.assertEqual(
                [match["path"] for match in shallow["matches"]],
                ["root.py"],
            )
            self.assertEqual(
                [match["path"] for match in recursive["matches"]],
                ["nested/child.py", "root.py"],
            )

    def test_limits_serialized_tool_result_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "RADE " + ("x" * (MAX_TOOL_RESULT_CHARS * 2)),
                encoding="utf-8",
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "RADE"}
            )

            content = ToolResult(
                tool_call_id="call-1",
                name="search_files",
                output=result,
            ).to_content()
            self.assertLessEqual(len(content), MAX_TOOL_RESULT_CHARS)
            self.assertTrue(result["matches"][0]["line_truncated"])

    def test_skips_non_utf8_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "binary.bin").write_bytes(b"\xffRADE")

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "RADE"}
            )

            self.assertEqual(result["matches"], [])
            self.assertEqual(result["skipped_files"], 1)

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_skips_symlinks_to_files_outside_workspace(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            workspace = Workspace(Path(directory))
            outside_file = Path(outside_directory) / "outside.txt"
            outside_file.write_text("RADE", encoding="utf-8")
            os.symlink(outside_file, workspace.path / "link.txt")

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "RADE"}
            )

            self.assertEqual(result["matches"], [])
            self.assertEqual(result["skipped_files"], 1)

    @unittest.skipUnless(
        os.name == "nt",
        "directory junctions are a Windows feature",
    )
    def test_skips_junctions_to_directories_outside_workspace(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as outside_directory,
        ):
            workspace = Workspace(Path(directory))
            outside_file = Path(outside_directory) / "outside.txt"
            outside_file.write_text("RADE", encoding="utf-8")
            subprocess.run(
                [
                    "cmd",
                    "/c",
                    "mklink",
                    "/J",
                    str(workspace.path / "link"),
                    outside_directory,
                ],
                check=True,
                capture_output=True,
            )

            result = _Bound(SearchFilesTool(), workspace).execute(
                {"path": ".", "pattern": "RADE"}
            )

            self.assertEqual(result["matches"], [])
            self.assertEqual(result["skipped_files"], 1)

    @unittest.skipUnless(os.name == "nt", "directory junctions are a Windows feature")
    def test_skips_junctions_to_descendants_and_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            nested = workspace.path / "nested"
            nested.mkdir()
            (nested / "match.txt").write_text("RADE", encoding="utf-8")
            links = [workspace.path / "alias", nested / "ancestor"]
            for link, target in zip(links, (nested, workspace.path)):
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    check=True,
                    capture_output=True,
                )
            try:
                tool = _Bound(SearchFilesTool(), workspace)
                for mode in ("files", "content"):
                    with self.subTest(mode=mode):
                        result = tool.execute(
                            {"path": ".", "pattern": "match" if mode == "files" else "RADE", "mode": mode}
                        )
                        self.assertEqual(
                            [match["path"] for match in result["matches"]],
                            ["nested/match.txt"],
                        )
                        self.assertEqual(result["scanned_files"], 1)
                        self.assertEqual(result["skipped_files"], 2)
            finally:
                for link in links:
                    link.rmdir()

    def test_rejects_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(SearchFilesTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(
                ValueError,
                "must stay within the workspace",
            ):
                tool.execute({"path": "..", "pattern": "RADE"})

    def test_rejects_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            (workspace.path / "notes.txt").write_text(
                "RADE",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must be a directory"):
                _Bound(SearchFilesTool(), workspace).execute(
                    {"path": "notes.txt", "pattern": "RADE"}
                )

    def test_rejects_invalid_regular_expression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(SearchFilesTool(), Workspace(Path(directory)))

            with self.assertRaisesRegex(Exception, "unterminated"):
                tool.execute({"path": ".", "pattern": "["})

    def test_validates_optional_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tool = _Bound(SearchFilesTool(), Workspace(Path(directory)))

            invalid_arguments = (
                {"path": ".", "pattern": "x", "glob": ""},
                {"path": ".", "pattern": "x", "offset": -1},
                {"path": ".", "pattern": "x", "limit": 201},
                {
                    "path": ".",
                    "pattern": "x",
                    "case_insensitive": 1,
                },
                {"path": ".", "pattern": "x", "fixed_strings": 1},
                {"path": ".", "pattern": "x", "timeout_seconds": 0},
                {"path": ".", "pattern": "x", "timeout_seconds": -1},
                {"path": ".", "pattern": "x", "timeout_seconds": True},
                {"path": ".", "pattern": "x", "timeout_seconds": 1.5},
                {"path": ".", "pattern": "x", "timeout_seconds": "1"},
                {"path": ".", "pattern": "x", "unknown": True},
            )

            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        tool.execute(arguments)


class ShellToolTest(unittest.TestCase):
    def test_delegates_command_to_executor(self) -> None:
        class RecordingExecutor:
            def __init__(self) -> None:
                self.commands = []

            def execute(self, command, timeout_seconds=60):
                self.commands.append((command, timeout_seconds))
                return CommandExecutionResult(
                    command=command,
                    exit_code=7,
                    stdout="output",
                    stderr="warning",
                    timeout_seconds=timeout_seconds,
                )

        executor = RecordingExecutor()
        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            command_executor=executor,
        )

        result = tool.execute({"command": "example command"})

        self.assertEqual(executor.commands, [("example command", 60)])
        self.assertEqual(
            result,
            {
                "command": "example command",
                "exit_code": 7,
                "stdout": "output",
                "stderr": "warning",
                "timed_out": False,
                "timeout_seconds": 60,
            },
        )

    def test_passes_explicit_timeouts_below_and_above_default(self) -> None:
        class RecordingExecutor:
            def __init__(self) -> None:
                self.timeouts = []

            def execute(self, command, timeout_seconds=60):
                self.timeouts.append(timeout_seconds)
                return CommandExecutionResult(
                    command=command,
                    exit_code=0,
                    stdout="",
                    stderr="",
                    timeout_seconds=timeout_seconds,
                )

        executor = RecordingExecutor()
        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            command_executor=executor,
        )

        for timeout in (10, 61, 900):
            with self.subTest(timeout=timeout):
                tool.execute({"command": "pwd", "timeout_seconds": timeout})

        self.assertEqual(executor.timeouts, [10, 61, 900])

    def test_advertises_the_shell_and_timeout_it_uses(self) -> None:
        class UnusedExecutor:
            def execute(self, command, timeout_seconds=60):
                raise AssertionError("executor should not be called")

        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            command_executor=UnusedExecutor(),
        )

        definition = tool.definition
        self.assertNotIn("scope", definition.parameters["properties"])
        timeout_schema = definition.parameters["properties"][
            "timeout_seconds"
        ]

        self.assertEqual(timeout_schema["maximum"], 900)
        self.assertIn("60 seconds by default", definition.description)
        self.assertIn("maximum of 900", timeout_schema["description"])
        self.assertIn("up to 900 seconds", definition.description)
        self.assertNotIn("background", definition.description)
        self.assertNotIn("86400", definition.description)
        self.assertNotIn("background", definition.parameters["properties"])
        serialized_schema = json.dumps(definition.parameters)
        self.assertNotIn("background", serialized_schema)
        self.assertNotIn("86400", serialized_schema)
        self.assertIn(
            "PowerShell 7" if os.name == "nt" else "/bin/sh",
            definition.description,
        )

    def test_background_shell_advertises_the_longer_timeout(self) -> None:
        from agent_core import JobManager

        class UnusedExecutor:
            def execute(self, command, timeout_seconds=60, cancellation=None):
                raise AssertionError("executor should not be called")

        session = Session("s")
        jobs = JobManager(session)
        context = context_for(
            _TMP_WORKSPACE,
            session=session,
            command_executor=UnusedExecutor(),
            jobs=jobs,
        )

        timeout_schema = ShellTool().definition(context).parameters[
            "properties"
        ]["timeout_seconds"]

        self.assertEqual(timeout_schema["maximum"], 86400)
        definition = ShellTool().definition(context)
        self.assertIn("background", definition.parameters["properties"])
        self.assertIn("background=true", definition.description)
        self.assertIn("86400", definition.description)
        self.assertIn("background=true", timeout_schema["description"])
        self.assertIn("86400", timeout_schema["description"])
        jobs.close()

    def test_validates_arguments(self) -> None:
        class UnusedExecutor:
            def execute(self, command, timeout_seconds=60):
                raise AssertionError("executor should not be called")

        tool = _Bound(
            ShellTool(),
            _TMP_WORKSPACE,
            command_executor=UnusedExecutor(),
        )

        with self.assertRaisesRegex(ValueError, "non-empty string"):
            tool.execute({})
        with self.assertRaisesRegex(ValueError, "accepts only"):
            tool.execute({"command": "pwd", "extra": True})
        with self.assertRaisesRegex(ValueError, "accepts only"):
            tool.execute({"command": "pwd", "scope": "workspace"})
        with self.assertRaisesRegex(ValueError, "'background'.*boolean"):
            tool.execute(
                {
                    "command": "pwd",
                    "background": "true",
                    "timeout_seconds": 86401,
                }
            )
        for timeout_seconds in (0, 901, True, "10"):
            with self.subTest(timeout_seconds=timeout_seconds):
                with self.assertRaisesRegex(ValueError, "between 1 and 900"):
                    tool.execute(
                        {
                            "command": "pwd",
                            "timeout_seconds": timeout_seconds,
                        }
                    )

if __name__ == "__main__":
    unittest.main()
