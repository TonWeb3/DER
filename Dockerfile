# Deriv Touch/No Touch — Breakout Probability bot
# Build:  docker build -t deriv-touch .
# Run:    docker run -p 8070:8070 \
#           -v "$(pwd)/config.json:/app/config.json" \
#           -v "$(pwd)/state_data.json:/app/state_data.json" \
#           -v "$(pwd)/logs:/app/logs" \
#           deriv-touch
# The mounts are optional but recommended: without them, saved Settings, trade
# state and signal logs live only inside the container and vanish when it is
# removed. config.json also holds the Deriv PAT — mounting it instead of relying
# on the copy baked in at build time keeps the token out of the image layers.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so code edits don't invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY main.py .
COPY bot/ ./bot/
COPY templates/ ./templates/

# Baseline config + the runtime-written files. The app rewrites config.json when
# Settings are saved and state_data.json on every trade, so both must be
# writable by the runtime user — hence the chown below rather than a bare COPY.
COPY config.json .
RUN mkdir -p logs \
    && printf '{\n  "active_trades": [],\n  "trade_history": [],\n  "entered_markets": []\n}\n' > state_data.json \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8070

# python-only healthcheck (curl/wget aren't in the slim base image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8070/health', timeout=4).status == 200 else 1)"

# Single worker is required: the bot keeps candles, breakout stats and open-trade
# state in process memory, and runs one Deriv WebSocket connection. Multiple
# workers would each open their own socket and trade off their own divergent state.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8070", "--workers", "1"]
