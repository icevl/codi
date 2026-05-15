FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEXBOT_DIR=/root/.codexbot \
    CODEX_HOME=/root/.codex

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        nodejs \
        npm \
        tmux \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex @anthropic-ai/claude-code pnpm@10.25.0

WORKDIR /app

# Build the Vite SPA first; the Python backend serves it from web-ui/dist.
COPY web-ui ./web-ui
RUN cd web-ui && pnpm install --frozen-lockfile && pnpm run build && rm -rf node_modules

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /usr/local/bin/codexbot-entrypoint
RUN chmod +x /usr/local/bin/codexbot-entrypoint

ENV CODEXBOT_WEB_DIST=/app/web-ui/dist

ENTRYPOINT ["codexbot-entrypoint"]
