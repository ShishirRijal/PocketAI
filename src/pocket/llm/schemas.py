"""Structured outputs for every LLM stage.

These are what the model is asked to fill in. They are deliberately *not* the DB
shape; core/mapper code turns them into rows (anti-goal: coupling the schema to
LLM output). Keep them flat-ish and provider friendly: no Decimal, no unions
beyond `X | None`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Intent(StrEnum):
    ADD = "ADD"
    EDIT = "EDIT"
    DELETE = "DELETE"
    QUERY = "QUERY"
    HELP = "HELP"
    CHITCHAT = "CHITCHAT"


class IntentResult(BaseModel):
    intent: Intent
    confidence: float = Field(ge=0, le=1)


TagKind = Literal["merchant", "activity", "person", "place", "other"]


class ExtractedTag(BaseModel):
    name: str
    kind: TagKind = "other"


class ExtractedTransaction(BaseModel):
    amount: float = Field(gt=0, description="positive number, no currency symbol")
    currency: str = Field(description="ISO-4217 code, e.g. EUR, NPR, USD")
    direction: Literal[
        "expense", "income", "transfer", "lent", "borrowed", "got_back", "paid_back"
    ] = "expense"
    merchant: str | None = None
    location: str | None = Field(
        default=None,
        description="where it happened if mentioned: a place, area or city ('Pirita beach', 'Kathmandu'); not the shop name",
    )
    category_hint: str | None = Field(
        default=None, description="best guess category, prefer one of the user's categories"
    )
    tags: list[ExtractedTag] = Field(default_factory=list)
    occurred_at: str | None = Field(
        default=None,
        description="ISO-8601 local datetime or date if the user mentioned when; null = now",
    )
    note: str | None = None
    confidence: float = Field(ge=0, le=1)
    reasoning: str = ""

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()[:3]


class ExtractionResult(BaseModel):
    transactions: list[ExtractedTransaction]


class CategorizationResult(BaseModel):
    """Either picks an existing category by id or proposes a new name."""

    category_id: int | None = None
    new_category_name: str | None = None
    rationale: str = ""
    confidence: float = Field(ge=0, le=1)


EditField = Literal[
    "amount", "currency", "category", "merchant", "location", "note", "date", "direction", "tags"
]


class FieldChange(BaseModel):
    field: EditField
    value: str = Field(description="new value as text, e.g. '29', 'Groceries', '2026-09-27'")


class EditResolution(BaseModel):
    """Which transaction does the user mean, and what should change?

    target_index refers to the numbered recent list in the prompt (1 = newest).
    """

    target_index: int | None = Field(default=None, description="1-based index into recent list")
    changes: list[FieldChange] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    reasoning: str = ""


class DeleteResolution(BaseModel):
    target_indexes: list[int] = Field(default_factory=list, description="1-based, newest first")
    confidence: float = Field(ge=0, le=1)
    reasoning: str = ""


Period = Literal[
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_year",
    "last_year",
    "last_7_days",
    "last_30_days",
    "all_time",
    "custom",
]

QueryKind = Literal[
    "total",  # sum
    "count",
    "average",  # per transaction
    "daily_average",
    "list",
    "breakdown",  # grouped by group_by
    "top_merchants",
    "top_categories",
    "top_tags",
    "largest",
]


class QueryPlan(BaseModel):
    kind: QueryKind
    period: Period = "this_month"
    start_date: str | None = Field(default=None, description="YYYY-MM-DD, only for custom")
    end_date: str | None = Field(default=None, description="YYYY-MM-DD inclusive, only for custom")
    category: str | None = None
    categories: list[str] = Field(
        default_factory=list, description="several categories, for umbrella words like 'food'"
    )
    merchant: str | None = None
    tag: str | None = None
    direction: Literal["expense", "income", "any"] = "expense"
    group_by: Literal["category", "merchant", "tag", "day", "weekday", "month"] | None = None
    weekdays_only: bool = False
    weekends_only: bool = False
    limit: int = Field(default=5, ge=1, le=50)
    confidence: float = Field(ge=0, le=1)


class Summary(BaseModel):
    text: str


class DocumentRow(BaseModel):
    """One transaction line read from a statement, screenshot or receipt."""

    date: str = Field(description="YYYY-MM-DD, the day the transaction happened")
    time: str | None = Field(
        default=None, description="HH:MM if the document shows a time, else null"
    )
    description: str = Field(description="the transaction text as printed, e.g. 'Rimi Naulaste'")
    amount: float = Field(gt=0, description="positive amount, no sign")
    currency: str = Field(description="ISO-4217, e.g. EUR")
    direction: Literal["expense", "income", "transfer"] = Field(
        description="money out = expense, money in = income; moves between your own accounts/pockets = transfer"
    )
    merchant: str | None = Field(
        default=None, description="clean merchant name, e.g. 'Rimi', 'Bolt'"
    )
    location: str | None = Field(
        default=None, description="city or place if printed, e.g. 'Tallinn'"
    )
    category_hint: str | None = Field(
        default=None, description="best fitting category from the user's list"
    )
    balance_after: float | None = Field(
        default=None, description="running balance printed on that row, if any"
    )
    note: str | None = Field(
        default=None, description="reference/memo text worth keeping, else null"
    )
    confidence: float = Field(ge=0, le=1)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()[:3]


class DocumentExtraction(BaseModel):
    kind: Literal["statement", "receipt", "screenshot", "other"]
    institution: str | None = Field(default=None, description="bank or shop name, e.g. 'Revolut'")
    account_holder: str | None = Field(
        default=None, description="the statement owner's name if printed"
    )
    account_currency: str | None = None
    # only if printed on this page (statement summary box)
    opening_balance: float | None = None
    closing_balance: float | None = None
    total_money_out: float | None = None
    total_money_in: float | None = None
    rows: list[DocumentRow] = Field(default_factory=list)
