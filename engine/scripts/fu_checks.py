"""Snapshot checks (what Claude gathered) and draft checks (what Claude wrote).

"AI writes, code checks": every amount, number and date in a draft must come from the
follow-up's evidence, and the recipient must be the customer or a thread participant.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import fu_db

QB_SCHEMA = "followup-qb/1"
MAIL_SCHEMA = "followup-mail/1"
ESTIMATE_STATUSES = ("Pending", "Accepted", "Closed", "Rejected", "Converted")

# ---------------------------------------------------------------------------
# Snapshot checks
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _str_id(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return text or None


def _check_date_field(row: Dict[str, Any], key: str, label: str, problems: List[str]) -> None:
    value = row.get(key)
    if value is None or value == "":
        row[key] = None
        return
    if not fu_db.is_iso_date(value):
        problems.append("%s: %s %r is not an ISO date (YYYY-MM-DD) or null; dates are never guessed." % (label, key, value))


def check_qb_snapshot(data: Any) -> Tuple[List[str], List[str]]:
    """Return (problems, warnings). Problems block the ingest; warnings are shown."""
    problems: List[str] = []
    warnings: List[str] = []
    if not isinstance(data, dict):
        return ["quickbooks.json must be a JSON object."], warnings
    if data.get("schema") != QB_SCHEMA:
        problems.append("quickbooks.json: schema must be %r, got %r." % (QB_SCHEMA, data.get("schema")))
    for key in ("customers", "estimates", "invoices"):
        if key not in data or data.get(key) is None:
            data[key] = []
            warnings.append("quickbooks.json has no %s list; treated as empty." % key)
        elif not isinstance(data.get(key), list):
            problems.append("quickbooks.json: %s must be a list." % key)
            data[key] = []

    qb_ids: Set[str] = set()
    for i, cust in enumerate(data["customers"]):
        label = "customers[%d]" % i
        if not isinstance(cust, dict):
            problems.append("%s must be an object." % label)
            continue
        qid = _str_id(cust.get("qb_id"))
        if not qid:
            problems.append("%s has no qb_id." % label)
            continue
        cust["qb_id"] = qid
        if qid in qb_ids:
            problems.append("Customer qb_id %s appears twice." % qid)
        qb_ids.add(qid)
        if not (cust.get("name") or cust.get("company")):
            problems.append("Customer %s has no name or company." % qid)
        email = cust.get("email")
        if email and not fu_db.split_emails(email):
            warnings.append("Customer %s has an email that doesn't look like one (%r); treated as no email." % (qid, email))
            cust["email"] = ""

    for kind, money_keys in (("estimates", ("amount",)), ("invoices", ("amount", "balance"))):
        numbers: Set[str] = set()
        for i, row in enumerate(data[kind]):
            label = "%s[%d]" % (kind, i)
            if not isinstance(row, dict):
                problems.append("%s must be an object." % label)
                continue
            num = _str_id(row.get("number"))
            if not num:
                problems.append("%s has no number." % label)
                continue
            row["number"] = num
            label = "%s %s" % (kind[:-1].capitalize(), num)
            if num in numbers:
                problems.append("%s appears twice." % label)
            numbers.add(num)
            cid = _str_id(row.get("customer_qb_id"))
            row["customer_qb_id"] = cid
            if not cid:
                problems.append("%s has no customer_qb_id." % label)
            elif cid not in qb_ids:
                warnings.append("%s refers to customer %s, who isn't in the customers list; added without contact details."
                                % (label, cid))
            for key in money_keys:
                if not _is_number(row.get(key)):
                    problems.append("%s: %s must be a number, got %r." % (label, key, row.get(key)))
            _check_date_field(row, "date", label, problems)
            if kind == "estimates":
                _check_date_field(row, "expiry_date", label, problems)
                status = row.get("status")
                match = [s for s in ESTIMATE_STATUSES if isinstance(status, str) and s.lower() == status.strip().lower()]
                if not match:
                    problems.append("%s: status %r must be one of %s." % (label, status, ", ".join(ESTIMATE_STATUSES)))
                else:
                    row["status"] = match[0]
                if row.get("status") == "Pending" and not row.get("date"):
                    warnings.append("%s is pending with no date; only mail contact can date it." % label)
            else:
                _check_date_field(row, "due_date", label, problems)
                if _is_number(row.get("balance")) and row["balance"] > 0 and not row.get("due_date"):
                    warnings.append("%s is open with no due date: shown as \"due date unknown\", not flagged." % label)

    totals = data.get("report_totals")
    if not isinstance(totals, dict):
        warnings.append("quickbooks.json has no report_totals; the rows could not be cross-checked against QuickBooks.")
        totals = {}
    est_sum = round(sum(float(e["amount"]) for e in data["estimates"]
                        if isinstance(e, dict) and e.get("status") == "Pending" and _is_number(e.get("amount"))), 2)
    inv_sum = round(sum(float(v["balance"]) for v in data["invoices"]
                        if isinstance(v, dict) and _is_number(v.get("balance")) and v["balance"] > 0), 2)
    for key, rows_sum, what in (("estimates_pending_amount", est_sum, "Pending estimates"),
                                ("invoices_open_balance", inv_sum, "Open invoice balances")):
        shown = totals.get(key)
        if shown is None:
            warnings.append("QuickBooks showed no total for %s; the rows were not cross-checked." % what.lower())
        elif not _is_number(shown):
            problems.append("report_totals.%s must be a number or null, got %r." % (key, shown))
        elif abs(float(shown) - rows_sum) > 0.01:
            problems.append("%s add up to %s, but QuickBooks showed %s: rows may be missing. Re-read the report."
                            % (what, fu_db.fmt_money(rows_sum), fu_db.fmt_money(float(shown))))
    return problems, warnings


def check_mail_snapshot(data: Any) -> Tuple[List[str], List[str]]:
    problems: List[str] = []
    warnings: List[str] = []
    if not isinstance(data, dict):
        return ["mail.json must be a JSON object."], warnings
    if data.get("schema") != MAIL_SCHEMA:
        problems.append("mail.json: schema must be %r, got %r." % (MAIL_SCHEMA, data.get("schema")))
    threads = data.get("threads")
    if threads is None:
        data["threads"] = threads = []
        warnings.append("mail.json has no threads list; treated as empty.")
    if not isinstance(threads, list):
        problems.append("mail.json: threads must be a list.")
        data["threads"] = []
        return problems, warnings
    seen: Dict[str, int] = {}
    for i, t in enumerate(threads):
        label = "threads[%d]" % i
        if not isinstance(t, dict):
            problems.append("%s must be an object." % label)
            continue
        email = fu_db.first_email(t.get("email"))
        if not email:
            problems.append("%s has no valid email." % label)
            continue
        t["email"] = email
        if not _str_id(t.get("thread_id")):
            problems.append("%s (%s) has no thread_id." % (label, email))
        else:
            t["thread_id"] = _str_id(t.get("thread_id"))
        if not fu_db.is_iso_datetime(t.get("last_at")):
            problems.append("%s (%s): last_at %r is not an ISO date/time; dates are never guessed." % (label, email, t.get("last_at")))
        last_from = str(t.get("last_from") or "").strip().lower()
        if last_from not in ("them", "us"):
            problems.append("%s (%s): last_from must be \"them\" or \"us\", got %r." % (label, email, t.get("last_from")))
        t["last_from"] = last_from
        if email in seen:
            warnings.append("%s appears in more than one thread entry; the latest one is used." % email)
        seen[email] = i
        parts = t.get("participants")
        if parts is None:
            t["participants"] = []
        elif not isinstance(parts, list):
            problems.append("%s (%s): participants must be a list." % (label, email))
    return problems, warnings


# ---------------------------------------------------------------------------
# Draft check
# ---------------------------------------------------------------------------

_MONTHS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
           r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_MONTH_INDEX = {m.lower(): i + 1 for i, m in enumerate(fu_db.MONTH_ABBR)}

MONEY_RE = re.compile(
    r"(?:(?:US)?\$\s?|\bUSD\s?)(?P<a>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"|(?P<c>\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\b\d+(?:\.\d{1,2})?)\s?(?:USD|dollars)\b"
    r"|(?P<b>\b\d{1,3}(?:,\d{3})+\.\d{2}(?!\d)|\b\d+\.\d{2}(?!\d))",
    re.IGNORECASE)
DATE_NAME_RE = re.compile(r"\b(?P<mon>%s)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{4})\b)?" % _MONTHS)
DATE_DMY_RE = re.compile(r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>%s)\b\.?(?:,?\s+(?P<year>\d{4})\b)?" % _MONTHS)
DATE_MY_RE = re.compile(r"\b(?P<mon>%s)\.?\s+(?P<year>(?:19|20|21)\d{2})\b" % _MONTHS)
DATE_NUM_RE = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}|\d{2})\b")
DATE_ISO_RE = re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b")
DAYS_RE = re.compile(r"\b(?P<n>\d{1,3})[\s-]?days?\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)\s?|\b\d{3}[\s.-])\d{3}[\s.-]\d{4}\b|\+\d[\d\s.-]{6,}\d|\b\d{3}-\d{4}\b")
EMAIL_URL_RE = re.compile(r"\S+@\S+\.\S+|\b(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)
NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
DATE_ASK_RE = re.compile(r"\b(?:dates?|when)\b", re.IGNORECASE)

PLACEHOLDERS = [
    (re.compile(r"\[[^\]\n]*[A-Z][^\]\n]*\]"), "a [bracketed] placeholder"),
    (re.compile(r"\{\{|\}\}"), "a {{template}} placeholder"),
    (re.compile(r"<\s*(?:(?:first|last|full|your|their|customer|business|company)\s+)?name\s*>", re.IGNORECASE),
     "a <NAME> placeholder"),
    (re.compile(r"<[A-Z][A-Z _]{1,30}>"), "a <PLACEHOLDER>"),
    (re.compile(r"XXX"), "XXX"),
    (re.compile(r"\bTBD\b", re.IGNORECASE), "TBD"),
    (re.compile(r"lorem", re.IGNORECASE), "lorem ipsum text"),
]


def _cents(value: Any) -> Optional[int]:
    try:
        return int(round(float(str(value).replace(",", "").replace("$", "").strip()) * 100))
    except (TypeError, ValueError):
        return None


def _mask(text: str, spans: Iterable[Tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def _month(name: str) -> int:
    return _MONTH_INDEX[name[:3].lower()]


def _evidence_dates(evidence: Dict[str, Any]) -> Tuple[Set[Tuple[int, int, int]], Set[Tuple[int, int]], Set[Tuple[int, int]]]:
    full: Set[Tuple[int, int, int]] = set()
    for item in evidence.get("dates") or []:
        text = str(item)
        for m in DATE_ISO_RE.finditer(text):
            full.add((int(m.group("y")), int(m.group("m")), int(m.group("d"))))
        for m in DATE_NAME_RE.finditer(text):
            if m.group("year"):
                full.add((int(m.group("year")), _month(m.group("mon")), int(m.group("day"))))
        for m in DATE_NUM_RE.finditer(text):
            y = int(m.group("y"))
            full.add((y + 2000 if y < 100 else y, int(m.group("m")), int(m.group("d"))))
    month_day = {(m, d) for (_y, m, d) in full}
    year_month = {(y, m) for (y, m, _d) in full}
    return full, month_day, year_month


def _allowed_numbers(evidence: Dict[str, Any]) -> Set[str]:
    allowed: Set[str] = set()
    for num in evidence.get("numbers") or []:
        text = str(num).strip()
        if not text:
            continue
        allowed.add(text.replace(",", ""))
        for run in re.findall(r"\d+", text):
            allowed.add(run)
    return allowed


def check_draft(subject: Optional[str], body: Optional[str], draft_to: Optional[str], evidence: Dict[str, Any],
                tier: Optional[str], customer_email: Optional[str], draft_cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """Return a list of problems; an empty list means the draft passes."""
    cfg = fu_db.normalize_config({"draft": draft_cfg or {}})["draft"]
    subject = subject or ""
    body = body or ""
    evidence = evidence or {}
    problems: List[str] = []

    # Recipient
    allowed = set(fu_db.split_emails(customer_email)) | set(fu_db.split_emails(evidence.get("email")))
    for p in evidence.get("participants") or []:
        allowed.update(fu_db.split_emails(p))
    to = (draft_to or "").strip().lower()
    if not to:
        problems.append("The draft has no recipient.")
    elif to not in allowed:
        problems.append("The draft is addressed to %s, but this customer's email is %s (or a thread participant)."
                        % (draft_to, ", ".join(sorted(allowed)) or "unknown"))

    # Length
    if not body.strip():
        problems.append("The body is empty.")
    elif len(body) > cfg["max_body_chars"]:
        problems.append("The body is %s characters; the limit is %s." % ("{:,}".format(len(body)), "{:,}".format(cfg["max_body_chars"])))
    if not subject.strip():
        problems.append("The subject is empty.")
    elif len(subject) > cfg["max_subject_chars"]:
        problems.append("The subject is %d characters; the limit is %d." % (len(subject), cfg["max_subject_chars"]))

    # Placeholders and banned phrases
    for part_name, text in (("subject", subject), ("body", body)):
        for regex, label in PLACEHOLDERS:
            m = regex.search(text)
            if m:
                problems.append("Placeholder left in the %s: %s (%r)." % (part_name, label, m.group(0)))
    lowered = (subject + "\n" + body).lower()
    for phrase in cfg["banned_phrases"]:
        if phrase and phrase.lower() in lowered:
            problems.append("Banned phrase: \"%s\"." % phrase)

    # Facts: amounts, dates, day counts, numbers
    allowed_cents = {c for c in (_cents(a) for a in evidence.get("amounts") or []) if c is not None}
    full, month_day, year_month = _evidence_dates(evidence)
    numbers_ok = _allowed_numbers(evidence)
    days_ok: Set[int] = set()
    for value in [evidence.get("days")] + list(evidence.get("other_days") or []):
        if isinstance(value, int) and not isinstance(value, bool):
            days_ok.update({value - 1, value, value + 1})
    reported: Set[str] = set()

    def report(msg: str) -> None:
        if msg not in reported:
            reported.add(msg)
            problems.append(msg)

    for text in (subject, body):
        text = _mask(text, [m.span() for m in EMAIL_URL_RE.finditer(text)])
        # money
        spans = []
        for m in MONEY_RE.finditer(text):
            raw = m.group("a") or m.group("b") or m.group("c")
            cents = _cents(raw)
            if cents is not None and cents not in allowed_cents:
                shown = ", ".join(fu_db.fmt_money(c / 100.0) for c in sorted(allowed_cents)) or "none"
                report("Amount %s isn't in the evidence (allowed: %s)." % (m.group(0).strip(), shown))
            spans.append(m.span())
        text = _mask(text, spans)
        # dates
        spans = []
        for regex in (DATE_ISO_RE, DATE_NUM_RE, DATE_NAME_RE, DATE_DMY_RE):
            for m in regex.finditer(text):
                gd = m.groupdict()
                try:
                    if regex is DATE_ISO_RE or regex is DATE_NUM_RE:
                        y = int(gd["y"])
                        y = y + 2000 if y < 100 else y
                        key_full: Optional[Tuple[int, int, int]] = (y, int(gd["m"]), int(gd["d"]))
                        ok = key_full in full
                    else:
                        mon, day = _month(gd["mon"]), int(gd["day"])
                        if gd.get("year"):
                            ok = (int(gd["year"]), mon, day) in full
                        else:
                            ok = (mon, day) in month_day
                except (KeyError, ValueError):
                    ok = False
                if not ok:
                    report("Date \"%s\" doesn't match any date in the evidence." % m.group(0).strip())
                spans.append(m.span())
            text = _mask(text, spans)
            spans = []
        for m in DATE_MY_RE.finditer(text):
            if (int(m.group("year")), _month(m.group("mon"))) not in year_month:
                report("Date \"%s\" doesn't match any date in the evidence." % m.group(0).strip())
            spans.append(m.span())
        text = _mask(text, spans)
        # day counts
        spans = []
        for m in DAYS_RE.finditer(text):
            n = int(m.group("n"))
            if 1 <= n <= 999 and n not in days_ok:
                expected = evidence.get("days")
                report("\"%s\" doesn't match the evidence (%s)." % (
                    m.group(0).strip(), ("%d days" % expected) if isinstance(expected, int) else "no day count"))
            spans.append(m.span())
        text = _mask(text, spans)
        # phone numbers are contact details, not facts
        text = _mask(text, [m.span() for m in PHONE_RE.finditer(text)])
        # remaining numbers of 3+ digits
        for m in NUMBER_RE.finditer(text):
            digits = m.group(0).replace(",", "")
            if len(digits) >= 3 and digits not in numbers_ok:
                shown = ", ".join(str(n) for n in (evidence.get("numbers") or [])) or "none"
                report("Number %s isn't one of the document numbers in the evidence (%s)." % (m.group(0), shown))

    # Tier tone
    if tier == "firm" and not DATE_ASK_RE.search(body):
        problems.append("A firm reminder must ask for a payment date (use \"when\" or \"date\").")
    return problems
