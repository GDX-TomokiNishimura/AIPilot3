"""Loopback-only knowledge administration server."""

import argparse
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urlsplit

from knowledge_db import CriterionStateError, KNOWLEDGE_KINDS, KnowledgeStore


LOOPBACK = "127.0.0.1"
ASSETS = Path(__file__).with_name("admin_web")
STATIC = {
    "/": ("index.html", "text/html"),
    "/admin.css": ("admin.css", "text/css"),
    "/admin.js": ("admin.js", "text/javascript"),
}
STATUS_FILTERS = ("pending", "awaiting_reason", "approved", "rejected")
CRITERION_ROUTE = re.compile(r"/api/criteria/([1-9][0-9]{0,17})(?:/(revise|approve|reject))?")
KNOWLEDGE_ROUTE = re.compile(r"/api/knowledge/([1-9][0-9]{0,17})")
MAX_REQUEST_BYTES = 256 * 1024
LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def local_reviewer(request: BaseHTTPRequestHandler) -> str:
    """Resolve the reviewer for the local session. A future SSO adapter can replace this."""
    return "ローカル利用者"


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


class AdminServer(ThreadingHTTPServer):
    def __init__(
        self, store: KnowledgeStore, port: int,
        reviewer_for_request: Callable[[BaseHTTPRequestHandler], str | None] = local_reviewer,
    ):
        self.store = store
        self.reviewer_for_request = reviewer_for_request
        super().__init__((LOOPBACK, port), AdminHandler)
        self.local_host = f"{LOOPBACK}:{self.server_port}"
        self.origin = f"http://{self.local_host}"


class AdminHandler(BaseHTTPRequestHandler):
    server: AdminServer
    timeout = 10

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        try:
            self._check_request()
            try:
                url = urlsplit(self.path)
            except ValueError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "リクエストURLが正しくありません。") from None
            if url.netloc or url.scheme or url.fragment:
                raise ApiError(HTTPStatus.BAD_REQUEST, "リクエストURLが正しくありません。")
            if method == "GET" and url.path in STATIC:
                filename, content_type = STATIC[url.path]
                self._send(HTTPStatus.OK, (ASSETS / filename).read_bytes(), content_type)
                return

            reviewer = self.server.reviewer_for_request(self)
            if not reviewer:
                raise ApiError(HTTPStatus.UNAUTHORIZED, "ログインが必要です。")
            params = self._query_params(url.query)
            self._json(HTTPStatus.OK, self._api(method, url.path, params, reviewer))
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "対象のデータが見つかりません。"})
        except CriterionStateError:
            self._json(HTTPStatus.CONFLICT, {
                "error": "判断基準が更新されたか、この状態では操作できません。ページを再読み込みしてください。",
            })
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            LOGGER.exception("Knowledge admin request failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "処理に失敗しました。"})

    def _check_request(self):
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if hosts != [self.server.local_host] or (origins and origins != [self.server.origin]):
            raise ApiError(HTTPStatus.FORBIDDEN, "ローカルの管理画面からアクセスしてください。")

    def _api(self, method, path, params, reviewer):
        store = self.server.store
        criterion = CRITERION_ROUTE.fullmatch(path)
        knowledge = KNOWLEDGE_ROUTE.fullmatch(path)
        if method == "GET":
            if path == "/api/dashboard":
                items = store.list_criteria()
                counts = Counter(item.status for item in items)
                return {"counts": {status: counts[status] for status in STATUS_FILTERS}, "recent": items[:5]}
            if path == "/api/criteria":
                status = self._filter(params, "status", STATUS_FILTERS)
                return {"items": store.list_criteria(status=status)}
            if criterion and criterion[2] is None:
                return store.get_criterion_detail(int(criterion[1]))
            if path == "/api/knowledge":
                kind = self._filter(params, "kind", KNOWLEDGE_KINDS)
                return {"items": store.list_knowledge(kind=kind)}
            if knowledge:
                return store.get_knowledge(int(knowledge[1]))

        if method == "POST" and criterion and criterion[2]:
            return self._review(int(criterion[1]), criterion[2], reviewer)
        raise ApiError(HTTPStatus.NOT_FOUND, "ページが見つかりません。")

    def _review(self, criterion_id, action, reviewer):
        data = self._read_json()
        revision = data.get("expected_revision")
        fields = {"expected_revision", "statement", "reason"} if action == "revise" else {"expected_revision"}
        if set(data) != fields or type(revision) is not int or revision < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "レビューの入力内容が正しくありません。")
        store = self.server.store
        if action == "approve":
            return store.approve_criterion(criterion_id, expected_revision=revision, actor=reviewer)
        if action == "reject":
            return store.reject_criterion(criterion_id, expected_revision=revision, actor=reviewer)

        statement, reason = data["statement"], data["reason"]
        if not isinstance(statement, str) or not statement.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "判断基準を入力してください。")
        if reason is not None and not isinstance(reason, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "判断理由をテキストで入力してください。")
        return store.revise_criterion(
            criterion_id, expected_revision=revision, statement=statement, reason=reason, actor=reviewer,
        )

    def _read_json(self):
        if (
            self.headers.get_content_type() != "application/json"
            or self.headers.get_all("Transfer-Encoding")
        ):
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON形式で送信してください。")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,9}", lengths[0]):
            raise ApiError(HTTPStatus.BAD_REQUEST, "リクエストの長さが正しくありません。")
        length = int(lengths[0])
        if length > MAX_REQUEST_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "入力が長すぎます。")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            for value in data.values():
                if isinstance(value, str):
                    value.encode("utf-8")  # Reject unpaired JSON surrogate escapes.
        except (ValueError, RecursionError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSONの内容が正しくありません。") from None
        return data

    @staticmethod
    def _query_params(query):
        try:
            pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=8)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "絞り込み条件が正しくありません。") from None
        params = dict(pairs)
        if len(params) != len(pairs):
            raise ApiError(HTTPStatus.BAD_REQUEST, "絞り込み条件が重複しています。")
        return params

    @staticmethod
    def _filter(params, name, allowed):
        value = params.get(name)
        if params and (set(params) != {name} or value not in allowed):
            raise ApiError(HTTPStatus.BAD_REQUEST, "絞り込み条件が正しくありません。")
        return value

    def _json(self, status, data):
        self._send(status, json.dumps(data, ensure_ascii=False, default=json_default).encode("utf-8"),
                   "application/json")

    def _send(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(content)


def serve(store, port):
    with AdminServer(store, port) as server:
        print(f"AIPilot3 ナレッジ管理: {server.origin}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--db", type=Path, help="Path to the local SQLite database")
    database.add_argument("--demo", action="store_true", help="Use a temporary DB with fictional data")
    parser.add_argument("--port", type=int, default=8080, help="Local TCP port (default: 8080)")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.demo:
        from knowledge_admin_demo import seed_demo

        with TemporaryDirectory(prefix="aipilot3-demo-") as directory:
            store = KnowledgeStore(Path(directory) / "demo.sqlite3")
            seed_demo(store)
            serve(store, args.port)
    else:
        serve(KnowledgeStore(args.db), args.port)


if __name__ == "__main__":
    main()
