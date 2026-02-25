# Slack Search

CLI tool that searches Slack messages for any topic using AI, gathers conversation context, and generates structured markdown reports.

Uses Playwright to scrape messages directly from the Slack web UI — no API tokens needed. Supports multiple AI providers: Claude CLI (free with Max subscription), Claude API, LM Studio, or Ollama (local).

## Quick Start

```bash
# Step 1: Log in to Slack (one time — saves session)
./slack-search --login --workspace https://app.slack.com/client/YOUR_WORKSPACE_ID

# Step 2: Search a channel
./slack-search \
  --urls https://app.slack.com/client/TGG6BJ82E/CGG6BJN5Q \
  --topic "injury" \
  --provider cli
```

Search multiple channels:

```bash
./slack-search \
  --urls "https://app.slack.com/client/TGG6BJ82E/CGG6BJN5Q,https://app.slack.com/client/TGG6BJ82E/CXXYYZZWW" \
  --topic "outage" \
  --provider lmstudio
```

## AI Providers

The tool supports four AI backends. If you don't pass `--provider`, you'll be prompted interactively.

### Claude CLI (recommended)

Uses your existing Claude Code installation and Max subscription. No API key needed.

```bash
./slack-search --provider cli --urls <URL> --topic "incident"
```

Requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code) to be installed and authenticated.

### Claude API

Pay-per-use via Anthropic API key.

```bash
./slack-search --provider api --claude-api-key sk-ant-... --urls <URL> --topic "incident"
```

Or set `ANTHROPIC_API_KEY` env var.

### LM Studio (local)

Free, runs locally via LM Studio's OpenAI-compatible API.

```bash
./slack-search --provider lmstudio --urls <URL> --topic "incident"
```

### Ollama (local)

Free, runs entirely on your machine. Requires [Ollama](https://ollama.com) running locally.

```bash
ollama pull llama3.1
./slack-search --provider ollama --urls <URL> --topic "incident"
```

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--login` | | Open browser to log in and save Slack session |
| `--workspace` | | Slack workspace URL (required with `--login`) |
| `--session-dir` | `.slack-session` | Directory for saved browser session |
| `--provider` | *(interactive)* | AI provider: `cli`, `api`, `lmstudio`, `ollama` |
| `--claude-api-key` | `$ANTHROPIC_API_KEY` | Anthropic API key (only for `--provider api`) |
| `--model` | `claude-sonnet-4-6` | Model for Claude API provider |
| `--lmstudio-model` | *(auto-detected)* | Model for LM Studio provider |
| `--lmstudio-url` | `http://localhost:1234` | LM Studio server URL |
| `--ollama-model` | `llama3.1` | Model for Ollama provider |
| `--ollama-url` | `http://localhost:11434` | Ollama server URL |
| `--urls` | *(required)* | Comma-separated Slack channel URLs |
| `--topic` | *(required)* | Topic to search for |
| `--output` | `report.md` | Output file path |
| `--scroll-delay` | `3.0` | Seconds between scroll steps when scraping |

## Standalone Scraper

The Playwright scraper can also be used standalone to export a channel to markdown:

```bash
# Save session (if not done via slack-search --login)
python scrape_slack.py --login --workspace https://app.slack.com/client/TGG6BJ82E

# Export a channel
python scrape_slack.py --url https://app.slack.com/client/TGG6BJ82E/CGG6BJN5Q --output general.md
```

## Building from Source

```bash
./build.sh
# Binary will be at dist/slack-search
```

Requires Python 3.10+. The build script creates a venv, installs dependencies, installs Chromium for Playwright, and builds the binary.

## How It Works

1. **Scrapes** messages from Slack channels via Playwright (scrolls to top, extracts all messages)
2. **Classifies** messages with AI to identify content related to your topic
3. **Gathers context** — surrounding messages from the full channel history for each match
4. **Summarizes** each incident, extracting title, summary, severity, and key quotes
5. **Generates** a structured markdown report
