"""
Sender for the reload-suggestion drafts the watcher writes. The bot sends directly; no
person forwards anything.

Modes
-----
* ``auto`` (default): To the carrier's dispatcher, Cc the load's sales rep (and the
  secondary sales rep when set), Reply-To the shared vehicles@ group so every reply lands
  where the team reads it. The sales rep is the only Circle person named in the email.
* ``pilot`` (optional switch): the email goes To the sales rep instead, with the carrier's
  address shown at the top, for a review period if one is ever wanted.

What happens to each draft (``<out-dir>/<load>_<carrier>.json`` from ``dispatch_watcher``)
--------------------------------------------------------------------------------------
1. Skip if already in the sent log (key = watcher event key), if the draft file is older
   than ``--draft-max-age-hours`` (default 12; the truck has moved on), if the carrier's
   email or name is on the do-not-email list, or if the carrier has no email at all.
2. Look the sales rep up in ``reps.csv`` (``name,email[,title]``; CargoTel's ``LAST,
   FIRST`` and ``First Last`` both match). No sales rep on the load, or no email for
   them, means hold the draft (``--fallback-to`` can name an inbox for the pilot).
3. One email per carrier per ``--carrier-cooldown-hours`` (default 24). The carrier is
   identified by its email address, so "88 Transpoort LLC" and "88 TRANSPOORT LLC" are
   the same carrier. A second truck of the same carrier the same day is held.
4. Trim the suggested loads: drop any load this carrier was already offered within
   ``--repeat-window-hours`` (default 72); drop any load already offered to
   ``--offer-cap`` other carriers (default 3) in the last ``--offer-window-hours``
   (default 24), so seven trucks near the same yard do not all chase the same five
   loads; with ``--recheck``, drop loads no longer on the Load Build search.
5. Re-render the email with what is left; skip the draft if nothing is left.
6. Send through Amazon SES (``--send``), or write it to ``outbox/`` and print it
   (default, a dry run). Record it in the sent log and the offers file either way.

Usage::

    python send_drafts.py --reps reps.csv                       # dry run: outbox/ only
    python send_drafts.py --reps reps.csv --send --from reloads@circledelivers.com
    python send_drafts.py --reps reps.csv --mode auto --send --recheck
    python send_drafts.py --reps reps.csv --fallback-to owner@circledelivers.com

Files it keeps beside the drafts: ``sent_log.jsonl`` (one line per send), ``offers.json``
(load id -> when it was offered to whom), ``do_not_email.txt`` (one email or carrier name
per line). ``boto3`` is imported only when ``--send`` is used.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

try:  # .env holds MAIL_FROM and the AWS profile; optional
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

from cargotel_search import CargoTelSession, Grid, SearchParams, parse_grid
from suggest_reloads import (
    DEFAULT_MILES,
    DEFAULT_SAVED_ID,
    Carrier,
    DispatchedLoad,
    Stop,
    Suggestion,
    email_text,
    nice_name,
    render_email,
)

DEFAULT_DRAFTS = "drafts"
DEFAULT_OUTBOX = "outbox"
DEFAULT_SENT_LOG = "sent_log.jsonl"
DEFAULT_OFFERS = "offers.json"
DEFAULT_DO_NOT_EMAIL = "do_not_email.txt"
DEFAULT_OFFER_CAP = 3
DEFAULT_OFFER_WINDOW_HOURS = 24
#: One email per carrier per this many hours, whatever trucks or loads are behind it.
DEFAULT_CARRIER_COOLDOWN_HOURS = 24
#: A load is never offered to the same carrier twice within this many hours.
DEFAULT_REPEAT_WINDOW_HOURS = 72
#: A draft held longer than this is not sent; the truck has moved on.
DEFAULT_DRAFT_MAX_AGE_HOURS = 12
PILOT_NOTE = (
    "PILOT: this went to you first. If the suggestions look right, forward it to the "
    "carrier at {carrier_email} (delete this note). If not, ignore it and tell the owner why."
)


# --------------------------------------------------------------------------- #
# Reps and lists                                                               #
# --------------------------------------------------------------------------- #


def _name_key(name: str) -> str:
    """``LINK, KYLE``, ``Kyle Link`` and ``kyle  link`` all become ``kyle link``."""

    return " ".join(nice_name(name).lower().split())


def load_rep_details(path: str) -> dict[str, dict[str, str]]:
    """``reps.csv`` -> {name key: {"email", "title"}}.

    Columns: ``name,email[,title]`` (header optional, ``#`` lines ignored). CargoTel names
    carry a comma ("LINK, KYLE"); unquoted, that splits the name into two fields, so the
    row is read around the email: the first field containing ``@`` is the email,
    everything before it is the name, the field after it (if any) is the title.
    """

    reps: dict[str, dict[str, str]] = {}
    if not path or not os.path.exists(path):
        return reps
    with open(path, encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 2 or row[0].lstrip().startswith("#"):
                continue
            at = next((i for i, cell in enumerate(row) if "@" in cell), None)
            if at is None or at == 0:
                continue
            name = ",".join(row[:at]).strip()
            if not name or name.lower() == "name":
                continue
            rest = [c.strip() for c in row[at + 1:]]
            reps[_name_key(name)] = {
                "email": row[at].strip(),
                "title": rest[0] if rest and rest[0] else "",
            }
    return reps


def load_reps(path: str) -> dict[str, str]:
    """``reps.csv`` -> {name key: email} (see :func:`load_rep_details`)."""

    return {key: rep["email"] for key, rep in load_rep_details(path).items()}


def rep_email_for(name: str | None, reps: dict[str, str]) -> str | None:
    if not name:
        return None
    return reps.get(_name_key(name))


def rep_signature_for(name: str | None, details: dict[str, dict[str, str]]) -> dict[str, str]:
    """Signature extras for the sales rep: ``{"title", "email"}`` where known."""

    if not name:
        return {}
    rep = details.get(_name_key(name)) or {}
    return {k: v for k, v in rep.items() if v}


def load_do_not_email(path: str) -> set[str]:
    entries: set[str] = set()
    if not path or not os.path.exists(path):
        return entries
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip().lower()
            if line:
                entries.add(" ".join(line.split()))
    return entries


def is_blocked(carrier_email: str | None, carrier_name: str | None, blocked: set[str]) -> bool:
    if carrier_email and carrier_email.strip().lower() in blocked:
        return True
    if carrier_name and " ".join(carrier_name.lower().split()) in blocked:
        return True
    return False


# --------------------------------------------------------------------------- #
# Drafts                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class Draft:
    path: str
    data: dict[str, Any]

    @property
    def key(self) -> str:
        event = self.data.get("event") or {}
        return f"{event.get('load_id')}|{' '.join((event.get('carrier') or '').split()).lower()}|{' '.join((event.get('driver') or '').split()).lower()}"

    @property
    def load_id(self) -> str:
        return str((self.data.get("event") or {}).get("load_id") or (self.data.get("dispatched") or {}).get("load_id") or "?")

    def drafted_at(self) -> datetime | None:
        """When the draft was written (the file's mtime; the event time is CargoTel's
        clock and may be in another time zone)."""

        try:
            return datetime.fromtimestamp(os.path.getmtime(self.path))
        except OSError:
            return None


def read_drafts(drafts_dir: str) -> list[Draft]:
    drafts = []
    for path in sorted(glob.glob(os.path.join(drafts_dir, "*.json"))):
        with open(path, encoding="utf-8") as handle:
            try:
                drafts.append(Draft(path, json.load(handle)))
            except json.JSONDecodeError:
                continue
    return drafts


def rebuild(draft: Draft) -> tuple[DispatchedLoad, list[Suggestion]]:
    """The watcher serialised dataclasses with ``asdict``; put them back."""

    d = draft.data.get("dispatched") or {}
    load = DispatchedLoad(
        load_id=str(d.get("load_id") or draft.load_id),
        customer=d.get("customer"),
        origin=Stop(**(d.get("origin") or {})),
        destination=Stop(**(d.get("destination") or {})),
        carrier=Carrier(**(d.get("carrier") or {})),
        dispatch_rep_id=d.get("dispatch_rep_id"),
        dispatch_rep=d.get("dispatch_rep"),
        sales_rep_id=d.get("sales_rep_id"),
        sales_rep=d.get("sales_rep"),
        sales_rep2_id=d.get("sales_rep2_id"),
        sales_rep2=d.get("sales_rep2"),
        vehicles=list(d.get("vehicles") or []),
        esigned=bool(d.get("esigned", False)),
    )
    picks = [Suggestion(**p) for p in (draft.data.get("picks") or [])]
    return load, picks


# --------------------------------------------------------------------------- #
# Offer cap and re-check                                                       #
# --------------------------------------------------------------------------- #


def carrier_key(email: str | None, name: str | None) -> str:
    """The identity used for duplicate checks: the carrier's email address when the load
    page has one (spelling of the name varies between loads), else the normalised name."""

    if email and email.strip():
        return email.strip().lower()
    return " ".join((name or "").split()).lower()


class Offers:
    """``offers.json``: load id -> [{"carrier": <carrier key>, "at": ISO}, ...]."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict[str, list[dict[str, str]]] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                self.data = json.load(handle)

    def recent(self, load_id: str, now: datetime, window_hours: float) -> list[dict[str, str]]:
        cutoff = now - timedelta(hours=window_hours)
        out = []
        for entry in self.data.get(str(load_id), []):
            try:
                when = datetime.fromisoformat(entry["at"])
            except (KeyError, ValueError):
                continue
            if when >= cutoff:
                out.append(entry)
        return out

    def count(self, load_id: str, now: datetime, window_hours: float, exclude_carrier: str | None = None) -> int:
        """Other carriers this load was offered to within the window."""

        key = " ".join((exclude_carrier or "").split()).lower()
        return sum(1 for e in self.recent(load_id, now, window_hours) if " ".join((e.get("carrier") or "").split()).lower() != key)

    def offered_to(self, load_id: str, carrier: str, now: datetime, window_hours: float) -> bool:
        """True when this very carrier already got this load within the window."""

        key = " ".join((carrier or "").split()).lower()
        return any(" ".join((e.get("carrier") or "").split()).lower() == key for e in self.recent(load_id, now, window_hours))

    def add(self, load_ids: list[str], carrier: str, now: datetime) -> None:
        for load_id in load_ids:
            self.data.setdefault(str(load_id), []).append({"carrier": carrier, "at": now.isoformat(timespec="seconds")})

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2)
        os.replace(tmp, self.path)


def apply_offer_cap(picks: list[Suggestion], offers: Offers, carrier: str, now: datetime, cap: int, window_hours: float) -> tuple[list[Suggestion], list[Suggestion]]:
    """Drop loads already offered to ``cap`` other carriers within the window."""

    kept, dropped = [], []
    for pick in picks:
        if offers.count(pick.load_id, now, window_hours, exclude_carrier=carrier) >= cap:
            dropped.append(pick)
        else:
            kept.append(pick)
    return kept, dropped


def drop_repeats(picks: list[Suggestion], offers: Offers, carrier: str, now: datetime, window_hours: float) -> tuple[list[Suggestion], list[Suggestion]]:
    """Drop loads this same carrier was already offered within the window, so a second
    email to the carrier never repeats what the first one said."""

    kept, dropped = [], []
    for pick in picks:
        (dropped if offers.offered_to(pick.load_id, carrier, now, window_hours) else kept).append(pick)
    return kept, dropped


def recheck_picks(picks: list[Suggestion], grid: Grid) -> tuple[list[Suggestion], list[Suggestion]]:
    """Keep a pick only if it (or, for a built load, one of its members) is still an
    unassigned row on the Load Build search."""

    open_ids = {row.load_id for row in grid.rows if row.load_id and not row.load.get("carrier_id")}
    kept, dropped = [], []
    for pick in picks:
        ids = {pick.load_id, *pick.member_ids}
        (kept if ids & open_ids else dropped).append(pick)
    return kept, dropped


def fetch_recheck_grid(session: CargoTelSession, zip_code: str, miles: int = DEFAULT_MILES, saved_id: str = DEFAULT_SAVED_ID) -> Grid:
    status, html, _ = session.fetch_search_html(SearchParams(saved_id, radius_origin=zip_code, radius_miles=miles, per_page="All"))
    grid = parse_grid(html, status)
    if grid.signin_page:
        raise RuntimeError("CargoTel answered with the sign-in page; the session cookie is stale")
    return grid


# --------------------------------------------------------------------------- #
# Composition and transport                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class Message:
    to: list[str]
    cc: list[str]
    reply_to: list[str]
    subject: str
    body: str
    mode: str
    carrier_email: str
    rep_email: str | None
    load_id: str
    carrier: str
    picks: list[str] = field(default_factory=list)

    def text(self) -> str:
        head = [f"To: {', '.join(self.to)}"]
        if self.cc:
            head.append(f"Cc: {', '.join(self.cc)}")
        if self.reply_to:
            head.append(f"Reply-To: {', '.join(self.reply_to)}")
        head.append(f"Subject: {self.subject}")
        return "\n".join(head) + "\n\n" + self.body + "\n"


def compose(load: DispatchedLoad, picks: list[Suggestion], draft: Draft, *, mode: str, rep_emails: list[str], miles: int, show_pay: bool,
            reply_to: str | None = None, rep_signature: dict[str, str] | None = None) -> Message:
    """Address the email.

    * ``auto``: To the carrier's dispatcher, Cc the load's sales rep(s), Reply-To the
      shared group address so replies land where the whole team reads them.
    * ``pilot``: To the sales rep, who forwards to the carrier after a look.
    The body names and is signed by the load's sales rep (``rep_signature`` adds title and
    email from reps.csv).
    """

    event = draft.data.get("event") or {}
    when = event.get("when_text") if event.get("trigger") == "delivered" else event.get("delivered_at")
    email = render_email(load, picks, miles=miles, show_pay=show_pay, rep_email=", ".join(rep_emails) or None,
                         delivered_at=when, trip_loads=event.get("loads"), rep_signature=rep_signature)
    carrier_email = (load.carrier.email or "").strip()
    first_rep = rep_emails[0] if rep_emails else None
    if mode == "pilot":
        to = [first_rep] if first_rep else []
        body = PILOT_NOTE.format(carrier_email=carrier_email or "(no carrier email on file)") + "\n\n" + "-" * 60 + "\n" + email["body"]
        return Message(to=to, cc=[], reply_to=[reply_to] if reply_to else [], subject="[Pilot] " + email["subject"], body=body, mode=mode,
                       carrier_email=carrier_email, rep_email=first_rep, load_id=load.load_id, carrier=load.carrier.name or "",
                       picks=[p.load_id for p in picks])
    return Message(to=[carrier_email], cc=list(rep_emails), reply_to=[reply_to] if reply_to else ([first_rep] if first_rep else []),
                   subject=email["subject"], body=email["body"], mode=mode, carrier_email=carrier_email, rep_email=first_rep,
                   load_id=load.load_id, carrier=load.carrier.name or "", picks=[p.load_id for p in picks])


def rep_emails_for(load: DispatchedLoad, reps: dict[str, str], fallback_to: str | None = None) -> tuple[list[str], list[str]]:
    """``(emails, names_without_email)`` for the sales rep(s) to Cc. Empty when the load
    has no sales rep: that draft is held, never redirected to someone else."""

    if not load.cc_reps:
        return [], []

    emails: list[str] = []
    missing: list[str] = []
    for name in load.cc_reps:
        email = rep_email_for(name, reps)
        if email and email not in emails:
            emails.append(email)
        elif not email:
            missing.append(nice_name(name))
    if not emails and fallback_to:
        emails.append(fallback_to)
    return emails, missing


class FileTransport:
    """Dry run: every message becomes ``outbox/<load>_<carrier>.txt``."""

    def __init__(self, outbox: str) -> None:
        self.outbox = outbox

    def send(self, message: Message, from_addr: str) -> str:
        os.makedirs(self.outbox, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9]+", "-", f"{message.load_id}_{message.carrier}").strip("-").lower() + ".txt"
        path = os.path.join(self.outbox, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"From: {from_addr}\n" + message.text())
        return f"file:{path}"


class SesTransport:
    """Amazon SES v2 ``SendEmail``. The From identity must be verified in SES."""

    def __init__(self, region: str | None = None, configuration_set: str | None = None, client: Any = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("sesv2", region_name=region) if region else boto3.client("sesv2")
        self.client = client
        self.configuration_set = configuration_set

    def send(self, message: Message, from_addr: str) -> str:
        request: dict[str, Any] = {
            "FromEmailAddress": from_addr,
            "Destination": {"ToAddresses": message.to, "CcAddresses": message.cc},
            "Content": {"Simple": {"Subject": {"Data": message.subject, "Charset": "UTF-8"}, "Body": {"Text": {"Data": message.body, "Charset": "UTF-8"}}}},
        }
        if message.reply_to:
            request["ReplyToAddresses"] = message.reply_to
        if self.configuration_set:
            request["ConfigurationSetName"] = self.configuration_set
        response = self.client.send_email(**request)
        return str(response.get("MessageId", ""))


# --------------------------------------------------------------------------- #
# The run                                                                      #
# --------------------------------------------------------------------------- #


class SentLog:
    """``sent_log.jsonl``: one line per send. Answers two questions: was this draft sent
    already, and when did this carrier last get anything from us."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.keys: set[str] = set()
        self.entries: list[dict[str, Any]] = []
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                        self.keys.add(entry["key"])
                        self.entries.append(entry)
                    except (ValueError, KeyError):
                        continue

    def __contains__(self, key: str) -> bool:
        return key in self.keys

    def last_sent_to(self, carrier: str) -> datetime | None:
        key = " ".join((carrier or "").split()).lower()
        latest: datetime | None = None
        for entry in self.entries:
            if " ".join((entry.get("carrier_key") or "").split()).lower() != key:
                continue
            try:
                when = datetime.fromisoformat(entry["at"])
            except (KeyError, ValueError):
                continue
            if latest is None or when > latest:
                latest = when
        return latest

    def add(self, entry: dict[str, Any]) -> None:
        self.keys.add(entry["key"])
        self.entries.append(entry)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")


def send_all(
    drafts: list[Draft],
    *,
    reps: dict[str, str],
    transport: Any,
    from_addr: str,
    mode: str = "auto",
    sent_log: SentLog,
    offers: Offers,
    blocked: set[str] | None = None,
    fallback_to: str | None = None,
    rep_details: dict[str, dict[str, str]] | None = None,
    offer_cap: int = DEFAULT_OFFER_CAP,
    offer_window_hours: float = DEFAULT_OFFER_WINDOW_HOURS,
    recheck: Callable[[str], Grid] | None = None,
    miles: int = DEFAULT_MILES,
    show_pay: bool = False,
    max_sends: int = 50,
    reply_to: str | None = None,
    carrier_cooldown_hours: float = DEFAULT_CARRIER_COOLDOWN_HOURS,
    repeat_window_hours: float = DEFAULT_REPEAT_WINDOW_HOURS,
    draft_max_age_hours: float = DEFAULT_DRAFT_MAX_AGE_HOURS,
    writer: Callable[..., tuple[str, str, dict[str, Any]] | None] | None = None,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Send what should go out, once. Guards, in order: already sent; draft too old; carrier
    on the do-not-email list; no carrier email; no sales rep; no sales rep email; carrier
    already emailed within ``carrier_cooldown_hours``; then the picks are trimmed (loads
    this carrier already saw within ``repeat_window_hours``, loads offered to ``offer_cap``
    other carriers, loads no longer open) and nothing left means no email."""

    now = now or datetime.now()
    counts: dict[str, int] = {"sent": 0, "already_sent": 0, "stale": 0, "blocked": 0, "no_carrier_email": 0, "no_sales_rep": 0, "no_rep_email": 0,
                              "carrier_recent": 0, "nothing_left": 0, "deferred": 0, "error": 0}
    blocked = blocked or set()
    rep_details = rep_details or {}
    for draft in drafts:
        if draft.key in sent_log:
            counts["already_sent"] += 1
            continue
        drafted_at = draft.drafted_at()
        if drafted_at is not None and now - drafted_at > timedelta(hours=draft_max_age_hours):
            counts["stale"] += 1
            log(f"skipped: load {draft.load_id}, draft is {int((now - drafted_at).total_seconds() // 3600)} h old; the truck has moved on")
            continue
        load, picks = rebuild(draft)
        if is_blocked(load.carrier.email, load.carrier.name, blocked):
            counts["blocked"] += 1
            log(f"blocked: load {load.load_id}, {load.carrier.name} is on the do-not-email list")
            continue
        if not load.carrier.email:
            counts["no_carrier_email"] += 1
            log(f"held: load {load.load_id}, {load.carrier.name or 'carrier'} has no email on the load page")
            continue
        ckey = carrier_key(load.carrier.email, load.carrier.name)
        if not load.sales_rep:
            counts["no_sales_rep"] += 1
            log(f"held: load {load.load_id}, no sales rep assigned on the load; set it in CargoTel")
            continue
        rep_emails, missing = rep_emails_for(load, reps, fallback_to)
        if not rep_emails:
            counts["no_rep_email"] += 1
            log(f"held: load {load.load_id}, no email in reps.csv for {', '.join(missing) or 'the sales rep'}")
            continue
        if missing:
            log(f"note: load {load.load_id}, no email in reps.csv for {', '.join(missing)}; cc goes to {', '.join(rep_emails)}")
        last = sent_log.last_sent_to(ckey)
        if last is not None and now - last < timedelta(hours=carrier_cooldown_hours):
            counts["carrier_recent"] += 1
            log(f"held: load {load.load_id}, {load.carrier.name} was emailed {int((now - last).total_seconds() // 60)} min ago; one email per carrier per {carrier_cooldown_hours:g} h")
            continue
        kept, repeated = drop_repeats(picks, offers, ckey, now, repeat_window_hours)
        kept, capped = apply_offer_cap(kept, offers, ckey, now, offer_cap, offer_window_hours)
        gone: list[Suggestion] = []
        if recheck is not None and kept and load.destination.zip:
            try:
                kept, gone = recheck_picks(kept, recheck(load.destination.zip))
            except Exception as exc:  # noqa: BLE001
                counts["error"] += 1
                log(f"error: load {load.load_id}, re-check failed: {exc}")
                continue
        if not kept:
            counts["nothing_left"] += 1
            log(f"skipped: load {load.load_id}, nothing left to offer ({len(repeated)} already offered to this carrier, {len(capped)} capped, {len(gone)} gone)")
            continue
        if counts["sent"] >= max_sends:
            counts["deferred"] += 1
            continue
        signature = rep_signature_for(load.sales_rep, rep_details)
        message = compose(load, kept, draft, mode=mode, rep_emails=rep_emails, miles=miles, show_pay=show_pay, reply_to=reply_to,
                          rep_signature=signature)
        source, usage = "template", {}
        if writer is not None:
            # Let the model word the email from the same facts; keep the template on any failure.
            event = draft.data.get("event") or {}
            delivered = event.get("when_text") if event.get("trigger") == "delivered" else event.get("delivered_at")
            try:
                written = writer(load, kept, signature=signature, delivered_at=delivered)
            except Exception as exc:  # noqa: BLE001
                written = None
                log(f"note: load {load.load_id}, model draft failed, template used: {exc}")
            if written:
                subject, body, usage = written
                prefix = "[Pilot] " if mode == "pilot" else ""
                message.subject = prefix + subject
                message.body = body if mode != "pilot" else message.body.split("-" * 60 + "\n", 1)[0] + "-" * 60 + "\n" + body
                source = "model"
        try:
            message_id = transport.send(message, from_addr)
        except Exception as exc:  # noqa: BLE001
            counts["error"] += 1
            log(f"error: load {load.load_id}, send failed: {exc}")
            continue
        offers.add([p.load_id for p in kept], ckey, now)
        sent_log.add({
            "key": draft.key, "at": now.isoformat(timespec="seconds"), "mode": mode, "load_id": load.load_id,
            "carrier": load.carrier.name, "carrier_key": ckey, "to": message.to, "cc": message.cc, "subject": message.subject,
            "picks": [p.load_id for p in kept], "repeated": [p.load_id for p in repeated], "capped": [p.load_id for p in capped],
            "gone": [p.load_id for p in gone], "message_id": message_id, "draft": draft.path, "source": source, "bedrock_usage": usage,
        })
        counts["sent"] += 1
        cache_note = ""
        if usage:
            cache_note = f"; tokens {usage.get('inputTokens', 0)} in, {usage.get('cacheReadInputTokens', 0)} cached, {usage.get('outputTokens', 0)} out"
        log(f"sent ({mode}, {source}): load {load.load_id} {load.carrier.name} -> {', '.join(message.to)}"
            f"{' cc ' + ', '.join(message.cc) if message.cc else ''}: {len(kept)} loads"
            f"{f', {len(repeated)} already seen by this carrier' if repeated else ''}"
            f"{f', {len(capped)} capped' if capped else ''}{f', {len(gone)} gone' if gone else ''}{cache_note} [{message_id}]")
    offers.save()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the watcher's reload drafts to carriers, Cc the sales rep (dry run to outbox/ unless --send).")
    parser.add_argument("--drafts", default=DEFAULT_DRAFTS, help=f"folder with the watcher's drafts (default {DEFAULT_DRAFTS}/)")
    parser.add_argument("--reps", default="reps.csv", help="CSV of rep name,email (default reps.csv)")
    parser.add_argument("--mode", choices=("auto", "pilot"), default="auto", help="auto (default): send to the carrier, Cc the sales rep; pilot: send to the sales rep to forward")
    parser.add_argument("--from", dest="from_addr", default=os.environ.get("MAIL_FROM", "Circle Logistics <vehicles@circledelivers.com>"), help="verified SES sender (default: MAIL_FROM or the vehicles@ group)")
    parser.add_argument("--reply-to", help="where replies go (default: the From address, i.e. the group)")
    parser.add_argument("--send", action="store_true", help="really send through SES; without it, write to --outbox")
    parser.add_argument("--outbox", default=DEFAULT_OUTBOX)
    parser.add_argument("--region", help="AWS region for SES (default: boto3's)")
    parser.add_argument("--configuration-set", help="SES configuration set for bounce/complaint events")
    parser.add_argument("--fallback-to", help="inbox to use when a rep has no email in reps.csv (pilot only)")
    parser.add_argument("--do-not-email", default=DEFAULT_DO_NOT_EMAIL)
    parser.add_argument("--sent-log", default=DEFAULT_SENT_LOG)
    parser.add_argument("--offers", default=DEFAULT_OFFERS)
    parser.add_argument("--offer-cap", type=int, default=DEFAULT_OFFER_CAP, help="a load goes to at most this many carriers per --offer-window-hours")
    parser.add_argument("--offer-window-hours", type=float, default=DEFAULT_OFFER_WINDOW_HOURS)
    parser.add_argument("--carrier-cooldown-hours", type=float, default=DEFAULT_CARRIER_COOLDOWN_HOURS, help="one email per carrier per this many hours (default 24)")
    parser.add_argument("--repeat-window-hours", type=float, default=DEFAULT_REPEAT_WINDOW_HOURS, help="never offer the same load to the same carrier twice within this (default 72)")
    parser.add_argument("--draft-max-age-hours", type=float, default=DEFAULT_DRAFT_MAX_AGE_HOURS, help="drafts older than this are not sent (default 12)")
    parser.add_argument("--recheck", action="store_true", help="drop loads no longer open on the Load Build search before sending")
    parser.add_argument("--miles", type=int, default=DEFAULT_MILES, choices=(10, 25, 50, 75, 150, 300))
    parser.add_argument("--show-pay", action="store_true")
    parser.add_argument("--max-sends", type=int, default=50)
    parser.add_argument("--llm", action="store_true", help="have Claude on Bedrock word each email (system prompt cached for 1 h); template on any failure")
    parser.add_argument("--model", default=os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5-5"))
    args = parser.parse_args(argv)
    reply_to = args.reply_to or re.sub(r"^.*<([^>]+)>.*$", r"\1", args.from_addr)

    drafts = read_drafts(args.drafts)
    rep_details = load_rep_details(args.reps)
    reps = {key: rep["email"] for key, rep in rep_details.items()}
    if not reps and not args.fallback_to:
        print(f"no reps loaded from {args.reps}; pilot mode needs rep emails (or --fallback-to)", file=sys.stderr)
    transport = SesTransport(args.region, args.configuration_set) if args.send else FileTransport(args.outbox)
    recheck = None
    if args.recheck:
        session = CargoTelSession()

        def recheck(zip_code: str) -> Grid:
            return fetch_recheck_grid(session, zip_code, args.miles)

    def log(message: str) -> None:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)

    writer = None
    if args.llm:
        from bedrock_email import write_email

        def writer(load, picks, *, signature, delivered_at):
            return write_email(load, picks, signature=signature, delivered_at=delivered_at, model_id=args.model,
                               profile=os.environ.get("AWS_PROFILE") or None, region=os.environ.get("AWS_REGION") or None)

    log(f"{len(drafts)} drafts in {args.drafts}; {len(reps)} reps; mode {args.mode}; wording {'Claude on Bedrock' if args.llm else 'template'}; "
        f"{'SENDING via SES' if args.send else 'dry run to ' + args.outbox + '/'}")
    counts = send_all(
        drafts, reps=reps, transport=transport, from_addr=args.from_addr, mode=args.mode,
        sent_log=SentLog(args.sent_log), offers=Offers(args.offers), blocked=load_do_not_email(args.do_not_email),
        fallback_to=args.fallback_to, rep_details=rep_details, offer_cap=args.offer_cap, offer_window_hours=args.offer_window_hours,
        recheck=recheck, miles=args.miles, show_pay=args.show_pay, max_sends=args.max_sends, reply_to=reply_to,
        carrier_cooldown_hours=args.carrier_cooldown_hours, repeat_window_hours=args.repeat_window_hours,
        draft_max_age_hours=args.draft_max_age_hours, writer=writer, log=log,
    )
    log("done: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
