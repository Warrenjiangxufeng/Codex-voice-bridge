from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_voice_pty


class CodexVoicePTYTriggerTests(unittest.TestCase):
    def test_due_message_is_submitted_through_existing_pty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "memory.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE proactive_state (
                        session_id TEXT PRIMARY KEY,
                        paused INTEGER NOT NULL,
                        next_due_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO proactive_state VALUES (?, ?, ?)",
                    ("codex-live", 0, 900.0),
                )

            host = object.__new__(codex_voice_pty.CodexVoicePTY)
            host.recording = None
            host.processing = False
            host.pending_enter = 0.0
            host.next_companion_check = 0.0
            host.last_user_activity = 600.0
            host.last_output_activity = 900.0
            host.last_companion_trigger = 0.0
            submitted: list[str] = []
            host._submit_text = submitted.append  # type: ignore[method-assign]

            with (
                mock.patch.object(
                    codex_voice_pty,
                    "COMPANION_DATABASE",
                    database,
                ),
                mock.patch.object(
                    codex_voice_pty,
                    "COMPANION_IDLE_SECONDS",
                    300.0,
                ),
                mock.patch.object(
                    codex_voice_pty,
                    "COMPANION_OUTPUT_QUIET_SECONDS",
                    15.0,
                ),
                mock.patch.object(
                    codex_voice_pty,
                    "COMPANION_RETRY_SECONDS",
                    300.0,
                ),
                mock.patch.object(
                    codex_voice_pty.time,
                    "time",
                    return_value=1_000.0,
                ),
                mock.patch.object(codex_voice_pty, "log"),
            ):
                host._maybe_trigger_companion()

            self.assertEqual(submitted, ["/companion"])

    def test_user_activity_prevents_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "memory.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE proactive_state (
                        session_id TEXT PRIMARY KEY,
                        paused INTEGER NOT NULL,
                        next_due_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO proactive_state VALUES (?, ?, ?)",
                    ("codex-live", 0, 900.0),
                )

            host = object.__new__(codex_voice_pty.CodexVoicePTY)
            host.recording = None
            host.processing = False
            host.pending_enter = 0.0
            host.next_companion_check = 0.0
            host.last_user_activity = 800.0
            host.last_output_activity = 900.0
            host.last_companion_trigger = 0.0
            submitted: list[str] = []
            host._submit_text = submitted.append  # type: ignore[method-assign]

            with (
                mock.patch.object(
                    codex_voice_pty,
                    "COMPANION_DATABASE",
                    database,
                ),
                mock.patch.object(
                    codex_voice_pty,
                    "COMPANION_IDLE_SECONDS",
                    300.0,
                ),
                mock.patch.object(
                    codex_voice_pty.time,
                    "time",
                    return_value=1_000.0,
                ),
            ):
                host._maybe_trigger_companion()

            self.assertEqual(submitted, [])


if __name__ == "__main__":
    unittest.main()
