"""End-to-end tests for the Westmont SMS Outreach safeguards.

Runs the real CLI (sms.py) and the real hook script (sms_guard.py) as
subprocesses against a throwaway ledger. Nothing here talks to Ringover.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import smslib as L  # noqa: E402

FROM = L.FROM_NUMBER


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ledger.db")
        os.environ["WESTMONT_SMS_LEDGER"] = self.db
        self.env = dict(os.environ, WESTMONT_SMS_LEDGER=self.db)
        self.cli("init", "--fresh")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True):
        r = subprocess.run([sys.executable, str(SCRIPTS / "sms.py"), *args], env=self.env,
                           capture_output=True, text=True)
        if ok and r.returncode != 0:
            raise AssertionError(f"sms.py {args} failed: {r.stdout}{r.stderr}")
        return r.stdout

    def hook(self, mode, payload):
        r = subprocess.run([sys.executable, str(SCRIPTS / "sms_guard.py"), mode], env=self.env,
                           input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def conn(self):
        return L.connect(self.db)

    def batch(self, recs, audience="candidate", purpose="opportunity"):
        bid = self.cli("new-batch", "--audience", audience, "--purpose", purpose, "--desc", "t").strip()
        f = os.path.join(self.tmp.name, "recs.json")
        Path(f).write_text(json.dumps(recs))
        out = self.cli("add", bid, f)
        return bid, out

    def present(self, bid):
        out = self.cli("present", bid)
        return out.split("REVIEW CODE: ")[1].split()[0]

    def say(self, text):
        return self.hook("prompt", {"prompt": text})

    def precheck(self, mid, **kw):
        args = ["precheck", mid, "--opted-out", kw.get("opted_out", "no"),
                "--identical-sent", kw.get("identical", "no")]
        if kw.get("last"):
            args += ["--last-outbound", kw["last"]]
        self.cli(*args)

    def status(self, mid):
        return L.get_msg(self.conn(), mid)["status"]

    def send(self, mid=None, **override):
        m = L.get_msg(self.conn(), mid)
        ti = {"from_number": FROM, "to_number": m["phone"], "content": m["content"]}
        ti.update(override)
        return self.hook("pre", {"tool_name": "mcp__ringover__sms_send", "tool_input": ti}), ti

    def decision(self, res):
        return res["hookSpecificOutput"]["permissionDecision"] if res else None

    def reason(self, res):
        return res["hookSpecificOutput"]["permissionDecisionReason"]


TXT = "Hi Dana, Mustapha at Westmont Global. Senior relay testing contract in Houston, 6 months. Open to a quick chat? Reply STOP to opt out"


def rec(name="Dana Fictional", phone="(214) 555-0101", text=TXT, **kw):
    r = {"name": name, "phone": phone, "consent_basis": "provided_number", "text": text}
    r.update(kw)
    return r


def in_window(phone="+12145550101"):
    """True if the recipient's sending window is open right now."""
    return L.window_check(phone) is None


class TestApproval(Base):
    def test_draft_is_not_sendable(self):
        bid, _ = self.batch([rec()])
        res, _ = self.send(f"{bid}-01")
        self.assertEqual(self.decision(res), "deny")
        self.assertIn("not approved", self.reason(res))

    def test_presented_is_not_sendable_without_typed_approval(self):
        bid, _ = self.batch([rec()])
        self.present(bid)
        res, _ = self.send(f"{bid}-01")
        self.assertEqual(self.decision(res), "deny")

    def test_typed_approval_records_and_wrong_code_does_not(self):
        bid, _ = self.batch([rec(), rec("Lee Fictional", "214-555-0102")])
        code = self.present(bid)
        self.say("approve Z9Z9")                     # unknown code
        self.assertEqual(self.status(f"{bid}-01"), "PRESENTED")
        self.say("Please prepare these")             # 'prepare' is never approval
        self.assertEqual(self.status(f"{bid}-01"), "PRESENTED")
        self.say(f"approve {code} 2")
        self.assertEqual(self.status(f"{bid}-01"), "PRESENTED")
        self.assertEqual(self.status(f"{bid}-02"), "APPROVED")

    def test_approve_all_except_and_reject(self):
        bid, _ = self.batch([rec(), rec("B Fictional", "214-555-0102"), rec("C Fictional", "214-555-0103")])
        code = self.present(bid)
        self.say(f"approve {code} all except 2")
        self.say(f"reject {code} 2")
        self.assertEqual([self.status(f"{bid}-0{i}") for i in (1, 2, 3)], ["APPROVED", "REJECTED", "APPROVED"])

    def test_edit_after_approval_invalidates(self):
        bid, _ = self.batch([rec()])
        code = self.present(bid)
        self.say(f"approve {code}")
        self.cli("edit", f"{bid}-01", "--text", TXT + " Thanks")
        self.assertEqual(self.status(f"{bid}-01"), "DRAFT")
        self.say(f"approve {code}")                   # old code: content changed -> refused
        self.assertEqual(self.status(f"{bid}-01"), "DRAFT")
        res, _ = self.send(f"{bid}-01")
        self.assertEqual(self.decision(res), "deny")

    def test_recipient_change_invalidates(self):
        bid, _ = self.batch([rec()])
        code = self.present(bid)
        self.say(f"approve {code}")
        self.cli("edit", f"{bid}-01", "--phone", "214-555-0109")
        self.assertEqual(self.status(f"{bid}-01"), "DRAFT")

    def test_edit_between_present_and_approval_is_refused(self):
        bid, _ = self.batch([rec()])
        code = self.present(bid)
        self.cli("edit", f"{bid}-01", "--text", TXT + " (changed)")
        ctx = self.say(f"approve {code}")
        self.assertIn("NOT approved", json.dumps(ctx))
        self.assertEqual(self.status(f"{bid}-01"), "DRAFT")

    def test_new_presentation_supersedes_old_code(self):
        bid, _ = self.batch([rec()])
        old = self.present(bid)
        self.present(bid)
        self.say(f"approve {old}")
        self.assertEqual(self.status(f"{bid}-01"), "PRESENTED")

    def test_tool_input_must_match_exact_text(self):
        bid, _ = self.batch([rec()])
        code = self.present(bid)
        self.say(f"approve {code}")
        res, _ = self.send(f"{bid}-01", content=TXT + "!")
        self.assertEqual(self.decision(res), "deny")
        self.assertIn("no drafted message matches", self.reason(res))

    def test_expired_approval(self):
        bid, _ = self.batch([rec()])
        code = self.present(bid)
        self.say(f"approve {code}")
        c = self.conn()
        L.update_msg(c, f"{bid}-01", approval_expires=L.iso(L.utcnow() - timedelta(minutes=1)))
        self.precheck(f"{bid}-01")
        ok, reasons, _ = L.evaluate_send(c, {"from_number": FROM, "to_number": "+12145550101", "content": TXT},
                                         simulate=True)
        self.assertFalse(ok)
        self.assertTrue(any("expired" in r for r in reasons))


class TestSendRules(Base):
    def approved(self, recs=None, **kw):
        bid, _ = self.batch(recs or [rec()], **kw)
        code = self.present(bid)
        self.say(f"approve {code}")
        return bid

    def ok_now(self, mid, **kw):
        c = self.conn()
        m = L.get_msg(c, mid)
        now = L.next_window_open(m["phone"], m["tz_override"])
        c.execute("INSERT INTO prechecks VALUES(?,?,?,?,?,?,?,?)",
                  (mid, L.iso(now), m["phone"], kw.get("opted_out", 0), kw.get("identical", 0),
                   kw.get("last"), None, None))
        L.update_msg(c, mid, approval_expires=L.iso(now + timedelta(hours=1)))
        return L.evaluate_send(c, {"from_number": FROM, "to_number": m["phone"], "content": m["content"]},
                               now=now, simulate=True)

    def test_fully_valid_message_passes_in_simulation(self):
        bid = self.approved()
        ok, reasons, _ = self.ok_now(f"{bid}-01")
        self.assertTrue(ok, reasons)

    def test_fictional_number_never_sent_by_hook(self):
        bid = self.approved()
        self.precheck(f"{bid}-01")
        res, _ = self.send(f"{bid}-01")
        self.assertEqual(self.decision(res), "deny")
        self.assertIn("fictional", self.reason(res))
        self.assertEqual(self.status(f"{bid}-01"), "APPROVED")

    def test_missing_precheck_blocks(self):
        bid = self.approved()
        c = self.conn()
        now = L.next_window_open("+12145550101")
        L.update_msg(c, f"{bid}-01", approval_expires=L.iso(now + timedelta(hours=1)))
        ok, reasons, _ = L.evaluate_send(c, {"from_number": FROM, "to_number": "+12145550101", "content": TXT},
                                         now=now, simulate=True)
        self.assertFalse(ok)
        self.assertTrue(any("check not done" in r for r in reasons))

    def test_optout_blocks(self):
        bid = self.approved()
        ok, reasons, _ = self.ok_now(f"{bid}-01", opted_out=1)
        self.assertFalse(ok)
        self.assertTrue(any("opted out" in r for r in reasons))
        self.cli("optout-add", "214-555-0101", "--source", "replied STOP")
        ok, reasons, _ = self.ok_now(f"{bid}-01")
        self.assertTrue(any("opt-out list" in r for r in reasons) or not ok)

    def test_optout_remove_tool_always_blocked(self):
        res = self.hook("pre", {"tool_name": "mcp__ringover__sms_opt_out_remove",
                                "tool_input": {"phone_number": "+12145550101"}})
        self.assertEqual(self.decision(res), "deny")

    def test_wrong_sender_and_scheduling_blocked(self):
        bid = self.approved()
        res, _ = self.send(f"{bid}-01", from_number="+13462143209")
        self.assertIn("from_number", self.reason(res))
        res, _ = self.send(f"{bid}-01", scheduled_at="2026-10-12T15:00:00Z")
        self.assertIn("scheduled", self.reason(res))

    def test_quiet_hours(self):
        sat = L.parse_iso("2026-10-10T17:00:00Z")      # Saturday
        late = L.parse_iso("2026-10-13T02:00:00Z")     # Mon 21:00 Central
        ok = L.parse_iso("2026-10-13T16:00:00Z")       # Tue 11:00 Central
        self.assertIsNotNone(L.window_check("+12145550101", now=sat))
        self.assertIsNotNone(L.window_check("+12145550101", now=late))
        self.assertIsNone(L.window_check("+12145550101", now=ok))
        # El Paso is Mountain time: 15:30 UTC = 09:30 Central but 08:30 Mountain
        early = L.parse_iso("2026-10-13T14:30:00Z")
        self.assertIsNone(L.window_check("+12145550101", now=L.parse_iso("2026-10-13T14:30:00Z")))
        self.assertIsNotNone(L.window_check("+19155550101", now=early))
        # split area code must satisfy both zones
        self.assertEqual(L.zones_for("+14325550101"), ["America/Chicago", "America/Denver"])

    def test_first_contact_needs_westmont_and_stop(self):
        bid = self.approved([rec(text="Hi Dana, senior relay testing contract in Houston. Interested?")])
        ok, reasons, _ = self.ok_now(f"{bid}-01")
        self.assertFalse(ok)
        self.assertTrue(any("Westmont" in r for r in reasons))
        bid2 = self.approved([rec("Pat Fictional", "214-555-0111",
                                  text="Hi Pat, following up on our call about the SCADA role. Still keen?")])
        ok, reasons, _ = self.ok_now(f"{bid2}-01", last="2026-10-01T15:00:00Z")
        self.assertTrue(ok, reasons)

    def test_cold_client_blocked_and_candidate_not_in_hubspot(self):
        bid, out = self.batch([rec(consent_basis="cold", hubspot_contact_id="123")], audience="client",
                              purpose="prospecting")
        self.assertIn("SKIPPED", out)
        bid, out = self.batch([rec(hubspot_contact_id="123")])
        self.assertIn("must not be linked to HubSpot", out)
        bid, out = self.batch([rec(consent_basis="prior_conversation")], audience="client", purpose="follow_up")
        self.assertIn("existing HubSpot contact", out)

    def test_bad_numbers_rejected(self):
        for p in ("+447700900123", "800-555-1234", "12345", "(114) 555-0101"):
            self.assertIsNotNone(L.normalize_phone(p)[1], p)


class TestDuplicatesAndOutcomes(Base):
    def real_batch(self, text=TXT, phone="214-321-0101"):
        bid, _ = self.batch([rec(phone=phone, text=text)])
        code = self.present(bid)
        self.say(f"approve {code}")
        self.precheck(f"{bid}-01")
        return bid

    def test_full_cycle_duplicate_uncertain_retry(self):
        if not in_window("+12143210101"):
            self.skipTest("Dallas sending window closed right now; rules covered by simulation tests")
        bid = self.real_batch()
        mid = f"{bid}-01"
        res, ti = self.send(mid)
        self.assertEqual(self.decision(res), "ask")           # still forces the Ringover prompt
        self.assertEqual(self.status(mid), "SUBMITTING")
        res2, _ = self.send(mid)                                # duplicate attempt before result
        self.assertEqual(self.decision(res2), "deny")
        # tool fails/times out -> UNCERTAIN, never auto-resent
        self.hook("fail", {"tool_name": "mcp__ringover__sms_send", "tool_input": ti, "error": "timeout"})
        self.assertEqual(self.status(mid), "UNCERTAIN")
        self.assertEqual(self.decision(self.send(mid)[0]), "deny")
        self.say(f"retry {mid}")                                # retry refused until reconciled
        self.assertEqual(self.status(mid), "UNCERTAIN")
        self.cli("reconcile", mid, "--found", "no")
        self.assertEqual(self.status(mid), "NOT_FOUND")
        self.say(f"retry {mid}")
        self.assertEqual(self.status(mid), "APPROVED")
        self.precheck(mid)
        res, ti = self.send(mid)
        self.assertEqual(self.decision(res), "ask")
        self.hook("post", {"tool_name": "mcp__ringover__sms_send", "tool_input": ti,
                           "tool_response": {"message_id": 999, "conversation_id": 42, "is_failed": False}})
        self.assertEqual(self.status(mid), "SUBMITTED")
        self.assertEqual(self.decision(self.send(mid)[0]), "deny")

    def test_same_text_in_new_batch_is_blocked_after_send(self):
        if not in_window("+12143210101"):
            self.skipTest("Dallas sending window closed right now")
        bid = self.real_batch()
        res, ti = self.send(f"{bid}-01")
        self.hook("post", {"tool_name": "mcp__ringover__sms_send", "tool_input": ti,
                           "tool_response": {"message_id": 1}})
        bid2 = self.real_batch()
        res, _ = self.send(f"{bid2}-01")
        self.assertEqual(self.decision(res), "deny")
        self.assertIn("identical text already sent", self.reason(res))

    def test_restore_never_restores_approvals_and_keeps_sent(self):
        bid, _ = self.batch([rec(), rec("B Fictional", "214-555-0102")])
        code = self.present(bid)
        self.say(f"approve {code}")
        c = self.conn()
        L.update_msg(c, f"{bid}-02", status="SUBMITTED", submitted_at=L.iso(L.utcnow()))
        export = json.loads(self.cli("sync-export"))
        # Google Sheets turns "+12145550102" into 12145550102; restore must undo that
        ph = export["messages_header"].index("phone")
        export["messages"] = [[(str(v).lstrip("+") if i == ph else ("" if v is None else str(v)))
                               for i, v in enumerate(r)] for r in export["messages"]]
        f = os.path.join(self.tmp.name, "restore.json")
        Path(f).write_text(json.dumps({"messages": export["messages"], "optouts": export["optouts"]}))
        os.remove(self.db)
        self.cli("restore", f)
        self.assertEqual(self.status(f"{bid}-01"), "DRAFT")
        self.assertEqual(self.status(f"{bid}-02"), "SUBMITTED")
        self.assertEqual(L.get_msg(self.conn(), f"{bid}-02")["phone"], "+12145550102")

    def test_unready_ledger_blocks_everything(self):
        os.remove(self.db)
        res = self.hook("pre", {"tool_name": "mcp__ringover__sms_send",
                                "tool_input": {"from_number": FROM, "to_number": "+12143210101", "content": "x"}})
        self.assertEqual(self.decision(res), "deny")
        self.assertIn("not initialised", self.reason(res))


class TestTamperGuard(Base):
    def test_bash_and_edit_access_to_ledger_blocked(self):
        for cmd in ("sqlite3 sms_data/ledger.db 'update messages set status=\"APPROVED\"'",
                    "echo '{}' | python3 .claude/skills/westmont-sms-outreach/scripts/sms_guard.py prompt",
                    "python3 -c 'import smslib'"):
            res = self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": cmd}})
            self.assertEqual(self.decision(res), "deny", cmd)
        res = self.hook("pre", {"tool_name": "Write", "tool_input": {"file_path": "/x/sms_data/ledger.db"}})
        self.assertEqual(self.decision(res), "deny")
        res = self.hook("pre", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertIsNone(res)


class TestSegments(unittest.TestCase):
    def test_segments(self):
        self.assertEqual(L.segment_info("a" * 160)["segments"], 1)
        self.assertEqual(L.segment_info("a" * 161)["segments"], 2)
        self.assertEqual(L.segment_info("café \U0001F600")["encoding"], "UCS-2")
        t, changed = L.clean_text("It’s great — thanks")
        self.assertEqual(t, "It's great - thanks")
        self.assertEqual(L.segment_info(t)["encoding"], "GSM-7")


if __name__ == "__main__":
    unittest.main(verbosity=2)
