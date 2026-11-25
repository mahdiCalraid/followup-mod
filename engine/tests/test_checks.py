"""Snapshot checks and draft checks ("AI writes, code checks")."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from test_helpers import TODAY, Workspace, ago, cust, est, inv, mail_snapshot, qb_snapshot, thread, ts

import fu_checks

EVIDENCE = {
    "facts": ["Invoice 1048: $500.00 open, due November 13, 2026 — 12 days late."],
    "amounts": [500.0],
    "numbers": ["1048"],
    "dates": ["2026-11-13", "November 13, 2026", "Nov 13, 2026", "November 13", "Nov 13", "2026-10-14"],
    "days": 12,
    "email": "owen@northside.example",
    "participants": ["owen@northside.example", "ap@northside.example"],
}
GOOD_SUBJECT = "Invoice 1048: a friendly reminder"
GOOD_BODY = ("Hi Owen,\n\nJust a friendly reminder that invoice 1048 for $500.00 was due on November 13, 2026, "
             "12 days ago. If it's already on its way, thank you.\n\nThanks,\nBrightwater Bakery Co.\n(555) 010-2030")


def check(subject=GOOD_SUBJECT, body=GOOD_BODY, to="owen@northside.example", evidence=None, tier="friendly", cfg=None):
    return fu_checks.check_draft(subject, body, to, evidence or EVIDENCE, tier, "owen@northside.example", cfg or {})


class DraftCheckTests(unittest.TestCase):
    def test_good_draft_passes(self):
        self.assertEqual(check(), [])

    def test_invented_amount(self):
        problems = check(body=GOOD_BODY.replace("$500.00", "$1,500.00"))
        self.assertEqual(len(problems), 1)
        self.assertIn("$1,500.00", problems[0])
        for text in ("USD 1500", "1,500.00 is open", "$1,500", "750.00 dollars"):
            with self.subTest(text=text):
                self.assertTrue(check(body=GOOD_BODY + "\n" + text), text)

    def test_amount_matches_to_the_cent_in_any_form(self):
        for text in ("$500", "USD 500.00", "500.00", "$ 500.00", "500 dollars"):
            with self.subTest(text=text):
                self.assertEqual(check(body="Hi Owen, %s is open on invoice 1048. Thanks" % text), [])

    def test_wrong_recipient(self):
        problems = check(to="someone@else.example")
        self.assertEqual(len(problems), 1)
        self.assertIn("addressed to someone@else.example", problems[0])

    def test_thread_participant_and_case_are_fine(self):
        self.assertEqual(check(to="AP@Northside.example"), [])

    def test_missing_recipient(self):
        self.assertIn("The draft has no recipient.", check(to=""))

    def test_placeholders(self):
        for text in ("[Name]", "[Insert date]", "{{first_name}}", "<NAME>", "<first name>", "XXX", "TBD", "Lorem ipsum"):
            with self.subTest(text=text):
                problems = check(body=GOOD_BODY + "\n" + text)
                self.assertTrue(any(p.startswith("Placeholder") for p in problems), problems)
        self.assertEqual(check(body=GOOD_BODY + "\n[sent from my phone]"), [])  # no capital letter inside

    def test_banned_phrase(self):
        problems = check(body="I hope this email finds you well.\n" + GOOD_BODY)
        self.assertEqual(problems, ['Banned phrase: "I hope this email finds you well".'])
        self.assertEqual(check(body="Just circling back. " + GOOD_BODY, cfg={"banned_phrases": ["circling back"]}),
                         ['Banned phrase: "circling back".'])

    def test_firm_tier_must_ask_for_a_date(self):
        ev = dict(EVIDENCE, days=45)
        body = "Hi Owen,\n\nInvoice 1048 for $500.00 is now 45 days past due. Please pay it.\n\nThanks"
        self.assertIn('A firm reminder must ask for a payment date (use "when" or "date").',
                      check(body=body, evidence=ev, tier="firm"))
        self.assertEqual(check(body=body + "\nWhen can we expect payment?", evidence=ev, tier="firm"), [])
        self.assertEqual(check(body=body + "\nCould you give me a payment date?", evidence=ev, tier="firm"), [])
        self.assertTrue(check(body=body + "\nAny update?", evidence=ev, tier="firm"))  # "update" is not "date"

    def test_wrong_days_count(self):
        problems = check(body=GOOD_BODY.replace("12 days ago", "30 days ago"))
        self.assertEqual(problems, ['"30 days" doesn\'t match the evidence (12 days).'])
        for n in (11, 12, 13):
            self.assertEqual(check(body=GOOD_BODY.replace("12 days", "%d days" % n)), [], n)

    def test_days_with_no_evidence_count(self):
        ev = dict(EVIDENCE, days=None)
        self.assertTrue(check(body=GOOD_BODY, evidence=ev))

    def test_unknown_document_number(self):
        problems = check(body=GOOD_BODY.replace("invoice 1048", "invoice 1049"))
        self.assertEqual(len(problems), 1)
        self.assertIn("Number 1049", problems[0])

    def test_dates_must_match(self):
        self.assertEqual(check(body=GOOD_BODY.replace("November 13, 2026", "Nov 13")), [])
        self.assertEqual(check(body=GOOD_BODY.replace("November 13, 2026", "11/13/2026")), [])
        self.assertEqual(check(body=GOOD_BODY.replace("November 13, 2026", "2026-11-13")), [])
        self.assertEqual(check(body=GOOD_BODY.replace("November 13, 2026", "13 November 2026")), [])
        for wrong in ("November 14, 2026", "Nov 12", "11/14/2026", "2026-11-14", "November 13, 2025"):
            with self.subTest(wrong=wrong):
                problems = check(body=GOOD_BODY.replace("November 13, 2026", wrong))
                self.assertTrue(any(p.startswith("Date") for p in problems), problems)

    def test_year_exempt_only_inside_a_matching_date(self):
        self.assertEqual(check(body=GOOD_BODY), [])  # 2026 inside "November 13, 2026"
        problems = check(body=GOOD_BODY + "\nBest wishes for 2027!")
        self.assertTrue(any("Number 2027" in p for p in problems), problems)

    def test_phone_numbers_and_emails_are_not_facts(self):
        self.assertEqual(check(body=GOOD_BODY + "\nCall +1 555 0101 or write to orders123@bakery.example"), [])

    def test_length_limits(self):
        self.assertIn("The body is empty.", check(body="   "))
        long_body = GOOD_BODY + "\n" + ("Thanks again. " * 100)
        self.assertTrue(any("characters; the limit is 1,200" in p for p in check(body=long_body)))
        self.assertTrue(any("subject is 121 characters" in p for p in check(subject="x" * 121)))
        self.assertEqual(check(subject="x" * 120), [])
        self.assertEqual(check(body=long_body, cfg={"max_body_chars": 5000}), [])


class SnapshotCheckTests(unittest.TestCase):
    def snap(self):
        return qb_snapshot([cust("58", "Copper Kettle Cafe")], estimates=[est("1031", "58", ago(6), 1840.0)],
                           invoices=[inv("1042", "58", ago(55), 2400.0), inv("1043", "58", ago(5), 0.0, amount=100.0)])

    def test_good_snapshot(self):
        problems, warnings = fu_checks.check_qb_snapshot(self.snap())
        self.assertEqual((problems, warnings), ([], []))

    def test_sum_mismatch_is_an_error(self):
        snap = self.snap()
        snap["report_totals"]["invoices_open_balance"] = 4800.0  # a row was dropped
        problems, _ = fu_checks.check_qb_snapshot(snap)
        self.assertEqual(problems, ["Open invoice balances add up to $2,400.00, but QuickBooks showed $4,800.00: rows may be "
                                    "missing. Re-read the report."])
        snap = self.snap()
        snap["report_totals"]["estimates_pending_amount"] = 1840.009  # within a cent is fine
        self.assertEqual(fu_checks.check_qb_snapshot(snap)[0], [])

    def test_null_total_only_warns(self):
        snap = self.snap()
        snap["report_totals"]["estimates_pending_amount"] = None
        problems, warnings = fu_checks.check_qb_snapshot(snap)
        self.assertEqual(problems, [])
        self.assertTrue(any("no total" in w for w in warnings))

    def test_schema_status_dates_duplicates_and_numbers(self):
        snap = self.snap()
        snap["schema"] = "other/1"
        snap["estimates"][0]["status"] = "Open"
        snap["estimates"].append(dict(snap["estimates"][0], status="Pending"))
        snap["invoices"][0]["due_date"] = "2026-02-30"
        snap["invoices"][1]["balance"] = "0"
        problems, _ = fu_checks.check_qb_snapshot(snap)
        text = " ".join(problems)
        for needle in ("schema", "status 'Open'", "Estimate 1031 appears twice", "2026-02-30", "balance must be a number"):
            self.assertIn(needle, text)

    def test_mail_snapshot_checks(self):
        mail = mail_snapshot([thread("dana@x.example", "t1", "yesterday", "them"), thread("not-an-email", "t2", ts(1), "us"),
                              thread("b@x.example", "t3", ts(1), "maybe")])
        problems, _ = fu_checks.check_mail_snapshot(mail)
        self.assertEqual(len(problems), 3)

    def test_ingest_writes_nothing_on_errors(self):
        w = Workspace()
        try:
            start = w.cli("run-start", today=TODAY)
            snap = self.snap()
            snap["report_totals"]["invoices_open_balance"] = 9999.0
            (Path(start["snapshot_dir"]) / "quickbooks.json").write_text(json.dumps(snap), encoding="utf-8")
            (Path(start["snapshot_dir"]) / "mail.json").write_text(json.dumps(mail_snapshot([thread("a@x.example", "t", ts(1), "us")])),
                                                                     encoding="utf-8")
            out = w.cli("ingest", "--run", start["run_id"], ok=False)
            self.assertTrue(any("rows may be missing" in p for p in out["problems"]))
            conn = w.conn()
            try:
                for table in ("customers", "documents", "threads"):
                    self.assertEqual(conn.execute("SELECT count(*) AS n FROM %s" % table).fetchone()["n"], 0, table)
            finally:
                conn.close()
        finally:
            w.cleanup()

    def test_unknown_customer_reference_adds_a_contactless_customer(self):
        snap = self.snap()
        snap["invoices"].append(dict(inv("1070", "99", ago(20), 50.0), customer_name="Walk-in Co"))
        snap["report_totals"]["invoices_open_balance"] = 2450.0
        problems, warnings = fu_checks.check_qb_snapshot(copy.deepcopy(snap))
        self.assertEqual(problems, [])
        self.assertTrue(any("isn't in the customers list" in w for w in warnings))
        w = Workspace()
        try:
            out = w.run_with(TODAY, snap)
            calls = [f for f in out["followups"] if f["state"] == "call"]
            self.assertEqual([c["customer_name"] for c in calls], ["Walk-in Co"])
        finally:
            w.cleanup()


if __name__ == "__main__":
    unittest.main()
