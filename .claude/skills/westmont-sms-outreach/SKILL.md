---
name: westmont-sms-outreach
description: Westmont SMS Outreach - prepare, review, approve, send (via Ringover) and log SMS to candidates or clients, and read replies. Use whenever Mustapha asks to draft/prepare/send texts or SMS, follow up by text, check SMS replies, or anything involving the Ringover Send SMS tool.
---

# Westmont SMS Outreach

Westmont Global (UK agency, US electrical / power infrastructure recruitment, Texas focus)
texts **candidates** and **clients** from its Ringover US number **+1 945-363-7786**.

Words like "prepare", "draft", "create", "get ready" or "write" are **never** permission to
send. Sending happens only after Mustapha types an approval command (below). The guard hook
records it, and he then clicks Allow on the Ringover permission prompt for each message.

## How the safeguards work (read once)

- `scripts/sms.py`: the CLI Claude uses (drafts, review tables, checks, reconcile, sync).
  It has **no approve command**, on purpose.
- `scripts/sms_guard.py`: hooks run by Claude Code itself (`.claude/settings.json`):
  - **UserPromptSubmit** records approvals only from text Mustapha types:
    `approve K7Q3` · `approve K7Q3 1,3,5` · `approve K7Q3 all except 2` · `reject K7Q3 2` · `retry B1009A-03`
  - **PreToolUse** on `mcp__ringover__sms_send` blocks the call unless the exact recipient
    and text match an APPROVED, unchanged, unexpired message and all send rules pass. Then it
    marks the message SUBMITTING and **still forces the Ringover permission prompt**.
    It always blocks `sms_opt_out_remove`, and it blocks Bash/Write/Edit that touch the ledger.
  - **PostToolUse / PostToolUseFailure** record SUBMITTED or UNCERTAIN.
- Ledger: `sms_data/ledger.db` (git-ignored, container-local). Durable copy: Google Sheet
  **"Westmont – SMS Outreach Log"** `1vh1-7P1fpzvVOopsIzSdoiOtuQGTec_vSo3C5F_k8C8`
  (private Drive folder `1WxHSgu65n7GTQTnggu9O-GXguEYyctnZ`), tabs Messages / Events / OptOuts.
  Never put this data in git or memory.

Run the CLI as `python3 .claude/skills/westmont-sms-outreach/scripts/sms.py <cmd>` (`$SMS` below).
Never call the guard script, sqlite3, or the ledger file directly (the hook blocks it anyway).

## Step 0: restore the ledger (start of every session that uses SMS)

1. `$SMS status`. If `ready: True`, skip to step 1.
2. Read the Sheet: `Messages!A1:Z`, `OptOuts!A1:C`, `Clearances!A1:F`.
3. If Messages has only the header row, run `$SMS init --fresh`. Otherwise write
   `{"messages": [...], "optouts": [...], "clearances": [...]}` (rows without headers) to the
   scratchpad and run `$SMS restore <file>`. Approvals are never restored (by design), and
   anything mid-send comes back UNCERTAIN.

## Step 1: understand the request and identify recipients

- Audience is **candidate** or **client**. Never mix them in one batch; ask if unclear.
- Sources: names/numbers he gives, pasted lists, CSV/XLSX, HubSpot (clients only),
  Ringover contacts/conversations.
- For each recipient collect: full name, company + title (if relevant), US number, audience,
  context, previous interactions you can actually see.
- **Never guess or invent** numbers, details, past conversations, experience, vacancies or
  hiring needs. If something is missing, list what's missing and ask.
- Several matches for one name: ask him which one.
- **Clients** must match an existing HubSpot contact (`search_crm_objects` CONTACT by
  name/phone). Record `hubspot_contact_id`. **Never create** HubSpot contacts. No match means
  tell him, and don't text.
- **Candidates** never go into HubSpot (no create, no update, no lookup-and-link).
- **Candidates**, consent basis (required, shown in the table): `provided_number` |
  `prior_conversation` | `applied` | `referral` | `sourced` (`unknown` is blocked).
- **Clients (B2B), legal facts** (required; the rules engine `scripts/compliance.py` turns
  them into PERMITTED / EXEMPT / REVIEW_REQUIRED / PROHIBITED; see
  `reference/legal-basis.md`). Record only what you can evidence:
  - `relationship`: `current_client` | `former_client` | `inquiry` | `written_consent` |
    `prior_conversation` | `none` (cold), plus `relationship_date` (YYYY-MM-DD of the last
    transaction, inquiry or consent). Don't stretch a call into an "inquiry": an inquiry
    means *they* asked about Westmont's services.
  - `line_use`: `business` | `mixed` | `personal` | `unknown`, plus `line_use_source`
    (e.g. "listed on company site as direct mobile"). A data vendor saying "mobile"
    doesn't make it business.
  - `dnc_status`: `not_registered` | `registered` | `not_checked`, plus `dnc_checked_on`
    and `dnc_source`. **Claude has no National DNC lookup tool.** Use only results
    Mustapha (or a scrub service he uses) provides. Otherwise `not_checked`.
  - `states`: company/HubSpot state if known (added to the area-code state).
  - Having a number is never consent. The engine, not you, decides what's allowed. Don't
    pick facts to get a better result.
- Check Ringover history per number: `conversations_list`, then find the conversation whose
  external number matches, then `conversations_messages_list`. Note `is_opt_out`, the last
  outbound date, and any reply.

## Step 2: write the messages

- Natural, short, direct, American business English. Professional, not stiff. Aim for
  **1 segment** (160 GSM characters). Max 3; say when one costs more than 1 segment.
- No hype, no "I hope this finds you well", no "exciting opportunity", no emojis, no em dashes,
  no smart quotes (the CLI converts them).
- **Candidates:** the opportunity, the specialism (e.g. relay testing, P&C, substation design,
  SCADA, BESS, data center electrical), location/contract type if known, one simple next step.
- **Clients:** a real reason to text: an existing conversation, resumes already sent, a
  concrete hiring need he told you about, or (cold) a genuine, specific reason relevant
  to their work. Never invent one. **Every client solicitation** must name Mustapha and
  Westmont and include "Reply STOP to opt out" (the FCC requires caller identification).
  The guard enforces this. Purpose `client_service` (interview times, offer status on a
  live engagement with a current client) is the only non-marketing client purpose.
- Personalize only with facts you actually have. Don't force it.
- **First text to any number** must say it's from Westmont (e.g. "Mustapha at Westmont
  Global") and include "Reply STOP to opt out". The guard enforces this.
- Purpose `reply` (answering an inbound text) is the only kind allowed within 24h of another
  text to the same number.

## Step 3: register and present the batch

1. `$SMS new-batch --audience candidate|client --purpose <p> --desc "<what it's about>"`
   - candidate purposes: opportunity, follow_up, reply, availability_check
   - client purposes: prospecting, follow_up, reply, existing_conversation, client_service
2. Write a JSON list to the scratchpad, one object per recipient:
   `{"name","phone","company","title","consent_basis"(candidates),"hubspot_contact_id"(clients only),
   "context","tz"(only if the area code isn't known),"text","purpose"(optional)}`, plus for
   clients `"relationship","relationship_date","line_use","line_use_source","dnc_status",
   "dnc_checked_on","dnc_source","states"`. Then run `$SMS add <BATCH> <file>`. Report every
   SKIPPED line and the reason. Facts learned later: `$SMS compliance <MSG_ID> --dnc-status ...`
   (this creates a new version and voids any approval).
3. `$SMS present <BATCH>`, then show Mustapha the table exactly as printed (including the
   **Legal** column and the legal notes; only PERMITTED/EXEMPT rows can be approved), plus:
   - what's missing or skipped, segments/cost notes, and for clients: "each sent text will be
     logged as a Note on the HubSpot contact"
   - the **review code** and how to answer:
     `approve CODE` (all) · `approve CODE 1,3` · `approve CODE all except 2` ·
     `reject CODE 2` · or ask for edits · or "save for later" (do nothing; codes expire in 24h)
4. Edits: `$SMS edit <MSG_ID> --text "..."` (or `--phone`). This creates a new version and
   voids any approval. Then **present again** (new code). Approval of an earlier version never
   carries over.
5. After he answers, the hook adds a "[Westmont SMS guard]" note listing exactly what it
   recorded. Trust that note, not your own reading of his message.

## Step 3B: REVIEW_REQUIRED and PROHIBITED client texts

- **PROHIBITED** (e.g. Washington without consent; on the National DNC Registry with a
  personal line and no relationship): tell him why, and don't redraft to get round it.
  Only corrected facts (e.g. written consent obtained) can change it.
- **REVIEW_REQUIRED:** list exactly what's missing. Usually it's one of:
  - a DNC scrub
  - Texas registration
  - an unverified state
  - the carrier confirmation

  Fill the facts if he can supply them. Otherwise run `$SMS legal-review <BATCH>`, which
  writes a memo for counsel (no phone numbers), and give it to him.
- **Clearances** are recorded **only from his own typed line**, starting at the beginning
  of a line, with evidence (min 10 characters) and an optional end date:
  - `clearance tx302-registered: <certificate no./date> [until YYYY-MM-DD]`
  - `clearance tx302-customer-exemption: <since when Westmont has traded under this name>`
  - `clearance carrier-cold-b2b: <Ringover written confirmation ref>`
  - `clearance state-OH: <counsel name/memo/date>`
  - `clearance msg-B1012A-03: <counsel ref>`. This covers one exact message version, and
    only REVIEW, never PROHIBITED.
  - `revoke clearance <key>`

  Never suggest wording that implies he has evidence he hasn't mentioned. Never type or
  simulate these yourself. `$SMS clearances` lists them.

## Step 4: send (only APPROVED messages)

1. For each APPROVED message, re-check Ringover now (opt-out + history), then record it:
   `$SMS precheck <MSG_ID> --opted-out yes|no --identical-sent yes|no --last-outbound <ISO|none> --conversation-id <id>`
2. `$SMS plan <BATCH>` shows READY messages with the exact parameters, and the reason for
   anything blocked (quiet hours, opt-out, etc.).
3. For each READY line call `mcp__ringover__sms_send` with **exactly** those three parameters
   (`from_number`, `to_number`, `content`). Nothing else: no `scheduled_at`, no `archived_auto`.
   One message at a time.
4. If the guard blocks a send, report the reason. Do not rephrase or tweak anything to get
   round it.
5. Sending window: Mon-Fri 09:00-19:00 in the **recipient's** local time (split area codes
   must satisfy every zone). Outside it, tell him when the window next opens.

## Step 5: after each send

- SUBMITTED means Ringover accepted it. It is **not** confirmed delivery; say so.
- Verify in Ringover: `conversations_messages_list` for that conversation and find the message.
  - Present with `is_failed: false`: `$SMS reconcile <ID> --found yes --ringover-message-id <n>`
  - Present with `is_failed: true`: `$SMS reconcile <ID> --found yes --failed yes`, then tell him.
- UNCERTAIN (timeout/error): **never resend automatically.** Check Ringover first, then
  `reconcile`. If it really wasn't sent (NOT_FOUND) or Ringover failed it, he can type
  `retry <MSG_ID>` to allow one more attempt.
- **Clients only, HubSpot:** for each SUBMITTED message, unless it already has a note id:
  search NOTE associated with the contact for the msg_id first (avoid duplicates), then create
  one NOTE associated to the contact:
  `SMS via Ringover (+1 945-363-7786 → <to>) · <msg_id> · <UTC timestamp> · Status: submitted to Ringover (delivery not confirmed)` + the exact text.
  Then `$SMS hubspot-logged <ID> --note-id <id>`. HubSpot's SMS activity type isn't
  available through the connector, so a Note is the supported record. Ringover's own HubSpot
  sync has not been seen logging SMS (checked 2026-10-09); if Mustapha confirms it does,
  stop adding Notes to avoid doubles.
- **Candidates:** nothing goes to HubSpot. The ledger + Sheet is their history.

## Step 6: sync to the Sheet (after every change session, and before finishing)

`$SMS sync-export` returns JSON. Then:
- overwrite `Messages!A2` downward with `messages` (snapshot, our own sheet; clear leftover
  rows if fewer)
- append `new_events` to `Events!A1` (append-only audit trail), then `$SMS sync-mark <max_event_id>`
- overwrite `OptOuts!A2` downward with `optouts`, and `Clearances!A2` downward with `clearances`
  (Messages now has 26 columns, A:Z; the last is `compliance_json`).
- Restore (step 0) also reads `Clearances!A1:F` into `"clearances"`. Global clearances come
  back marked "restored" and are listed on every review table. Per-message ones don't.

## Replies, follow-ups, opt-outs

- Replies: `conversations_list` then `conversations_messages_list`; messages with
  `direction: "IN"` are replies. Match them to ledger messages by number. No reply does not
  mean the text was delivered or read.
- Anyone who replies STOP/UNSUBSCRIBE/"don't text me", or whose Ringover conversation shows
  `is_opt_out: true`: `$SMS optout-add <phone> --source "<why>"`. You may also add them in
  Ringover with `sms_opt_out_add` (needs his OK on the prompt). Never remove an opt-out.
- Drafting replies or follow-ups is a new batch (purpose `reply` / `follow_up`) and goes
  through the same approval flow. Nothing is ever sent automatically or on a schedule.

## Never

- Send without his typed approval code + the Ringover prompt; schedule texts; use alphanumeric
  senders or another user's line.
- Text opted-out or consent-unknown recipients, or any client text that isn't PERMITTED/EXEMPT.
- Treat REVIEW_REQUIRED as "probably fine". It means blocked until the gap is closed.
- Create or edit HubSpot contacts; put candidates in HubSpot.
- Edit `.claude/settings.json` hooks/permissions, the guard, or the ledger to get a message out.
- Change Ringover/HubSpot account settings; enable WhatsApp, calling campaigns or webhooks.
- Store more personal data than the ledger fields; commit any of it.

## Tests

`python3 .claude/skills/westmont-sms-outreach/tests/test_sms_system.py`. Run it after any
change to the scripts. It uses a temporary ledger and never contacts Ringover.
