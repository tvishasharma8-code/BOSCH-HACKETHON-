DEMO_DIR := opentelemetry-demo

# The telemetry backends (Jaeger, Prometheus, Grafana, OpenSearch) live in a
# SEPARATE compose layer. Plain compose.yaml has none of them, and they are the
# entire tool surface the agent depends on — always bring up both files.
COMPOSE := docker compose -f compose.yaml -f compose.observability.yaml

.PHONY: demo-up demo-down demo-logs action-api test fmt

demo-up:
	cd $(DEMO_DIR) && $(COMPOSE) up -d

demo-down:
	cd $(DEMO_DIR) && $(COMPOSE) down --remove-orphans

demo-logs:
	cd $(DEMO_DIR) && $(COMPOSE) logs -f --tail=100

action-api:
	uvicorn envkit.action_api.app:app --host 127.0.0.1 --port 8700

test:
	pytest -q

fmt:
	python -m compileall -q agent envkit tests
