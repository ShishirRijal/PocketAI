import httpx
import respx

from pocket.channels.base import Option, OutboundMessage
from pocket.channels.discord import COMPONENT, DiscordAdapter, components, parse_interaction
from pocket.channels.telegram import TelegramAdapter, keyboard, parse_update, verify_telegram
from pocket.channels.whatsapp import (
    WhatsAppAdapter,
    chunks,
    parse_twilio,
    twilio_signature,
    verify_twilio,
)


def test_twilio_signature_known_vector():
    # expected value computed with twilio's own RequestValidator.compute_signature
    url = "https://example.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    sig = twilio_signature("12345", url, params)
    assert sig == "vNe7KK2kJwCsxc9K3OLkkKB3qqI="
    assert verify_twilio("12345", url, params, sig)
    assert not verify_twilio("12345", url, params, "nope")


def test_parse_twilio_media_and_location():
    form = {
        "MessageSid": "SM1",
        "From": "whatsapp:+372",
        "Body": "",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/m/1",
        "MediaContentType0": "image/jpeg",
        "Latitude": "59.43",
        "Longitude": "24.75",
        "Address": "Tallinn",
    }
    m = parse_twilio(form, ("AC", "tok"))
    assert m.media[0].auth == ("AC", "tok") and m.media[0].content_type == "image/jpeg"
    assert m.location.label == "Tallinn" and m.text is None
    assert parse_twilio({"Body": "x"}, None) is None


def test_whatsapp_chunks():
    long = "\n".join(f"line {i} " + "x" * 50 for i in range(80))
    parts = chunks(long)
    assert all(len(p) <= 1600 for p in parts) and "".join(parts).count("line") == 80


@respx.mock
async def test_whatsapp_send():
    route = respx.post("https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json").mock(
        return_value=httpx.Response(201, json={"sid": "SMx"})
    )
    await WhatsAppAdapter("AC1", "tok", "whatsapp:+1415").send(
        "whatsapp:+372", OutboundMessage(text="hi")
    )
    body = route.calls[0].request.content.decode()
    assert "To=whatsapp%3A%2B372" in body and "Body=hi" in body


def test_telegram_parse():
    photo = {
        "update_id": 1,
        "message": {
            "message_id": 7,
            "date": 1790000000,
            "chat": {"id": 42},
            "caption": "lunch",
            "photo": [{"file_id": "small"}, {"file_id": "big"}],
        },
    }
    m = parse_update(photo)
    assert m.text == "lunch" and m.media[0].url == "tg-file:big" and m.channel_msg_id == "42:7"
    voice = {
        "update_id": 2,
        "message": {
            "message_id": 8,
            "chat": {"id": 42},
            "voice": {"file_id": "v", "mime_type": "audio/ogg"},
        },
    }
    assert parse_update(voice).media[0].content_type == "audio/ogg"
    cb = {
        "update_id": 3,
        "callback_query": {
            "id": "cq1",
            "data": "yes",
            "from": {"id": 42},
            "message": {"chat": {"id": 42}},
        },
    }
    m = parse_update(cb)
    assert m.text == "yes" and m.meta == {"callback_query_id": "cq1"}
    edited = {
        "update_id": 4,
        "edited_message": {"message_id": 7, "chat": {"id": 42}, "text": "lunch 13"},
    }
    assert parse_update(edited).channel_msg_id == "edit:42:7"
    assert (
        verify_telegram("s", "s", None)
        and verify_telegram("s", None, "s")
        and not verify_telegram(None, "s", "s")
    )


def test_telegram_keyboard():
    msg = OutboundMessage(
        text="?", options=[Option(value="yes", label="Yes"), Option(value="no", label="No")]
    )
    assert keyboard(msg) == {
        "inline_keyboard": [
            [{"text": "Yes", "callback_data": "yes"}, {"text": "No", "callback_data": "no"}]
        ]
    }
    msg.options_style = "list"
    assert len(keyboard(msg)["inline_keyboard"]) == 2


@respx.mock
async def test_telegram_send_answers_callback():
    ans = respx.post("https://api.telegram.org/botT/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={})
    )
    send = respx.post("https://api.telegram.org/botT/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    await TelegramAdapter("T").send("42", OutboundMessage(text="ok"), {"callback_query_id": "cq1"})
    assert ans.called and send.called


def test_discord_component_and_buttons():
    m = parse_interaction(
        {
            "id": "i1",
            "type": COMPONENT,
            "token": "tok",
            "application_id": "app",
            "member": {"user": {"id": "9"}},
            "data": {"custom_id": "2"},
        }
    )
    assert m.text == "2" and m.user_channel_id == "9"
    msg = OutboundMessage(
        text="?", options=[Option(value="yes", label="Yes"), Option(value="no", label="No")]
    )
    [row] = components(msg)
    assert [b["custom_id"] for b in row["components"]] == ["yes", "no"]


@respx.mock
async def test_discord_followup_vs_dm():
    follow = respx.post("https://discord.com/api/v10/webhooks/app/tok").mock(
        return_value=httpx.Response(200, json={})
    )
    a = DiscordAdapter("bot", "app")
    await a.send(
        "9", OutboundMessage(text="hi"), {"interaction_token": "tok", "application_id": "app"}
    )
    assert follow.called
    dm = respx.post("https://discord.com/api/v10/users/@me/channels").mock(
        return_value=httpx.Response(200, json={"id": "c1"})
    )
    post = respx.post("https://discord.com/api/v10/channels/c1/messages").mock(
        return_value=httpx.Response(200, json={})
    )
    await a.send("9", OutboundMessage(text="digest"))
    assert dm.called and post.called
