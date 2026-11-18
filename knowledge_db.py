"""Persistence for conversation evidence, knowledge entries, and reviewed criteria."""

from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
import sqlite3

from conversation_storage import (
    CONVERSATION_SCHEMA,
    MESSAGE_SOURCES,
    open_database,
    store_message,
    utc_timestamp,
)


KNOWLEDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN (
        'business_knowledge', 'business_rule', 'project', 'employee_guidance', 'employee_proficiency'
    )),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    subject_ref TEXT
);

CREATE TABLE IF NOT EXISTS decision_criteria (
    id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS criterion_revisions (
    criterion_id INTEGER NOT NULL REFERENCES decision_criteria (id),
    revision INTEGER NOT NULL,
    statement TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'awaiting_reason', 'approved', 'rejected')),
    approved_at TEXT,
    CHECK (status NOT IN ('pending', 'approved') OR reason IS NOT NULL),
    CHECK (status <> 'awaiting_reason' OR reason IS NULL),
    CHECK ((status = 'approved') = (approved_at IS NOT NULL)),
    PRIMARY KEY (criterion_id, revision)
);

CREATE TABLE IF NOT EXISTS criterion_evidence (
    criterion_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    PRIMARY KEY (criterion_id, revision, chat_id, message_id),
    FOREIGN KEY (criterion_id, revision) REFERENCES criterion_revisions (criterion_id, revision),
    FOREIGN KEY (chat_id, message_id) REFERENCES teams_chat_messages (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS criterion_history (
    id INTEGER PRIMARY KEY,
    criterion_id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('submitted', 'revised', 'approved', 'rejected')),
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (criterion_id, revision, action),
    FOREIGN KEY (criterion_id, revision) REFERENCES criterion_revisions (criterion_id, revision)
);
"""

KNOWLEDGE_KINDS = (
    "business_knowledge", "business_rule", "project", "employee_guidance", "employee_proficiency",
)

MessageKey = tuple[str, str]


@dataclass(frozen=True)
class MessageSource:
    source: str
    reference: str | None


@dataclass(frozen=True)
class ConversationMessage:
    chat_id: str
    message_id: str
    sender_id: str
    sender_display_name: str | None
    sent_at: datetime
    body: str
    sources: tuple[MessageSource, ...]


@dataclass(frozen=True)
class KnowledgeEntry:
    id: int
    kind: str
    title: str
    body: str
    subject_ref: str | None


@dataclass(frozen=True)
class CriterionRevision:
    criterion_id: int
    revision: int
    statement: str
    reason: str | None
    status: str
    approved_at: datetime | None
    evidence: tuple[MessageKey, ...]


@dataclass(frozen=True)
class CriterionEvent:
    revision: int
    action: str
    actor: str
    occurred_at: datetime
    statement: str
    reason: str | None


class CriterionStateError(ValueError):
    """The requested change conflicts with the current criterion revision or state."""


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must contain text")


class KnowledgeStore:
    """Application-facing persistence methods backed by SQLite."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        with open_database(self._db_path) as db:
            db.executescript("BEGIN;\n" + CONVERSATION_SCHEMA + KNOWLEDGE_SCHEMA + "\nCOMMIT;")

    @contextmanager
    def _write(self):
        with open_database(self._db_path) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def register_message(
        self, *, chat_id: str, message_id: str, sender_id: str, sent_at: datetime, body: str,
        source: str, source_ref: str | None = None, sender_display_name: str | None = None,
    ) -> bool:
        """Return whether a message was added. Replays can add provenance to the same message."""
        for field, value in (("chat_id", chat_id), ("message_id", message_id),
                             ("sender_id", sender_id), ("body", body)):
            _require_text(value, field)
        if source not in MESSAGE_SOURCES:
            raise ValueError(f"Unknown message source: {source}")
        if source_ref is not None:
            _require_text(source_ref, "source_ref")
        row = (chat_id, message_id, sender_id, sender_display_name, utc_timestamp(sent_at), body)
        with self._write() as db:
            return store_message(db, row, source, source_ref)

    def search_messages(
        self, text: str = "", *, chat_id: str | None = None, source: str | None = None,
    ) -> list[ConversationMessage]:
        """Find literal text in message bodies and return each match with all its sources."""
        if source is not None and source not in MESSAGE_SOURCES:
            raise ValueError(f"Unknown message source: {source}")
        with open_database(self._db_path) as db:
            rows = db.execute("""
                SELECT message.*, COALESCE(provenance.source, 'graph') AS source,
                       provenance.source_ref
                FROM teams_chat_messages AS message
                LEFT JOIN conversation_message_sources AS provenance USING (chat_id, message_id)
                WHERE instr(message.body, ?) > 0
                  AND (? IS NULL OR message.chat_id = ?)
                  AND (
                      ? IS NULL
                      OR (? = 'graph' AND provenance.source IS NULL)
                      OR EXISTS (
                          SELECT 1 FROM conversation_message_sources AS matching
                          WHERE matching.chat_id = message.chat_id
                            AND matching.message_id = message.message_id
                            AND matching.source = ?
                      )
                  )
                ORDER BY message.sent_at DESC, message.chat_id, message.message_id, provenance.source
            """, (text, chat_id, chat_id, source, source, source)).fetchall()
        messages = []
        for (conversation_id, message_id), group in groupby(
            rows, key=lambda row: (row["chat_id"], row["message_id"]),
        ):
            row = next(group)
            origins = (MessageSource(row["source"], row["source_ref"]),) + tuple(
                MessageSource(origin["source"], origin["source_ref"]) for origin in group
            )
            messages.append(ConversationMessage(
                conversation_id, message_id, row["sender_id"], row["sender_display_name"],
                datetime.fromisoformat(row["sent_at"]), row["body"], origins,
            ))
        return messages

    def add_knowledge(
        self, *, kind: str, title: str, body: str, subject_ref: str | None = None,
    ) -> int:
        if kind not in KNOWLEDGE_KINDS:
            raise ValueError(f"Unknown knowledge kind: {kind}")
        _require_text(title, "title")
        _require_text(body, "body")
        with self._write() as db:
            return db.execute("""
                INSERT INTO knowledge_entries (kind, title, body, subject_ref) VALUES (?, ?, ?, ?)
            """, (kind, title, body, subject_ref)).lastrowid

    def list_knowledge(
        self, *, kind: str | None = None, subject_ref: str | None = None,
    ) -> list[KnowledgeEntry]:
        with open_database(self._db_path) as db:
            rows = db.execute("""
                SELECT id, kind, title, body, subject_ref FROM knowledge_entries
                WHERE (? IS NULL OR kind = ?) AND (? IS NULL OR subject_ref = ?)
                ORDER BY id
            """, (kind, kind, subject_ref, subject_ref))
            return [KnowledgeEntry(*row) for row in rows]

    def submit_criterion(
        self, *, statement: str, reason: str | None = None, evidence: Iterable[MessageKey] = (),
        actor: str = "ai",
    ) -> CriterionRevision:
        with self._write() as db:
            criterion_id = db.execute("INSERT INTO decision_criteria DEFAULT VALUES").lastrowid
            return self._add_revision(db, criterion_id, 1, statement, reason, evidence, actor, "submitted")

    def revise_criterion(
        self, criterion_id: int, *, expected_revision: int, statement: str, reason: str | None,
        actor: str, evidence: Iterable[MessageKey] | None = None,
    ) -> CriterionRevision:
        """Create a revision for review, keeping any approved revision in effect."""
        with self._write() as db:
            current = self._checked_revision(db, criterion_id, expected_revision)
            return self._add_revision(
                db, criterion_id, current.revision + 1, statement, reason,
                current.evidence if evidence is None else evidence, actor, "revised",
            )

    def approve_criterion(
        self, criterion_id: int, *, expected_revision: int, actor: str,
    ) -> CriterionRevision:
        return self._review(criterion_id, expected_revision, actor, "approved")

    def reject_criterion(
        self, criterion_id: int, *, expected_revision: int, actor: str,
    ) -> CriterionRevision:
        return self._review(criterion_id, expected_revision, actor, "rejected")

    def get_criterion(self, criterion_id: int, *, revision: int | None = None) -> CriterionRevision:
        """Load the latest revision or a specific snapshot, including its evidence."""
        with open_database(self._db_path) as db:
            return self._get_criterion(db, criterion_id, revision)

    def approved_criteria(self) -> list[CriterionRevision]:
        """Return the latest approved revision per criterion for use by the AI."""
        with open_database(self._db_path) as db:
            return self._read_criteria(db, """
                revision.status = 'approved'
                AND NOT EXISTS (
                    SELECT 1 FROM criterion_revisions AS later
                    WHERE later.criterion_id = revision.criterion_id
                      AND later.status = 'approved' AND later.revision > revision.revision
                )
            """)

    def criterion_history(self, criterion_id: int) -> list[CriterionEvent]:
        with open_database(self._db_path) as db:
            rows = db.execute("""
                SELECT history.revision, history.action, history.actor, history.occurred_at,
                       revision.statement, revision.reason
                FROM criterion_history AS history
                JOIN criterion_revisions AS revision USING (criterion_id, revision)
                WHERE history.criterion_id = ?
                ORDER BY history.id
            """, (criterion_id,))
            return [
                CriterionEvent(row["revision"], row["action"], row["actor"],
                               datetime.fromisoformat(row["occurred_at"]), row["statement"], row["reason"])
                for row in rows
            ]

    def _add_revision(self, db, criterion_id, revision, statement, reason, evidence, actor, action):
        _require_text(statement, "statement")
        _require_text(actor, "actor")
        if reason is not None:
            if not isinstance(reason, str):
                raise ValueError("reason must be text when supplied")
            if not reason.strip():
                reason = None
        status = "pending" if reason is not None else "awaiting_reason"
        db.execute("""
            INSERT INTO criterion_revisions (criterion_id, revision, statement, reason, status)
            VALUES (?, ?, ?, ?, ?)
        """, (criterion_id, revision, statement, reason, status))
        try:
            db.executemany("""
                INSERT INTO criterion_evidence (criterion_id, revision, chat_id, message_id)
                VALUES (?, ?, ?, ?)
            """, ((criterion_id, revision, chat_id, message_id)
                  for chat_id, message_id in sorted(set(evidence))))
        except sqlite3.IntegrityError as exc:
            raise ValueError("Criterion evidence must reference registered messages") from exc
        self._record_event(db, criterion_id, revision, action, actor, utc_timestamp(datetime.now(UTC)))
        return self._get_criterion(db, criterion_id, revision)

    def _review(self, criterion_id, expected_revision, actor, status):
        _require_text(actor, "actor")
        with self._write() as db:
            current = self._checked_revision(db, criterion_id, expected_revision)
            if current.status not in ("pending", "awaiting_reason"):
                raise CriterionStateError("Only a revision awaiting review can be approved or rejected")
            if status == "approved" and current.reason is None:
                raise CriterionStateError("A reason is required before approval")
            timestamp = utc_timestamp(datetime.now(UTC))
            db.execute("""
                UPDATE criterion_revisions SET status = ?, approved_at = ?
                WHERE criterion_id = ? AND revision = ?
            """, (status, timestamp if status == "approved" else None, criterion_id, current.revision))
            self._record_event(db, criterion_id, current.revision, status, actor, timestamp)
            return self._get_criterion(db, criterion_id, current.revision)

    def _record_event(self, db, criterion_id, revision, action, actor, timestamp):
        db.execute("""
            INSERT INTO criterion_history (criterion_id, revision, action, actor, occurred_at)
            VALUES (?, ?, ?, ?, ?)
        """, (criterion_id, revision, action, actor, timestamp))

    def _checked_revision(self, db, criterion_id, expected_revision):
        current = self._get_criterion(db, criterion_id)
        if current.revision != expected_revision:
            raise CriterionStateError("The criterion has changed since it was read")
        return current

    def _get_criterion(self, db, criterion_id, revision=None):
        revisions = self._read_criteria(db, """
            revision.criterion_id = ? AND revision.revision = COALESCE(
                ?, (
                    SELECT MAX(latest.revision) FROM criterion_revisions AS latest
                    WHERE latest.criterion_id = revision.criterion_id
                )
            )
        """, (criterion_id, revision))
        if not revisions:
            raise KeyError(criterion_id)
        return revisions[0]

    def _read_criteria(self, db, condition, params=()):
        rows = db.execute(f"""
            SELECT revision.*, evidence.chat_id, evidence.message_id
            FROM criterion_revisions AS revision
            LEFT JOIN criterion_evidence AS evidence USING (criterion_id, revision)
            WHERE {condition}
            ORDER BY revision.criterion_id, revision.revision, evidence.chat_id, evidence.message_id
        """, params)
        criteria = []
        for (criterion_id, revision), group in groupby(
            rows, key=lambda row: (row["criterion_id"], row["revision"]),
        ):
            snapshot = list(group)
            row = snapshot[0]
            approved_at = row["approved_at"]
            evidence = tuple(
                (item["chat_id"], item["message_id"]) for item in snapshot if item["chat_id"] is not None
            )
            criteria.append(CriterionRevision(
                criterion_id, revision, row["statement"], row["reason"], row["status"],
                datetime.fromisoformat(approved_at) if approved_at is not None else None, evidence,
            ))
        return criteria
