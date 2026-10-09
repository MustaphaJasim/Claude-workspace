# Legal basis for the SMS rules (B2B client texts)

Researched 2026-10-09 by Claude for Westmont Global. **This is not legal advice.** It
explains what the rules engine (`scripts/compliance.py`) does and why. Anything not
positively verified is set to REVIEW_REQUIRED (blocked until counsel clears it),
never to PERMITTED. Re-check before relying on it. Laws in this area change often.

## Working assumptions

- Every text is written, reviewed and sent **one at a time** through Ringover's
  standard send. No autodialer, no bulk "SMS campaign" tool, no scheduling.
  Several conclusions below depend on this.
- A text to a hiring manager offering Westmont's recruitment services is a
  **telephone solicitation**. It is sent to encourage the purchase of a service
  (47 U.S.C. 227(a)(4); Tex. Bus. & Com. Code 302.001(7)).
- A text to a **candidate** about a job is **not** a solicitation, because nothing is
  sold to them. Candidate rules are unchanged (identify Westmont + STOP on the first
  text, opt-outs, hours).

## Federal

| Rule | What it means for B2B texts | How the engine handles it |
|---|---|---|
| TCPA 227(b) autodialer/prerecorded consent | Since *Facebook v. Duguid* (2021) an "autodialer" must use a random/sequential number generator. Manual 1:1 sends are outside it, so no prior express (written) consent is required **for that rule**. | Assumed not to apply (manual sends only). |
| National Do-Not-Call Registry (47 CFR 64.1200(c)(2), (e)) | The FCC codified (Dec 2023, FCC 23-107) that DNC protections apply to **marketing texts**. Texting a number on the Registry needs signed written permission or an **established business relationship (EBR)**. | DNC scrub required unless there is an EBR/written consent; scrub must be ≤31 days old. |
| EBR definition (64.1200(f)(5)) | Purchase/transaction within **18 months**, or inquiry/application within **3 months**, not terminated. | `current_client`/`former_client` within 18 months, or `inquiry` within 3 months, gives EBR. |
| DNC and business mobiles | The Registry protects "residential subscribers". Courts disagree on business or mixed-use mobiles (and some on mobiles at all, e.g. *Loudermilk v. Maelys*). | On Registry + business line: REVIEW. On Registry + personal/mixed/unknown and no EBR: PROHIBITED. |
| Company-specific do-not-call (64.1200(d)) | Keep an internal do-not-call list, honor opt-outs (FCC 2024 rule: within 10 business days, by any reasonable means), have a written policy, and identify the caller and business. | Opt-outs block immediately. Every client solicitation must name Mustapha + Westmont and include STOP. Written policy: `reference/do-not-text-policy.md`. |
| Calling hours (64.1200(c)(1)) | 8am–9pm recipient local time. | Our window is stricter: Mon–Fri 9am–7pm. |

## Texas (Westmont's main market)

**Chapter 302, Regulation of Telephone Solicitation.** SB 140, effective 2025-09-01,
amended the definition (302.001(7)) to cover "a transmission of a text or graphic
message". A seller may not make a telephone solicitation **to a purchaser located in
Texas** without a registration certificate (302.101). Registration means a $200 fee,
$10,000 security and periodic reports. SB 140 also made violations actionable under the
Texas DTPA (private lawsuits).

The exemptions, checked against the statute text:
- 302.056 commercial sales: **only** where the business buys to resell or for
  manufacturing/recycling. It does **not** cover recruitment services. There is **no
  general B2B exemption**.
- 302.058: soliciting a **former or current customer**, if the seller has operated under
  the same business name for **at least two years**. "Customer" is undefined
  (Polsinelli, 2025).
- 302.059(1): no sale completed on the call, and a major sales presentation arranged
  **face-to-face** later. This is a poor fit for remote recruiting, so the engine doesn't rely on it.
- 302.061: an isolated transaction, not part of a repeated pattern. An outreach programme
  doesn't qualify.

Engine: every Texas client solicitation is REVIEW until one of these is recorded:
- `clearance tx302-registered`, or
- for current/former clients only, `clearance tx302-customer-exemption` (the 2-year name
  confirmation), or
- `clearance state-TX` from counsel.

Also: **Ch. 301** hours (Mon–Sat 9am–9pm, Sun noon–9pm) are inside our window. **Ch. 304**
(Texas no-call) and **Ch. 305** (state TCPA action, now also DTPA) were noted. Ch. 304
targets consumer goods/services and is believed not to fit B2B recruitment services, but
that wasn't verified, so it's flagged for counsel in the review memo.

## Other states (verified so far)

| State | Finding | Engine |
|---|---|---|
| Washington | RCW 19.190.060: no commercial text to a WA resident's mobile without clear, affirmative advance consent. **No B2B exemption.** Private action $500/text. | PROHIBITED unless written consent |
| Florida | FTSA consent rule covers automated selection/dialing systems and consumer goods/services. STOP replies must be honored within 15 days. | Manual B2B: allowed. Personal mobile: REVIEW |
| Oklahoma | OTSA consent rule covers automated systems. | Same as Florida |
| Maryland | Stop the Spam Calls Act covers automated systems. B2B exemption for sellers operating 3+ years. | Same as Florida |
| Connecticut | Covers live calls and texts, but exempts business-to-business contacts. | Business/mixed line: allowed. Otherwise REVIEW |
| **All other states** | **Not yet verified** (state telemarketing/registration laws vary). | REVIEW until `clearance state-XX` |

A recipient's state comes from the area code **plus** any company/HubSpot state you
supply. If they differ, both states' rules apply.

## Carrier rules (not law, but they decide whether texts get delivered)

US 10DLC registration (TCR via Ringover) describes the campaign and how recipients
opted in. Cold outreach outside the registered use case can get the number filtered or
suspended. Cold texts (no EBR/consent) need `clearance carrier-cold-b2b`, recorded
after Ringover confirms in writing that your campaign covers it.

## Westmont policy limits (not law)

- At most 2 texts per 30 days to a contact with no business relationship.
- No two texts to the same number within 24h (replies excepted).
- Mon–Fri 9am–7pm recipient time.
- The first text to any number names Westmont and includes STOP. Every client
  solicitation also names Mustapha.

## Sources

- FCC 23-107 / DA 24-910 (DNC rules apply to texts): https://docs.fcc.gov/public/attachments/DA-24-910A1.pdf
- 47 CFR 64.1200: https://www.law.cornell.edu/cfr/text/47/64.1200
- Texas SB 140 enrolled text: https://capitol.texas.gov/tlodocs/89R/billtext/html/SB00140F.htm
- Tex. Bus. & Com. Code ch. 302 (302.001, 302.056, 302.058, 302.059, 302.061): https://texas.public.law/statutes/tex._bus._%26_com._code_title_10_subtitle_a_chapter_302
- Bradley Arant, "Texas SB 140 and Text Marketing" (JD Supra); Polsinelli, "Texas Expands Its Mini-TCPA" (JD Supra)
- RCW 19.190.060: https://app.leg.wa.gov/rcw/default.aspx?cite=19.190.060
- McDermott Will & Schulte, "2024 Updates to State Mini-TCPA Laws" (FL, OK, MD, CT, NY, WA)
- Fla. Stat. 501.059: https://florida.public.law/statutes/fla._stat._501.059

## Questions to put to counsel

1. Does Westmont Global Ltd (UK) need Texas ch. 302 registration for B2B recruitment-services
   texts to Texas hiring managers? Does any exemption apply (302.058 for past clients;
   302.059 for remote meetings)?
2. Are hiring managers' business mobiles on the National DNC Registry protected? What
   evidence of "business use" is enough?
3. Does Texas ch. 304 apply to B2B recruitment-services texts?
4. Which other target states (list them) restrict manual B2B texts?
5. Does the inquiry-based EBR cover a prospect who agreed on a call to review resumes?
