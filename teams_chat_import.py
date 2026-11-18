"""Import the last six calendar months of a single Teams chat into SQLite."""

import argparse
import calendar
from contextlib import closing
from datetime import UTC, datetime
from html.parser import HTMLParser
from http.client import HTTPException
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


GRAPH_ORIGIN = "https://graph.microsoft.com"
LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams_chat_messages (
    chat_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_display_name TEXT,
    sent_at TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
)
"""

INSERT_MESSAGE = """
INSERT INTO teams_chat_messages
    (chat_id, message_id, sender_id, sender_display_name, sent_at, body)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (chat_id, message_id) DO NOTHING
"""


class ImportFailure(Exception):
    """A failure with a diagnostic safe to log."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GraphClient:
    def __init__(self, tenant_id, client_id, client_secret, *, opener=None):
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._opener = opener if opener is not None else build_opener(NoRedirect())
        self._token = None

    def _request_json(self, request, operation):
        try:
            with self._opener.open(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as exc:
            request_id = exc.headers.get("request-id") if exc.headers else None
            trace = f" request_id={request_id}" if request_id and re.fullmatch(
                r"[0-9a-fA-F-]{36}", request_id
            ) else ""
            exc.close()
            raise ImportFailure(f"{operation} failed (HTTP {exc.code}{trace})") from None
        except (URLError, OSError, HTTPException) as exc:
            raise ImportFailure(f"{operation} failed ({type(exc).__name__})") from None
        except ValueError:
            raise ImportFailure(f"{operation} returned invalid JSON") from None
        if not isinstance(result, dict):
            raise ImportFailure(f"{operation} returned an unexpected JSON response")
        return result

    def get(self, url, operation):
        if self._token is None:
            form = urlencode({
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": f"{GRAPH_ORIGIN}/.default",
                "grant_type": "client_credentials",
            }).encode("ascii")
            token_url = f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
            request = Request(token_url, data=form, headers={
                "Content-Type": "application/x-www-form-urlencoded",
            })
            token = self._request_json(request, "Entra token request").get("access_token")
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
                raise ImportFailure("Entra token request returned an invalid access token")
            self._token = token

        request = Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        })
        return self._request_json(request, operation)


class MessageTextParser(HTMLParser):
    _blocks = {"blockquote", "div", "li", "ol", "p", "pre", "table", "tr", "ul"}
    _hidden_tags = {"attachment", "script", "style"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in self._hidden_tags:
            self._hidden.append(tag)
        if self._hidden:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag in self._blocks:
            self._line_break()

    def handle_endtag(self, tag):
        if self._hidden and self._hidden[-1] == tag:
            self._hidden.pop()
        if not self._hidden and tag in self._blocks:
            self._line_break()

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)

    def _line_break(self):
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")


def six_months_before(now):
    year, month = divmod(now.year * 12 + now.month - 1 - 6, 12)
    month += 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


def message_timestamp(message):
    try:
        sent_at = datetime.fromisoformat(message["createdDateTime"])
        if sent_at.tzinfo is None:
            raise ValueError
        return sent_at.astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        raise ImportFailure("Graph message has an invalid creation timestamp") from None


def message_row(message, chat_id, sent_at):
    if message.get("messageType") != "message" or message.get("deletedDateTime"):
        return None
    identity = message.get("from")
    sender = identity.get("user") if isinstance(identity, dict) else None
    if not isinstance(sender, dict) or not sender.get("id"):
        return None
    body = message.get("body")
    if not isinstance(body, dict):
        raise ImportFailure("Graph message has an invalid body")
    content = body.get("content")
    content_type = body.get("contentType")
    if not isinstance(content, str) or content_type not in ("text", "html"):
        return None
    if content_type == "html":
        parser = MessageTextParser()
        parser.feed(content)
        parser.close()
        content = "".join(parser.parts).strip()
    if not content.strip():
        return None
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise ImportFailure("Graph text message has no message ID")
    return (chat_id, message_id, sender["id"], sender.get("displayName"),
            sent_at.isoformat(timespec="microseconds"), content)


def validate_page_url(url, messages_path):
    try:
        parts = urlsplit(url)
        if (
            parts.scheme == "https"
            and parts.netloc == "graph.microsoft.com"
            and unquote(parts.path) == unquote(messages_path)
            and not parts.fragment
            and not re.search(r"[\x00-\x20\x7f]", url)
        ):
            return
    except ValueError:
        pass
    raise ImportFailure("Graph returned a pagination link outside the configured chat")


def import_chat(client, chat_id, db_path, *, now=None):
    if not re.fullmatch(r"[A-Za-z0-9:@._+~-]+", chat_id) or chat_id in {".", ".."}:
        raise ImportFailure("Configure a valid Graph chat ID in TEAMS_CHAT_ID")
    now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    cutoff = six_months_before(now)
    chat_path = f"/v1.0/chats/{quote(chat_id, safe='')}"
    chat = client.get(GRAPH_ORIGIN + chat_path, "Graph get chat")
    if chat.get("id") != chat_id or chat.get("chatType") != "oneOnOne":
        raise ImportFailure("The configured chat must be a matching oneOnOne chat")

    messages_path = f"{chat_path}/messages"
    query = urlencode({"$top": 50, "$orderby": "createdDateTime desc"})
    url = f"{GRAPH_ORIGIN}{messages_path}?{query}"
    seen_urls = set()
    inserted = 0
    try:
        descriptor = os.open(db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)

    with closing(sqlite3.connect(db_path)) as db:
        db.execute(SCHEMA)
        while url is not None:
            if not isinstance(url, str):
                raise ImportFailure("Graph returned an invalid pagination link")
            validate_page_url(url, messages_path)
            if url in seen_urls:
                raise ImportFailure("Graph returned a repeated pagination link")
            seen_urls.add(url)

            page = client.get(url, "Graph list chat messages")
            messages = page.get("value")
            if not isinstance(messages, list):
                raise ImportFailure("Graph message page has no message collection")
            rows = []
            reached_cutoff = False
            for message in messages:
                if not isinstance(message, dict) or message.get("chatId") != chat_id:
                    raise ImportFailure("Graph response contains a message outside the configured chat")
                sent_at = message_timestamp(message)
                if sent_at < cutoff:
                    reached_cutoff = True
                if cutoff <= sent_at <= now:
                    row = message_row(message, chat_id, sent_at)
                    if row is not None:
                        rows.append(row)
            with db:
                inserted += db.executemany(INSERT_MESSAGE, rows).rowcount
            # Graph returns newest first by createdDateTime across pages.
            url = None if reached_cutoff else page.get("@odata.nextLink")
    return inserted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Path to the SQLite database")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = {name: os.environ.get(name) for name in (
        "TEAMS_TENANT_ID", "TEAMS_CLIENT_ID", "TEAMS_CLIENT_SECRET", "TEAMS_CHAT_ID"
    )}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        LOGGER.error("Missing environment variables: %s", ", ".join(missing))
        return 1
    try:
        tenant_id = str(UUID(settings["TEAMS_TENANT_ID"]))
        client_id = str(UUID(settings["TEAMS_CLIENT_ID"]))
    except ValueError:
        LOGGER.error("TEAMS_TENANT_ID and TEAMS_CLIENT_ID must be UUIDs")
        return 1
    client = GraphClient(tenant_id, client_id, settings["TEAMS_CLIENT_SECRET"])
    try:
        inserted = import_chat(client, settings["TEAMS_CHAT_ID"], args.db)
    except ImportFailure as exc:
        LOGGER.error("Teams chat import failed: %s", exc)
        return 1
    except (OSError, sqlite3.Error) as exc:
        LOGGER.error("Teams chat database operation failed (%s)", type(exc).__name__)
        return 1
    LOGGER.info("Teams chat import completed. Inserted messages: %d", inserted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
