"""How a synthesized media message behaves outside the turn that made it.

A message carrying a tool's images has the user role, because that is
the only role an image can travel in.  Everything that treats "the user
role" as "what the person said" therefore has to distinguish them.
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_core.content import ImagePart
from agent_core.session import Message, Session
from agent_core.session_store import JsonlSessionStore


class ToolMediaPersistenceTest(unittest.TestCase):
    """The origin has to survive a reload, or the UI regains the ghost."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.store = JsonlSessionStore(self.root / "runs")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def round_trip(self, *items: Message) -> list[Message]:
        session = Session()

        session.begin_turn("turn-1")
        for item in items:
            session.add_item(
                item.role, item.content, item.timestamp_utc, item.tool_calls,
                item.tool_call_id, item.reasoning, origin=item.origin,
            )
        session.finish_turn("completed")
        self.store.append_events(session.session_id, session.journal)
        return self.store.load(
            session.session_id
        ).items

    def test_the_origin_survives_a_reload(self) -> None:
        loaded = self.round_trip(
            Message(
                role="user",
                content="typed",
                timestamp_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
            ),
            Message(
                role="user",
                content=(
                    ImagePart(
                        path="found.png",
                        mime_type="image/png",
                        filename="found.png",
                        size_bytes=123,
                    ),
                ),
                timestamp_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
                origin="tool_media",
            ),
        )

        self.assertEqual(
            [item.origin for item in loaded], ["conversation", "tool_media"]
        )

    def test_an_ordinary_message_reloads_as_a_user_message(self) -> None:
        loaded = self.round_trip(Message(role="user", content="hello"))

        self.assertEqual(loaded[0].origin, "conversation")
        self.assertFalse(loaded[0].is_tool_media)


if __name__ == "__main__":
    unittest.main()
