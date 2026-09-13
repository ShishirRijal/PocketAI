"""The orchestrator: a plain state machine (§3.2, §16).

    message -> pending answer? -> deterministic command? -> intent -> stage -> policy -> action

The LLM is only consulted for understanding language. Every side effect is
decided here, in code you can step through.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session as DBSession

from pocket.channels.base import Location, MediaAttachment, Option, OutboundMessage
from pocket.config import Settings
from pocket.core import commands, money, render
from pocket.core.calllog import CallLog
from pocket.core.categorize import CatRef, match_category
from pocket.core.dates import humanize_when, local_now, resolve_occurred_at
from pocket.core.directions import DIRECTIONS, LENDING
from pocket.core.policy import Decision, Proposal, Thresholds, combined_confidence, decide_add
from pocket.core.session import LastAction, Session, SessionStore
from pocket.data.db import Database, utcnow
from pocket.data.models import Transaction, User
from pocket.data.repositories import (
    CategoryRepo,
    PendingRepo,
    RawMessageRepo,
    TagRepo,
    TransactionRepo,
    UserRepo,
    normalize_tag,
)
from pocket.llm.router import CostCapExceeded, LLMUnavailable
from pocket.llm.schemas import ExtractedTransaction, FieldChange, Intent, QueryPlan
from pocket.llm.stages import Pipeline, UserContext
from pocket.services import queries
from pocket.services.fx import FxError, FxService

log = logging.getLogger(__name__)


def out(
    text: str, options: list[tuple[str, str]] | None = None, style: str = "buttons"
) -> OutboundMessage:
    return OutboundMessage(
        text=text,
        options=[Option(value=v, label=lab) for v, lab in (options or [])],
        options_style=style,
    )


YES_NO = [("yes", "✅ Yes"), ("no", "❌ No")]

READ_ONLY_COMMANDS = frozenset(
    {"categories", "tags", "cost", "settings", "review", "history", "budgets", "recurring",
     "lending", "person", "digest", "export"}
)  # fmt: skip


@dataclass
class Turn:
    """Everything about the message being handled."""

    s: DBSession
    user: User
    session: Session
    raw_message_id: int | None
    now: datetime
    stages: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "ok"
    _uctx: UserContext | None = None

    @property
    def base(self) -> str:
        return self.user.base_currency

    @property
    def tz(self) -> str:
        return self.user.timezone

    def stage(self, name: str, /, **info: Any) -> None:
        self.stages.append({"stage": name, **info})


# a hook the extras module registers into, keeps this file about the core flow
CommandHandler = Callable[["Orchestrator", Turn, commands.Command], Any]


class Orchestrator:
    def __init__(
        self,
        db: Database,
        pipeline: Pipeline,
        sessions: SessionStore,
        fx: FxService,
        settings: Settings,
        *,
        call_log: CallLog | None = None,
        clock: Callable[[], datetime] = utcnow,
        query_limiter: Any = None,
    ):
        self.db = db
        self.pipeline = pipeline
        self.sessions = sessions
        self.fx = fx
        self.settings = settings
        self.call_log = call_log or CallLog(db)
        self.clock = clock
        self.query_limiter = query_limiter
        self.thresholds = Thresholds(commit=settings.confidence_commit, ask=settings.confidence_ask)
        self.extra_commands: dict[str, CommandHandler] = {}
        # hooks run after a transaction is committed (budgets nudge, etc)
        self.after_commit: list[Callable[[Turn, list[Transaction]], str | None]] = []
        # media handlers (receipt photos, voice notes) plug in here
        self.media_handler: (
            Callable[
                [Orchestrator, Turn, list[MediaAttachment], str], Awaitable[list[OutboundMessage]]
            ]
            | None
        ) = None

        from pocket.core import extras

        extras.register(self)

    # ------------------------------------------------------------ entry points

    async def handle_raw(self, raw_message_id: int) -> list[OutboundMessage]:
        """Worker entry point: process a persisted raw message."""
        started = time.perf_counter()
        token = self.call_log.start()
        turn_info: dict[str, Any] = {}
        try:
            with self.db.session() as s:
                raw = RawMessageRepo(s).get(raw_message_id)
                if raw is None:
                    log.warning("raw message %s vanished", raw_message_id)
                    return []
                if raw.processed_at is not None and raw.outcome != "llm_unavailable":
                    log.info("raw message %s already processed, skipping", raw_message_id)
                    return []
                user = UserRepo(s).get(raw.user_id)
                assert user is not None
                media = [
                    MediaAttachment.model_validate(m) for m in (raw.media_json or []) if "url" in m
                ]
                loc_raw = next(
                    (m for m in (raw.media_json or []) if m.get("type") == "location"), None
                )
                location = Location.model_validate(loc_raw) if loc_raw else None
                replies, turn = await self._handle(
                    s, user, raw.text or "", raw_message_id, media=media, location=location
                )
                RawMessageRepo(s).mark(raw, turn.outcome)
                turn_info = {"stages": turn.stages, "outcome": turn.outcome, "user_id": user.id}
            # only after the db commit: session state must never point at rolled back rows
            await self.sessions.save(turn.session)
            return replies
        finally:
            calls = self.call_log.flush(token)
            log.info(
                "handled message",
                extra={
                    "event": "message_handled",
                    "msg_id": raw_message_id,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                    "llm_calls": len(calls),
                    "cost_usd": round(sum(float(c.get("cost_usd") or 0) for c in calls), 6),
                    **turn_info,
                },
            )

    async def replay(self, raw_message_id: int, *, commit: bool = False) -> dict[str, Any]:
        """Re-run the pipeline on a stored message (§9 /admin/replay).

        Dry run by default: everything happens inside a transaction that gets
        rolled back, so you see stages, LLM calls and the reply without side
        effects. Note it runs against the *current* state (pending actions,
        recent transactions), not the state at the time.
        """
        token = self.call_log.start()
        s = self.db.new_session()
        try:
            raw = RawMessageRepo(s).get(raw_message_id)
            if raw is None:
                raise KeyError(raw_message_id)
            user = UserRepo(s).get(raw.user_id)
            assert user is not None
            media = [
                MediaAttachment.model_validate(m) for m in (raw.media_json or []) if "url" in m
            ]
            replies, turn = await self._handle(s, user, raw.text or "", raw.id, media=media)
            if commit:
                RawMessageRepo(s).mark(raw, turn.outcome)
                s.commit()
                await self.sessions.save(turn.session)
            else:
                s.rollback()
            return {
                "raw_message_id": raw_message_id,
                "text": raw.text,
                "committed": commit,
                "outcome": turn.outcome,
                "stages": turn.stages,
                "replies": [r.model_dump() for r in replies],
            }
        finally:
            s.close()
            calls = self.call_log.flush(token)
            log.info("replayed %s (%d llm calls)", raw_message_id, len(calls))

    async def handle_text(
        self, user_id: int, text: str, *, raw_message_id: int | None = None
    ) -> list[OutboundMessage]:
        """Direct entry (tests, CLI in-process, replay dry runs)."""
        token = self.call_log.start()
        try:
            with self.db.session() as s:
                user = UserRepo(s).get(user_id)
                assert user is not None
                replies, turn = await self._handle(s, user, text, raw_message_id)
            await self.sessions.save(turn.session)
            return replies
        finally:
            self.call_log.flush(token)

    async def _handle(
        self,
        s: DBSession,
        user: User,
        text: str,
        raw_message_id: int | None,
        *,
        media: list[MediaAttachment] | None = None,
        location: Location | None = None,
    ) -> tuple[list[OutboundMessage], Turn]:
        session = await self.sessions.get(user.id)
        turn = Turn(
            s=s, user=user, session=session, raw_message_id=raw_message_id, now=self.clock()
        )
        try:
            replies = await self._route(turn, text.strip(), media or [], location)
        except LLMUnavailable as e:
            log.warning("llm unavailable: %s", e.errors)
            turn.outcome = "llm_unavailable"
            replies = [out(render.LLM_DOWN)]
        except CostCapExceeded:
            turn.outcome = "cost_cap"
            replies = [out(render.COST_CAP)]
        return replies, turn

    # ------------------------------------------------------------ routing

    async def _route(
        self, t: Turn, text: str, media: list[MediaAttachment], location: Location | None
    ) -> list[OutboundMessage]:
        if media and self.media_handler:
            t.stage("media", count=len(media))
            return await self.media_handler(self, t, media, text)
        if location and not text:
            return await self._location(t, location)
        if not text:
            return [out('Got an empty message 🤔 send "help" to see what I can do.')]

        pending = PendingRepo(t.s).active(t.user.id)
        if pending:
            t.stage("pending", kind=pending.kind)
            handled = await self._answer_pending(t, pending.kind, pending.payload, text)
            if handled is not None:
                return handled

        cmd = commands.parse(text)
        if cmd:
            t.stage("command", name=cmd.name)
            return await self._command(t, cmd)

        intent = await self.pipeline.intent(text, self._uctx(t))
        t.stage("intent", intent=intent.intent.value, confidence=intent.confidence)
        match intent.intent:
            case Intent.ADD:
                return await self._add(t, text)
            case Intent.EDIT:
                return await self._edit(t, text)
            case Intent.DELETE:
                return await self._delete(t, text)
            case Intent.QUERY:
                return await self._query(t, text)
            case Intent.HELP:
                return [out(render.HELP)]
            case _:
                t.outcome = "chitchat"
                return [out(render.CHITCHAT)]

    # ------------------------------------------------------------ context

    def _uctx(self, t: Turn, *, recent: list[Transaction] | None = None) -> UserContext:
        if t._uctx is None or recent is not None:
            cats = CategoryRepo(t.s).recently_used(t.user.id, limit=40)
            rec = recent if recent is not None else self._numbered(t)
            t._uctx = UserContext(
                user_id=t.user.id,
                base_currency=t.base,
                timezone=t.tz,
                now_local=local_now(t.tz, t.now),
                categories=[CatRef(c.id, c.full_name) for c in cats],
                recent=[self._recent_dict(i, r, t) for i, r in enumerate(rec, 1)],
                raw_message_id=t.raw_message_id,
            )
        return t._uctx

    def user_context(self, s: DBSession, user: User) -> UserContext:
        """Context for LLM stages outside a chat turn (digests, receipts)."""
        cats = CategoryRepo(s).recently_used(user.id, limit=40)
        return UserContext(
            user_id=user.id,
            base_currency=user.base_currency,
            timezone=user.timezone,
            now_local=local_now(user.timezone, self.clock()),
            categories=[CatRef(c.id, c.full_name) for c in cats],
        )

    def _recent_dict(self, i: int, r: Transaction, t: Turn) -> dict[str, Any]:
        return {
            "index": i,
            "id": r.id,
            "amount": f"{money.from_minor(r.amount_minor, r.currency)}",
            "currency": r.currency,
            "merchant": r.merchant,
            "category": r.category.full_name if r.category else None,
            "tags": [x.name for x in r.tags],
            "when": humanize_when(r.occurred_at, t.tz, t.now),
            "direction": r.direction,
        }

    def _numbered(self, t: Turn, n: int = 5) -> list[Transaction]:
        """The list that '1', '2', ... refer to: what the user was last shown,
        otherwise the most recently logged."""
        repo = TransactionRepo(t.s)
        if t.session.shown_list:
            rows = [repo.get(t.user.id, i) for i in t.session.shown_list]
            live = [r for r in rows if r is not None]
            if live:
                return live
        return repo.recent(t.user.id, n)

    def _show_list(self, t: Turn, rows: list[Transaction], header: str) -> OutboundMessage:
        t.session.shown_list = [r.id for r in rows]
        lines = [f"{i}. {render.txn_line(r, t.base, t.tz)}" for i, r in enumerate(rows, 1)]
        opts = [
            (
                str(i),
                f"{i}. {money.fmt(r.amount_minor, r.currency)} {r.merchant or (r.category.name if r.category else '')}"[
                    :24
                ],
            )
            for i, r in enumerate(rows, 1)
        ]
        return out(f"{header}\n" + "\n".join(lines), opts, style="list")

    # ------------------------------------------------------------ ADD

    async def _add(self, t: Turn, text: str) -> list[OutboundMessage]:
        ext = await self.pipeline.extract(text, self._uctx(t))
        t.stage("extract", n=len(ext.transactions))
        if not ext.transactions:
            t.outcome = "no_amount"
            return [out(render.NO_AMOUNT)]
        proposals = [await self._propose(t, x, text) for x in ext.transactions]
        return await self._decide(t, proposals)

    async def propose_from_extracted(self, t: Turn, x: ExtractedTransaction, text: str) -> Proposal:
        return await self._propose(t, x, text)

    async def _propose(self, t: Turn, x: ExtractedTransaction, text: str) -> Proposal:
        currency = x.currency if re.fullmatch(r"[A-Z]{3}", x.currency or "") else t.base
        amount_minor = money.to_minor(Decimal(str(x.amount)), currency)
        occurred = resolve_occurred_at(x.occurred_at, t.tz, t.now)
        base_minor, rate, src = await self._convert(amount_minor, currency, t.base)

        if x.direction in LENDING:
            # money between people isn't spending; the person tag carries it
            cat_id, cat_name, new_cat, cat_conf = None, None, None, None
        else:
            cat_id, cat_name, new_cat, cat_conf = await self._categorize(t, x, text)
        tags: dict[str, str | None] = {}
        for tg in x.tags:
            name = normalize_tag(tg.name)
            if name and len(name) <= 32:
                tags.setdefault(name, tg.kind if tg.kind != "other" else None)
        merchant = x.merchant.strip() if x.merchant else None
        if merchant:
            tags.setdefault(normalize_tag(merchant), "merchant")

        p = Proposal(
            amount_minor=amount_minor,
            currency=currency,
            amount_base_minor=base_minor,
            fx_rate=str(rate) if rate is not None else None,
            fx_source=src,
            direction=x.direction,
            merchant=merchant,
            note=x.note,
            occurred_at=occurred.isoformat(),
            category_id=cat_id,
            category_name=cat_name,
            new_category=new_cat,
            tags=list(tags.items())[:8],
            confidence=combined_confidence(x.confidence, cat_conf),
        )
        dups = TransactionRepo(t.s).find_duplicates(
            t.user.id,
            amount_minor=amount_minor,
            currency=currency,
            merchant=merchant,
            occurred_at=occurred,
            window=timedelta(minutes=self.settings.duplicate_window_minutes),
            direction=x.direction,
        )
        p.duplicate_of = [d.id for d in dups]
        t.stage(
            "propose",
            amount=amount_minor,
            currency=currency,
            category=cat_name or new_cat,
            conf=p.confidence,
        )
        return p

    async def _convert(
        self, amount_minor: int, currency: str, base: str
    ) -> tuple[int, Decimal | None, str | None]:
        if currency == base:
            return amount_minor, None, None
        try:
            r = await self.fx.rate(currency, base)
        except FxError:
            log.warning("no fx rate %s->%s, storing unconverted", currency, base)
            return amount_minor, None, "unconverted"
        return money.convert_minor(amount_minor, currency, base, r.rate), r.rate, r.source

    async def _categorize(
        self, t: Turn, x: ExtractedTransaction, text: str
    ) -> tuple[int | None, str | None, str | None, float]:
        """Deterministic first (learned merchant mapping, name match), LLM second."""
        cats_repo = CategoryRepo(t.s)
        if x.merchant:
            learned = cats_repo.merchant_category(t.user.id, x.merchant)
            if learned:
                t.stage("categorize", via="merchant_history")
                return learned.id, learned.full_name, None, 0.95
        refs = self._uctx(t).categories
        c, score = match_category(x.category_hint, refs)
        if c and score >= 0.85:
            t.stage("categorize", via="name_match", score=score)
            return c.id, c.name, None, score
        res = await self.pipeline.categorize(x, text, self._uctx(t))
        valid = {r.id: r for r in refs}
        if res.category_id is not None and res.category_id in valid:
            t.stage("categorize", via="llm", conf=res.confidence)
            return res.category_id, valid[res.category_id].name, None, res.confidence
        if res.new_category_name:
            c, score = match_category(res.new_category_name, refs)
            if c and score >= 0.9:
                return c.id, c.name, None, min(res.confidence, score)
            t.stage("categorize", via="llm_new", name=res.new_category_name)
            return None, None, res.new_category_name.strip()[:60], max(res.confidence, 0.7)
        misc = next((r for r in refs if r.name.lower() in ("miscellaneous", "misc")), None)
        return (misc.id if misc else None), (misc.name if misc else None), None, 0.4

    async def decide(
        self,
        t: Turn,
        proposals: list[Proposal],
        *,
        force_confirm: bool = False,
        intro: str | None = None,
    ) -> list[OutboundMessage]:
        """Public entry for other input paths (receipts, voice) to hand proposals to policy."""
        replies = await self._decide(t, proposals, force_confirm=force_confirm)
        if intro and replies:
            replies[0].text = f"{intro}\n{replies[0].text}"
        return replies

    async def _decide(
        self, t: Turn, proposals: list[Proposal], *, force_confirm: bool = False
    ) -> list[OutboundMessage]:
        d = decide_add(proposals, self.thresholds)
        if (
            force_confirm
            and d in (Decision.COMMIT, Decision.COMMIT_SHOW)
            and not all(p.confirmed for p in proposals)
        ):
            d = Decision.CONFIRM
        t.stage("policy", decision=d.value)
        pending = PendingRepo(t.s)
        ttl = timedelta(minutes=self.settings.pending_ttl_minutes)
        payload: dict[str, Any] = {"proposals": [p.model_dump(mode="json") for p in proposals]}

        if d is Decision.CONFIRM_DUPLICATE:
            t.outcome = "pending"
            dup_id = next(p for p in proposals if p.duplicate_of).duplicate_of[0]
            existing = TransactionRepo(t.s).get(t.user.id, dup_id)
            pending.set(t.user.id, "confirm_duplicate", payload, ttl)
            prev = render.txn_line(existing, t.base, t.tz) if existing else "a matching one"
            return [
                out(
                    f"Looks like a repeat 🤔 You already logged:\n{prev}\nSave this one too?",
                    YES_NO,
                )
            ]

        if d is Decision.CONFIRM_CATEGORY:
            t.outcome = "pending"
            idx = next(i for i, p in enumerate(proposals) if p.novel)
            p = proposals[idx]
            payload["index"] = idx
            pending.set(t.user.id, "confirm_category", payload, ttl)
            line = render.proposal_line(
                p.model_copy(update={"new_category": None, "category_name": "?"}), t.base, t.tz
            )
            return [
                out(
                    f"{line}\nI don't have a category that fits. Options:\n"
                    f'a) Create "{p.new_category}" (new)\n'
                    'b) Put under "Miscellaneous"\n'
                    "c) You pick — reply with a name",
                    [
                        ("a", f"Create {p.new_category}"[:20]),
                        ("b", "Miscellaneous"),
                        ("c", "I'll pick"),
                    ],
                )
            ]

        if d is Decision.CONFIRM:
            t.outcome = "pending"
            pending.set(t.user.id, "confirm_add", payload, ttl)
            lines = [render.proposal_line(p, t.base, t.tz) for p in proposals]
            if len(proposals) == 1:
                return [
                    out(
                        f"{lines[0]}\nLook right? (yes / no, or tell me what's off)"
                        if force_confirm
                        else f"Not 100% sure I got this:\n{lines[0]}\nSave it? (yes / no, or tell me what's off)",
                        YES_NO,
                    )
                ]
            words = {2: "Two", 3: "Three", 4: "Four", 5: "Five"}
            numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
            picks = " or ".join(f'"{i}"' for i in range(1, len(proposals) + 1))
            opts = [("yes", "✅ Save all")] + [
                (str(i), f"Only {i}") for i in range(1, min(len(proposals), 2) + 1)
            ]
            return [
                out(
                    f"{words.get(len(proposals), str(len(proposals)))} transactions?\n{numbered}\n"
                    f'Reply "yes" to save {"both" if len(proposals) == 2 else "all"}, {picks} to save one, '
                    "or tell me what's off.",
                    opts,
                )
            ]

        saved = self._commit(t, proposals)
        pending.clear(t.user.id)
        t.outcome = "added"
        if len(saved) == 1:
            line = render.txn_line(saved[0], t.base, t.tz)
            if proposals[0].new_category and saved[0].category:
                name = saved[0].category.full_name
                line = line.replace(name, f"{name} (new)", 1)
            body = f"✅ Logged {line}"
        else:
            total = sum(x.amount_base_minor for x in saved)
            body = f"✅ Saved {len(saved)} transactions ({money.fmt(total, t.base)} total)"
        if d is Decision.COMMIT_SHOW:
            body += "\n🤏 Not fully sure about this one, check it looks right."
        extra = [line for line in (hook(t, saved) for hook in self.after_commit) if line]
        fx_line = render.fx_footer(proposals, t.base)
        tail = [fx_line] if fx_line else []
        tail += extra
        tail.append(f"({render.UNDO_HINT})")
        return [out("\n".join([body, *tail]))]

    def _commit(self, t: Turn, proposals: list[Proposal]) -> list[Transaction]:
        cats = CategoryRepo(t.s)
        tags = TagRepo(t.s)
        txns = TransactionRepo(t.s)
        saved = []
        for p in proposals:
            cat_id = p.category_id
            if cat_id is None and p.new_category:
                parent_id = None
                name = p.new_category
                if "/" in name:
                    parent_name, name = [x.strip() for x in name.split("/", 1)]
                    parent_id = cats.create(t.user.id, parent_name).id
                cat_id = cats.create(t.user.id, name, parent_id=parent_id).id
            tag_rows = [tags.get_or_create(t.user.id, n, k) for n, k in p.tags]
            txn = Transaction(
                user_id=t.user.id,
                amount_minor=p.amount_minor,
                currency=p.currency,
                amount_base_minor=p.amount_base_minor,
                fx_rate=Decimal(p.fx_rate) if p.fx_rate else None,
                direction=p.direction,
                category_id=cat_id,
                merchant=p.merchant,
                note=p.note,
                occurred_at=datetime.fromisoformat(p.occurred_at),
                created_at=t.now,
                raw_message_id=t.raw_message_id,
                llm_confidence=Decimal(str(round(p.confidence, 3))),
            )
            saved.append(txns.add(txn, tag_rows))
        ids = [x.id for x in saved]
        t.session.remember(ids)
        t.session.shown_list = []
        t.session.last_action = LastAction(kind="add", transaction_ids=ids, at=t.now)
        return saved

    # ------------------------------------------------------------ pending answers

    async def _answer_pending(
        self, t: Turn, kind: str, payload: dict[str, Any], text: str
    ) -> list[OutboundMessage] | None:
        """Returns None when the message isn't an answer, so it gets handled fresh."""
        pending = PendingRepo(t.s)
        proposals = [Proposal.model_validate(p) for p in payload.get("proposals", [])]

        if commands.is_no(text):
            pending.clear(t.user.id)
            t.outcome = "cancelled"
            return [out("👌 Dropped it, nothing saved." if proposals else "👌 Cancelled.")]

        if kind == "confirm_duplicate":
            if commands.is_yes(text):
                for p in proposals:
                    p.duplicate_of = []
                return await self._decide(t, proposals)
            pending.clear(t.user.id)
            return None

        if kind == "confirm_category":
            idx = payload["index"]
            p = proposals[idx]
            ans = commands.norm(text)
            if ans in ("a", "a)", "create", "yes", "y", "ok"):
                pass  # keep new_category, _commit creates it
            elif ans in ("b", "b)", "misc", "miscellaneous"):
                misc = CategoryRepo(t.s).by_name(t.user.id, "Miscellaneous") or CategoryRepo(
                    t.s
                ).create(t.user.id, "Miscellaneous")
                p.category_id, p.category_name, p.new_category = misc.id, misc.name, None
            elif ans in ("c", "c)"):
                return [out("Sure, what should I call the category?")]
            elif commands.parse(text) or len(ans) > 40:
                pending.clear(t.user.id)
                return None
            else:
                name = text.strip().strip('"').strip()
                refs = self._uctx(t).categories
                c, score = match_category(name, refs)
                if c and score >= 0.85:
                    p.category_id, p.category_name, p.new_category = c.id, c.name, None
                else:
                    p.new_category = name[:60].title() if name.islower() else name[:60]
            p.confirmed = True
            p.category_confirmed = True
            proposals[idx] = p
            return await self._decide(t, proposals)

        if kind == "confirm_add":
            if commands.is_yes(text):
                for p in proposals:
                    p.confirmed = True
                return await self._decide(t, proposals)
            picks = commands.pick_numbers(text, len(proposals))
            if picks and len(proposals) > 1:
                chosen = [proposals[i - 1] for i in picks]
                for p in chosen:
                    p.confirmed = True
                return await self._decide(t, chosen)
            # "tell me what's off": treat as an edit against the proposals
            revised = await self._revise_proposals(t, proposals, text)
            if revised is None:
                pending.clear(t.user.id)
                return None
            return await self._decide(t, revised)

        if kind == "choose_target":
            rows = [TransactionRepo(t.s).get(t.user.id, i) for i in payload["candidates"]]
            live = [r for r in rows if r is not None]
            picks = commands.pick_numbers(text, len(live))
            if not picks:
                pending.clear(t.user.id)
                return None
            pending.clear(t.user.id)
            targets = [live[i - 1] for i in picks]
            if payload["action"] == "delete":
                return self._do_delete(t, targets)
            changes = [FieldChange.model_validate(c) for c in payload["changes"]]
            return await self._apply_edit(t, targets[0], changes, label=f"#{picks[0]}")

        if kind == "confirm_delete":
            if commands.is_yes(text):
                pending.clear(t.user.id)
                rows = [TransactionRepo(t.s).get(t.user.id, i) for i in payload["ids"]]
                return self._do_delete(t, [r for r in rows if r])
            pending.clear(t.user.id)
            return None

        # unknown / stale kinds: drop and carry on
        pending.clear(t.user.id)
        return None

    async def _revise_proposals(
        self, t: Turn, proposals: list[Proposal], text: str
    ) -> list[Proposal] | None:
        fake_recent = [
            {
                "index": i,
                "id": -i,
                "amount": f"{money.from_minor(p.amount_minor, p.currency)}",
                "currency": p.currency,
                "merchant": p.merchant,
                "category": p.category_name or p.new_category,
                "tags": [n for n, _ in p.tags],
                "when": humanize_when(datetime.fromisoformat(p.occurred_at), t.tz, t.now),
            }
            for i, p in enumerate(proposals, 1)
        ]
        u = self._uctx(t)
        u.recent = fake_recent
        res = await self.pipeline.resolve_edit(text, u)
        t._uctx = None
        if not res.changes or res.confidence < 0.5:
            return None
        idx = (res.target_index or 1) - 1
        if not 0 <= idx < len(proposals):
            idx = 0
        p = proposals[idx]
        for ch in res.changes:
            await self._apply_to_proposal(t, p, ch)
        return proposals

    async def _apply_to_proposal(self, t: Turn, p: Proposal, ch: FieldChange) -> None:
        match ch.field:
            case "amount":
                v = _parse_amount(ch.value)
                if v is not None:
                    p.amount_minor = money.to_minor(v, p.currency)
                    p.amount_base_minor, rate, p.fx_source = await self._convert(
                        p.amount_minor, p.currency, t.base
                    )
                    p.fx_rate = str(rate) if rate else None
            case "currency":
                p.currency = ch.value.upper()[:3]
                p.amount_base_minor, rate, p.fx_source = await self._convert(
                    p.amount_minor, p.currency, t.base
                )
                p.fx_rate = str(rate) if rate else None
            case "category":
                c, score = match_category(ch.value, self._uctx(t).categories)
                if c and score >= 0.8:
                    p.category_id, p.category_name, p.new_category = c.id, c.name, None
                else:
                    p.category_id, p.category_name, p.new_category = (
                        None,
                        None,
                        ch.value.strip().title(),
                    )
            case "merchant":
                p.merchant = ch.value.strip()
            case "note":
                p.note = ch.value.strip()
            case "date":
                p.occurred_at = resolve_occurred_at(ch.value, t.tz, t.now).isoformat()
            case "direction":
                if ch.value in DIRECTIONS:
                    p.direction = ch.value
            case "tags":
                p.tags = [(normalize_tag(x), None) for x in ch.value.split(",") if x.strip()]

    # ------------------------------------------------------------ EDIT

    async def _edit(self, t: Turn, text: str) -> list[OutboundMessage]:
        rows = self._numbered(t)
        if not rows:
            return [out("Nothing to edit yet — log something first.")]
        res = await self.pipeline.resolve_edit(text, self._uctx(t, recent=rows))
        t.stage(
            "edit_resolve", target=res.target_index, changes=len(res.changes), conf=res.confidence
        )
        if not res.changes:
            return [
                out(
                    'What should I change? e.g. "amount 29", "category groceries" or "it was yesterday".'
                )
            ]
        idx = res.target_index
        if idx is None or not 1 <= idx <= len(rows) or res.confidence < self.thresholds.ask:
            PendingRepo(t.s).set(
                t.user.id,
                "choose_target",
                {
                    "action": "edit",
                    "changes": [c.model_dump() for c in res.changes],
                    "candidates": [r.id for r in rows],
                },
                timedelta(minutes=self.settings.pending_ttl_minutes),
            )
            t.outcome = "pending"
            return [self._show_list(t, rows, "Which one should I change?")]
        label = "last transaction" if idx == 1 else f"#{idx}"
        return await self._apply_edit(t, rows[idx - 1], res.changes, label=label)

    async def _apply_edit(
        self, t: Turn, txn: Transaction, changes: list[FieldChange], *, label: str
    ) -> list[OutboundMessage]:
        repo = TransactionRepo(t.s)
        before_line = money.fmt(txn.amount_minor, txn.currency)
        updates: dict[str, Any] = {}
        described: list[str] = []
        new_tags: list[str] | None = None
        currency = txn.currency
        amount_minor = txn.amount_minor
        for ch in changes:
            match ch.field:
                case "amount":
                    v = _parse_amount(ch.value)
                    if v is None:
                        continue
                    amount_minor = money.to_minor(v, currency)
                case "currency":
                    currency = ch.value.strip().upper()[:3]
                case "category":
                    c, score = match_category(ch.value, self._uctx(t).categories)
                    if c and score >= 0.8:
                        cat_id, cat_name = c.id, c.name
                    else:
                        created = CategoryRepo(t.s).create(t.user.id, ch.value.strip().title())
                        cat_id, cat_name = created.id, f"{created.name} (new)"
                    if cat_id != txn.category_id:
                        old = txn.category.full_name if txn.category else "Uncategorized"
                        updates["category_id"] = cat_id
                        described.append(f"{old} → {cat_name}")
                case "merchant":
                    updates["merchant"] = ch.value.strip() or None
                    described.append(f"merchant → {ch.value.strip()}")
                case "note":
                    updates["note"] = ch.value.strip() or None
                    described.append("note updated")
                case "date":
                    when = resolve_occurred_at(ch.value, t.tz, t.now)
                    updates["occurred_at"] = when
                    described.append(f"date → {humanize_when(when, t.tz, t.now)}")
                case "direction":
                    if ch.value in DIRECTIONS:
                        updates["direction"] = ch.value
                        described.append(f"now {ch.value}")
                case "tags":
                    new_tags = [
                        normalize_tag(x) for x in re.split(r"[,\s]+", ch.value) if x.strip()
                    ]
        if amount_minor != txn.amount_minor or currency != txn.currency:
            if currency == txn.currency and txn.fx_rate is not None:
                base_minor = money.convert_minor(amount_minor, currency, t.base, txn.fx_rate)
                rate: Decimal | None = txn.fx_rate
            else:
                base_minor, rate, _ = await self._convert(amount_minor, currency, t.base)
            updates.update(
                amount_minor=amount_minor,
                currency=currency,
                amount_base_minor=base_minor,
                fx_rate=rate,
            )
            described.insert(0, f"{before_line} → {money.fmt(amount_minor, currency)}")

        before_versions = {v.id for v in repo.versions(txn.id)}
        diff = repo.update(txn, updates, changed_by="user", reason="edit") if updates else {}
        if new_tags is not None:
            repo.set_tags(
                txn, [TagRepo(t.s).get_or_create(t.user.id, n) for n in new_tags], reason="edit"
            )
            described.append("tags → " + render.tags_str(new_tags))
            diff["tags"] = ["", ""]
        if not diff:
            return [out("That's already how it is, nothing changed.")]
        t.s.refresh(txn)
        new_versions = [v.id for v in repo.versions(txn.id) if v.id not in before_versions]
        t.session.last_action = LastAction(
            kind="edit", transaction_ids=[txn.id], version_ids=new_versions, at=t.now
        )
        t.session.remember([txn.id])
        t.outcome = "edited"
        return [
            out(f"✏️ Updated {label}: {', '.join(described)}\n{render.txn_line(txn, t.base, t.tz)}")
        ]

    # ------------------------------------------------------------ DELETE

    async def _delete(self, t: Turn, text: str) -> list[OutboundMessage]:
        rows = self._numbered(t)
        if not rows:
            return [out("Nothing to delete.")]
        res = await self.pipeline.resolve_delete(text, self._uctx(t, recent=rows))
        t.stage("delete_resolve", targets=res.target_indexes, conf=res.confidence)
        valid = [i for i in res.target_indexes if 1 <= i <= len(rows)]
        ttl = timedelta(minutes=self.settings.pending_ttl_minutes)
        if not valid or res.confidence < self.thresholds.ask:
            PendingRepo(t.s).set(
                t.user.id,
                "choose_target",
                {"action": "delete", "candidates": [r.id for r in rows]},
                ttl,
            )
            t.outcome = "pending"
            return [self._show_list(t, rows, "Which one should I delete?")]
        targets = [rows[i - 1] for i in valid]
        if len(targets) > 1 or res.confidence < self.thresholds.commit:
            PendingRepo(t.s).set(t.user.id, "confirm_delete", {"ids": [r.id for r in targets]}, ttl)
            t.outcome = "pending"
            lines = "\n".join(render.txn_line(r, t.base, t.tz) for r in targets)
            return [out(f"Delete {'these' if len(targets) > 1 else 'this'}?\n{lines}", YES_NO)]
        return self._do_delete(t, targets)

    def _do_delete(self, t: Turn, rows: list[Transaction]) -> list[OutboundMessage]:
        repo = TransactionRepo(t.s)
        for r in rows:
            repo.soft_delete(r, reason="user delete")
        t.session.last_action = LastAction(
            kind="delete", transaction_ids=[r.id for r in rows], at=t.now
        )
        t.session.shown_list = []
        t.session.recent_transactions = [
            i for i in t.session.recent_transactions if i not in {r.id for r in rows}
        ]
        t.outcome = "deleted"
        lines = "\n".join(render.txn_line(r, t.base, t.tz) for r in rows)
        return [
            out(
                f'🗑️ Deleted:\n{lines}\n(reply "undo" to bring {"them" if len(rows) > 1 else "it"} back)'
            )
        ]

    # ------------------------------------------------------------ UNDO

    def _undo(self, t: Turn) -> list[OutboundMessage]:
        la = t.session.last_action
        window = timedelta(minutes=self.settings.undo_window_minutes)
        if la is None:
            return [out("Nothing to undo.")]
        if t.now - la.at > window:
            return [
                out(
                    f'Undo only works for {self.settings.undo_window_minutes} min. Use "edit" or "delete" instead.'
                )
            ]
        repo = TransactionRepo(t.s)
        found = [repo.get(t.user.id, i, include_deleted=True) for i in la.transaction_ids]
        rows = [r for r in found if r is not None]
        t.session.last_action = None
        t.outcome = "undone"
        if la.kind == "add":
            for r in rows:
                repo.soft_delete(r, reason="undo add")
            lines = "\n".join(render.txn_line(r, t.base, t.tz) for r in rows)
            return [out(f"↩️ Undone, removed:\n{lines}")]
        if la.kind == "delete":
            for r in rows:
                repo.restore(r, reason="undo delete")
            lines = "\n".join(render.txn_line(r, t.base, t.tz) for r in rows)
            return [out(f"↩️ Restored:\n{lines}")]
        # edit: reverse the exact version rows we wrote, newest first
        from pocket.data.models import TransactionVersion

        for vid in sorted(la.version_ids, reverse=True):
            v = t.s.get(TransactionVersion, vid)
            if v is None:
                continue
            txn = repo.get(t.user.id, v.transaction_id)
            if txn is None:
                continue
            revert: dict[str, Any] = {}
            for fld, (old, _new) in v.diff.items():
                if fld == "tags":
                    repo.set_tags(
                        txn, [TagRepo(t.s).get_or_create(t.user.id, n) for n in old], reason="undo"
                    )
                elif fld == "occurred_at":
                    revert[fld] = datetime.fromisoformat(old)
                elif fld == "fx_rate":
                    revert[fld] = Decimal(old) if old is not None else None
                else:
                    revert[fld] = old
            if revert:
                repo.update(txn, revert, changed_by="user", reason="undo edit")
        for r in rows:
            t.s.refresh(r)
        lines = "\n".join(render.txn_line(r, t.base, t.tz) for r in rows)
        return [out(f"↩️ Reverted:\n{lines}")]

    # ------------------------------------------------------------ QUERY

    async def _query(self, t: Turn, text: str) -> list[OutboundMessage]:
        if self.query_limiter and not self.query_limiter.allow(f"q:{t.user.id}"):
            t.outcome = "rate_limited"
            return [out(render.RATE_LIMITED)]
        plan = await self.pipeline.plan_query(text, self._uctx(t))
        t.stage("query_plan", **plan.model_dump(exclude_none=True, exclude_defaults=True))
        return [out(self.run_query(t, plan))]

    def run_query(self, t: Turn, plan: QueryPlan) -> str:
        res = queries.execute(t.s, t.user, plan, t.now)
        t.outcome = "query"
        return res.text()

    async def _location(self, t: Turn, loc: Location) -> list[OutboundMessage]:
        la = t.session.last_action
        if la and la.kind == "add" and t.now - la.at < timedelta(minutes=15):
            from pocket.services.geo import reverse_city

            city = loc.label or await reverse_city(loc.lat, loc.lon)
            if city:
                repo = TransactionRepo(t.s)
                for tid in la.transaction_ids:
                    txn = repo.get(t.user.id, tid)
                    if txn:
                        tag = TagRepo(t.s).get_or_create(t.user.id, city, "place")
                        repo.set_tags(txn, [*txn.tags, tag], reason="location")
                return [out(f"📍 Tagged #{normalize_tag(city)}")]
        return [
            out(
                "📍 Got a location. Send it right after logging something and I'll tag it with the city."
            )
        ]

    # ------------------------------------------------------------ commands

    async def _command(self, t: Turn, cmd: commands.Command) -> list[OutboundMessage]:
        match cmd.name:
            case "undo":
                PendingRepo(t.s).clear(t.user.id)
                return self._undo(t)
            case "help":
                return [out(render.HELP)]
            case "recent":
                n = cmd.args.get("n", 5)
                rows = TransactionRepo(t.s).recent(t.user.id, min(n, 20))
                if not rows:
                    return [out('Nothing logged yet. Try "12 eur lunch".')]
                hint = (
                    'Reply "delete 2" to remove one.'
                    if cmd.args.get("for") == "delete"
                    else 'Reply e.g. "edit 2 amount 29" or "delete 3".'
                )
                msg = self._show_list(t, rows, "Recent:")
                msg.text += f"\n{hint}"
                msg.options = []
                return [msg]
            case "edit":
                rows = self._numbered(t)
                idx = cmd.args["index"]
                if not 1 <= idx <= len(rows):
                    return [out(f'I only have {len(rows)} recent ones. Send "edit" to see them.')]
                ch = FieldChange(field=cmd.args["field"], value=cmd.args["value"])
                label = "last transaction" if idx == 1 else f"#{idx}"
                return await self._apply_edit(t, rows[idx - 1], [ch], label=label)
            case "delete":
                rows = self._numbered(t)
                idxs = [i for i in cmd.args["indexes"] if 1 <= i <= len(rows)]
                if not idxs:
                    return [out('Which one? Send "delete" to see the recent list.')]
                return self._do_delete(t, [rows[i - 1] for i in idxs])
            case "show":
                plan = QueryPlan(
                    kind="list", period=cmd.args["period"], direction="any", limit=15, confidence=1
                )
                return [out(self.run_query(t, plan))]
        handler = self.extra_commands.get(cmd.name)
        if handler:
            if cmd.name not in READ_ONLY_COMMANDS:
                # undo only reverses transaction adds/edits/deletes; after anything
                # else it would reach back to an older action, which surprises people
                t.session.last_action = None
            return await handler(self, t, cmd)
        return [out(f"`{cmd.name}` isn't wired up yet.")]


def _parse_amount(value: str) -> Decimal | None:
    m = re.search(r"\d+(?:[.,]\d{1,2})?", value.replace(" ", ""))
    if not m:
        return None
    return Decimal(m.group(0).replace(",", "."))
