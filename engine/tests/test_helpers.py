"""Shared helpers for the engine tests (no test cases here).

Builds throwaway workspaces and synthetic snapshots with a fixed ``today`` so every test is
deterministic. Calls the CLI in-process through ``fu.run``.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fu  # noqa: E402
import fu_db  # noqa: E402

TODAY = date(2026, 11, 25)


def iso(d: date) -> str:
    return d.isoformat()


def ago(days: int, today: date = TODAY) -> str:
    return (today - timedelta(days=days)).isoformat()


def ts(days: int, today: date = TODAY) -> str:
    return "%sT10:00:00Z" % ago(days, today)


def cust(qb_id: str, company: str, email: Optional[str] = None, phone: str = "+1 555 0100", name: Optional[str] = None) -> Dict[str, Any]:
    if email is None:
        email = "%s@%s.example" % ((name or "Pat Doe").split()[0].lower(), company.lower().replace(" ", "").replace("&", ""))
    return {"qb_id": qb_id, "name": name or "Pat Doe", "company": company, "email": email, "phone": phone, "type": "Wholesale", "notes": ""}


def est(number: str, qb_id: str, txn_date: Optional[str], amount: float, status: str = "Pending", memo: str = "") -> Dict[str, Any]:
    return {"number": number, "customer_qb_id": qb_id, "customer_name": "", "date": txn_date, "expiry_date": None,
            "amount": amount, "status": status, "memo": memo}


def inv(number: str, qb_id: str, due: Optional[str], balance: float, amount: Optional[float] = None,
        txn_date: Optional[str] = None, memo: str = "") -> Dict[str, Any]:
    return {"number": number, "customer_qb_id": qb_id, "customer_name": "", "date": txn_date or due, "due_date": due,
            "amount": balance if amount is None else amount, "balance": balance,
            "status": "Open" if balance > 0 else "Paid", "memo": memo}


def qb_snapshot(customers: List[Dict[str, Any]] = (), estimates: List[Dict[str, Any]] = (),
                invoices: List[Dict[str, Any]] = (), totals: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    estimates, invoices = list(estimates), list(invoices)
    if totals is None:
        totals = {"estimates_pending_amount": round(sum(e["amount"] for e in estimates if e["status"] == "Pending"), 2),
                  "invoices_open_balance": round(sum(i["balance"] for i in invoices if i["balance"] > 0), 2)}
    return {"schema": "followup-qb/1", "captured_at": "2026-11-25T14:02:00Z", "source": "connector",
            "customers": list(customers), "estimates": estimates, "invoices": invoices, "report_totals": totals}


def thread(email: str, tid: str, last_at: str, last_from: str, subject: str = "Hello", snippet: str = "",
           name: str = "Pat Doe", participants: Optional[List[str]] = None, inquiry: bool = False) -> Dict[str, Any]:
    return {"email": email, "name": name, "thread_id": tid, "subject": subject, "last_at": last_at, "last_from": last_from,
            "last_message_id": tid + "-last", "snippet": snippet, "message_count": 2,
            "participants": participants if participants is not None else [email, "owner@ourbakery.example"],
            "inquiry": inquiry, "url": "https://mail.google.com/mail/u/0/#inbox/" + tid}


def mail_snapshot(threads: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"schema": "followup-mail/1", "captured_at": "2026-11-25T14:05:00Z", "source": "gmail",
            "owner_emails": ["owner@ourbakery.example"], "threads": list(threads)}


class Workspace:
    """A temporary <ws> with followup_mod/config.json, driven through fu.run()."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, init: bool = True):
        self.ws = Path(tempfile.mkdtemp(prefix="fu-test-"))
        (self.ws / "followup_mod").mkdir()
        if config is not None:
            self.write_config(config)
        if init:
            self.cli("init")

    def write_config(self, config: Dict[str, Any]) -> None:
        (self.ws / "followup_mod" / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def cleanup(self) -> None:
        shutil.rmtree(str(self.ws), ignore_errors=True)

    def cli(self, *args: str, today: Optional[date] = None, ok: Optional[bool] = True) -> Dict[str, Any]:
        argv = list(args) + ["--ws", str(self.ws)]
        if today is not None:
            argv += ["--today", today.isoformat()]
        out, code = fu.run(argv)
        if ok is True:
            assert out.get("ok") is True and code == 0, out
        elif ok is False:
            assert out.get("ok") is False and code == 1, out
        return out

    def run_with(self, today: date, qb: Optional[Dict[str, Any]] = None, mail: Optional[Dict[str, Any]] = None,
                 rank: bool = True) -> Dict[str, Any]:
        start = self.cli("run-start", "--qb", "connector", "--mail", "gmail", today=today)
        folder = Path(start["snapshot_dir"])
        if qb is not None:
            (folder / "quickbooks.json").write_text(json.dumps(qb), encoding="utf-8")
        if mail is not None:
            (folder / "mail.json").write_text(json.dumps(mail), encoding="utf-8")
        if qb is not None or mail is not None:
            self.cli("ingest", "--run", start["run_id"])
        if not rank:
            return start
        out = self.cli("rank", "--run", start["run_id"])
        out["run_id"] = start["run_id"]
        return out

    def conn(self):
        return fu_db.connect(self.ws / "followup.db")

    def followup(self, fid: str) -> Dict[str, Any]:
        conn = self.conn()
        try:
            return fu_db.get_followup(conn, fid)
        finally:
            conn.close()

    def pane(self) -> Dict[str, Any]:
        return json.loads((self.ws / "views" / "pane.json").read_text(encoding="utf-8"))


def by_customer(rank_out: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {f["customer_name"]: f for f in rank_out["followups"]}
