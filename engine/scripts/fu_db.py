"""Workspace paths, config, clock, SQLite schema and data access for the Follow-up engine.

Standard library only; Python 3.9 compatible. No network.
"""
from __future__ import annotations

import calendar
import copy
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1"

# ---------------------------------------------------------------------------
# Vocabulary (CONTRACT section 5 and 8)
# ---------------------------------------------------------------------------

KIND_ORDER = [
    "invoice_overdue",
    "unanswered_inbound",
    "quote_quiet",
    "silent_after_proposal",
    "dormant_customer",
    "old_conversation",
]

KIND_LABELS = {
    "invoice_overdue": "Overdue invoice",
    "unanswered_inbound": "Waiting on our reply",
    "quote_quiet": "Quote gone quiet",
    "silent_after_proposal": "Proposal, no answer",
    "dormant_customer": "Past customer gone quiet",
    "old_conversation": "Quiet conversation",
}

STATE_LABELS = {
    "found": "✍️ Claude is drafting",
    "revise": "✍️ Redrafting",
    "drafted": "\U0001f4dd Ready to review",
    "needs_you": "✋ Needs you",
    "approved": "✅ Approved, going to Gmail",
    "staged": "\U0001f4ec Draft in Gmail, press Send there",
    "sent": "✔ Sent",
    "replied": "\U0001f4ac They replied",
    "closed": "Closed",
    "skipped": "Skipped",
    "snoozed": "\U0001f4a4 Snoozed",
    "call": "\U0001f4de Call (no email)",
    "failed": "✖ Failed",
}

OPEN_STATES = ("found", "drafted", "revise", "needs_you", "approved", "staged", "call")

TIER_LABELS = {
    "friendly": "Friendly reminder",
    "second": "Second reminder",
    "firm": "Firm reminder (asks for a payment date)",
}

OUTCOMES = ("interested", "later", "not_interested", "won", "paid", "no_reply", "do_not_contact")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "business_name": "",
    "sender_name": "",
    "owner_emails": [],
    "internal_domains": [],
    "mail": "gmail",
    "quickbooks": "connector",
    "python": "python3",
    "rules": {
        "quote_quiet_days": 5,
        "quote_stale_days": 90,
        "lead_quiet_days": 21,
        "dormant_months": 6,
        "include_old_conversations": False,
        "invoice_tiers": {"friendly": 1, "second": 15, "firm": 31},
        "order": list(KIND_ORDER),
        "max_per_run": 7,
        "skip_cooldown_days": 14,
        "no_reply_cooldown_days": 14,
    },
    "draft": {
        "max_body_chars": 1200,
        "max_subject_chars": 120,
        "banned_phrases": [
            "I hope this email finds you well",
            "just circling back",
            "per my last email",
            "as per my previous email",
            "final notice",
            "legal action",
            "collections agency",
            "act now",
            "as an AI",
        ],
    },
    "do_not_contact": [],
}

_INT_RULES = ("quote_quiet_days", "quote_stale_days", "lead_quiet_days", "dormant_months",
              "max_per_run", "skip_cooldown_days", "no_reply_cooldown_days")


class ConfigError(Exception):
    pass


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;\n]", value) if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def normalize_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge with defaults and coerce types; a bad value falls back to its default."""
    cfg = _merge(DEFAULT_CONFIG, raw if isinstance(raw, dict) else {})
    rules = cfg["rules"] if isinstance(cfg.get("rules"), dict) else copy.deepcopy(DEFAULT_CONFIG["rules"])
    for key in _INT_RULES:
        try:
            rules[key] = int(rules.get(key))
            if rules[key] < 0:
                raise ValueError
        except (TypeError, ValueError):
            rules[key] = DEFAULT_CONFIG["rules"][key]
    inc = rules.get("include_old_conversations")
    rules["include_old_conversations"] = inc is True or str(inc).lower() in ("1", "true", "yes", "on")
    tiers = rules.get("invoice_tiers")
    if isinstance(tiers, list):  # also accept [[1, "friendly"], ...] or [["friendly", 1], ...]
        conv = {}
        for pair in tiers:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                a, b = pair
                if isinstance(a, str):
                    conv[a] = b
                else:
                    conv[str(b)] = a
        tiers = conv
    clean_tiers: Dict[str, int] = {}
    if isinstance(tiers, dict):
        for name, floor in tiers.items():
            try:
                clean_tiers[str(name)] = int(floor)
            except (TypeError, ValueError):
                pass
    rules["invoice_tiers"] = clean_tiers or dict(DEFAULT_CONFIG["rules"]["invoice_tiers"])
    order = [k for k in _as_list(rules.get("order")) if k in KIND_ORDER]
    seen = []
    for k in order + KIND_ORDER:
        if k not in seen:
            seen.append(k)
    rules["order"] = seen
    cfg["rules"] = rules
    draft = cfg["draft"] if isinstance(cfg.get("draft"), dict) else copy.deepcopy(DEFAULT_CONFIG["draft"])
    for key in ("max_body_chars", "max_subject_chars"):
        try:
            draft[key] = int(draft.get(key))
            if draft[key] <= 0:
                raise ValueError
        except (TypeError, ValueError):
            draft[key] = DEFAULT_CONFIG["draft"][key]
    draft["banned_phrases"] = _as_list(draft.get("banned_phrases"))
    cfg["draft"] = draft
    cfg["owner_emails"] = [e.lower() for e in _as_list(cfg.get("owner_emails"))]
    cfg["internal_domains"] = [d.lower().lstrip("@") for d in _as_list(cfg.get("internal_domains"))]
    cfg["do_not_contact"] = [e.lower() for e in _as_list(cfg.get("do_not_contact"))]
    for key in ("business_name", "sender_name", "mail", "quickbooks", "python"):
        value = cfg.get(key)
        cfg[key] = "" if value is None else str(value)
    return cfg


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return normalize_config({})
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError("%s could not be read (%s). Fix it, or copy templates/config.example.json again." % (path, exc))
    return normalize_config(raw)


# ---------------------------------------------------------------------------
# Workspace paths and clock
# ---------------------------------------------------------------------------

class Paths:
    def __init__(self, ws: Path):
        self.ws = ws
        self.mod = ws / "followup_mod"
        self.config = self.mod / "config.json"
        self.voice = self.mod / "VOICE.md"
        self.learned = self.mod / "learned"
        self.edits = self.learned / "edits.jsonl"
        self.contacts = self.learned / "contacts.json"
        self.db = ws / "followup.db"
        self.snapshots = ws / "snapshots"
        self.views = ws / "views"
        self.pane = self.views / "pane.json"
        self.xlsx = ws / "Follow-ups.xlsx"
        self.html = ws / "Follow-ups.html"


class Clock:
    """The run's notion of today. ``forced`` comes from --today; otherwise the local date."""

    def __init__(self, forced: Optional[date] = None):
        self.forced = forced

    def today(self) -> date:
        return self.forced or date.today()

    def now(self) -> datetime:
        n = datetime.now().astimezone().replace(microsecond=0)
        if self.forced is not None:
            n = n.replace(year=self.forced.year, month=self.forced.month, day=self.forced.day)
        return n

    def now_iso(self) -> str:
        return self.now().isoformat()


# ---------------------------------------------------------------------------
# Dates and formatting
# ---------------------------------------------------------------------------

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]
MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")


def parse_date(value: Any) -> Optional[date]:
    """ISO date or datetime string -> date. Anything else -> None (dates are never guessed)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not _ISO_RE.match(text):
        return None
    try:
        return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def is_iso_date(value: Any) -> bool:
    return isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()) is not None and parse_date(value) is not None


def is_iso_datetime(value: Any) -> bool:
    return isinstance(value, str) and parse_date(value) is not None


def add_months(d: date, months: int) -> date:
    """Calendar-month arithmetic with clamping (Aug 31 - 6 months = Feb 28/29)."""
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def fmt_long(d: date) -> str:
    return "%s %d, %d" % (MONTH_NAMES[d.month - 1], d.day, d.year)


def fmt_short(d: date) -> str:
    return "%s %d, %d" % (MONTH_ABBR[d.month - 1], d.day, d.year)


def date_forms(d: Optional[date]) -> List[str]:
    if d is None:
        return []
    return [d.isoformat(), fmt_long(d), fmt_short(d), "%s %d" % (MONTH_NAMES[d.month - 1], d.day),
            "%s %d" % (MONTH_ABBR[d.month - 1], d.day)]


def fmt_money(amount: Optional[float]) -> str:
    if amount is None:
        return ""
    sign = "-" if amount < 0 else ""
    return "%s$%s" % (sign, "{:,.2f}".format(abs(float(amount))))


def plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=False)


def jload(text: Any, default: Any) -> Any:
    if text is None or text == "":
        return copy.deepcopy(default)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return copy.deepcopy(default)


def split_emails(value: Any) -> List[str]:
    if not value:
        return []
    return [e.strip().lower() for e in re.split(r"[,;\s]+", str(value)) if "@" in e]


def first_email(value: Any) -> str:
    emails = split_emails(value)
    return emails[0] if emails else ""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(
  id TEXT PRIMARY KEY,
  qb_id TEXT, name TEXT, company TEXT, email TEXT, phone TEXT, type TEXT,
  source TEXT,
  do_not_contact INTEGER DEFAULT 0, notes TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS documents(
  id TEXT PRIMARY KEY,
  kind TEXT, number TEXT, customer_id TEXT, txn_date TEXT, due_date TEXT, expiry_date TEXT,
  amount REAL, balance REAL, status TEXT, memo TEXT, run_id TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS threads(
  id TEXT PRIMARY KEY,
  customer_id TEXT, email TEXT, subject TEXT, last_at TEXT, last_from TEXT, last_message_id TEXT,
  snippet TEXT, message_count INTEGER, participants TEXT, inquiry INTEGER,
  suspicious INTEGER DEFAULT 0, url TEXT, run_id TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS followups(
  id TEXT PRIMARY KEY,
  run_id TEXT, customer_id TEXT, kind TEXT, reason TEXT, priority REAL, tier TEXT,
  evidence TEXT,
  state TEXT, items TEXT, problems TEXT,
  draft_subject TEXT, draft_body TEXT, draft_to TEXT, revise_instruction TEXT,
  gmail_draft_id TEXT, gmail_draft_url TEXT, staged_at TEXT,
  sent_message_id TEXT, sent_at TEXT, replied_at TEXT, reply_snippet TEXT,
  outcome TEXT, outcome_note TEXT, later_date TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, followup_id TEXT, customer_id TEXT, kind TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, today TEXT, started_at TEXT, finished_at TEXT, source_qb TEXT, source_mail TEXT,
  flagged INTEGER, drafted INTEGER, notes TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_documents_customer ON documents(customer_id);
CREATE INDEX IF NOT EXISTS idx_threads_customer ON threads(customer_id);
CREATE INDEX IF NOT EXISTS idx_followups_customer ON followups(customer_id);
CREATE INDEX IF NOT EXISTS idx_events_followup ON events(followup_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


# ---------------------------------------------------------------------------
# Events and follow-ups
# ---------------------------------------------------------------------------

def add_event(conn: sqlite3.Connection, at: str, followup_id: Optional[str], customer_id: Optional[str],
              kind: str, note: str = "") -> None:
    conn.execute("INSERT INTO events(at, followup_id, customer_id, kind, note) VALUES (?, ?, ?, ?, ?)",
                 (at, followup_id, customer_id, kind, note))


_FU_JSON = {"evidence": {}, "items": [], "problems": []}


def decode_followup(row: Any) -> Dict[str, Any]:
    d = dict(row)
    for key, default in _FU_JSON.items():
        d[key] = jload(d.get(key), default)
    return d


def get_followup(conn: sqlite3.Connection, fid: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM followups WHERE id = ?", (fid,)).fetchone()
    return decode_followup(row) if row else None


def all_followups(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM followups" + ((" WHERE " + where) if where else "") + " ORDER BY created_at, id"
    return [decode_followup(r) for r in conn.execute(sql, params).fetchall()]


def update_followup(conn: sqlite3.Connection, fid: str, at: str, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for key, value in fields.items():
        if key in _FU_JSON and not isinstance(value, (str, type(None))):
            value = jdump(value)
        cols.append("%s = ?" % key)
        vals.append(value)
    cols.append("updated_at = ?")
    vals.append(at)
    vals.append(fid)
    conn.execute("UPDATE followups SET %s WHERE id = ?" % ", ".join(cols), vals)


def set_state(conn: sqlite3.Connection, at: str, fu: Dict[str, Any], new_state: str, note: str = "",
              **fields: Any) -> None:
    old = fu.get("state")
    update_followup(conn, fu["id"], at, state=new_state, **fields)
    text = "%s → %s" % (old or "-", new_state)
    if note:
        text += ": " + note
    add_event(conn, at, fu["id"], fu.get("customer_id"), "state:" + new_state, text)
    fu["state"] = new_state
    fu.update(fields)


def next_followup_id(conn: sqlite3.Connection, today: date) -> str:
    prefix = "fu-%s-" % today.strftime("%Y%m%d")
    best = 0
    for row in conn.execute("SELECT id FROM followups WHERE id LIKE ?", (prefix + "%",)):
        tail = row["id"][len(prefix):]
        if tail.isdigit():
            best = max(best, int(tail))
    return "%s%d" % (prefix, best + 1)


def get_customer(conn: sqlite3.Connection, cid: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    return dict(row) if row else None


def display_name(customer: Optional[Dict[str, Any]]) -> str:
    if not customer:
        return "Unknown customer"
    return (customer.get("company") or customer.get("name") or customer.get("email") or customer.get("id") or "").strip()


def get_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def latest_run(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def current_doc_filter(conn: sqlite3.Connection) -> tuple:
    """Open documents are trusted only from the latest QuickBooks snapshot (stale rows drop out)."""
    qb_run = get_meta(conn, "last_qb_run")
    if qb_run:
        return " AND run_id = ?", (qb_run,)
    return "", ()


def open_estimates(conn: sqlite3.Connection, customer_id: Optional[str] = None) -> List[Dict[str, Any]]:
    extra, params = current_doc_filter(conn)
    sql = "SELECT * FROM documents WHERE kind = 'estimate' AND lower(status) = 'pending'" + extra
    if customer_id is not None:
        sql += " AND customer_id = ?"
        params = params + (customer_id,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY txn_date, number", params).fetchall()]


def open_invoices(conn: sqlite3.Connection, customer_id: Optional[str] = None) -> List[Dict[str, Any]]:
    extra, params = current_doc_filter(conn)
    sql = "SELECT * FROM documents WHERE kind = 'invoice' AND balance > 0" + extra
    if customer_id is not None:
        sql += " AND customer_id = ?"
        params = params + (customer_id,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY due_date, number", params).fetchall()]


def days_overdue(invoice: Dict[str, Any], today: date) -> Optional[int]:
    due = parse_date(invoice.get("due_date"))
    if due is None:
        return None
    return (today - due).days
