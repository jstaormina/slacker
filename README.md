# Slack Search

CLI tool that searches Slack messages for any topic using AI, gathers conversation context, and generates structured markdown reports.

Supports multiple AI providers: Claude CLI (free with Max subscription), Claude API, or Ollama (local).

## Quick Start

```bash
# Interactive mode — prompts you to pick a provider
./slack-search --channels general,safety --topic "injury"

# Or specify everything up front
./slack-search \
  --slack-token xoxp-YOUR-TOKEN \
  --provider cli \
  --channels safety,operations \
  --topic "outage" \
  --days 90
```

Set environment variables to skip token prompts:

```bash
export SLACK_TOKEN=xoxp-YOUR-TOKEN
./slack-search --channels safety,general --topic "hiring" --days 30
```

## AI Providers

The tool supports three AI backends. If you don't pass `--provider`, you'll be prompted interactively.

### Claude CLI (recommended)

Uses your existing Claude Code installation and Max subscription. No API key needed.

```bash
./slack-search --provider cli --channels safety --topic "incident"
```

Requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code) to be installed and authenticated.

### Claude API

Pay-per-use via Anthropic API key.

```bash
./slack-search --provider api --claude-api-key sk-ant-... --channels safety --topic "incident"
```

Or set `ANTHROPIC_API_KEY` env var and you'll be prompted if needed.

### Ollama (local)

Free, runs entirely on your machine. Requires [Ollama](https://ollama.com) running locally.

```bash
ollama pull llama3.1
./slack-search --provider ollama --channels safety --topic "incident"
```

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--slack-token` | `$SLACK_TOKEN` | Slack User OAuth Token (prompted if missing) |
| `--provider` | *(interactive)* | AI provider: `cli`, `api`, or `ollama` |
| `--claude-api-key` | `$ANTHROPIC_API_KEY` | Anthropic API key (only for `--provider api`) |
| `--model` | `claude-sonnet-4-6` | Model for Claude API provider |
| `--ollama-model` | `llama3.1` | Model for Ollama provider |
| `--ollama-url` | `http://localhost:11434` | Ollama server URL |
| `--channels` | *(required)* | Comma-separated channel names |
| `--topic` | *(required)* | Topic to search for |
| `--output` | `report.md` | Output file path |
| `--days` | `90` | Number of days to search back |

## Slack App Setup

You need a Slack App with a **User OAuth Token** to access message history. This is free on all Slack plans.

### Step 1: Create the App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **Create New App** → **From Scratch**
3. Name it (e.g. "Slack Search Tool") and select your workspace
4. Click **Create App**

### Step 2: Add Permissions

1. In the left sidebar, click **OAuth & Permissions**
2. Scroll to **User Token Scopes** and add these scopes:

| Scope | Purpose |
|-------|---------|
| `channels:history` | Read messages from public channels |
| `channels:read` | List public channels |
| `groups:history` | Read messages from private channels |
| `groups:read` | List private channels |
| `users:read` | Resolve user IDs to display names |

### Step 3: Install and Get Token

1. Scroll up to **OAuth Tokens** and click **Install to Workspace**
2. Review and **Allow** the permissions
3. Copy the **User OAuth Token** (starts with `xoxp-`)

### Important Notes

- **Free Slack plans** only retain the last 90 days of message history
- The tool uses a **User Token** (not Bot Token) because bots can't access `conversations.history` for all channels without being invited
- You must be a member of the channels you want to search

## Building from Source

```bash
./build.sh
# Binary will be at dist/slack-search
```

Requires Python 3.10+.

## How It Works

1. **Fetches** messages from specified Slack channels within the date range
2. **Classifies** messages with AI to identify content related to your topic
3. **Gathers context** — surrounding channel messages + thread replies for each match
4. **Summarizes** each incident, extracting title, summary, severity, and key quotes
5. **Generates** a structured markdown report
