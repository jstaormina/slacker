"""Main CLI entry point for Slack Topic Search."""

import sys
from datetime import datetime, timezone

from config import setup
from slack_client import SlackClient
from ai_analyzer import AIAnalyzer
from report_generator import ReportGenerator

# Messages within this many hours of each other are grouped into one incident.
CLUSTER_GAP_HOURS = 4


def cluster_messages(messages: list[dict], gap_hours: float = CLUSTER_GAP_HOURS) -> list[list[dict]]:
    """Group messages into clusters based on time proximity and shared threads.

    Messages within `gap_hours` of each other are merged into the same cluster.
    Clusters sharing a thread_ts are also merged.
    """
    if not messages:
        return []

    gap_seconds = gap_hours * 3600

    # Sort by timestamp
    sorted_msgs = sorted(messages, key=lambda m: float(m.get("ts", 0)))

    # First pass: cluster by time gap
    clusters: list[list[dict]] = [[sorted_msgs[0]]]
    for msg in sorted_msgs[1:]:
        prev_ts = float(clusters[-1][-1].get("ts", 0))
        curr_ts = float(msg.get("ts", 0))

        if (curr_ts - prev_ts) <= gap_seconds:
            clusters[-1].append(msg)
        else:
            clusters.append([msg])

    # Second pass: merge clusters that share a thread_ts
    merged = True
    while merged:
        merged = False
        new_clusters = []
        skip = set()
        for i, cluster_a in enumerate(clusters):
            if i in skip:
                continue
            threads_a = {m.get("thread_ts") for m in cluster_a if m.get("thread_ts")}
            for j in range(i + 1, len(clusters)):
                if j in skip:
                    continue
                threads_b = {m.get("thread_ts") for m in clusters[j] if m.get("thread_ts")}
                if threads_a & threads_b:
                    cluster_a = cluster_a + clusters[j]
                    threads_a |= threads_b
                    skip.add(j)
                    merged = True
            new_clusters.append(cluster_a)
        clusters = new_clusters

    # Re-sort each cluster
    return [sorted(c, key=lambda m: float(m.get("ts", 0))) for c in clusters]


def dedup_by_context_overlap(
    incidents: list[dict], overlap_threshold: float = 0.5
) -> list[dict]:
    """Merge incidents whose context messages overlap by more than the threshold.

    Each incident dict must have a 'context_ts_set' key (set of message timestamps).
    When two incidents overlap, they're merged — the earlier one absorbs the later.
    """
    if len(incidents) <= 1:
        return incidents

    # Sort by date
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

                # Calculate overlap ratio (relative to the smaller set)
                intersection = ts_a & ts_b
                smaller = min(len(ts_a), len(ts_b))
                if smaller > 0 and len(intersection) / smaller >= overlap_threshold:
                    # Merge j into i — combine context, keep earlier date
                    ts_a = ts_a | ts_b
                    inc_a["context_ts_set"] = ts_a
                    inc_a["context_messages"] = sorted(
                        {m["ts"]: m for m in inc_a["context_messages"] + incidents[j]["context_messages"]}.values(),
                        key=lambda m: float(m.get("ts", 0)),
                    )
                    inc_a["user_ids"] = inc_a["user_ids"] | incidents[j]["user_ids"]
                    skip.add(j)
                    merged = True

            new_incidents.append(inc_a)
        incidents = new_incidents

    return incidents


def main():
    args, provider = setup()

    print(f"\nSlack Topic Search")
    print(f"  Topic: {args.topic}")
    print(f"  Channels: {', '.join(args.channel_list)}")
    print(f"  Period: last {args.days} days")
    print(f"  Output: {args.output}")
    print(f"  AI Provider: {provider.name}")
    print()

    # Initialize clients
    slack = SlackClient(args.slack_token)
    analyzer = AIAnalyzer(provider)
    report = ReportGenerator(args.topic, args.channel_list, args.days)

    # Step 1: Resolve channel names to IDs
    print("[1/5] Resolving channel names...")
    channel_map = slack.resolve_channel_ids(args.channel_list)

    missing = set(args.channel_list) - set(channel_map.keys())
    if missing:
        print(f"  Warning: Could not find channels: {', '.join(missing)}")
    if not channel_map:
        print("  Error: No channels found. Check channel names and bot permissions.")
        sys.exit(1)

    for name, cid in channel_map.items():
        print(f"  #{name} -> {cid}")

    # Step 2: Fetch messages from each channel
    print(f"\n[2/5] Fetching messages (last {args.days} days)...")
    all_channel_messages: dict[str, list[dict]] = {}
    total_messages = 0

    for name, cid in channel_map.items():
        print(f"  #{name}...", end=" ", flush=True)
        messages = slack.fetch_messages(cid, args.days)
        all_channel_messages[name] = messages
        total_messages += len(messages)
        print(f"{len(messages)} messages")

    if total_messages == 0:
        print("  No messages found in any channel.")
        report.write(args.output)
        print(f"\nEmpty report written to {args.output}")
        return

    # Step 3: Classify messages with AI
    print(f"\n[3/5] Analyzing {total_messages} messages for \"{args.topic}\" content...")
    relevant_by_channel: dict[str, list[dict]] = {}

    for name, messages in all_channel_messages.items():
        print(f"  #{name}: analyzing {len(messages)} messages...", end=" ", flush=True)

        if not messages:
            print("0 relevant")
            continue

        relevant = analyzer.classify_messages(messages, args.topic)
        if relevant:
            relevant_by_channel[name] = relevant
        print(f"{len(relevant)} relevant")

    total_relevant = sum(len(v) for v in relevant_by_channel.values())
    if total_relevant == 0:
        print(f"\n  No messages related to \"{args.topic}\" found.")
        report.write(args.output)
        print(f"\nEmpty report written to {args.output}")
        return

    # Step 4: Cluster, gather context, dedup, and summarize
    for name, relevant_msgs in relevant_by_channel.items():
        cid = channel_map[name]
        clusters = cluster_messages(relevant_msgs)
        print(f"\n[4/5] #{name}: {len(relevant_msgs)} relevant msgs -> {len(clusters)} cluster(s). Gathering context...")

        # Gather context for each cluster
        raw_incidents: list[dict] = []
        claimed_ts: set[str] = set()  # track messages already in an incident

        for cluster_idx, cluster in enumerate(clusters):
            first_ts = cluster[0].get("ts")
            last_ts = cluster[-1].get("ts")

            # Skip if all cluster messages are already claimed
            cluster_ts = {m["ts"] for m in cluster}
            if cluster_ts <= claimed_ts:
                print(f"  cluster {cluster_idx + 1}/{len(clusters)}: skipped (already covered)")
                continue

            print(f"  cluster {cluster_idx + 1}/{len(clusters)} ({len(cluster)} msgs)...", end=" ", flush=True)

            # Gather context from multiple sources, deduped by ts
            all_context: dict[str, dict] = {}

            # 1. Surrounding messages for the first and last message in the cluster
            for anchor_ts in {first_ts, last_ts}:
                surrounding = slack.fetch_surrounding_messages(cid, anchor_ts)
                for m in surrounding:
                    all_context[m["ts"]] = m

            # 2. Thread replies for any threads referenced by cluster messages
            thread_tss = {m.get("thread_ts") for m in cluster if m.get("thread_ts")}
            thread_tss |= {m.get("ts") for m in cluster if m.get("reply_count", 0) > 0}
            for tts in thread_tss:
                thread_msgs = slack.fetch_thread(cid, tts)
                for m in thread_msgs:
                    all_context[m["ts"]] = m

            # Sort all context chronologically
            context_messages = sorted(all_context.values(), key=lambda m: float(m.get("ts", 0)))

            if not context_messages:
                context_messages = cluster

            # Mark these messages as claimed
            context_ts_set = {m["ts"] for m in context_messages}
            claimed_ts |= context_ts_set

            # Collect user IDs
            user_ids = {m.get("user") for m in context_messages if m.get("user")}

            print(f"{len(context_messages)} context msgs")

            raw_incidents.append({
                "cluster": cluster,
                "context_messages": context_messages,
                "context_ts_set": context_ts_set,
                "user_ids": user_ids,
                "first_ts": first_ts,
                "date": datetime.fromtimestamp(float(first_ts), tz=timezone.utc).strftime("%Y-%m-%d"),
                "channel_name": name,
                "channel_id": cid,
            })

        # Dedup incidents with overlapping context
        deduped = dedup_by_context_overlap(raw_incidents, overlap_threshold=0.4)
        print(f"  After dedup: {len(deduped)} unique incident(s)")

        # Now summarize each deduped incident
        for inc_idx, inc in enumerate(deduped):
            print(f"  Summarizing {inc_idx + 1}/{len(deduped)}...", end=" ", flush=True)

            user_names = slack.resolve_users_bulk(inc["user_ids"])
            permalink = slack.get_message_permalink(cid, inc["first_ts"])

            summary = analyzer.summarize_incident(
                inc["context_messages"], name, user_names, args.topic
            )
            print(f"'{summary.get('title', 'untitled')}'")

            participants = [user_names.get(uid, uid) for uid in inc["user_ids"]]
            report.add_incident(
                summary=summary,
                channel_name=name,
                date=inc["date"],
                participants=participants,
                thread_messages=inc["context_messages"],
                user_names=user_names,
                permalink=permalink,
            )

    # Step 5: Generate report
    print(f"\n[5/5] Generating report...")
    output_path = report.write(args.output)
    print(f"\nDone! Report written to {output_path}")
    print(f"  {len(report.incidents)} incident(s) documented")


if __name__ == "__main__":
    main()
