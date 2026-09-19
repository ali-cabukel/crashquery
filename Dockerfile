# syntax=docker/dockerfile:1
# CLI/TUI image. Postgres stays in docker-compose.yml.
#
#   docker compose up -d --build
#   docker compose exec crashquery crashquery check
#   docker compose exec crashquery crashquery ask "How many people were killed in 2022?"

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1

RUN pip install --no-cache-dir poetry

COPY pyproject.toml poetry.lock README.md ./
COPY src ./src

RUN poetry install --only main

ENTRYPOINT ["crashquery"]
CMD ["check"]
