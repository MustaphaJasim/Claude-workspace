"""Westmont SMS Outreach - legal classification of client (B2B) texts.

Every client text is classified before Mustapha can approve it, and again at
send time:

  PERMITTED        no rule found that restricts it, all required checks done
  EXEMPT           a restriction applies but a recorded exemption covers it
  REVIEW_REQUIRED  facts missing or law unsettled: blocked until the gap is
                   filled or a legal clearance is recorded
  PROHIBITED       a rule forbids it on the recorded facts: blocked

This is a rules engine built from the research in reference/legal-basis.md
(2026-10-09). It is not legal advice. Anything not positively verified goes
to REVIEW_REQUIRED, never to PERMITTED.

Assumption built into every rule: messages are typed/approved one at a time
and sent individually through Ringover (no autodialer, no bulk campaign
tool). If that ever changes, the Florida/Oklahoma/Maryland rows and the
federal TCPA consent analysis change too.
"""

import json
from datetime import datetime, timedelta, timezone

PERMITTED, EXEMPT = "PERMITTED", "EXEMPT"
REVIEW, PROHIBITED = "REVIEW_REQUIRED", "PROHIBITED"
SENDABLE = {PERMITTED, EXEMPT}
_RANK = {PERMITTED: 0, EXEMPT: 1, REVIEW: 2, PROHIBITED: 3}

RELATIONSHIPS = {
    "current_client": "has bought Westmont services (engagement/placement) - date = last transaction",
    "former_client": "bought Westmont services before - date = last transaction",
    "inquiry": "asked Westmont about its services (e.g. asked to see resumes) - date = inquiry",
    "written_consent": "signed/written permission to be texted by Westmont at this number - date = consent",
    "prior_conversation": "has spoken with Westmont but no purchase/inquiry/written consent",
    "none": "cold - no prior relationship",
}
LINE_USE = {"business", "mixed", "personal", "unknown"}
DNC = {"not_registered", "registered", "not_checked"}
DNC_MAX_AGE_DAYS = 31          # FCC: scrub against the National DNC Registry at least every 31 days
EBR_PURCHASE_MONTHS = 18       # 47 CFR 64.1200(f)(5)
EBR_INQUIRY_MONTHS = 3
COLD_MAX_PER_30_DAYS = 2       # Westmont policy (not law) for texts without a relationship

# Area code -> state (used to decide which state laws apply).
_STATE_CODES = {
    "AL": "205 251 256 334 483 659 938", "AK": "907", "AZ": "480 520 602 623 928",
    "AR": "327 479 501 870",
    "CA": "209 213 279 310 323 341 350 369 408 415 424 442 510 530 559 562 619 626 628 650 657 661 "
          "669 707 714 747 760 805 818 820 831 840 858 909 916 925 949 951",
    "CO": "303 719 720 970 983", "CT": "203 475 860 959", "DE": "302", "DC": "202 771",
    "FL": "239 305 321 324 352 386 407 448 561 645 656 689 727 728 754 772 786 813 850 863 904 941 954",
    "GA": "229 404 470 478 678 706 762 770 912 943", "HI": "808", "ID": "208 986",
    "IL": "217 224 309 312 331 447 464 618 630 708 730 773 779 815 847 861 872",
    "IN": "219 260 317 463 574 765 812 930", "IA": "319 515 563 641 712", "KS": "316 620 785 913",
    "KY": "270 364 502 606 859", "LA": "225 318 337 457 504 985", "ME": "207",
    "MD": "227 240 301 410 443 667", "MA": "339 351 413 508 617 774 781 857 978",
    "MI": "231 248 269 313 517 586 616 679 734 810 906 947 989",
    "MN": "218 320 507 612 651 763 924 952", "MS": "228 601 662 769",
    "MO": "235 314 417 557 573 636 660 816 975", "MT": "406", "NE": "308 402 531",
    "NV": "702 725 775", "NH": "603", "NJ": "201 551 609 640 732 848 856 862 908 973",
    "NM": "505 575",
    "NY": "212 315 329 332 347 363 516 518 585 607 624 631 646 680 716 718 838 845 914 917 929 934",
    "NC": "252 336 472 704 743 828 910 919 980 984", "ND": "701",
    "OH": "216 220 234 283 326 330 380 419 436 440 513 567 614 740 937",
    "OK": "405 539 572 580 918", "OR": "458 503 541 971",
    "PA": "215 223 267 272 412 445 484 570 582 610 717 724 814 835 878", "RI": "401",
    "SC": "803 839 843 854 864", "SD": "605", "TN": "423 615 629 731 865 901 931",
    "TX": "210 214 254 281 325 346 361 409 430 432 469 512 682 713 726 737 806 817 830 832 903 915 "
          "936 940 945 956 972 979",
    "UT": "385 435 801", "VT": "802", "VA": "276 434 540 571 686 703 757 804 826 948",
    "WA": "206 253 360 425 509 564", "WV": "304 681", "WI": "262 274 353 414 534 608 715 920",
    "WY": "307", "PR": "787 939",
}
AREA_STATE = {c: s for s, codes in _STATE_CODES.items() for c in codes.split()}

# Global clearance keys Mustapha can record (typed by him; see sms_guard.py)
GLOBAL_CLEARANCES = {
    "tx302-registered": "Westmont holds a current Texas Ch. 302 telephone-solicitation registration certificate",
    "tx302-customer-exemption": "Westmont has operated under the same business name for 2+ years (Tex. Bus. & Com. Code 302.058)",
    "carrier-cold-b2b": "Ringover confirmed the 10DLC campaign registration covers B2B outreach without prior opt-in",
}
# state-XX: counsel has cleared cold/relationship B2B texting into state XX
# msg-<ID>: counsel has cleared one specific message version (resolves REVIEW only)


def _d(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def facts_of(m):
    try:
        return json.loads(m["compliance_json"] or "{}")
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def states_for(phone, facts):
    states = []
    s = AREA_STATE.get(phone[2:5])
    if s:
        states.append(s)
    for extra in facts.get("states") or []:
        extra = str(extra).upper().strip()
        if extra and extra not in states:
            states.append(extra)
    return states


def active_clearances(conn, now):
    rows = conn.execute("SELECT * FROM clearances WHERE revoked_at IS NULL").fetchall()
    out = {}
    for r in rows:
        exp = _d(r["expires_at"])
        if exp and exp < now:
            continue
        out[r["key"]] = r
    return out


def assess(conn, m, now, msg_hash=None):
    """Classify one message. Returns dict(status, reasons, notes, exemptions)."""
    if m["audience"] != "client":
        return _assess_candidate(m)

    f = facts_of(m)
    clear = active_clearances(conn, now)
    res = {"status": PERMITTED, "reasons": [], "exemptions": [], "notes": []}

    def bump(level, why):
        if _RANK[level] > _RANK[res["status"]]:
            res["status"] = level
        (res["exemptions"] if level == EXEMPT else res["reasons"]).append(why)

    rel = f.get("relationship") or m["consent_basis"] or "none"
    rel_date = _d(f.get("relationship_date"))
    line_use = f.get("line_use") or "unknown"
    dnc = f.get("dnc_status") or "not_checked"
    dnc_date = _d(f.get("dnc_checked_on"))
    kind = "client_service" if m["purpose"] == "client_service" else "solicitation"

    if rel not in RELATIONSHIPS:
        bump(REVIEW, f"relationship '{rel}' not recognised")
        rel = "none"
    if rel in ("current_client", "former_client", "inquiry", "written_consent") and not rel_date:
        bump(REVIEW, f"{rel}: date of the transaction/inquiry/consent not recorded")

    # ---- client-service messages (not marketing) ----
    if kind == "client_service":
        if rel != "current_client":
            bump(REVIEW, "'client_service' is only for live engagements with a current client;"
                         " anything else is a solicitation")
        else:
            res["notes"].append("service message on a live engagement - not a telephone solicitation")
        return _finish(res, conn, m, clear, msg_hash)

    # ---- federal: TCPA / FCC Do-Not-Call (47 CFR 64.1200(c),(e)) ----
    ebr = False
    if rel in ("current_client", "former_client") and rel_date and \
            now - rel_date <= timedelta(days=30.5 * EBR_PURCHASE_MONTHS):
        ebr = True
        bump(EXEMPT, "federal DNC: established business relationship (purchase within 18 months)")
    elif rel == "inquiry" and rel_date and now - rel_date <= timedelta(days=30.5 * EBR_INQUIRY_MONTHS):
        ebr = True
        bump(EXEMPT, "federal DNC: established business relationship (inquiry within 3 months)")
    elif rel == "written_consent" and rel_date:
        ebr = True
        bump(EXEMPT, "federal DNC: written permission on file")
    if not ebr:
        if dnc == "not_checked" or not dnc_date:
            bump(REVIEW, "National DNC Registry not checked for this number (needed when there is no"
                         " recent purchase/inquiry/written consent)")
        elif now - dnc_date > timedelta(days=DNC_MAX_AGE_DAYS):
            bump(REVIEW, f"National DNC check is older than {DNC_MAX_AGE_DAYS} days; re-scrub")
        elif dnc == "registered":
            if line_use == "business":
                bump(REVIEW, "number is on the National DNC Registry; courts disagree whether a"
                             " business-used mobile is protected - legal review")
            else:
                bump(PROHIBITED, f"number is on the National DNC Registry ({line_use} line) and there is"
                                 " no established business relationship or written permission")
        else:
            res["notes"].append("federal: not on National DNC (checked "
                                f"{f.get('dnc_checked_on')}); manual 1:1 send, so TCPA autodialer"
                                " consent rules do not apply")

    # ---- states ----
    states = states_for(m["phone"], f)
    if not states:
        bump(REVIEW, "recipient state unknown")
    for st in states:
        _state_rule(st, rel, rel_date, ebr, line_use, clear, bump, res, now)

    # ---- carrier (not law): 10DLC registration terms ----
    if not ebr and "carrier-cold-b2b" not in clear:
        bump(REVIEW, "carrier rules (not law): confirm with Ringover that your 10DLC campaign"
                     " allows B2B texts without prior opt-in, then record clearance carrier-cold-b2b")

    return _finish(res, conn, m, clear, msg_hash)


def _state_rule(st, rel, rel_date, ebr, line_use, clear, bump, res, now):
    cleared = f"state-{st}" in clear
    if st == "WA":
        # RCW 19.190.060: no commercial text to a WA resident's mobile without clear,
        # affirmative advance consent. No B2B exemption.
        if rel == "written_consent":
            bump(EXEMPT, "Washington CEMA: clear advance consent on file")
        else:
            bump(PROHIBITED, "Washington (RCW 19.190.060): commercial texts to WA mobiles need clear"
                             " advance consent; no business-to-business exemption")
        return
    if st == "TX":
        # Tex. Bus. & Com. Code ch. 302 (texts included since SB 140, eff. 2025-09-01):
        # registration + $10,000 security unless exempt. No general B2B exemption.
        if "tx302-registered" in clear:
            bump(EXEMPT, "Texas ch. 302: Westmont registration certificate recorded")
        elif rel in ("current_client", "former_client") and "tx302-customer-exemption" in clear:
            bump(EXEMPT, "Texas 302.058: current/former customer + same business name 2+ years")
        elif cleared:
            bump(EXEMPT, "Texas: counsel clearance recorded (state-TX)")
        else:
            why = ("Texas ch. 302 applies to texts sent to induce a purchase. Needs Westmont's"
                   " registration (clearance tx302-registered)")
            if rel in ("current_client", "former_client"):
                why += " or confirmation of the 302.058 customer exemption (tx302-customer-exemption)"
            bump(REVIEW, why)
        res["notes"].append("Texas hours (ch. 301): Mon-Sat 9am-9pm, Sun noon-9pm - inside our window")
        return
    if st in ("FL", "OK", "MD"):
        # Mini-TCPAs whose consent rules cover automated systems (FL also only
        # consumer goods/services). Manual 1:1 B2B texts fall outside them.
        res["notes"].append(f"{st}: state mini-TCPA covers automated dialing systems; manual 1:1 B2B text"
                            " is outside it")
        if line_use == "personal" and not cleared:
            bump(REVIEW, f"{st}: personal mobile - consumer-protection rules may apply; legal review")
        return
    if st == "CT":
        # CT telemarketing law covers live and text messages but exempts B2B contacts.
        if line_use in ("business", "mixed") or ebr or cleared:
            res["notes"].append("CT: business-to-business contact exemption")
        else:
            bump(REVIEW, "Connecticut: B2B exemption depends on the number being used for business;"
                         " confirm line use or get legal review")
        return
    if cleared:
        bump(EXEMPT, f"{st}: counsel clearance recorded (state-{st})")
        return
    bump(REVIEW, f"{st}: state telemarketing/text rules not yet verified for B2B texts; legal review"
                 f" (record clearance state-{st} once counsel confirms)")


def _finish(res, conn, m, clear, msg_hash):
    key = f"msg-{m['msg_id']}"
    c = clear.get(key)
    if res["status"] == REVIEW and c is not None and msg_hash and c["bound_hash"] == msg_hash:
        res["status"] = EXEMPT
        res["exemptions"].append(f"legal clearance for this exact message: {c['evidence']}")
    if res["status"] == PERMITTED and res["exemptions"]:
        res["status"] = EXEMPT
    return res


def _assess_candidate(m):
    # Recruiting a candidate for a job is not a telephone solicitation (nothing is
    # sold to them); TCPA autodialer consent rules do not apply to manual 1:1 sends.
    # Carrier/CTIA expectations are met by the identification + STOP rules.
    if m["consent_basis"] == "unknown":
        return {"status": REVIEW, "reasons": ["candidate consent basis unknown"], "exemptions": [], "notes": []}
    return {"status": PERMITTED, "reasons": [], "exemptions": [],
            "notes": ["recruiting message to a candidate - not a sales solicitation"]}


def short(res):
    if res["status"] in SENDABLE:
        return res["status"]
    return res["status"] + ": " + "; ".join(res["reasons"])
