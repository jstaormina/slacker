# Slacker

Extract undocumented knowledge from Slack conversations and generate organized knowledge base articles.

Uses Playwright to scrape messages directly from the Slack web UI — no API tokens needed. Supports multiple AI providers: Claude CLI, Claude API, OpenAI-compatible endpoints, LM Studio, and Ollama.

Available as a CLI tool, a web UI, or a Docker container.

## Quick Start (CLI)

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Log in to Slack (one time — saves browser session)
python slack_search.py --login --workspace https://app.slack.com/client/YOUR_WORKSPACE_ID

# Extract knowledge from a channel
python slack_search.py \
  --urls https://app.slack.com/client/TGG6BJ82E/CGG6BJN5Q \
  --topic "AML" \
  --provider cli
```

Search multiple channels with a specific output format:

```bash
python slack_search.py \
  --urls "https://app.slack.com/client/TGG6BJ82E/CGG6BJN5Q,https://app.slack.com/client/TGG6BJ82E/CXXYYZZWW" \
  --topic "onboarding" \
  --provider ollama \
  --format docx
```

Pre-built binaries (`slacker`) are available on the [releases page](../../releases).

## Web UI

The web UI provides a browser-based interface with real-time progress via SSE and ZIP download of results.

```bash
# Run directly
pip install -r requirements.txt flask gunicorn
python web.py

# Or with gunicorn
gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 --worker-class gthread --timeout 300 web:app
```

The AI provider is configured via environment variables (see [Environment Variables](#environment-variables) below). The web UI status card shows the current provider and Slack session state.

## Docker

A Docker image is published to GHCR on every push to `main`:

```
ghcr.io/jstaormina/slacker:latest
```

### docker compose

```bash
docker compose up --build
```

The default [docker-compose.yaml](docker-compose.yaml) mounts your local Slack session, cache, and Claude credentials. Edit the `environment` section to switch providers:

```yaml
environment:
  - AI_PROVIDER=cli
  # - ANTHROPIC_API_KEY=sk-ant-...      # for api provider
  # - OPENAI_BASE_URL=https://...       # for openai provider
  # - OPENAI_API_KEY=sk-...             # for openai provider
  # - OPENAI_MODEL=gpt-4o              # for openai provider
```

### Passing a Slack session to the container

The Playwright browser session must be created on the host first, then mounted or copied into the container:

```bash
# Create session locally
python slack_search.py --login --workspace https://app.slack.com/client/YOUR_WORKSPACE_ID

# docker compose mounts .slack-session automatically
docker compose up
```

For Kubernetes, copy the session into the pod:

```bash
kubectl cp .slack-session slacker/<pod-name>:/data/slack-session
```

## Kubernetes

Full Kustomize manifests are in [k8s/](k8s/):

```bash
# Create the namespace and Claude credentials secret
kubectl create namespace slacker
kubectl create secret generic claude-credentials \
  --from-file=.credentials.json=$HOME/.claude/.credentials.json \
  -n slacker

# Deploy
kubectl apply -k k8s/

# Copy Slack session into the pod
kubectl cp .slack-session -n slacker <pod-name>:/data/slack-session
```

The deployment uses Cilium ingress and cert-manager for TLS. Edit [k8s/ingress.yaml](k8s/ingress.yaml) to set your domain.

## AI Providers

The provider is selected via the `AI_PROVIDER` environment variable (for web/Docker) or `--provider` flag (for CLI). If neither is set, the CLI prompts interactively.

### Claude CLI (default)

Uses your existing Claude Code installation and Max subscription. No API key needed.

```bash
AI_PROVIDER=cli
```

Requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code) to be installed and authenticated. In Docker, mount `~/.claude/.credentials.json` into the container.

### Claude API

Pay-per-use via Anthropic API key.

```bash
AI_PROVIDER=api
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-sonnet-4-6  # optional, default: claude-sonnet-4-6
```

### OpenAI-compatible

Works with any OpenAI-compatible endpoint (OpenAI, Together, vLLM, etc.).

```bash
AI_PROVIDER=openai
OPENAI_BASE_URL=https://api.openai.com
OPENAI_API_KEY=sk-...          # optional, depends on endpoint
OPENAI_MODEL=gpt-4o            # optional, auto-detected if omitted
```

### LM Studio (local)

Free, runs locally via LM Studio's OpenAI-compatible API.

```bash
AI_PROVIDER=lmstudio
LMSTUDIO_URL=http://localhost:1234   # optional, default
LMSTUDIO_MODEL=...                   # optional, auto-detected
```

### Ollama (local)

Free, runs entirely on your machine. Requires [Ollama](https://ollama.com).

```bash
AI_PROVIDER=ollama
OLLAMA_URL=http://localhost:11434    # optional, default
OLLAMA_MODEL=llama3.1               # optional, default: llama3.1
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PROVIDER` | `cli` | AI backend: `cli`, `api`, `openai`, `lmstudio`, `ollama` |
| `ANTHROPIC_API_KEY` | | Anthropic API key (for `api` provider) |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Model name (for `cli`/`api` providers) |
| `OPENAI_BASE_URL` | | Base URL for OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | | API key for OpenAI-compatible endpoint |
| `OPENAI_MODEL` | *(auto-detect)* | Model name for OpenAI-compatible endpoint |
| `LMSTUDIO_URL` | `http://localhost:1234` | LM Studio server URL |
| `LMSTUDIO_MODEL` | *(auto-detect)* | LM Studio model name |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `llama3.1` | Ollama model name |
| `SLACK_SESSION_DIR` | `.slack-session` | Playwright browser session directory |
| `SLACK_CACHE_DIR` | `.slack-cache` | Scraped message cache directory |
| `KB_OUTPUT_DIR` | `kb` | Output directory for generated articles |

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--login` | | Open browser to log in and save Slack session |
| `--workspace` | | Slack workspace URL (required with `--login`) |
| `--session-dir` | `.slack-session` | Directory for saved browser session |
| `--provider` | *(interactive)* | AI provider: `cli`, `api`, `openai`, `lmstudio`, `ollama` |
| `--claude-api-key` | `$ANTHROPIC_API_KEY` | Anthropic API key (for `api` provider) |
| `--model` | `claude-sonnet-4-6` | Model for Claude API provider |
| `--openai-url` | | Base URL for OpenAI-compatible API |
| `--openai-api-key` | | API key for OpenAI-compatible provider |
| `--openai-model` | *(auto-detect)* | Model for OpenAI-compatible provider |
| `--lmstudio-model` | *(auto-detect)* | Model for LM Studio provider |
| `--lmstudio-url` | `http://localhost:1234` | LM Studio server URL |
| `--ollama-model` | `llama3.1` | Model for Ollama provider |
| `--ollama-url` | `http://localhost:11434` | Ollama server URL |
| `--urls` | *(required)* | Comma-separated Slack channel URLs |
| `--topic` | *(required)* | Topic to extract knowledge about |
| `--output` | `kb` | Output directory for KB articles |
| `--format` | `md` | Output format: `md`, `html`, `pdf`, `docx` |
| `--scroll-delay` | `3.0` | Seconds between scroll steps when scraping |
| `--cache-dir` | `.slack-cache` | Directory to cache raw scraped messages |
| `--no-cache` | | Skip cache and always re-scrape via Playwright |

## Standalone Scraper

The Playwright scraper can be used standalone to export a channel to markdown:

```bash
python scrape_slack.py --login --workspace https://app.slack.com/client/TGG6BJ82E
python scrape_slack.py --url https://app.slack.com/client/TGG6BJ82E/CGG6BJN5Q --output general.md
```

## Building from Source

```bash
./build.sh
# Binary at dist/slacker
```

Requires Python 3.10+. The build script creates a venv, installs dependencies, installs Chromium for Playwright, and builds the binary.

## How It Works

1. **Scrapes** messages from Slack channels via Playwright (scrolls to top, extracts all messages)
2. **Classifies** messages with AI to find knowledge-sharing conversations
3. **Clusters** related messages by time proximity
4. **Gathers context** — surrounding messages from the full channel history for each cluster
5. **Deduplicates** overlapping clusters
6. **Extracts knowledge** from each cluster (detailed content, not just summaries)
7. **Groups** related extractions by topic across channels and time periods
8. **Synthesizes** cohesive KB articles from grouped extractions
9. **Generates** a structured knowledge base with table of contents, organized by category
