#!/usr/bin/env python3
"""Westmont SMS Outreach - Claude Code hooks (run by the harness, not by Claude).

  prompt  UserPromptSubmit : records approvals/rejections/retries that Mustapha
                             TYPES, e.g. "approve K7Q3", "approve K7Q3 1,3",
                             "approve K7Q3 all except 2", "reject K7Q3 2",
                             "retry B1009A-03".
  pre     PreToolUse       : on mcp__ringover__sms_send - blocks anything that is
                             not an approved, unchanged, eligible message; on
                             success marks it SUBMITTING and still forces the
                             normal Ringover permission prompt ("ask").
                             On mcp__ringover__sms_opt_out_remove - always blocks.
                             On Bash/Write/Edit - blocks direct tampering with the
                             ledger or with this guard.
  post    PostToolUse      : records Ringover's response (SUBMITTED / UNCERTAIN).
  fail    PostToolUseFailure: records UNCERTAIN (never auto-resent).
"""

import json
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smslib as L  # noqa: E402

SEND_TOOL = "mcp__ringover__sms_send"
OPTOUT_REMOVE_TOOL = "mcp__ringover__sms_opt_out_remove"


def out(obj):
    sys.stdout.write(json.dumps(obj))


def deny(reason):
    out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                "permissionDecisionReason": reason}})


def ask(reason):
    out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                "permissionDecisionReason": reason}})


# --------------------------------------------------------------------------
# Approval parsing (user-typed text only)
# --------------------------------------------------------------------------
def _numbers(text):
    nums = set()
    for a, b in re.findall(r"(\d+)\s*-\s*(\d+)", text):
        nums.update(range(int(a), int(b) + 1))
    text = re.sub(r"(\d+)\s*-\s*(\d+)", " ", text)
    nums.update(int(n) for n in re.findall(r"\d+", text))
    return nums


def parse_commands(prompt):
    cmds = []
    for line in prompt.splitlines():
        for m in re.finditer(r"\b(approve|reject)\b(.*?)\b(" + L.CODE_RE + r")\b([^.;\n]*)",
                             line, re.I):
            verb, code, rest = m.group(1).lower(), m.group(3).upper(), m.group(4)
            rest_l = rest.lower()
            if "except" in rest_l or "but not" in rest_l:
                sel = ("except", _numbers(rest))
            elif _numbers(rest):
                sel = ("only", _numbers(rest))
            else:
                sel = ("all", set())
            cmds.append((verb, code, sel))
        for m in re.finditer(r"\bretry\s+([A-Z]\d{4}[A-Z]-\d{2})\b", line, re.I):
            cmds.append(("retry", m.group(1).upper(), None))
    return cmds


def handle_prompt(data, conn, now=None):
    now = now or L.utcnow()
    prompt = data.get("prompt") or ""
    cmds = parse_commands(prompt)
    if not cmds:
        return None
    notes = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for verb, code, sel in cmds:
            if verb == "retry":
                m = L.get_msg(conn, code)
                if not m:
                    notes.append(f"retry {code}: no such message")
                elif m["status"] not in (L.RINGOVER_FAILED, L.NOT_FOUND):
                    notes.append(f"retry {code}: refused, status is {m['status']} (only"
                                 " RINGOVER_FAILED or NOT_FOUND can be retried)")
                else:
                    m = L.update_msg(conn, code, status=L.APPROVED, approved_hash=L.msg_hash(m),
                                     approved_at=L.iso(now),
                                     approval_expires=L.iso(now + timedelta(hours=L.APPROVAL_TTL_HOURS)))
                    L.log_event(conn, "user", "RETRY_APPROVED", m)
                    notes.append(f"retry {code}: approved for one more attempt")
                continue
            p = conn.execute("SELECT * FROM presentations WHERE code=?", (code,)).fetchone()
            if not p or not p["active"]:
                notes.append(f"{verb} {code}: unknown or superseded review code; nothing recorded")
                continue
            if L.parse_iso(p["expires_at"]) < now:
                notes.append(f"{verb} {code}: review code expired; ask Claude to show the batch again")
                continue
            items = json.loads(p["items"])  # [{line, msg_id, hash}]
            kind, nums = sel
            lines = {i["line"] for i in items}
            if kind == "only":
                chosen = nums & lines
                missing = nums - lines
                if missing:
                    notes.append(f"{verb} {code}: line(s) {sorted(missing)} not in that table; ignored")
            elif kind == "except":
                chosen = lines - nums
            else:
                chosen = lines
            for it in items:
                if it["line"] not in chosen:
                    continue
                m = L.get_msg(conn, it["msg_id"])
                if not m:
                    continue
                if L.msg_hash(m) != it["hash"]:
                    notes.append(f"{it['msg_id']}: changed after you saw it; NOT {verb}d")
                    continue
                if m["status"] not in (L.PRESENTED, L.APPROVED, L.REJECTED):
                    notes.append(f"{it['msg_id']}: status {m['status']}; NOT {verb}d")
                    continue
                if verb == "approve":
                    m = L.update_msg(conn, m["msg_id"], status=L.APPROVED, approved_hash=it["hash"],
                                     approved_at=L.iso(now),
                                     approval_expires=L.iso(now + timedelta(hours=L.APPROVAL_TTL_HOURS)))
                    L.log_event(conn, "user", "APPROVED", m, detail={"code": code})
                    notes.append(f"{m['msg_id']}: APPROVED")
                else:
                    m = L.update_msg(conn, m["msg_id"], status=L.REJECTED, approved_hash=None,
                                     approved_at=None, approval_expires=None)
                    L.log_event(conn, "user", "REJECTED", m, detail={"code": code})
                    notes.append(f"{m['msg_id']}: REJECTED")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return ("[Westmont SMS guard - recorded from Mustapha's own message]\n- " + "\n- ".join(notes)
            + "\nOnly APPROVED messages can be sent; each send still shows the Ringover prompt.")


# --------------------------------------------------------------------------
# PreToolUse
# --------------------------------------------------------------------------
_PROTECTED = ("sms_data", "ledger.db", "sms_guard", "smslib", "sqlite3", "WESTMONT_SMS_LEDGER")


def handle_pre(data, conn, now=None):
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    if tool == OPTOUT_REMOVE_TOOL:
        return deny("Removing a number from the SMS opt-out list is blocked. If someone"
                    " genuinely opted back in, Mustapha must do it himself in the Ringover app.")
    if tool == "Bash":
        cmd = ti.get("command", "")
        if any(p in cmd for p in _PROTECTED):
            return deny("Direct access to the SMS ledger or guard is blocked; use sms.py commands.")
        return None
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        fp = ti.get("file_path") or ti.get("notebook_path") or ""
        if "sms_data" in fp or fp.endswith("ledger.db"):
            return deny("Editing the SMS ledger directly is blocked.")
        return None
    if tool != SEND_TOOL:
        return None

    conn.execute("BEGIN IMMEDIATE")
    try:
        ok, reasons, m = L.evaluate_send(conn, ti, now=now)
        if not ok:
            if m is not None:
                L.log_event(conn, "hook", "SEND_BLOCKED", m, detail={"reasons": reasons})
            conn.execute("COMMIT")
            return deny("Westmont SMS guard blocked this send:\n- " + "\n- ".join(reasons))
        m = L.update_msg(conn, m["msg_id"], status=L.SUBMITTING, attempts=(m["attempts"] or 0) + 1,
                         submitted_at=L.iso(now or L.utcnow()))
        L.log_event(conn, "hook", "SUBMITTING", m)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    seg = L.segment_info(m["content"])
    return ask(f"Approved SMS {m['msg_id']} to {m['recipient_name']} ({m['phone']}), "
               f"{seg['segments']} segment(s). Approve this Ringover send?")


# --------------------------------------------------------------------------
# PostToolUse / PostToolUseFailure
# --------------------------------------------------------------------------
def _find_submitting(conn, ti):
    to, _ = L.normalize_phone(ti.get("to_number"))
    return conn.execute("SELECT * FROM messages WHERE phone=? AND content=? AND status=?",
                        (to, ti.get("content") or "", L.SUBMITTING)).fetchone()


def _flatten(resp):
    if isinstance(resp, str):
        return resp
    try:
        return json.dumps(resp)
    except Exception:
        return str(resp)


def handle_post(data, conn, failed=False):
    if data.get("tool_name") != SEND_TOOL:
        return None
    ti = data.get("tool_input") or {}
    m = _find_submitting(conn, ti)
    if not m:
        return None
    text = _flatten(data.get("tool_response") if not failed else data.get("error"))
    low = re.sub(r'"is_failed"\s*:\s*false', "", text.lower())
    mid = re.search(r'"message_id"\s*:\s*(\d+)', text)
    cid = re.search(r'"conversation_id"\s*:\s*(\d+)', text)
    errorish = failed or any(w in low for w in ('"error"', "is_error\": true", "failed", "denied",
                                                 "invalid", "forbidden", "unauthorized", "timeout"))
    if not errorish and (mid or '"success"' in low or "sent" in low or "message" in low):
        status, event = L.SUBMITTED, "SUBMITTED"
    else:
        status, event = L.UNCERTAIN, "UNCERTAIN"
    m = L.update_msg(conn, m["msg_id"], status=status, result_detail=text[:1500],
                     ringover_message_id=mid.group(1) if mid else None,
                     ringover_conversation_id=cid.group(1) if cid else None)
    L.log_event(conn, "hook", event, m, detail={"response": text[:500]})
    note = ("Ringover accepted the request (this is submission, not confirmed delivery)."
            if status == L.SUBMITTED else
            "Result is UNCERTAIN. Do NOT resend. Check the Ringover conversation and run"
            " `sms.py reconcile`.")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure" if failed else "PostToolUse",
                                   "additionalContext": f"[Westmont SMS guard] {m['msg_id']} -> {status}. {note}"}}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    try:
        conn = L.connect()
    except Exception as e:  # if the ledger is unusable, sending must fail closed
        if mode == "pre" and data.get("tool_name") in (SEND_TOOL, OPTOUT_REMOVE_TOOL):
            deny(f"SMS ledger unavailable ({e}); sending blocked.")
        return 0
    try:
        if mode == "prompt":
            msg = handle_prompt(data, conn)
            if msg:
                out({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                            "additionalContext": msg}})
        elif mode == "pre":
            handle_pre(data, conn)
        elif mode in ("post", "fail"):
            r = handle_post(data, conn, failed=(mode == "fail"))
            if r:
                out(r)
    except Exception as e:
        if mode == "pre" and data.get("tool_name") in (SEND_TOOL, OPTOUT_REMOVE_TOOL):
            deny(f"SMS guard error ({type(e).__name__}: {e}); sending blocked.")
        elif mode == "prompt":
            out({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                        "additionalContext": f"[Westmont SMS guard] error recording"
                                        f" approval: {e}. Nothing was approved."}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
