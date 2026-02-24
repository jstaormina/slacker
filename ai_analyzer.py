"""AI provider abstraction for message classification and summarization."""

import json
import subprocess
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from datetime import datetime, timezone


import re

CLASSIFICATION_BATCH_SIZE = 15


def _extract_json(text: str):
    """Extract JSON from a response, handling code blocks, preamble, and rambling.

    Tries multiple strategies:
    1. Direct parse
    2. Strip markdown code blocks
    3. Find the first [...] or {...} in the text via bracket matching
    """
    text = text.strip()

    # Strategy 1: direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown code blocks
    if "```" in text:
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

    # Strategy 3: find first balanced [...] or {...}
    for open_ch, close_ch in [("[", "]"), ("{", "}")]:
        start = text.find(open_ch)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise json.JSONDecodeError("No valid JSON found in response", text, 0)


def _build_classification_prompt(messages: list[dict], topic: str) -> str:
    formatted = []
    for idx, msg in enumerate(messages):
        ts = float(msg.get("ts", 0))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = msg.get("text", "")
        formatted.append(f"[{idx}] ({dt}) {text}")

    messages_text = "\n".join(formatted)

    return f"""You are a JSON-only classifier. No explanation. No thinking. Output ONLY valid JSON.

Task: Which of these Slack messages are related to "{topic}"?

Messages:
{messages_text}

Output format — a JSON array, nothing else:
[{{"index": 0, "reason": "brief reason"}}, ...]

If none are relevant, output: []

Rules:
- Include direct mentions, indirect references, incident discussions, follow-ups related to "{topic}"
- Include messages discussing consequences, prevention, or responses related to "{topic}"
- Exclude messages that use the word casually or in an unrelated context
- Output ONLY the JSON array. No other text before or after."""


def _build_summary_prompt(
    thread_messages: list[dict],
    channel_name: str,
    user_names: dict[str, str],
    topic: str,
) -> str:
    formatted = []
    for msg in thread_messages:
        ts = float(msg.get("ts", 0))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        user = user_names.get(msg.get("user", ""), msg.get("user", "unknown"))
        text = msg.get("text", "")
        formatted.append(f"[{dt}] @{user}: {text}")

    conversation = "\n".join(formatted)

    return f"""You are a JSON-only summarizer. No explanation. No thinking. Output ONLY valid JSON.

Task: Summarize this Slack conversation from #{channel_name} related to "{topic}".

Conversation:
{conversation}

Output format — a single JSON object, nothing else:
{{"title": "Brief title (max 10 words)", "summary": "2-4 sentence factual summary", "key_quotes": ["quote1", "quote2"], "severity": "informational|minor|moderate|serious"}}

Rules:
- Keep the summary factual, based only on the messages above
- severity must be one of: informational, minor, moderate, serious
- Output ONLY the JSON object. No other text before or after."""


FALLBACK_SUMMARY = {
    "title": "Untitled Incident",
    "summary": "Could not generate summary.",
    "key_quotes": [],
    "severity": "informational",
}


# ---------------------------------------------------------------------------
# Provider base class
# ---------------------------------------------------------------------------

class AIProvider(ABC):
    @abstractmethod
    def complete(self, prompt: str, max_tokens: int = 2048) -> str:
        """Send a prompt and return the text response."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""


# ---------------------------------------------------------------------------
# Claude API provider (requires ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------

class ClaudeAPIProvider(AIProvider):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    @property
    def name(self) -> str:
        return f"Claude API ({self.model})"

    def complete(self, prompt: str, max_tokens: int = 2048) -> str:
        import anthropic
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except anthropic.APIError as e:
            raise RuntimeError(f"Claude API error: {e}") from e


# ---------------------------------------------------------------------------
# Claude CLI provider (uses `claude -p`, covered by Max subscription)
# ---------------------------------------------------------------------------

class ClaudeCLIProvider(AIProvider):
    def __init__(self, model: str | None = None):
        self.model = model
        # Verify claude CLI is available
        try:
            subprocess.run(
                ["claude", "--version"],
                capture_output=True, timeout=10, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            raise RuntimeError(
                "Claude Code CLI not found. Install it or use a different provider.\n"
                "See: https://docs.anthropic.com/en/docs/claude-code"
            ) from e

    @property
    def name(self) -> str:
        return f"Claude CLI{f' ({self.model})' if self.model else ''}"

    def complete(self, prompt: str, max_tokens: int = 2048) -> str:
        cmd = ["claude", "-p", "--output-format", "text"]
        if self.model:
            cmd.extend(["--model", self.model])

        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI error: {result.stderr.strip()}")
        return result.stdout.strip()


# ---------------------------------------------------------------------------
# Ollama provider (local, free)
# ---------------------------------------------------------------------------

class OllamaProvider(AIProvider):
    def __init__(self, model: str = "llama3.1", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        # Verify Ollama is running
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            urllib.request.urlopen(req, timeout=5)
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(
                f"Cannot connect to Ollama at {self.base_url}. "
                "Make sure Ollama is running: https://ollama.com"
            ) from e

    @property
    def name(self) -> str:
        return f"Ollama ({self.model})"

    def complete(self, prompt: str, max_tokens: int = 2048) -> str:
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read())
                return data.get("response", "").strip()
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"Ollama error: {e}") from e


# ---------------------------------------------------------------------------
# LM Studio provider (local, OpenAI-compatible API)
# ---------------------------------------------------------------------------

class LMStudioProvider(AIProvider):
    def __init__(self, model: str | None = None, base_url: str = "http://localhost:1234"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        # Verify LM Studio is running
        try:
            req = urllib.request.Request(f"{self.base_url}/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                models = data.get("data", [])
                if not self.model and models:
                    self.model = models[0].get("id")
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(
                f"Cannot connect to LM Studio at {self.base_url}. "
                "Make sure LM Studio's local server is running."
            ) from e

    @property
    def name(self) -> str:
        return f"LM Studio ({self.model or 'auto'})"

    def complete(self, prompt: str, max_tokens: int = 2048) -> str:
        body: dict = {
            "messages": [
                {"role": "system", "content": "You are a JSON-only assistant. You output ONLY valid JSON with no explanation, commentary, or thinking. Never include text outside the JSON."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "stream": False,
            "temperature": 0.1,
        }
        if self.model:
            body["model"] = self.model

        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"LM Studio error: {e}") from e


# ---------------------------------------------------------------------------
# Analyzer (uses any provider)
# ---------------------------------------------------------------------------

class AIAnalyzer:
    def __init__(self, provider: AIProvider):
        self.provider = provider

    def classify_messages(self, messages: list[dict], topic: str) -> list[dict]:
        """Classify messages as relevant or not. Returns relevant messages with 'relevance_reason'."""
        relevant = []
        for i in range(0, len(messages), CLASSIFICATION_BATCH_SIZE):
            batch = messages[i : i + CLASSIFICATION_BATCH_SIZE]
            batch_relevant = self._classify_batch(batch, topic)
            relevant.extend(batch_relevant)
        return relevant

    def _classify_batch(self, messages: list[dict], topic: str) -> list[dict]:
        prompt = _build_classification_prompt(messages, topic)
        try:
            content = self.provider.complete(prompt, max_tokens=4096)
            results = _extract_json(content)
            if not isinstance(results, list):
                results = [results]
            relevant = []
            for item in results:
                # Handle both {"index": 0, "reason": "..."} and bare int formats
                if isinstance(item, int):
                    idx = item
                    reason = ""
                elif isinstance(item, dict):
                    idx = item.get("index", -1)
                    reason = item.get("reason", "")
                else:
                    continue
                if 0 <= idx < len(messages):
                    msg = messages[idx].copy()
                    msg["relevance_reason"] = reason
                    relevant.append(msg)
            return relevant
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            print(f"  Warning: Could not parse classification response: {e}")
            return []
        except RuntimeError as e:
            print(f"  Error: {e}")
            return []

    def summarize_incident(
        self,
        thread_messages: list[dict],
        channel_name: str,
        user_names: dict[str, str],
        topic: str,
    ) -> dict:
        prompt = _build_summary_prompt(thread_messages, channel_name, user_names, topic)
        try:
            content = self.provider.complete(prompt, max_tokens=1024)
            return _extract_json(content)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  Warning: Could not parse summary response: {e}")
            return dict(FALLBACK_SUMMARY)
        except RuntimeError as e:
            print(f"  Error: {e}")
            return dict(FALLBACK_SUMMARY)
