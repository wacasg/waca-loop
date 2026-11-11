# WACA Loop — hosted remote MCP server (hosted mode, Cloud Run).
# Runs `waca-loop-serve` (Streamable HTTP; the server is its own OAuth AS,
# see src/waca_loop/auth/as_provider.py). BigQuery auth is ADC via the Cloud Run
# service account (BQ_AUTH=adc) — no user creds baked in.
FROM python:3.12-slim

WORKDIR /app

# Install the package first so source edits reuse the dependency layer.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Cloud Run injects $PORT; serve_http binds 0.0.0.0:$PORT (config.py defaults to 8080).
ENV SERVER_MODE=http \
    BQ_AUTH=adc \
    PYTHONUNBUFFERED=1

# Non-root runtime user (Cloud Run best practice).
RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8080

CMD ["waca-loop-serve"]
