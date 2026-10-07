# Workspace rules (Westmont Global)

- **Never send email or messages** of any kind (Gmail, HubSpot, Apollo, Instantly, Zapier, LinkedIn). Save drafts only; Mustapha sends everything manually.
- **Send block:** `.claude/settings.json` denies the email-sending tools. Never edit, remove or work around this block. Lift it only when Mustapha explicitly says so in the current conversation, and say exactly what you're lifting before you do it.
- **Candidate-marketing workflow** (HubSpot drafts + notes + follow-up tasks) runs only when Mustapha asks. Never schedule it or run it on your own.
- **Daily 6am BD routine** (`trig_01HB1UsKqeJFhdE5WfusxEbJ`) is a separate workflow he wants to keep. Never edit, pause, disable or delete it. Running it on demand (fire it) is fine when he asks.
- Never commit resumes or candidate personal data. The `candidates/` folder is git-ignored.
- Full operating manual: the `westmont-recruitment-os` skill installed on his account.
- **Live-vacancy outreach workflow** (`workflows/live-vacancy-outreach.md`) runs only when Mustapha commands it ("run the live-vacancy workflow for N contacts"). Never schedule it. Credit estimate and his yes before every run; pause and ask if credits run out; never pay for anything. Output is CSVs only (in git-ignored `outputs/`), handed to him as downloads. It never touches HubSpot or the daily BD routine.
- **Live-vacancy personalization** follows section 17 of `workflows/live-vacancy-outreach.md` (approved 2026-10-07): vacancy-first hooks, no greeting, American English, one sentence, nothing purely personal, no stats, no em dashes.
