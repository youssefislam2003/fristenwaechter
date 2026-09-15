.PHONY: venv install test test-unit test-integration lint typecheck migrate run worker clean

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

venv:
	python3.11 -m venv $(VENV)

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-test.txt

# Full suite: pure unit tests + Docker-backed integration tests. Requires a
# running Docker daemon; integration tests skip loudly (not silently) if one
# isn't reachable — see tests/integration/conftest.py.
test:
	$(PY) -m pytest tests/ -v

# Pure logic only (tests/test_dates.py) — no Docker required.
test-unit:
	$(PY) -m pytest tests/ -v --ignore=tests/integration

test-integration:
	$(PY) -m pytest tests/integration -v

lint:
	$(PY) -m ruff check app/

typecheck:
	$(PY) -m mypy --strict app/

migrate:
	$(PY) -m alembic upgrade head

run:
	$(PY) -m uvicorn app.main:app --reload

worker:
	$(PY) -m app.worker

clean:
	rm -rf $(VENV) .pytest_cache **/__pycache__
