import httpx
import respx
from sqlalchemy import select

from pocket.channels.base import MediaAttachment
from pocket.data.models import Transaction
from pocket.data.repositories import UserRepo


async def run_media(services, url, ctype, caption=""):
    o = services.orchestrator
    with services.db.session() as s:
        user = UserRepo(s).get(services.user_id)
        replies, _ = await o._handle(
            s, user, caption, None, media=[MediaAttachment(url=url, content_type=ctype)]
        )
    return "\n".join(r.text for r in replies)


@respx.mock
async def test_receipt_photo(services, fake, say):
    respx.get("https://media.example/r.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8jpeg")
    )
    fake.queue(
        "document_image",
        {
            "kind": "receipt",
            "rows": [
                {
                    "date": "2026-09-28",
                    "time": "17:05",
                    "description": "Prisma Peremarket",
                    "amount": 42.9,
                    "currency": "EUR",
                    "direction": "expense",
                    "merchant": "Prisma",
                    "category_hint": "Groceries",
                    "confidence": 0.9,
                }
            ],
        },
    )
    r = await run_media(services, "https://media.example/r.jpg", "image/jpeg")
    assert r.startswith("🧾 Read from your receipt:")
    assert "€42.90 · Groceries" in r and "Look right?" in r
    # the image went to the model inline with its real type, not as the provider url
    _, purpose, prompt = fake.calls[-1]
    assert purpose == "document_image"
    assert prompt.images[0].startswith("data:image/jpeg;base64,")
    r = await say("yes")
    assert "Logged €42.90 · Groceries" in r and "#receipt" in r


@respx.mock
async def test_not_a_receipt(services, fake):
    respx.get("https://media.example/cat.jpg").mock(
        return_value=httpx.Response(200, content=b"img")
    )
    fake.queue("document_image", {"kind": "other", "rows": []})
    assert "couldn't find any transactions" in await run_media(
        services, "https://media.example/cat.jpg", "image/png"
    )


@respx.mock
async def test_voice_note(services, say):
    respx.get("https://media.example/v.ogg").mock(
        return_value=httpx.Response(200, content=b"OggS...")
    )

    async def fake_transcribe(data, ctype, raw_id):
        assert ctype == "audio/ogg"
        return "5 euro parking and 12 euro sandwich"

    services.extras["transcriber"] = fake_transcribe
    r = await run_media(services, "https://media.example/v.ogg", "audio/ogg")
    assert r.startswith('🎙️ Transcribed: "5 euro parking and 12 euro sandwich"')
    assert "1. €5.00 · Transport" in r and "2. €12.00 · Restaurants" in r
    await say("yes")
    with services.db.session() as s:
        assert len(s.scalars(select(Transaction)).all()) == 2


@respx.mock
async def test_voice_question(services, say):
    await say("coffee 4")
    respx.get("https://media.example/q.ogg").mock(return_value=httpx.Response(200, content=b"OggS"))

    async def fake_transcribe(*a):
        return "how much did I spend today?"

    services.extras["transcriber"] = fake_transcribe
    r = await run_media(services, "https://media.example/q.ogg", "audio/ogg")
    assert '🎙️ "how much did I spend today?"' in r and "€4.00" in r


@respx.mock
async def test_download_failure(services):
    respx.get("https://media.example/x").mock(return_value=httpx.Response(404))
    assert "Couldn't download" in await run_media(services, "https://media.example/x", "image/jpeg")
