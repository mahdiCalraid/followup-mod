# Set up the follow-up workspace (`/followup setup`)

You're setting this up with the business owner, often with Ed from Transition States sitting next to them. Be brief and friendly. Ask in small batches of 2 or 3 questions, and use only what they tell you.

## Ground rules

- Never sign in for them, and never ask for or type passwords, codes or API keys. They connect each connector themselves, in Claude's settings.
- Their data stays in this workspace folder on their computer, and in their own Claude account.
- Write only these owner files: `followup_mod/config.json`, `followup_mod/VOICE.md` and `followup_mod/learned/*`.

## Step 1. Check prerequisites (report each with ✅ or ❌)

1. **Python 3.** Run `python3 followup_mod/scripts/fu.py init`; on Windows try `python` or `py -3`. Write the command that works into `config.json` → `python`.
2. **Mail connector.**
   - Gmail or Google Workspace: check that a Gmail connector's `search_threads` tool is available. Try one harmless search (`newer_than:1d`, `pageSize: 1`, `THREAD_VIEW_IDS_ONLY`).
   - Outlook: check for the Microsoft 365 connector.
   - If it's missing, say: "Open Settings → Connectors, add Gmail (or Microsoft 365), sign in, then tell me." Wait for them.
3. **QuickBooks.** Prefer the **Intuit QuickBooks** connector: test one read, such as company info or a customer search. If it's not available, check that Claude in Chrome is connected and that they're signed in to QuickBooks Online in that Chrome. Set `config.json` → `quickbooks` to `connector` or `chrome`.
4. **Safety check.** Confirm the pane shows **Follow-ups**: that means the guard that blocks sending is active.

## Step 2. Interview

Batch 1, the business:
- The business name, and the name to sign emails with (`business_name`, `sender_name`).
- The email addresses they send from, and any internal domains to ignore (`owner_emails`, `internal_domains`).

Batch 2, the rules (offer the defaults; most people keep them):
- "I flag a quote after 5 days of silence and stop after 90 days. Overdue invoices go friendly at 1–14 days late, a second reminder at 15–30, firm at 31+. Old leads after 21 days. At most 7 a day. Change anything?"
- "Anyone we should never contact?" → `do_not_contact`, as a list of emails.

Batch 3, the voice:
- "How do you usually greet people and sign off? Anything you never say?"
- "Paste one or two follow-up emails you've written before, if you have them." Their real wording is the best guide. Remove any customer names or amounts before saving it into `VOICE.md`.

## Step 3. Write the files

Update `followup_mod/config.json` with their answers. Keep every other key. Fill in `followup_mod/VOICE.md`.

## Step 4. Show it working

Ask: "Shall I do a first run now? It reads QuickBooks and your mail, then shows the follow-ups in the pane. Nothing is sent, and nothing is changed in QuickBooks." If yes, follow `followup_mod/RUN.md`.

## Step 5. Explain, in 5 short lines

- **Run now** (or `/followup run`): Claude checks QuickBooks and your mail and drafts the follow-ups.
- **Today tab**: read each draft, then **Approve**, **Ask for a change**, **Skip** or **Snooze**.
- **Put in Gmail**: approved drafts go into your Gmail Drafts. You press Send there.
- **Check sent & replies**: marks what you sent, and asks you what people answered.
- **Open spreadsheet / dashboard**: every customer, follow-up and reply, kept in `followup.db` on this computer.
