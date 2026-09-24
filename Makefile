.PHONY: install test live lint fmt typecheck check serve chat demo eval migrate docker

install:
	uv sync --extra postgres

test:
	uv run pytest -q

live:  ## golden set against the real LLM chain (needs keys, costs cents)
	uv run pytest -m live -q

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:
	uv run mypy src

check: lint typecheck test

serve:
	uv run pocket serve --reload

chat:
	uv run pocket chat

demo:
	uv run pocket demo --months 6

eval:
	uv run pocket eval --out docs/eval.md

migrate:
	uv run pocket migrate

docker:
	docker build -t pocket:dev .
