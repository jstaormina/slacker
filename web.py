"""Web UI for Slacker."""

import io
import json
import os
import queue
import subprocess
import threading
import time
import uuid
import zipfile

from flask import Flask, render_template, request, jsonify, Response

from ai_analyzer import (
    AIProvider, ClaudeAPIProvider, ClaudeCLIProvider,
    LMStudioProvider, OllamaProvider, OpenAICompatibleProvider,
)
from slack_search import run_extraction

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Container-friendly defaults via env vars
# ---------------------------------------------------------------------------

DEFAULT_SESSION_DIR = os.environ.get("SLACK_SESSION_DIR", ".slack-session")
DEFAULT_CACHE_DIR = os.environ.get("SLACK_CACHE_DIR", ".slack-cache")
DEFAULT_OUTPUT_DIR = os.environ.get("KB_OUTPUT_DIR", "kb")

# AI provider configured via env vars
AI_PROVIDER = os.environ.get("AI_PROVIDER", "cli")  # cli, api, openai, lmstudio, ollama

# Slack API token (if set, uses API instead of Playwright browser session)
SLACK_API_TOKEN = os.environ.get("SLACK_API_TOKEN", "")

# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------

jobs: dict[str, "Job"] = {}


class Job:
    def __init__(self, job_id, config):
        self.id = job_id
        self.config = config
        self.queue = queue.Queue()
        self.status = "running"
        self.result = None
        self.error = None

    def log(self, message):
        self.queue.put({"type": "log", "message": message})

    def complete(self, result):
        self.status = "complete"
        self.result = result
        self.queue.put({"type": "complete", "result": result})

    def fail(self, error):
        self.status = "error"
        self.error = str(error)
        self.queue.put({"type": "error", "message": str(error)})


# ---------------------------------------------------------------------------
# Auth checks
# ---------------------------------------------------------------------------

def _claude_credentials_path() -> str:
    return os.path.join(os.path.expanduser("~/.claude"), ".credentials.json")


def _check_claude_auth() -> tuple[bool, str]:
    """Check if Claude CLI is authenticated. Returns (ok, message)."""
    creds = _claude_credentials_path()
    if not os.path.exists(creds):
        return False, (
            "Claude CLI not authenticated. "
            "Mount .credentials.json from ~/.claude/ into the container. "
            "See k8s/secret.yaml for setup instructions."
        )
    try:
        with open(creds) as f:
            data = json.load(f)
        oauth = data.get("claudeAiOauth", {})
        if not oauth.get("accessToken"):
            return False, "Claude credentials file exists but contains no access token."
        expires = oauth.get("expiresAt", 0)
        if expires and expires < time.time() * 1000:
            return False, (
                "Claude access token has expired. "
                "Re-run 'claude login' locally and update the mounted credentials."
            )
        return True, f"Authenticated (subscription: {oauth.get('subscriptionType', 'unknown')})"
    except (json.JSONDecodeError, OSError) as e:
        return False, f"Could not read Claude credentials: {e}"


def _check_slack_session() -> tuple[bool, str]:
    """Check if a Slack browser session exists."""
    session_dir = DEFAULT_SESSION_DIR
    if not os.path.isdir(session_dir):
        return False, f"Slack session directory not found at {session_dir}."
    # Check for Chromium profile marker
    if not os.path.exists(os.path.join(session_dir, "Default")):
        return False, f"Slack session at {session_dir} appears empty. Run login first."
    return True, "Slack session found."


def _check_slack_auth() -> tuple[bool, str, str]:
    """Check Slack connectivity. Returns (ok, message, mode).

    mode is 'api' when SLACK_API_TOKEN is set, 'session' otherwise.
    """
    if SLACK_API_TOKEN:
        try:
            from slack_api import create_client, test_auth
            client = create_client(SLACK_API_TOKEN)
            info = test_auth(client)
            team = info.get("team", "unknown workspace")
            return True, f"API connected ({team})", "api"
        except Exception as e:
            return False, f"Slack API error: {e}", "api"
    else:
        ok, msg = _check_slack_session()
        return ok, msg, "session"


def _check_provider() -> tuple[bool, str]:
    """Check if the configured AI provider is ready. Returns (ok, message)."""
    p = AI_PROVIDER.lower()

    if p == "cli":
        ok, msg = _check_claude_auth()
        model = os.environ.get("CLAUDE_MODEL", "default")
        if ok:
            return True, f"Claude CLI ({model})"
        return False, msg

    elif p == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY env var not set."
        model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
        return True, f"Claude API ({model})"

    elif p == "openai":
        url = os.environ.get("OPENAI_BASE_URL", "")
        if not url:
            return False, "OPENAI_BASE_URL env var not set."
        model = os.environ.get("OPENAI_MODEL", "auto-detect")
        return True, f"OpenAI Compatible ({model} at {url})"

    elif p == "lmstudio":
        url = os.environ.get("LMSTUDIO_URL", "http://localhost:1234")
        model = os.environ.get("LMSTUDIO_MODEL", "auto-detect")
        return True, f"LM Studio ({model} at {url})"

    elif p == "ollama":
        url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
        model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        return True, f"Ollama ({model} at {url})"

    else:
        return False, f"Unknown AI_PROVIDER: {AI_PROVIDER}"


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

def build_provider() -> AIProvider:
    """Build AI provider from environment variables."""
    p = AI_PROVIDER.lower()

    if p == "cli":
        ok, msg = _check_claude_auth()
        if not ok:
            raise ValueError(msg)
        model = os.environ.get("CLAUDE_MODEL") or None
        return ClaudeCLIProvider(model=model)

    elif p == "api":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY env var is required for Claude API provider")
        model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
        return ClaudeAPIProvider(api_key=api_key, model=model)

    elif p == "openai":
        url = os.environ.get("OPENAI_BASE_URL", "")
        if not url:
            raise ValueError("OPENAI_BASE_URL env var is required for OpenAI-compatible provider")
        api_key = os.environ.get("OPENAI_API_KEY", "")
        model = os.environ.get("OPENAI_MODEL", "")
        return OpenAICompatibleProvider(base_url=url, api_key=api_key, model=model)

    elif p == "lmstudio":
        model = os.environ.get("LMSTUDIO_MODEL") or None
        url = os.environ.get("LMSTUDIO_URL", "http://localhost:1234")
        return LMStudioProvider(model=model, base_url=url)

    elif p == "ollama":
        model = os.environ.get("OLLAMA_MODEL", "llama3.1")
        url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
        return OllamaProvider(model=model, base_url=url)

    else:
        raise ValueError(
            f"Unknown AI_PROVIDER: {AI_PROVIDER}. "
            "Valid options: cli, api, openai, lmstudio, ollama"
        )


# ---------------------------------------------------------------------------
# Pipeline runner (background thread)
# ---------------------------------------------------------------------------

def run_pipeline(job: Job):
    """Run the extraction pipeline, posting progress to the job queue."""
    try:
        config = job.config
        provider = build_provider()
        job.log(f"Using AI provider: {provider.name}")

        use_api = bool(SLACK_API_TOKEN)
        raw_input = config.get("urls", "").strip()

        if use_api:
            # In API mode, input field contains channel names (optional)
            channel_names = [n.strip().lstrip("#") for n in raw_input.split(",") if n.strip()] if raw_input else None
            urls = None
            if channel_names:
                job.log(f"Searching channels: {', '.join(channel_names)}")
            else:
                job.log("Searching all accessible channels")
        else:
            urls = [u.strip() for u in raw_input.split(",") if u.strip()]
            channel_names = None
            if not urls:
                job.fail("No channel URLs provided")
                return

            ok, msg = _check_slack_session()
            if not ok:
                job.log(f"Warning: {msg}")

        result = run_extraction(
            topic=config["topic"],
            urls=urls,
            provider=provider,
            output_dir=config.get("output") or DEFAULT_OUTPUT_DIR,
            fmt=config.get("format", "md"),
            cache_dir=config.get("cache_dir") or DEFAULT_CACHE_DIR,
            session_dir=config.get("session_dir") or DEFAULT_SESSION_DIR,
            scroll_delay=float(config.get("scroll_delay", 3.0)),
            no_cache=config.get("no_cache", False),
            slack_api_token=SLACK_API_TOKEN or None,
            channel_names=channel_names,
            log=job.log,
        )

        job.complete(result)
    except Exception as e:
        job.fail(e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    provider_ok, provider_msg = _check_provider()
    slack_ok, slack_msg, slack_mode = _check_slack_auth()
    return render_template(
        "index.html",
        provider_ok=provider_ok,
        provider_msg=provider_msg,
        slack_ok=slack_ok,
        slack_msg=slack_msg,
        slack_mode=slack_mode,
    )


@app.route("/healthz")
def healthz():
    """Health check endpoint for K8s probes."""
    return jsonify({"status": "ok"})


@app.route("/api/run", methods=["POST"])
def start_run():
    """Start a new extraction pipeline job."""
    config = request.json
    if not config.get("topic"):
        return jsonify({"error": "Topic is required"}), 400
    if not SLACK_API_TOKEN and not config.get("urls"):
        return jsonify({"error": "Channel URLs are required"}), 400

    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id, config)
    jobs[job_id] = job

    thread = threading.Thread(target=run_pipeline, args=(job,), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def job_progress(job_id):
    """SSE stream of job progress."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        while True:
            try:
                msg = job.queue.get(timeout=60)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] in ("complete", "error"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/download/<job_id>")
def download_kb(job_id):
    """Zip the output directory and return it as a download."""
    job = jobs.get(job_id)
    if not job or not job.result:
        return jsonify({"error": "Job not found or not complete"}), 404

    output_path = job.result.get("output_path", "")
    if not output_path or not os.path.exists(output_path):
        return jsonify({"error": "Output not found"}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if os.path.isdir(output_path):
            for root, _dirs, files in os.walk(output_path):
                for f in files:
                    full = os.path.join(root, f)
                    arcname = os.path.relpath(full, os.path.dirname(output_path))
                    zf.write(full, arcname)
        else:
            zf.write(output_path, os.path.basename(output_path))

    buf.seek(0)
    topic = job.config.get("topic", "kb").replace(" ", "-").lower()
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f"attachment; filename={topic}-knowledge-base.zip"},
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
