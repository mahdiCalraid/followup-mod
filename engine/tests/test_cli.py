"""The CLI end to end: state transitions, JSON output, argument forms the mod uses."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_helpers import SCRIPTS, TODAY, Workspace, ago, cust, inv, qb_snapshot  # noqa: E402

import fu  # noqa: E402
import fu_db  # noqa: E402


def events(w: Workspace, fid: str):
    conn = w.conn()
    try:
        return [r["kind"] for r in conn.execute("SELECT kind FROM events WHERE followup_id = ? ORDER BY id", (fid,))]
    finally:
        conn.close()


class DemoDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = Workspace()
        cls.demo = cls.w.cli("demo", "--with-drafts", today=TODAY)

    @classmethod
    def tearDownClass(cls):
        cls.w.cleanup()

    def test_with_drafts_stores_passing_drafts(self):
        self.assertEqual(self.demo["drafted"], 7)
        self.assertTrue(all(d["passed"] for d in self.demo["drafts"]))
        for item in self.w.cli("list", "--state", "drafted")["items"]:
            with self.subTest(item=item["customer_name"]):
                out = self.w.cli("check-draft", item["id"])
                self.assertEqual(out["problems"], [])
                self.assertTrue(item["draft_body"].rstrip().endswith("Brightwater Bakery Co."))
                self.assertEqual(item["draft_to"], item["email"])

    def test_partly_paid_draft_quotes_500(self):
        item = [v for v in self.w.cli("list")["items"] if v["customer_name"] == "Northside Coffee Co."][0]
        self.assertIn("$500.00", item["draft_body"])
        self.assertNotIn("1,500", item["draft_body"])

    def test_firm_drafts_ask_for_a_date(self):
        for v in self.w.cli("list", "--kind", "invoice_overdue")["items"]:
            if v["tier"] == "firm":
                self.assertIn("date", v["draft_body"])

    def test_suspicious_problem_survives_a_passing_draft(self):
        westgate = [v for v in self.w.cli("list")["items"] if v["customer_name"] == "Westgate Cafe"][0]
        self.assertEqual(westgate["state"], "drafted")
        self.assertIn("Suspicious text", " ".join(westgate["problems"]))

    def test_call_has_no_draft(self):
        call = self.w.cli("list", "--state", "call")["items"]
        self.assertEqual([c["customer_name"] for c in call], ["Riverside Deli"])
        self.assertIsNone(call[0]["draft_body"])
        self.assertEqual(call[0]["phone"], "+1 555 0108")

    def test_sender_name_from_config(self):
        w = Workspace(config={"sender_name": "Alex at Brightwater"})
        try:
            w.cli("demo", "--with-drafts", today=TODAY)
            bodies = [v["draft_body"] for v in w.cli("list", "--state", "drafted")["items"]]
            self.assertTrue(bodies and all(b.endswith("Alex at Brightwater") for b in bodies))
        finally:
            w.cleanup()


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.w = Workspace()
        self.w.cli("demo", "--with-drafts", today=TODAY)
        self.items = {v["customer_name"]: v for v in self.w.cli("list")["items"]}

    def tearDown(self):
        self.w.cleanup()

    def test_draft_approve_staged_sent_replied_outcome(self):
        fid = self.items["Harbor Grocers"]["id"]
        self.assertEqual(self.w.cli("approve", fid)["approved"], [fid])
        staged = self.w.cli("stage-list")["items"]
        entry = [s for s in staged if s["id"] == fid][0]
        self.assertEqual(entry["to"], "sam@harborgrocers.example")
        self.assertTrue(entry["subject"] and entry["body"])
        self.assertIsNone(entry["thread_id"])  # no thread with Harbor: a new email
        self.w.cli("staged", fid, "--draft-id", "r-123", "--url", "https://mail.google.com/mail/#drafts/r-123")
        check = self.w.cli("check-list")["items"]
        self.assertEqual([c["id"] for c in check], [fid])
        self.assertEqual(check[0]["email"], "sam@harborgrocers.example")
        self.w.cli("sent", fid, "--message-id", "m-1", "--at", "2026-11-25T16:00:00Z")
        replied = self.w.cli("replied", fid, "--at", "2026-11-26T09:00:00Z", "--snippet", "Paying today, sorry!")
        item = replied["items"][-1]
        self.assertEqual(item["text"], "What did they say?")
        self.assertEqual(item["options"], ["Interested", "Later", "Not interested", "Won / paid", "No real answer"])
        out = self.w.cli("outcome", fid, "paid", "--note", "paid by card")
        self.assertEqual((out["state"], out["outcome"]), ("closed", "paid"))
        fu_row = self.w.followup(fid)
        self.assertEqual((fu_row["gmail_draft_id"], fu_row["sent_message_id"]), ("r-123", "m-1"))
        self.assertEqual(fu_row["replied_at"], "2026-11-26T09:00:00Z")
        self.assertEqual(events(self.w, fid), ["state:found", "state:drafted", "state:approved", "state:staged", "state:sent",
                                               "state:replied", "state:closed", "outcome:paid"])
        pane = self.w.pane()
        self.assertIn(fid, [v["id"] for v in pane["sent"]])
        self.assertNotIn(fid, [v["id"] for v in pane["pipeline"]])

    def test_reply_question_answer_maps_to_outcome(self):
        harbor = self.items["Harbor Grocers"]["id"]
        copper = self.items["Copper Kettle Cafe"]["id"]
        for fid in (harbor, copper):
            self.w.cli("approve", fid)
            self.w.cli("staged", fid, "--draft-id", "d-" + fid)
            self.w.cli("sent", fid, "--message-id", "m-" + fid, "--at", "2026-11-25T16:00:00Z")
            self.w.cli("replied", fid, "--at", "2026-11-26T09:00:00Z")
        self.assertEqual(self.w.cli("answer", harbor, "n1", "Won / paid")["outcome"], "paid")
        self.assertEqual(self.w.cli("answer", copper, "n1", "Won / paid")["outcome"], "won")
        self.assertEqual(self.w.followup(copper)["items"][0]["answer"], "Won / paid")

    def test_approve_all_drafted_then_skip_snooze_dnc(self):
        out = self.w.cli("approve", "--all-drafted")
        self.assertEqual(len(out["approved"]), 7)
        self.assertEqual(self.w.pane()["counts"]["approved"], 7)
        pine = self.items["Pinecrest Hotel"]["id"]
        self.w.cli("skip", pine, "--note", "called them")
        self.assertEqual(self.w.followup(pine)["state"], "skipped")
        sun = self.items["Sunrise Juice Bar"]["id"]
        self.w.cli("snooze", sun, "2026-12-10")
        self.assertEqual(self.w.followup(sun)["later_date"], "2026-12-10")
        west = self.items["Westgate Cafe"]["id"]
        self.w.cli("dnc", west)
        row = self.w.followup(west)
        self.assertEqual((row["state"], row["outcome"]), ("closed", "do_not_contact"))
        conn = self.w.conn()
        try:
            self.assertEqual(conn.execute("SELECT do_not_contact FROM customers WHERE id = ?", (row["customer_id"],))
                             .fetchone()["do_not_contact"], 1)
        finally:
            conn.close()

    def test_bad_transitions_are_refused(self):
        fid = self.items["Harbor Grocers"]["id"]
        self.assertIn("Can't mark sent", self.w.cli("sent", fid, "--message-id", "m", "--at", "2026-11-25", ok=False)["error"])
        call = self.items["Riverside Deli"]["id"]
        self.assertIn("Can't approve", self.w.cli("approve", call, ok=False)["error"])
        self.assertIn("No follow-up", self.w.cli("show", "fu-nope", ok=False)["error"])
        self.assertIn("OUTCOME must be", self.w.cli("outcome", fid, "maybe", ok=False)["error"])
        self.assertIn("needs --date", self.w.cli("outcome", fid, "later", ok=False)["error"])
        self.assertEqual(self.w.followup(fid)["state"], "drafted")

    def test_show_has_everything_to_draft(self):
        voice = self.w.ws / "followup_mod" / "VOICE.md"
        voice.write_text("Warm and brief.", encoding="utf-8")
        fid = self.items["Maple Street Market"]["id"]
        self.w.cli("revise", fid, "--instruction", "Mention we deliver on Tuesdays")
        out = self.w.cli("show", fid)
        self.assertEqual(out["voice"], "Warm and brief.")
        self.assertEqual(out["thread"]["subject"], "Wholesale pricing?")
        self.assertEqual(out["revise_instruction"], "Mention we deliver on Tuesdays")
        self.assertEqual(out["learned_edits"][-1]["instruction"], "Mention we deliver on Tuesdays")
        self.assertEqual(out["draft_rules"]["to"], "orders@maplestreetmarket.example")
        self.assertTrue(out["guidance"])
        firm = self.w.cli("show", self.items["Pinecrest Hotel"]["id"])
        self.assertIn("payment date", firm["guidance"])
        self.assertTrue(firm["draft_rules"]["firm_needs_date_ask"])

    def test_revise_writes_learned_edits_and_todo(self):
        fid = self.items["Copper Kettle Cafe"]["id"]
        before = self.w.followup(fid)
        out = self.w.cli("revise", fid, "--instruction", "Shorter, and offer a tasting")
        self.assertEqual(out["state"], "revise")
        lines = (self.w.ws / "followup_mod" / "learned" / "edits.jsonl").read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[-1])
        self.assertEqual(set(rec), {"at", "followup_id", "kind", "instruction", "before_subject", "before_body"})
        self.assertEqual((rec["followup_id"], rec["kind"], rec["before_body"]), (fid, "quote_quiet", before["draft_body"]))
        self.assertIn(fid, [i["id"] for i in self.w.cli("todo-drafts")["items"]])

    def test_draft_fails_once_then_needs_you(self):
        fid = self.items["Copper Kettle Cafe"]["id"]
        self.w.cli("revise", fid, "--instruction", "Mention the price")
        body = self.w.ws / "bad.txt"
        body.write_text("Hi Dana,\n\nYour estimate for $9,999.00 is waiting.\n\nThanks", encoding="utf-8")
        first = self.w.cli("draft", fid, "--subject", "Your estimate", "--body-file", str(body), ok=False)
        self.assertFalse(first["passed"])
        self.assertEqual(first["state"], "revise")
        self.assertTrue(any("$9,999.00" in p for p in first["problems"]))
        self.assertTrue(any("$9,999.00" in p for p in self.w.followup(fid)["problems"]))
        self.assertFalse(self.w.cli("check-draft", fid, ok=False)["ok"])
        second = self.w.cli("draft", fid, "--subject", "Your estimate", "--body-file", str(body), ok=False)
        self.assertEqual(second["state"], "needs_you")
        row = self.w.followup(fid)
        self.assertEqual(row["items"][-1]["type"], "task")
        # the owner fixes it: answering the task sends it back for a redraft
        out = self.w.cli("answer", fid, row["items"][-1]["id"], "done")
        self.assertEqual(out["state"], "revise")
        good = self.w.ws / "good.txt"
        good.write_text("Hi Dana,\n\nJust checking in on estimate 1031 for $1,840.00. Any questions?\n\nThanks", encoding="utf-8")
        ok = self.w.cli("draft", fid, "--subject", "Estimate 1031", "--body-file", str(good))
        self.assertEqual((ok["passed"], ok["state"]), (True, "drafted"))

    def test_give_up_sets_needs_you(self):
        fid = self.items["Copper Kettle Cafe"]["id"]
        self.w.cli("revise", fid, "--instruction", "try again")
        out = self.w.cli("draft", fid, "--give-up", ok=False)
        self.assertEqual(out["state"], "needs_you")

    def test_wrong_recipient_draft_is_refused(self):
        fid = self.items["Harbor Grocers"]["id"]
        body = self.w.ws / "b.txt"
        body.write_text("Hi Sam,\n\nA reminder about invoice 1050.\n\nThanks", encoding="utf-8")
        out = self.w.cli("draft", fid, "--subject", "Invoice 1050", "--body-file", str(body), "--to", "boss@other.example", ok=False)
        self.assertTrue(any("addressed to boss@other.example" in p for p in out["problems"]))

    def test_needs_you_question_and_answer(self):
        fid = self.items["Maple Street Market"]["id"]
        out = self.w.cli("needs-you", fid, "--question", "Do we deliver to Maple Street?", "--options", "Yes|No", "--key", "delivery")
        self.assertEqual(out["item"], {"id": "n1", "type": "question", "text": "Do we deliver to Maple Street?", "answer": None,
                                       "options": ["Yes", "No"], "key": "delivery"})
        self.w.cli("needs-you", fid, "--task", "Find the price list")
        self.assertEqual(self.w.pane()["counts"]["needs_you"], 1)
        part = self.w.cli("answer", fid, "n1", "Yes")
        self.assertEqual(part["state"], "needs_you")
        done = self.w.cli("answer", fid, "n2", "done")
        self.assertEqual((done["state"], done["all_resolved"]), ("revise", True))

    def test_mod_argument_forms(self):
        """The mod passes --ws after the subcommand's own arguments, and values that start with '-'."""
        ws = str(self.w.ws)
        fid = self.items["Harbor Grocers"]["id"]
        out, code = fu.run(["approve", fid, "--ws", ws])
        self.assertEqual((out["ok"], code, out["approved"]), (True, 0, [fid]))
        cid = self.items["Copper Kettle Cafe"]["id"]
        out, code = fu.run(["revise", cid, "--instruction=-shorter, please", "--ws", ws])
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.w.followup(cid)["revise_instruction"], "-shorter, please")
        mid = self.items["Maple Street Market"]["id"]
        fu.run(["needs-you", mid, "--question=-which day?", "--ws", ws])
        fu.run(["needs-you", mid, "--question", "Price?", "--ws", ws])
        out, code = fu.run(["answer", mid, "n1", "--ws", ws, "--", "-Tuesday"])
        self.assertTrue(out["ok"], out)
        out, code = fu.run(["answer", mid, "n2", "4.50 a loaf", "--ws", ws])
        self.assertTrue(out["ok"], out)
        items = self.w.followup(mid)["items"]
        self.assertEqual([i["answer"] for i in items], ["-Tuesday", "4.50 a loaf"])
        self.assertEqual(self.w.followup(mid)["state"], "revise")
        out, code = fu.run(["--ws", ws, "list", "--state", "revise"])
        self.assertEqual(out["count"], 2)
        out, code = fu.run(["list", "--ws", ws, "--today", "2026-11-25", "--kind", "quote_quiet"])
        self.assertEqual(out["count"], 1)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.w = Workspace()

    def tearDown(self):
        self.w.cleanup()

    def subprocess_run(self, *args, cwd=None, env=None, script=None):
        script = script or str(SCRIPTS / "fu.py")
        proc = subprocess.run([sys.executable, script] + list(args), cwd=cwd or tempfile.gettempdir(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        text = proc.stdout.decode("utf-8")
        self.assertEqual(len(text.strip().splitlines()), 1, text)
        return json.loads(text), proc.returncode

    def test_every_command_prints_one_json_object(self):
        ws = str(self.w.ws)
        out, code = self.subprocess_run("demo", "--with-drafts", "--today", "2026-11-25", "--ws", ws)
        self.assertEqual((out["ok"], code), (True, 0))
        for args in (["list"], ["customers"], ["stage-list"], ["check-list"], ["todo-drafts"], ["views"], ["export"]):
            with self.subTest(args=args):
                out, code = self.subprocess_run(*(args + ["--ws", ws]))
                self.assertEqual((out["ok"], code), (True, 0))
        out, code = self.subprocess_run("show", "fu-missing", "--ws", ws)
        self.assertEqual((out["ok"], code), (False, 1))
        out, code = self.subprocess_run("no-such-command", "--ws", ws)
        self.assertEqual((out["ok"], code), (False, 1))
        out, code = self.subprocess_run("snooze", "fu-x", "--ws", ws)
        self.assertFalse(out["ok"])

    def test_requires_init(self):
        empty = tempfile.mkdtemp()
        try:
            out, code = fu.run(["list", "--ws", empty])
            self.assertEqual(code, 1)
            self.assertIn("Run `fu.py init` first", out["error"])
            self.assertFalse((Path(empty) / "followup.db").exists())
        finally:
            shutil.rmtree(empty)

    def test_bad_today_and_bad_config(self):
        out, code = fu.run(["list", "--ws", str(self.w.ws), "--today", "11/25/2026"])
        self.assertEqual(code, 1)
        (self.w.ws / "followup_mod" / "config.json").write_text("{not json", encoding="utf-8")
        out, code = fu.run(["list", "--ws", str(self.w.ws)])
        self.assertEqual(code, 1)
        self.assertIn("config.json", out["error"])

    def test_missing_config_keys_fall_back_to_defaults(self):
        self.w.write_config({"rules": {"max_per_run": "not a number", "order": ["bogus"]}, "draft": {}})
        cfg = fu_db.load_config(self.w.ws / "followup_mod" / "config.json")
        self.assertEqual(cfg["rules"]["max_per_run"], 7)
        self.assertEqual(cfg["rules"]["order"], fu_db.KIND_ORDER)
        self.assertEqual(cfg["draft"]["max_body_chars"], 1200)
        self.assertEqual(cfg["owner_emails"], [])

    def test_workspace_found_from_script_location(self):
        ws = Path(tempfile.mkdtemp())
        try:
            mod = ws / "followup_mod"
            shutil.copytree(str(SCRIPTS), str(mod / "scripts"), ignore=shutil.ignore_patterns("__pycache__"))
            (mod / "config.json").write_text("{}", encoding="utf-8")
            env = dict(os.environ)
            env.pop("FOLLOWUP_WS", None)
            out, code = self.subprocess_run("init", script=str(mod / "scripts" / "fu.py"), env=env)
            self.assertEqual(Path(out["workspace"]).resolve(), ws.resolve())
            self.assertTrue((ws / "followup.db").exists())
            self.assertTrue((ws / "views" / "pane.json").exists())
            env["FOLLOWUP_WS"] = str(self.w.ws)
            out, code = self.subprocess_run("views", script=str(mod / "scripts" / "fu.py"), env=env)
            self.assertEqual(Path(out["views"]["pane"]).parent.parent.resolve(), self.w.ws.resolve())
        finally:
            shutil.rmtree(str(ws))

    def test_run_cycle_by_hand(self):
        start = self.w.cli("run-start", "--qb", "connector", "--mail", "gmail", today=TODAY)
        self.assertRegex(start["run_id"], r"^r-20261125-\d{6}$")
        self.assertTrue(Path(start["snapshot_dir"]).is_dir())
        second = self.w.cli("run-start", today=TODAY)
        self.assertNotEqual(start["run_id"], second["run_id"])
        snap = qb_snapshot([cust("23", "Harbor Grocers", "sam@harbor.example")], invoices=[inv("1050", "23", ago(10), 860.0)])
        (Path(start["snapshot_dir"]) / "quickbooks.json").write_text(json.dumps(snap), encoding="utf-8")
        ing = self.w.cli("ingest", "--run", start["run_id"])
        self.assertEqual(ing["counts"]["quickbooks"]["invoices"], 1)
        ranked = self.w.cli("rank", "--run", start["run_id"])
        self.assertEqual(ranked["flagged"], 1)
        todo = self.w.cli("todo-drafts", "--run", start["run_id"])
        self.assertEqual(todo["count"], 1)
        fin = self.w.cli("run-finish", "--run", start["run_id"])
        self.assertEqual((fin["flagged"], fin["drafted"]), (1, 0))
        self.assertIn("No run r-nope", self.w.cli("ingest", "--run", "r-nope", ok=False)["error"])

    def test_reset(self):
        self.w.cli("demo", today=TODAY)
        self.assertFalse(fu.run(["reset", "--ws", str(self.w.ws)])[0]["ok"])
        out = self.w.cli("reset", "--yes")
        self.assertTrue(out["deleted"])
        for name in ("followup.db", "views", "Follow-ups.xlsx", "Follow-ups.html"):
            self.assertFalse((self.w.ws / name).exists(), name)
        self.assertTrue((self.w.ws / "snapshots").exists())


if __name__ == "__main__":
    unittest.main()
