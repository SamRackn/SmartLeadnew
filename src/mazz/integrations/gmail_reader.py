"""Read side of the Gmail API: turn a `users.messages.get(format=full)` payload
into a plain `ParsedMessage`, and page through new mail incrementally.

Parsing is pure (a dict in, a dataclass out) so it is tested with fixtures and
no Google account. The API calls are in `new_message_ids` / `thread_messages`,
which take an injected ``service`` for the same reason.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterator


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    message_id: str
    thread_id: str | None
    from_address: str
    subject: str
    #: lower-cased header name -> value (first occurrence).
    headers: dict[str, str]
    #: text/plain if present, else the HTML flattened. Full, quotes included.
    text: str
    received_at: datetime
    labels: tuple[str, ...] = ()
    #: Concatenated text of every decodable part, including delivery-status
    #: parts. Used only to read a bounce.
    raw_text: str = field(default="", repr=False)


_ADDR = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


class _Flatten(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []

    def handle_data(self, data: str) -> None:
        self.out.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("br", "p", "div", "tr", "li"):
            self.out.append("\n")


def html_to_text(html: str) -> str:
    parser = _Flatten()
    parser.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.out)).strip()


def _decode(data: str | None) -> str:
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def bare_address(value: str) -> str:
    match = _ADDR.search(value or "")
    return match.group(0).lower() if match else (value or "").strip().lower()


def parse_gmail_message(msg: dict[str, Any]) -> ParsedMessage:
    payload = msg.get("payload") or {}
    headers: dict[str, str] = {}
    for h in payload.get("headers") or []:
        headers.setdefault(str(h.get("name", "")).lower(), str(h.get("value", "")))

    plain, html, everything = [], [], []
    for part in _walk(payload):
        text = _decode((part.get("body") or {}).get("data"))
        if not text:
            continue
        mime = str(part.get("mimeType", ""))
        if mime == "text/plain":
            plain.append(text)
        elif mime == "text/html":
            html.append(text)
        everything.append(text)

    body = "\n".join(plain) if plain else html_to_text("\n".join(html))
    millis = msg.get("internalDate")
    received = (
        datetime.fromtimestamp(int(millis) / 1000, tz=timezone.utc)
        if millis
        else datetime.now(timezone.utc)
    )
    return ParsedMessage(
        message_id=str(msg["id"]),
        thread_id=msg.get("threadId"),
        from_address=bare_address(headers.get("from", "")),
        subject=headers.get("subject", ""),
        headers=headers,
        text=body,
        received_at=received,
        labels=tuple(msg.get("labelIds") or ()),
        raw_text="\n".join(everything),
    )


# --------------------------------------------------------------------------
# API paging (service injected)
# --------------------------------------------------------------------------


class HistoryExpired(Exception):
    """Gmail no longer has history from our cursor (404). Fall back to a scan."""


def profile(service) -> dict[str, Any]:
    return service.users().getProfile(userId="me").execute()


def new_message_ids(service, start_history_id: str) -> tuple[list[str], str]:
    """Inbox messages added since the cursor, and the new cursor."""

    ids: list[str] = []
    cursor = start_history_id
    token = None
    while True:
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                    pageToken=token,
                )
                .execute()
            )
        except Exception as exc:  # googleapiclient.errors.HttpError
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                raise HistoryExpired(str(exc)) from exc
            raise
        for record in resp.get("history") or []:
            for added in record.get("messagesAdded") or []:
                mid = (added.get("message") or {}).get("id")
                if mid and mid not in ids:
                    ids.append(mid)
        cursor = str(resp.get("historyId") or cursor)
        token = resp.get("nextPageToken")
        if not token:
            return ids, cursor


def get_message(service, message_id: str) -> ParsedMessage:
    return parse_gmail_message(
        service.users().messages().get(userId="me", id=message_id, format="full").execute()
    )


def thread_messages(service, thread_id: str) -> list[ParsedMessage]:
    resp = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    return [parse_gmail_message(m) for m in resp.get("messages") or []]
