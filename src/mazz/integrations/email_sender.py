"""The sending rail. This is the module that replaced Smartlead.

**We send email now.** That sentence used to be false — `smartlead.py` carried a
banner saying the opposite, and a test asserted that no send/launch/trigger verb
existed anywhere on it. The client's decision is to run outbound out of mailboxes
he owns, so the verb lands here, once, deliberately, behind these controls:

* **The human gate is upstream and unchanged.** Guardrail 4 (no auto-send) is
  enforced at the approve route and by `is_approvable()`. Nothing in this module
  decides *whether* to send; it is handed one approved message and puts it on the
  wire. There is no "send all", no scheduler that approves, and no retry path
  that can resurrect a rejected draft.
* **Live sending is opt-in by explicit config, never by credential presence.**
  `EMAIL_SENDER` selects the adapter and defaults to `stub`. A service-account
  key sitting in the environment is not consent to start emailing strangers —
  the same rule `draft_composer` and `linkedin_provider` follow.
* **Failures raise.** A send that did not happen must never look like one that
  did: the caller marks the lead `sent` on a receipt, so a swallowed error would
  tell the client he contacted someone he never contacted.
* **No credential is stored in the database.** `mailboxes.credential_ref` NAMES a
  credential; this module resolves it from the environment at send time.

What we deliberately do NOT do here, per CLAUDE.md: warm mailboxes (a network
effect needing thousands of opted-in accounts — buy it), provision mailboxes or
domains, or send over a cheap bulk-SMTP provider, whose AUPs prohibit cold
outreach and whose IP pools are already burnt. Real sending runs from Google
Workspace accounts on burner domains, rotated, which is what `mailboxes` holds.
"""

from __future__ import annotations

import abc
import base64
import json
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from pathlib import Path
from typing import Any, Mapping

from mazz.integrations.base import (
    InstrumentedProvider,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
    find_repo_root,
    setting,
    utc_now,
)

__all__ = [
    "SENT_OUTBOX_FILENAME",
    "GMAIL_SEND_SCOPE",
    "OutgoingMessage",
    "SendReceipt",
    "EmailSender",
    "StubEmailSender",
    "GmailSender",
    "SenderNotConfigured",
    "build_mime",
    "get_email_sender",
]

SENT_OUTBOX_FILENAME = "sent_outbox.jsonl"
STUB_PROVIDER = "email-stub"
GOOGLE_PROVIDER = "google"

#: Send only. NOT `gmail.modify`, NOT `mail.google.com`. A compromised key for
#: this scope can send mail as the mailbox; it cannot read the client's inbox.
#: The unified inbox will need a read scope later — that is a separate grant.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class SenderNotConfigured(ProviderAuthError):
    """No credential resolves for this mailbox's ``credential_ref``."""


# --------------------------------------------------------------------------
# The message and the receipt
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    """One approved email, ready for the wire.

    ``body`` is the FULL rendered body — `pipeline.draft.render_body` has already
    appended the CAN-SPAM footer with the physical address and the one-click
    unsubscribe link. This module never composes and never edits copy; it would
    be the second place a footer could go missing, so it does not get the chance.
    """

    from_address: str
    from_name: str
    to_address: str
    subject: str
    body: str
    to_name: str | None = None
    #: Extra RFC 5322 headers. `List-Unsubscribe` lands here, and on a
    #: follow-up `In-Reply-To` / `References`.
    headers: Mapping[str, str] = field(default_factory=dict)
    #: Gmail threadId of the conversation a follow-up belongs to.
    thread_id: str | None = None


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """Proof that a specific message left a specific mailbox.

    There is no ``accepted: bool``. A send either produced a receipt or raised;
    a falsey receipt would be one more thing a caller could forget to check.
    """

    provider: str
    rfc_message_id: str
    sent_at: datetime
    provider_message_id: str | None = None
    provider_thread_id: str | None = None


def build_mime(message: OutgoingMessage, *, now: datetime | None = None) -> EmailMessage:
    """Render an `OutgoingMessage` to MIME, with the deliverability headers.

    `List-Unsubscribe` and `List-Unsubscribe-Post` are set here rather than left
    to the adapter: Gmail and Outlook both surface a native unsubscribe control
    from them, Google's bulk-sender rules require one-click for senders at
    volume, and a recipient who unsubscribes in one click does not press the
    spam button, which is what actually kills a burner domain. The footer link
    the body already carries is the other half of guardrail 6; these headers are
    not a substitute for it.
    """

    mime = EmailMessage()
    mime["From"] = formataddr((message.from_name, message.from_address))
    mime["To"] = formataddr((message.to_name or "", message.to_address))
    mime["Subject"] = message.subject
    mime["Date"] = format_datetime(now or utc_now())

    domain = message.from_address.rpartition("@")[2] or None
    mime["Message-ID"] = make_msgid(domain=domain)

    for name, value in message.headers.items():
        if name.lower() in {"from", "to", "subject", "date", "message-id"}:
            # Those four are ours. Letting a caller override them is how a
            # display-name spoof or a broken thread key gets in.
            raise ValueError(f"header {name!r} is set by build_mime, not by the caller")
        mime[name] = value

    mime.set_content(message.body)
    return mime


# --------------------------------------------------------------------------
# The interface
# --------------------------------------------------------------------------


class EmailSender(InstrumentedProvider, abc.ABC):
    """Put one approved message on the wire, or raise.

    Implementations must NOT: batch, queue, retry internally (the jobs table
    owns retry and backoff), rewrite copy, or decide that a message should not
    be sent — a caller that got this far has already passed the approval gate
    and the suppression re-check.
    """

    provider: str = "email"

    @abc.abstractmethod
    def send(self, message: OutgoingMessage, *, credential_ref: str) -> SendReceipt:
        """Send, and return the receipt. Raises `ProviderError` on any failure."""


# --------------------------------------------------------------------------
# Stub: the whole path, no credentials, no network
# --------------------------------------------------------------------------


class StubEmailSender(EmailSender):
    """Appends the real MIME to `sent_outbox.jsonl` instead of sending it.

    The bytes written are the bytes `GmailSender` would transmit, so the outbox
    is a review artifact rather than a log line: the whole approve → rotate →
    send → record path is exercisable with no Workspace account, which is also
    what keeps the live adapter's lazy imports honest.
    """

    provider = STUB_PROVIDER

    def __init__(
        self,
        *,
        outbox_path: Path | str | None = None,
        clock=utc_now,
    ) -> None:
        super().__init__()
        self.outbox_path = (
            Path(outbox_path) if outbox_path else find_repo_root() / SENT_OUTBOX_FILENAME
        )
        self._clock = clock

    def send(self, message: OutgoingMessage, *, credential_ref: str) -> SendReceipt:
        self._record("send", message.to_address, credential_ref)
        now = self._clock()
        mime = build_mime(message, now=now)
        rfc_message_id = mime["Message-ID"]

        payload = {
            "sent_at": now.isoformat(),
            "provider": self.provider,
            "credential_ref": credential_ref,
            "from": message.from_address,
            "to": message.to_address,
            "subject": message.subject,
            "rfc_message_id": rfc_message_id,
            "mime": mime.as_string(),
        }
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

        # Shaped like Gmail's, so nothing downstream learns the stub's shape.
        digest = base64.urlsafe_b64encode(rfc_message_id.encode()).decode().rstrip("=")
        return SendReceipt(
            provider=self.provider,
            rfc_message_id=rfc_message_id,
            sent_at=now,
            provider_message_id=f"stub-{digest[:16]}",
            provider_thread_id=f"stub-thread-{digest[:16]}",
        )


# --------------------------------------------------------------------------
# Live: Google Workspace
# --------------------------------------------------------------------------


class GmailSender(EmailSender):
    """Sends as a Workspace mailbox over the Gmail API.

    Auth is a service account with domain-wide delegation, impersonating the
    mailbox address (`credentials.with_subject`). Two reasons over SMTP with an
    app password: the sent copy lands in the mailbox's own Sent folder, so the
    client sees his outbound where he expects it and a reply threads against
    something real; and there is no per-mailbox secret to rotate, get revoked,
    or leak — burner domains usually mean several Workspace tenants, which is
    why the key is resolved per `credential_ref` rather than once globally.

    `credential_ref` resolves to `GMAIL_SA_<REF>` (uppercased, dashes to
    underscores), falling back to `GMAIL_SA_DEFAULT`. The value is either a path
    to the service-account JSON or the JSON itself.
    """

    provider = GOOGLE_PROVIDER

    def __init__(self, *, service_factory=None) -> None:
        super().__init__()
        # Injectable so tests can drive the mapping and the error translation
        # without a Google account. Nothing else overrides it.
        self._service_factory = service_factory or self._build_service

    # -- credential resolution --------------------------------------------

    @staticmethod
    def _credential_env_name(credential_ref: str) -> str:
        normalized = credential_ref.strip().upper().replace("-", "_").replace(".", "_")
        return f"GMAIL_SA_{normalized}"

    def _resolve_credential(self, credential_ref: str) -> str:
        for name in (self._credential_env_name(credential_ref), "GMAIL_SA_DEFAULT"):
            value = setting(name)
            if value:
                return str(value)
        raise SenderNotConfigured(
            f"no Google service-account credential for mailbox credential_ref "
            f"{credential_ref!r}: set {self._credential_env_name(credential_ref)} "
            "(or GMAIL_SA_DEFAULT) to the service-account JSON, or to a path to "
            "it. The key is never stored on the mailbox row.",
            provider=self.provider,
            target=credential_ref,
        )

    def _build_service(self, *, credential_ref: str, mailbox_address: str):
        try:  # lazy: the stub path must import with nothing installed
            from google.oauth2 import service_account  # type: ignore
            from googleapiclient.discovery import build  # type: ignore
        except ImportError as exc:  # pragma: no cover - needs the extra absent
            raise SenderNotConfigured(
                "GmailSender needs the google extra: pip install -e '.[google]'",
                provider=self.provider,
                target=mailbox_address,
            ) from exc

        raw = self._resolve_credential(credential_ref)
        try:
            if raw.lstrip().startswith("{"):
                info = json.loads(raw)
                creds = service_account.Credentials.from_service_account_info(
                    info, scopes=[GMAIL_SEND_SCOPE]
                )
            else:
                creds = service_account.Credentials.from_service_account_file(
                    raw, scopes=[GMAIL_SEND_SCOPE]
                )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SenderNotConfigured(
                f"service-account credential for {credential_ref!r} is unreadable "
                f"or malformed: {exc}",
                provider=self.provider,
                target=mailbox_address,
            ) from exc

        # Domain-wide delegation: act as this mailbox, not as the robot.
        delegated = creds.with_subject(mailbox_address)
        return build("gmail", "v1", credentials=delegated, cache_discovery=False)

    # -- send --------------------------------------------------------------

    def send(self, message: OutgoingMessage, *, credential_ref: str) -> SendReceipt:
        self._record("send", message.to_address, credential_ref)
        now = utc_now()
        mime = build_mime(message, now=now)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        service = self._service_factory(
            credential_ref=credential_ref, mailbox_address=message.from_address
        )
        try:
            # userId="me" is the impersonated subject, i.e. this mailbox.
            result = (
                service.users()
                .messages()
                .send(
                    userId="me",
                    body={"raw": raw, **({"threadId": message.thread_id} if message.thread_id else {})},
                )
                .execute()
            )
        except ProviderError:
            raise
        except Exception as exc:  # googleapiclient.errors.HttpError et al.
            raise self._translate(exc, message) from exc

        if not isinstance(result, Mapping) or not result.get("id"):
            raise ProviderBadResponse(
                f"Gmail accepted the send but returned no message id: {result!r}",
                provider=self.provider,
                target=message.to_address,
            )

        return SendReceipt(
            provider=self.provider,
            rfc_message_id=mime["Message-ID"],
            sent_at=now,
            provider_message_id=str(result.get("id")),
            provider_thread_id=(
                str(result["threadId"]) if result.get("threadId") else None
            ),
        )

    def _translate(self, exc: Exception, message: OutgoingMessage) -> ProviderError:
        """Map a Gmail failure onto the retryability the queue branches on.

        Getting this wrong is expensive in both directions: a retryable error
        classed permanent holds a lead that would have sent on the next attempt,
        and a permanent error classed retryable burns five attempts and forty
        minutes before anyone reads it.
        """

        status = getattr(getattr(exc, "resp", None), "status", None)
        if status is None:
            status = getattr(exc, "status_code", None)
        detail = f"{exc.__class__.__name__}: {exc}"
        target = message.to_address

        if status in (401, 403):
            # 403 is overloaded: quota exhaustion arrives here too, and that one
            # is worth retrying tomorrow rather than disabling the mailbox over.
            if "rateLimitExceeded" in detail or "quota" in detail.lower():
                return ProviderRateLimited(
                    f"Gmail quota exhausted for {message.from_address}: {detail}",
                    provider=self.provider,
                    target=target,
                    status_code=status,
                )
            return ProviderAuthError(
                f"Gmail refused {message.from_address}: {detail}. Check the "
                "service account's domain-wide delegation and that "
                f"{GMAIL_SEND_SCOPE} is authorised for this Workspace.",
                provider=self.provider,
                target=target,
                status_code=status,
            )
        if status == 429:
            return ProviderRateLimited(
                detail, provider=self.provider, target=target, status_code=status
            )
        if status is not None and 500 <= int(status) < 600:
            return ProviderUnavailable(
                detail, provider=self.provider, target=target, status_code=status
            )
        if status is not None and 400 <= int(status) < 500:
            return ProviderBadResponse(
                detail, provider=self.provider, target=target, status_code=status
            )
        # Transport-level: DNS, connection reset, timeout. Worth another go
        # as far as the provider is concerned -- but no status means the send
        # step cannot rule out delivery (see `send.definitely_not_sent`).
        return ProviderUnavailable(detail, provider=self.provider, target=target)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

_SENDERS: dict[str, Any] = {
    "stub": StubEmailSender,
    STUB_PROVIDER: StubEmailSender,
    GOOGLE_PROVIDER: GmailSender,
    "gmail": GmailSender,
}


def get_email_sender(provider: str | None = None, **kwargs: Any) -> EmailSender:
    """Return the configured sender. Defaults to the stub, on purpose.

    `EMAIL_SENDER=google` is the ONLY thing that switches real sending on.
    Presence of `GMAIL_SA_*` does not, and must never: the difference between
    the two is whether strangers receive email, and that is a decision someone
    makes explicitly, in config, not one that follows from a key appearing in
    the environment.
    """

    name = (provider or str(setting("EMAIL_SENDER", "stub") or "stub")).strip().lower()
    try:
        factory = _SENDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown EMAIL_SENDER {name!r}; known senders: "
            f"{', '.join(sorted(set(_SENDERS)))}"
        ) from None
    return factory(**kwargs)
