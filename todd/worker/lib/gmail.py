"""Gmail client wrapper. The only module that talks to Google.

Scope is `gmail.readonly` and nothing else. The connected address comes from
`users.getProfile`, **not** the oauth2 userinfo endpoint — that needs `email`
and `openid` scopes we deliberately do not request (§7.5).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from todd.config import Settings
from todd.log import get_logger

logger = get_logger(__name__)

#: Read-only, and only this. Anything wider would let a bug delete mail.
SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.readonly"]

TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"


@dataclass(slots=True)
class Attachment:
    filename: str
    data: bytes


@dataclass(slots=True)
class Message:
    provider_msg_id: str
    from_address: str
    subject: str
    received_at_ms: int
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def has_pdf(self) -> bool:
        return any(a.filename.lower().endswith(".pdf") for a in self.attachments)


def _client_config(settings: Settings) -> dict[str, Any]:
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": [settings.google_oauth_redirect_uri],
        }
    }


def build_consent_url(settings: Settings, state: str) -> tuple[str, str | None]:
    """The URL the browser is sent to, and the PKCE verifier that pairs with it.

    `access_type=offline` with `prompt=consent` is what actually returns a
    refresh token — without the prompt, a user who has consented before gets
    an access token only, and the connection dies in an hour (§7.5).

    The verifier must be returned, not discarded. `Flow` turns PKCE on by
    default and puts a `code_challenge` in this URL; the exchange then happens
    on a *different* `Flow` instance, which without the matching verifier gets
    `(invalid_grant) Missing code verifier` from Google. It is stored against
    the state row, which is why `oauth_states.code_verifier` exists.
    """
    flow = Flow.from_client_config(_client_config(settings), scopes=SCOPES)
    flow.redirect_uri = settings.google_oauth_redirect_uri
    url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state,
    )
    return str(url), getattr(flow, "code_verifier", None)


def exchange_code(settings: Settings, code: str, code_verifier: str | None = None) -> Credentials:
    """Swap the one-time code for credentials carrying a refresh token."""
    flow = Flow.from_client_config(
        _client_config(settings), scopes=SCOPES, code_verifier=code_verifier
    )
    flow.redirect_uri = settings.google_oauth_redirect_uri
    flow.fetch_token(code=code)
    return flow.credentials  # type: ignore[no-any-return]


def credentials_from_refresh_token(settings: Settings, refresh_token: str) -> Credentials:
    creds = Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())  # type: ignore[no-untyped-call]
    return creds


class GmailClient:
    def __init__(self, credentials: Credentials) -> None:
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def address(self) -> str:
        """The connected mailbox, from getProfile — not oauth2 userinfo."""
        profile = self._service.users().getProfile(userId="me").execute()
        return str(profile.get("emailAddress", ""))

    def search_ids(self, query: str, *, limit: int = 500) -> list[str]:
        """Message ids matching the allowlist query, newest first."""
        ids: list[str] = []
        token: str | None = None
        while len(ids) < limit:
            response = (
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(100, limit - len(ids)),
                    pageToken=token,
                )
                .execute()
            )
            ids.extend(m["id"] for m in response.get("messages", []))
            token = response.get("nextPageToken")
            if not token:
                break
        return ids

    def fetch(self, message_id: str) -> Message:
        """One message with its PDF attachments.

        Email content is untrusted: only headers and PDF parts are read. The
        HTML body is never parsed, no link is followed, nothing is executed.
        """
        raw = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}
        message = Message(
            provider_msg_id=message_id,
            from_address=headers.get("from", ""),
            subject=headers.get("subject", ""),
            received_at_ms=int(raw.get("internalDate", 0)),
        )
        self._collect_pdfs(raw.get("payload", {}), message_id, message)
        return message

    def _collect_pdfs(self, part: dict[str, Any], message_id: str, into: Message) -> None:
        filename = part.get("filename") or ""
        body = part.get("body", {})

        if filename.lower().endswith(".pdf"):
            data = body.get("data")
            if not data and body.get("attachmentId"):
                fetched = (
                    self._service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=body["attachmentId"])
                    .execute()
                )
                data = fetched.get("data")
            if data:
                into.attachments.append(
                    Attachment(filename=filename, data=base64.urlsafe_b64decode(data))
                )

        for child in part.get("parts", []) or []:
            self._collect_pdfs(child, message_id, into)
