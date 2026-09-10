FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/app/.local/bin:$PATH

RUN useradd --create-home --uid 10001 app
COPY --from=builder /install /usr/local
USER app
WORKDIR /app
EXPOSE 8080

CMD ["uvicorn", "central_brain.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
