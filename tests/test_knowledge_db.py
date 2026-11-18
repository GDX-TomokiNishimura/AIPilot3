from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from knowledge_db import CriterionStateError, KnowledgeStore, MessageSource
from teams_chat_import import import_chat


CHAT_ID = "19:dummy-mentoring@unq.gbl.spaces"
NOW = datetime(2026, 8, 31, tzinfo=UTC)
SENT_AT = datetime(2026, 8, 10, 15, 30, tzinfo=timezone(timedelta(hours=9)))
DUMMY_TEAMS_CHAT = (
    {
        "message_id": "message-1",
        "sender_id": "mentor",
        "sender_display_name": "ダミー指導担当",
        "body": "手配前に在庫数を確認してください。",
    },
    {
        "message_id": "message-2",
        "sender_id": "trainee",
        "sender_display_name": "ダミー新入社員",
        "body": "在庫数が不足している場合はどうしますか。",
    },
)


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "knowledge.sqlite3"
        self.store = KnowledgeStore(self.path)

    def add_message(self, index=0, *, chat_id=CHAT_ID, source="manual", source_ref=None):
        return self.store.register_message(
            **DUMMY_TEAMS_CHAT[index], chat_id=chat_id, sent_at=SENT_AT,
            source=source, source_ref=source_ref,
        )

    def test_registers_searches_and_deduplicates_conversations_across_sources(self):
        self.assertTrue(self.add_message(source_ref="notes:page-1"))
        self.assertTrue(self.add_message(1, source="graph", source_ref="teams:message-2"))
        self.assertTrue(self.add_message(chat_id="different-chat"))
        self.assertFalse(self.add_message(source_ref="notes:duplicate"))
        purview_replay = {
            **DUMMY_TEAMS_CHAT[0],
            "body": "会話の再登録で元の本文を上書きしない",
        }
        self.assertFalse(self.store.register_message(
            **purview_replay, chat_id=CHAT_ID, sent_at=SENT_AT, source="purview",
            source_ref="export:record-10",
        ))

        rows = self.store.search_messages("手配前", chat_id=CHAT_ID, source="purview")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.chat_id, CHAT_ID)
        self.assertEqual(row.message_id, "message-1")
        self.assertEqual(row.sender_id, "mentor")
        self.assertEqual(row.sender_display_name, "ダミー指導担当")
        self.assertEqual(row.sent_at.isoformat(), "2026-08-10T06:30:00+00:00")
        self.assertEqual(row.body, DUMMY_TEAMS_CHAT[0]["body"])
        self.assertEqual(row.sources, (
            MessageSource("manual", "notes:page-1"),
            MessageSource("purview", "export:record-10"),
        ))
        self.assertEqual(len(self.store.search_messages("在庫数", chat_id=CHAT_ID)), 2)
        self.assertEqual(len(self.store.search_messages(chat_id="different-chat")), 1)
        self.assertEqual(self.store.search_messages("%"), [])
        self.assertEqual([m.message_id for m in self.store.search_messages(source="graph")], ["message-2"])

    def test_can_fill_a_missing_source_reference_on_replay(self):
        self.assertTrue(self.add_message(source="graph"))
        self.assertFalse(self.add_message(source="graph", source_ref="teams:message-1"))
        row, = self.store.search_messages()
        self.assertEqual(row.sources, (MessageSource("graph", "teams:message-1"),))

    def test_records_each_knowledge_kind(self):
        cases = {
            "business_knowledge": "倉庫の稼働時間",
            "business_rule": "発注の締め時間",
            "project": "商品入れ替え",
            "employee_guidance": "在庫表の読み方を指導した",
            "employee_proficiency": "在庫表を自分で確認できる",
        }
        for kind, title in cases.items():
            self.store.add_knowledge(kind=kind, title=title, body=f"記録 {title}", subject_ref="dummy")
        self.assertEqual({record.kind: record.title for record in self.store.list_knowledge()}, cases)
        reopened = KnowledgeStore(self.path)
        entry, = reopened.list_knowledge(kind="employee_proficiency", subject_ref="dummy")
        self.assertEqual(entry.body, "記録 在庫表を自分で確認できる")
        self.assertEqual(reopened.list_knowledge(subject_ref="unknown"), [])

    def test_approves_only_after_reason_is_recorded_and_keeps_evidence_and_history(self):
        self.add_message()
        evidence = (CHAT_ID, "message-1")
        submitted = self.store.submit_criterion(
            statement="手配前に在庫数を確認する", reason=" ", evidence=(evidence, evidence),
        )
        self.assertEqual(submitted.status, "awaiting_reason")
        self.assertEqual(submitted.evidence, (evidence,))
        self.assertEqual(self.store.approved_criteria(), [])
        with self.assertRaisesRegex(CriterionStateError, "reason is required"):
            self.store.approve_criterion(submitted.criterion_id, expected_revision=1, actor="reviewer")

        revised = self.store.revise_criterion(
            submitted.criterion_id, expected_revision=1, statement="発注前に在庫数を確認する",
            reason="二重発注を防ぐため", actor="reviewer",
        )
        self.assertEqual(revised.status, "pending")
        self.assertEqual(revised.evidence, submitted.evidence)
        self.assertEqual(self.store.approved_criteria(), [])
        with self.assertRaisesRegex(CriterionStateError, "changed since"):
            self.store.approve_criterion(submitted.criterion_id, expected_revision=1, actor="reviewer")

        approved = self.store.approve_criterion(submitted.criterion_id, expected_revision=2, actor="reviewer")
        self.assertEqual(approved.status, "approved")
        self.assertIsNotNone(approved.approved_at)
        reopened = KnowledgeStore(self.path)
        self.assertEqual(reopened.get_criterion(submitted.criterion_id, revision=1), submitted)
        self.assertEqual(reopened.get_criterion(submitted.criterion_id), approved)
        self.assertEqual(reopened.approved_criteria(), [approved])
        history = reopened.criterion_history(submitted.criterion_id)
        self.assertEqual([(event.revision, event.action, event.actor) for event in history], [
            (1, "submitted", "ai"),
            (2, "revised", "reviewer"),
            (2, "approved", "reviewer"),
        ])
        self.assertEqual(history[0].statement, "手配前に在庫数を確認する")
        self.assertIsNone(history[0].reason)
        self.assertEqual(history[1].reason, "二重発注を防ぐため")
        self.assertEqual(history[2].occurred_at, approved.approved_at)

    def test_conflicting_candidates_and_rejected_edits_preserve_approved_criteria(self):
        submitted = self.store.submit_criterion(statement="在庫を確認する", reason="過剰発注防止")
        key = submitted.criterion_id
        original = self.store.approve_criterion(key, expected_revision=1, actor="reviewer")
        conflicting = self.store.submit_criterion(statement="在庫を確認しない", reason="手配を急ぐ")
        self.assertNotEqual(conflicting.criterion_id, key)
        rejected = self.store.reject_criterion(conflicting.criterion_id, expected_revision=1, actor="reviewer")
        self.assertEqual(rejected.status, "rejected")
        self.assertIsNone(rejected.approved_at)
        self.assertEqual(self.store.approved_criteria(), [original])

        self.store.revise_criterion(
            key, expected_revision=1, statement="全倉庫の在庫を確認する", reason="同時に確認する", actor="reviewer",
        )
        self.assertEqual(self.store.approved_criteria(), [original])
        self.store.reject_criterion(key, expected_revision=2, actor="reviewer")
        with self.assertRaisesRegex(CriterionStateError, "awaiting review"):
            self.store.approve_criterion(key, expected_revision=2, actor="reviewer")
        self.assertEqual([event.action for event in self.store.criterion_history(key)],
                         ["submitted", "approved", "revised", "rejected"])
        self.assertEqual(self.store.approved_criteria(), [original])

        self.store.revise_criterion(
            key, expected_revision=2, statement="対象倉庫の在庫を確認する", reason="対象拠点を限定する", actor="reviewer",
        )
        updated = self.store.approve_criterion(key, expected_revision=3, actor="reviewer")
        self.assertEqual(self.store.get_criterion(key, revision=1), original)
        self.assertEqual(self.store.approved_criteria(), [updated])

    def test_reasonless_candidate_can_be_rejected(self):
        submitted = self.store.submit_criterion(statement="早めに手配する")
        rejected = self.store.reject_criterion(submitted.criterion_id, expected_revision=1, actor="reviewer")
        self.assertEqual(rejected.status, "rejected")
        self.assertIsNone(rejected.reason)
        self.assertEqual(self.store.approved_criteria(), [])

    def test_evidence_integrity_rolls_back_failed_revision(self):
        self.add_message()
        submitted = self.store.submit_criterion(statement="在庫を確認する", reason="過剰発注防止")
        key = submitted.criterion_id
        with self.assertRaisesRegex(ValueError, "registered messages"):
            self.store.revise_criterion(
                key, expected_revision=1, statement="別の手順", reason="別の理由", actor="reviewer",
                evidence=((CHAT_ID, "message-1"), (CHAT_ID, "missing")),
            )
        self.assertEqual(self.store.get_criterion(key), submitted)
        self.assertEqual([event.action for event in self.store.criterion_history(key)], ["submitted"])

    def test_concurrent_reviews_only_record_one_approval(self):
        submitted = self.store.submit_criterion(statement="在庫を確認する", reason="過剰発注防止")
        key = submitted.criterion_id

        def approve(actor):
            try:
                self.store.approve_criterion(key, expected_revision=1, actor=actor)
                return True
            except CriterionStateError:
                return False

        with ThreadPoolExecutor(max_workers=2) as reviewers:
            outcomes = list(reviewers.map(approve, ("reviewer-1", "reviewer-2")))
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual([event.action for event in self.store.criterion_history(key)], ["submitted", "approved"])

    def test_preserves_original_graph_db_and_new_imports(self):
        legacy_path = self.path.parent / "legacy.sqlite3"
        legacy = (CHAT_ID, "legacy-message", "mentor", "指導担当", NOW.isoformat(), "既存の本文")
        with closing(sqlite3.connect(legacy_path)) as db, db:
            db.execute("""
                CREATE TABLE teams_chat_messages (
                    chat_id TEXT NOT NULL, message_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                    sender_display_name TEXT, sent_at TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                )
            """)
            db.execute("INSERT INTO teams_chat_messages VALUES (?, ?, ?, ?, ?, ?)", legacy)

        store = KnowledgeStore(legacy_path)
        old, = store.search_messages(source="graph")
        self.assertEqual(old.sources, (MessageSource("graph", None),))
        self.assertFalse(store.register_message(
            chat_id=CHAT_ID, message_id="legacy-message", sender_id="mentor", sent_at=NOW,
            body="再登録", source="purview", source_ref="export:legacy",
        ))
        old, = store.search_messages(source="purview")
        self.assertEqual(old.sources, (MessageSource("graph", None), MessageSource("purview", "export:legacy")))
        self.assertEqual(old.body, legacy[-1])

        def mock_graph():
            return Mock(**{"get.side_effect": [
                {"id": CHAT_ID, "chatType": "oneOnOne"},
                {"value": [
                    {
                        "id": "new-message",
                        "chatId": CHAT_ID,
                        "createdDateTime": SENT_AT.isoformat(),
                        "messageType": "message",
                        "from": {"user": {"id": "mentor", "displayName": "指導担当"}},
                        "body": {"contentType": "text", "content": "新規Graphメッセージ"},
                        "webUrl": "https://teams.example.invalid/messages/new",
                    },
                ]},
            ]})

        self.assertEqual(import_chat(mock_graph(), CHAT_ID, legacy_path, now=NOW), 1)
        self.assertEqual(import_chat(mock_graph(), CHAT_ID, legacy_path, now=NOW), 0)
        imported, = store.search_messages("新規", source="graph")
        self.assertEqual(imported.sources, (MessageSource("graph", "https://teams.example.invalid/messages/new"),))
        with closing(sqlite3.connect(legacy_path)) as db:
            self.assertEqual(db.execute(
                "SELECT * FROM teams_chat_messages WHERE message_id = 'legacy-message'"
            ).fetchone(), legacy)
        criterion = store.submit_criterion(
            statement="在庫を確認する", evidence=((CHAT_ID, "legacy-message"), (CHAT_ID, "new-message")),
        )
        self.assertEqual(criterion.evidence, ((CHAT_ID, "legacy-message"), (CHAT_ID, "new-message")))


if __name__ == "__main__":
    unittest.main()
