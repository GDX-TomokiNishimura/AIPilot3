"""Shared SQLite storage for normalized conversation messages."""

from contextlib import contextmanager
from datetime import UTC, datetime
import os
import sqlite3


MESSAGE_SOURCES = ("graph", "purview", "manual")

# Keep the table and primary key used by the original Graph importer.
CONVERSATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams_chat_messages (
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_display_name TEXT,
    sent_at TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS conversation_message_sources (
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('graph', 'purview', 'manual')),
    source_ref TEXT,
    PRIMARY KEY (chat_id, message_id, source),
    FOREIGN KEY (chat_id, message_id) REFERENCES teams_chat_messages (chat_id, message_id)
);
"""

INSERT_MESSAGE = """
INSERT INTO teams_chat_messages
    (chat_id, message_id, sender_id, sender_display_name, sent_at, body)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (chat_id, message_id) DO NOTHING
"""


@contextmanager
def open_database(path):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)

    db = sqlite3.connect(path)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        yield db
    finally:
        db.close()


def utc_timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def store_message(db, row, source, source_ref=None):
    """Insert a message and record its source within the caller's transaction."""
    inserted = db.execute(INSERT_MESSAGE, row).rowcount == 1
    chat_id, message_id = row[:2]
    if not inserted and source != "graph":
        # Rows from the original importer have no explicit provenance.
        db.execute("""
            INSERT INTO conversation_message_sources (chat_id, message_id, source)
            SELECT ?, ?, 'graph'
            WHERE NOT EXISTS (
                SELECT 1 FROM conversation_message_sources WHERE chat_id = ? AND message_id = ?
            )
        """, (chat_id, message_id, chat_id, message_id))
    db.execute("""
        INSERT INTO conversation_message_sources (chat_id, message_id, source, source_ref)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (chat_id, message_id, source) DO UPDATE
        SET source_ref = excluded.source_ref
        WHERE conversation_message_sources.source_ref IS NULL AND excluded.source_ref IS NOT NULL
    """, (chat_id, message_id, source, source_ref))
    return inserted
