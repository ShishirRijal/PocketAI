# Roadmap

## Next

- **Telegram as the everyday channel.** Plain messages, photos, voice notes and inline buttons already work; it needs a bot token, a webhook secret, and `pocket link telegram:<chat id>`.
- **Deploy to the VM.** `docker compose up -d` with Caddy for TLS, so the bot is reachable without a tunnel and Discord/Telegram webhooks keep a stable URL.
- **WhatsApp beyond the Twilio sandbox.** The sandbox has no real buttons and expires after 3 days of silence; Meta's Cloud API removes both limits.
- **Import the v1 history.** `pocket import-v1 <v1 db> --currency NPR`, a dry run first, then `--apply`.

## Ideas

- Revolut/Wise automatic sync: Wise has a personal API token; Revolut needs an open-banking aggregator (GoCardless Bank Account Data). Until then, the monthly statement PDF import covers it.
- Per-row "this is mine/not mine" memory for imports, so a merchant you always switch to transfer stays a transfer next month.

- Monthly PDF report.
- Anomaly alerts beyond the per-category "unusually large" nudge (e.g. a merchant you've never used, a duplicate subscription).
- Google sign-in for the dashboard instead of the admin token.
- A local model via Ollama for offline weeks (`config/llm.local.yaml` is ready, `docker compose --profile local`).
- Multi-user: the schema already has `user_id` everywhere; it needs onboarding and per-user settings in the dashboard.

## Decisions on record

- **OpenAI first, Gemini second** in every model chain. `POCKET_LLM_PROVIDER_ORDER=gemini,openai` flips it without editing `config/llm.yaml`.
- **No offline parser.** If every provider is down, messages wait in `raw_messages` and are retried; commands keep working.
- **Confidence thresholds** start at 0.85 (save) / 0.6 (ask) and are meant to be tuned from real use; `review` lists everything under 0.85.
- **Multi-item messages always ask** ("Two transactions?"), even when confident.
- **Edits update the row and write a version**; deletes are soft. Nothing is silently overwritten.
- **Dashboard login is the admin token** exchanged for a signed cookie; for a single-user tool that's enough.
