from datetime import datetime
from http.client import HTTPConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import json
import unittest
from unittest.mock import patch

from knowledge_admin import AdminHandler, AdminServer
from knowledge_admin_demo import seed_demo
from knowledge_db import KnowledgeStore


class KnowledgeAdminTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "admin.sqlite3"
        self.store = KnowledgeStore(self.path)
        seed_demo(self.store)

        self.server = AdminServer(self.store, 0)
        self.addCleanup(self.server.server_close)
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

        logging = patch.object(AdminHandler, "log_request")
        logging.start()
        self.addCleanup(logging.stop)

    def request(self, path, *, body=None, headers=None):
        connection = HTTPConnection(*self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        method = "POST" if body is not None else "GET"
        request_headers = dict(headers or {})
        if isinstance(body, dict):
            request_headers.setdefault("Content-Type", "application/json")
            body = json.dumps(body).encode()
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        content = response.read().decode()
        if response.getheader("Content-Type").startswith("application/json"):
            content = json.loads(content)
        return response.status, response.headers, content

    def test_serves_japanese_ui_static_assets_and_dashboard(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn('<html lang="ja">', html)
        for asset in ("/admin.css", "/admin.js"):
            self.assertIn(asset, html)
            self.assertEqual(self.request(asset)[0], 200)
        self.assertEqual(self.request("/README.md")[0], 404)

        status, _, dashboard = self.request("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["counts"], dict(pending=1, awaiting_reason=1, approved=1, rejected=1))
        dates = [datetime.fromisoformat(item["updated_at"]) for item in dashboard["recent"]]
        self.assertEqual(len(dates), 4)
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_criteria_filters_and_detail_include_linked_messages_and_history(self):
        for value in ("pending", "awaiting_reason", "approved", "rejected"):
            status, _, response = self.request(f"/api/criteria?status={value}")
            self.assertEqual(status, 200)
            item, = response["items"]
            self.assertEqual(item["status"], value)
            self.assertIn("statement", item)
            self.assertIn("reason", item)
            self.assertIn("updated_at", item)
        self.assertEqual(self.request("/api/criteria?status=typo")[0], 400)

        pending, = self.request("/api/criteria?status=pending")[2]["items"]
        status, _, detail = self.request(f"/api/criteria/{pending['criterion_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["criterion"]["revision"], 2)
        self.assertEqual({message["message_id"] for message in detail["messages"]}, {"m1", "m2"})
        for message in detail["messages"]:
            self.assertTrue(message["sender_display_name"])
            self.assertIsNotNone(datetime.fromisoformat(message["sent_at"]).tzinfo)
            self.assertIn("在庫", message["body"])
        self.assertEqual([event["action"] for event in detail["history"]], ["submitted", "revised"])
        self.assertEqual(self.request("/api/criteria/999")[0], 404)

    def test_revises_approves_and_rejects_using_persisted_review_workflow(self):
        waiting, = self.request("/api/criteria?status=awaiting_reason")[2]["items"]
        route = f"/api/criteria/{waiting['criterion_id']}"
        self.assertEqual(self.request(f"{route}/approve", body={"expected_revision": 1})[0], 409)
        self.assertNotIn(waiting["criterion_id"], {c.criterion_id for c in self.store.approved_criteria()})

        status, _, revised = self.request(f"{route}/revise", body={
            "expected_revision": 1, "statement": "納期上の懸念を担当者に共有する", "reason": "遅延を防ぐため",
        })
        self.assertEqual(status, 200)
        self.assertEqual((revised["revision"], revised["status"]), (2, "pending"))
        self.assertEqual(self.request(f"{route}/approve", body={"expected_revision": 1})[0], 409)
        self.assertEqual(self.request(f"{route}/approve", body={"expected_revision": 2})[0], 200)

        pending, = self.request("/api/criteria?status=pending")[2]["items"]
        reject_route = f"/api/criteria/{pending['criterion_id']}/reject"
        self.assertEqual(self.request(reject_route, body={"expected_revision": pending["revision"]})[0], 200)
        reopened = KnowledgeStore(self.path)
        official_ids = {c.criterion_id for c in reopened.approved_criteria()}
        self.assertIn(waiting["criterion_id"], official_ids)
        self.assertNotIn(pending["criterion_id"], official_ids)
        self.assertEqual(reopened.get_criterion(pending["criterion_id"]).status, "rejected")
        history = reopened.criterion_history(waiting["criterion_id"])
        self.assertEqual([event.action for event in history], ["submitted", "revised", "approved"])
        self.assertEqual(history[-1].actor, "ローカル利用者")
        self.assertEqual(history[-1].reason, "遅延を防ぐため")

    def test_knowledge_filters_entries_and_returns_full_details(self):
        for kind in ("business_knowledge", "business_rule", "project", "employee_guidance"):
            status, _, result = self.request(f"/api/knowledge?kind={kind}")
            self.assertEqual(status, 200)
            entry, = result["items"]
            self.assertEqual(entry["kind"], kind)
            status, _, detail = self.request(f"/api/knowledge/{entry['id']}")
            self.assertEqual(status, 200)
            self.assertEqual(detail["title"], entry["title"])
            self.assertTrue(detail["body"])
        self.assertEqual(self.request("/api/knowledge?kind=typo")[0], 400)
        self.assertEqual(self.request("/api/knowledge/999")[0], 404)

    def test_restricts_origin_requires_json_and_uses_server_reviewer(self):
        pending, = self.request("/api/criteria?status=pending")[2]["items"]
        path = f"/api/criteria/{pending['criterion_id']}/approve"
        body = {"expected_revision": pending["revision"]}
        for headers in ({"Host": "external.invalid"}, {"Origin": "https://external.invalid"}):
            self.assertEqual(self.request(path, body=body, headers=headers)[0], 403)
        self.assertEqual(self.request(path, body=b"x", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request(path, body=b"{", headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request(path, body={**body, "actor": "偽装ユーザー"})[0], 400)
        self.assertEqual(self.store.get_criterion(pending["criterion_id"]).status, "pending")

        self.server.reviewer_for_request = lambda request: None
        self.assertEqual(self.request("/api/dashboard")[0], 401)
        self.assertEqual(self.request(path, body=body)[0], 401)
        self.server.reviewer_for_request = lambda request: "確認済みユーザー"
        self.assertEqual(self.request(path, body=body, headers={"Origin": self.server.origin})[0], 200)
        self.assertEqual(self.store.criterion_history(pending["criterion_id"])[-1].actor, "確認済みユーザー")
