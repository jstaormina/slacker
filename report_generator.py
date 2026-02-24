"""Markdown report generator for topic search results."""

from datetime import datetime, timezone


class ReportGenerator:
    def __init__(self, topic: str, channel_names: list[str], days: int):
        self.topic = topic
        self.channel_names = channel_names
        self.days = days
        self.incidents: list[dict] = []

    def add_incident(
        self,
        summary: dict,
        channel_name: str,
        date: str,
        participants: list[str],
        thread_messages: list[dict],
        user_names: dict[str, str],
        permalink: str | None = None,
    ):
        """Add an incident to the report.

        summary: dict from AIAnalyzer.summarize_incident (title, summary, key_quotes, severity)
        """
        self.incidents.append(
            {
                "summary": summary,
                "channel_name": channel_name,
                "date": date,
                "participants": participants,
                "thread_messages": thread_messages,
                "user_names": user_names,
                "permalink": permalink,
            }
        )

    def generate(self) -> str:
        """Generate the full markdown report."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        channels_str = ", ".join(f"#{c}" for c in self.channel_names)

        lines = [
            f"# {self.topic.title()}-Related Incidents Report",
            "",
            f"**Generated:** {now}",
            f"**Channels:** {channels_str}",
            f"**Search Period:** Last {self.days} days",
            f"**Topic:** {self.topic}",
            "",
            "---",
            "",
            "## Summary",
            "",
            f"- **{len(self.incidents)} incident(s)** found across {len(self.channel_names)} channel(s)",
        ]

        if self.incidents:
            severity_counts = {}
            for inc in self.incidents:
                sev = inc["summary"].get("severity", "informational")
                severity_counts[sev] = severity_counts.get(sev, 0) + 1
            for sev, count in sorted(severity_counts.items()):
                lines.append(f"- {sev.title()}: {count}")

        lines.extend(["", "---", ""])

        if not self.incidents:
            lines.append(f"*No incidents related to \"{self.topic}\" were found in the searched channels.*")
            return "\n".join(lines)

        # Sort incidents by date (newest first)
        self.incidents.sort(key=lambda x: x["date"], reverse=True)

        for idx, inc in enumerate(self.incidents, 1):
            summary = inc["summary"]
            title = summary.get("title", "Untitled Incident")
            severity = summary.get("severity", "informational")

            lines.extend([
                f"## Incident {idx}: {title}",
                "",
                f"- **Date:** {inc['date']}",
                f"- **Channel:** #{inc['channel_name']}",
                f"- **Severity:** {severity.title()}",
                f"- **Participants:** {', '.join(f'@{p}' for p in inc['participants'])}",
            ])

            if inc.get("permalink"):
                lines.append(f"- **Link:** {inc['permalink']}")

            lines.extend(["", "### Summary", "", summary.get("summary", "No summary available."), ""])

            key_quotes = summary.get("key_quotes", [])
            if key_quotes:
                lines.append("### Key Messages")
                lines.append("")
                for quote in key_quotes:
                    lines.append(f"> {quote}")
                    lines.append("")

            # Include full thread context
            if inc["thread_messages"]:
                lines.append("### Full Thread Context")
                lines.append("")
                for msg in inc["thread_messages"]:
                    ts = float(msg.get("ts", 0))
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                    user = inc["user_names"].get(msg.get("user", ""), msg.get("user", "unknown"))
                    text = msg.get("text", "").replace("\n", "\n> ")
                    lines.append(f"> **@{user}** ({dt}):")
                    lines.append(f"> {text}")
                    lines.append(">")
                lines.append("")

            lines.extend(["---", ""])

        return "\n".join(lines)

    def write(self, output_path: str):
        """Generate and write the report to a file."""
        content = self.generate()
        with open(output_path, "w") as f:
            f.write(content)
        return output_path
