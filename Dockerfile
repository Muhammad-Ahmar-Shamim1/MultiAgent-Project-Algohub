FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY web_department ./web_department

RUN pip install --no-cache-dir .

RUN groupadd --system app && useradd --system --gid app --home /app app \
    && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["uvicorn", "web_department.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
