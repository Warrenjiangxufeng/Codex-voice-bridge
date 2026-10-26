from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from companion_trigger import (
    ProactiveState,
    mark_user_activity,
    read_base_interval,
    read_proactive_state,
    should_trigger,
)


class CompanionTriggerTests(unittest.TestCase):
    def test_reads_due_state_without_changing_database(self) -> None:
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
                    ("codex-live", 0, 100.0),
                )

            state = read_proactive_state(database, "codex-live")

            self.assertEqual(state, ProactiveState(False, 100.0))

    def test_user_activity_resets_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "memory.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE proactive_state (
                        session_id TEXT PRIMARY KEY,
                        paused INTEGER NOT NULL,
                        next_due_at REAL NOT NULL,
                        last_sent_at REAL,
                        last_user_at REAL,
                        unanswered_streak INTEGER NOT NULL,
                        topic_index INTEGER NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO proactive_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("codex-live", 0, 1_000.0, 900.0, 800.0, 2, 3, "old"),
                )

            changed = mark_user_activity(
                database,
                "codex-live",
                now=1_100.0,
                base_interval_sec=300,
            )

            self.assertTrue(changed)
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    """
                    SELECT last_user_at, unanswered_streak, next_due_at
                    FROM proactive_state
                    WHERE session_id = 'codex-live'
                    """
                ).fetchone()
            self.assertEqual(row, (1_100.0, 0, 1_400.0))

    def test_reads_configured_base_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(
                '{"proactive": {"base_interval_sec": 420}}',
                encoding="utf-8",
            )

            self.assertEqual(read_base_interval(config), 420)

    def test_triggers_only_after_due_and_quiet_period(self) -> None:
        arguments = dict(
            state=ProactiveState(False, 900.0),
            now=1_000.0,
            last_input_at=600.0,
            last_output_at=900.0,
            last_trigger_at=0.0,
            idle_seconds=300.0,
            output_quiet_seconds=15.0,
            retry_seconds=300.0,
        )

        self.assertEqual(should_trigger(**arguments), (True, "due"))

        self.assertEqual(
            should_trigger(
                **{
                    **arguments,
                    "last_input_at": 800.0,
                }
            ),
            (False, "user_active"),
        )
        self.assertEqual(
            should_trigger(
                **{
                    **arguments,
                    "last_output_at": 990.0,
                }
            ),
            (False, "codex_busy"),
        )
        self.assertEqual(
            should_trigger(
                **{
                    **arguments,
                    "state": ProactiveState(False, 1_001.0),
                }
            ),
            (False, "not_due"),
        )
        self.assertEqual(
            should_trigger(
                **{
                    **arguments,
                    "state": ProactiveState(True, 900.0),
                }
            ),
            (False, "paused"),
        )

    def test_retry_cooldown_prevents_duplicate_submission(self) -> None:
        allowed, reason = should_trigger(
            state=ProactiveState(False, 900.0),
            now=1_000.0,
            last_input_at=600.0,
            last_output_at=900.0,
            last_trigger_at=950.0,
            idle_seconds=300.0,
            output_quiet_seconds=15.0,
            retry_seconds=300.0,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "retry_cooldown")


if __name__ == "__main__":
    unittest.main()
