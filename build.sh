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

echo "Installing Playwright browsers..."
playwright install chromium

echo "Building standalone binary..."
pyinstaller --onefile slack_injury_search.py \
  --name slack-search \
  --hidden-import anthropic \
  --hidden-import config \
  --hidden-import scrape_slack \
  --hidden-import ai_analyzer \
  --hidden-import report_generator

echo ""
echo "Build complete! Binary is at: dist/slack-search"
echo "Run it with: ./dist/slack-search --help"
