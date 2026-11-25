"""Views: views/pane.json (what the pane reads), Follow-ups.xlsx and Follow-ups.html.

The spreadsheet is hand-built OOXML (zipfile); the dashboard is one self-contained HTML file
with embedded data, vanilla JS and CSS, and no external requests.
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fu_db
from fu_db import parse_date

STATE_GROUP = {"needs_you": 0, "drafted": 1, "replied": 2, "call": 3, "approved": 4, "found": 5, "revise": 5}
SENT_STATES = ("staged", "sent", "replied", "closed")

# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _kind_index(kind: str, order: List[str]) -> int:
    return order.index(kind) if kind in order else len(order)


def followup_view(fu: Dict[str, Any], customer: Optional[Dict[str, Any]], thread_url: Optional[str]) -> Dict[str, Any]:
    customer = customer or {}
    tier = fu.get("tier")
    return {
        "id": fu["id"],
        "customer_id": fu.get("customer_id"),
        "customer_name": fu_db.display_name(customer) if customer else fu.get("customer_id"),
        "company": customer.get("company"),
        "email": fu_db.first_email(customer.get("email")) or (fu.get("evidence") or {}).get("email"),
        "phone": customer.get("phone"),
        "kind": fu.get("kind"),
        "kind_label": fu_db.KIND_LABELS.get(fu.get("kind") or "", fu.get("kind")),
        "reason": fu.get("reason"),
        "priority": round(float(fu.get("priority") or 0), 2),
        "state": fu.get("state"),
        "state_label": fu_db.STATE_LABELS.get(fu.get("state") or "", fu.get("state")),
        "tier": tier,
        "tier_label": fu_db.TIER_LABELS.get(tier) if tier else None,
        "facts": list((fu.get("evidence") or {}).get("facts") or []),
        "draft_subject": fu.get("draft_subject"),
        "draft_to": fu.get("draft_to"),
        "draft_body": fu.get("draft_body"),
        "problems": list(fu.get("problems") or []),
        "items": list(fu.get("items") or []),
        "gmail_draft_url": fu.get("gmail_draft_url"),
        "thread_url": thread_url,
        "staged_at": fu.get("staged_at"),
        "sent_at": fu.get("sent_at"),
        "replied_at": fu.get("replied_at"),
        "reply_snippet": fu.get("reply_snippet"),
        "outcome": fu.get("outcome"),
        "later_date": fu.get("later_date"),
        "updated_at": fu.get("updated_at"),
    }


class Snapshot:
    """Everything the views need, read once."""

    def __init__(self, conn: sqlite3.Connection, cfg: Dict[str, Any], today: date):
        self.today = today
        self.order = cfg["rules"]["order"]
        self.customers = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM customers")}
        self.thread_urls = {r["id"]: r["url"] for r in conn.execute("SELECT id, url FROM threads")}
        self.latest_thread: Dict[str, Dict[str, Any]] = {}
        for r in conn.execute("SELECT customer_id, last_at, last_from FROM threads ORDER BY last_at DESC"):
            self.latest_thread.setdefault(r["customer_id"], dict(r))
        self.followups = fu_db.all_followups(conn)
        self.views = [self.view(fu) for fu in self.followups]
        self.open_estimates = fu_db.open_estimates(conn)
        self.open_invoices = fu_db.open_invoices(conn)
        self.events = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 5000")]
        self.runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY started_at DESC, id DESC")]

    def view(self, fu: Dict[str, Any]) -> Dict[str, Any]:
        thread_id = (fu.get("evidence") or {}).get("thread_id")
        return followup_view(fu, self.customers.get(fu.get("customer_id")), self.thread_urls.get(thread_id))

    def sort_key(self, v: Dict[str, Any]) -> tuple:
        return (STATE_GROUP.get(v["state"], 9), _kind_index(v["kind"], self.order), -(v["priority"] or 0), v["id"])

    def overdue(self) -> List[Tuple[Dict[str, Any], int]]:
        out = []
        for inv in self.open_invoices:
            d = fu_db.days_overdue(inv, self.today)
            if d is not None and d >= 1:
                out.append((inv, d))
        return out


def customer_views(snap: Snapshot) -> List[Dict[str, Any]]:
    est_by: Dict[str, List[Dict[str, Any]]] = {}
    for e in snap.open_estimates:
        est_by.setdefault(e["customer_id"], []).append(e)
    overdue_by: Dict[str, float] = {}
    for inv, _d in snap.overdue():
        overdue_by[inv["customer_id"]] = overdue_by.get(inv["customer_id"], 0.0) + float(inv["balance"])
    sent_by: Dict[str, int] = {}
    outcome_by: Dict[str, Tuple[str, str]] = {}
    for fu in snap.followups:
        cid = fu["customer_id"]
        if fu.get("sent_at"):
            sent_by[cid] = sent_by.get(cid, 0) + 1
        if fu.get("outcome"):
            stamp = fu.get("updated_at") or ""
            if cid not in outcome_by or stamp >= outcome_by[cid][0]:
                outcome_by[cid] = (stamp, fu["outcome"])
    rows = []
    for cid, c in snap.customers.items():
        ests = est_by.get(cid, [])
        lt = snap.latest_thread.get(cid) or {}
        rows.append({
            "id": cid, "name": c.get("name"), "company": c.get("company"), "email": c.get("email"), "phone": c.get("phone"),
            "type": c.get("type"), "do_not_contact": bool(c.get("do_not_contact")),
            "open_estimates": len(ests), "open_estimate_amount": round(sum(float(e["amount"] or 0) for e in ests), 2),
            "overdue_balance": round(overdue_by.get(cid, 0.0), 2),
            "last_contact_at": lt.get("last_at"), "last_contact_from": lt.get("last_from"),
            "followups_sent": sent_by.get(cid, 0), "last_outcome": (outcome_by.get(cid) or (None, None))[1],
        })
    rows.sort(key=lambda r: (fu_db.display_name(snap.customers[r["id"]]).lower(), r["id"]))
    return rows


def build_pane(snap: Snapshot, paths: fu_db.Paths, cfg: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
    views = snap.views
    by_state: Dict[str, int] = {}
    for v in views:
        by_state[v["state"]] = by_state.get(v["state"], 0) + 1
    overdue = snap.overdue()
    today_states = ("drafted", "needs_you", "replied", "approved", "call", "found", "revise")
    today_items = sorted([v for v in views if v["state"] in today_states], key=snap.sort_key)
    pipeline = sorted([v for v in views if v["state"] in fu_db.OPEN_STATES], key=snap.sort_key)
    sent = sorted([v for v in views if v["state"] in SENT_STATES],
                  key=lambda v: (v.get("replied_at") or v.get("sent_at") or v.get("staged_at") or v.get("updated_at") or "", v["id"]),
                  reverse=True)[:100]
    last = snap.runs[0] if snap.runs else None
    return {
        "schema": "followup-pane/1",
        "updated_at": now_iso,
        "workspace": str(paths.ws.resolve()),
        "today": snap.today.isoformat(),
        "business_name": cfg.get("business_name") or "",
        "last_run": None if last is None else {k: last.get(k) for k in
                                               ("id", "today", "started_at", "finished_at", "source_qb", "source_mail", "flagged", "drafted")},
        "counts": {
            "review": by_state.get("drafted", 0), "needs_you": by_state.get("needs_you", 0),
            "approved": by_state.get("approved", 0), "staged": by_state.get("staged", 0),
            "sent": by_state.get("sent", 0), "replied": by_state.get("replied", 0), "call": by_state.get("call", 0),
            "customers": len(snap.customers),
            "open_estimates": len(snap.open_estimates),
            "open_estimate_amount": round(sum(float(e["amount"] or 0) for e in snap.open_estimates), 2),
            "overdue_invoices": len(overdue),
            "overdue_balance": round(sum(float(inv["balance"]) for inv, _d in overdue), 2),
        },
        "today_items": today_items,
        "pipeline": pipeline,
        "sent": sent,
        "customers": customer_views(snap),
        "files": {"xlsx": str(paths.xlsx.resolve()), "html": str(paths.html.resolve()), "db": str(paths.db.resolve())},
    }


# ---------------------------------------------------------------------------
# Tables shared by the spreadsheet and the dashboard
# ---------------------------------------------------------------------------

def _event_label(kind: str) -> str:
    if kind.startswith("state:"):
        return "Now: " + re.sub(r"^[^\w]+\s*", "", fu_db.STATE_LABELS.get(kind[6:], kind[6:]))
    if kind.startswith("outcome:"):
        return "Outcome: " + kind[8:].replace("_", " ")
    return kind.replace("_", " ").replace(":", ": ").capitalize()


def build_tables(snap: Snapshot, pane: Dict[str, Any]) -> List[Dict[str, Any]]:
    today = snap.today

    def fu_row(v: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": v["id"], "state": v["state_label"], "kind": v["kind_label"], "customer": v["customer_name"],
                "email": v["email"], "phone": v["phone"], "tier": v["tier_label"], "reason": v["reason"],
                "priority": v["priority"], "subject": v["draft_subject"], "draft": v["draft_body"],
                "problems": "; ".join(v["problems"]), "staged": v["staged_at"], "sent": v["sent_at"],
                "replied": v["replied_at"], "reply": v["reply_snippet"], "outcome": (v["outcome"] or "").replace("_", " "),
                "later": v["later_date"], "updated": v["updated_at"]}

    created = {fu["id"]: fu.get("created_at") for fu in snap.followups}
    all_rows = []
    for v in sorted(snap.views, key=lambda v: (created.get(v["id"]) or "", v["id"]), reverse=True):
        row = fu_row(v)
        row["created"] = created.get(v["id"])
        all_rows.append(row)
    cust_rows = []
    for c in pane["customers"]:
        cust_rows.append({"name": c["name"], "company": c["company"], "email": c["email"], "phone": c["phone"],
                          "type": c["type"], "dnc": "Yes" if c["do_not_contact"] else "No",
                          "open_estimates": c["open_estimates"], "open_estimate_amount": c["open_estimate_amount"],
                          "overdue_balance": c["overdue_balance"], "last_contact": c["last_contact_at"],
                          "last_from": c["last_contact_from"], "sent": c["followups_sent"],
                          "outcome": (c["last_outcome"] or "").replace("_", " ")})
    est_rows = []
    for e in snap.open_estimates:
        d = parse_date(e.get("txn_date"))
        est_rows.append({"number": e["number"], "customer": fu_db.display_name(snap.customers.get(e["customer_id"])),
                         "date": e.get("txn_date"), "expiry": e.get("expiry_date"), "amount": e["amount"],
                         "status": e.get("status"), "days": (today - d).days if d else None, "memo": e.get("memo")})
    inv_rows = []
    for inv in snap.open_invoices:
        d = fu_db.days_overdue(inv, today)
        status = "Due date unknown" if d is None else ("Overdue" if d >= 1 else "Not due yet")
        inv_rows.append({"number": inv["number"], "customer": fu_db.display_name(snap.customers.get(inv["customer_id"])),
                         "date": inv.get("txn_date"), "due": inv.get("due_date"), "amount": inv["amount"],
                         "balance": inv["balance"], "days": (max(d, 0) if d is not None else "due date unknown"),
                         "status": status, "memo": inv.get("memo")})
    inv_rows.sort(key=lambda r: (-(r["days"] if isinstance(r["days"], int) else -1), r["number"]))
    act_rows = []
    for ev in snap.events:
        act_rows.append({"at": ev.get("at"), "followup": ev.get("followup_id"),
                         "customer": fu_db.display_name(snap.customers.get(ev.get("customer_id"))) if ev.get("customer_id") else "",
                         "event": _event_label(ev.get("kind") or ""), "note": ev.get("note")})

    T, M, I, N, L = "text", "money", "int", "num", "long"
    fu_cols_review = [("Follow-up", "id", T), ("State", "state", T), ("Kind", "kind", T), ("Customer", "customer", T),
                      ("Email", "email", T), ("Phone", "phone", T), ("Tier", "tier", T), ("Reason", "reason", L),
                      ("Subject", "subject", T), ("Draft", "draft", L), ("Problems", "problems", L)]
    fu_cols_all = [("Follow-up", "id", T), ("Created", "created", T), ("State", "state", T), ("Kind", "kind", T),
                   ("Customer", "customer", T), ("Email", "email", T), ("Reason", "reason", L), ("Tier", "tier", T),
                   ("Priority", "priority", N), ("Subject", "subject", T), ("Staged", "staged", T), ("Sent", "sent", T),
                   ("Replied", "replied", T), ("Outcome", "outcome", T), ("Later date", "later", T), ("Updated", "updated", T)]
    sent_cols = [("Follow-up", "id", T), ("Customer", "customer", T), ("Email", "email", T), ("Kind", "kind", T),
                 ("Subject", "subject", T), ("State", "state", T), ("Staged", "staged", T), ("Sent", "sent", T),
                 ("Replied", "replied", T), ("Their reply", "reply", L), ("Outcome", "outcome", T)]
    return [
        {"id": "to-review", "name": "To review", "filter": "state", "columns": fu_cols_review,
         "rows": [fu_row(v) for v in pane["today_items"]]},
        {"id": "all-follow-ups", "name": "All follow-ups", "filter": "state", "columns": fu_cols_all, "rows": all_rows},
        {"id": "sent", "name": "Sent", "filter": "state", "columns": sent_cols, "rows": [fu_row(v) for v in pane["sent"]]},
        {"id": "customers", "name": "Customers", "filter": "type",
         "columns": [("Name", "name", T), ("Company", "company", T), ("Email", "email", T), ("Phone", "phone", T),
                     ("Type", "type", T), ("Do not contact", "dnc", T), ("Open estimates", "open_estimates", I),
                     ("Open estimate amount", "open_estimate_amount", M), ("Overdue balance", "overdue_balance", M),
                     ("Last contact", "last_contact", T), ("Last from", "last_from", T), ("Follow-ups sent", "sent", I),
                     ("Last outcome", "outcome", T)], "rows": cust_rows},
        {"id": "open-estimates", "name": "Open estimates", "filter": "status",
         "columns": [("Estimate", "number", T), ("Customer", "customer", T), ("Date", "date", T), ("Expires", "expiry", T),
                     ("Amount", "amount", M), ("Status", "status", T), ("Days since", "days", I), ("Memo", "memo", L)],
         "rows": est_rows},
        {"id": "open-invoices", "name": "Open invoices", "filter": "status",
         "columns": [("Invoice", "number", T), ("Customer", "customer", T), ("Date", "date", T), ("Due date", "due", T),
                     ("Amount", "amount", M), ("Balance", "balance", M), ("Days overdue", "days", I), ("Status", "status", T),
                     ("Memo", "memo", L)], "rows": inv_rows},
        {"id": "activity", "name": "Activity", "filter": "event",
         "columns": [("At", "at", T), ("Follow-up", "followup", T), ("Customer", "customer", T), ("Event", "event", T),
                     ("Note", "note", L)], "rows": act_rows},
    ]


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

class LockedError(Exception):
    pass


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    try:
        with open(str(tmp), "wb") as fh:
            fh.write(data)
        os.replace(str(tmp), str(path))
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise LockedError(str(exc))


def write_pane(paths: fu_db.Paths, pane: Dict[str, Any]) -> None:
    _atomic_write(paths.pane, (json.dumps(pane, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))


_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")


def xml_text(value: Any) -> str:
    text = _BAD_XML.sub("", str(value))
    if len(text) > 32000:
        text = text[:32000] + "…"
    return html.escape(text, quote=False)


def xml_attr(value: Any) -> str:
    return html.escape(_BAD_XML.sub("", str(value)), quote=True)


def col_letter(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


_STYLE = {"header": 1, "money": 2, "int": 3, "num": 4}

STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE8EEF4"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="5">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="1" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="2" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float("inf"), float("-inf"))


def _cell(ref: str, value: Any, ctype: str) -> str:
    if value is None or value == "":
        return ""
    if ctype in ("money", "int", "num") and _is_num(value):
        num = repr(float(value)) if isinstance(value, float) else str(value)
        return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, _STYLE[ctype], num)
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, xml_text(value))


def sheet_xml(columns: List[Tuple[str, str, str]], rows: List[Dict[str, Any]]) -> str:
    ncol = len(columns)
    last = col_letter(ncol - 1)
    nrows = len(rows) + 1
    widths = []
    for i, (header, key, ctype) in enumerate(columns):
        longest = len(header)
        for r in rows[:500]:
            v = r.get(key)
            if v is None:
                continue
            text = ("{:,.2f}".format(v) if ctype == "money" and _is_num(v) else str(v))
            longest = max(longest, max(len(line) for line in text.split("\n")) if text else 0)
        width = min(max(longest + 2, 12 if ctype == "money" else 8), 60 if ctype == "long" else 48)
        widths.append(width)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
           '<dimension ref="A1:%s%d"/>' % (last, nrows),
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
           '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>',
           '<sheetFormatPr defaultRowHeight="15"/>', '<cols>']
    for i, w in enumerate(widths):
        out.append('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (i + 1, i + 1, w))
    out.append('</cols><sheetData>')
    out.append('<row r="1">' + "".join(
        '<c r="%s1" t="inlineStr" s="1"><is><t xml:space="preserve">%s</t></is></c>' % (col_letter(i), xml_text(h))
        for i, (h, _k, _t) in enumerate(columns)) + '</row>')
    for rn, row in enumerate(rows, start=2):
        cells = "".join(_cell("%s%d" % (col_letter(i), rn), row.get(key), ctype) for i, (_h, key, ctype) in enumerate(columns))
        out.append('<row r="%d">%s</row>' % (rn, cells))
    out.append('</sheetData>')
    out.append('<autoFilter ref="A1:%s%d"/>' % (last, nrows))
    out.append('</worksheet>')
    return "".join(out)


def xlsx_bytes(tables: List[Dict[str, Any]], now_iso: str) -> bytes:
    import io
    n = len(tables)
    stamp = now_iso[:19] + "Z" if len(now_iso) >= 19 else now_iso
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join('<Override PartName="/xl/worksheets/sheet%d.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1) for i in range(n))
        + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>')
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>')
    app = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<Application>Follow-up mod</Application>'
        '<TitlesOfParts><vt:vector size="%d" baseType="lpstr">%s</vt:vector></TitlesOfParts>'
        '</Properties>') % (n, "".join("<vt:lpstr>%s</vt:lpstr>" % xml_text(t["name"]) for t in tables))
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:title>Follow-ups</dc:title><dc:creator>Follow-up mod</dc:creator>'
        '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
        '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
        '</cp:coreProperties>') % (xml_text(stamp), xml_text(stamp))
    defined = []
    for i, t in enumerate(tables):
        last = col_letter(len(t["columns"]) - 1)
        sheet_ref = "'%s'" % t["name"].replace("'", "''")
        defined.append('<definedName name="_xlnm._FilterDatabase" localSheetId="%d" hidden="1">%s!$A$1:$%s$%d</definedName>'
                       % (i, xml_text(sheet_ref), last, len(t["rows"]) + 1))
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<bookViews><workbookView/></bookViews><sheets>'
        + "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (xml_attr(t["name"][:31]), i + 1, i + 1) for i, t in enumerate(tables))
        + '</sheets><definedNames>' + "".join(defined) + '</definedNames></workbook>')
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                  'Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1) for i in range(n))
        + '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
          'Target="styles.xml"/>' % (n + 1)
        + '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("docProps/app.xml", app)
        zf.writestr("docProps/core.xml", core)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/styles.xml", STYLES_XML)
        for i, t in enumerate(tables):
            zf.writestr("xl/worksheets/sheet%d.xml" % (i + 1), sheet_xml(t["columns"], t["rows"]))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"\b(?:https?|ftp):[^\s\"'<>]*", re.IGNORECASE)


def _scrub(value: Any) -> Any:
    """Drop links from the data so the page never references anything external."""
    if isinstance(value, str):
        return _BAD_XML.sub("", _URL_RE.sub("(link)", value))
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in ("thread_url", "gmail_draft_url", "url")}
    return value


def _human_time(now_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(now_iso)
    except ValueError:
        return now_iso
    hour = dt.hour % 12 or 12
    return "%s at %d:%02d %s" % (fu_db.fmt_long(dt.date()), hour, dt.minute, "AM" if dt.hour < 12 else "PM")


def html_text(tables: List[Dict[str, Any]], pane: Dict[str, Any], now_iso: str) -> str:
    today = parse_date(pane.get("today")) or date.today()
    month = today.strftime("%Y-%m")
    sent_this_month = sum(1 for t in tables if t["id"] == "all-follow-ups" for r in t["rows"]
                          if (r.get("sent") or "")[:7] == month)
    counts = pane["counts"]
    payload = {
        "business": pane.get("business_name") or "",
        "today": pane.get("today"),
        "todayLong": fu_db.fmt_long(today),
        "generated": _human_time(now_iso),
        "tiles": [
            {"label": "Overdue balance", "value": counts["overdue_balance"], "money": True,
             "note": "%s past due" % fu_db.plural(counts["overdue_invoices"], "invoice")},
            {"label": "Open quotes", "value": counts["open_estimates"], "money": False,
             "note": "%s pending" % fu_db.fmt_money(counts["open_estimate_amount"])},
            {"label": "Ready to review", "value": counts["review"], "money": False,
             "note": "drafts waiting for you" if counts["review"] != 1 else "draft waiting for you"},
            {"label": "Sent this month", "value": sent_this_month, "money": False,
             "note": fu_db.MONTH_NAMES[today.month - 1] + " " + str(today.year)},
        ],
        "tables": [{"id": t["id"], "name": t["name"], "filter": t.get("filter"),
                    "columns": [{"h": h, "k": k, "t": ct} for (h, k, ct) in t["columns"]],
                    "rows": [{k: r.get(k) for (_h, k, _ct) in t["columns"]} for r in t["rows"]]} for t in tables],
    }
    # json.dumps, then neutralise anything the HTML parser could read inside a <script> block:
    # "</" becomes "<\\/", and every remaining "<", ">" and "&" becomes a \\u escape (JSON.parse restores them).
    data = (json.dumps(_scrub(payload), ensure_ascii=False).replace("</", "<\\/")
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    title = "Follow-ups" + ((" — " + payload["business"]) if payload["business"] else "")
    return HTML_TEMPLATE.replace("__TITLE__", html.escape(_URL_RE.sub("(link)", title))).replace("__DATA__", data)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light dark;--bg:#f5f6f8;--panel:#fff;--text:#1c2330;--muted:#5d6676;--border:#dde1e8;--accent:#2a5bd7;--accent-soft:#e9effc;--stripe:#fafbfc;--warn:#9a5b00}
@media (prefers-color-scheme:dark){:root{--bg:#121519;--panel:#1b1f26;--text:#e7eaef;--muted:#9ba5b4;--border:#2d333d;--accent:#82a6ff;--accent-soft:#222b3d;--stripe:#1f242c;--warn:#f1b660}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
header{padding:22px 24px 6px}
h1{margin:0 0 2px;font-size:22px;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;padding:14px 24px 6px}
.tile{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.tile .label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.tile .value{font-size:26px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
.tile .note{color:var(--muted);font-size:12.5px}
nav.tabs{display:flex;flex-wrap:wrap;gap:2px;padding:14px 24px 0;border-bottom:1px solid var(--border)}
nav.tabs button{font:inherit;color:var(--muted);background:none;border:1px solid transparent;border-bottom:0;border-radius:8px 8px 0 0;padding:8px 14px;cursor:pointer}
nav.tabs button:hover{color:var(--text)}
nav.tabs button[aria-selected="true"]{background:var(--panel);color:var(--text);border-color:var(--border);margin-bottom:-1px;font-weight:600}
nav.tabs .n{display:inline-block;min-width:20px;margin-left:6px;padding:0 6px;border-radius:10px;background:var(--accent-soft);color:var(--accent);font-size:12px;text-align:center}
main{padding:16px 24px 8px}
section.panel{display:none}
section.panel.active{display:block}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
.toolbar input,.toolbar select,.toolbar button{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--border);border-radius:7px;padding:6px 10px}
.toolbar input{flex:1 1 220px;max-width:340px}
.toolbar button{cursor:pointer}
.toolbar button:hover{border-color:var(--accent);color:var(--accent)}
.toolbar .count{margin-left:auto;color:var(--muted)}
.wrap{overflow:auto;max-height:72vh;background:var(--panel);border:1px solid var(--border);border-radius:10px}
table{border-collapse:collapse;width:100%}
th,td{padding:8px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border)}
th{position:sticky;top:0;z-index:1;background:var(--panel);font-size:12.5px;font-weight:600;white-space:nowrap;cursor:pointer;user-select:none}
th:hover{color:var(--accent)}
th .arrow{color:var(--accent);margin-left:4px}
tbody tr:nth-child(even){background:var(--stripe)}
td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
td.long{min-width:240px;max-width:480px;white-space:pre-wrap;overflow-wrap:anywhere}
td summary{cursor:pointer}
td summary::marker{color:var(--accent)}
.empty{padding:28px;text-align:center;color:var(--muted)}
footer{padding:10px 24px 26px;color:var(--muted);font-size:12px}
@media (max-width:640px){header,main,footer{padding-left:12px;padding-right:12px}.tiles,nav.tabs{padding-left:12px;padding-right:12px}}
@media print{nav.tabs,.toolbar{display:none}section.panel{display:block;page-break-after:always}.wrap{max-height:none;overflow:visible}}
</style>
</head>
<body>
<header><h1 id="title"></h1><div class="sub" id="subtitle"></div></header>
<div class="tiles" id="tiles"></div>
<nav class="tabs" id="tabs" role="tablist"></nav>
<main id="panels"></main>
<footer id="footer"></footer>
<script type="application/json" id="fu-data">__DATA__</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("fu-data").textContent);
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function money(v) {
    if (typeof v !== "number") return v === null || v === undefined ? "" : String(v);
    var s = Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return (v < 0 ? "-$" : "$") + s;
  }
  function show(col, v) {
    if (v === null || v === undefined) return "";
    if (col.t === "money") return money(v);
    if (col.t === "num" && typeof v === "number") return v.toFixed(2);
    return String(v);
  }
  document.getElementById("title").textContent = DATA.business ? DATA.business + " — Follow-ups" : "Follow-ups";
  document.getElementById("subtitle").textContent = "As of " + DATA.todayLong;
  document.getElementById("footer").textContent = "Generated " + DATA.generated + " from followup.db";
  var tiles = document.getElementById("tiles");
  DATA.tiles.forEach(function (t) {
    var d = el("div", "tile");
    d.appendChild(el("div", "label", t.label));
    d.appendChild(el("div", "value", t.money ? money(t.value) : String(t.value)));
    d.appendChild(el("div", "note", t.note));
    tiles.appendChild(d);
  });
  var tabs = document.getElementById("tabs"), panels = document.getElementById("panels");
  var buttons = [], sections = [];
  function select(i) {
    buttons.forEach(function (b, j) { b.setAttribute("aria-selected", j === i ? "true" : "false"); });
    sections.forEach(function (s, j) { s.className = "panel" + (j === i ? " active" : ""); });
  }
  DATA.tables.forEach(function (tbl, i) {
    var b = el("button", "", tbl.name);
    b.setAttribute("role", "tab");
    b.appendChild(el("span", "n", String(tbl.rows.length)));
    b.addEventListener("click", function () { select(i); });
    tabs.appendChild(b); buttons.push(b);
    var s = el("section", "panel"); s.setAttribute("role", "tabpanel");
    panels.appendChild(s); sections.push(s);
    buildTable(tbl, s);
  });
  select(0);

  function buildTable(tbl, section) {
    var st = {q: "", f: "", key: null, dir: 1};
    var bar = el("div", "toolbar");
    var search = el("input"); search.type = "search"; search.placeholder = "Search " + tbl.name.toLowerCase() + "…";
    search.setAttribute("aria-label", "Search " + tbl.name);
    bar.appendChild(search);
    var fcol = null, sel = null;
    tbl.columns.forEach(function (c) { if (c.k === tbl.filter) fcol = c; });
    if (fcol) {
      sel = el("select"); sel.setAttribute("aria-label", "Filter by " + fcol.h);
      var all = el("option", "", "All " + fcol.h.toLowerCase() + "s"); all.value = ""; sel.appendChild(all);
      var seen = {};
      tbl.rows.forEach(function (r) { var v = show(fcol, r[fcol.k]); if (v) seen[v] = 1; });
      Object.keys(seen).sort().forEach(function (v) { var o = el("option", "", v); o.value = v; sel.appendChild(o); });
      bar.appendChild(sel);
      sel.addEventListener("change", function () { st.f = sel.value; render(); });
    }
    var dl = el("button", "", "Download CSV"); dl.type = "button"; bar.appendChild(dl);
    var count = el("span", "count"); bar.appendChild(count);
    section.appendChild(bar);
    var wrap = el("div", "wrap"); var table = el("table"); var thead = el("thead"); var tbody = el("tbody");
    var hr = el("tr"); var ths = [];
    tbl.columns.forEach(function (c) {
      var th = el("th", "", c.h); th.title = "Sort by " + c.h;
      th.addEventListener("click", function () {
        if (st.key === c.k) { st.dir = -st.dir; } else { st.key = c.k; st.dir = 1; }
        render();
      });
      hr.appendChild(th); ths.push(th);
    });
    thead.appendChild(hr); table.appendChild(thead); table.appendChild(tbody); wrap.appendChild(table); section.appendChild(wrap);
    search.addEventListener("input", function () { st.q = search.value.trim().toLowerCase(); render(); });
    dl.addEventListener("click", function () { downloadCsv(tbl, current()); });

    function current() {
      var rows = tbl.rows.filter(function (r) {
        if (st.f && show(fcol, r[fcol.k]) !== st.f) return false;
        if (!st.q) return true;
        return tbl.columns.some(function (c) { return show(c, r[c.k]).toLowerCase().indexOf(st.q) !== -1; });
      });
      if (st.key !== null) {
        var key = st.key, dir = st.dir;
        rows = rows.slice().sort(function (a, b) {
          var x = a[key], y = b[key];
          var xe = x === null || x === undefined || x === "", ye = y === null || y === undefined || y === "";
          if (xe && ye) return 0; if (xe) return 1; if (ye) return -1;
          if (typeof x === "number" && typeof y === "number") return (x - y) * dir;
          if (typeof x === "number") return -dir; if (typeof y === "number") return dir;
          return String(x).localeCompare(String(y), undefined, {numeric: true, sensitivity: "base"}) * dir;
        });
      }
      return rows;
    }
    function render() {
      var rows = current();
      ths.forEach(function (th, i) {
        var old = th.querySelector(".arrow"); if (old) th.removeChild(old);
        if (tbl.columns[i].k === st.key) th.appendChild(el("span", "arrow", st.dir > 0 ? "▲" : "▼"));
      });
      while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
      if (!rows.length) {
        var tr0 = el("tr"); var td0 = el("td", "empty", tbl.rows.length ? "No rows match." : "Nothing here yet.");
        td0.colSpan = tbl.columns.length; tr0.appendChild(td0); tbody.appendChild(tr0);
      }
      rows.forEach(function (r) {
        var tr = el("tr");
        tbl.columns.forEach(function (c) {
          var v = show(c, r[c.k]);
          var td = el("td", (c.t === "money" || c.t === "int" || c.t === "num") ? "num" : (c.t === "long" ? "long" : ""));
          if (c.t === "long" && v.length > 180) {
            var d = el("details"); d.appendChild(el("summary", "", v.slice(0, 120).replace(/\s+/g, " ") + "…"));
            d.appendChild(el("div", "", v)); td.appendChild(d);
          } else { td.textContent = v; }
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      count.textContent = rows.length === tbl.rows.length ? rows.length + " rows" : rows.length + " of " + tbl.rows.length + " rows";
    }
    render();
  }
  function csvCell(v) {
    if (v === null || v === undefined) return "";
    var s = typeof v === "number" ? String(v) : String(v);
    if (typeof v !== "number" && /^[=+\-@]/.test(s)) s = "'" + s;
    if (/[",\r\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
    return s;
  }
  function downloadCsv(tbl, rows) {
    var lines = [tbl.columns.map(function (c) { return csvCell(c.h); }).join(",")];
    rows.forEach(function (r) { lines.push(tbl.columns.map(function (c) { return csvCell(r[c.k]); }).join(",")); });
    var blob = new Blob(["﻿" + lines.join("\r\n")], {type: "text/csv;charset=utf-8"});
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = tbl.id + "-" + DATA.today + ".csv";
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); document.body.removeChild(a); }, 500);
  }
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def refresh(conn: sqlite3.Connection, paths: fu_db.Paths, cfg: Dict[str, Any], today: date, now_iso: str,
            full: bool = True) -> Tuple[Dict[str, str], List[str]]:
    """Write pane.json and, when ``full``, the xlsx and html. Locked files become warnings."""
    warnings: List[str] = []
    snap = Snapshot(conn, cfg, today)
    pane = build_pane(snap, paths, cfg, now_iso)
    files = {"pane": str(paths.pane.resolve())}
    try:
        write_pane(paths, pane)
    except LockedError as exc:
        warnings.append("Couldn't write views/pane.json (%s)." % exc)
    if not full:
        return files, warnings
    tables = build_tables(snap, pane)
    try:
        _atomic_write(paths.xlsx, xlsx_bytes(tables, now_iso))
        files["xlsx"] = str(paths.xlsx.resolve())
    except LockedError as exc:
        warnings.append("Couldn't update Follow-ups.xlsx; it may be open in Excel. Close it and run export. (%s)" % exc)
    try:
        _atomic_write(paths.html, html_text(tables, pane, now_iso).encode("utf-8"))
        files["html"] = str(paths.html.resolve())
    except LockedError as exc:
        warnings.append("Couldn't update Follow-ups.html (%s)." % exc)
    return files, warnings
