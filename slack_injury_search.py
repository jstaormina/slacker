"""Main CLI entry point for Slack Topic Search."""

import sys
from datetime import datetime, timezone

from config import setup
from scrape_slack import do_login, open_browser, scrape_channel
from ai_analyzer import AIAnalyzer
from report_generator import ReportGenerator

# Messages within this many hours of each other are grouped into one incident.
CLUSTER_GAP_HOURS = 4

# Number of surrounding messages to include as context for each incident.
CONTEXT_WINDOW = 15


def convert_scraped_messages(scraped: list[dict]) -> list[dict]:
    """Convert scraper message format to the format AI analyzer expects.

    Scraper: {sender, timestamp, ts_value, text, key, day_divider}
    AI:      {ts, text, user}
    """
    converted = []
    for msg in scraped:
        # Skip day dividers and empty messages
        if msg.get("day_divider") and not msg.get("text"):
            continue
        if not msg.get("text"):
            continue

        converted.append({
            "ts": msg.get("ts_value", "0"),
            "text": msg.get("text", ""),
            "user": msg.get("sender", "unknown"),
        })
    return converted


def cluster_messages(messages: list[dict], gap_hours: float = CLUSTER_GAP_HOURS) -> list[list[dict]]:
    """Group messages into clusters based on time proximity.

    Messages within `gap_hours` of each other are merged into the same cluster.
    """
    if not messages:
        return []

    gap_seconds = gap_hours * 3600

    # Sort by timestamp
    sorted_msgs = sorted(messages, key=lambda m: float(m.get("ts", 0)))

    # Cluster by time gap
    clusters: list[list[dict]] = [[sorted_msgs[0]]]
    for msg in sorted_msgs[1:]:
        prev_ts = float(clusters[-1][-1].get("ts", 0))
        curr_ts = float(msg.get("ts", 0))

        if (curr_ts - prev_ts) <= gap_seconds:
            clusters[-1].append(msg)
        else:
            clusters.append([msg])

    return clusters


def gather_context(cluster: list[dict], all_messages: list[dict], window: int = CONTEXT_WINDOW) -> list[dict]:
    """Pull surrounding messages from the full channel message list.

    Finds the cluster's position in all_messages and returns a window of messages
    before and after, plus the cluster messages themselves.
    """
    if not all_messages or not cluster:
        return cluster

    # Build a set of cluster timestamps for lookup
    cluster_ts = {m["ts"] for m in cluster}

    # Find indices of cluster messages in the full list
    indices = []
    for i, msg in enumerate(all_messages):
        if msg["ts"] in cluster_ts:
            indices.append(i)

    if not indices:
        return cluster

    # Get the range: window messages before first match, window after last match
    first_idx = max(0, min(indices) - window)
    last_idx = min(len(all_messages), max(indices) + window + 1)

    return all_messages[first_idx:last_idx]


def dedup_by_context_overlap(
    incidents: list[dict], overlap_threshold: float = 0.5
) -> list[dict]:
    """Merge incidents whose context messages overlap by more than the threshold."""
    if len(incidents) <= 1:
        return incidents

    incidents.sort(key=lambda x: x["date"])

    merged = True
    while merged:
        merged = False
        new_incidents = []
        skip = set()

        for i, inc_a in enumerate(incidents):
            if i in skip:
                continue
            ts_a = inc_a["context_ts_set"]

            for j in range(i + 1, len(incidents)):
                if j in skip:
                    continue
                ts_b = incidents[j]["context_ts_set"]

                intersection = ts_a & ts_b
                smaller = min(len(ts_a), len(ts_b))
                if smaller > 0 and len(intersection) / smaller >= overlap_threshold:
                    ts_a = ts_a | ts_b
                    inc_a["context_ts_set"] = ts_a
                    inc_a["context_messages"] = sorted(
                        {m["ts"]: m for m in inc_a["context_messages"] + incidents[j]["context_messages"]}.values(),
                        key=lambda m: float(m.get("ts", 0)),
                    )
                    inc_a["participants"] = inc_a["participants"] | incidents[j]["participants"]
                    skip.add(j)
                    merged = True

            new_incidents.append(inc_a)
        incidents = new_incidents

    return incidents


def channel_name_from_url(url: str) -> str:
    """Extract a short label from a Slack channel URL for display."""
    # URL format: https://app.slack.com/client/TXXXXXXXX/CXXXXXXXX
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts else url


def main():
    args, provider = setup()

    # Handle login mode
    if args.login:
        do_login(args.workspace, args.session_dir)
        return

    print(f"\nSlack Topic Search")
    print(f"  Topic: {args.topic}")
    print(f"  Channels: {len(args.url_list)} URL(s)")
    print(f"  Output: {args.output}")
    print(f"  AI Provider: {provider.name}")
    print()

    analyzer = AIAnalyzer(provider)
    report = ReportGenerator(args.topic, [channel_name_from_url(u) for u in args.url_list], days=0)

    # Step 1: Scrape all channels
    print("[1/4] Scraping channel messages via Playwright...")
    pw, browser, page = open_browser(args.session_dir, headless=True)

    all_channel_data: dict[str, list[dict]] = {}  # url -> converted messages
    total_messages = 0

    try:
        for url in args.url_list:
            label = channel_name_from_url(url)
            print(f"\n  Scraping {label}...")
            raw_messages = scrape_channel(page, url, args.scroll_delay)
            converted = convert_scraped_messages(raw_messages)
            all_channel_data[url] = converted
            total_messages += len(converted)
            print(f"  {label}: {len(converted)} messages")
    finally:
        browser.close()
        pw.stop()

    if total_messages == 0:
        print("  No messages found in any channel.")
        report.write(args.output)
        print(f"\nEmpty report written to {args.output}")
        return

    # Step 2: Classify messages with AI
    print(f"\n[2/4] Analyzing {total_messages} messages for \"{args.topic}\" content...")
    relevant_by_channel: dict[str, list[dict]] = {}

    for url, messages in all_channel_data.items():
        label = channel_name_from_url(url)
        print(f"  {label}: analyzing {len(messages)} messages...", end=" ", flush=True)

        if not messages:
            print("0 relevant")
            continue

        relevant = analyzer.classify_messages(messages, args.topic)
        if relevant:
            relevant_by_channel[url] = relevant
        print(f"{len(relevant)} relevant")

    total_relevant = sum(len(v) for v in relevant_by_channel.values())
    if total_relevant == 0:
        print(f"\n  No messages related to \"{args.topic}\" found.")
        report.write(args.output)
        print(f"\nEmpty report written to {args.output}")
        return

    # Step 3: Cluster, gather context, dedup, and summarize
    for url, relevant_msgs in relevant_by_channel.items():
        label = channel_name_from_url(url)
        all_messages = all_channel_data[url]
        clusters = cluster_messages(relevant_msgs)
        print(f"\n[3/4] {label}: {len(relevant_msgs)} relevant msgs -> {len(clusters)} cluster(s). Gathering context...")

        raw_incidents: list[dict] = []

        for cluster_idx, cluster in enumerate(clusters):
            first_ts = cluster[0].get("ts")
            print(f"  cluster {cluster_idx + 1}/{len(clusters)} ({len(cluster)} msgs)...", end=" ", flush=True)

            # Gather surrounding context from the full scraped message list
            context_messages = gather_context(cluster, all_messages)
            context_ts_set = {m["ts"] for m in context_messages}

            # Collect participant names (already display names from scraper)
            participants = {m.get("user") for m in context_messages if m.get("user")}

            print(f"{len(context_messages)} context msgs")

            raw_incidents.append({
                "cluster": cluster,
                "context_messages": context_messages,
                "context_ts_set": context_ts_set,
                "participants": participants,
                "first_ts": first_ts,
                "date": datetime.fromtimestamp(float(first_ts or 0), tz=timezone.utc).strftime("%Y-%m-%d"),
                "channel_name": label,
                "channel_url": url,
            })

        # Dedup incidents with overlapping context
        deduped = dedup_by_context_overlap(raw_incidents, overlap_threshold=0.4)
        print(f"  After dedup: {len(deduped)} unique incident(s)")

        # Summarize each incident
        for inc_idx, inc in enumerate(deduped):
            print(f"  Summarizing {inc_idx + 1}/{len(deduped)}...", end=" ", flush=True)

            # Build pass-through user_names dict (user field is already the display name)
            user_names = {name: name for name in inc["participants"]}

            summary = analyzer.summarize_incident(
                inc["context_messages"], inc["channel_name"], user_names, args.topic
            )
            print(f"'{summary.get('title', 'untitled')}'")

            report.add_incident(
                summary=summary,
                channel_name=inc["channel_name"],
                date=inc["date"],
                participants=sorted(inc["participants"]),
                thread_messages=inc["context_messages"],
                user_names=user_names,
                permalink=inc["channel_url"],
            )

    # Step 4: Generate report
    print(f"\n[4/4] Generating report...")
    output_path = report.write(args.output)
    print(f"\nDone! Report written to {output_path}")
    print(f"  {len(report.incidents)} incident(s) documented")


if __name__ == "__main__":
    main()
