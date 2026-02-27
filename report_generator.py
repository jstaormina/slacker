"""Report generator for knowledge base articles in Markdown, HTML, and PDF."""

import os
import re
from datetime import datetime, timezone

import markdown
from fpdf import FPDF


CATEGORY_ORDER = [
    "Troubleshooting",
    "How-To",
    "FAQ",
    "Feature Explanation",
    "Configuration",
    "Best Practice",
]

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         max-width: 900px; margin: 0 auto; padding: 2rem; color: #24292e; line-height: 1.6; }}
  h1 {{ border-bottom: 2px solid #e1e4e8; padding-bottom: .3em; }}
  h2 {{ border-bottom: 1px solid #e1e4e8; padding-bottom: .3em; margin-top: 2em; }}
  h3 {{ margin-top: 1.5em; }}
  hr {{ border: none; border-top: 1px solid #e1e4e8; margin: 2em 0; }}
  code {{ background: #f6f8fa; padding: .2em .4em; border-radius: 3px; font-size: 85%; }}
  pre {{ background: #f6f8fa; padding: 1em; border-radius: 6px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  a {{ color: #0366d6; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .meta {{ color: #586069; font-size: 0.9em; }}
  .article {{ page-break-before: always; }}
  .article:first-of-type {{ page-break-before: avoid; }}
  ul {{ padding-left: 1.5em; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _sanitize_for_pdf(text: str) -> str:
    """Replace common Unicode characters that Latin-1 PDF fonts can't encode."""
    replacements = {
        "\u2014": "--",   # em dash
        "\u2013": "-",    # en dash
        "\u2018": "'",    # left single quote
        "\u2019": "'",    # right single quote / apostrophe
        "\u201c": '"',    # left double quote
        "\u201d": '"',    # right double quote
        "\u2026": "...",  # ellipsis
        "\u2022": "*",    # bullet
        "\u00a0": " ",    # non-breaking space
        "\u2011": "-",    # non-breaking hyphen
        "\u2010": "-",    # hyphen
        "\u00b7": "*",    # middle dot
    }
    for char, replacement in replacements.items():
        text = text.replace(char, replacement)
    # Drop anything else outside Latin-1
    return text.encode("latin-1", errors="replace").decode("latin-1")


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

    # -- Markdown helpers (used by all formats) --

    def _generate_index_md(self) -> str:
        """Generate the index/README markdown."""
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

    def _generate_article_md(self, article: dict) -> str:
        """Generate a single article markdown."""
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

    # -- Combined markdown (used for HTML/PDF conversion) --

    def _generate_combined_md(self) -> str:
        """Generate a single markdown document with index + all articles."""
        parts = [self._generate_index_md()]

        for article in self.articles:
            parts.append("\n\n---\n\n")
            parts.append(self._generate_article_md(article))

        return "".join(parts)

    # -- Writers --

    def _write_md(self, output_dir: str) -> str:
        """Write the KB as a directory with an index and individual article files."""
        os.makedirs(output_dir, exist_ok=True)

        index_path = os.path.join(output_dir, "index.md")
        with open(index_path, "w") as f:
            f.write(self._generate_index_md())

        used_slugs: set[str] = set()
        for article in self.articles:
            slug = _slugify(article["title"])
            if slug in used_slugs:
                counter = 2
                while f"{slug}-{counter}" in used_slugs:
                    counter += 1
                slug = f"{slug}-{counter}"
            used_slugs.add(slug)

            article_path = os.path.join(output_dir, f"{slug}.md")
            with open(article_path, "w") as f:
                f.write(self._generate_article_md(article))

        return output_dir

    def _render_html(self) -> str:
        """Render the combined markdown to a full HTML document."""
        md_text = self._generate_combined_md()
        body_html = markdown.markdown(
            md_text,
            extensions=["fenced_code", "tables", "toc"],
        )
        return HTML_TEMPLATE.format(
            title=f"{self.topic} Knowledge Base",
            body=body_html,
        )

    def _write_html(self, output_dir: str) -> str:
        """Write the KB as a single HTML file."""
        os.makedirs(output_dir, exist_ok=True)
        html_content = self._render_html()
        out_path = os.path.join(output_dir, "knowledge-base.html")
        with open(out_path, "w") as f:
            f.write(html_content)
        return out_path

    def _write_pdf(self, output_dir: str) -> str:
        """Write the KB as a single PDF file."""
        os.makedirs(output_dir, exist_ok=True)
        md_text = self._generate_combined_md()
        body_html = markdown.markdown(
            md_text,
            extensions=["fenced_code", "tables"],
        )
        body_html = _sanitize_for_pdf(body_html)
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.write_html(body_html)
        out_path = os.path.join(output_dir, "knowledge-base.pdf")
        pdf.output(out_path)
        return out_path

    def write(self, output_dir: str, fmt: str = "pdf") -> str:
        """Write the KB in the specified format. Returns the output path."""
        if fmt == "md":
            return self._write_md(output_dir)
        elif fmt == "html":
            return self._write_html(output_dir)
        else:
            return self._write_pdf(output_dir)
