#!/usr/bin/env bash
set -euo pipefail

VENV_DIR=".venv"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Activate venv
source "$VENV_DIR/bin/activate"

echo "Installing dependencies..."
pip install -r requirements.txt

echo "Building standalone binary..."
pyinstaller --onefile slack_injury_search.py \
  --name slack-search \
  --hidden-import slack_sdk \
  --hidden-import slack_sdk.web \
  --hidden-import slack_sdk.web.client \
  --hidden-import anthropic \
  --hidden-import config \
  --hidden-import slack_client \
  --hidden-import ai_analyzer \
  --hidden-import report_generator

echo ""
echo "Build complete! Binary is at: dist/slack-search"
echo "Run it with: ./dist/slack-search --help"
