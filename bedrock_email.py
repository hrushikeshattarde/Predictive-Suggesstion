"""
Write the reload-suggestion email for one load with Claude on Amazon Bedrock, check it
against the facts, and optionally send it through Gmail as the vehicles@ group.

The facts (load, delivery, rep, suggested loads) come from ``suggest_reloads``, exactly
as the watcher produces them. The model only words the email; it is told to use nothing
but those facts, and the result is checked before anything is sent:

* every suggested load number must appear in the body;
* no other six-digit number may appear (no invented loads);
* no dollar amounts (rates are "on request" until the manager decides otherwise).

If the check fails the plain template email from ``suggest_reloads`` is used instead,
and the reason is printed.

Usage::

    python bedrock_email.py 344141                       # generate and print, send nothing
    python bedrock_email.py 344141 --send                # send to TEST_TO, cc TEST_CC from .env
    python bedrock_email.py 344141 --send --to a@x.com,b@x.com --cc me@x.com
    python bedrock_email.py 344141 --delivered "10/12/2026 15:10"   # "you dropped" wording

Settings come from ``.env`` (AWS_PROFILE, AWS_REGION, BEDROCK_MODEL_ID, GOOGLE_SA_FILE,
GMAIL_USER, MAIL_FROM, TEST_TO, TEST_CC). Run it with the project's ``.venv``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any

from cargotel_search import CargoTelSession
from suggest_reloads import DEFAULT_MAX, DEFAULT_MILES, DEFAULT_MIN, DEFAULT_SAVED_ID, draft_for_load, email_text, nice_name, render_email

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:  # .env is optional; environment variables work too
    pass

DEFAULT_MODEL = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5-5")
#: Cache TTL for the system prompt: "1h" keeps it warm across the watcher's 15-minute
#: passes (each hit refreshes it); "" means the 5-minute default.
DEFAULT_CACHE_TTL = os.environ.get("BEDROCK_CACHE_TTL", "1h")

# The system prompt is the static, cached part of every request. It must stay byte-identical
# between calls (no dates, no per-load text) and stay above Bedrock's 512-token minimum for
# a cache checkpoint on Sonnet 5.5, which is why it carries the full style guide and a
# worked example rather than a few lines.
SYSTEM_PROMPT = """You write short, plain emails for Circle Logistics, a vehicle transport broker, to the dispatcher at a trucking company whose truck is about to be empty (or has just emptied) after delivering a Circle load.

The email suggests the next loads the truck can take from Circle's open freight near the delivery point. You are given the facts as JSON.

Hard rules
- Use only the facts given. Never invent a load, a place, a date, a count, a distance or a rate. If a fact is missing, leave it out; do not guess.
- Include every suggested load number exactly as given, each on its own numbered line, in the order given. Never add, drop or renumber a load.
- Do not mention prices or rates anywhere. One sentence near the end says rates are available on request.
- Never include a phone number. The only Circle person named anywhere is the sales rep given in circle_rep.
- No marketing tone, no emojis, no exclamation marks, no bullet symbols other than the numbered list.

Shape of the email
- Greeting: the dispatcher's contact name if given ("Hi Eugen,"), otherwise "Hi there,".
- One sentence that says which load the truck is delivering (or has dropped), where and when, using the facts in delivery.
- One sentence that introduces the list: how many loads, where they pick up, and how far that yard is from the delivery point. When the loads pick up at more than one yard, group the lines by yard and give each yard's distance once.
- Built loads (several vehicles already grouped into one truckload) come before single orders.
- Each load line: the load number, then the vehicle count and models, then the destination as city and state only (no zip codes), then the road miles. Keep each line under 110 characters. Example: "1. Load 344231: 5 vehicles (3 x 1500, 3500, 5500) to Milwaukee, Monroe and Madison, WI, 150 road miles."
- Closing: one sentence asking them to reply to this email or call the sales rep, with "Rates are available on request." Then the signature: the sales rep's name on one line, their title and "Circle Logistics" on the next, their email on a third line when given.
- About 10 to 16 lines in total. Short sentences, plain words.

Worked example
Facts: load 343514, 3 x 2026 Mercedes G-Class, delivers to Grapevine, TX on Wed 7 PM; contact Eugen Padureanu; sales rep Kyle Link, Sales Rep, kyle.link@example.com; suggested: order 331794 (1 vehicle, Topolino) from Dallas, TX 17 miles away to Florence, KY, 910 road miles; order 331796 (same).
Email:
Subject: Next loads near Grapevine, TX after load 343514

Hi Eugen,

Your truck delivers load 343514 (3 x 2026 Mercedes G-Class) to Grapevine, TX on Wednesday at 7 PM. Two Circle orders are open at our Dallas, TX yard, 17 miles from the drop:

1. Order 331794: 1 vehicle (Topolino) to Florence, KY, 910 road miles.
2. Order 331796: 1 vehicle (Topolino) to Florence, KY, 910 road miles.

Rates are available on request. Reply to this email or call Kyle Link if either fits your truck.

Kyle Link
Sales Rep, Circle Logistics
kyle.link@example.com

Output format: the first line is "Subject: <subject>", then a blank line, then the email body. Nothing else, no commentary."""


def facts_for(result: dict[str, Any], delivered_at: str | None, rep_signature: dict[str, str] | None = None) -> dict[str, Any]:
    load = result["load"]
    picks = result["picks_objects"]
    sig = rep_signature or {}
    return {
        "load_id": load.load_id,
        "status": "delivered" if delivered_at else "dispatched, delivering soon",
        "vehicles_on_this_load": load.units,
        "vehicle_summary": load.vehicle_summary(),
        "delivery": {"name": load.destination.name, "city_state": load.destination.city_state, "zip": load.destination.zip,
                     "scheduled": load.destination.when, "delivered_at": delivered_at},
        "pickup_of_this_load": {"city_state": load.origin.city_state, "when": load.origin.when},
        "circle_rep": {"name": load.contact_rep_display, "role": "sales rep", "title": sig.get("title") or "Sales Rep",
                       "email": sig.get("email")},
        "carrier": {"name": load.carrier.name, "contact_name": load.carrier.contact_name, "base": ", ".join(p for p in (load.carrier.city, load.carrier.state) if p) or None},
        "search_radius_miles": (result.get("search") or {}).get("params", {}).get("Menu__radius__1__oradius", DEFAULT_MILES),
        "suggested_loads": [
            {
                "number": p.load_id,
                "kind": "built load" if p.kind == "load" else "single order",
                "vehicles": p.units,
                "models": p.model_summary(),
                "pickup_yard": p.origin,
                "miles_from_delivery_point": p.deadhead_miles,
                "destinations": p.dest_summary(),
                "road_miles": p.road_miles,
            }
            for p in picks
        ],
    }


def _compact(value: Any) -> Any:
    """Drop None/empty values and indentation from the facts: fewer uncached input tokens."""

    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _system_blocks(cache_ttl: str | None) -> list[dict[str, Any]]:
    """The static system prompt followed by a Bedrock cache checkpoint. Everything before
    the checkpoint is cached (prefix match, byte-identical between calls); the per-load
    facts go in the user message after it and are never cached."""

    point: dict[str, Any] = {"type": "default"}
    if cache_ttl:
        point["ttl"] = cache_ttl
    return [{"text": SYSTEM_PROMPT}, {"cachePoint": point}]


def generate(facts: dict[str, Any], *, model_id: str, profile: str | None, region: str | None, cache_ttl: str | None = DEFAULT_CACHE_TTL) -> tuple[str, str, dict[str, Any]]:
    """Returns (subject, body, usage). ``usage`` carries Bedrock's ``inputTokens``
    (uncached only), ``cacheReadInputTokens``, ``cacheWriteInputTokens`` and
    ``outputTokens``."""

    import boto3
    from botocore.exceptions import ClientError

    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    client = session.client("bedrock-runtime")
    prompt = "Write the email from these facts:\n" + json.dumps(_compact(facts), separators=(",", ":"))
    request = {
        "modelId": model_id,
        "system": _system_blocks(cache_ttl),
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": 4000},  # Sonnet 5.5 rejects `temperature` as deprecated
    }
    try:
        response = client.converse(**request)
    except ClientError as exc:
        # A model that only supports the 5-minute TTL rejects "ttl"; retry with the default.
        if cache_ttl and "ttl" in str(exc).lower():
            request["system"] = _system_blocks(None)
            response = client.converse(**request)
        else:
            raise
    if response.get("stopReason") != "end_turn":
        raise RuntimeError(f"model stopped early (stopReason={response.get('stopReason')}); the email would be cut off")
    text = "".join(block.get("text", "") for block in response["output"]["message"]["content"]).strip()
    subject, _, body = text.partition("\n")
    subject = re.sub(r"^\s*subject\s*:\s*", "", subject, flags=re.I).strip()
    return subject, body.strip(), response.get("usage", {})


def usage_line(usage: dict[str, Any]) -> str:
    """One readable line: uncached in / cache read / cache write / out."""

    return (f"{usage.get('inputTokens', 0)} uncached in, {usage.get('cacheReadInputTokens', 0)} cache read, "
            f"{usage.get('cacheWriteInputTokens', 0)} cache write, {usage.get('outputTokens', 0)} out")


def write_email(load, picks, *, signature: dict[str, str] | None, delivered_at: str | None, model_id: str, profile: str | None,
                region: str | None, cache_ttl: str | None = DEFAULT_CACHE_TTL) -> tuple[str, str, dict[str, Any]] | None:
    """Have the model write the email and check it against the facts. Returns
    ``(subject, body, usage)`` or ``None`` when the draft fails the check, so the caller
    can fall back to the template. Used by the sender's ``--llm`` option."""

    facts = facts_for({"load": load, "picks_objects": picks, "search": {"params": {}}}, delivered_at, signature)
    subject, body, usage = generate(facts, model_id=model_id, profile=profile, region=region, cache_ttl=cache_ttl)
    if check(body, facts):
        return None
    return subject, body, usage


def check(body: str, facts: dict[str, Any]) -> list[str]:
    """Deterministic gate: every suggested load present, no unknown six-digit numbers, no rates."""

    problems = []
    allowed = {facts["load_id"]} | {s["number"] for s in facts["suggested_loads"]}
    for number in sorted({s["number"] for s in facts["suggested_loads"]}):
        if number not in body:
            problems.append(f"suggested load {number} is missing from the email")
    for number in sorted(set(re.findall(r"(?<!\d)\d{6}(?!\d)", body))):
        if number not in allowed:
            problems.append(f"unknown six-digit number {number} in the email")
    if re.search(r"\$\s?\d", body):
        problems.append("a dollar amount appears in the email")
    if "circle logistics" not in body.lower():
        problems.append("the signature is missing (the email may be cut off)")
    if re.search(r"\(\d{3}\)\s?\d{3}-\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b", body):
        problems.append("a phone number appears in the email")
    rep = (facts.get("circle_rep") or {}).get("name")
    if rep and rep not in body:
        problems.append(f"the sales rep {rep} is not named in the email")
    return problems


def send_via_ses(*, from_addr: str, to: list[str], cc: list[str], subject: str, body: str, reply_to: str, profile: str | None, region: str | None) -> str:
    """Amazon SES v2 SendEmail. The From identity (vehicles@circledelivers.com, or the whole
    circledelivers.com domain) is verified in us-east-1, so the From line arrives exactly
    as given, DKIM-signed. Returns the SES message id."""

    import boto3

    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    client = session.client("sesv2")
    response = client.send_email(
        FromEmailAddress=from_addr,
        Destination={"ToAddresses": to, "CcAddresses": cc},
        ReplyToAddresses=[reply_to],
        Content={"Simple": {"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": {"Text": {"Data": body, "Charset": "UTF-8"}}}},
    )
    return str(response.get("MessageId", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the reload email with Claude on Bedrock and optionally send it (SES by default, or Gmail).")
    parser.add_argument("load_id")
    parser.add_argument("--delivered", metavar="WHEN", help='the truck already dropped it, e.g. "10/12/2026 15:10"')
    parser.add_argument("--miles", type=int, default=DEFAULT_MILES, choices=(10, 25, 50, 75, 150, 300))
    parser.add_argument("--min", dest="min_n", type=int, default=DEFAULT_MIN)
    parser.add_argument("--max", dest="max_n", type=int, default=DEFAULT_MAX)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--cache-ttl", default=DEFAULT_CACHE_TTL, help='prompt cache TTL for the system prompt: "1h" (default) or "" for the 5-minute default')
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or None)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or None)
    parser.add_argument("--send", action="store_true", help="send as MAIL_FROM")
    parser.add_argument("--via", choices=("ses", "gmail"), default=os.environ.get("MAIL_VIA", "ses"), help="transport: ses (From arrives as given) or gmail (impersonated mailbox)")
    parser.add_argument("--production", action="store_true", help="address it for real: To the carrier's dispatcher, Cc the load's sales rep(s) from --reps, no [TEST] prefix")
    parser.add_argument("--reps", default="reps.csv", help="rep name,email CSV used for the Cc in --production")
    parser.add_argument("--to", default="", help="override recipients (comma-separated); default: TEST_TO, or the carrier with --production")
    parser.add_argument("--cc", default="", help="override cc; default: TEST_CC, or the sales rep(s) with --production")
    parser.add_argument("--from", dest="from_addr", default=os.environ.get("MAIL_FROM", ""))
    parser.add_argument("--subject-prefix", default=None, help='default "[TEST] ", or nothing with --production')
    parser.add_argument("--template", action="store_true", help="skip Bedrock, use the plain template email")
    parser.add_argument("--out", metavar="FILE", help="write the final email text here")
    args = parser.parse_args(argv)

    session = CargoTelSession()
    result = draft_for_load(session, args.load_id, miles=args.miles, min_n=args.min_n, max_n=args.max_n, delivered_at=args.delivered)
    picks = result["picks_objects"]
    load = result["load"]
    print(f"load {load.load_id}: {load.origin.city_state} -> {load.destination.city_state} {load.destination.zip or ''} {load.destination.when or ''}; "
          f"dispatch rep {load.rep_display or '?'}; sales rep {', '.join(nice_name(n) for n in load.cc_reps) or '(none)'}; "
          f"carrier {load.carrier.name or '(none)'}; {len(picks)} picks from {result['search']['rows']} rows", file=sys.stderr)
    if not picks:
        print("nothing to suggest; no email", file=sys.stderr)
        return 1
    if not load.sales_rep:
        print(f"load {load.load_id} has no sales rep assigned; the email is signed by the sales rep, so set one in CargoTel first", file=sys.stderr)
        if args.production:
            return 2

    from send_drafts import load_rep_details, rep_signature_for

    rep_details = load_rep_details(args.reps)
    signature = rep_signature_for(load.sales_rep, rep_details)
    template = render_email(load, picks, miles=args.miles, show_pay=False, delivered_at=args.delivered, rep_signature=signature)
    subject, body, source = template["subject"], template["body"], "template"
    usage: dict[str, Any] = {}
    if not args.template:
        facts = facts_for(result, args.delivered, signature)
        try:
            gen_subject, gen_body, usage = generate(facts, model_id=args.model, profile=args.profile, region=args.region, cache_ttl=args.cache_ttl or None)
            problems = check(gen_body, facts)
            if problems:
                print("Bedrock draft rejected, using the template: " + "; ".join(problems), file=sys.stderr)
            else:
                subject, body, source = gen_subject or subject, gen_body, f"bedrock {args.model}"
            print(f"Bedrock usage: {usage_line(usage)}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"Bedrock failed, using the template: {exc}", file=sys.stderr)

    if args.production:
        from send_drafts import rep_emails_for

        rep_emails, missing = rep_emails_for(load, {key: rep["email"] for key, rep in rep_details.items()})
        to = [a.strip() for a in args.to.split(",") if a.strip()] or ([load.carrier.email] if load.carrier.email else [])
        cc = [a.strip() for a in args.cc.split(",") if a.strip()] or rep_emails
        prefix = args.subject_prefix or ""
        if missing:
            print(f"note: no email in {args.reps} for {', '.join(missing)}", file=sys.stderr)
        if not to:
            print(f"cannot address: load {load.load_id} has no carrier email on the page", file=sys.stderr)
    else:
        to = [a.strip() for a in (args.to or os.environ.get("TEST_TO", "")).split(",") if a.strip()]
        cc = [a.strip() for a in (args.cc or os.environ.get("TEST_CC", "")).split(",") if a.strip()]
        prefix = "[TEST] " if args.subject_prefix is None else args.subject_prefix
    full_subject = f"{prefix}{subject}"
    email = {"to": ", ".join(to) or "(none)", "cc": ", ".join(cc), "subject": full_subject, "body": body}
    print(f"\n--- email ({source}) ---\nFrom: {args.from_addr}\n" + email_text(email))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(f"From: {args.from_addr}\n" + email_text(email))

    if not args.send:
        print("not sent (no --send)", file=sys.stderr)
        return 0
    if not to or not args.from_addr:
        print("cannot send: need --to and --from (or TEST_TO and MAIL_FROM in .env)", file=sys.stderr)
        return 2
    reply_to = re.sub(r"^.*<([^>]+)>.*$", r"\1", args.from_addr)
    if args.via == "ses":
        message_id = send_via_ses(from_addr=args.from_addr, to=to, cc=cc, subject=full_subject, body=body, reply_to=reply_to,
                                  profile=args.profile, region=args.region)
        from_as_sent = args.from_addr
        print(f"sent via SES: message id {message_id}; From {args.from_addr}; Reply-To {reply_to}", file=sys.stderr)
    else:
        from gmail_send import send_mail

        sa_file = os.environ.get("GOOGLE_SA_FILE", "")
        user = os.environ.get("GMAIL_USER", "")
        if not (sa_file and user):
            print("cannot send: set GOOGLE_SA_FILE and GMAIL_USER in .env", file=sys.stderr)
            return 2
        sent = send_mail(sa_file, user, from_addr=args.from_addr, to=to, cc=cc, subject=full_subject, body=body, reply_to=reply_to)
        message_id, from_as_sent = sent["id"], sent["from_as_sent"]
        print(f"sent via Gmail as {user}: id {message_id}; From as delivered: {from_as_sent}; To: {sent['to_as_sent']}", file=sys.stderr)
    log = {"at": datetime.now().isoformat(timespec="seconds"), "load_id": load.load_id, "to": to, "cc": cc, "subject": full_subject,
           "source": source, "via": args.via, "message_id": message_id, "from_as_sent": from_as_sent, "reply_to": reply_to, "picks": [p.load_id for p in picks],
           "bedrock_usage": usage}
    with open("sent_log.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(log) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
