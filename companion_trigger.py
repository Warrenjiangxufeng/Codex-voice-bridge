# -*- coding: utf-8 -*-
"""Read-only helpers for triggering Codex Live from the existing PTY host."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BASE_INTERVAL_SECONDS = 300


@dataclass(frozen=True)
class ProactiveState:
    paused: bool
    next_due_at: float | None


def read_proactive_state(
    database: Path,
    session_id: str,
) -> ProactiveState:
    """Return the persisted Codex Live due state without modifying it."""
    if not database.is_file():
        return ProactiveState(paused=False, next_due_at=None)
    try:
        with sqlite3.connect(
            f"file:{database}?mode=ro",
            uri=True,
            timeout=1,
        ) as connection:
            row = connection.execute(
                """
                SELECT paused, next_due_at
                FROM proactive_state
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
    except sqlite3.Error:
        return ProactiveState(paused=False, next_due_at=None)
    if row is None:
        return ProactiveState(paused=False, next_due_at=None)
    return ProactiveState(
        paused=bool(row[0]),
        next_due_at=float(row[1]),
    )


def read_base_interval(config_path: Path) -> int:
    """Read the same base interval used by the Codex Live config."""
    override = os.environ.get("CODEX_LIVE_PROACTIVE_BASE_INTERVAL_SEC", "")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        proactive = payload.get("proactive", {})
        return max(
            1,
            int(
                proactive.get(
                    "base_interval_sec",
                    DEFAULT_BASE_INTERVAL_SECONDS,
                )
            ),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULT_BASE_INTERVAL_SECONDS


def mark_user_activity(
    database: Path,
    session_id: str,
    *,
    now: float,
    base_interval_sec: int,
) -> bool:
    """Reset the unanswered streak when the real PTY receives user input."""
    if not database.is_file():
        return False
    try:
        with sqlite3.connect(database, timeout=1) as connection:
            cursor = connection.execute(
                """
                UPDATE proactive_state
                SET last_user_at = ?,
                    unanswered_streak = 0,
                    next_due_at = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    now,
                    now + max(1, int(base_interval_sec)),
                    now,
                    session_id,
                ),
            )
            return cursor.rowcount > 0
    except sqlite3.Error:
        return False


def should_trigger(
    *,
    state: ProactiveState,
    now: float,
    last_input_at: float,
    last_output_at: float,
    last_trigger_at: float,
    idle_seconds: float,
    output_quiet_seconds: float,
    retry_seconds: float,
) -> tuple[bool, str]:
    """Decide whether the active Codex PTY should submit /companion now."""
    if state.paused:
        return False, "paused"
    if state.next_due_at is None:
        return False, "no_scheduler_state"
    if now < state.next_due_at:
        return False, "not_due"
    if now - last_input_at < idle_seconds:
        return False, "user_active"
    if now - last_output_at < output_quiet_seconds:
        return False, "codex_busy"
    if last_trigger_at and now - last_trigger_at < retry_seconds:
        return False, "retry_cooldown"
    return True, "due"
