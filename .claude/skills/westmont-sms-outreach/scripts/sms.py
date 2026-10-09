#!/usr/bin/env python3
"""Westmont SMS Outreach - command-line tool Claude uses to manage the ledger.

There is deliberately NO command that approves a message. Approvals are only
recorded by the UserPromptSubmit hook from text Mustapha types himself.

Commands
  status                         ledger summary + whether it is ready
  init --fresh                   first-ever start (Google Sheet log is empty)
  restore FILE.json              rebuild from the Google Sheet export (see SKILL.md)
  new-batch --audience A --purpose P --desc TEXT
  add BATCH FILE.json            add recipients/messages (list of objects)
  edit MSG_ID [--text T] [--phone P] [--tz Z]   new version, approval cleared
  remove MSG_ID                  cancel a draft
  present BATCH                  show review table + new approval code
  show BATCH                     current statuses
  precheck MSG_ID --opted-out yes|no --identical-sent yes|no
           [--last-outbound ISO|none] [--conversation-id N] [--note T]
  plan BATCH [--simulate]        which approved messages can go now + exact params
  reconcile MSG_ID --found yes|no [--failed yes|no] [--ringover-message-id N]
  hubspot-logged MSG_ID --note-id N
  optout-add PHONE --source TEXT
  sync-export                    rows for the Google Sheet (JSON)
  sync-mark EVENT_ID             mark events up to EVENT_ID as synced
"""

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smslib as L  # noqa: E402

FIELDS = ["msg_id", "batch_id", "line", "version", "status", "recipient_name", "company", "title",
          "phone", "audience", "purpose", "consent_basis", "hubspot_contact_id", "tz_override",
          "content", "segments", "encoding", "attempts", "submitted_at", "ringover_message_id",
          "ringover_conversation_id", "hubspot_note_id", "context_note", "created_at", "updated_at"]


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def need_ready(conn):
    if not L.is_ready(conn):
        die("ledger not ready in this session. Restore it from the Google Sheet first"
            " (SKILL.md step 0), or `init --fresh` if the Sheet has no rows yet.")


def cmd_status(conn, a):
    print(f"ledger: {L.ledger_path()}")
    print(f"ready: {L.is_ready(conn)}  (since {L.get_meta(conn, 'ready_at')}, via {L.get_meta(conn, 'ready_via')})")
    for r in conn.execute("SELECT status, COUNT(*) n FROM messages GROUP BY status"):
        print(f"  {r['status']}: {r['n']}")
    print(f"  opt-outs: {conn.execute('SELECT COUNT(*) FROM optouts').fetchone()[0]}")
    print(f"  unsynced events: {conn.execute('SELECT COUNT(*) FROM events WHERE synced=0').fetchone()[0]}")


def cmd_init(conn, a):
    if not a.fresh:
        die("use `init --fresh` only when the Google Sheet log has no message rows")
    if conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]:
        die("ledger already has messages")
    L.set_meta(conn, "ledger_ready", "yes")
    L.set_meta(conn, "ready_at", L.iso(L.utcnow()))
    L.set_meta(conn, "ready_via", "init-fresh")
    L.log_event(conn, "cli", "LEDGER_INIT_FRESH")
    print("ledger initialised (fresh)")


def cmd_restore(conn, a):
    """Import the Sheet's Messages + OptOuts tabs. Approvals never survive a
    restore: anything PRESENTED/APPROVED comes back as DRAFT; anything that was
    mid-send comes back UNCERTAIN so it can never be sent twice."""
    data = json.loads(Path(a.file).read_text())
    msgs, optouts = data.get("messages", []), data.get("optouts", [])
    n = 0
    conn.execute("BEGIN IMMEDIATE")
    for row in msgs:
        r = dict(zip(FIELDS, row)) if isinstance(row, list) else dict(row)
        if not r.get("msg_id") or r.get("msg_id") == "msg_id":
            continue
        st = r.get("status")
        if st in (L.PRESENTED, L.APPROVED):
            st = L.DRAFT
        elif st == L.SUBMITTING:
            st = L.UNCERTAIN
        r["status"] = st
        for k in ("line", "version", "segments", "attempts"):
            r[k] = int(r[k]) if str(r.get(k, "")).strip() not in ("", "None") else None
        for k in FIELDS:
            if r.get(k) in ("", "None"):
                r[k] = None
        existing = L.get_msg(conn, r["msg_id"])
        if existing and existing["status"] in L.SENT_LIKE | {L.RINGOVER_FAILED} and \
                r["status"] not in L.SENT_LIKE | {L.RINGOVER_FAILED}:
            continue  # never downgrade a sent record
        cols = [k for k in FIELDS]
        conn.execute(f"INSERT OR REPLACE INTO messages({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                     [r.get(k) for k in cols])
        b = r["batch_id"]
        if b and not conn.execute("SELECT 1 FROM batches WHERE batch_id=?", (b,)).fetchone():
            conn.execute("INSERT INTO batches(batch_id, created_at, audience, purpose, description)"
                         " VALUES(?,?,?,?,?)", (b, r.get("created_at"), r.get("audience"), r.get("purpose"),
                                                "restored"))
        n += 1
    for row in optouts:
        r = row if isinstance(row, dict) else dict(zip(["phone", "source", "added_at"], row))
        if r.get("phone") and r["phone"] != "phone":
            conn.execute("INSERT OR IGNORE INTO optouts(phone, source, added_at) VALUES(?,?,?)",
                         (r["phone"], r.get("source"), r.get("added_at")))
    L.set_meta(conn, "ledger_ready", "yes")
    L.set_meta(conn, "ready_at", L.iso(L.utcnow()))
    L.set_meta(conn, "ready_via", f"restore:{Path(a.file).name}")
    L.log_event(conn, "cli", "LEDGER_RESTORED", detail={"messages": n, "optouts": len(optouts)})
    conn.execute("UPDATE events SET synced=1")  # restored history is already in the Sheet
    conn.execute("COMMIT")
    print(f"restored {n} messages, {len(optouts)} opt-outs. Approvals were not restored (by design).")


def cmd_new_batch(conn, a):
    need_ready(conn)
    if a.audience not in L.AUDIENCES:
        die("audience must be candidate or client (never mixed in one batch)")
    if a.purpose not in L.PURPOSES[a.audience]:
        die(f"purpose must be one of {sorted(L.PURPOSES[a.audience])}")
    stamp = L.utcnow().strftime("B%m%d")
    for letter in "ABCDEFGHJKLMNPQRSTUVWXYZ":
        bid = stamp + letter
        if not conn.execute("SELECT 1 FROM batches WHERE batch_id=?", (bid,)).fetchone():
            break
    conn.execute("INSERT INTO batches VALUES(?,?,?,?,?)", (bid, L.iso(L.utcnow()), a.audience, a.purpose, a.desc))
    L.log_event(conn, "cli", "BATCH_CREATED", batch_id=bid, detail={"audience": a.audience, "purpose": a.purpose})
    print(bid)


def _validate(rec, audience):
    probs = []
    phone, err = L.normalize_phone(rec.get("phone"))
    if err:
        probs.append(err)
    if not (rec.get("name") or "").strip():
        probs.append("missing recipient name")
    basis = rec.get("consent_basis")
    if basis not in L.CONSENT[audience]:
        probs.append(f"consent_basis must be one of {sorted(L.CONSENT[audience])}")
    elif not L.CONSENT[audience][basis]:
        probs.append(f"consent_basis '{basis}' is not allowed for {audience}s - do not text")
    if audience == "client" and not rec.get("hubspot_contact_id"):
        probs.append("client must match an existing HubSpot contact (hubspot_contact_id)")
    if audience == "candidate" and rec.get("hubspot_contact_id"):
        probs.append("candidates must not be linked to HubSpot")
    if phone and not L.zones_for(phone, rec.get("tz")):
        probs.append(f"unknown time zone for area code {phone[2:5]}; add \"tz\"")
    if not (rec.get("text") or "").strip():
        probs.append("missing message text")
    return phone, probs


def cmd_add(conn, a):
    need_ready(conn)
    b = conn.execute("SELECT * FROM batches WHERE batch_id=?", (a.batch,)).fetchone()
    if not b:
        die("no such batch")
    recs = json.loads(Path(a.file).read_text())
    line = (conn.execute("SELECT MAX(line) FROM messages WHERE batch_id=?", (a.batch,)).fetchone()[0] or 0)
    for rec in recs:
        phone, probs = _validate(rec, b["audience"])
        text, changed = L.clean_text(rec.get("text") or "")
        if probs:
            print(f"SKIPPED {rec.get('name')}: " + "; ".join(probs))
            continue
        dup = conn.execute("SELECT msg_id FROM messages WHERE batch_id=? AND phone=? AND status<>?",
                           (a.batch, phone, L.CANCELLED)).fetchone()
        if dup:
            print(f"SKIPPED {rec.get('name')}: {phone} already in this batch ({dup[0]})")
            continue
        line += 1
        mid = f"{a.batch}-{line:02d}"
        seg = L.segment_info(text)
        now = L.iso(L.utcnow())
        conn.execute(
            "INSERT INTO messages(msg_id,batch_id,line,version,recipient_name,company,title,phone,audience,"
            "purpose,consent_basis,context_note,hubspot_contact_id,tz_override,content,segments,encoding,"
            "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, a.batch, line, 1, rec["name"].strip(), rec.get("company"), rec.get("title"), phone,
             b["audience"], rec.get("purpose") or b["purpose"], rec["consent_basis"], rec.get("context"),
             rec.get("hubspot_contact_id"), rec.get("tz"), text, seg["segments"], seg["encoding"],
             L.DRAFT, now, now))
        L.log_event(conn, "cli", "DRAFTED", L.get_msg(conn, mid))
        warn = []
        if changed:
            warn.append("cleaned " + ", ".join(changed))
        if seg["segments"] > 1:
            warn.append(f"{seg['segments']} segments ({seg['encoding']})")
        if seg["encoding"] != "GSM-7":
            warn.append(f"unicode chars {seg.get('unicode_chars')} make it cost more")
        print(f"{mid}  {rec['name']}  {phone}  {seg['chars']} chars/{seg['segments']} seg  " + "; ".join(warn))


def cmd_edit(conn, a):
    m = L.get_msg(conn, a.msg_id)
    if not m:
        die("no such message")
    if m["status"] in L.SENT_LIKE | {L.RINGOVER_FAILED, L.NOT_FOUND, L.CANCELLED}:
        die(f"cannot edit a message with status {m['status']}; draft a new one")
    fields = {}
    if a.text is not None:
        text, _ = L.clean_text(a.text)
        seg = L.segment_info(text)
        fields.update(content=text, segments=seg["segments"], encoding=seg["encoding"])
    if a.phone is not None:
        phone, err = L.normalize_phone(a.phone)
        if err:
            die(err)
        fields["phone"] = phone
    if a.tz is not None:
        fields["tz_override"] = a.tz
    fields.update(version=m["version"] + 1, status=L.DRAFT, approved_hash=None, approved_at=None,
                  approval_expires=None, presented_hash=None)
    m = L.update_msg(conn, a.msg_id, **fields)
    L.log_event(conn, "cli", "EDITED", m, detail={"fields": [k for k in fields if k in ("content", "phone", "tz_override")]})
    print(f"{a.msg_id} now version {m['version']} (DRAFT). Any earlier approval is void; present again.")


def cmd_remove(conn, a):
    m = L.get_msg(conn, a.msg_id)
    if not m or m["status"] in L.SENT_LIKE:
        die("cannot cancel (missing or already sent)")
    m = L.update_msg(conn, a.msg_id, status=L.CANCELLED, approved_hash=None, approval_expires=None)
    L.log_event(conn, "cli", "CANCELLED", m)
    print(f"{a.msg_id} cancelled")


def _table(rows, with_status=True):
    out = ["| # | ID | Recipient | Company | Phone | Message | Seg | Consent | Status |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        msg = r["content"].replace("\n", " / ").replace("|", "/")
        out.append(f"| {r['line']} | {r['msg_id']} v{r['version']} | {r['recipient_name']} | {r['company'] or '-'} |"
                   f" {r['phone']} | {msg} | {r['segments']} | {r['consent_basis']} | {r['status']} |")
    return "\n".join(out)


def cmd_present(conn, a):
    need_ready(conn)
    rows = conn.execute("SELECT * FROM messages WHERE batch_id=? AND status IN (?,?,?) ORDER BY line",
                        (a.batch, L.DRAFT, L.PRESENTED, L.APPROVED)).fetchall()
    if not rows:
        die("nothing to present")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE presentations SET active=0 WHERE batch_id=?", (a.batch,))
    code = L.new_code(conn)
    now = L.utcnow()
    items = []
    for r in rows:
        h = L.msg_hash(r)
        items.append({"line": r["line"], "msg_id": r["msg_id"], "hash": h})
        if r["status"] == L.APPROVED and r["approved_hash"] == h:
            continue
        m = L.update_msg(conn, r["msg_id"], status=L.PRESENTED, presented_code=code, presented_hash=h)
        L.log_event(conn, "cli", "PRESENTED", m, detail={"code": code})
    conn.execute("INSERT INTO presentations VALUES(?,?,?,?,?,?)",
                 (code, a.batch, L.iso(now), L.iso(now + timedelta(hours=L.PRESENTATION_TTL_HOURS)), 1,
                  json.dumps(items)))
    conn.execute("COMMIT")
    rows = conn.execute("SELECT * FROM messages WHERE batch_id=? AND status IN (?,?) ORDER BY line",
                        (a.batch, L.PRESENTED, L.APPROVED)).fetchall()
    print(_table(rows))
    print(f"\nREVIEW CODE: {code}  (valid {L.PRESENTATION_TTL_HOURS}h; replaces any earlier code for {a.batch})")


def cmd_show(conn, a):
    rows = conn.execute("SELECT * FROM messages WHERE batch_id=? ORDER BY line", (a.batch,)).fetchall()
    print(_table(rows))


def yn(v):
    return 1 if str(v).lower() in ("yes", "y", "true", "1") else 0


def cmd_precheck(conn, a):
    m = L.get_msg(conn, a.msg_id)
    if not m:
        die("no such message")
    last = None if (a.last_outbound or "none").lower() == "none" else a.last_outbound
    conn.execute("INSERT INTO prechecks VALUES(?,?,?,?,?,?,?,?)",
                 (a.msg_id, L.iso(L.utcnow()), m["phone"], yn(a.opted_out), yn(a.identical_sent), last,
                  a.conversation_id, a.note))
    L.log_event(conn, "cli", "PRECHECK", m, detail={"opted_out": yn(a.opted_out),
                                                    "identical_sent": yn(a.identical_sent), "last_outbound": last})
    if yn(a.opted_out):
        conn.execute("INSERT OR IGNORE INTO optouts VALUES(?,?,?)", (m["phone"], "ringover", L.iso(L.utcnow())))
        m = L.update_msg(conn, a.msg_id, status=L.BLOCKED_OPTOUT)
        L.log_event(conn, "cli", "BLOCKED_OPTOUT", m)
        print(f"{a.msg_id}: OPTED OUT - blocked permanently")
    else:
        print(f"{a.msg_id}: precheck recorded")


def cmd_plan(conn, a):
    rows = conn.execute("SELECT * FROM messages WHERE batch_id=? ORDER BY line", (a.batch,)).fetchall()
    for r in rows:
        if r["status"] != L.APPROVED:
            print(f"{r['msg_id']}: {r['status']} - not sendable")
            continue
        params = {"from_number": L.FROM_NUMBER, "to_number": r["phone"], "content": r["content"]}
        ok, reasons, _ = L.evaluate_send(conn, params, simulate=a.simulate)
        if ok:
            print(f"{r['msg_id']}: READY" + (" (simulation - fictional number allowed)" if a.simulate else ""))
            print("  " + json.dumps(params))
        else:
            print(f"{r['msg_id']}: WOULD BE BLOCKED - " + "; ".join(reasons))
            nxt = L.next_window_open(r["phone"], r["tz_override"])
            if any("outside sending hours" in x for x in reasons) and nxt:
                print(f"  window next opens {L.iso(nxt)}")


def cmd_reconcile(conn, a):
    m = L.get_msg(conn, a.msg_id)
    if not m or m["status"] not in (L.UNCERTAIN, L.SUBMITTING, L.SUBMITTED):
        die("reconcile applies to SUBMITTING/UNCERTAIN/SUBMITTED messages only")
    if yn(a.found):
        st = L.RINGOVER_FAILED if yn(a.failed) else L.SUBMITTED
    else:
        if m["status"] == L.SUBMITTED:
            die("Ringover had accepted this message; recheck before marking it not found")
        st = L.NOT_FOUND
    m = L.update_msg(conn, a.msg_id, status=st,
                     ringover_message_id=a.ringover_message_id or m["ringover_message_id"])
    L.log_event(conn, "cli", f"RECONCILED_{st}", m)
    hint = " Mustapha may type `retry " + a.msg_id + "` to allow one more attempt." if st in (L.RINGOVER_FAILED, L.NOT_FOUND) else ""
    print(f"{a.msg_id} -> {st}.{hint}")


def cmd_hubspot_logged(conn, a):
    m = L.get_msg(conn, a.msg_id)
    if not m or m["audience"] != "client":
        die("only client messages are logged to HubSpot")
    if m["hubspot_note_id"]:
        die(f"already logged as note {m['hubspot_note_id']}")
    m = L.update_msg(conn, a.msg_id, hubspot_note_id=a.note_id)
    L.log_event(conn, "cli", "HUBSPOT_LOGGED", m, detail={"note_id": a.note_id})
    print("recorded")


def cmd_optout_add(conn, a):
    phone, err = L.normalize_phone(a.phone)
    if err:
        die(err)
    conn.execute("INSERT OR IGNORE INTO optouts VALUES(?,?,?)", (phone, a.source, L.iso(L.utcnow())))
    for r in conn.execute("SELECT msg_id FROM messages WHERE phone=? AND status IN (?,?,?)",
                          (phone, L.DRAFT, L.PRESENTED, L.APPROVED)).fetchall():
        m = L.update_msg(conn, r["msg_id"], status=L.BLOCKED_OPTOUT)
        L.log_event(conn, "cli", "BLOCKED_OPTOUT", m)
    L.log_event(conn, "cli", "OPTOUT_ADDED", detail={"phone": phone, "source": a.source})
    print(f"{phone} opted out")


def cmd_sync_export(conn, a):
    msgs = [[r[k] for k in FIELDS] for r in conn.execute("SELECT * FROM messages ORDER BY msg_id")]
    opt = [[r["phone"], r["source"], r["added_at"]] for r in conn.execute("SELECT * FROM optouts")]
    ev = [[r["event_id"], r["ts"], r["actor"], r["batch_id"], r["msg_id"], r["version"], r["event"],
           r["status_after"], r["detail"]] for r in conn.execute("SELECT * FROM events WHERE synced=0 ORDER BY event_id")]
    print(json.dumps({"messages_header": FIELDS, "messages": msgs,
                      "optouts_header": ["phone", "source", "added_at"], "optouts": opt,
                      "events_header": ["event_id", "ts", "actor", "batch_id", "msg_id", "version", "event",
                                        "status_after", "detail"], "new_events": ev,
                      "max_event_id": ev[-1][0] if ev else None}, default=str))


def cmd_sync_mark(conn, a):
    conn.execute("UPDATE events SET synced=1 WHERE event_id<=?", (a.event_id,))
    print("marked")


def main():
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("status")
    x = s.add_parser("init"); x.add_argument("--fresh", action="store_true")
    x = s.add_parser("restore"); x.add_argument("file")
    x = s.add_parser("new-batch"); x.add_argument("--audience", required=True); x.add_argument("--purpose", required=True); x.add_argument("--desc", default="")
    x = s.add_parser("add"); x.add_argument("batch"); x.add_argument("file")
    x = s.add_parser("edit"); x.add_argument("msg_id"); x.add_argument("--text"); x.add_argument("--phone"); x.add_argument("--tz")
    x = s.add_parser("remove"); x.add_argument("msg_id")
    x = s.add_parser("present"); x.add_argument("batch")
    x = s.add_parser("show"); x.add_argument("batch")
    x = s.add_parser("precheck"); x.add_argument("msg_id"); x.add_argument("--opted-out", required=True)
    x.add_argument("--identical-sent", required=True); x.add_argument("--last-outbound"); x.add_argument("--conversation-id"); x.add_argument("--note")
    x = s.add_parser("plan"); x.add_argument("batch"); x.add_argument("--simulate", action="store_true")
    x = s.add_parser("reconcile"); x.add_argument("msg_id"); x.add_argument("--found", required=True); x.add_argument("--failed", default="no"); x.add_argument("--ringover-message-id")
    x = s.add_parser("hubspot-logged"); x.add_argument("msg_id"); x.add_argument("--note-id", required=True)
    x = s.add_parser("optout-add"); x.add_argument("phone"); x.add_argument("--source", required=True)
    s.add_parser("sync-export")
    x = s.add_parser("sync-mark"); x.add_argument("event_id", type=int)
    a = p.parse_args()
    conn = L.connect()
    globals()["cmd_" + a.cmd.replace("-", "_")](conn, a)


if __name__ == "__main__":
    main()
