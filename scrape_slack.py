"""Playwright-based Slack channel scraper.

Scrolls to the top of a Slack channel in the web UI, extracts all messages,
and saves them as a structured markdown file.

Usage:
    # First time: log in and save session
    python scrape_slack.py --login --workspace https://app.slack.com/client/TGG6BJ82E

    # Scrape a channel (headless by default)
    python scrape_slack.py --url https://app.slack.com/client/TGG6BJ82E/CGG6BJN5Q --output general.md
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

# When running from a PyInstaller bundle, Playwright can't find its Chromium
# browser in the temp extraction directory. Point it to the system cache.
if getattr(sys, '_MEIPASS', None):
    if 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ:
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = os.path.expanduser(
            '~/.cache/ms-playwright'
        )

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


DEFAULT_SESSION_DIR = ".slack-session"

# JavaScript to extract all currently-visible messages from the Slack DOM.
EXTRACT_MESSAGES_JS = """() => {
    const messages = [];
    const msgElements = document.querySelectorAll(
        '[data-qa="virtual-list-item"], .c-virtual_list__item, .c-message_kit__message'
    );
    for (const el of msgElements) {
        const senderEl = el.querySelector(
            '[data-qa="message_sender_name"], .c-message__sender_button, '
            + '.c-message_kit__sender [data-stringify-text]'
        );
        const sender = senderEl ? senderEl.textContent.trim() : '';

        const timeEl = el.querySelector(
            '[data-qa="message_time"], time, .c-timestamp__label, '
            + '[data-ts], .c-message__time'
        );
        let timestamp = '';
        let ts_value = '';
        if (timeEl) {
            const dt = timeEl.getAttribute('datetime');
            timestamp = dt || timeEl.textContent.trim();
            ts_value = timeEl.getAttribute('data-ts') || '';
        }
        if (!ts_value) {
            const tsEl = el.closest('[data-ts]') || el.querySelector('[data-ts]');
            if (tsEl) ts_value = tsEl.getAttribute('data-ts') || '';
        }

        const dayDivider = el.querySelector(
            '[data-qa="day_divider__label"], .c-message_list__day_divider__label'
        );

        const textEl = el.querySelector(
            '[data-qa="message-text"], .p-rich_text_section, '
            + '.c-message__body, .p-block_kit_renderer'
        );
        const text = textEl ? textEl.innerText.trim() : '';

        if (!text && !sender && !dayDivider) continue;

        const key = ts_value || (sender + '|' + timestamp + '|' + text.substring(0, 80));

        messages.push({
            sender: sender,
            timestamp: timestamp,
            ts_value: ts_value,
            text: text,
            key: key,
            day_divider: dayDivider ? dayDivider.textContent.trim() : ''
        });
    }
    return messages;
}"""

# Smooth-scroll the message pane to the top. Returns current scroll state.
SMOOTH_SCROLL_TOP_JS = """() => {
    const scrollers = document.querySelectorAll('[data-qa="slack_kit_scrollbar"]');
    let best = null, bestH = 0;
    for (const s of scrollers) {
        if (s.scrollHeight > bestH) { bestH = s.scrollHeight; best = s; }
    }
    if (!best) return null;
    const before = {scrollTop: best.scrollTop, scrollHeight: best.scrollHeight};
    best.scrollTo({top: 0, behavior: 'smooth'});
    return before;
}"""

# Check scroll state of the message pane.
SCROLL_STATE_JS = """() => {
    const scrollers = document.querySelectorAll('[data-qa="slack_kit_scrollbar"]');
    let best = null, bestH = 0;
    for (const s of scrollers) {
        if (s.scrollHeight > bestH) { bestH = s.scrollHeight; best = s; }
    }
    if (!best) return null;
    return {scrollTop: best.scrollTop, scrollHeight: best.scrollHeight, clientHeight: best.clientHeight};
}"""

# Smooth-scroll down by a viewport's worth.
SMOOTH_SCROLL_DOWN_JS = """() => {
    const scrollers = document.querySelectorAll('[data-qa="slack_kit_scrollbar"]');
    let best = null, bestH = 0;
    for (const s of scrollers) {
        if (s.scrollHeight > bestH) { bestH = s.scrollHeight; best = s; }
    }
    if (!best) return null;
    best.scrollBy({top: best.clientHeight * 0.8, behavior: 'smooth'});
    return {
        scrollTop: best.scrollTop,
        scrollHeight: best.scrollHeight,
        clientHeight: best.clientHeight,
        atBottom: best.scrollTop + best.clientHeight >= best.scrollHeight - 20,
    };
}"""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="scrape-slack",
        description="Scrape a Slack channel's full message history via the web UI.",
    )
    parser.add_argument(
        "--login", action="store_true",
        help="Open a browser to log in and save session. Use this first.",
    )
    parser.add_argument(
        "--workspace", default=None,
        help="Slack workspace URL for login.",
    )
    parser.add_argument(
        "--url", default=None,
        help="Full Slack channel URL to scrape.",
    )
    parser.add_argument(
        "--output", default="channel_export.md",
        help="Output markdown file path (default: channel_export.md).",
    )
    parser.add_argument(
        "--session-dir", default=DEFAULT_SESSION_DIR,
        help=f"Directory for browser session (default: {DEFAULT_SESSION_DIR}).",
    )
    parser.add_argument(
        "--scroll-delay", type=float, default=3.0,
        help="Seconds between scroll steps (default: 3.0).",
    )
    parser.add_argument(
        "--max-scrolls", type=int, default=0,
        help="Max scroll attempts, 0=unlimited (default: 0).",
    )
    parser.add_argument(
        "--headed", action="store_true",
        help="Show the browser window while scraping (default: headless).",
    )

    args = parser.parse_args(argv)

    if not args.login and not args.url:
        parser.error("Either --login or --url is required.")
    if args.login and not args.workspace:
        parser.error("--workspace is required with --login.")

    return args


def do_login(workspace_url: str, session_dir: str):
    """Open a visible browser for the user to log in, then save session state."""
    session_path = os.path.abspath(session_dir)
    os.makedirs(session_path, exist_ok=True)

    print("Opening browser for login...")
    print(f"Session will be saved to: {session_path}")
    print()
    print("  1. Log in to your Slack workspace in the browser window")
    print("  2. Make sure you can see your channels")
    print("  3. Come back here and press Enter to save the session")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            session_path,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(workspace_url, wait_until="domcontentloaded")

        input("Press Enter after you've logged in and can see your channels...")
        browser.close()

    print(f"\nSession saved to {session_path}")
    print("You can now scrape channels with:")
    print(f"  python scrape_slack.py --url <channel-url>")


def harvest_messages(page, messages_by_key: dict):
    """Extract currently-visible messages and merge into the accumulator dict."""
    new_msgs = page.evaluate(EXTRACT_MESSAGES_JS)
    added = 0
    for msg in new_msgs:
        key = msg["key"]
        if key and key not in messages_by_key:
            messages_by_key[key] = msg
            added += 1
    return added


def reached_channel_top(page) -> bool:
    """Check if the channel hero header (name + invite/add people button) is visible."""
    return page.evaluate("""() => {
        if (document.querySelector('[data-qa="channel_hero"]')) return true;
        if (document.querySelector('[data-qa="channel_created_message"]')) return true;
        if (document.querySelector('.p-channel_hero')) return true;
        if (document.querySelector('.p-channel_created_message')) return true;

        const mainArea = document.querySelector('[role="main"], .p-message_pane');
        if (mainArea) {
            const buttons = mainArea.querySelectorAll('button');
            for (const btn of buttons) {
                const text = btn.textContent.toLowerCase();
                if (text.includes('add people') || text.includes('invite')) return true;
            }
        }

        const headings = document.querySelectorAll('h1, h2, h3');
        for (const h of headings) {
            const text = h.textContent.toLowerCase();
            if (text.includes('beginning of') || text.includes('created this channel')
                || text.includes('the very beginning') || text.includes('this is the start')) {
                return true;
            }
        }
        return false;
    }""")


def scroll_up_and_extract(page, scroll_delay: float, max_scrolls: int,
                           messages_by_key: dict):
    """Scroll up using smooth scrollTo(0) which triggers Slack's lazy loading.

    scrollTo({top: 0, behavior: 'smooth'}) fires continuous native scroll events
    during the animation. Slack's virtual list responds to these by loading older
    messages when near the top. When older messages are prepended, scrollHeight
    grows and scrollTop shifts back up, allowing us to repeat.
    """
    print("  Scrolling up through channel history...", flush=True)

    scroll_count = 0
    stall_count = 0
    max_stalls = 15
    prev_scroll_height = 0

    while True:
        if max_scrolls > 0 and scroll_count >= max_scrolls:
            print(f"  Reached max scroll limit ({max_scrolls}).")
            break

        # Harvest current messages
        harvest_messages(page, messages_by_key)

        # Check if we've reached the channel header
        if reached_channel_top(page):
            print(f"  Reached the channel header! ({scroll_count} scrolls, {len(messages_by_key)} messages)")
            break

        # Trigger smooth scroll to top
        state = page.evaluate(SMOOTH_SCROLL_TOP_JS)
        if not state:
            print("  Error: Could not find message scroller.")
            break

        scroll_count += 1

        # Wait for smooth scroll animation + Slack to load messages
        time.sleep(scroll_delay)

        # Harvest after scroll
        harvest_messages(page, messages_by_key)

        # Check if scrollHeight grew (Slack loaded older messages)
        new_state = page.evaluate(SCROLL_STATE_JS)
        if new_state:
            new_scroll_height = new_state["scrollHeight"]
            if new_scroll_height > prev_scroll_height:
                stall_count = 0
                prev_scroll_height = new_scroll_height
            else:
                stall_count += 1
                if stall_count >= max_stalls:
                    print(f"  No new content loading after {stall_count} attempts. ({scroll_count} scrolls)")
                    break

        if scroll_count % 5 == 0:
            print(f"  ... {scroll_count} scrolls, {len(messages_by_key)} messages", flush=True)

    # Final harvest
    harvest_messages(page, messages_by_key)
    print(f"  Upward scroll complete. {scroll_count} scrolls, {len(messages_by_key)} messages collected.")


def scroll_down_and_extract(page, messages_by_key: dict):
    """Scroll back down to catch messages the virtual list dropped during upward scroll."""
    print("  Scrolling back down to fill gaps...", flush=True)

    pass_count = 0
    stall_count = 0
    prev_msg_count = len(messages_by_key)

    while True:
        harvest_messages(page, messages_by_key)

        result = page.evaluate(SMOOTH_SCROLL_DOWN_JS)
        if not result:
            break

        time.sleep(0.8)
        harvest_messages(page, messages_by_key)

        if result.get("atBottom"):
            break

        new_msg_count = len(messages_by_key)
        if new_msg_count == prev_msg_count:
            stall_count += 1
            if stall_count >= 10:
                break
        else:
            stall_count = 0
        prev_msg_count = new_msg_count

        pass_count += 1
        if pass_count % 20 == 0:
            print(f"  ... {new_msg_count} messages ({pass_count} passes)", flush=True)

    harvest_messages(page, messages_by_key)
    print(f"  Downward scroll complete. {len(messages_by_key)} total messages.")


def format_timestamp(ts_str: str) -> str:
    """Format a timestamp string for display."""
    if not ts_str:
        return ""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        pass
    try:
        ts_float = float(ts_str)
        dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        pass
    return ts_str


def write_markdown(messages: list[dict], output_path: str, channel_url: str):
    """Write extracted messages as a markdown file."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Slack Channel Export",
        "",
        f"**Exported:** {now}",
        f"**Source:** {channel_url}",
        f"**Messages:** {len(messages)}",
        "",
        "---",
        "",
    ]

    current_date = ""

    for msg in messages:
        sender = msg.get("sender", "unknown")
        timestamp = format_timestamp(msg.get("timestamp") or msg.get("ts_value", ""))
        text = msg.get("text", "")
        day_divider = msg.get("day_divider", "")

        if day_divider and not sender and not text:
            lines.append(f"## {day_divider}")
            lines.append("")
            continue

        date_part = timestamp[:10] if len(timestamp) >= 10 else ""
        if date_part and date_part != current_date:
            current_date = date_part
            lines.append(f"## {current_date}")
            lines.append("")

        time_part = timestamp[11:] if len(timestamp) > 11 else timestamp
        if sender:
            lines.append(f"**@{sender}** ({time_part}):")
        else:
            lines.append(f"({time_part}):")

        if text:
            for line in text.split("\n"):
                lines.append(line)
        lines.append("")

    content = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\nExport written to {output_path}")
    print(f"  {len(messages)} messages")


def scrape_channel(page, channel_url: str, scroll_delay: float = 3.0,
                   max_scrolls: int = 0) -> list[dict]:
    """Scrape all messages from a Slack channel. Returns sorted list of message dicts.

    This is the reusable core — call it from other scripts with an already-open
    Playwright page that has a valid Slack session loaded.

    Each returned dict has: sender, timestamp, ts_value, text, key, day_divider.
    """
    print(f"Navigating to {channel_url}")
    page.goto(channel_url, wait_until="domcontentloaded", timeout=60000)

    print("Waiting for messages to load...")
    try:
        page.wait_for_selector(
            '.c-message_kit__message, [data-qa="virtual-list-item"]',
            timeout=30000,
        )
    except PlaywrightTimeout:
        print("  Warning: No messages detected after 30s, continuing anyway...")

    time.sleep(3)

    messages_by_key: dict[str, dict] = {}

    print("\n  [1/2] Scrolling to top and extracting messages...")
    scroll_up_and_extract(page, scroll_delay, max_scrolls, messages_by_key)

    print("\n  [2/2] Scrolling back down to fill gaps...")
    scroll_down_and_extract(page, messages_by_key)

    messages = list(messages_by_key.values())
    messages.sort(key=lambda m: float(m.get("ts_value") or "0") or 0)
    return messages


def open_browser(session_dir: str, headless: bool = True):
    """Open a Playwright browser with a saved Slack session. Returns (playwright, browser, page).

    Caller is responsible for closing the browser when done.
    """
    session_path = os.path.abspath(session_dir)

    if not os.path.exists(session_path):
        print(f"Error: No saved session found at {session_path}")
        print("Run with --login first to save your Slack session.")
        sys.exit(1)

    print(f"Loading saved session from {session_path}")

    pw = sync_playwright().start()
    browser = pw.chromium.launch_persistent_context(
        session_path,
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = browser.pages[0] if browser.pages else browser.new_page()
    return pw, browser, page


def do_scrape(channel_url: str, output_path: str, session_dir: str,
              scroll_delay: float, max_scrolls: int, headless: bool):
    """Load saved session, scroll to top extracting messages, write markdown."""
    pw, browser, page = open_browser(session_dir, headless)

    try:
        messages = scrape_channel(page, channel_url, scroll_delay, max_scrolls)
    finally:
        browser.close()
        pw.stop()

    if not messages:
        print("\nNo messages were extracted. The page might not have loaded correctly.")
        print("Try running --login again to refresh your session.")
        sys.exit(1)

    print("\nWriting markdown...")
    write_markdown(messages, output_path, channel_url)


def main():
    args = parse_args()

    if args.login:
        do_login(args.workspace, args.session_dir)
    else:
        do_scrape(
            args.url, args.output, args.session_dir,
            args.scroll_delay, args.max_scrolls,
            headless=not args.headed,
        )


if __name__ == "__main__":
    main()
