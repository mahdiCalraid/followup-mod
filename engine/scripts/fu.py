#!/usr/bin/env python3
"""Follow-up engine CLI. Every command prints exactly one JSON object.

    python3 <ws>/followup_mod/scripts/fu.py <command> [args] [--ws PATH] [--today YYYY-MM-DD]

Standard library only, Python 3.9+, macOS and Windows. No network; never sends anything.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fu_checks  # noqa: E402
import fu_db  # noqa: E402
import fu_demo  # noqa: E402
import fu_export  # noqa: E402
import fu_rules  # noqa: E402
from fu_db import parse_date  # noqa: E402

REPLY_OPTIONS = ["Interested", "Later", "Not interested", "Won / paid", "No real answer"]
TIER_GUIDANCE = {
    "friendly": "1-14 days late: a friendly reminder. Assume good faith (it may have been missed), quote the open "
                "balance, the invoice number and the due date, and offer to resend the invoice.",
    "second": "15-30 days late: a second reminder. Polite and direct; quote the balance, invoice number and due date, "
              "and ask them to check on it.",
    "firm": "31+ days late: a firm reminder. Stay polite and factual, state the open balance and due date, and ask "
            "for a payment date (the body must ask \"when\" or for a \"date\").",
}
KIND_GUIDANCE = {
    "quote_quiet": "A quote has gone quiet. Check in briefly, offer to answer questions or adjust it. No pressure.",
    "unanswered_inbound": "They wrote last and are waiting on us. Apologize briefly for the slow reply and answer or "
                          "move their request forward.",
    "silent_after_proposal": "We sent a quote or proposal and heard nothing. A short, friendly nudge; ask if they "
                             "want to go ahead, wait, or change anything.",
    "dormant_customer": "A past customer gone quiet for months. A warm hello; offer something useful (a new item, "
                        "the current list). No guilt.",
    "old_conversation": "A conversation that went quiet. A short, low-pressure check-in.",
}


class CliError(Exception):
    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.extra = extra


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise CliError("%s (usage: %s)" % (message, self.format_usage().strip()))


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def resolve_ws(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("FOLLOWUP_WS")
    if env:
        return Path(env).expanduser().resolve()
    guess = Path(__file__).resolve().parents[2]
    if (guess / "followup_mod" / "config.json").exists():
        return guess
    return Path.cwd().resolve()


def parse_day(value: Optional[str], what: str) -> Optional[date]:
    if value is None:
        return None
    if not fu_db.is_iso_date(value):
        raise CliError("%s must be a date like 2026-11-25, got %r." % (what, value))
    return parse_date(value)


class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.paths = fu_db.Paths(resolve_ws(getattr(args, "ws", None)))
        self.cfg = fu_db.load_config(self.paths.config)
        self.forced = parse_day(getattr(args, "today", None), "--today")
        self.clock = fu_db.Clock(self.forced)
        self._conn = None
        self.refresh_mode: Optional[str] = None  # None, "pane" or "full"

    def conn(self, create: bool = False):
        if self._conn is None:
            if not self.paths.db.exists():
                if not create:
                    raise CliError("No followup.db in %s. Run `fu.py init` first (or pass --ws)." % self.paths.ws)
                self.paths.ws.mkdir(parents=True, exist_ok=True)
            self._conn = fu_db.connect(self.paths.db)
            fu_db.init_db(self._conn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def now(self) -> str:
        return self.clock.now_iso()

    def today(self) -> date:
        return self.clock.today()

    def view_today(self) -> date:
        if self.forced:
            return self.forced
        run = fu_db.latest_run(self.conn())
        return (parse_date(run["today"]) if run else None) or date.today()

    def view_cfg(self) -> Dict[str, Any]:
        cfg = dict(self.cfg)
        if not cfg.get("business_name"):
            run = fu_db.latest_run(self.conn())
            if run and run.get("source_qb") == "demo":
                cfg["business_name"] = fu_demo.DEMO_BUSINESS
        return cfg

    def changed(self, mode: str = "full") -> None:
        if mode == "full" or self.refresh_mode is None:
            self.refresh_mode = mode

    def refresh(self) -> Tuple[Dict[str, str], List[str]]:
        self.paths.views.mkdir(parents=True, exist_ok=True)
        return fu_export.refresh(self.conn(), self.paths, self.view_cfg(), self.view_today(), self.now(),
                                 full=self.refresh_mode == "full")


def require_followup(ctx: Ctx, fid: str) -> Dict[str, Any]:
    fu = fu_db.get_followup(ctx.conn(), fid)
    if fu is None:
        raise CliError("No follow-up %s." % fid)
    return fu


def require_state(fu: Dict[str, Any], allowed: Tuple[str, ...], action: str) -> None:
    if fu["state"] not in allowed:
        raise CliError("Can't %s %s: it is %s (allowed from %s)." % (action, fu["id"], fu["state"], ", ".join(allowed)))


def require_run(ctx: Ctx, run_id: str) -> Dict[str, Any]:
    run = fu_db.get_run(ctx.conn(), run_id)
    if run is None:
        raise CliError("No run %s." % run_id)
    return run


def view_of(ctx: Ctx, fu: Dict[str, Any]) -> Dict[str, Any]:
    conn = ctx.conn()
    customer = fu_db.get_customer(conn, fu["customer_id"])
    url = None
    tid = (fu.get("evidence") or {}).get("thread_id")
    if tid:
        row = conn.execute("SELECT url FROM threads WHERE id = ?", (tid,)).fetchone()
        url = row["url"] if row else None
    return fu_export.followup_view(fu, customer, url)


def base_problems(fu: Dict[str, Any]) -> List[str]:
    ev = fu.get("evidence") or {}
    return [fu_rules.SUSPICIOUS_PROBLEM] if (ev.get("suspicious") or ev.get("reply_suspicious")) else []


def run_check(ctx: Ctx, fu: Dict[str, Any]) -> List[str]:
    customer = fu_db.get_customer(ctx.conn(), fu["customer_id"]) or {}
    return fu_checks.check_draft(fu.get("draft_subject"), fu.get("draft_body"), fu.get("draft_to"), fu.get("evidence") or {},
                                 fu.get("tier"), customer.get("email"), ctx.cfg["draft"])


def next_item_id(items: List[Dict[str, Any]]) -> str:
    best = 0
    for it in items:
        tail = str(it.get("id", ""))[1:]
        if tail.isdigit():
            best = max(best, int(tail))
    return "n%d" % (best + 1)


def resolved(item: Dict[str, Any]) -> bool:
    if item.get("type") == "task":
        return bool(item.get("done"))
    return item.get("answer") not in (None, "")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    ctx.conn(create=True)
    ctx.paths.views.mkdir(parents=True, exist_ok=True)
    ctx.changed("pane")
    return {"workspace": str(ctx.paths.ws), "db": str(ctx.paths.db), "views": str(ctx.paths.views),
            "config_found": ctx.paths.config.exists()}


def start_run(ctx: Ctx, qb: Optional[str], mail: Optional[str]) -> Dict[str, Any]:
    conn = ctx.conn()
    today = ctx.today()
    stamp = datetime.combine(today, datetime.now().time().replace(microsecond=0))
    run_id = "r-%s" % stamp.strftime("%Y%m%d-%H%M%S")
    while conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() or (ctx.paths.snapshots / run_id).exists():
        stamp += timedelta(seconds=1)
        run_id = "r-%s" % stamp.strftime("%Y%m%d-%H%M%S")
    folder = ctx.paths.snapshots / run_id
    folder.mkdir(parents=True, exist_ok=True)
    with conn:
        conn.execute("INSERT INTO runs(id, today, started_at, source_qb, source_mail, flagged, drafted) VALUES (?,?,?,?,?,0,0)",
                     (run_id, today.isoformat(), ctx.now(), qb, mail))
        fu_db.add_event(conn, ctx.now(), None, None, "run", "run %s started (today %s)" % (run_id, today.isoformat()))
    ctx.changed("full")
    return {"run_id": run_id, "today": today.isoformat(), "snapshot_dir": str(folder)}


def cmd_run_start(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    return start_run(ctx, a.qb, a.mail)


def ingest_run(ctx: Ctx, run: Dict[str, Any]) -> Dict[str, Any]:
    folder = ctx.paths.snapshots / run["id"]
    files = [(name, folder / name) for name in ("quickbooks.json", "mail.json") if (folder / name).exists()]
    if not files:
        raise CliError("No quickbooks.json or mail.json in %s." % folder)
    problems: List[str] = []
    warnings: List[str] = []
    loaded: Dict[str, Any] = {}
    for name, path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            problems.append("%s is not valid JSON (%s)." % (name, exc))
            continue
        checker = fu_checks.check_qb_snapshot if name == "quickbooks.json" else fu_checks.check_mail_snapshot
        p, w = checker(data)
        problems += p
        warnings += w
        loaded[name] = data
    if problems:
        raise CliError("The snapshot check failed; nothing was written.", problems=problems, warnings=warnings)
    conn = ctx.conn()
    counts: Dict[str, Any] = {}
    at = ctx.now()
    with conn:
        if "quickbooks.json" in loaded:
            counts["quickbooks"] = fu_rules.ingest_qb(conn, loaded["quickbooks.json"], run["id"], at)
            if not run.get("source_qb"):
                conn.execute("UPDATE runs SET source_qb = ? WHERE id = ?", (loaded["quickbooks.json"].get("source"), run["id"]))
        if "mail.json" in loaded:
            mc, mw = fu_rules.ingest_mail(conn, loaded["mail.json"], run["id"], at, ctx.cfg, ctx.paths)
            counts["mail"] = mc
            warnings += mw
            if not run.get("source_mail"):
                conn.execute("UPDATE runs SET source_mail = ? WHERE id = ?", (loaded["mail.json"].get("source"), run["id"]))
        fu_db.add_event(conn, at, None, None, "ingest", "run %s: %s" % (run["id"], ", ".join(n for n, _p in files)))
    ctx.changed("full")
    return {"run_id": run["id"], "files": [n for n, _p in files], "counts": counts, "warnings": warnings}


def cmd_ingest(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    return ingest_run(ctx, require_run(ctx, a.run))


def cmd_mail_targets(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    run = require_run(ctx, a.run)
    today = parse_date(run["today"]) or ctx.today()
    out = fu_rules.mail_targets(ctx.conn(), ctx.cfg, today, batch=a.batch)
    out["run_id"] = run["id"]
    return out


def rank_run(ctx: Ctx, run: Dict[str, Any]) -> Dict[str, Any]:
    conn = ctx.conn()
    today = parse_date(run["today"]) or ctx.today()
    with conn:
        result = fu_rules.rank(conn, ctx.cfg, run["id"], today, ctx.now())
    ctx.changed("full")
    views = [view_of(ctx, fu_db.get_followup(conn, fid)) for fid in result["created"]]
    return {"run_id": run["id"], "today": today.isoformat(), "flagged": len(views),
            "drafts_needed": sum(1 for v in views if v["state"] == "found"),
            "calls": sum(1 for v in views if v["state"] == "call"),
            "followups": views, "suppressed": result["suppressed"], "over_cap": result["over_cap"]}


def cmd_rank(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    return rank_run(ctx, require_run(ctx, a.run))


def cmd_show(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    conn = ctx.conn()
    fu = require_followup(ctx, a.id)
    ev = fu.get("evidence") or {}
    customer = fu_db.get_customer(conn, fu["customer_id"])
    thread = None
    if ev.get("thread_id"):
        row = conn.execute("SELECT * FROM threads WHERE id = ?", (ev["thread_id"],)).fetchone()
        if row:
            thread = dict(row)
            thread["participants"] = fu_db.jload(thread.get("participants"), [])
    docs = []
    for did in ev.get("documents") or []:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
        if row:
            docs.append(dict(row))
    voice = ""
    if ctx.paths.voice.exists():
        voice = ctx.paths.voice.read_text(encoding="utf-8-sig")
    edits: List[Any] = []
    if ctx.paths.edits.exists():
        for line in ctx.paths.edits.read_text(encoding="utf-8-sig").splitlines()[-8:]:
            try:
                edits.append(json.loads(line))
            except ValueError:
                pass
    tier = fu.get("tier")
    guidance = TIER_GUIDANCE.get(tier or "") or KIND_GUIDANCE.get(fu["kind"], "")
    sender = ctx.cfg.get("sender_name") or ctx.view_cfg().get("business_name") or ""
    return {
        "followup": view_of(ctx, fu), "customer": customer, "evidence": ev, "thread": thread, "documents": docs,
        "tier": tier, "tier_label": fu_db.TIER_LABELS.get(tier) if tier else None, "guidance": guidance,
        "voice": voice, "voice_missing": not bool(voice.strip()), "learned_edits": edits,
        "revise_instruction": fu.get("revise_instruction"), "items": fu.get("items") or [],
        "draft_rules": {"to": fu.get("draft_to") or ev.get("email"), "max_body_chars": ctx.cfg["draft"]["max_body_chars"],
                        "max_subject_chars": ctx.cfg["draft"]["max_subject_chars"],
                        "banned_phrases": ctx.cfg["draft"]["banned_phrases"],
                        "firm_needs_date_ask": tier == "firm",
                        "allowed_amounts": ev.get("amounts") or [], "allowed_numbers": ev.get("numbers") or [],
                        "allowed_dates": ev.get("dates") or [], "days": ev.get("days")},
        "sender_name": sender, "business_name": ctx.view_cfg().get("business_name") or "",
    }


def cmd_todo_drafts(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    where, params = "state IN ('found','revise')", ()
    if a.run:
        where += " AND run_id = ?"
        params = (a.run,)
    items = []
    for fu in fu_db.all_followups(ctx.conn(), where, params):
        to = fu.get("draft_to") or (fu.get("evidence") or {}).get("email")
        if not to:
            continue
        v = view_of(ctx, fu)
        items.append({"id": fu["id"], "customer_name": v["customer_name"], "kind": fu["kind"], "kind_label": v["kind_label"],
                      "state": fu["state"], "tier": fu.get("tier"), "reason": fu["reason"], "to": to,
                      "revise_instruction": fu.get("revise_instruction")})
    return {"count": len(items), "items": items}


def store_draft(ctx: Ctx, fu: Dict[str, Any], subject: Optional[str], body: Optional[str], to: Optional[str],
                give_up: bool = False) -> Dict[str, Any]:
    conn = ctx.conn()
    at = ctx.now()
    fields: Dict[str, Any] = {}
    if subject is not None:
        fields["draft_subject"] = subject.strip()
    if body is not None:
        fields["draft_body"] = body.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    fields["draft_to"] = (to or fu.get("draft_to") or (fu.get("evidence") or {}).get("email") or "").strip() or None
    with conn:
        fu_db.update_followup(conn, fu["id"], at, **fields)
        fu.update(fields)
        problems = run_check(ctx, fu) if (subject is not None or body is not None) else ["No draft was written."]
        if not problems and not give_up:
            fu_db.set_state(conn, at, fu, "drafted", "draft passed the checks", problems=base_problems(fu))
            ctx.changed("full")
            return {"id": fu["id"], "passed": True, "state": "drafted", "problems": []}
        fu_db.add_event(conn, at, fu["id"], fu["customer_id"], "check:failed", "; ".join(problems))
        reset = conn.execute("SELECT max(id) AS m FROM events WHERE followup_id = ? AND kind IN ('state:found','state:revise')",
                             (fu["id"],)).fetchone()["m"] or 0
        failures = conn.execute("SELECT count(*) AS n FROM events WHERE followup_id = ? AND kind = 'check:failed' AND id > ?",
                                (fu["id"], reset)).fetchone()["n"]
        saved = base_problems(fu) + problems
        if give_up or failures >= 2:
            items = list(fu.get("items") or [])
            items.append({"id": next_item_id(items), "type": "task",
                          "text": "Claude couldn't write a draft that passes the checks. Edit it yourself, or tell Claude "
                                  "what to change. Problems: " + " ".join(problems),
                          "answer": None, "done": False})
            fu_db.set_state(conn, at, fu, "needs_you", "draft failed the checks" + (" (gave up)" if give_up else " twice"),
                            problems=saved, items=items)
        else:
            fu_db.update_followup(conn, fu["id"], at, problems=saved)
    ctx.changed("full")
    raise CliError("The draft didn't pass the checks.", id=fu["id"], passed=False, state=fu["state"], problems=problems)


def cmd_draft(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    require_state(fu, ("found", "revise", "drafted", "needs_you", "failed"), "draft")
    if fu["state"] == "call" or not (a.to or fu.get("draft_to") or (fu.get("evidence") or {}).get("email")):
        raise CliError("%s has no email address; it's a phone call." % fu["id"])
    body = None
    if a.body_file:
        path = Path(a.body_file).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            body = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise CliError("Couldn't read the body file %s (%s)." % (path, exc))
    if not a.give_up and (a.subject is None or body is None):
        raise CliError("draft needs --subject and --body-file (or --give-up).")
    return store_draft(ctx, fu, a.subject, body, a.to, give_up=a.give_up)


def cmd_check_draft(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    problems = run_check(ctx, fu)
    return {"ok": not problems, "id": fu["id"], "problems": problems}


def cmd_needs_you(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    require_state(fu, fu_db.OPEN_STATES + ("failed",), "ask about")
    items = list(fu.get("items") or [])
    if a.question:
        item: Dict[str, Any] = {"id": next_item_id(items), "type": "question", "text": a.question, "answer": None}
        if a.options:
            item["options"] = [o.strip() for o in a.options.split("|") if o.strip()]
        if a.key:
            item["key"] = a.key
    else:
        item = {"id": next_item_id(items), "type": "task", "text": a.task, "answer": None, "done": False}
    items.append(item)
    conn = ctx.conn()
    with conn:
        fu_db.set_state(conn, ctx.now(), fu, "needs_you", item["text"][:200], items=items)
    ctx.changed("full")
    return {"id": fu["id"], "item": item, "state": "needs_you"}


def _reply_outcome(value: str, kind: str) -> Optional[str]:
    v = value.strip().lower().replace("_", " ")
    mapping = {"interested": "interested", "later": "later", "not interested": "not_interested",
               "no real answer": "no_reply", "no reply": "no_reply", "won": "won", "paid": "paid",
               "do not contact": "do_not_contact"}
    if v in ("won / paid", "won/paid", "won or paid"):
        return "paid" if kind == "invoice_overdue" else "won"
    return mapping.get(v)


def apply_outcome(ctx: Ctx, fu: Dict[str, Any], outcome: str, when: Optional[date], note: Optional[str]) -> Dict[str, Any]:
    conn = ctx.conn()
    at = ctx.now()
    items = list(fu.get("items") or [])
    for it in items:
        if it.get("key") == "reply_outcome" and not resolved(it):
            it["answer"] = outcome
    with conn:
        if outcome == "later":
            if when is None:
                raise CliError("Outcome later needs --date YYYY-MM-DD (when to follow up again).")
            fu_db.set_state(conn, at, fu, "snoozed", "later, until %s" % when.isoformat(), outcome="later",
                            outcome_note=note, later_date=when.isoformat(), items=items)
            event_at = at
        else:
            fu_db.set_state(conn, at, fu, "closed", outcome.replace("_", " "), outcome=outcome, outcome_note=note, items=items)
            event_at = ("%sT12:00:00" % when.isoformat()) if when else at
        fu_db.add_event(conn, event_at, fu["id"], fu["customer_id"], "outcome:" + outcome, note or "")
        if outcome == "do_not_contact":
            conn.execute("UPDATE customers SET do_not_contact = 1, updated_at = ? WHERE id = ?", (at, fu["customer_id"]))
    ctx.changed("full")
    return {"id": fu["id"], "state": fu["state"], "outcome": outcome, "later_date": fu.get("later_date")}


def cmd_answer(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    items = list(fu.get("items") or [])
    item = next((it for it in items if str(it.get("id")) == a.item_id), None)
    if item is None:
        raise CliError("%s has no item %s." % (fu["id"], a.item_id))
    value = a.value
    if item.get("type") == "task":
        if value.strip().lower() == "done":
            item["done"] = True
        else:
            item["answer"] = value
    else:
        item["answer"] = value
    conn = ctx.conn()
    at = ctx.now()
    if item.get("key") == "reply_outcome" and fu["state"] == "replied":
        outcome = _reply_outcome(value, fu["kind"])
        if outcome is None:
            raise CliError("Answer one of: %s." % ", ".join(REPLY_OPTIONS))
        with conn:
            fu_db.update_followup(conn, fu["id"], at, items=items)
            fu_db.add_event(conn, at, fu["id"], fu["customer_id"], "answer", "%s: %s" % (item["id"], value))
        fu["items"] = items
        later = ctx.today() + timedelta(days=ctx.cfg["rules"]["skip_cooldown_days"]) if outcome == "later" else None
        out = apply_outcome(ctx, fu, outcome, later, "from the reply question")
        out["item"] = item
        return out
    with conn:
        fu_db.add_event(conn, at, fu["id"], fu["customer_id"], "answer", "%s: %s" % (item["id"], value))
        if fu["state"] == "needs_you" and all(resolved(it) for it in items):
            fu_db.set_state(conn, at, fu, "revise", "every question answered", items=items)
        else:
            fu_db.update_followup(conn, fu["id"], at, items=items)
    ctx.changed("full")
    return {"id": fu["id"], "item": item, "state": fu["state"], "all_resolved": all(resolved(it) for it in items)}


def cmd_approve(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    conn = ctx.conn()
    if a.all_drafted:
        targets = fu_db.all_followups(conn, "state = 'drafted'")
    elif a.id:
        fu = require_followup(ctx, a.id)
        require_state(fu, ("drafted",), "approve")
        targets = [fu]
    else:
        raise CliError("approve needs an ID or --all-drafted.")
    approved, refused = [], []
    at = ctx.now()
    with conn:
        for fu in targets:
            problems = run_check(ctx, fu)
            if problems:
                fu_db.update_followup(conn, fu["id"], at, problems=base_problems(fu) + problems)
                refused.append({"id": fu["id"], "problems": problems})
                continue
            fu_db.set_state(conn, at, fu, "approved")
            approved.append(fu["id"])
    ctx.changed("full")
    if a.id and refused:
        raise CliError("The draft no longer passes the checks; not approved.", problems=refused[0]["problems"])
    return {"approved": approved, "not_approved": refused}


def cmd_revise(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    require_state(fu, ("found", "drafted", "approved", "needs_you", "revise", "failed"), "revise")
    at = ctx.now()
    ctx.paths.learned.mkdir(parents=True, exist_ok=True)
    record = {"at": at, "followup_id": fu["id"], "kind": fu["kind"], "instruction": a.instruction,
              "before_subject": fu.get("draft_subject"), "before_body": fu.get("draft_body")}
    with open(str(ctx.paths.edits), "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    conn = ctx.conn()
    with conn:
        fu_db.set_state(conn, at, fu, "revise", a.instruction[:200], revise_instruction=a.instruction)
    ctx.changed("full")
    return {"id": fu["id"], "state": "revise", "instruction": a.instruction, "edits_file": str(ctx.paths.edits)}


def cmd_skip(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    require_state(fu, fu_db.OPEN_STATES + ("failed", "snoozed"), "skip")
    conn = ctx.conn()
    with conn:
        fu_db.set_state(conn, ctx.now(), fu, "skipped", a.note or "", outcome_note=a.note)
    ctx.changed("full")
    return {"id": fu["id"], "state": "skipped"}


def cmd_snooze(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    require_state(fu, fu_db.OPEN_STATES + ("failed", "sent", "replied", "snoozed"), "snooze")
    until = parse_day(a.date, "DATE")
    conn = ctx.conn()
    with conn:
        fu_db.set_state(conn, ctx.now(), fu, "snoozed", "until %s" % until.isoformat(), later_date=until.isoformat())
    ctx.changed("full")
    return {"id": fu["id"], "state": "snoozed", "later_date": until.isoformat()}


def cmd_dnc(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    out = apply_outcome(ctx, fu, "do_not_contact", None, "marked do not contact")
    out["customer_id"] = fu["customer_id"]
    return out


def cmd_stage_list(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    items = []
    for fu in fu_db.all_followups(ctx.conn(), "state = 'approved'"):
        ev = fu.get("evidence") or {}
        items.append({"id": fu["id"], "to": fu.get("draft_to"), "subject": fu.get("draft_subject"), "body": fu.get("draft_body"),
                      "reply_to_message_id": ev.get("reply_to_message_id"), "thread_id": ev.get("thread_id"),
                      "customer_name": view_of(ctx, fu)["customer_name"]})
    return {"count": len(items), "items": items}


def cmd_staged(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    require_state(fu, ("approved", "staged"), "mark staged")
    conn = ctx.conn()
    at = ctx.now()
    with conn:
        fu_db.set_state(conn, at, fu, "staged", "Gmail draft %s" % a.draft_id, gmail_draft_id=a.draft_id,
                        gmail_draft_url=a.url, staged_at=at)
    ctx.changed("full")
    return {"id": fu["id"], "state": "staged", "gmail_draft_id": a.draft_id}


def cmd_check_list(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    items = []
    for fu in fu_db.all_followups(ctx.conn(), "state IN ('staged','sent')"):
        ev = fu.get("evidence") or {}
        items.append({"id": fu["id"], "email": fu.get("draft_to"), "subject": fu.get("draft_subject"),
                      "staged_at": fu.get("staged_at"), "sent_at": fu.get("sent_at"), "thread_id": ev.get("thread_id"),
                      "state": fu["state"], "gmail_draft_id": fu.get("gmail_draft_id"),
                      "sent_message_id": fu.get("sent_message_id")})
    return {"count": len(items), "items": items}


def _iso_at(value: str, what: str) -> str:
    if not fu_db.is_iso_datetime(value):
        raise CliError("%s must be an ISO date or time like 2026-11-25T14:02:00Z, got %r." % (what, value))
    return value


def cmd_sent(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    require_state(fu, ("staged", "approved", "sent"), "mark sent")
    at = _iso_at(a.at, "--at")
    conn = ctx.conn()
    with conn:
        fu_db.set_state(conn, ctx.now(), fu, "sent", "message %s" % a.message_id, sent_message_id=a.message_id, sent_at=at)
    ctx.changed("full")
    return {"id": fu["id"], "state": "sent", "sent_at": at}


def cmd_replied(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    require_state(fu, ("sent", "staged", "replied"), "mark replied")
    at = _iso_at(a.at, "--at")
    items = list(fu.get("items") or [])
    if not any(it.get("key") == "reply_outcome" and not resolved(it) for it in items):
        items.append({"id": next_item_id(items), "type": "question", "text": "What did they say?",
                      "options": list(REPLY_OPTIONS), "key": "reply_outcome", "answer": None})
    ev = dict(fu.get("evidence") or {})
    fields: Dict[str, Any] = {"replied_at": at, "reply_snippet": a.snippet, "items": items}
    if fu_rules.is_suspicious(a.snippet):
        ev["reply_suspicious"] = True
        fields["evidence"] = ev
        fu["evidence"] = ev
        fields["problems"] = base_problems(fu)
    conn = ctx.conn()
    with conn:
        fu_db.set_state(conn, ctx.now(), fu, "replied", "", **fields)
    ctx.changed("full")
    return {"id": fu["id"], "state": "replied", "replied_at": at, "items": items}


def cmd_outcome(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    fu = require_followup(ctx, a.id)
    outcome = a.outcome.strip().lower()
    if outcome not in fu_db.OUTCOMES:
        raise CliError("OUTCOME must be one of %s." % ", ".join(fu_db.OUTCOMES))
    return apply_outcome(ctx, fu, outcome, parse_day(a.date, "--date"), a.note)


def cmd_run_finish(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    run = require_run(ctx, a.run)
    conn = ctx.conn()
    with conn:
        drafted = conn.execute(
            "SELECT count(*) AS n FROM followups WHERE run_id = ? AND draft_body IS NOT NULL AND draft_body != '' AND "
            "state IN ('drafted','approved','staged','sent','replied','closed')", (run["id"],)).fetchone()["n"]
        flagged = conn.execute("SELECT count(*) AS n FROM followups WHERE run_id = ?", (run["id"],)).fetchone()["n"]
        conn.execute("UPDATE runs SET finished_at = ?, drafted = ?, flagged = ? WHERE id = ?", (ctx.now(), drafted, flagged, run["id"]))
        fu_db.add_event(conn, ctx.now(), None, None, "run", "run %s finished: %d flagged, %d drafted" % (run["id"], flagged, drafted))
    ctx.changed("full")
    return {"run_id": run["id"], "flagged": flagged, "drafted": drafted}


def cmd_list(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    where, params = [], []
    if a.state:
        where.append("state = ?")
        params.append(a.state)
    if a.kind:
        where.append("kind = ?")
        params.append(a.kind)
    fus = fu_db.all_followups(ctx.conn(), " AND ".join(where), tuple(params))
    return {"count": len(fus), "items": [view_of(ctx, fu) for fu in fus]}


def cmd_customers(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    snap = fu_export.Snapshot(ctx.conn(), ctx.cfg, ctx.view_today())
    rows = fu_export.customer_views(snap)
    return {"count": len(rows), "items": rows}


def cmd_views(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    ctx.conn()
    ctx.changed("pane")
    return {}


def cmd_export(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    ctx.conn()
    ctx.changed("full")
    return {}


def cmd_demo(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    conn = ctx.conn(create=True)
    ctx.paths.views.mkdir(parents=True, exist_ok=True)
    run = start_run(ctx, "demo", "demo")
    today = date.fromisoformat(run["today"])
    qb, mail, dnc_emails = fu_demo.build(today)
    folder = Path(run["snapshot_dir"])
    for name, data in (("quickbooks.json", qb), ("mail.json", mail)):
        with open(str(folder / name), "w", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    ingest = ingest_run(ctx, fu_db.get_run(conn, run["run_id"]))
    with conn:
        for email in dnc_emails:
            conn.execute("UPDATE customers SET do_not_contact = 1 WHERE lower(email) = ?", (email.lower(),))
    ranked = rank_run(ctx, fu_db.get_run(conn, run["run_id"]))
    drafts = []
    if a.with_drafts:
        sender = ctx.cfg.get("sender_name") or fu_demo.DEMO_BUSINESS
        docs = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM documents")}
        for v in ranked["followups"]:
            fu = fu_db.get_followup(conn, v["id"])
            if fu["state"] != "found":
                continue
            customer = fu_db.get_customer(conn, fu["customer_id"]) or {}
            subject, body = fu_demo.template_draft(fu, customer, docs, sender)
            try:
                drafts.append(store_draft(ctx, fu, subject, body, None))
            except CliError as exc:
                drafts.append({"id": fu["id"], "passed": False, "problems": exc.extra.get("problems", [str(exc)])})
    finish = cmd_run_finish(ctx, argparse.Namespace(run=run["run_id"]))
    return {"run_id": run["run_id"], "today": run["today"], "snapshot_dir": run["snapshot_dir"],
            "ingest": ingest["counts"], "ingest_warnings": ingest["warnings"],
            "flagged": finish["flagged"], "drafted": finish["drafted"],
            "followups": [{"id": v["id"], "customer": v["customer_name"], "kind": v["kind"], "tier": v["tier"],
                           "state": (fu_db.get_followup(conn, v["id"]) or {}).get("state"), "reason": v["reason"]}
                          for v in ranked["followups"]],
            "drafts": drafts, "suppressed": ranked["suppressed"], "over_cap": ranked["over_cap"]}


def cmd_reset(ctx: Ctx, a: argparse.Namespace) -> Dict[str, Any]:
    if not a.yes:
        raise CliError("reset deletes followup.db, views/ and the two export files. Pass --yes to confirm.")
    ctx.close()
    deleted = []
    p = ctx.paths
    for path in (p.db, Path(str(p.db) + "-journal"), Path(str(p.db) + "-wal"), Path(str(p.db) + "-shm"), p.xlsx, p.html):
        if path.exists():
            path.unlink()
            deleted.append(str(path))
    if p.views.exists():
        shutil.rmtree(str(p.views))
        deleted.append(str(p.views))
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Parser and entry point
# ---------------------------------------------------------------------------

def build_parser() -> Parser:
    common = Parser(add_help=False)
    common.add_argument("--ws", default=argparse.SUPPRESS, help="workspace folder")
    common.add_argument("--today", default=argparse.SUPPRESS, help="the run's today, YYYY-MM-DD")
    p = Parser(prog="fu.py", description="Follow-up engine. Prints one JSON object.")
    p.add_argument("--ws", default=None, help="workspace folder")
    p.add_argument("--today", default=None, help="the run's today, YYYY-MM-DD")
    sub = p.add_subparsers(dest="command", metavar="command")
    sub.required = True

    def add(name: str, func: Any, help_text: str) -> Parser:
        sp = sub.add_parser(name, parents=[common], help=help_text)
        sp.set_defaults(func=func)
        return sp

    add("init", cmd_init, "create followup.db and views/")
    sp = add("run-start", cmd_run_start, "start a run")
    sp.add_argument("--qb", default=None)
    sp.add_argument("--mail", default=None)
    add("ingest", cmd_ingest, "check and load the run's snapshots").add_argument("--run", required=True)
    sp = add("mail-targets", cmd_mail_targets, "emails worth searching")
    sp.add_argument("--run", required=True)
    sp.add_argument("--batch", type=int, default=10)
    add("rank", cmd_rank, "apply the rules").add_argument("--run", required=True)
    add("show", cmd_show, "everything needed to draft one follow-up").add_argument("id")
    add("todo-drafts", cmd_todo_drafts, "follow-ups that still need a draft").add_argument("--run", default=None)
    sp = add("draft", cmd_draft, "store and check a draft")
    sp.add_argument("id")
    sp.add_argument("--subject", default=None)
    sp.add_argument("--body-file", default=None)
    sp.add_argument("--to", default=None)
    sp.add_argument("--give-up", action="store_true")
    add("check-draft", cmd_check_draft, "check the stored draft").add_argument("id")
    sp = add("needs-you", cmd_needs_you, "ask the owner a question or give them a task")
    sp.add_argument("id")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--question", default=None)
    g.add_argument("--task", default=None)
    sp.add_argument("--options", default=None)
    sp.add_argument("--key", default=None)
    sp = add("answer", cmd_answer, "answer an item")
    sp.add_argument("id")
    sp.add_argument("item_id")
    sp.add_argument("value")
    sp = add("approve", cmd_approve, "approve drafted follow-ups")
    sp.add_argument("id", nargs="?", default=None)
    sp.add_argument("--all-drafted", action="store_true")
    sp = add("revise", cmd_revise, "ask for a redraft")
    sp.add_argument("id")
    sp.add_argument("--instruction", required=True)
    sp = add("skip", cmd_skip, "skip a follow-up")
    sp.add_argument("id")
    sp.add_argument("--note", default=None)
    sp = add("snooze", cmd_snooze, "snooze until a date")
    sp.add_argument("id")
    sp.add_argument("date")
    add("dnc", cmd_dnc, "do not contact this customer").add_argument("id")
    add("stage-list", cmd_stage_list, "approved follow-ups to put in Gmail")
    sp = add("staged", cmd_staged, "a Gmail draft exists")
    sp.add_argument("id")
    sp.add_argument("--draft-id", required=True)
    sp.add_argument("--url", default=None)
    add("check-list", cmd_check_list, "staged and sent follow-ups to check")
    sp = add("sent", cmd_sent, "the owner sent it")
    sp.add_argument("id")
    sp.add_argument("--message-id", required=True)
    sp.add_argument("--at", required=True)
    sp = add("replied", cmd_replied, "they replied")
    sp.add_argument("id")
    sp.add_argument("--at", required=True)
    sp.add_argument("--snippet", default=None)
    sp = add("outcome", cmd_outcome, "record the outcome")
    sp.add_argument("id")
    sp.add_argument("outcome")
    sp.add_argument("--date", default=None)
    sp.add_argument("--note", default=None)
    add("run-finish", cmd_run_finish, "finish a run").add_argument("--run", required=True)
    sp = add("list", cmd_list, "list follow-ups")
    sp.add_argument("--state", default=None)
    sp.add_argument("--kind", default=None)
    add("customers", cmd_customers, "list customers")
    add("views", cmd_views, "refresh pane.json")
    add("export", cmd_export, "refresh pane.json, the xlsx and the html")
    sp = add("demo", cmd_demo, "load the fictional Brightwater Bakery dataset")
    sp.add_argument("--with-drafts", action="store_true")
    add("reset", cmd_reset, "delete the database and views (demo resets)").add_argument("--yes", action="store_true")
    return p


def run(argv: List[str]) -> Tuple[Dict[str, Any], int]:
    """Run one command; return (json object, exit code). Never raises."""
    ctx: Optional[Ctx] = None
    try:
        args = build_parser().parse_args(argv)
        ctx = Ctx(args)
        try:
            result = args.func(ctx, args)
            out: Dict[str, Any] = {"ok": True}
            out.update(result)
            ok = bool(out.get("ok"))
        except CliError as exc:
            out = {"ok": False, "error": str(exc)}
            out.update(exc.extra)
            ok = False
        if ctx.refresh_mode and ctx._conn is not None:
            try:
                files, warnings = ctx.refresh()
                out["views"] = files
                if args.command == "export":
                    out.update(files)
                if warnings:
                    out["warnings"] = list(out.get("warnings") or []) + warnings
            except Exception as exc:  # the data change already happened; report, don't hide it
                out.setdefault("warnings", []).append("Couldn't refresh the views: %s: %s" % (type(exc).__name__, exc))
        return out, 0 if ok else 1
    except CliError as exc:
        out = {"ok": False, "error": str(exc)}
        out.update(exc.extra)
        return out, 1
    except fu_db.ConfigError as exc:
        return {"ok": False, "error": str(exc)}, 1
    except Exception as exc:  # pragma: no cover - last-resort guard so the output is always JSON
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 1
    finally:
        if ctx is not None:
            ctx.close()


def main() -> None:
    out, code = run(sys.argv[1:])
    data = (json.dumps(out, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except AttributeError:  # stdout replaced by a text-only stream
        sys.stdout.write(data.decode("utf-8"))
    sys.exit(code)


if __name__ == "__main__":
    main()
