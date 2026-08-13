.PHONY: install test lint run docker-up docker-down logs
install:
	python -m pip install -e '.[dev]'
test:
	pytest -q
lint:
	ruff check .
run:
	python -m app.main
docker-up:
	docker compose up -d --build
docker-down:
	docker compose down
logs:
	docker compose logs -f app
