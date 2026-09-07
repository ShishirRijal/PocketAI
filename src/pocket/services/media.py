"""Photos and voice notes (§12.9, §12.10).

- image/*  -> vision chain (receipt prompt) -> one proposed transaction, always confirmed
- audio/*  -> transcription (gpt-4o-mini-transcribe / whisper) -> the normal text pipeline,
              with the transcript echoed back and saves confirmed

Media is downloaded server-side (Twilio needs basic auth; Telegram gives file ids)
and sent to the model inline as base64, so provider URLs never leak to the LLM.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml

from pocket.channels.base import MediaAttachment, OutboundMessage
from pocket.llm.router import CostCapExceeded, LLMUnavailable

if TYPE_CHECKING:
    from pocket.core.orchestrator import Orchestrator, Turn
    from pocket.wiring import Services

log = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
Transcriber = Callable[[bytes, str, int | None], Awaitable[str]]


class MediaError(Exception):
    pass


async def download(
    att: MediaAttachment, adapters: dict[str, Any], client: httpx.AsyncClient | None = None
) -> tuple[bytes, str]:
    url = att.url
    if url.startswith("tg-file:"):
        tg = adapters.get("telegram")
        if tg is None:
            raise MediaError("telegram adapter missing")
        url = await tg.file_url(url.removeprefix("tg-file:"))
    own = client is None
    client = client or httpx.AsyncClient(timeout=30, follow_redirects=True)
    try:
        r = await client.get(url, auth=tuple(att.auth) if att.auth else None)  # type: ignore[arg-type]
        r.raise_for_status()
        data = r.content
    finally:
        if own:
            await client.aclose()
    if len(data) > MAX_BYTES:
        raise MediaError("file too large")
    ctype = att.content_type or r.headers.get("content-type", "application/octet-stream")
    return data, ctype.split(";")[0].strip()


def transcription_models(config_path) -> list[str]:
    try:
        raw = yaml.safe_load(Path(config_path).read_text()) or {}
    except OSError:
        return ["openai/whisper-1"]
    t = raw.get("transcription") or {}
    return [m for m in [t.get("model"), *(t.get("fallbacks") or [])] if m]


def litellm_transcriber(models: list[str], sink: Callable[[dict], None]) -> Transcriber:
    async def run(audio: bytes, content_type: str, raw_message_id: Any = None) -> str:
        import litellm

        ext = {
            "audio/ogg": "ogg",
            "audio/mpeg": "mp3",
            "audio/mp4": "m4a",
            "audio/amr": "amr",
            "audio/wav": "wav",
        }.get(content_type, "ogg")
        last: Exception | None = None
        for model in models:
            t0 = time.perf_counter()
            try:
                resp = await litellm.atranscription(
                    model=model, file=(f"voice.{ext}", audio), timeout=30
                )
                text = str(getattr(resp, "text", "") or "").strip()
                cost = float((getattr(resp, "_hidden_params", {}) or {}).get("response_cost") or 0)
                sink({"purpose": "transcribe", "model": model, "success": True, "cost_usd": cost,
                      "latency_ms": int((time.perf_counter() - t0) * 1000), "raw_message_id": raw_message_id,
                      "response_json": {"text": text}})  # fmt: skip
                return text
            except Exception as e:
                last = e
                sink({"purpose": "transcribe", "model": model, "success": False, "error": str(e)[:500],
                      "latency_ms": int((time.perf_counter() - t0) * 1000), "raw_message_id": raw_message_id})  # fmt: skip
        raise LLMUnavailable("transcribe", [("transcription", str(last))])

    return run


def make_handler(services: Services) -> Callable[..., Awaitable[list[OutboundMessage]]]:
    async def handle(
        o: Orchestrator, t: Turn, media: list[MediaAttachment], caption: str
    ) -> list[OutboundMessage]:
        adapters = services.extras.get("adapters", {})
        att = media[0]
        try:
            data, ctype = await download(att, adapters)
        except Exception as e:
            log.warning("media download failed: %s", e)
            t.outcome = "media_failed"
            return [
                OutboundMessage(text="Couldn't download that file 😕 Try again, or type it out.")
            ]

        if ctype.startswith("image/"):
            return await _receipt(o, t, data, ctype, caption)
        if ctype.startswith("audio/") or ctype in ("video/ogg", "application/ogg"):
            return await _voice(o, t, data, ctype, caption, services)
        t.outcome = "media_unsupported"
        return [
            OutboundMessage(
                text="I can read receipt photos and voice notes; that file type I can't use."
            )
        ]

    return handle


async def _receipt(
    o: Orchestrator, t: Turn, data: bytes, ctype: str, caption: str
) -> list[OutboundMessage]:
    data_url = f"data:{ctype};base64,{base64.b64encode(data).decode()}"
    try:
        res = await o.pipeline.receipt(data_url, caption, o._uctx(t))
    except (LLMUnavailable, CostCapExceeded):
        t.outcome = "media_llm_unavailable"
        return [
            OutboundMessage(
                text="I can't read photos right now (no vision model available). Type it and I'll log it."
            )
        ]
    t.stage("receipt", is_receipt=res.is_receipt)
    if not res.is_receipt or res.transaction is None:
        t.outcome = "not_receipt"
        return [
            OutboundMessage(
                text="That doesn't look like a receipt 🤔 Send a clearer photo or just type the amount."
            )
        ]
    p = await o.propose_from_extracted(t, res.transaction, caption or "receipt photo")
    if "receipt" not in {n for n, _ in p.tags}:
        p.tags.append(("receipt", None))
    return await o.decide(t, [p], force_confirm=True, intro="🧾 Read from your receipt:")


async def _voice(
    o: Orchestrator, t: Turn, data: bytes, ctype: str, caption: str, services: Services
) -> list[OutboundMessage]:
    transcribe: Transcriber = services.extras["transcriber"]
    try:
        text = await transcribe(data, ctype, t.raw_message_id)
    except LLMUnavailable:
        t.outcome = "media_llm_unavailable"
        return [
            OutboundMessage(text="Couldn't transcribe that voice note right now. Mind typing it?")
        ]
    if not text:
        return [OutboundMessage(text="I couldn't make out any words in that voice note.")]
    t.stage("transcribe", chars=len(text))
    ext = await o.pipeline.extract(text, o._uctx(t))
    if not ext.transactions:
        # not an expense; run it as a normal message (question, edit, ...)
        replies = await o._route(t, text, [], None)
        if replies:
            replies[0].text = f'🎙️ "{text}"\n{replies[0].text}'
        return replies
    proposals = [await o.propose_from_extracted(t, x, text) for x in ext.transactions]
    return await o.decide(t, proposals, force_confirm=True, intro=f'🎙️ Transcribed: "{text}"')


def install(services: Services) -> None:
    services.extras.setdefault(
        "transcriber",
        litellm_transcriber(
            transcription_models(services.settings.llm_config_path), services.call_log.sink
        ),
    )
    services.orchestrator.media_handler = make_handler(services)
