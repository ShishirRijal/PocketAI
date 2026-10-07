import io
import time
from datetime import UTC, datetime

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import select

from pocket.channels.base import MediaAttachment
from pocket.data.models import Import, Transaction
from pocket.data.repositories import UserRepo
from pocket.llm.schemas import DocumentRow
from pocket.main import create_app
from pocket.runtime import build_runtime
from pocket.services.documents import DocumentResult, read_pages, reconcile, redact

H = {"Authorization": "Bearer secret"}

LINES = [
    ("Sep 26, 2026", "Rimi Kristiine", "23.00", "", "77.00"),
    ("Sep 26, 2026", "Payment from Acme OU", "", "20.00", "97.00"),
    ("Sep 27, 2026", "To Test Person Savings", "10.00", "", "87.00"),
    ("Sep 27, 2026", "Bolt Ride", "12.50", "", "74.50"),
]


def statement_pdf(scanned_page: bool = False) -> bytes:
    """A small Revolut-style statement: header with an IBAN, the summary box, rows."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for text in (
        "EUR Statement",
        "Test Person   IBAN EE38 2200 2210 2014 5685   test.person@example.com",
        "Opening balance 100.00   Money out 45.50   Money in 20.00   Closing balance 74.50",
        "Date            Description                 Money out   Money in   Balance",
    ):
        c.drawString(40, y, text)
        y -= 18
    for d, desc, out_, in_, bal in LINES:
        c.drawString(40, y, d)
        c.drawString(130, y, desc)
        c.drawRightString(400, y, out_)
        c.drawRightString(470, y, in_)
        c.drawRightString(540, y, bal)
        y -= 16
    c.showPage()
    if scanned_page:
        c.rect(100, 400, 300, 200, fill=1)  # a "scan": no text layer at all
        c.showPage()
    c.save()
    return buf.getvalue()


def extraction(**over):
    rows = [
        {"date": "2026-09-26", "description": "Rimi Kristiine", "amount": 23.0,
         "currency": "EUR", "direction": "expense", "merchant": "Rimi",
         "category_hint": "Groceries", "balance_after": 77.0, "confidence": 0.95},
        # misread as an expense; the running balance says it's money in
        {"date": "2026-09-26", "description": "Payment from Acme OU", "amount": 20.0,
         "currency": "EUR", "direction": "expense", "merchant": "Acme OU",
         "category_hint": "Salary", "balance_after": 97.0, "confidence": 0.8},
        {"date": "2026-09-27", "description": "To Test Person Savings", "amount": 10.0,
         "currency": "EUR", "direction": "expense", "balance_after": 87.0, "confidence": 0.8},
        {"date": "2026-09-27", "time": "08:15", "description": "Bolt Ride", "amount": 12.5,
         "currency": "EUR", "direction": "expense", "merchant": "Bolt",
         "category_hint": "Transport", "balance_after": 74.5, "confidence": 0.9},
    ]  # fmt: skip
    return {
        "kind": "statement",
        "institution": "Revolut",
        "account_holder": "Test Person",
        "account_currency": "EUR",
        "opening_balance": 100.0,
        "closing_balance": 74.5,
        "total_money_out": 45.5,
        "total_money_in": 20.0,
        "rows": rows,
    } | over


# ------------------------------------------------------------------ reading


def test_read_pages_text_and_scan():
    pages = read_pages(statement_pdf(scanned_page=True), "application/pdf", "s.pdf")
    assert [p.number for p in pages] == [1, 2]
    assert "Rimi Kristiine" in pages[0].text and pages[0].image is None
    # layout is kept: description and its amounts stay on one line
    assert any("Bolt Ride" in ln and "12.50" in ln for ln in pages[0].text.splitlines())
    assert pages[1].text is None and pages[1].image.startswith(b"\x89PNG")
    assert pages[1].mime == "image/png"


def test_redact_keeps_amounts_and_dates():
    text = (
        "IBAN EE38 2200 2210 2014 5685 card 4111 1111 1111 1234 mail a.b@example.com\n"
        "Card: 416598******5186\n"
        "Sep 26, 2026  Rimi  1,234.56  2026-09-26"
    )
    out = redact(text)
    assert "2200" not in out and "••5685" in out
    assert "4111" not in out and "••1234" in out
    assert "416598" not in out and "Card: ••5186" in out
    assert "[email]" in out and "example.com" not in out
    assert "1,234.56" in out and "Sep 26, 2026" in out and "2026-09-26" in out


def test_reconcile_fixes_directions_and_own_transfers():
    ex = extraction()
    doc = DocumentResult(
        kind="statement", institution="Revolut", account_currency="EUR", pages=1,
        rows=[DocumentRow(**r) for r in ex["rows"]],
        summary={k: ex[k] for k in ("opening_balance", "closing_balance",
                                    "total_money_out", "total_money_in")},
    )  # fmt: skip
    check = reconcile(doc, "Test Person")
    assert [r.direction for r in doc.rows] == ["expense", "income", "transfer", "expense"]
    assert check["directions_fixed"] == 1 and check["own_transfers"] == 1
    # the transfer still moved money, so the totals match the statement's box
    assert check["money_out"] == 45.5 and check["money_in"] == 20.0
    assert check["matches_statement"] is True and check["balance_ok"] is True


def test_reconcile_flags_a_mismatch():
    ex = extraction(total_money_out=99.0)
    doc = DocumentResult(
        kind="statement", institution=None, account_currency="EUR", pages=1,
        rows=[DocumentRow(**r) for r in ex["rows"]], summary={"total_money_out": 99.0},
    )  # fmt: skip
    assert reconcile(doc)["matches_statement"] is False


# ------------------------------------------------------------------ chat


async def send_file(services, url, ctype, filename):
    with services.db.session() as s:
        user = UserRepo(s).get(services.user_id)
        replies, _ = await services.orchestrator._handle(
            s, user, "", None,
            media=[MediaAttachment(url=url, content_type=ctype, filename=filename)],
        )  # fmt: skip
    return replies


@respx.mock
async def test_statement_in_chat(services, fake, say, clock):
    await say("23 eur rimi yesterday")  # already logged by hand: the import must not double it
    respx.get("https://media.example/s.pdf").mock(
        return_value=httpx.Response(200, content=statement_pdf())
    )
    fake.queue("document", extraction())
    replies = await send_file(services, "https://media.example/s.pdf", "application/pdf", "sep.pdf")
    r = replies[0]
    assert r.text.startswith("📄 sep.pdf · Revolut · 1 page")
    assert "4 transactions · Sep 26 – Sep 27 · ✓ totals match the statement" in r.text
    assert "1 already logged (Rimi €23.00), left out" in r.text
    assert "Importing 3: out €12.50 · in €20.00 · 1 transfer between your accounts" in r.text
    assert [o.label for o in r.options] == ["Import 3", "Review", "Cancel"]
    # personal identifiers never reached the model
    _, purpose, prompt = fake.calls[-1]
    sent = str(prompt.messages)
    assert purpose == "document" and "2210" not in sent and "example.com" not in sent

    clock.advance(minutes=2)
    r = await say("yes")
    assert r.startswith("Imported 3 transactions from sep.pdf")
    with services.db.session() as s:
        imp = s.scalars(select(Import)).one()
        assert imp.status == "committed"
        txns = s.scalars(select(Transaction).where(Transaction.import_id == imp.id)).all()
        by_merchant = {t.merchant: t for t in txns}
        assert set(by_merchant) == {"Acme OU", "Bolt", None}
        assert by_merchant["Acme OU"].direction == "income"
        assert by_merchant[None].direction == "transfer"
        bolt = by_merchant["Bolt"]
        # paid at = the statement's time (08:15 Tallinn), logged at = now
        assert bolt.occurred_at == datetime(2026, 9, 27, 5, 15, tzinfo=UTC)
        assert bolt.created_at == clock.now
        assert bolt.category.name == "Transport"

    r = await say("undo")
    assert r == "↩️ Undone, removed the 3 transactions imported from sep.pdf."
    with services.db.session() as s:
        assert s.scalars(select(Import)).one().status == "reverted"
        live = s.scalars(
            select(Transaction).where(Transaction.import_id.is_not(None),
                                      Transaction.deleted_at.is_(None))
        ).all()  # fmt: skip
        assert live == []


@respx.mock
async def test_statement_review_then_cancel(services, fake, say):
    respx.get("https://media.example/s.pdf").mock(
        return_value=httpx.Response(200, content=statement_pdf())
    )
    fake.queue("document", extraction())
    await send_file(services, "https://media.example/s.pdf", "application/pdf", "sep.pdf")
    r = await say("review")
    assert "/app?tab=imports&import=" in r
    r = await say("no")
    assert "nothing imported" in r
    with services.db.session() as s:
        assert s.scalars(select(Import)).one().status == "cancelled"
        assert s.scalars(select(Transaction)).all() == []


@respx.mock
async def test_other_message_leaves_import_waiting(services, fake, say):
    respx.get("https://media.example/s.pdf").mock(
        return_value=httpx.Response(200, content=statement_pdf())
    )
    fake.queue("document", extraction())
    await send_file(services, "https://media.example/s.pdf", "application/pdf", "sep.pdf")
    assert "Logged €4.00" in await say("coffee 4")
    with services.db.session() as s:
        assert s.scalars(select(Import)).one().status == "ready"


# ------------------------------------------------------------------ dashboard


@pytest.fixture
def client(settings, services):
    rt = build_runtime(settings, services=services)
    app = create_app(settings, runtime=rt, run_worker=False, run_scheduler=False)
    with TestClient(app) as c:
        yield c


def wait_ready(client, imp_id):
    for _ in range(100):
        imp = client.get(f"/api/v1/imports/{imp_id}", headers=H).json()
        if imp["status"] != "processing":
            return imp
        time.sleep(0.05)
    raise AssertionError("import never finished")


def test_dashboard_import(client, fake):
    fake.queue("document", extraction())
    bad = client.post("/api/v1/imports", files={"file": ("x.txt", b"hello", "text/plain")},
                      headers=H)  # fmt: skip
    assert bad.status_code == 415
    up = client.post(
        "/api/v1/imports",
        files={"file": ("sep.pdf", statement_pdf(), "application/pdf")},
        headers=H,
    ).json()
    imp = wait_ready(client, up["id"])
    assert imp["status"] == "ready" and imp["channel"] == "web"
    assert imp["summary"]["check"]["matches_statement"] is True
    assert len(imp["rows"]) == 4 and imp["stats"]["included"] == 4

    rows = {r["description"]: r for r in imp["rows"]}
    bolt = rows["Bolt Ride"]
    assert bolt["occurred_at"].startswith("2026-09-27T05:15")
    base = f"/api/v1/imports/{imp['id']}"
    client.patch(f"{base}/rows/{bolt['id']}",
                 json={"location": "Tallinn", "amount": 13.5}, headers=H)  # fmt: skip
    client.patch(f"{base}/rows/{rows['Rimi Kristiine']['id']}", json={"include": False}, headers=H)
    imp = client.get(base, headers=H).json()
    assert imp["stats"]["included"] == 3

    r = client.post(f"{base}/commit", headers=H).json()
    assert r["imported"] == 3
    assert client.patch(f"{base}/rows/{bolt['id']}", json={"include": False},
                        headers=H).status_code == 409  # fmt: skip
    listed = client.get(f"/api/v1/transactions?period=all_time&direction=all&import={imp['id']}",
                        headers=H).json()  # fmt: skip
    assert listed["total"] == 3
    t = next(x for x in listed["items"] if x["merchant"] == "Bolt")
    assert t["amount_minor"] == 1350 and t["location"] == "Tallinn" and t["import_id"] == imp["id"]
    assert t["created_at"] != t["occurred_at"]
    assert [i["id"] for i in client.get("/api/v1/imports", headers=H).json()] == [imp["id"]]

    assert client.post(f"{base}/revert", headers=H).json()["reverted"] == 3
    after = client.get(f"/api/v1/transactions?period=all_time&direction=all&import={imp['id']}",
                       headers=H).json()  # fmt: skip
    assert after["total"] == 0


def test_dashboard_import_failure(client, fake):
    up = client.post(
        "/api/v1/imports",
        files={"file": ("broken.pdf", b"%PDF-1.4 not really", "application/pdf")},
        headers=H,
    ).json()
    imp = wait_ready(client, up["id"])
    assert imp["status"] == "failed" and imp["error"]
