import sqlite3

import httpx
import respx

from pocket.config import Settings
from pocket.data.db import Database
from pocket.services.backups import backup_now, blob_url, restore


def _settings(tmp_path, **kw):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'p.db'}",
        backup_dir=tmp_path / "b",
        backup_keep=2,
        _env_file=None,
        **kw,
    )


async def test_backup_rotate_restore(tmp_path):
    s = _settings(tmp_path)
    db = Database(s.database_url)
    with sqlite3.connect(tmp_path / "p.db") as c:
        c.execute("create table t (x int)")
        c.execute("insert into t values (42)")
    for _ in range(3):
        await backup_now(s, db)
    files = sorted((tmp_path / "b").glob("*.gz"))
    assert len(files) == 2
    with sqlite3.connect(tmp_path / "p.db") as c:
        c.execute("delete from t")
    msg = restore(s, str(files[-1]))
    assert "integrity: ok" in msg
    with sqlite3.connect(tmp_path / "p.db") as c:
        assert c.execute("select x from t").fetchall() == [(42,)]
    assert list(tmp_path.glob("p.db.before-restore-*"))


def test_blob_url():
    u = blob_url("https://acct.blob.core.windows.net/backups?sv=1&sig=abc", "pocket-1.db.gz")
    assert u == "https://acct.blob.core.windows.net/backups/pocket-1.db.gz?sv=1&sig=abc"


@respx.mock
async def test_azure_upload(tmp_path):
    route = respx.put(url__regex=r"https://acct\.blob\.core\.windows\.net/backups/pocket-.*").mock(
        return_value=httpx.Response(201)
    )
    s = _settings(
        tmp_path, backup_azure_sas_url="https://acct.blob.core.windows.net/backups?sv=1&sig=abc"
    )
    with sqlite3.connect(tmp_path / "p.db") as c:
        c.execute("create table t (x int)")
    msg = await backup_now(s, Database(s.database_url))
    assert "uploaded to azure" in msg
    assert route.calls[0].request.headers["x-ms-blob-type"] == "BlockBlob"
