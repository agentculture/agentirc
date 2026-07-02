"""SQLite disk persistence for channel message history."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)


class HistoryStore:
    """Save and load channel message history to/from SQLite."""

    def __init__(self, data_dir: str):
        db_dir = Path(data_dir)
        db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = db_dir / "history.db"
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                nick TEXT NOT NULL,
                text TEXT NOT NULL,
                timestamp REAL NOT NULL,
                msgid TEXT
            )""")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_channel_ts ON history(channel, timestamp, id)"
        )
        self._migrate_msgid_column()
        self._conn.commit()

    def _migrate_msgid_column(self) -> None:
        """Add the ``msgid`` column to a pre-existing (pre-t6) database.

        ``CREATE TABLE IF NOT EXISTS`` above is a no-op against a database
        created before the ``msgid`` column existed, so it's added here via a
        lightweight ``ALTER TABLE`` on open, guarded by a ``PRAGMA
        table_info`` check so it only runs once (SQLite has no
        ``ADD COLUMN IF NOT EXISTS``). Fresh databases already have the
        column from ``CREATE TABLE`` and this is a no-op for them.
        """
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(history)")}
        if "msgid" not in cols:
            self._conn.execute("ALTER TABLE history ADD COLUMN msgid TEXT")

    def append(
        self,
        channel: str,
        nick: str,
        text: str,
        timestamp: float,
        msgid: str | None = None,
    ) -> int:
        """Insert a single history entry (batched — not committed per call).

        Returns the assigned ``AUTOINCREMENT`` row id, which doubles as the
        monotonic tie-break component of the HISTORY SINCE cursor (see
        ``agentirc/skills/history.py``'s module docstring).
        """
        cur = self._conn.execute(
            "INSERT INTO history (channel, nick, text, timestamp, msgid) VALUES (?, ?, ?, ?, ?)",
            (channel, nick, text, timestamp, msgid),
        )
        return cur.lastrowid

    def get_recent(self, channel: str, count: int) -> list[dict]:
        """Return the last *count* entries for a channel, in chronological order."""
        cur = self._conn.execute(
            "SELECT id, nick, text, timestamp, msgid FROM history "
            "WHERE channel = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (channel, count),
        )
        rows = cur.fetchall()
        return [
            {"id": r[0], "nick": r[1], "text": r[2], "timestamp": r[3], "msgid": r[4]}
            for r in reversed(rows)
        ]

    def get_since(self, channel: str, after: tuple[float, int] | None, limit: int) -> list[dict]:
        """Return up to *limit* entries strictly after cursor tuple *after*.

        *after* is a decoded ``(timestamp, id)`` pair (or ``None`` for "from
        the beginning"). Results are ordered ascending by ``(timestamp,
        id)`` — the same order the ``idx_history_channel_ts`` index is built
        for — so this is a plain keyset-pagination range scan, not an
        offset scan: deterministic and non-overlapping across successive
        calls chained by the previous page's last row.
        """
        if limit <= 0:
            return []
        if after is None:
            cur = self._conn.execute(
                "SELECT id, nick, text, timestamp, msgid FROM history "
                "WHERE channel = ? ORDER BY timestamp ASC, id ASC LIMIT ?",
                (channel, limit),
            )
        else:
            after_ts, after_id = after
            cur = self._conn.execute(
                "SELECT id, nick, text, timestamp, msgid FROM history "
                "WHERE channel = ? AND (timestamp, id) > (?, ?) "
                "ORDER BY timestamp ASC, id ASC LIMIT ?",
                (channel, after_ts, after_id, limit),
            )
        return [
            {"id": r[0], "nick": r[1], "text": r[2], "timestamp": r[3], "msgid": r[4]}
            for r in cur
        ]

    def search(self, channel: str, term: str) -> list[dict]:
        """Case-insensitive substring search within a channel."""
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        cur = self._conn.execute(
            "SELECT nick, text, timestamp FROM history "
            "WHERE channel = ? AND text LIKE ? ESCAPE '\\' ORDER BY timestamp ASC",
            (channel, f"%{escaped}%"),
        )
        return [{"nick": r[0], "text": r[1], "timestamp": r[2]} for r in cur]

    def load_channels(self, maxlen: int) -> dict[str, deque]:
        """Load the last *maxlen* entries per channel for startup restore.

        Returns a dict mapping channel names to deques of
        ``{"id": ..., "nick": ..., "text": ..., "timestamp": ..., "msgid": ...}``
        dicts — ``id``/``msgid`` were added in t6 for HISTORY SINCE support.
        """
        cur = self._conn.execute("SELECT DISTINCT channel FROM history")
        channels: dict[str, deque] = {}
        for (channel,) in cur:
            entries = self.get_recent(channel, maxlen)
            channels[channel] = deque(entries, maxlen=maxlen)
        return channels

    def prune(self, max_age_days: int) -> int:
        """Delete entries older than *max_age_days*.  Returns rows deleted."""
        cutoff = time.time() - (max_age_days * 86400)
        cur = self._conn.execute("DELETE FROM history WHERE timestamp < ?", (cutoff,))
        self._conn.commit()
        deleted = cur.rowcount
        if deleted:
            logger.info("Pruned %d history entries older than %d days", deleted, max_age_days)
        return deleted

    def close(self) -> None:
        """Flush pending writes and close the database connection."""
        try:
            self._conn.commit()
        except sqlite3.Error:
            pass
        self._conn.close()
