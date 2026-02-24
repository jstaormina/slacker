"""Configuration, argument parsing, and interactive setup for Slack Topic Search."""

import argparse
import getpass
import os
import sys

from ai_analyzer import AIProvider, ClaudeAPIProvider, ClaudeCLIProvider, LMStudioProvider, OllamaProvider


def _prompt_choice(prompt: str, options: list[str], default: int = 1) -> int:
    """Prompt user to pick from numbered options. Returns 1-based index."""
    print(prompt)
    for i, opt in enumerate(options, 1):
        marker = " *" if i == default else ""
        print(f"  {i}) {opt}{marker}")
    while True:
        raw = input(f"Choice [{default}]: ").strip()
        if not raw:
            return default
        try:
            choice = int(raw)
            if 1 <= choice <= len(options):
                return choice
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(options)}")


def _setup_provider(args) -> AIProvider:
    """Interactively select and configure the AI provider."""

    # If provider was passed via CLI flags, skip the interactive prompt
    if args.provider:
        return _build_provider_from_args(args)

    print("\nSelect AI provider:")
    choice = _prompt_choice(
        "",
        [
            "Claude CLI  — uses your Claude Code / Max subscription (free, no API key)",
            "Claude API  — requires Anthropic API key (pay-per-use)",
            "LM Studio   — local model via LM Studio (OpenAI-compatible API)",
            "Ollama      — local model, free, requires Ollama running",
        ],
        default=1,
    )

    if choice == 1:
        return _setup_claude_cli(args)
    elif choice == 2:
        return _setup_claude_api(args)
    elif choice == 3:
        return _setup_lmstudio(args)
    else:
        return _setup_ollama(args)


def _setup_claude_cli(args) -> ClaudeCLIProvider:
    model = args.model if args.model != "claude-sonnet-4-6" else None
    print(f"  Using Claude CLI{f' with model {model}' if model else ''}")
    return ClaudeCLIProvider(model=model)


def _setup_claude_api(args) -> ClaudeAPIProvider:
    api_key = args.claude_api_key
    if not api_key:
        api_key = getpass.getpass("  Enter your Anthropic API key: ").strip()
    if not api_key:
        print("  Error: API key is required for Claude API provider.")
        sys.exit(1)
    print(f"  Using Claude API ({args.model})")
    return ClaudeAPIProvider(api_key=api_key, model=args.model)


def _setup_lmstudio(args) -> LMStudioProvider:
    model = args.lmstudio_model or None
    base_url = args.lmstudio_url or "http://localhost:1234"
    provider = LMStudioProvider(model=model, base_url=base_url)
    print(f"  Using LM Studio ({provider.model} at {base_url})")
    return provider


def _setup_ollama(args) -> OllamaProvider:
    model = args.ollama_model or "llama3.1"
    base_url = args.ollama_url or "http://localhost:11434"
    print(f"  Using Ollama ({model} at {base_url})")
    return OllamaProvider(model=model, base_url=base_url)


def _build_provider_from_args(args) -> AIProvider:
    """Build provider from explicit CLI flags (non-interactive)."""
    p = args.provider.lower()
    if p in ("cli", "claude-cli"):
        return _setup_claude_cli(args)
    elif p in ("api", "claude-api"):
        return _setup_claude_api(args)
    elif p in ("lmstudio", "lm-studio"):
        return _setup_lmstudio(args)
    elif p == "ollama":
        return _setup_ollama(args)
    else:
        print(f"  Unknown provider: {args.provider}")
        print(f"  Valid options: cli, api, lmstudio, ollama")
        sys.exit(1)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="slack-search",
        description="Search Slack messages for topic-related content and generate markdown reports.",
    )

    # Slack auth
    parser.add_argument(
        "--slack-token",
        default=os.environ.get("SLACK_TOKEN"),
        help="Slack User OAuth Token (xoxp-...). Also reads SLACK_TOKEN env var.",
    )

    # AI provider selection
    parser.add_argument(
        "--provider",
        default=None,
        help="AI provider: cli (Claude CLI), api (Claude API), lmstudio, ollama. "
             "If omitted, you'll be prompted interactively.",
    )
    parser.add_argument(
        "--claude-api-key",
        default=os.environ.get("ANTHROPIC_API_KEY"),
        help="Anthropic API key (only for --provider api). Also reads ANTHROPIC_API_KEY env var.",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Model name for Claude API provider (default: claude-sonnet-4-6).",
    )
    parser.add_argument(
        "--lmstudio-model",
        default=None,
        help="LM Studio model name (auto-detected if not specified).",
    )
    parser.add_argument(
        "--lmstudio-url",
        default=None,
        help="LM Studio server URL (default: http://localhost:1234).",
    )
    parser.add_argument(
        "--ollama-model",
        default=None,
        help="Ollama model name (default: llama3.1).",
    )
    parser.add_argument(
        "--ollama-url",
        default=None,
        help="Ollama base URL (default: http://localhost:11434).",
    )

    # Search parameters
    parser.add_argument(
        "--channels",
        required=True,
        help="Comma-separated list of channel names to search (e.g. general,safety,operations).",
    )
    parser.add_argument(
        "--topic",
        required=True,
        help='Topic to search for (e.g. "injury", "outage", "hiring").',
    )
    parser.add_argument(
        "--output",
        default="report.md",
        help='Output markdown file path (default: "report.md").',
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of days back to search (default: 90).",
    )

    args = parser.parse_args(argv)

    # Slack token: prompt if missing
    if not args.slack_token:
        args.slack_token = getpass.getpass("Enter your Slack User OAuth Token (xoxp-...): ").strip()
    if not args.slack_token:
        parser.error("Slack token is required (--slack-token or SLACK_TOKEN env var)")

    args.channel_list = [c.strip().lstrip("#") for c in args.channels.split(",") if c.strip()]
    if not args.channel_list:
        parser.error("At least one channel name is required")

    return args


def setup(argv=None):
    """Parse args and set up the AI provider. Returns (args, provider)."""
    args = parse_args(argv)
    provider = _setup_provider(args)
    return args, provider
