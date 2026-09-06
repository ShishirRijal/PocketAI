import time

from fastapi.testclient import TestClient

from pocket.main import create_app
from pocket.runtime import build_runtime
from pocket.services.digests import plain_digest, weekly_data
from pocket.services.exports import render_rows


async def test_export_command_and_download(say, services, settings):
    await say("23 eur groceries at rimi")
    await say("chiya 30 rs")
    r = await say("export csv month")
    assert "📦 CSV export" in r and "2 transactions" in r
    url = r.split("\n")[-1]
    path = url.split(settings.public_url.rstrip("/"))[1]
    rt = build_runtime(settings, services=services)
    app = create_app(settings, runtime=rt, run_worker=False, run_scheduler=False)
    with TestClient(app) as c:
        got = c.get(path)
        assert got.status_code == 200
        assert "Rimi" in got.text and "NPR" in got.text
        tampered = path.replace("sig=", "sig=0")
        assert c.get(tampered).status_code == 403
        expired = path.split("?")[0] + f"?exp={int(time.time()) - 5}&sig=abc"
        assert c.get(expired).status_code == 403


async def test_export_formats(say, services):
    await say("23 eur groceries at rimi")
    await say("got salary 2500")
    from sqlalchemy import select

    from pocket.data.models import Transaction, User

    with services.db.session() as s:
        rows = s.scalars(select(Transaction)).all()
        user = s.get(User, services.user_id)
        qif = render_rows(rows, user, "qif")
        assert qif.startswith("!Type:Bank")
        assert "T-23.00" in qif and "T2500.00" in qif and "LGroceries" in qif
        js = render_rows(rows, user, "json")
        assert '"merchant": "Rimi"' in js


async def test_digest_plain(say, services, clock):
    await say("23 eur groceries at rimi")
    await say("coffee 4")
    d = weekly_data(services.db, services.user_id, now=clock.now)
    text = plain_digest(d)
    assert "€27.00 across 2 transactions" in text
    assert "Groceries €23.00" in text
    r = await say("digest")
    assert "€27.00" in r


async def test_empty_export(say):
    assert "Nothing to export" in await say("export json today")
