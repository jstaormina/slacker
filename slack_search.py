"""Main CLI entry point for Slack Knowledge Base Extractor."""

import json
import os
import sys
from datetime import datetime, timezone

from config import setup
from scrape_slack import do_login, open_browser, scrape_channel
from ai_analyzer import AIAnalyzer
from report_generator import KBReportGenerator

# Messages within this many hours of each other are grouped into one cluster.
CLUSTER_GAP_HOURS = 4

# Number of surrounding messages to include as context for each cluster.
CONTEXT_WINDOW = 15


def _cache_path(cache_dir: str, url: str) -> str:
    """Return the JSON cache file path for a channel URL."""
    # Use the last path segment (channel ID) as the filename
    slug = url.rstrip("/").split("/")[-1]
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{slug}.json")


def load_cache(cache_dir: str, url: str) -> list[dict] | None:
    """Load cached raw messages for a URL, or None if no cache exists."""
    path = _cache_path(cache_dir, url)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache_dir: str, url: str, messages: list[dict]):
    """Save raw scraped messages to the cache."""
    path = _cache_path(cache_dir, url)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    print(f"  Cached {len(messages)} messages -> {path}")


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
    clusters: list[dict], overlap_threshold: float = 0.5
) -> list[dict]:
    """Merge clusters whose context messages overlap by more than the threshold."""
    if len(clusters) <= 1:
        return clusters

    clusters.sort(key=lambda x: x["date"])

    merged = True
    while merged:
        merged = False
        new_clusters = []
        skip = set()

        for i, cl_a in enumerate(clusters):
            if i in skip:
                continue
            ts_a = cl_a["context_ts_set"]

            for j in range(i + 1, len(clusters)):
                if j in skip:
                    continue
                ts_b = clusters[j]["context_ts_set"]

                intersection = ts_a & ts_b
                smaller = min(len(ts_a), len(ts_b))
                if smaller > 0 and len(intersection) / smaller >= overlap_threshold:
                    ts_a = ts_a | ts_b
                    cl_a["context_ts_set"] = ts_a
                    cl_a["context_messages"] = sorted(
                        {m["ts"]: m for m in cl_a["context_messages"] + clusters[j]["context_messages"]}.values(),
                        key=lambda m: float(m.get("ts", 0)),
                    )
                    cl_a["participants"] = cl_a["participants"] | clusters[j]["participants"]
                    skip.add(j)
                    merged = True

            new_clusters.append(cl_a)
        clusters = new_clusters

    return clusters


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

    print(f"\nSlack Knowledge Base Extractor")
    print(f"  Topic: {args.topic}")
    print(f"  Channels: {len(args.url_list)} URL(s)")
    print(f"  Output: {args.output} ({args.format})")
    print(f"  AI Provider: {provider.name}")
    print()

    analyzer = AIAnalyzer(provider)
    report = KBReportGenerator(args.topic, [channel_name_from_url(u) for u in args.url_list])

    # Step 1: Scrape all channels (or load from cache)
    print("[1/6] Scraping channel messages via Playwright...")

    all_channel_data: dict[str, list[dict]] = {}  # url -> converted messages
    total_messages = 0
    urls_needing_scrape = []

    for url in args.url_list:
        label = channel_name_from_url(url)
        if not args.no_cache:
            cached = load_cache(args.cache_dir, url)
            if cached is not None:
                print(f"\n  {label}: loaded {len(cached)} messages from cache")
                converted = convert_scraped_messages(cached)
                all_channel_data[url] = converted
                total_messages += len(converted)
                continue
        urls_needing_scrape.append(url)

    if urls_needing_scrape:
        pw, browser, page = open_browser(args.session_dir, headless=True)
        try:
            for url in urls_needing_scrape:
                label = channel_name_from_url(url)
                print(f"\n  Scraping {label}...")
                raw_messages = scrape_channel(page, url, args.scroll_delay)
                save_cache(args.cache_dir, url, raw_messages)
                converted = convert_scraped_messages(raw_messages)
                all_channel_data[url] = converted
                total_messages += len(converted)
                print(f"  {label}: {len(converted)} messages")
        finally:
            browser.close()
            pw.stop()

    if total_messages == 0:
        print("  No messages found in any channel.")
        report.write(args.output, args.format)
        print(f"\nEmpty KB written to {args.output}/")
        return

    # Step 2: Classify messages with AI
    print(f'\n[2/6] Analyzing {total_messages} messages for "{args.topic}" knowledge...')
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
        print(f'\n  No knowledge related to "{args.topic}" found.')
        report.write(args.output, args.format)
        print(f"\nEmpty KB written to {args.output}/")
        return

    # Steps 3-4: Cluster, gather context, dedup, extract knowledge
    all_extractions: list[dict] = []

    for url, relevant_msgs in relevant_by_channel.items():
        label = channel_name_from_url(url)
        all_messages = all_channel_data[url]
        clusters = cluster_messages(relevant_msgs)
        print(f"\n[3/6] {label}: {len(relevant_msgs)} relevant msgs -> {len(clusters)} cluster(s)")

        raw_clusters: list[dict] = []

        for cluster_idx, cluster in enumerate(clusters):
            first_ts = cluster[0].get("ts")
            print(f"  cluster {cluster_idx + 1}/{len(clusters)} ({len(cluster)} msgs)...", end=" ", flush=True)

            context_messages = gather_context(cluster, all_messages)
            context_ts_set = {m["ts"] for m in context_messages}
            participants = {m.get("user") for m in context_messages if m.get("user")}

            print(f"{len(context_messages)} context msgs")

            raw_clusters.append({
                "cluster": cluster,
                "context_messages": context_messages,
                "context_ts_set": context_ts_set,
                "participants": participants,
                "first_ts": first_ts,
                "date": datetime.fromtimestamp(float(first_ts or 0), tz=timezone.utc).strftime("%Y-%m-%d"),
                "channel_name": label,
                "channel_url": url,
            })

        deduped = dedup_by_context_overlap(raw_clusters, overlap_threshold=0.4)
        print(f"  After dedup: {len(deduped)} unique cluster(s)")

        # Extract knowledge from each cluster
        print(f"\n[4/6] Extracting knowledge from {len(deduped)} cluster(s)...")
        for ext_idx, cluster_data in enumerate(deduped):
            print(f"  Extracting {ext_idx + 1}/{len(deduped)}...", end=" ", flush=True)

            user_names = {name: name for name in cluster_data["participants"]}

            extraction = analyzer.extract_knowledge(
                cluster_data["context_messages"],
                cluster_data["channel_name"],
                user_names,
                args.topic,
            )
            print(f"'{extraction.get('title', 'untitled')}'")

            # Attach source metadata for report generation
            extraction["_source_channel"] = cluster_data["channel_name"]
            extraction["_source_date"] = cluster_data["date"]
            extraction["_source_contributors"] = sorted(cluster_data["participants"])

            all_extractions.append(extraction)

    if not all_extractions:
        print("  No knowledge extracted.")
        report.write(args.output, args.format)
        print(f"\nEmpty KB written to {args.output}/")
        return

    # Step 5: Group extractions by topic
    print(f"\n[5/6] Grouping {len(all_extractions)} extraction(s) by topic...")
    groups = analyzer.group_topics(all_extractions)
    print(f"  {len(groups)} topic group(s) identified")

    # Step 6: Synthesize articles per group
    print(f"\n[6/6] Synthesizing {len(groups)} KB article(s)...")
    for grp_idx, group in enumerate(groups):
        group_title = group.get("group_title", "Untitled")
        indices = group.get("indices", [])
        group_extractions = [all_extractions[i] for i in indices if i < len(all_extractions)]

        if not group_extractions:
            continue

        print(f"  Article {grp_idx + 1}/{len(groups)}: '{group_title}'...", end=" ", flush=True)

        article = analyzer.synthesize_article(group_title, group_extractions, args.topic)
        print("done")

        # Collect source metadata from all extractions in the group
        source_channels = [ext.get("_source_channel", "") for ext in group_extractions]
        source_dates = [ext.get("_source_date", "") for ext in group_extractions]
        contributors = []
        for ext in group_extractions:
            contributors.extend(ext.get("_source_contributors", []))

        report.add_article(
            title=article.get("title", group_title),
            category=article.get("category", "FAQ"),
            content=article.get("content", ""),
            source_channels=source_channels,
            source_dates=source_dates,
            contributors=contributors,
        )

    # Generate report
    output_path = report.write(args.output, args.format)
    print(f"\nDone! Knowledge base written to {output_path}")
    print(f"  {len(report.articles)} article(s) generated")


if __name__ == "__main__":
    main()
