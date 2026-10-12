"""
Send mail through the Gmail API with a service account (domain-wide delegation).

Same mechanism as the payment bot and the pod-intake prototype: the service account's
key signs a JWT whose ``sub`` is a real licensed mailbox, Google exchanges it for an
access token, and the Gmail API sends as that mailbox. A Google Group cannot be
impersonated, so to send *as* a group address (vehicles@circledelivers.com) the
impersonated user must have that address set up under Gmail "Send mail as"; otherwise
Gmail rewrites the From header to the user's own address. ``send_mail`` reports the From
header Gmail actually used, so that is visible rather than silent.

Scopes requested: gmail.compose (send) and gmail.readonly (to read the sent message's
headers back). Both must be authorised for this client id in the Google Admin console.
"""

from __future__ import annotations

import base64
import json
import time
from email.message import EmailMessage
from typing import Any

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
)


def load_service_account(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        info = json.load(handle)
    for key in ("client_email", "private_key", "token_uri"):
        if key not in info:
            raise ValueError(f"{path} is not a service account key (missing {key})")
    return info


def access_token(info: dict[str, Any], user: str, scopes: tuple[str, ...] = SCOPES) -> str:
    """Mint an access token for ``user`` via the service account's JWT grant."""

    from google.auth import jwt as google_jwt
    from google.auth.crypt import RSASigner

    now = int(time.time())
    payload = {
        "iss": info["client_email"],
        "scope": " ".join(scopes),
        "aud": info.get("token_uri", TOKEN_URL),
        "iat": now,
        "exp": now + 3600,
        "sub": user,
    }
    assertion = google_jwt.encode(RSASigner.from_service_account_info(info), payload)
    if isinstance(assertion, bytes):
        assertion = assertion.decode("ascii")
    response = requests.post(
        info.get("token_uri", TOKEN_URL),
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        timeout=30,
    )
    if response.status_code != 200:
        body = response.text[:300]
        hint = ""
        if "unauthorized_client" in body:
            hint = " (the service account's client id is not authorised for these scopes under Admin console > Security > API controls > Domain-wide delegation)"
        elif "invalid_grant" in body:
            hint = f" (the impersonated user {user!r} does not exist, or the key was revoked, or the clock is skewed)"
        raise RuntimeError(f"Google token exchange failed: HTTP {response.status_code} {body}{hint}")
    return response.json()["access_token"]


def build_message(*, from_addr: str, to: list[str], cc: list[str], subject: str, body: str, reply_to: str | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if reply_to:
        message["Reply-To"] = reply_to
    message["Subject"] = subject
    message.set_content(body)
    return message


def send_mail(
    sa_file: str,
    user: str,
    *,
    from_addr: str,
    to: list[str],
    cc: list[str] | None = None,
    subject: str,
    body: str,
    reply_to: str | None = None,
) -> dict[str, Any]:
    """Send and return ``{"id", "threadId", "from_as_sent", "to_as_sent"}``."""

    info = load_service_account(sa_file)
    token = access_token(info, user)
    headers = {"Authorization": f"Bearer {token}"}
    message = build_message(from_addr=from_addr, to=to, cc=cc or [], subject=subject, body=body, reply_to=reply_to)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    response = requests.post(f"{GMAIL_API}/messages/send", headers=headers, json={"raw": raw}, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"Gmail send failed: HTTP {response.status_code} {response.text[:300]}")
    sent = response.json()
    result: dict[str, Any] = {"id": sent.get("id"), "threadId": sent.get("threadId"), "from_as_sent": None, "to_as_sent": None}
    # Read the message back: Gmail may have rewritten From to the impersonated user.
    meta = requests.get(
        f"{GMAIL_API}/messages/{sent['id']}",
        headers=headers,
        params={"format": "metadata", "metadataHeaders": ["From", "To", "Cc"]},
        timeout=30,
    )
    if meta.status_code == 200:
        for header in (meta.json().get("payload") or {}).get("headers", []):
            if header.get("name") == "From":
                result["from_as_sent"] = header.get("value")
            elif header.get("name") == "To":
                result["to_as_sent"] = header.get("value")
    return result
