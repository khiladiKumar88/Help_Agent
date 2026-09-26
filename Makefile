# PaperMind — paper-trading only.
.PHONY: install dev dev-sim backend backend-sim frontend test test-backend test-frontend lint typecheck check

BACKEND = cd backend &&
FRONTEND = cd frontend &&

install:            ## install backend (uv) and frontend (npm) dependencies
	$(BACKEND) uv sync
	$(FRONTEND) npm install

dev:                ## backend (live public crypto data via ccxt) + frontend on http://127.0.0.1:5173
	@$(MAKE) -j2 backend frontend

dev-sim:            ## same, but with the offline SIMULATED feed (no network needed)
	@$(MAKE) -j2 backend-sim frontend

backend:
	$(BACKEND) uv run uvicorn papermind.main:app --host 127.0.0.1 --port 8000 --reload

backend-sim:
	$(BACKEND) PAPERMIND_CRYPTO_PROVIDER=simulated PAPERMIND_DB_URL=sqlite:///papermind-sim.db \
		uv run uvicorn papermind.main:app --host 127.0.0.1 --port 8000 --reload

frontend:
	$(FRONTEND) npm run dev

test: test-backend test-frontend

test-backend:
	$(BACKEND) uv run pytest --cov=papermind --cov-report=term-missing

test-frontend:
	$(FRONTEND) npm test

lint:
	$(BACKEND) uv run ruff check . && uv run ruff format --check .

typecheck:
	$(BACKEND) uv run mypy papermind
	$(FRONTEND) npm run typecheck

check: lint typecheck test   ## everything CI would run
