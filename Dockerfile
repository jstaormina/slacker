FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

# Install Node.js + Claude CLI
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g @anthropic-ai/claude-code && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir gunicorn

COPY . .

# Persistent data lives under /data (mount a PVC here in K8s)
RUN mkdir -p /data/slack-session /data/slack-cache /data/kb /data/claude-auth && \
    ln -s /data/claude-auth /root/.claude

# Prevent claude login from trying to open a browser inside the container
ENV AI_PROVIDER=cli \
    SLACK_SESSION_DIR=/data/slack-session \
    SLACK_CACHE_DIR=/data/slack-cache \
    KB_OUTPUT_DIR=/data/kb \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BROWSER=""

EXPOSE 5000

# gthread workers handle SSE connections via threads
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--worker-class", "gthread", "--timeout", "300", "web:app"]
