"""Markdown report generator for knowledge base articles."""

import os
import re
from datetime import datetime, timezone


CATEGORY_ORDER = [
    "Troubleshooting",
    "How-To",
    "FAQ",
    "Feature Explanation",
    "Configuration",
    "Best Practice",
]


def _slugify(text: str) -> str:
    """Convert a title to a filename-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


class KBReportGenerator:
    def __init__(self, topic: str, channel_names: list[str]):
        self.topic = topic
        self.channel_names = channel_names
        self.articles: list[dict] = []

    def add_article(
        self,
        title: str,
        category: str,
        content: str,
        source_channels: list[str],
        source_dates: list[str],
        contributors: list[str],
    ):
        """Add a synthesized KB article to the report."""
        self.articles.append({
            "title": title,
            "category": category,
            "content": content,
            "source_channels": source_channels,
            "source_dates": source_dates,
            "contributors": contributors,
        })

    def _generate_index(self) -> str:
        """Generate the index/README markdown file."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        channels_str = ", ".join(f"#{c}" for c in self.channel_names)

        lines = [
            f"# {self.topic} Knowledge Base",
            "",
            f"**Generated:** {now}",
            f"**Source Channels:** {channels_str}",
            f"**Articles:** {len(self.articles)}",
            "",
            "---",
            "",
        ]

        if not self.articles:
            lines.append(
                f'*No knowledge articles related to "{self.topic}" '
                f"were found in the searched channels.*"
            )
            return "\n".join(lines)

        # Group articles by category
        by_category: dict[str, list[dict]] = {}
        for article in self.articles:
            cat = article["category"]
            by_category.setdefault(cat, []).append(article)

        ordered_cats = [c for c in CATEGORY_ORDER if c in by_category]
        ordered_cats += sorted(c for c in by_category if c not in CATEGORY_ORDER)

        for cat in ordered_cats:
            lines.append(f"## {cat}")
            lines.append("")
            for article in by_category[cat]:
                slug = _slugify(article["title"])
                lines.append(f"- [{article['title']}]({slug}.md)")
            lines.append("")

        return "\n".join(lines)

    def _generate_article(self, article: dict) -> str:
        """Generate a single article markdown file."""
        lines = [
            f"# {article['title']}",
            "",
            f"**Category:** {article['category']}",
            "",
            "---",
            "",
            article["content"],
            "",
            "---",
            "",
            "**Sources:**",
        ]

        if article["source_dates"]:
            date_range = sorted(set(article["source_dates"]))
            lines.append(f"- **Dates:** {', '.join(date_range)}")
        if article["source_channels"]:
            ch_str = ", ".join(
                f"#{c}" for c in sorted(set(article["source_channels"]))
            )
            lines.append(f"- **Channels:** {ch_str}")
        if article["contributors"]:
            contrib_str = ", ".join(
                f"@{c}" for c in sorted(set(article["contributors"]))
            )
            lines.append(f"- **Contributors:** {contrib_str}")
        lines.append("")

        return "\n".join(lines)

    def write(self, output_dir: str) -> str:
        """Write the KB as a directory with an index and individual article files."""
        os.makedirs(output_dir, exist_ok=True)

        # Write index
        index_path = os.path.join(output_dir, "index.md")
        with open(index_path, "w") as f:
            f.write(self._generate_index())

        # Write individual articles
        used_slugs: set[str] = set()
        for article in self.articles:
            slug = _slugify(article["title"])
            # Ensure unique filenames
            if slug in used_slugs:
                counter = 2
                while f"{slug}-{counter}" in used_slugs:
                    counter += 1
                slug = f"{slug}-{counter}"
            used_slugs.add(slug)

            article_path = os.path.join(output_dir, f"{slug}.md")
            with open(article_path, "w") as f:
                f.write(self._generate_article(article))

        return output_dir
