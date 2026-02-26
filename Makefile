VENV := .venv
BIN := dist/slack-search

.PHONY: build install clean

build: install
	$(VENV)/bin/pyinstaller --onefile slack_search.py \
		--name slack-search \
		--hidden-import anthropic \
		--hidden-import config \
		--hidden-import scrape_slack \
		--hidden-import ai_analyzer \
		--hidden-import report_generator \
		--hidden-import markdown \
		--hidden-import fpdf
	@echo "\nBuild complete: $(BIN)"

install: $(VENV)/bin/activate
	$(VENV)/bin/pip install -q -r requirements.txt
	$(VENV)/bin/playwright install chromium

$(VENV)/bin/activate:
	python3 -m venv $(VENV)

clean:
	rm -rf build dist *.spec
