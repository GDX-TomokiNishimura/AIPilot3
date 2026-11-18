from contextlib import closing
from datetime import UTC, datetime
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from teams_chat_import import GraphClient, ImportFailure, import_chat, main, six_months_before


CHAT_ID = "19:selected@unq.gbl.spaces"
CHAT_URL = "https://graph.microsoft.com/v1.0/chats/19%3Aselected%40unq.gbl.spaces"
MESSAGES_URL = CHAT_URL + "/messages"
TOKEN_URL = "https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/oauth2/v2.0/token"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
NEXT = "https://graph.microsoft.com/v1.0/chats/19:selected@unq.gbl.spaces/messages?$skiptoken=a%2Bb"
NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def message(message_id, sent_at="2026-08-01T12:30:00+09:00", **overrides):
    return {
        "id": message_id,
        "chatId": CHAT_ID,
        "createdDateTime": sent_at,
        "messageType": "message",
        "from": {"user": {"id": "employee-1", "displayName": "テスト社員"}},
        "body": {"contentType": "text", "content": "  確認しました\nありがとうございます。  "},
        **overrides,
    }


class MockOpener:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append(request)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())


class TeamsChatImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "chat.sqlite3"

    def graph(self, *responses, chat_type="oneOnOne", chat_id=CHAT_ID):
        opener = MockOpener(
            {"access_token": "mock-token"},
            {"id": chat_id, "chatType": chat_type},
            *responses,
        )
        return GraphClient(TOKEN_URL.split("/")[3], CLIENT_ID, "mock+secret", opener=opener), opener

    def test_imports_selected_chat_with_pagination_filters_and_no_duplicates(self):
        future = message("future", "2026-08-31T12:00:01Z")
        first = message("plain")
        html = message("html", "2026-06-01T12:00:00Z", body={
            "contentType": "html",
            "content": '<p><at id="0">社員</at> &amp; 同僚</p><div>確認<br>完了</div>'
                       '<attachment id="file">非表示</attachment>',
        }, attachments=[{"id": "file"}])
        attachment = message("attachment", body={
            "contentType": "html",
            "content": '<attachment id="file"></attachment><img src="https://example.invalid/file">',
        })
        event = message("event", messageType="systemEventMessage")
        deleted = message("deleted", deletedDateTime="2026-08-01T12:32:00Z")
        system_sender = message("application", **{"from": {"application": {"id": "app"}}})
        boundary = message("boundary", "2026-02-28T21:00:00+09:00")
        old = message("old", "2026-02-28T11:59:59Z",
                      lastModifiedDateTime="2026-08-30T00:00:00Z")
        pages = (
            {"value": [future, first, attachment, event, deleted, system_sender], "@odata.nextLink": NEXT},
            {"value": [first, html, boundary, old], "@odata.nextLink": NEXT + "extra"},
        )

        client, opener = self.graph(*pages)
        self.assertEqual(import_chat(client, CHAT_ID, self.db_path, now=NOW), 3)
        retry, _ = self.graph(*pages)
        self.assertEqual(import_chat(retry, CHAT_ID, self.db_path, now=NOW), 0)

        with closing(sqlite3.connect(self.db_path)) as db:
            rows = db.execute("""
                SELECT chat_id, message_id, sender_id, sender_display_name, sent_at, body
                FROM teams_chat_messages ORDER BY sent_at DESC
            """).fetchall()
        self.assertEqual(rows, [
            (CHAT_ID, "plain", "employee-1", "テスト社員", "2026-08-01T03:30:00.000000+00:00",
             first["body"]["content"]),
            (CHAT_ID, "html", "employee-1", "テスト社員", "2026-06-01T12:00:00.000000+00:00",
             "社員 & 同僚\n確認\n完了"),
            (CHAT_ID, "boundary", "employee-1", "テスト社員", "2026-02-28T12:00:00.000000+00:00",
             boundary["body"]["content"]),
        ])
        token_request, chat_request, list_request, next_request = opener.requests
        self.assertEqual(token_request.full_url, TOKEN_URL)
        self.assertEqual(token_request.get_method(), "POST")
        self.assertEqual(parse_qs(token_request.data.decode()), {
            "client_id": [CLIENT_ID],
            "client_secret": ["mock+secret"],
            "scope": ["https://graph.microsoft.com/.default"],
            "grant_type": ["client_credentials"],
        })
        self.assertEqual(chat_request.full_url, CHAT_URL)
        self.assertEqual(chat_request.get_header("Authorization"), "Bearer mock-token")
        self.assertEqual(list_request.full_url.split("?")[0], MESSAGES_URL)
        self.assertEqual(parse_qs(urlsplit(list_request.full_url).query),
                         {"$top": ["50"], "$orderby": ["createdDateTime desc"]})
        self.assertEqual(next_request.full_url, NEXT)

    def test_rejects_non_one_on_one_and_mismatched_chat_before_listing(self):
        for chat_type, chat_id in (("group", CHAT_ID), ("meeting", CHAT_ID), ("oneOnOne", "other")):
            with self.subTest(chat_type=chat_type, chat_id=chat_id):
                client, opener = self.graph(chat_type=chat_type, chat_id=chat_id)
                with self.assertRaisesRegex(ImportFailure, "matching oneOnOne"):
                    import_chat(client, CHAT_ID, self.db_path, now=NOW)
                self.assertEqual(len(opener.requests), 2)
                self.assertFalse(self.db_path.exists())

    def test_rejects_wrong_host_or_chat_pagination_without_requesting_it(self):
        for link in (
            NEXT.replace("graph.microsoft.com", "other.invalid"),
            NEXT.replace("selected", "other"),
            NEXT.replace("/messages", "/members"),
        ):
            with self.subTest(link=link):
                client, opener = self.graph({"value": [], "@odata.nextLink": link})
                with self.assertRaisesRegex(ImportFailure, "pagination link outside"):
                    import_chat(client, CHAT_ID, self.db_path, now=NOW)
                self.assertEqual(len(opener.requests), 3)

    def test_rejects_repeated_pagination_link(self):
        page = {"value": [], "@odata.nextLink": NEXT}
        client, opener = self.graph(page, page)
        with self.assertRaisesRegex(ImportFailure, "repeated pagination link"):
            import_chat(client, CHAT_ID, self.db_path, now=NOW)
        self.assertEqual(len(opener.requests), 4)

    def test_rejects_page_containing_a_message_from_another_chat(self):
        client, _ = self.graph({"value": [message("valid"), message("wrong", chatId="other")]})
        with self.assertRaisesRegex(ImportFailure, "message outside the configured chat"):
            import_chat(client, CHAT_ID, self.db_path, now=NOW)
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM teams_chat_messages").fetchone(), (0,))

    def test_graph_error_is_logged_and_cli_returns_failure(self):
        request_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        error = HTTPError(MESSAGES_URL, 403, "Forbidden", {"request-id": request_id},
                          io.BytesIO(b"private Graph error body"))
        client, _ = self.graph(error)
        settings = {
            "TEAMS_TENANT_ID": TOKEN_URL.split("/")[3],
            "TEAMS_CLIENT_ID": CLIENT_ID,
            "TEAMS_CLIENT_SECRET": "mock+secret",
            "TEAMS_CHAT_ID": CHAT_ID,
        }
        with patch.dict("os.environ", settings, clear=True), patch(
            "teams_chat_import.GraphClient", return_value=client
        ), self.assertLogs("teams_chat_import", "ERROR") as captured:
            self.assertEqual(main(["--db", str(self.db_path)]), 1)
        log = "\n".join(captured.output)
        self.assertIn(f"Graph list chat messages failed (HTTP 403 request_id={request_id})", log)
        for private_text in ("private Graph error body", "mock-token", "mock+secret", CHAT_ID):
            self.assertNotIn(private_text, log)

    def test_retry_after_a_failed_page_can_finish_without_duplicates(self):
        page = {"value": [message("first")], "@odata.nextLink": NEXT}
        error = HTTPError(NEXT, 503, "Unavailable", {}, io.BytesIO())
        client, _ = self.graph(page, error)
        with self.assertRaisesRegex(ImportFailure, "HTTP 503"):
            import_chat(client, CHAT_ID, self.db_path, now=NOW)
        retry, _ = self.graph(page, {"value": [message("second")]})
        self.assertEqual(import_chat(retry, CHAT_ID, self.db_path, now=NOW), 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            ids = db.execute("SELECT message_id FROM teams_chat_messages ORDER BY message_id").fetchall()
        self.assertEqual(ids, [("first",), ("second",)])

    def test_six_calendar_months_handles_year_boundary_and_leap_day(self):
        self.assertEqual(six_months_before(datetime(2026, 1, 31, tzinfo=UTC)),
                         datetime(2025, 7, 31, tzinfo=UTC))
        self.assertEqual(six_months_before(datetime(2024, 8, 31, tzinfo=UTC)),
                         datetime(2024, 2, 29, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
