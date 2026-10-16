# BayOS Gateway image for Google Cloud Run.
# Cloud Run sets PORT. Secrets arrive as environment variables at runtime
# and are never copied into this image.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bayos

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY main.py bayos_public.py bayos_calendar.py bayos_oauth.py bayos_mcp.py bayos_runner.py ./
COPY shortcuts/runner/BayOS-Runner.shortcut ./shortcuts/runner/BayOS-Runner.shortcut

USER bayos

EXPOSE 8080

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
