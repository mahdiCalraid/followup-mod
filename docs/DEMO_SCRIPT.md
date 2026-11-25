# Meeting demo: about 6 minutes

Fictional data: **Brightwater Bakery Co.**, a wholesale bakery with 18 customers. Every email address ends in `.example`, so nothing can be delivered.

## Before the meeting (5 minutes)

1. Install the mod once:
   ```
   claude plugin marketplace add /Users/ed/TS_projects/products/followup_mod
   claude plugin install followup@followup-mod
   ```
2. In the Claude desktop app, open a new Code session in `/Users/ed/TS_projects/followup-demo`.
3. Run `/followup demo instant` and check that the pane fills. Then reset for the meeting:
   ```
   python3 followup_mod/scripts/fu.py reset --yes
   ```
4. Optional: connect the **Intuit QuickBooks** connector to your test company, to show the real path at the end.

## In the meeting

1. **The problem (30 s).** "Every week you have quotes that went quiet, invoices that are late, and people you meant to get back to. The information is in QuickBooks and your inbox. Writing the emails is the chore."
2. **Run (90 s).** Type `/followup demo`. Claude reads the books and the mail, picks who needs a follow-up, and writes the drafts live. While it works, point at the rules in plain words: quotes after 5 quiet days, invoices by how late they are, old leads after 21 days, and at most 7 a day, so you're never buried.
3. **Review (2 min), on the Today tab:**
   - **Pinecrest Hotel:** 45 days late. The draft is firm and asks for a payment date.
   - **Northside Coffee:** partly paid. It asks for the remaining $500.00, not the original $1,500.00.
   - **Maple Street Market:** a new lead nobody answered, so the draft apologises and asks what they need.
   - **Westgate Cafe:** its email contains a fake "ignore your instructions" line. It's flagged, and the draft only asks about the invoice.
   - Press **Ask for a change** on one draft ("shorter, mention our holiday menu") and watch it redraft.
   - **Approve** two drafts. Then **Put in Gmail**: they appear in Gmail Drafts, inside the right thread. "You press Send. It can't."
4. **Your records (1 min).** Open the **Customers** and **Sent** tabs, then **Open spreadsheet** (Excel) and **Open dashboard** (any browser, works offline). "This is a small database on your own computer. Every customer, follow-up and reply is kept."
5. **What it never does (30 s).** It never sends, never changes QuickBooks, never invents a number (code checks every amount and date) and never signs in for you.
6. **Next step (30 s).** "We set it up on your computer in about an hour: connect QuickBooks and Gmail, teach it your voice, then run it on your real data together."

## If something goes wrong

- If the live drafting is slow, use `/followup demo instant`: the drafts are ready at once.
- To reset everything, run `python3 followup_mod/scripts/fu.py reset --yes`, then `/followup demo instant`.
