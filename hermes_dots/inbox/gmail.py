"""Gmail reads only. Authentication is supplied by the host, never loaded here.

The host signs in with SCOPES and injects either a read client or a GET transport.
No Google SDK dependency or network activity occurs at construction time.
"""

import base64
from datetime import datetime, timezone
from email.utils import parseaddr
from urllib.parse import quote

from .sources import CursorLost, MailBatch


SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)


class GmailReadonlySource:
    scopes = SCOPES

    def __init__(self, client, *, lost_cursor_errors=(CursorLost,)):
        if tuple(client.scopes) != SCOPES:
            raise ValueError("Mail access requires exactly the read-only scope")
        self.client = client
        self.lost_cursor_errors = lost_cursor_errors

    def poll(self, cursor=None, *, since=None):
        gap = False
        if cursor is None:
            result = self.client.list_messages(unread_only=True, since=since)
        else:
            try:
                result = self.client.history(cursor)
            except self.lost_cursor_errors:
                result = self.client.list_messages(unread_only=False)
                gap = True
        return MailBatch(tuple(result["messages"]), str(result["cursor"]), gap)

    def sent_mail(self):
        return self.client.sent_mail()


class GmailReadClient:
    """Injected transport: get(path, params) returns a decoded JSON object.

    It must raise an exception with status_code=404 for an expired history ID.
    Authentication/credential storage and OAuth UI belong to the host adapter.
    """

    scopes = SCOPES
    base = "/gmail/v1/users/me"

    def __init__(self, transport):
        self.transport = transport

    def _pages(self, resource, params, key, *, limit=None):
        items, cursor = [], None
        while True:
            result = self.transport.get(self.base + resource, dict(params))
            items.extend(result.get(key, ()))
            cursor = result.get("historyId", cursor)
            if limit is not None and len(items) >= limit:
                return items[:limit], cursor
            token = result.get("nextPageToken")
            if not token:
                return items, cursor
            params = {**params, "pageToken": token}

    def list_messages(self, *, unread_only=False, since=None):
        # Snapshot before listing: mail arriving during pagination is caught by history.
        cursor = str(self.transport.get(self.base + "/profile", {})["historyId"])
        query = "is:unread" if unread_only else ""
        if since is not None:
            query += f" after:{int(since.timestamp())}"
        refs, _ = self._pages("/messages", {"q": query.strip()}, "messages")
        messages = [self.get_message(ref["id"]) for ref in refs]
        if since is not None:
            messages = [m for m in messages if datetime.fromisoformat(m["date"]) >= since]
        return {"messages": messages, "cursor": cursor}

    def history(self, cursor):
        try:
            history, end = self._pages("/history", {"startHistoryId": cursor,
                                                  "historyTypes": "messageAdded"}, "history")
        except Exception as error:
            if getattr(error, "status_code", None) == 404:
                raise CursorLost("Mail history gap; full resync required") from None
            raise
        ids = dict.fromkeys(entry["message"]["id"] for record in history
                            for entry in record.get("messagesAdded", ()))
        return {"messages": [self.get_message(id) for id in ids], "cursor": str(end or cursor)}

    def get_message(self, message_id):
        raw = self.transport.get(self.base + "/messages/" + quote(message_id, safe=""), {"format": "full"})
        payload = raw.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", ())}
        parts, malformed = [], False

        def read(part):
            nonlocal malformed
            data = part.get("body", {}).get("data")
            if data and part.get("mimeType") == "text/plain":
                try:
                    parts.append(base64.b64decode(data + "=" * (-len(data) % 4),
                                                 altchars=b"-_", validate=True).decode("utf-8"))
                except (ValueError, UnicodeError):
                    malformed = True
            for child in part.get("parts", ()):
                read(child)

        read(payload)
        body = "\n".join(parts) or raw.get("snippet", "")
        return {"message_id": raw["id"], "thread_id": raw["threadId"],
                "sender": parseaddr(headers.get("from", ""))[1],
                "subject": headers.get("subject", ""), "body": body,
                "date": datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc).isoformat(),
                "unread": "UNREAD" in raw.get("labelIds", ()), "malformed_mime": malformed or not body}

    def sent_mail(self):
        refs, _ = self._pages("/messages", {"q": "in:sent", "maxResults": 10}, "messages", limit=10)
        return [self.get_message(ref["id"]) for ref in refs[:10]]
