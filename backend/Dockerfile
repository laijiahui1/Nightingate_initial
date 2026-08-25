# Nightingale backend image — FastAPI + uvicorn, single worker.
#
# Dev-oriented single stage: installs the runtime deps AND the dev extra so
# `make test` / `make fmt` can exec pytest/ruff inside the running container.
# The compose service bind-mounts ./backend:/app so source changes are live.
FROM python:3.12-slim

# curl is only used by the container healthcheck (see docker-compose.yml).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy metadata first so dependency installs can be cached independently.
COPY pyproject.toml ./
COPY app ./app

# Editable install: /app is bind-mounted at runtime, so `uvicorn app.main:app`
# and `pytest` always run against the current host code.
RUN pip install --no-cache-dir -e ".[dev]"

EXPOSE 8000

# Single worker intentionally — keeps the in-process WebSocket fan-out correct
# (docs/ARCHITECTURE.md §9).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
