"""Policy layer (§4.4). Plain Python, no LLM: decides whether a proposed ADD is
committed, committed-with-caveat, or held for confirmation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Proposal(BaseModel):
    """A transaction we intend to write. JSON-safe so it can sit in pending_actions."""

    amount_minor: int
    currency: str
    amount_base_minor: int
    fx_rate: str | None = None
    fx_source: str | None = None
    direction: str = "expense"
    merchant: str | None = None
    note: str | None = None
    occurred_at: str  # ISO, UTC
    category_id: int | None = None
    category_name: str | None = None
    new_category: str | None = None  # set when the categorizer proposed a new one
    tags: list[tuple[str, str | None]] = Field(default_factory=list)
    confidence: float = 1.0
    duplicate_of: list[int] = Field(default_factory=list)
    confirmed: bool = False  # the user already said yes to this exact proposal
    category_confirmed: bool = False  # user okayed creating new_category

    @property
    def novel(self) -> bool:
        return (
            self.new_category is not None
            and self.category_id is None
            and not self.category_confirmed
        )


class Decision(StrEnum):
    COMMIT = "commit"  # silent one-line receipt
    COMMIT_SHOW = "commit_show"  # saved, but show the parse and nudge undo
    CONFIRM = "confirm"  # ask before saving
    CONFIRM_CATEGORY = "confirm_category"
    CONFIRM_DUPLICATE = "confirm_duplicate"


class Thresholds(BaseModel):
    commit: float = 0.85
    ask: float = 0.6
    always_confirm_multi: bool = True


def decide_add(proposals: list[Proposal], t: Thresholds) -> Decision:
    if any(p.duplicate_of for p in proposals):
        return Decision.CONFIRM_DUPLICATE
    if any(p.novel for p in proposals):
        return Decision.CONFIRM_CATEGORY
    if all(p.confirmed for p in proposals):
        return Decision.COMMIT
    lowest = min(p.confidence for p in proposals)
    if lowest < t.ask:
        return Decision.CONFIRM
    if len(proposals) > 1 and t.always_confirm_multi:
        return Decision.CONFIRM
    if lowest < t.commit:
        return Decision.COMMIT_SHOW
    return Decision.COMMIT


def combined_confidence(*scores: float | None) -> float:
    """Chain of stages: take the weakest link, not the product. Product punishes
    long pipelines too hard for self-reported scores."""
    vals = [s for s in scores if s is not None]
    return round(min(vals), 3) if vals else 0.0
