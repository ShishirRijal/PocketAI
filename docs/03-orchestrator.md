# 3. The orchestrator

`src/pocket/core/orchestrator.py` is the brain. It's deliberately **not an AI agent**: no "the model decides which tool to call". It's a plain state machine where the LLM is asked narrow questions ("what's the intent?", "extract the transactions") and **code** decides what happens. You can put a breakpoint anywhere and see exactly why something happened.

## The Turn

Every message is handled inside a `Turn`, a small object holding everything about this one message:

| field | what |
|---|---|
| `s` | the SQLAlchemy session: one DB transaction for the whole message |
| `user` | you (base currency, timezone) |
| `session` | short-lived state: recent transaction ids, the list you were last shown, the last action (for undo) |
| `now` | the clock (injectable, so tests can time-travel) |
| `stages` | a log of what happened (`intent`, `extract`, `categorize via=name_match`, `policy decision=commit`, …), which ends up in the JSON log and in `/admin/replay` output |
| `outcome` | the final label stored on the raw message (`added`, `edited`, `pending`, …) |

## Routing: what kind of message is this?

```mermaid
flowchart TD
  IN["message text"]:::user --> MED{"has a photo /<br/>voice note?"}:::core
  MED -- yes --> MH["media handler<br/>services/media.py"]:::ai
  MED -- no --> N["normalize()<br/>unicode, signatures,<br/>२५० → 250"]:::core
  N --> PEND{"a question is<br/>pending?"}:::core
  PEND -- yes --> ANS["_answer_pending()"]:::core
  ANS -- "it was an answer" --> DONE["reply"]:::user
  ANS -- "not an answer:<br/>drop the question" --> CMD
  PEND -- no --> CMD{"deterministic<br/>command?<br/>commands.py"}:::core
  CMD -- yes --> RUN["run command<br/>no LLM"]:::core --> DONE
  CMD -- no --> YN{"bare yes/no,<br/>thanks, hi?"}:::core
  YN -- yes --> SMALL["canned reply"]:::core --> DONE
  YN -- no --> INT["LLM: intent"]:::ai
  INT --> ADD["ADD"]:::ai
  INT --> EDIT["EDIT"]:::ai
  INT --> DEL["DELETE"]:::ai
  INT --> QRY["QUERY"]:::ai
  INT --> HELP["HELP → help text"]:::core
  INT --> CHAT["CHITCHAT → 'I only keep<br/>your money log'"]:::core
  ADD & EDIT & DEL & QRY --> DONE

  classDef user fill:#dbeafe,stroke:#2563eb,color:#0f172a
  classDef core fill:#dcfce7,stroke:#16a34a,color:#0f172a
  classDef ai fill:#ffedd5,stroke:#ea580c,color:#0f172a
```

The order matters:
1. **Pending answers first.** If Pocket just asked "Two transactions? Reply yes…", then "yes" must mean *that*, not a new message.
2. **Commands second.** `undo`, `edit 2 amount 29`, `show week`, `budget cafes 80` are exact and free. They work during an LLM outage and after the daily cost cap.
3. **The LLM last**, only for real natural language.

## ADD: turning an extraction into a saved transaction

```mermaid
flowchart TD
  T["text"]:::user --> X["LLM extract<br/>→ list of ExtractedTransaction"]:::ai
  X --> G["guards<br/>currency from text wins;<br/>amount not in text → conf ≤ 0.5"]:::core
  G --> P["_propose() per transaction"]:::core
  P --> P1["amount → minor units<br/>money.to_minor"]:::core
  P --> P2["date → UTC<br/>'yesterday' = noon local"]:::core
  P --> P3["FX to base currency<br/>services/fx.py"]:::core
  P --> P4["categorize<br/>(3 tiers, below)"]:::core
  P --> P5["tags: merchant, activity,<br/>person, place"]:::core
  P --> P6["duplicate check<br/>same amount+currency+<br/>direction(+merchant) ≤ 5 min"]:::store
  P1 & P2 & P3 & P4 & P5 & P6 --> PR["Proposal<br/>(JSON-safe)"]:::core
  PR --> POL["policy.decide_add()"]:::core

  classDef user fill:#dbeafe,stroke:#2563eb,color:#0f172a
  classDef core fill:#dcfce7,stroke:#16a34a,color:#0f172a
  classDef ai fill:#ffedd5,stroke:#ea580c,color:#0f172a
  classDef store fill:#fef9c3,stroke:#ca8a04,color:#0f172a
```

A **`Proposal`** (`core/policy.py`) is a transaction that hasn't been saved yet. It's JSON-serializable on purpose, so it can sit in the `pending_actions` table while Pocket waits for your "yes".

### Categorization: cheapest answer first

```mermaid
flowchart LR
  M{"merchant seen before?<br/>(most common category<br/>for 'Rimi')"}:::store -- yes --> A["use it<br/>conf 0.95"]:::core
  M -- no --> N{"the LLM's category_hint<br/>matches one of yours?<br/>exact / plural / typo"}:::core
  N -- "score ≥ 0.85" --> B["use it"]:::core
  N -- no --> L["LLM categorizer<br/>with your category list"]:::ai
  L -- "picked an existing id" --> C["use it"]:::core
  L -- "proposed a new name" --> R{"new name fuzzy-<br/>matches existing?"}:::core
  R -- yes --> D["use existing"]:::core
  R -- no --> NEW["novel category<br/>→ ask you (a/b/c)"]:::ask

  classDef core fill:#dcfce7,stroke:#16a34a,color:#0f172a
  classDef ai fill:#ffedd5,stroke:#ea580c,color:#0f172a
  classDef store fill:#fef9c3,stroke:#ca8a04,color:#0f172a
  classDef ask fill:#fee2e2,stroke:#dc2626,color:#0f172a
```

Most messages never need a separate categorizer call: the extractor already suggests a category from your list ("Groceries"), which is matched by name ("Grocery" and "Groceries" count as the same), and a known merchant goes wherever it went last time. New categories always need your OK, which stops "Grocery", "Groceries" and "Food shopping" from all existing at once.

### Policy: save, or ask?

`decide_add(proposals, thresholds)` in `core/policy.py`, checked top to bottom:

```mermaid
flowchart TD
  S["proposals"]:::core --> D1{"looks like a<br/>duplicate?"}:::core
  D1 -- yes --> A1["ask: 'Looks like a repeat…<br/>save this one too?'"]:::ask
  D1 -- no --> D2{"proposes a new<br/>category?"}:::core
  D2 -- yes --> A2["ask: a) create b) Misc<br/>c) name one"]:::ask
  D2 -- no --> D3{"you already said<br/>yes to exactly this?"}:::core
  D3 -- yes --> C1["COMMIT"]:::core
  D3 -- no --> D4{"any confidence<br/>< 0.6?"}:::core
  D4 -- yes --> A3["ask: 'Not 100% sure…<br/>save it?'"]:::ask
  D4 -- no --> D5{"more than one<br/>transaction?"}:::core
  D5 -- yes --> A4["ask: 'Two transactions?<br/>yes / 1 / 2'"]:::ask
  D5 -- no --> D6{"confidence<br/>< 0.85?"}:::core
  D6 -- yes --> C2["COMMIT_SHOW<br/>save + 'not fully sure'"]:::core
  D6 -- no --> C3["COMMIT<br/>one-line receipt"]:::core

  classDef core fill:#dcfce7,stroke:#16a34a,color:#0f172a
  classDef ask fill:#fee2e2,stroke:#dc2626,color:#0f172a
```

Confidence is the **minimum** across stages (extraction, categorization), not the product. Receipts and voice notes pass `force_confirm=True`, so they always ask. Thresholds live in settings (`POCKET_CONFIDENCE_COMMIT=0.85`, `POCKET_CONFIDENCE_ASK=0.6`).

After a commit, **after-commit hooks** run and can add lines to the reply: the budget nudge ("🟠 Cafes: €68 of €80 this month") and the unusual-amount nudge ("📈 about 12× your usual Cafes spend").

## Pending actions: the conversation's memory

When Pocket asks something, it stores a row in `pending_actions` (one per user, 15-minute expiry). Your next message is checked against it first.

```mermaid
stateDiagram-v2
  [*] --> Idle
  Idle --> ConfirmAdd: low confidence or multiple txns
  Idle --> ConfirmCategory: novel category
  Idle --> ConfirmDuplicate: looks like a repeat
  Idle --> ChooseTarget: edit/delete, unclear which one
  Idle --> ConfirmDelete: several or low-confidence delete

  ConfirmAdd --> Idle: yes (save all) / 1,2 (save some) / no
  ConfirmAdd --> ConfirmAdd: "the metro was 2.40" (correction)
  ConfirmCategory --> Idle: a / b / category name / no
  ConfirmCategory --> ConfirmCategory: c ("what should I call it?")
  ConfirmDuplicate --> Idle: yes (save) / no
  ChooseTarget --> Idle: a number from the list
  ConfirmDelete --> Idle: yes / no

  ConfirmAdd --> Idle: unrelated message (question dropped)
  ConfirmCategory --> Idle: unrelated message
  ChooseTarget --> Idle: unrelated message
  Idle --> Idle: 15 min expiry

  classDef ask fill:#fee2e2,stroke:#dc2626,color:#0f172a
  class ConfirmAdd, ConfirmCategory, ConfirmDuplicate, ChooseTarget, ConfirmDelete ask
```

Details worth knowing:
- **"Tell me what's off."** While a proposal is pending, a correction like "the metro was 2.40" runs the *edit resolver* against the unsaved proposals instead of saved transactions. Then Pocket asks again with the fix applied. It only does this if the intent classifier says EDIT; a brand-new expense ("lunch 12") drops the question and is logged normally.
- A reply to "a/b/c" that contains a number or a "?" is never taken as a category name.
- `no`, `cancel`, `hoina` always clear the pending question.

## EDIT and DELETE

Both work against a **numbered list** of recent transactions (1 = newest). If Pocket just showed you a list (`edit`, `review`, "which one?"), the numbers refer to that list; otherwise to your 5 most recently logged.

```mermaid
flowchart TD
  E["'sorry it was 29'<br/>'make the coffee one groceries'"]:::user --> R["LLM edit resolver<br/>→ target index + field changes"]:::ai
  R --> C{"target clear and<br/>confidence ≥ 0.6?"}:::core
  C -- no --> ASK["show recent list:<br/>'Which one should I change?'"]:::ask
  C -- yes --> AP["_apply_edit()<br/>amount / currency / category /<br/>merchant / note / date /<br/>direction / tags"]:::core
  AP --> V["TransactionRepo.update()<br/>writes transaction_versions<br/>{field: [old, new]}"]:::store
  V --> REP["'✏️ Updated last transaction:<br/>€23.00 → €29.00'"]:::user

  classDef user fill:#dbeafe,stroke:#2563eb,color:#0f172a
  classDef core fill:#dcfce7,stroke:#16a34a,color:#0f172a
  classDef ai fill:#ffedd5,stroke:#ea580c,color:#0f172a
  classDef store fill:#fef9c3,stroke:#ca8a04,color:#0f172a
  classDef ask fill:#fee2e2,stroke:#dc2626,color:#0f172a
```

- Changing the amount of a foreign-currency transaction keeps its original FX rate; changing the currency fetches a fresh one.
- Editing to a category that doesn't exist creates it (you asked explicitly), marked "(new)".
- Deletes are soft (`deleted_at`), ask when ambiguous or when deleting several, and are reversible with `undo` or the dashboard's "Restore".
- Direct forms skip the LLM entirely: `edit 2 amount 29`, `edit last category groceries`, `delete 3`, `d 1,2`.

## Undo

`undo` (or `u`) reverses the **last transaction change** within 5 minutes:

| last action | undo does |
|---|---|
| add | soft-deletes what was added |
| edit | re-applies the *old* values from the exact version rows that edit wrote |
| delete | restores the rows |

Undo itself writes versions too, so the history shows everything. Any other state-changing command (setting a budget, creating a goal) clears the undo target, so `undo` never reaches back to an older, unrelated action. Read-only commands (`categories`, `budgets`, `goals`) keep it.

The undo target lives in the **session store** (`core/session.py`): Redis when configured, otherwise a `sessions` table. Either way it survives a restart.

## QUERY

```mermaid
flowchart LR
  Q["'how much on food<br/>this month?'"]:::user --> PL["LLM query planner<br/>→ QueryPlan"]:::ai
  PL --> EX["services/queries.py<br/>execute(plan)"]:::core
  EX --> DB[("filter in SQL,<br/>group in Python<br/>(timezone-aware)")]:::store
  DB --> TXT["'September (so far) ·<br/>Groceries + Restaurants + Cafes:<br/>€412.30 across 31'"]:::user

  classDef user fill:#dbeafe,stroke:#2563eb,color:#0f172a
  classDef core fill:#dcfce7,stroke:#16a34a,color:#0f172a
  classDef ai fill:#ffedd5,stroke:#ea580c,color:#0f172a
  classDef store fill:#fef9c3,stroke:#ca8a04,color:#0f172a
```

The LLM never writes SQL. It fills a small typed **`QueryPlan`**:

```text
kind      total | count | average | daily_average | list | breakdown |
          top_merchants | top_categories | top_tags | largest
period    today | yesterday | this_week | last_week | this_month | last_month |
          this_year | last_year | last_7_days | last_30_days | all_time | custom
category / categories   one, or several for umbrella words ("food", "bills")
merchant, tag           person tags too: "with arjun"
direction               expense | income | any
group_by                category | merchant | tag | day | weekday | month
weekdays_only / weekends_only, limit
```

The executor turns that into SQL filters and formats the answer itself, so numbers can't be hallucinated. Queries are rate-limited to 5/min.

Next: [4. The LLM layer](04-llm-layer.md)
