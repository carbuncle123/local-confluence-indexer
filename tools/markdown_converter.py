"""Conversion helpers for Confluence storage XHTML to Markdown."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import yaml
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown


CONVERTER_NAME = "confluence-storage-to-md"
CONVERTER_VERSION = "0.1.0"
INVALID_SLUG_CHARS = r'[/:*?"<>|\\]+'


@dataclass(slots=True)
class ConvertedMarkdown:
    """A rendered Markdown page plus its hash and metadata."""

    markdown: str
    content_hash: str
    frontmatter: dict[str, Any]


def slugify(title: str, max_length: int = 80) -> str:
    """Create a filesystem-safe slug while preserving Unicode where possible."""

    slug = re.sub(INVALID_SLUG_CHARS, "-", title.strip())
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "untitled"


def sha256_text(value: str) -> str:
    """Compute a sha256 hash with the expected prefix."""

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def extract_labels(page: dict[str, Any]) -> list[str]:
    """Extract label names from a page detail payload."""

    labels = page.get("labels") or {}
    results = labels.get("results") or []
    return [item.get("name", "") for item in results if item.get("name")]


def preprocess_storage_html(storage_html: str, page: dict[str, Any]) -> str:
    """Normalize Confluence-specific elements before Markdown conversion."""

    soup = BeautifulSoup(storage_html, "html.parser")

    for macro in soup.find_all(lambda tag: tag.name == "ac:structured-macro"):
        macro_name = macro.attrs.get("ac:name", "unknown")
        replacement = soup.new_tag("p")
        replacement.string = f"Confluence macro: {macro_name}"
        macro.replace_with(replacement)

    for mention in soup.find_all(lambda tag: tag.name == "ri:user"):
        account_id = mention.attrs.get("ri:account-id", "unknown")
        mention.replace_with(f"@accountId:{account_id}")

    for attachment in soup.find_all(lambda tag: tag.name == "ri:attachment"):
        filename = attachment.attrs.get("ri:filename", "attachment")
        replacement = soup.new_tag("p")
        replacement.string = f"attachment: {filename}"
        attachment.replace_with(replacement)

    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if href and href.startswith("/"):
            base = page.get("_links", {}).get("base")
            if base:
                anchor["href"] = f"{base}{href}"

    return str(soup)


def build_frontmatter(page: dict[str, Any], fetched_at: str, content_hash: str) -> dict[str, Any]:
    """Build frontmatter from a Confluence page detail payload."""

    base_url = page.get("_links", {}).get("base", "")
    webui = page.get("_links", {}).get("webui", "")
    site = urlparse(base_url).netloc
    version = page.get("version") or {}

    return {
        "source": "confluence",
        "site": site,
        "space_key": page.get("space_key"),
        "space_id": page.get("spaceId"),
        "page_id": page.get("id"),
        "title": page.get("title"),
        "status": page.get("status"),
        "parent_id": page.get("parentId"),
        "url": f"{base_url}{webui}" if base_url and webui else None,
        "webui": webui or None,
        "version_number": version.get("number"),
        "version_created_at": version.get("createdAt"),
        "version_message": version.get("message"),
        "version_minor_edit": version.get("minorEdit"),
        "version_author_id": version.get("authorId"),
        "author_id": page.get("authorId"),
        "owner_id": page.get("ownerId"),
        "created_at": page.get("createdAt"),
        "fetched_at": fetched_at,
        "body_format": "storage",
        "content_hash": content_hash,
        "labels": extract_labels(page),
        "converter": {
            "name": CONVERTER_NAME,
            "version": CONVERTER_VERSION,
        },
        "indexing": {
            "chunking_hint": "heading",
            "include": True,
        },
    }


def render_frontmatter(frontmatter: dict[str, Any]) -> str:
    """Render YAML frontmatter with UTF-8-friendly output."""

    return "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False) + "---\n"


def build_header_block(frontmatter: dict[str, Any]) -> str:
    """Render the Markdown header block after frontmatter."""

    return "\n".join(
        [
            f"# {frontmatter['title']}",
            "",
            "> Source: Confluence  ",
            f"> Space: {frontmatter['space_key']}  ",
            f"> Page ID: {frontmatter['page_id']}  ",
            f"> Version: {frontmatter['version_number']}  ",
            f"> Last updated: {frontmatter['version_created_at']}  ",
            f"> Fetched at: {frontmatter['fetched_at']}  ",
            f"> URL: {frontmatter['url'] or ''}",
            "",
        ]
    )


def convert_page_to_markdown(page: dict[str, Any], fetched_at: str) -> ConvertedMarkdown:
    """Convert a Confluence page detail payload to Markdown."""

    storage_html = (
        page.get("body", {}).get("storage", {}).get("value")
        or page.get("body", {}).get("value")
        or ""
    )
    normalized_html = preprocess_storage_html(storage_html, page)
    body_markdown = html_to_markdown(
        normalized_html,
        heading_style="ATX",
        bullets="-",
        strong_em_symbol="*",
    ).strip()
    content_hash = sha256_text(storage_html)
    frontmatter = build_frontmatter(page, fetched_at, content_hash)
    markdown = (
        f"{render_frontmatter(frontmatter)}\n"
        f"{build_header_block(frontmatter)}"
        f"{body_markdown}\n"
    )
    return ConvertedMarkdown(markdown=markdown, content_hash=content_hash, frontmatter=frontmatter)
