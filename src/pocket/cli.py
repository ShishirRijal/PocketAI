"""`pocket` command line.

pocket chat                 talk to Pocket in the terminal (in-process)
pocket chat --remote URL    ...or against a running server
pocket send "23 eur lunch"  one-shot
pocket serve                api (+ inline worker + scheduler)
pocket worker               redis-streams worker process
pocket migrate              alembic upgrade head
pocket replay 42 [--commit] re-run a stored message
pocket backup / restore     sqlite backups
pocket digest               send the weekly digest now
pocket demo --months 6      seed realistic fake history (for the dashboard)
pocket import-v1 old.db     bring over history from the v1 telegram bot (dry run first)
pocket export --format qif  write an export file
pocket link whatsapp:+372…  allow another channel identity for the owner
pocket telegram-webhook     register the telegram webhook
pocket discord-commands     register discord slash commands
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

from pocket.config import get_settings


def _runtime(**kw: Any):
    from pocket.logging_setup import setup_logging
    from pocket.runtime import build_runtime

    settings = get_settings()
    setup_logging("WARNING" if kw.pop("quiet", True) else settings.log_level)
    return build_runtime(settings, **kw)


async def _say(rt, text: str) -> str:
    from pocket.channels.base import InboundMessage
    from pocket.channels.cli import render_terminal

    ucid = rt.settings.owner_cli
    msg = InboundMessage(
        channel="cli", channel_msg_id=uuid.uuid4().hex, user_channel_id=ucid, text=text
    )
    res = await rt.ingestor.ingest(msg, enqueue=False)
    if res.raw_message_id is None:
        return f"[{res.status.value}]"
    await rt.dispatcher.process(res.raw_message_id)
    return "\n\n".join(render_terminal(r) for r in rt.cli.drain(ucid))


def _chat_local() -> None:
    rt = _runtime()
    models = rt.services.router.usable_models("extract")
    print(f"Pocket · models for extraction: {', '.join(models)}")
    print('Type "help" for commands, ctrl-d to quit.\n')

    async def loop() -> None:
        while True:
            try:
                text = await asyncio.to_thread(input, "you  › ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not text.strip():
                continue
            reply = await _say(rt, text)
            print("pocket › " + reply.replace("\n", "\n         ") + "\n")

    asyncio.run(loop())


def _chat_remote(url: str) -> None:
    import httpx

    s = get_settings()
    headers = {"Authorization": f"Bearer {s.admin_token}"} if s.admin_token else {}
    with httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=60) as client:
        while True:
            try:
                text = input("you  › ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not text.strip():
                continue
            r = client.post("/webhook/cli", json={"text": text})
            if r.status_code != 200:
                print(f"error {r.status_code}: {r.text}")
                continue
            print("pocket › " + r.json()["text"].replace("\n", "\n         ") + "\n")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="pocket", description="Pocket, your finance memory")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat")
    c.add_argument("--remote", help="base URL of a running server")
    sub.add_parser("send").add_argument("text", nargs="+")
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--reload", action="store_true")
    sub.add_parser("worker")
    sub.add_parser("migrate")
    r = sub.add_parser("replay")
    r.add_argument("raw_id", type=int)
    r.add_argument("--commit", action="store_true")
    sub.add_parser("backup")
    rs = sub.add_parser("restore")
    rs.add_argument("file")
    sub.add_parser("digest")
    ln = sub.add_parser("link")
    ln.add_argument("identity", help="e.g. whatsapp:+3725…, telegram:123456, discord:9876")
    sub.add_parser("telegram-webhook")
    sub.add_parser("discord-commands")
    dm = sub.add_parser("demo", help="fill the db with realistic fake history")
    dm.add_argument("--months", type=int, default=6)
    iv = sub.add_parser("import-v1", help="import history from the v1 telegram bot's sqlite db")
    iv.add_argument("path")
    iv.add_argument(
        "--currency", default="NPR", help="v1 had no currency column (its DEFAULT_CURRENCY)"
    )
    iv.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ex = sub.add_parser("export")
    ex.add_argument("--format", default="csv", choices=["csv", "json", "qif"])
    ex.add_argument("--period", default="all_time")
    args = p.parse_args(argv)

    match args.cmd:
        case "chat":
            _chat_remote(args.remote) if args.remote else _chat_local()
        case "send":
            rt = _runtime()
            print(asyncio.run(_say(rt, " ".join(args.text))))
        case "serve":
            import uvicorn

            uvicorn.run(
                "pocket.main:create_app",
                factory=True,
                host=args.host,
                port=args.port,
                reload=args.reload,
            )
        case "worker":
            _worker()
        case "migrate":
            from pocket.data.db import Database
            from pocket.data.migrate import upgrade

            upgrade(Database(get_settings().database_url))
            print("migrated to head")
        case "replay":
            rt = _runtime()
            res = asyncio.run(rt.services.orchestrator.replay(args.raw_id, commit=args.commit))
            print(json.dumps(res, indent=2, default=str, ensure_ascii=False))
        case "backup":
            from pocket.services.backups import backup_now

            s = get_settings()
            from pocket.data.db import Database

            print(asyncio.run(backup_now(s, Database(s.database_url))))
        case "restore":
            from pocket.services.backups import restore

            print(restore(get_settings(), args.file))
        case "digest":
            from pocket.services.digests import send_digest

            rt = _runtime()
            print(asyncio.run(send_digest(rt, rt.owner_id, force=True)))
        case "link":
            _link(args.identity)
        case "telegram-webhook":
            s = get_settings()
            if not (s.telegram_bot_token and s.telegram_webhook_secret):
                sys.exit("set POCKET_TELEGRAM_BOT_TOKEN and POCKET_TELEGRAM_WEBHOOK_SECRET first")
            from pocket.channels.telegram import TelegramAdapter

            tg = TelegramAdapter(s.telegram_bot_token)
            print(asyncio.run(tg.set_webhook(s.public_url, s.telegram_webhook_secret)))
            print(asyncio.run(tg.set_commands()))
        case "discord-commands":
            s = get_settings()
            from pocket.channels.discord import DiscordAdapter

            print(
                asyncio.run(
                    DiscordAdapter(
                        s.discord_bot_token, s.discord_application_id
                    ).register_commands()
                )
            )
        case "demo":
            from pocket.services.demo import seed_demo

            rt = _runtime()
            print(
                f"added {seed_demo(rt.services.db, args.months, user_id=rt.owner_id)} demo transactions"
            )
        case "import-v1":
            from pocket.services.import_v1 import import_v1

            rt = _runtime()
            report = import_v1(
                rt.services.db, rt.owner_id, args.path, args.currency, apply=args.apply
            )
            print(report.text(args.apply))
        case "export":
            from pocket.services.exports import export_to_file

            rt = _runtime()
            path = export_to_file(
                rt.services.db, rt.owner_id, args.format, args.period, rt.settings
            )
            print(path)


def _link(identity: str) -> None:
    from pocket.data.db import Database
    from pocket.data.repositories import UserRepo
    from pocket.wiring import ensure_owner

    channel, _, ident = identity.partition(":")
    if channel == "whatsapp":
        ident = identity  # twilio sends the full "whatsapp:+..." as From
    if channel not in ("whatsapp", "telegram", "discord", "cli") or not ident:
        sys.exit("identity must look like whatsapp:+3725…, telegram:<chat id>, discord:<user id>")
    s = get_settings()
    db = Database(s.database_url)
    uid = ensure_owner(db, s)
    with db.session() as sess:
        repo = UserRepo(sess)
        user = repo.get(uid)
        assert user
        repo.add_identity(user, channel, ident)
    print(f"linked {channel} {ident} -> user {uid}")


def _worker() -> None:
    from pocket.logging_setup import setup_logging
    from pocket.runtime import build_runtime

    s = get_settings()
    setup_logging(s.log_level, s.log_json)
    if s.queue_backend != "redis":
        sys.exit(
            "the standalone worker needs POCKET_QUEUE_BACKEND=redis (inline runs inside the api)"
        )

    async def run() -> None:
        rt = build_runtime(s)
        await rt.start(worker=True, scheduler=s.scheduler_enabled)
        try:
            await rt.stop_event.wait()
        finally:
            await rt.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()
