"""MacroPoint "Location Updates" callback receiver (AWS Lambda behind a function URL).

MacroPoint pushes every location it gets on a tracked load to the URL set under Manage
Company > Company Callback Urls > Location Updates, as an HTTP GET with the values in the
query string (Descartes MacroPoint docs, "Load Visibility Data Retrieval > Location
Updates")::

    ?MPOrderID=1234567&ID=Load-123&Latitude=39.84218&Longitude=-82.91753&Uncertainty=0
     &Street1=...&Street2=...&Neighborhood=&City=Independence&State=OH&Postal=44131
     &Country=USA&LocationDateTimeUTC=2020-01-01T12:00:00Z
     &ApproxLocationDateTimeInLocalTime=2020-01-01T12:00:00Z&DataSource=Ping
     &MPTrackingRequestID=A04E...&locator=2167894270

``ID`` is the order id the load was created with in MacroPoint: the vehicles team uses
the CargoTel load number (six digits). Circle's MacroPoint account also tracks the
TransportPro loads, so the callback brings every ping of every active load; only the ids
matching ``MP_ID_PATTERN`` (default six digits) are kept, the rest are answered 200 and
dropped.

What it keeps, in ``s3://MP_BUCKET/MP_PREFIX``::

    latest/<load id>.json    {"load_id", "latest": {ping}, "history": [{ping}, ...]}

newest ping first, ``MP_HISTORY`` (20) pings of history; an older or repeated ping never
replaces a newer one. The hourly tracking-update job reads ``latest/`` and writes the
place into the load's Location/ETA in CargoTel.

Who may call it: the URL carries a secret path segment (``MP_PATH_TOKEN``), e.g.
``https://<id>.lambda-url.us-east-1.on.aws/<token>``; MacroPoint appends its query string
to it. ``MP_BASIC_AUTH`` (``user:password``) is accepted instead, for MacroPoint's Basic
authentication option. Anything else gets 403. Calls from outside MacroPoint's published
callback addresses are logged (not refused: Descartes may add addresses).

Environment::

    MP_PATH_TOKEN     secret path segment (required)
    MP_BASIC_AUTH     optional "user:password" for Basic authentication
    MP_BUCKET         bucket (default circle-reload-suggestions)
    MP_PREFIX         key prefix (default macropoint/)
    MP_ID_PATTERN     regex of the order ids to keep (default ^\\d{6}$, CargoTel load numbers)
    MP_HISTORY        pings of history kept per load (default 20)
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qsl

DEFAULT_BUCKET = "circle-reload-suggestions"
DEFAULT_PREFIX = "macropoint/"
DEFAULT_ID_PATTERN = r"^\d{6}$"
DEFAULT_HISTORY = 20
#: MacroPoint's published callback source addresses (docs, "IP Addresses").
MACROPOINT_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "52.141.169.150/32", "52.141.169.192/28", "52.152.27.96/28", "52.237.140.174/32"))

_ignored = 0  # per warm container, for a sampled log line


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


def _response(status: int, body: str) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"content-type": "text/plain"}, "body": body}


def parse_when(text: str | None) -> datetime | None:
    """ISO 8601 (``2020-01-01T12:00:00Z``, with or without fraction or offset) -> aware UTC."""

    if not text:
        return None
    value = text.strip().replace(" ", "T")
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)  # .NET's seven-digit fractions
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when).astimezone(timezone.utc)


def _float(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


def request_params(event: Mapping[str, Any]) -> dict[str, str]:
    """The call's parameters, keys lower-cased: the query string, plus a form or JSON body
    when MacroPoint (or a test) POSTs instead."""

    params: dict[str, str] = {}
    raw = event.get("rawQueryString")
    if raw:
        pairs = parse_qsl(raw, keep_blank_values=True)
    else:
        pairs = list((event.get("queryStringParameters") or {}).items())
    for key, value in pairs:
        params[str(key).lower()] = value
    body = event.get("body")
    if body:
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8", "replace")
        body = body.strip()
        if body.startswith("{"):
            try:
                for key, value in json.loads(body).items():
                    params.setdefault(str(key).lower(), "" if value is None else str(value))
            except (ValueError, AttributeError):
                pass
        elif "=" in body:
            for key, value in parse_qsl(body, keep_blank_values=True):
                params.setdefault(key.lower(), value)
    return params


def ping_from_params(params: Mapping[str, str], received: datetime) -> dict[str, Any]:
    when = parse_when(params.get("locationdatetimeutc"))
    return {
        "load_id": (params.get("id") or "").strip(),
        "mp_order_id": params.get("mporderid", ""),
        "mp_tracking_request_id": params.get("mptrackingrequestid", ""),
        "when_utc": when.isoformat() if when else None,
        "when_local_text": params.get("approxlocationdatetimeinlocaltime", ""),
        "lat": _float(params.get("latitude")),
        "lng": _float(params.get("longitude")),
        "uncertainty_mi": _float(params.get("uncertainty")),
        "street1": params.get("street1", ""),
        "city": params.get("city", ""),
        "state": params.get("state", "") or params.get("stateorprovince", ""),
        "postal": params.get("postal", ""),
        "country": params.get("country", ""),
        "data_source": params.get("datasource", ""),
        "locator": params.get("locator", ""),
        "received_at": received.isoformat(timespec="seconds"),
    }


def authorized(event: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    token = (env.get("MP_PATH_TOKEN") or "").strip()
    if token:
        segments = [s for s in str(event.get("rawPath") or "").split("/") if s]
        if any(hmac.compare_digest(s, token) for s in segments):
            return True
    basic = (env.get("MP_BASIC_AUTH") or "").strip()
    if basic:
        headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
        auth = headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                given = base64.b64decode(auth[6:].strip()).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            return hmac.compare_digest(given, basic)
    return False


def from_macropoint(event: Mapping[str, Any]) -> bool:
    ip = ((event.get("requestContext") or {}).get("http") or {}).get("sourceIp", "")
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in net for net in MACROPOINT_NETWORKS)


def merge(existing: dict[str, Any] | None, ping: dict[str, Any], history: int = DEFAULT_HISTORY) -> tuple[dict[str, Any], bool]:
    """``(record, changed)``: the stored record with ``ping`` added. The newest ping (by
    ``when_utc``; a ping without a time sorts by when it arrived) becomes ``latest``; a
    ping already stored changes nothing."""

    def key(p: dict[str, Any]) -> str:
        return p.get("when_utc") or p.get("received_at") or ""

    pings = [] if not existing else ([existing["latest"]] if existing.get("latest") else []) + list(existing.get("history") or [])
    same = [p for p in pings if key(p) == key(ping) and p.get("lat") == ping.get("lat") and p.get("lng") == ping.get("lng")]
    if same:
        return existing or {}, False
    pings.append(ping)
    pings.sort(key=key, reverse=True)
    pings = pings[:history + 1]
    return {"load_id": ping["load_id"], "latest": pings[0], "history": pings[1:]}, True


def handle(event: Mapping[str, Any], *, s3: Any, env: Mapping[str, str], now: datetime | None = None) -> dict[str, Any]:
    global _ignored
    now = now or datetime.now(timezone.utc)
    if not (env.get("MP_PATH_TOKEN") or env.get("MP_BASIC_AUTH")):
        log("misconfigured", reason="no MP_PATH_TOKEN or MP_BASIC_AUTH set")
        return _response(500, "not configured")
    if not authorized(event, env):
        log("refused", path_len=len(str(event.get("rawPath") or "")),
            source_ip=((event.get("requestContext") or {}).get("http") or {}).get("sourceIp"))
        return _response(403, "forbidden")
    params = request_params(event)
    ping = ping_from_params(params, now)
    pattern = env.get("MP_ID_PATTERN") or DEFAULT_ID_PATTERN
    if not ping["load_id"] or not re.match(pattern, ping["load_id"]):
        _ignored += 1
        if _ignored % 500 == 1:
            log("ignored", reason="order id not a CargoTel load number", sample_id=ping["load_id"][:40], ignored_here=_ignored)
        return _response(200, "ignored")
    if not from_macropoint(event):
        log("unexpected_source", load_id=ping["load_id"],
            source_ip=((event.get("requestContext") or {}).get("http") or {}).get("sourceIp"))
    bucket = env.get("MP_BUCKET") or DEFAULT_BUCKET
    key = f"{env.get('MP_PREFIX') or DEFAULT_PREFIX}latest/{ping['load_id']}.json"
    existing = None
    try:
        existing = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception as exc:  # noqa: BLE001 - a missing key is the normal first ping
        if "NoSuchKey" not in type(exc).__name__ and "NoSuchKey" not in str(exc) and "404" not in str(exc):
            log("read_failed", load_id=ping["load_id"], error=str(exc)[:200])
            return _response(500, "storage error")
    record, changed = merge(existing, ping, int(env.get("MP_HISTORY") or DEFAULT_HISTORY))
    if changed:
        s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(record, indent=1).encode("utf-8"), ContentType="application/json")
    log("ping", load_id=ping["load_id"], when_utc=ping["when_utc"], place=f"{ping['city']}, {ping['state']}",
        source=ping["data_source"], stored=changed, newest=record.get("latest", {}).get("when_utc") == ping["when_utc"])
    return _response(200, "OK")


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    import boto3

    return handle(event or {}, s3=boto3.client("s3"), env=os.environ)
