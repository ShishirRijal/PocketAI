import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import select

from pocket.channels.cli import CliAdapter
from pocket.channels.whatsapp import twilio_signature
from pocket.data.models import RawMessage, Transaction
from pocket.main import create_app
from pocket.runtime import build_runtime


class Recorder:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.media_auth = None

    async def send(self, ucid, message, meta=None):
        self.sent.append((ucid, message, meta))


@pytest.fixture
def rt(settings, services):
    adapters = {
        "cli": CliAdapter(),
        "whatsapp": Recorder("whatsapp"),
        "telegram": Recorder("telegram"),
        "discord": Recorder("discord"),
    }
    return build_runtime(settings, services=services, adapters=adapters)


@pytest.fixture
def client(rt, settings):
    app = create_app(settings, runtime=rt, run_worker=False, run_scheduler=False)
    with TestClient(app) as c:
        yield c


def raw_count(services):
    with services.db.session() as s:
        return len(s.scalars(select(RawMessage)).all())


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/readyz").json()
    assert r["status"] == "ok" and r["checks"]["db"] == "ok"
    assert r["checks"]["llm_offline_only"] is True


def test_cli_webhook_roundtrip(client, services):
    h = {"Authorization": "Bearer secret"}
    r = client.post("/webhook/cli", json={"text": "23 eur groceries at rimi"}, headers=h)
    assert r.status_code == 200
    assert "✅ Logged €23.00 · Groceries" in r.json()["text"]
    assert client.post("/webhook/cli", json={"text": "hi"}).status_code == 401


async def _drain(rt):
    while rt.queue.qsize():
        raw_id = rt.queue._q.get_nowait()
        rt.queue._q.task_done()
        await rt.dispatcher.process(raw_id)


def _twilio_form(body="lunch 12", sid="SM1", sender="whatsapp:+37255500000"):
    return {"MessageSid": sid, "From": sender, "Body": body, "NumMedia": "0"}


def test_whatsapp_signature_required(client, settings, rt):
    settings.verify_signatures = True
    settings.twilio_auth_token = "tok"
    form = _twilio_form()
    r = client.post("/webhook/whatsapp", data=form, headers={"X-Twilio-Signature": "nope"})
    assert r.status_code == 403
    url = settings.public_url.rstrip("/") + "/webhook/whatsapp"
    sig = twilio_signature("tok", url, form)
    r = client.post("/webhook/whatsapp", data=form, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200 and "<Response>" in r.text
    settings.verify_signatures = False


async def test_whatsapp_flow_and_idempotency(client, rt, services):
    form = _twilio_form("23 eur groceries at rimi", sid="SMabc")
    assert client.post("/webhook/whatsapp", data=form).status_code == 200
    # twilio retry of the same message
    assert client.post("/webhook/whatsapp", data=form).status_code == 200
    assert raw_count(services) == 1
    await _drain(rt)
    wa = rt.adapters["whatsapp"]
    assert len(wa.sent) == 1
    ucid, msg, _ = wa.sent[0]
    assert ucid == "whatsapp:+37255500000" and "Groceries" in msg.text
    with services.db.session() as s:
        assert len(s.scalars(select(Transaction)).all()) == 1


def test_unknown_sender_is_silent(client, services):
    r = client.post(
        "/webhook/whatsapp", data=_twilio_form(sender="whatsapp:+10000000000", sid="SMx")
    )
    assert r.status_code == 200
    assert raw_count(services) == 0


def test_rate_limit(client, rt, settings, services):
    settings.rate_limit_per_min = 2
    for i in range(3):
        r = client.post("/webhook/whatsapp", data=_twilio_form(f"coffee {i + 1}", sid=f"SMr{i}"))
    assert "too many messages" in r.text
    with services.db.session() as s:
        outcomes = [m.outcome for m in s.scalars(select(RawMessage)).all()]
    assert outcomes == [None, None, "rate_limited"]


async def test_telegram(client, rt, settings, services):
    settings.verify_signatures = True
    settings.telegram_webhook_secret = "tgsecret"
    with services.db.session() as s:
        from pocket.data.repositories import UserRepo

        repo = UserRepo(s)
        repo.add_identity(repo.get(services.user_id), "telegram", "777")
    update = {
        "update_id": 1,
        "message": {"message_id": 5, "date": 1790000000, "chat": {"id": 777}, "text": "coffee 3.5"},
    }
    assert client.post("/webhook/telegram/wrong", json=update).status_code == 403
    assert client.post("/webhook/telegram/tgsecret", json=update).status_code == 200
    assert (
        client.post(
            "/webhook/telegram",
            json={**update, "update_id": 2},
            headers={"X-Telegram-Bot-Api-Secret-Token": "tgsecret"},
        ).status_code
        == 200
    )  # same message id -> deduped
    assert raw_count(services) == 1
    await _drain(rt)
    [(ucid, msg, _)] = rt.adapters["telegram"].sent
    assert ucid == "777" and "Cafes" in msg.text
    settings.verify_signatures = False


async def test_discord(client, rt, settings, services):
    key = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization

    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )
    settings.verify_signatures = True
    settings.discord_public_key = pub

    def signed(payload):
        body = json.dumps(payload).encode()
        ts = "1790000000"
        sig = key.sign(ts.encode() + body).hex()
        return client.post(
            "/webhook/discord",
            content=body,
            headers={
                "X-Signature-Ed25519": sig,
                "X-Signature-Timestamp": ts,
                "Content-Type": "application/json",
            },
        )

    assert signed({"type": 1}).json() == {"type": 1}
    bad = client.post(
        "/webhook/discord",
        content=b'{"type":1}',
        headers={"X-Signature-Ed25519": "00", "X-Signature-Timestamp": "1"},
    )
    assert bad.status_code == 401

    with services.db.session() as s:
        from pocket.data.repositories import UserRepo

        repo = UserRepo(s)
        repo.add_identity(repo.get(services.user_id), "discord", "42")
    interaction = {
        "id": "int1",
        "type": 2,
        "token": "tok",
        "application_id": "app",
        "user": {"id": "42"},
        "data": {"name": "pocket", "options": [{"name": "text", "value": "metro 2"}]},
    }
    assert signed(interaction).json() == {"type": 5}
    await _drain(rt)
    [(ucid, msg, meta)] = rt.adapters["discord"].sent
    assert "Transport" in msg.text and meta["interaction_token"] == "tok"
    stranger = {**interaction, "id": "int2", "user": {"id": "999"}}
    assert "private" in signed(stranger).json()["data"]["content"]
    settings.verify_signatures = False


def test_admin_requires_token(client):
    assert client.get("/admin/cost.json").status_code == 401
    assert (
        client.get("/admin/cost.json", headers={"Authorization": "Bearer secret"}).status_code
        == 200
    )
    assert client.get("/admin/cost?token=secret").status_code == 200


async def test_admin_replay_is_dry_run(client, services, rt):
    h = {"Authorization": "Bearer secret"}
    client.post("/webhook/cli", json={"text": "lunch 12"}, headers=h)
    with services.db.session() as s:
        raw_id = s.scalars(select(RawMessage.id)).first()
    r = client.post(f"/admin/replay/{raw_id}", headers=h).json()
    assert r["committed"] is False
    assert any(st["stage"] == "intent" for st in r["stages"])
    with services.db.session() as s:
        assert len(s.scalars(select(Transaction)).all()) == 1  # replay didn't add a second
    page = client.get("/admin?token=secret")
    assert "lunch 12" in page.text


def test_docs_hidden_in_prod(settings, services):
    settings.env = "prod"
    rt = build_runtime(settings, services=services)
    with TestClient(create_app(settings, runtime=rt, run_worker=False, run_scheduler=False)) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
    settings.env = "test"
