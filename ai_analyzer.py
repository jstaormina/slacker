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


KNOWLEDGE_CATEGORIES = [
    "Troubleshooting",
    "How-To",
    "FAQ",
    "Feature Explanation",
    "Configuration",
    "Best Practice",
]


def _build_classification_prompt(messages: list[dict], topic: str) -> str:
    formatted = []
    for idx, msg in enumerate(messages):
        ts = float(msg.get("ts", 0))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = msg.get("text", "")
        formatted.append(f"[{idx}] ({dt}) {text}")

    messages_text = "\n".join(formatted)

    return f"""You are a JSON-only classifier. No explanation. No thinking. Output ONLY valid JSON.

Task: Which of these Slack messages contain knowledge worth capturing about "{topic}"?

Look for messages where someone is sharing expertise, not just chatting:
- Troubleshooting exchanges (problem described, solution found)
- How-to explanations or step-by-step guidance
- Feature behavior descriptions or clarifications
- Workarounds, tips, or best practices
- Configuration guidance or specific settings
- Q&A exchanges where questions get substantive answers
- Edge cases, gotchas, or warnings
- Process explanations or policy clarifications

Messages:
{messages_text}

Output format — a JSON array, nothing else:
[{{"index": 0, "reason": "brief reason"}}, ...]

If none contain extractable knowledge, output: []

Rules:
- Include messages that share actionable knowledge, procedures, or expertise about "{topic}"
- Include follow-up messages that add detail, corrections, or confirmation to knowledge being shared
- Exclude messages that merely mention "{topic}" casually without sharing knowledge
- Exclude purely social or scheduling messages even if they mention the topic
- Output ONLY the JSON array. No other text before or after."""


def _build_extraction_prompt(
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
    categories_str = ", ".join(KNOWLEDGE_CATEGORIES)

    return f"""You are a JSON-only knowledge extractor. No explanation. No thinking. Output ONLY valid JSON.

Task: Extract the knowledge being shared in this Slack conversation from #{channel_name} about "{topic}".

Conversation:
{conversation}

Output format — a single JSON object, nothing else:
{{
  "title": "Descriptive article title (like a KB article heading)",
  "category": "one of: {categories_str}",
  "content": "Detailed article content as prose. Preserve specific procedures, steps, values, settings, and technical details. Write as if explaining to a colleague — not a summary, but the actual knowledge. Use paragraphs, not bullet points.",
  "tags": ["keyword1", "keyword2", "keyword3"],
  "source_summary": "One sentence describing what this conversation covered, for use when grouping related topics."
}}

Rules:
- The content field should be DETAILED — capture the actual knowledge, not just a summary
- Preserve specific values, settings, steps, error messages, and technical details mentioned
- Write content as clear prose that could stand alone as a KB article section
- If the conversation contains a problem and solution, structure the content to explain both
- category must be exactly one of: {categories_str}
- tags should be lowercase keywords useful for grouping related content
- Output ONLY the JSON object. No other text before or after."""


def _build_grouping_prompt(extractions: list[dict]) -> str:
    items = []
    for idx, ext in enumerate(extractions):
        title = ext.get("title", "Untitled")
        tags = ", ".join(ext.get("tags", []))
        source = ext.get("source_summary", "")
        items.append(f"[{idx}] Title: {title} | Tags: {tags} | Summary: {source}")

    items_text = "\n".join(items)

    return f"""You are a JSON-only topic grouper. No explanation. No thinking. Output ONLY valid JSON.

Task: Group these knowledge extractions by topic. Items that cover the same subject, procedure, or feature should be in the same group — even if they approach it from different angles.

Items:
{items_text}

Output format — a JSON array of groups, nothing else:
[
  {{"group_title": "Descriptive title for this topic", "indices": [0, 3, 5]}},
  {{"group_title": "Another topic", "indices": [1, 2]}}
]

Rules:
- Every index must appear in exactly one group
- Items that are clearly about the same feature, process, or concept should be grouped together
- Singletons are fine — not everything needs to be grouped
- group_title should be a clear, descriptive title suitable for a KB article
- Output ONLY the JSON array. No other text before or after."""


def _build_synthesis_prompt(
    group_title: str,
    extractions: list[dict],
    topic: str,
) -> str:
    sections = []
    for idx, ext in enumerate(extractions):
        title = ext.get("title", "Untitled")
        content = ext.get("content", "")
        category = ext.get("category", "FAQ")
        sections.append(f"--- Source {idx + 1}: {title} (Category: {category}) ---\n{content}")

    sources_text = "\n\n".join(sections)
    categories_str = ", ".join(KNOWLEDGE_CATEGORIES)

    return f"""You are a JSON-only article writer. No explanation. No thinking. Output ONLY valid JSON.

Task: Synthesize the following knowledge sources into one cohesive, well-structured KB article about "{group_title}" (related to {topic}).

Sources:
{sources_text}

Output format — a single JSON object, nothing else:
{{
  "title": "Final article title",
  "category": "most appropriate category from: {categories_str}",
  "content": "The full article as well-structured markdown prose. Use ## subheadings to organize sections. Combine and deduplicate information from all sources into a single coherent article. Preserve all specific technical details, steps, values, and procedures. Write in a clear, professional tone suitable for a knowledge base."
}}

Rules:
- Merge overlapping information — do not repeat the same point from different sources
- Preserve ALL specific technical details, settings, values, and procedures
- Use ## subheadings to organize the content logically
- Write as detailed prose, not bullet-point summaries
- The result should read as a single, polished KB article
- Output ONLY the JSON object. No other text before or after."""


FALLBACK_EXTRACTION = {
    "title": "Untitled Article",
    "content": "Could not extract knowledge from this conversation.",
    "category": "FAQ",
    "tags": [],
    "source_summary": "Unable to extract.",
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

    def extract_knowledge(
        self,
        thread_messages: list[dict],
        channel_name: str,
        user_names: dict[str, str],
        topic: str,
    ) -> dict:
        """Extract knowledge from a conversation cluster."""
        prompt = _build_extraction_prompt(thread_messages, channel_name, user_names, topic)
        try:
            content = self.provider.complete(prompt, max_tokens=4096)
            return _extract_json(content)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  Warning: Could not parse extraction response: {e}")
            return dict(FALLBACK_EXTRACTION)
        except RuntimeError as e:
            print(f"  Error: {e}")
            return dict(FALLBACK_EXTRACTION)

    def group_topics(self, extractions: list[dict]) -> list[dict]:
        """Group related extractions by topic. Returns list of {group_title, indices}."""
        if len(extractions) <= 1:
            return [{"group_title": extractions[0].get("title", "Untitled"), "indices": [0]}]

        prompt = _build_grouping_prompt(extractions)
        try:
            content = self.provider.complete(prompt, max_tokens=2048)
            groups = _extract_json(content)
            if not isinstance(groups, list):
                groups = [groups]
            # Ensure every index appears
            all_indices = set()
            for g in groups:
                for idx in g.get("indices", []):
                    all_indices.add(idx)
            for i in range(len(extractions)):
                if i not in all_indices:
                    groups.append({
                        "group_title": extractions[i].get("title", "Untitled"),
                        "indices": [i],
                    })
            return groups
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"  Warning: Could not parse grouping response: {e}")
            return [
                {"group_title": ext.get("title", "Untitled"), "indices": [i]}
                for i, ext in enumerate(extractions)
            ]
        except RuntimeError as e:
            print(f"  Error: {e}")
            return [
                {"group_title": ext.get("title", "Untitled"), "indices": [i]}
                for i, ext in enumerate(extractions)
            ]

    def synthesize_article(
        self,
        group_title: str,
        extractions: list[dict],
        topic: str,
    ) -> dict:
        """Synthesize multiple extractions into one KB article."""
        if len(extractions) == 1:
            ext = extractions[0]
            return {
                "title": ext.get("title", "Untitled"),
                "category": ext.get("category", "FAQ"),
                "content": ext.get("content", ""),
            }

        prompt = _build_synthesis_prompt(group_title, extractions, topic)
        try:
            content = self.provider.complete(prompt, max_tokens=8192)
            return _extract_json(content)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  Warning: Could not parse synthesis response: {e}")
            combined = "\n\n".join(ext.get("content", "") for ext in extractions)
            return {
                "title": group_title,
                "category": extractions[0].get("category", "FAQ"),
                "content": combined,
            }
        except RuntimeError as e:
            print(f"  Error: {e}")
            combined = "\n\n".join(ext.get("content", "") for ext in extractions)
            return {
                "title": group_title,
                "category": extractions[0].get("category", "FAQ"),
                "content": combined,
            }
