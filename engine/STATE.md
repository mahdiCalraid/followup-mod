# Files, states and words (reference for Claude)

The engine (`followup_mod/scripts/fu.py`) owns `followup.db`, `views/pane.json`, `Follow-ups.xlsx` and `Follow-ups.html`. You only write the two snapshot files below and the draft bodies, then load them through `fu.py`.

## 1. `snapshots/<run_id>/quickbooks.json`

```json
{
  "schema": "followup-qb/1",
  "captured_at": "2026-11-25T14:02:00Z",
  "source": "connector",
  "customers": [{"qb_id": "58", "name": "Dana Ruiz", "company": "Copper Kettle Cafe", "email": "dana@copperkettle.com", "phone": "+1 555 0101", "type": "Wholesale", "notes": ""}],
  "estimates": [{"number": "1031", "customer_qb_id": "58", "customer_name": "Copper Kettle Cafe", "date": "2026-11-18", "expiry_date": null, "amount": 1840.00, "status": "Pending", "memo": ""}],
  "invoices":  [{"number": "1042", "customer_qb_id": "58", "customer_name": "Copper Kettle Cafe", "date": "2026-09-01", "due_date": "2026-10-01", "amount": 2400.00, "balance": 2400.00, "status": "Open", "memo": ""}],
  "report_totals": {"estimates_pending_amount": 1840.00, "invoices_open_balance": 2400.00}
}
```

- **Estimate status** is one of `Pending`, `Accepted`, `Closed`, `Rejected` or `Converted`. Only Pending estimates count as open.
- **Invoices:** include every invoice with `balance > 0`, plus each customer's paid invoices from the last 24 months (`balance: 0`).
- **Dates** are `YYYY-MM-DD`, or `null` when unknown.
- **Amounts** are plain numbers.
- **`report_totals`** must be the totals **QuickBooks itself shows**, never a sum you computed. Use `null` when QuickBooks showed none.

## 2. `snapshots/<run_id>/mail.json`

```json
{
  "schema": "followup-mail/1",
  "captured_at": "...",
  "source": "gmail",
  "owner_emails": ["orders@yourbusiness.com"],
  "threads": [{"email": "dana@copperkettle.com", "name": "Dana Ruiz", "thread_id": "18c…", "subject": "Holiday order",
               "last_at": "2026-11-20T15:04:00Z", "last_from": "them", "last_message_id": "18c…", "snippet": "…",
               "message_count": 4, "participants": ["dana@copperkettle.com"], "inquiry": false, "url": "https://mail.google.com/…"}]
}
```

Write one entry per counterpart: their latest thread. `last_from` is `them` or `us`. `inquiry: true` only for inbound price, quote or order questions from people who aren't customers.

## 3. Follow-up states

| State | Meaning | Set by |
|---|---|---|
| `found` | flagged by the rules, not drafted yet | `rank` |
| `drafted` | draft passed the checks; waiting for the owner | `draft` |
| `revise` | the owner asked for a change, or answered your questions: redraft | pane |
| `needs_you` | a question or task for the owner | `draft` (2nd failure) / `needs-you` |
| `approved` | the owner approved it: create the Gmail draft | pane |
| `staged` | the draft is in Gmail; the owner presses Send there | `staged` |
| `sent` | found in the Sent folder | `sent` |
| `replied` | they wrote back; the owner records what they said | `replied` |
| `closed` | an outcome was recorded | pane |
| `skipped` | skipped; hidden for 14 days | pane |
| `snoozed` | hidden until `later_date` | pane |
| `call` | no email address: on the phone list | `rank` |

## 4. Kinds

| Kind | Meaning |
|---|---|
| `invoice_overdue` | Overdue invoice. Tiers: `friendly` (1–14 days late), `second` (15–30), `firm` (31+). |
| `unanswered_inbound` | They wrote last and we haven't replied (more than 21 days). |
| `quote_quiet` | A Pending estimate with no contact for 5–90 days. |
| `silent_after_proposal` | We sent a quote or proposal by email and they went quiet (more than 21 days). |
| `dormant_customer` | A past customer with nothing for more than 6 months. |
| `old_conversation` | Any other conversation quiet for more than 21 days. |

All thresholds are in `followup_mod/config.json` → `rules`.
