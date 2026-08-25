.PHONY: bootstrap api worker web dev test e2e smoke-train docker-build docker-up docker-down docker-logs docker-smoke

bootstrap:
	bash scripts/bootstrap.sh

api:
	cd backend && uvicorn app.main:app --host $${APP_HOST:-127.0.0.1} --port $${APP_PORT:-8000} --reload

worker:
	cd backend && python -m app.worker.main

web:
	cd frontend && npm run dev

dev:
	bash scripts/dev.sh

test:
	cd backend && python -m pytest -q

e2e:
	cd frontend && npm run test:e2e

smoke-train:
	python scripts/smoke_train.py --base-url http://127.0.0.1:8000/api/v1

# ---------- Docker ----------
docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=100

docker-smoke:
	python scripts/smoke_train.py --base-url http://127.0.0.1:8000/api/v1
