# Follow-ups: a follow-up assistant for Claude Code

A Claude Code mod for small businesses. Press **Run now** and Claude:

1. reads **QuickBooks** (open quotes, overdue invoices, customers) through Intuit's QuickBooks connector, or through the QuickBooks reports in your own Chrome
2. reads your **mail** to see who spoke last
3. picks who needs a follow-up, and why, using plain rules written in code
4. drafts each email in **your voice**

You review the drafts in the **Follow-ups** pane: **Approve**, **Ask for a change**, **Skip** or **Snooze**. Approved emails land in your **Gmail Drafts**, inside the right conversation, and you press Send there.

Everything is kept in a small database on your computer (`followup.db`). You can see it as:
- the pane's tabs: **Today**, **Pipeline**, **Sent** and **Customers**
- **Follow-ups.xlsx**, which opens in Excel or Numbers
- **Follow-ups.html**, an offline dashboard that opens in any browser

All three are refreshed after every change.

## What it never does

- **It never sends, replies to or forwards an email.** A guard in the mod blocks those tools in this workspace. You press Send yourself.
- **It never changes QuickBooks.** In a follow-up folder, Claude doesn't even see the QuickBooks tools that could create, edit, delete, send or import anything: the mod hides them and refuses any call of one. The Chrome path reads report pages only.
- **It never invents a fact.** A code check rejects any draft whose amount, invoice number, date, day count or recipient doesn't match QuickBooks and your mail.
- **It never nags.** At most 7 a day. A skip hides a customer for 14 days. Do-not-contact is permanent.
- **It never signs in for you or types passwords.** You connect QuickBooks and Gmail yourself, in Claude's settings.
- **It doesn't treat text in emails or QuickBooks memos as instructions.** That text is data.

## What you need

- The Claude desktop app with Claude Code, on a Claude plan (a Team plan is recommended for business data).
- The **Gmail** connector (Microsoft 365 for Outlook: search only, so drafts go through Outlook on the web).
- The **Intuit QuickBooks** connector, or the **Claude in Chrome** extension signed in to QuickBooks Online.
- Python 3 (built in on macOS; on Windows, install it from python.org).

## Install

You need the Claude desktop app with Claude Code. Install the mod once, from a terminal:

```
claude plugin marketplace add mahdiCalraid/followup-mod
claude plugin install followup@followup-mod
```

Or type the same two lines, with `/plugin` in place of `claude plugin`, into a Claude Code session.

Then:
1. Make an empty folder for the business, for example `Documents/Follow-ups`.
2. In the Claude app, open a **new Code session in that folder** and type `/followup setup`.

To see it with fictional data first, open a session in another empty folder and type `/followup demo` (Claude drafts live) or `/followup demo instant` (drafts ready at once).

**Updates:** `claude plugin marketplace update followup-mod`, then `claude plugin update followup@followup-mod`. Then type `/followup update` in the business folder. Your settings, voice and history are kept.

## Connect QuickBooks safely

Type `/followup connect`, or press **Connect QuickBooks** in the pane. Claude walks you through it:

1. In Claude, open **Customize → Connectors** (or claude.ai/customize/connectors), find **Intuit QuickBooks** (made by Intuit) and press **Connect**.
2. Intuit's own sign-in page opens. Check that the address ends in **intuit.com**, sign in, pick your company and approve. Claude never sees your password.
3. Start a new Code session in your folder and type `/followup connect` again. Claude reads the company name and counts your customers, open quotes and unpaid invoices, so you can check it's the right company.

What keeps it safe:
- **Intuit's official connector**, with Intuit's sign-in. Disconnect it any time in Claude's settings or in your Intuit account.
- **Read-only in your follow-up folder.** The mod hides every QuickBooks tool that could change your books (create, edit, delete, send, import, record payments...), so Claude can't call them. It also refuses any call that gets through. The pane shows **QuickBooks: connected, read-only here**, with how many tools are hidden.
- **Not "QuickBooks Labs".** That beta connector can send invoices and change records. Use the one by Intuit.

Don't want to connect it? Claude can read the QuickBooks reports in your own Chrome instead (with the Claude in Chrome extension), or you can drop exported reports into the folder's `imports/`.

## Commands

| Command | Does |
|---|---|
| `/followup` | opens the pane |
| `/followup setup` | prerequisites check and a short interview (voice, rules, do-not-contact) |
| `/followup connect` | connects QuickBooks, read-only, and tests one read |
| `/followup run` | reads QuickBooks and mail, ranks, drafts |
| `/followup stage` | puts approved drafts into Gmail |
| `/followup check` | marks sent drafts and finds replies |
| `/followup export` | rewrites and opens the spreadsheet |
| `/followup demo [instant]` | loads the fictional Brightwater Bakery Co. data |
| `/followup update` | refreshes the instructions; your config, voice and history are kept |

## The rules (all adjustable in `followup_mod/config.json`)

| Follow-up | When |
|---|---|
| Overdue invoice | the due date has passed and there's a balance. 1–14 days late: friendly. 15–30: second reminder. 31+: firm, asks for a payment date |
| Waiting on our reply | they wrote last, more than 21 days ago |
| Quote gone quiet | a Pending estimate, 5 to 90 days without contact |
| Proposal, no answer | we sent a quote by email and heard nothing for 21+ days |
| Past customer gone quiet | no invoice or mail for 6+ months |

There is one follow-up per customer per run. Customers with no email go on the phone list.

## For developers

- `docs/CONTRACT.md`: the database schema, the CLI, the JSON shapes and the rules
- `engine/`: the instructions Claude follows (`RUN`, `DRAFT`, `STAGE`, `CHECK`, `SETUP`, `CONNECT`, `STATE`) and `scripts/fu.py` (standard-library Python and SQLite)
- `hooks/register.tsx`: the pane, the commands and the send/QuickBooks-write guard

Run the tests:

```
python3 -m unittest discover -s engine/tests
```

```
claude plugin test .
```
