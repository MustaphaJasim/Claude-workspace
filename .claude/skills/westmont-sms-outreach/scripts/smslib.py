"""Westmont SMS Outreach - shared ledger, validation and send-rule logic.

The ledger (SQLite) is the single source of truth for drafts, approvals and
send attempts inside a session. It lives in <project>/sms_data/ (git-ignored)
and is mirrored to the private Google Sheet "Westmont - SMS Outreach Log".

Approvals can only be written by the UserPromptSubmit hook (sms_guard.py),
which reads the text Mustapha actually typed. Nothing in this module grants
approval on its own.
"""

import hashlib
import json
import math
import os
import random
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import compliance as C

# --------------------------------------------------------------------------
# Configuration (change only by committing a code change)
# --------------------------------------------------------------------------
FROM_NUMBER = "+19453637786"          # Westmont US Ringover number (Texas, 945)
APPROVAL_TTL_HOURS = 12                # approvals expire after this
PRESENTATION_TTL_HOURS = 24            # approval codes expire after this
PRECHECK_MAX_AGE_MIN = 120             # Ringover opt-out/history check freshness
MIN_GAP_HOURS_SAME_NUMBER = 24         # no 2 outbound texts to one number in 24h (replies exempt)
SAME_CONTENT_LOOKBACK_DAYS = 90        # identical text to same number blocked
SEND_DAYS = {0, 1, 2, 3, 4}            # Mon-Fri, recipient local time
SEND_START_HOUR = 9                    # 09:00 local
SEND_END_HOUR = 19                     # until 19:00 local
MAX_SEGMENTS = 3

AUDIENCES = {"candidate", "client"}
PURPOSES = {
    "candidate": {"opportunity", "follow_up", "reply", "availability_check"},
    "client": {"prospecting", "follow_up", "reply", "existing_conversation", "client_service"},
}
CONSENT = {
    # basis -> allowed?  (shown to Mustapha in every review table)
    "candidate": {
        "provided_number": True,      # candidate gave Westmont this number
        "prior_conversation": True,   # we have spoken/texted before
        "applied": True,              # applied to a Westmont role
        "referral": True,             # referred, number passed with permission
        "sourced": True,              # found via data tool; first text must identify + STOP
        "unknown": False,
    },
    # Client texts are judged by the legal rules engine (compliance.py), not by a
    # fixed allow-list: every relationship type, including "none" (cold), can be
    # drafted; the engine decides PERMITTED / EXEMPT / REVIEW_REQUIRED / PROHIBITED.
    "client": {k: True for k in C.RELATIONSHIPS},
}

# Statuses
DRAFT, PRESENTED, APPROVED, REJECTED = "DRAFT", "PRESENTED", "APPROVED", "REJECTED"
SUBMITTING, SUBMITTED, UNCERTAIN = "SUBMITTING", "SUBMITTED", "UNCERTAIN"
RINGOVER_FAILED, NOT_FOUND, CANCELLED = "RINGOVER_FAILED", "NOT_FOUND", "CANCELLED"
BLOCKED_OPTOUT = "BLOCKED_OPTOUT"
# A message in one of these states has (or may have) reached Ringover.
SENT_LIKE = {SUBMITTING, SUBMITTED, UNCERTAIN}


def project_dir():
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[4]


def ledger_path():
    override = os.environ.get("WESTMONT_SMS_LEDGER")
    if override:
        return Path(override)
    return project_dir() / "sms_data" / "ledger.db"


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY, created_at TEXT, audience TEXT, purpose TEXT,
  description TEXT);
CREATE TABLE IF NOT EXISTS messages (
  msg_id TEXT PRIMARY KEY, batch_id TEXT, line INTEGER, version INTEGER,
  recipient_name TEXT, company TEXT, title TEXT, phone TEXT,
  audience TEXT, purpose TEXT, consent_basis TEXT, context_note TEXT,
  hubspot_contact_id TEXT, tz_override TEXT,
  content TEXT, segments INTEGER, encoding TEXT,
  status TEXT, presented_code TEXT, presented_hash TEXT,
  approved_hash TEXT, approved_at TEXT, approval_expires TEXT,
  attempts INTEGER DEFAULT 0, submitted_at TEXT,
  ringover_message_id TEXT, ringover_conversation_id TEXT, result_detail TEXT,
  hubspot_note_id TEXT, created_at TEXT, updated_at TEXT, compliance_json TEXT);
CREATE TABLE IF NOT EXISTS clearances (
  key TEXT PRIMARY KEY, evidence TEXT, granted_at TEXT, expires_at TEXT,
  revoked_at TEXT, bound_hash TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS presentations (
  code TEXT PRIMARY KEY, batch_id TEXT, created_at TEXT, expires_at TEXT,
  active INTEGER, items TEXT);
CREATE TABLE IF NOT EXISTS prechecks (
  msg_id TEXT, checked_at TEXT, phone TEXT, opted_out INTEGER,
  identical_already_sent INTEGER, last_outbound_at TEXT,
  conversation_id TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS optouts (phone TEXT PRIMARY KEY, source TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT,
  batch_id TEXT, msg_id TEXT, version INTEGER, event TEXT, status_after TEXT,
  detail TEXT, synced INTEGER DEFAULT 0);
"""


def connect(path=None):
    path = Path(path) if path else ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "compliance_json" not in cols:   # ledgers created before the legal rules engine
        conn.execute("ALTER TABLE messages ADD COLUMN compliance_json TEXT")
    return conn


def get_meta(conn, k, default=None):
    r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_meta(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))


def is_ready(conn):
    return get_meta(conn, "ledger_ready") == "yes"


def log_event(conn, actor, event, msg=None, batch_id=None, detail=None):
    conn.execute(
        "INSERT INTO events(ts, actor, batch_id, msg_id, version, event, status_after, detail)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (iso(utcnow()), actor,
         (msg["batch_id"] if msg else batch_id),
         (msg["msg_id"] if msg else None),
         (msg["version"] if msg else None),
         event,
         (msg["status"] if msg else None),
         json.dumps(detail) if detail is not None else None))


def get_msg(conn, msg_id):
    return conn.execute("SELECT * FROM messages WHERE msg_id=?", (msg_id,)).fetchone()


def update_msg(conn, msg_id, **fields):
    fields["updated_at"] = iso(utcnow())
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE messages SET {cols} WHERE msg_id=?", (*fields.values(), msg_id))
    return get_msg(conn, msg_id)


# --------------------------------------------------------------------------
# Phone numbers and time zones
# --------------------------------------------------------------------------
TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}

_TZ_CODES = {
    "America/New_York": """201 202 203 207 212 215 216 220 223 227 229 231 234 239 240 248 252 260 267 269 272
        276 283 301 302 304 305 313 315 317 321 324 326 329 330 332 336 339 347 351 352 363 380 386
        401 404 407 410 412 413 419 423 434 436 440 443 445 463 470 472 475 478 484 502 508 513 516
        517 518 540 551 561 567 570 571 582 585 586 603 606 607 609 610 614 616 617 624 631 640 645
        646 656 667 678 679 680 681 686 689 703 704 706 716 717 718 724 727 728 732 734 740 743 754
        757 762 765 770 771 772 774 781 786 802 803 804 810 813 814 826 828 835 838 839 843 845 848
        854 856 857 859 860 862 863 864 865 878 904 908 910 912 914 917 919 929 934 937 941 943 947
        948 954 959 973 978 980 984 989""",
    "America/Chicago": """205 210 214 217 218 224 225 228 235 251 254 256 262 274 281 309 312 314
        316 318 319 320 325 327 331 334 337 346 353 361 402 405 409 414 417 430 447 457 464 469 479
        483 501 504 507 512 515 531 534 539 557 563 572 573 580 601 608 612 615 618 629 630 636 641
        651 659 660 662 682 708 712 713 715 726 730 731 737 763 769 773 779 806 815 816 817 830 832
        847 861 870 872 901 903 913 918 920 924 936 938 940 945 952 956 972 975 979 985""",
    "America/Denver": "303 307 385 406 435 505 575 719 720 801 915 970 983",
    "America/Phoenix": "480 520 602 623 928",
    "America/Los_Angeles": """206 209 213 253 279 310 323 341 350 360 369 408 415 424 425 442 458
        503 509 510 530 559 562 564 619 626 628 650 657 661 669 702 707 714 725 747 760 775 805 818
        820 831 840 858 909 916 925 949 951 971""",
    "America/Anchorage": "907",
    "Pacific/Honolulu": "808",
    "America/Puerto_Rico": "787 939",
}
# Area codes that span more than one time zone: the send window must be
# satisfied in every zone listed.
_SPLIT = {
    "850": ["America/Chicago", "America/New_York"], "448": ["America/Chicago", "America/New_York"],
    "812": ["America/New_York", "America/Chicago"], "930": ["America/New_York", "America/Chicago"],
    "219": ["America/Chicago"], "574": ["America/New_York", "America/Chicago"],
    "270": ["America/Chicago", "America/New_York"], "364": ["America/Chicago", "America/New_York"],
    "906": ["America/New_York", "America/Chicago"],
    "931": ["America/Chicago", "America/New_York"],
    "620": ["America/Chicago", "America/Denver"], "785": ["America/Chicago", "America/Denver"],
    "308": ["America/Chicago", "America/Denver"], "701": ["America/Chicago", "America/Denver"],
    "605": ["America/Chicago", "America/Denver"], "432": ["America/Chicago", "America/Denver"],
    "208": ["America/Denver", "America/Los_Angeles"], "986": ["America/Denver", "America/Los_Angeles"],
    "541": ["America/Los_Angeles", "America/Denver"],
}
AREA_TZ = {}
for _tz, _codes in _TZ_CODES.items():
    for _c in _codes.split():
        AREA_TZ[_c] = [_tz]
AREA_TZ.update(_SPLIT)


def normalize_phone(raw):
    """Return (e164, error). Only US/NANP mobile-style numbers are accepted."""
    if raw is None:
        return None, "no phone number"
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        return None, f"'{raw}' is not a US number (+1 and 10 digits)"
    area, exch = digits[1:4], digits[4:7]
    if area[0] in "01" or area[1:] == "11" or exch[0] in "01":
        return None, f"'{raw}' is not a valid US number"
    if area in TOLL_FREE:
        return None, f"'{raw}' is a toll-free number, not a mobile"
    return "+" + digits, None


def is_fictional(e164):
    """555-0100 to 555-0199 are reserved for fiction in the NANP."""
    d = e164[-7:]
    return d[:3] == "555" and 100 <= int(d[3:]) <= 199


def zones_for(e164, tz_override=None):
    if tz_override:
        return [tz_override]
    return AREA_TZ.get(e164[2:5])


def window_check(e164, tz_override=None, now=None):
    """Return None if now is inside the send window for the recipient, else reason."""
    now = now or utcnow()
    zones = zones_for(e164, tz_override)
    if not zones:
        return (f"unknown time zone for area code {e164[2:5]}; set the recipient's"
                " time zone (tz) before sending")
    for z in zones:
        local = now.astimezone(ZoneInfo(z))
        if local.weekday() not in SEND_DAYS or not (SEND_START_HOUR <= local.hour < SEND_END_HOUR):
            return (f"outside sending hours for recipient ({z.split('/')[-1].replace('_', ' ')}"
                    f" local time {local.strftime('%a %H:%M')}; allowed Mon-Fri"
                    f" {SEND_START_HOUR:02d}:00-{SEND_END_HOUR:02d}:00)")
    return None


def next_window_open(e164, tz_override=None, now=None):
    now = now or utcnow()
    t = now.replace(second=0, microsecond=0)
    for _ in range(60 * 24 * 8 // 15):
        if window_check(e164, tz_override, t) is None:
            return t
        t += timedelta(minutes=15)
    return None


# --------------------------------------------------------------------------
# SMS text: segments and character clean-up
# --------------------------------------------------------------------------
_GSM_BASIC = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
                 "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM_EXT = set("^{}\\[~]|€")
_REPLACE = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
            "—": "-", "…": "...", " ": " ", "•": "-"}


def clean_text(text):
    """Replace typographic characters that force expensive Unicode encoding."""
    changed = []
    for bad, good in _REPLACE.items():
        if bad in text:
            changed.append(f"{bad!r}->{good!r}")
            text = text.replace(bad, good)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return text, changed


def segment_info(text):
    if all(c in _GSM_BASIC or c in _GSM_EXT for c in text):
        n = sum(2 if c in _GSM_EXT else 1 for c in text)
        return {"encoding": "GSM-7", "chars": n,
                "segments": 1 if n <= 160 else math.ceil(n / 153), "limit": 160}
    n = len(text.encode("utf-16-le")) // 2
    bad = sorted({c for c in text if c not in _GSM_BASIC and c not in _GSM_EXT})
    return {"encoding": "UCS-2", "chars": n, "segments": 1 if n <= 70 else math.ceil(n / 67),
            "limit": 70, "unicode_chars": bad}


def content_hash(msg_id, version, phone, content, compliance_json=""):
    # The legal facts are part of what is approved: changing them voids approval.
    return hashlib.sha256(f"{msg_id}\n{version}\n{phone}\n{content}\n{compliance_json or ''}"
                          .encode()).hexdigest()


def msg_hash(m):
    return content_hash(m["msg_id"], m["version"], m["phone"], m["content"], m["compliance_json"])


def solicitation_id_problems(content):
    """FCC 64.1200(d)(4): a telemarketing message must identify the individual and
    the business. Required on every client solicitation, plus an opt-out line."""
    low = content.lower()
    probs = []
    if "mustapha" not in low or "westmont" not in low:
        probs.append("client solicitation must name Mustapha and Westmont in every text")
    if "stop" not in low:
        probs.append("client solicitation must include an opt-out line (e.g. 'Reply STOP to opt out')")
    return probs


def first_contact_problems(content):
    probs = []
    if "westmont" not in content.lower():
        probs.append("first text to this number must say it is from Westmont")
    if "stop" not in content.lower():
        probs.append("first text to this number must include an opt-out line (e.g. 'Reply STOP to opt out')")
    return probs


# --------------------------------------------------------------------------
# Presentation codes (one per review table shown to Mustapha)
# --------------------------------------------------------------------------
_LET = "ABCDEFGHJKLMNPQRSTUVWXYZ"   # no I/O
_DIG = "23456789"                   # no 0/1


def new_code(conn):
    rng = random.SystemRandom()
    while True:
        code = rng.choice(_LET) + rng.choice(_DIG) + rng.choice(_LET) + rng.choice(_DIG)
        if not conn.execute("SELECT 1 FROM presentations WHERE code=?", (code,)).fetchone():
            return code


CODE_RE = r"[A-HJ-NP-Z][2-9][A-HJ-NP-Z][2-9]"


# --------------------------------------------------------------------------
# The send rules. Used by the PreToolUse hook (mutate=True) and by the CLI
# dry-run (mutate=False). Returns (allowed: bool, reasons: list, msg_row).
# --------------------------------------------------------------------------
def evaluate_send(conn, tool_input, now=None, simulate=False):
    now = now or utcnow()
    reasons = []
    ti = tool_input or {}

    if not is_ready(conn):
        return False, ["SMS ledger not initialised/restored in this session; run the skill's"
                       " restore step first"], None

    mode = ti.get("mode") or "standard"
    if mode != "standard" or ti.get("from_alphanum"):
        reasons.append("only standard mode from the Westmont US number is allowed")
    if ti.get("scheduled_at"):
        reasons.append("scheduled sending is disabled (send inside the window instead)")
    if ti.get("user_id_forced") or ti.get("user_id"):
        reasons.append("sending on behalf of another user is not allowed")
    if ti.get("archived_auto"):
        reasons.append("auto-archiving conversations is not allowed (replies would be hidden)")

    frm, err = normalize_phone(ti.get("from_number"))
    if frm != FROM_NUMBER:
        reasons.append(f"from_number must be the Westmont US number {FROM_NUMBER}")

    to, err = normalize_phone(ti.get("to_number"))
    if err:
        return False, reasons + [err], None
    if is_fictional(to) and not simulate:
        reasons.append("fictional 555-01xx test number; never sent for real")

    content = ti.get("content") or ""
    rows = conn.execute("SELECT * FROM messages WHERE phone=? AND content=?", (to, content)).fetchall()
    approved = [r for r in rows if r["status"] == APPROVED]
    if not rows:
        return False, reasons + ["no drafted message matches this exact recipient and text"
                                 " (any edit needs a fresh approval)"], None
    if len(approved) != 1:
        sts = ", ".join(f"{r['msg_id']}={r['status']}" for r in rows)
        if len(approved) > 1:
            return False, reasons + [f"ambiguous: several approved messages match ({sts})"], None
        return False, reasons + [f"not approved for sending (status: {sts})"], rows[0]
    m = approved[0]

    if m["approved_hash"] != msg_hash(m):
        reasons.append("message or recipient changed after approval")
    exp = parse_iso(m["approval_expires"])
    if not exp or exp < now:
        reasons.append("approval has expired; show the message again for a new approval")

    if conn.execute("SELECT 1 FROM optouts WHERE phone=?", (to,)).fetchone():
        reasons.append("recipient is on the opt-out list")
    pc = conn.execute("SELECT * FROM prechecks WHERE msg_id=? ORDER BY checked_at DESC LIMIT 1",
                      (m["msg_id"],)).fetchone()
    if not pc:
        reasons.append("Ringover opt-out/history check not done for this message")
    else:
        age = now - parse_iso(pc["checked_at"])
        if age > timedelta(minutes=PRECHECK_MAX_AGE_MIN):
            reasons.append("Ringover opt-out/history check is older than"
                           f" {PRECHECK_MAX_AGE_MIN} minutes; re-check")
        if pc["opted_out"]:
            reasons.append("Ringover shows this number has opted out")
        if pc["identical_already_sent"]:
            reasons.append("Ringover already shows this exact text sent to this number")

    legal = C.assess(conn, m, now, msg_hash(m))
    if legal["status"] not in C.SENDABLE:
        reasons.append(f"legal check {legal['status']}: " + "; ".join(legal["reasons"]))
    if m["audience"] == "candidate" and not CONSENT["candidate"].get(m["consent_basis"]):
        reasons.append(f"consent basis '{m['consent_basis']}' does not allow texting this candidate")
    is_solicitation = m["audience"] == "client" and m["purpose"] not in ("client_service", "reply")
    if is_solicitation:
        reasons.extend(solicitation_id_problems(content))
    has_ebr = any("established business relationship" in e or "written permission" in e
                  for e in legal["exemptions"])

    w = window_check(to, m["tz_override"], now)
    if w:
        reasons.append(w)

    others = conn.execute(
        "SELECT * FROM messages WHERE phone=? AND msg_id<>? AND status IN (?,?,?)",
        (to, m["msg_id"], *SENT_LIKE)).fetchall()
    for o in others:
        t = parse_iso(o["submitted_at"]) or parse_iso(o["updated_at"])
        if o["content"] == content and t and now - t < timedelta(days=SAME_CONTENT_LOOKBACK_DAYS):
            reasons.append(f"identical text already sent to this number ({o['msg_id']})")
        elif m["purpose"] != "reply" and t and now - t < timedelta(hours=MIN_GAP_HOURS_SAME_NUMBER):
            reasons.append(f"another text went to this number in the last"
                           f" {MIN_GAP_HOURS_SAME_NUMBER}h ({o['msg_id']})")
        if o["status"] in (SUBMITTING, UNCERTAIN):
            reasons.append(f"an earlier text to this number has an unresolved status ({o['msg_id']})")

    if is_solicitation and not has_ebr:
        recent = [o for o in others if o["audience"] == "client" and
                  (parse_iso(o["submitted_at"]) or now) > now - timedelta(days=30)]
        if len(recent) >= C.COLD_MAX_PER_30_DAYS:
            reasons.append(f"Westmont policy: max {C.COLD_MAX_PER_30_DAYS} texts in 30 days to a contact"
                           " with no business relationship")

    prior_out = bool(others) or bool(pc and pc["last_outbound_at"])
    if not prior_out:
        reasons.extend(first_contact_problems(content))

    seg = segment_info(content)
    if seg["segments"] > MAX_SEGMENTS:
        reasons.append(f"message is {seg['segments']} segments; max {MAX_SEGMENTS}")

    return (not reasons), reasons, m
