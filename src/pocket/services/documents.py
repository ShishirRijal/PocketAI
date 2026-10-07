"""Documents -> transactions: bank statement PDFs, banking-app screenshots, receipts.

Flow:
    read_pages()     PDF pages as layout-preserved text; pages without a text layer
                     (scans) are rendered to PNG for the vision model. Images are one page.
    read_document()  every page through the `document` LLM stage, in parallel
    reconcile()      deterministic checks: the running balance decides money out vs in,
                     and the sums are compared with the statement's own totals
    stage_import()   rows -> import_rows (categories matched, FX converted,
                     duplicates of what you already logged switched off)
    commit_import()  included rows -> transactions (paid at = statement time,
                     logged at = now), all linked to the import so it can be reverted

Personal identifiers (IBANs, long card/account numbers, e-mail addresses) are masked
before any page is sent to a model.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from pocket.core import money
from pocket.core.categorize import CatRef, match_category
from pocket.data.db import utcnow
from pocket.data.models import Import, ImportRow, Transaction, User
from pocket.data.repositories import CategoryRepo, TagRepo, TransactionRepo
from pocket.llm.schemas import DocumentExtraction, DocumentRow

if TYPE_CHECKING:
    from pocket.core.orchestrator import Orchestrator
    from pocket.llm.stages import Pipeline, UserContext

log = logging.getLogger(__name__)

MAX_PAGES = 30
MIN_TEXT_CHARS = 40  # below this a PDF page is treated as a scan
PARALLEL_PAGES = 3


# ------------------------------------------------------------------ reading


@dataclass
class Page:
    number: int
    text: str | None = None
    image: bytes | None = None
    mime: str = "image/png"


def is_pdf(mime: str | None, filename: str | None = None, data: bytes | None = None) -> bool:
    return (
        (mime or "").lower() == "application/pdf"
        or (filename or "").lower().endswith(".pdf")
        or (data or b"")[:5] == b"%PDF-"
    )


def read_pages(data: bytes, mime: str | None, filename: str | None = None) -> list[Page]:
    if not is_pdf(mime, filename, data):
        return [
            Page(1, image=data, mime=mime if mime and mime.startswith("image/") else "image/jpeg")
        ]
    import pdfplumber

    pages: list[Page] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, pg in enumerate(pdf.pages[:MAX_PAGES], 1):
            text = (pg.extract_text(layout=True) or "").rstrip()
            if len(text.strip()) >= MIN_TEXT_CHARS:
                pages.append(Page(i, text=_trim_layout(text)))
            else:
                pages.append(Page(i, image=_render_page(data, i - 1)))
    return pages


def _trim_layout(text: str) -> str:
    # layout mode pads with lots of spaces; keep alignment, drop blank lines
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    indent = min((len(ln) - len(ln.lstrip()) for ln in lines), default=0)
    return "\n".join(ln[indent:] for ln in lines)


def _render_page(data: bytes, index: int, scale: float = 2.0) -> bytes:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        img = pdf[index].render(scale=scale).to_pil()
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    finally:
        pdf.close()


_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b")
_LONG_NUMBER = re.compile(r"\b\d[\d ]{10,}\d\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_MASKED_CARD = re.compile(r"\b\d{4,6}[*•xX]{4,}(\d{4})\b")  # 416598******5186


def redact(text: str) -> str:
    """Mask identifiers a model doesn't need. Amounts and dates are untouched."""
    text = _MASKED_CARD.sub(r"••\1", text)
    text = _IBAN.sub(lambda m: f"••{re.sub(r'\\s', '', m.group(0))[-4:]}", text)
    text = _LONG_NUMBER.sub(lambda m: f"••{re.sub(r'\\s', '', m.group(0))[-4:]}", text)
    return _EMAIL.sub("[email]", text)


# ------------------------------------------------------------------ extraction


@dataclass
class DocumentResult:
    kind: str
    institution: str | None
    account_currency: str | None
    pages: int
    rows: list[DocumentRow]
    summary: dict[str, Any] = field(default_factory=dict)
    check: dict[str, Any] = field(default_factory=dict)  # reconciliation report


async def read_document(pipeline: Pipeline, u: UserContext, pages: list[Page]) -> DocumentResult:
    sem = asyncio.Semaphore(PARALLEL_PAGES)

    async def one(p: Page) -> DocumentExtraction:
        async with sem:
            if p.text is not None:
                return await pipeline.read_document_page(
                    u, page=p.number, pages=len(pages), text=redact(p.text)
                )
            assert p.image is not None
            url = f"data:{p.mime};base64," + base64.b64encode(p.image).decode()
            return await pipeline.read_document_page(
                u, page=p.number, pages=len(pages), image_url=url
            )

    results = await asyncio.gather(*(one(p) for p in pages))
    rows = [r for res in results for r in res.rows]
    kinds = [r.kind for r in results if r.rows] or [r.kind for r in results]
    holder = next((r.account_holder for r in results if r.account_holder), None)
    summary: dict[str, Any] = {}
    for key in ("opening_balance", "closing_balance", "total_money_out", "total_money_in"):
        summary[key] = next((getattr(r, key) for r in results if getattr(r, key) is not None), None)
    doc = DocumentResult(
        kind=max(set(kinds), key=kinds.count) if kinds else "other",
        institution=next((r.institution for r in results if r.institution), None),
        account_currency=next((r.account_currency for r in results if r.account_currency), None),
        pages=len(pages),
        rows=rows,
        summary={k: v for k, v in summary.items() if v is not None},
    )
    doc.check = reconcile(doc, holder)
    return doc


def reconcile(doc: DocumentResult, holder: str | None = None) -> dict[str, Any]:
    """Deterministic checks on what the model read.

    1. Running balance: each row's balance minus the previous one must equal +/- the
       amount. That tells money out from money in for certain, so a misread column
       gets fixed here.
    2. Totals: money out / money in must match the statement's own summary box.
    """
    own = 0
    if holder and len(holder.split()) >= 2:
        # money to/from the statement owner themselves is a move between own accounts
        name = re.sub(r"\s+", " ", holder.strip().lower())
        for r in doc.rows:
            hay = f"{r.description} {r.note or ''} {r.merchant or ''}".lower()
            if name in re.sub(r"\s+", " ", hay) and r.direction != "transfer":
                r.direction = "transfer"
                own += 1
    fixed = 0
    flows: list[int] = []  # -1 out, +1 in, 0 unknown
    prev = doc.summary.get("opening_balance")
    for r in doc.rows:
        flow = 0
        if prev is not None and r.balance_after is not None:
            delta = round(r.balance_after - prev, 2)
            if abs(abs(delta) - r.amount) < 0.011:
                flow = -1 if delta < 0 else 1
                if r.direction == "expense" and flow == 1:
                    r.direction = "income"
                    fixed += 1
                elif r.direction == "income" and flow == -1:
                    r.direction = "expense"
                    fixed += 1
        if flow == 0:
            flow = {"expense": -1, "income": 1}.get(r.direction, 0)
        if r.direction == "transfer":
            r.category_hint = None
        flows.append(flow)
        if r.balance_after is not None:
            prev = r.balance_after

    out_total = round(sum(r.amount for r, f in zip(doc.rows, flows, strict=True) if f == -1), 2)
    in_total = round(sum(r.amount for r, f in zip(doc.rows, flows, strict=True) if f == 1), 2)
    report: dict[str, Any] = {
        "money_out": out_total,
        "money_in": in_total,
        "directions_fixed": fixed,
        "own_transfers": own,
    }
    want_out, want_in = doc.summary.get("total_money_out"), doc.summary.get("total_money_in")
    if want_out is not None or want_in is not None:
        ok_out = want_out is None or abs(want_out - out_total) < 0.02
        ok_in = want_in is None or abs(want_in - in_total) < 0.02
        report.update(
            matches_statement=ok_out and ok_in, statement_out=want_out, statement_in=want_in
        )
    opening, closing = doc.summary.get("opening_balance"), doc.summary.get("closing_balance")
    if opening is not None and closing is not None:
        report["balance_ok"] = abs(opening - out_total + in_total - closing) < 0.02
    return report


# ------------------------------------------------------------------ staging


def _occurred(r: DocumentRow, tz: str, now: datetime) -> tuple[datetime, bool]:
    zone = ZoneInfo(tz)
    try:
        d = date.fromisoformat(r.date)
    except ValueError:
        return now, False
    t, has_time = time(12, 0), False
    if r.time:
        m = re.match(r"^(\d{1,2}):(\d{2})", r.time.strip())
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            t, has_time = time(int(m.group(1)), int(m.group(2))), True
    when = datetime.combine(d, t, tzinfo=zone).astimezone(ZoneInfo("UTC"))
    return min(when, now), has_time


async def stage_import(
    o: Orchestrator,
    s: Session,
    user: User,
    doc: DocumentResult,
    *,
    filename: str,
    mime: str,
    channel: str,
    raw_message_id: int | None,
    imp: Import | None = None,
) -> Import:
    """Write the rows as a ready-to-review import (nothing in transactions yet)."""
    now = o.clock()
    if imp is None:
        imp = Import(user_id=user.id, filename=filename[:200], mime=mime[:80], channel=channel,
                     raw_message_id=raw_message_id)  # fmt: skip
        s.add(imp)
    imp.kind, imp.institution, imp.pages = doc.kind, doc.institution, doc.pages
    imp.summary = {**doc.summary, "check": doc.check, "account_currency": doc.account_currency}
    s.flush()
    cats = CategoryRepo(s)
    refs = [CatRef(c.id, c.full_name) for c in cats.active(user.id)]
    txns = TransactionRepo(s)
    fx_cache: dict[str, Any] = {}
    for i, r in enumerate(doc.rows):
        occurred, has_time = _occurred(r, user.timezone, now)
        currency = r.currency if re.fullmatch(r"[A-Z]{3}", r.currency or "") else user.base_currency
        amount_minor = money.to_minor(Decimal(str(r.amount)), currency)
        if currency not in fx_cache:
            fx_cache[currency] = await o._convert(
                10 ** money.exponent(currency), currency, user.base_currency
            )
        _, rate, _ = fx_cache[currency]
        base_minor = (
            money.convert_minor(amount_minor, currency, user.base_currency, rate)
            if rate
            else amount_minor
        )
        merchant = (r.merchant or "").strip()[:120] or None
        cat_id = None
        if merchant and (learned := cats.merchant_category(user.id, merchant)):
            cat_id = learned.id
        if cat_id is None:
            c, score = match_category(r.category_hint, refs)
            if c and score >= 0.85:
                cat_id = c.id
        dups = txns.find_duplicates(
            user.id, amount_minor=amount_minor, currency=currency, merchant=None,
            occurred_at=occurred, window=timedelta(hours=36), direction=r.direction,
        )  # fmt: skip
        # re-staging the same import must not flag its own rows; a second upload of the
        # same statement does flag every row of the first one
        dup = next((d for d in dups if d.import_id != imp.id), None)
        s.add(
            ImportRow(
                import_id=imp.id, idx=i, occurred_at=occurred, has_time=has_time,
                description=(r.description or merchant or "?")[:300], amount_minor=amount_minor,
                currency=currency, amount_base_minor=base_minor, fx_rate=rate, direction=r.direction,
                merchant=merchant, location=(r.location or "").strip()[:120] or None, note=r.note,
                category_id=cat_id, category_hint=(r.category_hint or "")[:80] or None,
                confidence=Decimal(str(round(r.confidence, 3))),
                duplicate_of=dup.id if dup else None, include=dup is None,
            )
        )  # fmt: skip
    imp.status = "ready"
    s.flush()
    s.refresh(imp)
    return imp


def commit_import(
    s: Session, user: User, imp: Import, now: datetime | None = None
) -> list[Transaction]:
    if imp.status != "ready":
        raise ValueError(f"import is {imp.status}")
    now = now or utcnow()
    cats = CategoryRepo(s)
    misc = cats.by_name(user.id, "Miscellaneous") or cats.create(user.id, "Miscellaneous")
    tags = TagRepo(s)
    txns = TransactionRepo(s)
    saved: list[Transaction] = []
    for row in imp.rows:
        if not row.include or row.transaction_id:
            continue
        tag_rows = [tags.get_or_create(user.id, row.merchant, "merchant")] if row.merchant else []
        if imp.institution:
            tag_rows.append(tags.get_or_create(user.id, imp.institution, "other"))
        t = txns.add(
            Transaction(
                user_id=user.id, amount_minor=row.amount_minor, currency=row.currency,
                amount_base_minor=row.amount_base_minor, fx_rate=row.fx_rate, direction=row.direction,
                category_id=row.category_id,
                merchant=row.merchant, location=row.location,
                note=row.note or (row.description if row.description != row.merchant else None),
                occurred_at=row.occurred_at, created_at=now, raw_message_id=imp.raw_message_id,
                import_id=imp.id, llm_confidence=row.confidence,
            ),
            tag_rows,
        )  # fmt: skip
        if t.category_id is None and t.direction == "expense":
            t.category_id = misc.id
        row.transaction_id = t.id
        saved.append(t)
    imp.status = "committed"
    imp.committed_at = now
    s.flush()
    return saved


def revert_import(s: Session, user: User, imp: Import) -> int:
    txns = TransactionRepo(s)
    rows = s.scalars(
        select(Transaction).where(
            Transaction.import_id == imp.id,
            Transaction.user_id == user.id,
            Transaction.deleted_at.is_(None),
        )
    ).all()
    for t in rows:
        txns.soft_delete(t, reason=f"revert import {imp.id}")
    imp.status = "reverted"
    s.flush()
    return len(rows)


# ------------------------------------------------------------------ summaries


def stats(imp: Import) -> dict[str, Any]:
    rows = list(imp.rows)
    inc = [r for r in rows if r.include]
    dates = sorted(r.occurred_at for r in rows)
    by_cat: dict[str, int] = {}
    for r in inc:
        if r.direction == "expense":
            name = r.category.full_name if r.category else (r.category_hint or "Uncategorized")
            by_cat[name] = by_cat.get(name, 0) + r.amount_base_minor
    return {
        "rows": len(rows),
        "included": len(inc),
        "duplicates": sum(1 for r in rows if r.duplicate_of),
        "out_minor": sum(r.amount_base_minor for r in inc if r.direction == "expense"),
        "in_minor": sum(r.amount_base_minor for r in inc if r.direction == "income"),
        "transfers": sum(1 for r in inc if r.direction == "transfer"),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
        "top": sorted(by_cat.items(), key=lambda kv: -kv[1])[:3],
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def summary_text(imp: Import, base: str, tz: str, link: str | None) -> str:
    st = stats(imp)
    zone = ZoneInfo(tz)
    lines = [f"📄 {imp.filename}" + (f" · {imp.institution}" if imp.institution else "")
             + (f" · {imp.pages} page{'s' * (imp.pages != 1)}" if imp.pages else "")]  # fmt: skip
    if not st["rows"]:
        return lines[0] + "\nI couldn't find any transactions in it."
    span = ""
    if st["first"] and st["last"]:
        a, b = st["first"].astimezone(zone), st["last"].astimezone(zone)
        span = f" · {a:%b %-d}" + (f" – {b:%b %-d}" if a.date() != b.date() else "")
    check = (imp.summary or {}).get("check", {})
    head = f"{_plural(st['rows'], 'transaction')}{span}"
    if check.get("matches_statement") is True:
        head += " · ✓ totals match the statement"
    lines.append(head)
    if check.get("matches_statement") is False:
        lines.append(
            f"⚠ Doesn't match the statement's totals (it says out {check.get('statement_out')}, "
            f"in {check.get('statement_in')}); worth a review"
        )
    if st["duplicates"]:
        dups = [r for r in imp.rows if r.duplicate_of]
        d = dups[0]
        what = f" ({d.merchant or d.description} {money.fmt(d.amount_base_minor, base)})"
        lines.append(f"{len(dups)} already logged{what if len(dups) == 1 else ''}, left out")
    moves = (
        f" · {_plural(st['transfers'], 'transfer')} between your accounts"
        if st["transfers"]
        else ""
    )
    lines.append(f"Importing {st['included']}: out {money.fmt(st['out_minor'], base)}"
                 f" · in {money.fmt(st['in_minor'], base)}{moves}")  # fmt: skip
    if st["top"]:
        lines.append("Top: " + " · ".join(f"{k} {money.fmt(v, base)}" for k, v in st["top"]))
    if link:
        lines.append(f"Review: {link}")
    return "\n".join(lines)
