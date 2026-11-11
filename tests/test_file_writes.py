import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    ApplyPatchTool,
    EditFileTool,
    Session,
    ToolCall,
    ToolCatalog,
    ToolExecutionContext,
    Workspace,
    WriteFileTool,
)
from agent_core.tools.files import write_file_bytes
from agent_core.turn_control import AgentCancelled, TurnControl
from tests.file_write_fixtures import patch_text, write_call


class ApplyPatchRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.context = ToolExecutionContext(Workspace(self.root), Session())
        self.tools = ToolCatalog([ApplyPatchTool()]).select(["apply_patch"], self.context)

    def apply(self, body: str):
        return self.tools.execute(ToolCall("patch", "apply_patch", {"patch": patch_text(body)}))

    def test_append_keeps_lines_separate_without_adding_a_final_newline(self) -> None:
        for original in (b"old", b"first\r\nold"):
            with self.subTest(original=original):
                target = self.root / "file.txt"
                target.write_bytes(original)
                result = self.apply("*** Update File: file.txt\n@@\n old\n+new")
                self.assertIsNone(result.error)
                newline = b"\r\n" if b"\r\n" in original else b"\n"
                self.assertEqual(target.read_bytes(), original + newline + b"new")

    def test_preserves_mixed_line_endings_including_the_last_line(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"old\nkeep\r\n")
        result = self.apply("*** Update File: file.txt\n@@\n-old\n+new")
        self.assertIsNone(result.error)
        self.assertEqual(target.read_bytes(), b"new\nkeep\r\n")

    def test_numbered_insertions_support_start_middle_end_and_empty_file(self) -> None:
        cases = (
            (b"one\ntwo\n", "@@ -0,0 +1,1 @@", b"new\none\ntwo\n"),
            (b"one\ntwo\n", "@@ -1,0 +2,1 @@", b"one\nnew\ntwo\n"),
            (b"one\ntwo", "@@ -2,0 +3,1 @@", b"one\ntwo\nnew"),
            (b"", "@@ -0,0 +1,1 @@", b"new\n"),
        )
        for original, header, expected in cases:
            with self.subTest(header=header, original=original):
                target = self.root / "file.txt"
                target.write_bytes(original)
                result = self.apply(f"*** Update File: file.txt\n{header}\n+new")
                self.assertIsNone(result.error)
                self.assertEqual(target.read_bytes(), expected)

    def test_rejects_out_of_bounds_and_overlapping_ranges_before_any_write(self) -> None:
        bodies = (
            "@@ -99,0 +100,1 @@\n+new",
            "@@ -2,2 +2,1 @@\n-two\n-missing\n+new",
            "@@ -2 +2 @@\n-two\n+new\n@@ -1 +1 @@\n-one\n+new",
        )
        target = self.root / "file.txt"
        for body in bodies:
            with self.subTest(body=body):
                target.write_bytes(b"one\ntwo\n")
                result = self.apply("*** Add File: new/deep/file.txt\n+new\n*** Update File: file.txt\n" + body)
                self.assertIsNotNone(result.error)
                self.assertEqual(target.read_bytes(), b"one\ntwo\n")
                self.assertFalse((self.root / "new").exists())

    def test_multiple_numbered_hunks_account_for_insertions_and_deletions(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"one\ntwo\nthree\nfour\n")
        result = self.apply(
            "*** Update File: file.txt\n"
            "@@ -1,0 +2,1 @@\n+inserted\n"
            "@@ -3,1 +3,0 @@\n-three\n"
            "@@ -4,1 +4,1 @@\n-four\n+last"
        )
        self.assertIsNone(result.error)
        self.assertEqual(target.read_bytes(), b"one\ninserted\ntwo\nlast\n")

    def test_creates_shared_missing_parent_directories(self) -> None:
        result = self.apply("*** Add File: src/deep/a.txt\n+a\n*** Add File: src/deep/b.txt\n+b")
        self.assertIsNone(result.error)
        self.assertEqual((self.root / "src/deep/a.txt").read_bytes(), b"a\n")
        self.assertEqual((self.root / "src/deep/b.txt").read_bytes(), b"b\n")

    def test_rejects_parent_file_conflicts_without_creating_anything(self) -> None:
        for body in (
            "*** Add File: new\n+file\n*** Add File: new/file.txt\n+child",
            "*** Add File: new/file.txt\n+child\n*** Add File: new\n+file",
        ):
            with self.subTest(body=body):
                result = self.apply(body)
                self.assertIn("also used as a parent", result.error.message)
                self.assertFalse((self.root / "new").exists())
        (self.root / "blocker").write_bytes(b"file")
        result = self.apply("*** Add File: new/file.txt\n+new\n*** Add File: blocker/deep/file.txt\n+no")
        self.assertEqual(result.error.type, "NotADirectoryError")
        self.assertFalse((self.root / "new").exists())

    def test_deletes_binary_files_without_decoding(self) -> None:
        target = self.root / "file.bin"
        target.write_bytes(b"\xff\xfe\x00")
        result = self.apply("*** Delete File: file.bin")
        self.assertIsNone(result.error)
        self.assertFalse(target.exists())

    def test_reports_partial_failure_through_the_tool_result(self) -> None:
        for name in ("a", "b", "c"):
            (self.root / name).write_bytes(b"old\n")
        body = "\n".join(f"*** Update File: {name}\n@@\n-old\n+new" for name in ("a", "b", "c"))

        def fail_second(path, data):
            if path.name == "b":
                raise PermissionError("locked")
            write_file_bytes(path, data)

        with patch("agent_core.tools.builtin.apply_patch.write_file_bytes", side_effect=fail_second):
            result = self.apply(body)

        self.assertTrue(result.executed)
        self.assertEqual(result.error.type, "PatchApplyError")
        details = json.loads(result.to_content())["error"]["details"]
        self.assertEqual(details["completed"], [{"path": "a", "operation": "updated"}])
        self.assertEqual(details["failed"], {"path": "b", "operation": "updated"})
        self.assertEqual(details["pending"], [{"path": "c", "operation": "updated"}])
        self.assertEqual(details["cause"]["type"], "PermissionError")
        self.assertEqual((self.root / "a").read_bytes(), b"new\n")
        self.assertEqual((self.root / "b").read_bytes(), b"old\n")
        self.assertEqual((self.root / "c").read_bytes(), b"old\n")

    def test_failed_atomic_replace_keeps_original_file_and_cleans_temporary(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"old\n")
        with patch("agent_core.atomic.os.replace", side_effect=PermissionError("locked")):
            result = self.apply("*** Update File: file.txt\n@@\n-old\n+new")
        self.assertEqual(result.error.details["completed"], [])
        self.assertEqual(target.read_bytes(), b"old\n")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_cancellation_after_preparation_prevents_writes(self) -> None:
        control = TurnControl()
        self.context = ToolExecutionContext(Workspace(self.root), Session(), cancellation=control)
        tools = ToolCatalog([ApplyPatchTool()]).select(["apply_patch"], self.context)

        def cancel_during_validation(*args):
            control.cancel()
            return []

        with patch("agent_core.tools.builtin.apply_patch.missing_parent_directories", side_effect=cancel_during_validation):
            with self.assertRaises(AgentCancelled):
                tools.execute(ToolCall("patch", "apply_patch", {
                    "patch": patch_text("*** Add File: new/file.txt\n+new"),
                }))
        self.assertFalse((self.root / "new").exists())

    def test_matching_diagnostics_include_hunk_and_expected_context(self) -> None:
        (self.root / "file.txt").write_bytes(b"one\ntwo\n")
        result = self.apply("*** Update File: file.txt\n@@\n-one\n+new\n@@\n-missing\n+new")
        self.assertIn("hunk 2", result.error.message)
        self.assertIn("missing", result.error.message)
        self.assertEqual((self.root / "file.txt").read_bytes(), b"one\ntwo\n")

    def test_tolerates_context_trailing_whitespace_and_preserves_original_bytes(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"before \t\r\nold\r\ninside\t\r\nremove\r\nafter \r\n")
        result = self.apply(
            "*** Update File: file.txt\n@@\n before\n-old\n+new\n"
            " inside \n-remove\n+replacement\n after"
        )
        self.assertIsNone(result.error)
        self.assertEqual(
            target.read_bytes(),
            b"before \t\r\nnew\r\ninside\t\r\nreplacement\r\nafter \r\n",
        )

    def test_discards_at_most_two_outer_context_lines_without_overwriting_them(self) -> None:
        cases = (
            ("before\nold\nafter", " stale\n-old\n+new\n after"),
            ("before\nold\nafter", " before\n-old\n+new\n stale"),
            ("before\nold\nafter", " stale\n-old\n+new\n stale"),
            ("before\nold\nafter", " stale\n stale\n-old\n+new\n after"),
            ("before\nold\nafter", " before\n-old\n+new\n stale\n stale"),
            ("old", " stale\n-old\n+new\n stale"),
        )
        target = self.root / "file.txt"
        for original, hunk in cases:
            with self.subTest(hunk=hunk):
                target.write_bytes(original.encode())
                result = self.apply(f"*** Update File: file.txt\n@@\n{hunk}")
                self.assertIsNone(result.error)
                self.assertEqual(target.read_bytes(), original.replace("old", "new").encode())

    def test_exact_full_context_takes_priority_over_relaxed_matches(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"before \nold\nafter\nbefore\nold\nafter\n")
        # Even a numbered hint at a weaker match cannot beat full exact context.
        result = self.apply(
            "*** Update File: file.txt\n@@ -1,3 +1,3 @@\n before\n-old\n+new\n after"
        )
        self.assertIsNone(result.error)
        self.assertEqual(target.read_bytes(), b"before \nold\nafter\nbefore\nnew\nafter\n")

    def test_whitespace_full_context_takes_priority_over_trimmed_matches(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"before \nold\nafter\nother\nold\nafter\n")
        result = self.apply("*** Update File: file.txt\n@@\n before\n-old\n+new\n after")
        self.assertIsNone(result.error)
        self.assertEqual(target.read_bytes(), b"before \nnew\nafter\nother\nold\nafter\n")

    def test_numbered_hunks_relocate_uniquely_and_ignore_stale_new_positions(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"intro\none\ntwo\nthree\n")
        result = self.apply(
            "*** Update File: file.txt\n@@ -1 +99,2 @@\n-one\n+first\n+extra\n"
            "@@ -99 +3 @@\n-three\n+last"
        )
        self.assertIsNone(result.error)
        self.assertEqual(target.read_bytes(), b"intro\nfirst\nextra\ntwo\nlast\n")

    def test_numbered_full_matches_disambiguate_exact_and_whitespace_context(self) -> None:
        target = self.root / "file.txt"
        for ending in (b"", b" \t"):
            with self.subTest(ending=ending):
                block = b"before" + ending + b"\nold\nafter\n"
                target.write_bytes(block * 2)
                result = self.apply(
                    "*** Update File: file.txt\n@@ -4,3 +4,3 @@\n before\n-old\n+new\n after"
                )
                self.assertIsNone(result.error)
                self.assertEqual(target.read_bytes(), block + block.replace(b"old", b"new"))

    def test_relaxed_matching_rejects_ambiguity_before_any_write(self) -> None:
        cases = (
            (b"before \nold\nafter\nbefore\t\nold\nafter\n", "@@\n before\n-old\n+new\n after"),
            (b"a\nold\nb\na\nold\nb\n", "@@\n stale\n-old\n+new\n b"),
            # A range hint must not select between ambiguous trimmed matches.
            (b"a\nold\nb\na\nold\nb\n", "@@ -1,3 +1,3 @@\n stale\n-old\n+new\n b"),
            # Different ways to trim context must be checked together.
            (b"a\nold\nx\ny\nold\nb\n", "@@\n a\n-old\n+new\n b"),
        )
        target = self.root / "file.txt"
        for original, hunk in cases:
            with self.subTest(hunk=hunk, original=original):
                target.write_bytes(original)
                result = self.apply("*** Add File: new.txt\n+new\n*** Update File: file.txt\n" + hunk)
                self.assertIn("ambiguous", result.error.message)
                self.assertEqual(target.read_bytes(), original)
                self.assertFalse((self.root / "new.txt").exists())

    def test_relaxation_keeps_removals_and_internal_context_exact_and_is_bounded(self) -> None:
        cases = (
            (b"old \n", "-old\n+new"),
            (b"    old\n", "-old\n+new"),
            (b"different\n", "-old\n+new"),
            (b"old\nchanged\nremove\n", "-old\n+new\n stale\n-remove\n+new"),
            (b"old\n", " stale\n stale\n-old\n+new\n stale"),
            # Pure insertions have no exact removal to anchor trimmed context.
            (b"before\nafter\n", " stale\n+new\n after"),
            (b"\n", " stale\n-\n+new"),
        )
        target = self.root / "file.txt"
        for original, hunk in cases:
            with self.subTest(hunk=hunk):
                target.write_bytes(original)
                result = self.apply(f"*** Update File: file.txt\n@@\n{hunk}")
                self.assertIn("did not match", result.error.message)
                self.assertEqual(target.read_bytes(), original)

    def test_trimmed_matches_preserve_context_between_hunks(self) -> None:
        target = self.root / "file.txt"
        target.write_bytes(b"before\nold\nkeep\nremove\nafter\n")
        result = self.apply(
            "*** Update File: file.txt\n@@\n stale\n-old\n+new\n stale\n"
            "@@\n stale\n-remove\n+last\n stale"
        )
        self.assertIsNone(result.error)
        self.assertEqual(target.read_bytes(), b"before\nnew\nkeep\nlast\nafter\n")

    @unittest.skipIf(os.name == "nt", "POSIX file mode semantics")
    def test_all_file_writers_preserve_executable_permissions(self) -> None:
        for tool in (ApplyPatchTool(), EditFileTool(), WriteFileTool()):
            with self.subTest(tool=tool.name):
                target = self.root / "file.txt"
                target.write_bytes(b"old\n")
                target.chmod(0o751)
                tools = ToolCatalog([tool]).select([tool.name], self.context)
                result = tools.execute(write_call(tool.name))
                self.assertIsNone(result.error)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o751)
