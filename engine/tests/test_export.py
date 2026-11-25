"""views/pane.json shape, the hand-built xlsx and the offline html dashboard."""
from __future__ import annotations

import json
import re
import sys
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import TODAY, Workspace, ago, cust, inv, mail_snapshot, qb_snapshot, thread, ts  # noqa: E402

import fu  # noqa: E402

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
SHEETS = ["To review", "All follow-ups", "Sent", "Customers", "Open estimates", "Open invoices", "Activity"]
FOLLOWUP_VIEW_KEYS = {"id", "customer_id", "customer_name", "company", "email", "phone", "kind", "kind_label", "reason",
                      "priority", "state", "state_label", "tier", "tier_label", "facts", "draft_subject", "draft_to",
                      "draft_body", "problems", "items", "gmail_draft_url", "thread_url", "staged_at", "sent_at",
                      "replied_at", "reply_snippet", "outcome", "later_date", "updated_at"}
CUSTOMER_VIEW_KEYS = {"id", "name", "company", "email", "phone", "type", "do_not_contact", "open_estimates",
                      "open_estimate_amount", "overdue_balance", "last_contact_at", "last_contact_from",
                      "followups_sent", "last_outcome"}
COUNT_KEYS = {"review", "needs_you", "approved", "staged", "sent", "replied", "call", "customers", "open_estimates",
              "open_estimate_amount", "overdue_invoices", "overdue_balance"}


def cell_text(c):
    t = c.find("m:is/m:t", NS)
    if t is not None:
        return t.text or ""
    v = c.find("m:v", NS)
    return v.text if v is not None else None


class DemoExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = Workspace()
        cls.w.cli("demo", "--with-drafts", today=TODAY)
        cls.out = cls.w.cli("export")

    @classmethod
    def tearDownClass(cls):
        cls.w.cleanup()

    def test_export_returns_paths(self):
        for key in ("pane", "xlsx", "html"):
            self.assertTrue(Path(self.out["views"][key]).exists(), key)
            self.assertEqual(self.out[key], self.out["views"][key])

    def test_pane_shape(self):
        pane = self.w.pane()
        self.assertEqual(set(pane), {"schema", "updated_at", "workspace", "today", "business_name", "last_run", "counts",
                                     "today_items", "pipeline", "sent", "customers", "files"})
        self.assertEqual(pane["schema"], "followup-pane/1")
        self.assertEqual(pane["today"], "2026-11-25")
        self.assertEqual(pane["business_name"], "Brightwater Bakery Co.")
        self.assertEqual(Path(pane["workspace"]), self.w.ws.resolve())
        self.assertEqual(set(pane["counts"]), COUNT_KEYS)
        self.assertEqual(pane["counts"]["review"], 7)
        self.assertEqual(pane["counts"]["call"], 1)
        self.assertEqual(pane["counts"]["overdue_invoices"], 7)
        self.assertEqual(pane["counts"]["overdue_balance"], 8090.75)
        self.assertEqual((pane["counts"]["open_estimates"], pane["counts"]["open_estimate_amount"]), (4, 7550.0))
        self.assertEqual(set(pane["last_run"]) >= {"id", "started_at", "finished_at", "source_qb", "source_mail", "flagged",
                                                   "drafted"}, True)
        self.assertEqual((pane["last_run"]["flagged"], pane["last_run"]["drafted"]), (8, 7))
        self.assertEqual(set(pane["files"]), {"xlsx", "html", "db"})
        for v in pane["today_items"] + pane["pipeline"]:
            self.assertEqual(set(v), FOLLOWUP_VIEW_KEYS)
        for c in pane["customers"]:
            self.assertEqual(set(c), CUSTOMER_VIEW_KEYS)
        self.assertEqual(len(pane["customers"]), 18)
        # drafted first, then call; kind order inside the group
        states = [v["state"] for v in pane["today_items"]]
        self.assertEqual(states, ["drafted"] * 7 + ["call"])
        self.assertEqual(pane["today_items"][0]["customer_name"], "Pinecrest Hotel")
        self.assertEqual(pane["today_items"][0]["state_label"], "\U0001f4dd Ready to review")
        self.assertEqual(pane["today_items"][-1]["state_label"], "\U0001f4de Call (no email)")
        harbor = [v for v in pane["today_items"] if v["customer_name"] == "Harbor Grocers"][0]
        self.assertEqual((harbor["kind_label"], harbor["tier_label"]), ("Overdue invoice", "Friendly reminder"))

    def test_xlsx_is_valid_ooxml(self):
        path = self.w.ws / "Follow-ups.xlsx"
        with zipfile.ZipFile(str(path)) as zf:
            self.assertIsNone(zf.testzip())
            names = set(zf.namelist())
            for part in ("[Content_Types].xml", "_rels/.rels", "docProps/app.xml", "docProps/core.xml", "xl/workbook.xml",
                         "xl/_rels/workbook.xml.rels", "xl/styles.xml"):
                self.assertIn(part, names)
            parsed = {name: ET.fromstring(zf.read(name)) for name in names}  # every part parses
        wb = parsed["xl/workbook.xml"]
        self.assertEqual([s.get("name") for s in wb.findall("m:sheets/m:sheet", NS)], SHEETS)
        rels = parsed["xl/_rels/workbook.xml.rels"]
        targets = {r.get("Target") for r in rels}
        for i in range(1, 8):
            self.assertIn("worksheets/sheet%d.xml" % i, targets)
            self.assertIn("xl/worksheets/sheet%d.xml" % i, names)
        types = ET.tostring(parsed["[Content_Types].xml"]).decode()
        self.assertIn("/xl/worksheets/sheet7.xml", types)
        styles = parsed["xl/styles.xml"]
        self.assertEqual(styles.find("m:numFmts/m:numFmt", NS).get("formatCode"), "#,##0.00")
        self.assertIsNotNone(styles.find("m:fonts/m:font/m:b", NS))
        for i in range(1, 8):
            sheet = parsed["xl/worksheets/sheet%d.xml" % i]
            pane = sheet.find("m:sheetViews/m:sheetView/m:pane", NS)
            self.assertEqual((pane.get("ySplit"), pane.get("state")), ("1", "frozen"))
            self.assertIsNotNone(sheet.find("m:autoFilter", NS))
            self.assertTrue(sheet.findall("m:cols/m:col", NS))
            header = sheet.find("m:sheetData/m:row", NS)
            self.assertTrue(all(c.get("s") == "1" and c.get("t") == "inlineStr" for c in header))
            # child order matters to Excel
            tags = [child.tag.split("}")[1] for child in sheet]
            self.assertEqual(tags, ["dimension", "sheetViews", "sheetFormatPr", "cols", "sheetData", "autoFilter"])

    def test_xlsx_money_is_numeric(self):
        with zipfile.ZipFile(str(self.w.ws / "Follow-ups.xlsx")) as zf:
            sheet = ET.fromstring(zf.read("xl/worksheets/sheet6.xml"))  # Open invoices
        rows = sheet.findall("m:sheetData/m:row", NS)
        header = [cell_text(c) for c in rows[0]]
        self.assertEqual(header[:7], ["Invoice", "Customer", "Date", "Due date", "Amount", "Balance", "Days overdue"])
        first = {header[i]: c for i, c in enumerate(rows[1])}
        self.assertEqual(cell_text(first["Invoice"]), "1039")
        self.assertEqual((first["Balance"].get("s"), first["Balance"].get("t")), ("2", None))
        self.assertEqual(float(cell_text(first["Balance"])), 3420.0)
        self.assertEqual(cell_text(first["Days overdue"]), "45")
        self.assertEqual(cell_text(first["Due date"]), "2026-10-11")

    def test_html_is_self_contained(self):
        text = (self.w.ws / "Follow-ups.html").read_text(encoding="utf-8")
        self.assertNotIn("http", text.lower())
        self.assertIsNone(re.search(r"<[^>]*\s(src|href|action)\s*=", text, re.I))  # no external references in tags
        self.assertNotIn("@import", text)
        for name in SHEETS:
            self.assertIn(name, text)
        for needle in ("prefers-color-scheme", "Download CSV", "Generated ", "from followup.db", "Overdue balance",
                       "Open quotes", "Ready to review", "Sent this month"):
            self.assertIn(needle, text)
        data = re.search(r'<script type="application/json" id="fu-data">(.*?)</script>', text, re.S).group(1)
        payload = json.loads(data)
        self.assertEqual([t["name"] for t in payload["tables"]], SHEETS)
        self.assertEqual(payload["tiles"][0]["value"], 8090.75)
        self.assertEqual(text.count("</script>"), 2)


class EscapingTests(unittest.TestCase):
    def test_hostile_text_is_escaped_everywhere(self):
        w = Workspace()
        try:
            name = 'Tricky <&> "Co" \x07</script><script>alert(1)</script>'
            snap = qb_snapshot([cust("1", name, "t@x.example")], invoices=[inv("1050", "1", ago(10), 860.0, memo="see https://evil.example/x")])
            mail = mail_snapshot([thread("t@x.example", "t1", ts(3), "them", "Hello <b>", "link: http://evil.example <!-- x")])
            w.run_with(TODAY, snap, mail)
            w.cli("export")
            with zipfile.ZipFile(str(w.ws / "Follow-ups.xlsx")) as zf:
                for part in zf.namelist():
                    ET.fromstring(zf.read(part))
                sheet = ET.fromstring(zf.read("xl/worksheets/sheet4.xml"))  # Customers
            texts = [cell_text(c) for c in sheet.iter("{%s}c" % NS["m"])]
            self.assertIn('Tricky <&> "Co" </script><script>alert(1)</script>', texts)  # control char stripped
            html_text = (w.ws / "Follow-ups.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>alert", html_text)
            self.assertNotIn("http", html_text.lower())
            self.assertNotIn("<!-- x", html_text)
            self.assertNotIn("mail.google", html_text)
            pane = w.pane()
            self.assertTrue(pane["today_items"][0]["thread_url"].startswith("https://mail.google.com/"))  # the pane keeps links
        finally:
            w.cleanup()


class RefreshTests(unittest.TestCase):
    def test_fresh_init_pane(self):
        w = Workspace()
        try:
            pane = w.pane()
            self.assertIsNone(pane["last_run"])
            self.assertEqual(pane["today_items"], [])
            self.assertEqual(pane["counts"]["customers"], 0)
            self.assertFalse((w.ws / "Follow-ups.xlsx").exists())  # init writes pane.json only
        finally:
            w.cleanup()

    def test_locked_xlsx_is_a_warning(self):
        w = Workspace()
        try:
            (w.ws / "Follow-ups.xlsx").mkdir()  # stands in for a file Excel has locked
            out, code = fu.run(["demo", "--ws", str(w.ws), "--today", TODAY.isoformat()])
            self.assertEqual((out["ok"], code), (True, 0))
            self.assertTrue(any("Follow-ups.xlsx" in x for x in out["warnings"]))
            self.assertTrue((w.ws / "Follow-ups.html").exists())
            self.assertTrue((w.ws / "views" / "pane.json").exists())
        finally:
            w.cleanup()

    def test_every_change_refreshes_all_three(self):
        w = Workspace()
        try:
            w.cli("demo", "--with-drafts", today=TODAY)
            fid = w.cli("list", "--state", "drafted")["items"][0]["id"]
            before = (w.ws / "Follow-ups.html").read_text(encoding="utf-8")
            out = w.cli("approve", fid)
            self.assertEqual(set(out["views"]), {"pane", "xlsx", "html"})
            self.assertNotEqual(before, (w.ws / "Follow-ups.html").read_text(encoding="utf-8"))
            self.assertEqual(w.pane()["counts"]["approved"], 1)
            views_only = w.cli("views")
            self.assertEqual(set(views_only["views"]), {"pane"})
            self.assertNotIn("views", w.cli("list"))  # read-only commands don't rewrite files
        finally:
            w.cleanup()


if __name__ == "__main__":
    unittest.main()
