import ast
import base64
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_dots.inbox import CursorLost, GmailReadClient, GmailReadonlySource
from hermes_dots.inbox.gmail import SCOPES


def raw_message(id="one", *, data=None):
    return {"id": id, "threadId": "thread", "internalDate": "1791792000000", "labelIds": ["UNREAD"],
            "payload": {"mimeType": "multipart/mixed", "headers": [
                {"name": "From", "value": "Alex <alex@example.com>"},
                {"name": "Subject", "value": "Acme update"}],
                "parts": [{"mimeType": "text/plain", "body": {"data": data or base64.urlsafe_b64encode(
                    "요청 자료를 확인해 주세요".encode()).decode().rstrip("=")}}]}}


class Transport:
    def __init__(self):
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        if path.endswith("/profile"):
            return {"historyId": "10"}
        if path.endswith("/messages"):
            if params.get("q") == "in:sent":
                return {"messages": [{"id": str(i)} for i in range(10)], "nextPageToken": "unused"}
            if params.get("pageToken") == "page2":
                return {"messages": [{"id": "two"}]}
            return {"messages": [{"id": "one"}], "nextPageToken": "page2"}
        if path.endswith("/history"):
            if params["startHistoryId"] == "lost":
                error = RuntimeError("Not found")
                error.status_code = 404
                raise error
            if params.get("pageToken") == "page2":
                return {"historyId": "12", "history": [{"messagesAdded": [{"message": {"id": "two"}}]}]}
            return {"historyId": "11", "nextPageToken": "page2", "history": [
                {"messagesAdded": [{"message": {"id": "one"}}, {"message": {"id": "one"}}]}]}
        return raw_message(path.rsplit("/", 1)[1])


def test_only_read_scope_and_no_mutating_endpoint_or_call():
    path = Path(__file__).parents[3] / "hermes_dots/inbox/gmail.py"
    source = path.read_text()
    assert SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)
    forbidden = ("gmail.send", "gmail.compose", "gmail.modify", "mail.google.com", "/send", "/modify",
                 "/drafts", "/trash", "/untrash", "/batchDelete", "/batchModify", "/labels")
    assert not any(word in source for word in forbidden)
    tree = ast.parse(source)
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
                   node.func.attr in {"send", "modify", "create", "delete", "trash", "untrash", "drafts",
                                      "post", "put", "patch", "batchModify", "batchDelete"}
                   for node in ast.walk(tree))
    with pytest.raises(ValueError):
        GmailReadonlySource(type("Client", (), {"scopes": SCOPES + ("extra",)})())


def test_paginated_backlog_uses_unread_three_day_boundary_and_early_cursor():
    transport = Transport()
    source = GmailReadonlySource(GmailReadClient(transport))
    since = datetime(2026, 10, 9, tzinfo=timezone.utc)
    result = source.poll(since=since)
    assert len(result.messages) == 2
    assert result.cursor == "10"
    assert transport.calls[0][0].endswith("/profile")
    lists = [params for path, params in transport.calls if path.endswith("/messages")]
    assert lists[0]["q"] == f"is:unread after:{int(since.timestamp())}"
    assert lists[1]["pageToken"] == "page2"
    assert result.messages[0]["sender"] == "alex@example.com"
    assert result.messages[0]["body"] == "요청 자료를 확인해 주세요"


def test_incremental_pages_deduplicate_ids_and_gap_resync():
    transport = Transport()
    source = GmailReadonlySource(GmailReadClient(transport))
    result = source.poll("10")
    assert [message["message_id"] for message in result.messages] == ["one", "two"]
    assert result.cursor == "12"
    assert not result.gap
    result = source.poll("lost")
    assert result.gap
    assert len(result.messages) == 2
    assert any(path.endswith("/messages") and params["q"] == "" for path, params in transport.calls)


def test_access_failure_not_resynced_as_lost_cursor():
    class Denied:
        def get(self, path, params):
            error = RuntimeError("Access denied")
            error.status_code = 401
            raise error
    with pytest.raises(RuntimeError, match="Access denied"):
        GmailReadonlySource(GmailReadClient(Denied())).poll("10")


def test_sent_voice_samples_bounded_to_ten():
    transport = Transport()
    result = GmailReadClient(transport).sent_mail()
    assert len(result) == 10
    assert len([path for path, params in transport.calls if path.endswith("/messages")]) == 1


def test_malformed_mime_decode_is_flagged():
    class Broken:
        def get(self, path, params):
            return raw_message(data="%%%%")
    assert GmailReadClient(Broken()).get_message("one")["malformed_mime"]
