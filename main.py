
"""
TREQUARTISTA — BETWEEN THE LINES.
RSS Football News Collector
"""

import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import feedparser
import requests


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL_ID = os.getenv(
    "TELEGRAM_CHANNEL_ID", "@Trequartista_official"
).strip()

STATE_FILE = Path("seen_articles.json")
MAX_POSTS_PER_RUN = 3
MAX_SEEN_ARTICLES = 2000
REQUEST_TIMEOUT = 20

RSS_FEEDS = {
    "BBC Sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "The Guardian": "https://www.theguardian.com/football/rss",
}

HEADERS = {
    "User-Agent": "TrequartistaFootballNews/1.0"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("trequartista")


# --------------------------------------------------
# TEXT AND ARTICLE UTILITIES
# --------------------------------------------------

def clean_text(value):
    """Remove HTML and normalize article text."""
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url):
    """Remove URL fragments and common tracking parameters."""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return ""

        return urlunparse((
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",
            "",
            "",
        ))
    except (ValueError, AttributeError):
        return ""


def load_seen_articles():
    """Load previously processed article links."""
    if not STATE_FILE.exists():
        return []

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [
                str(item) for item in data
                if isinstance(item, str)
            ][-MAX_SEEN_ARTICLES:]
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Cannot read article history: %s", exc)
        raise RuntimeError(
            "Article history is unavailable; stopping to avoid reposts."
        ) from exc

    raise RuntimeError("Article history has an invalid format.")


def save_seen_articles(links):
    """Save article history for the next scheduled run."""
    STATE_FILE.write_text(
        json.dumps(links[-MAX_SEEN_ARTICLES:], indent=2),
        encoding="utf-8",
    )


def get_entry_date(entry):
    """Return a comparable publication timestamp."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")

    if parsed:
        return time.mktime(parsed)

    return 0


# --------------------------------------------------
# RSS COLLECTION
# --------------------------------------------------

def collect_articles(seen):
    """Fetch available RSS feeds and find new articles."""
    articles = []
    seen_set = set(seen)

    for source, feed_url in RSS_FEEDS.items():
        try:
            log.info("Checking %s", source)

            response = requests.get(
                feed_url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            feed = feedparser.parse(response.content)

            if feed.bozo and not feed.entries:
                log.warning("%s returned an unreadable feed", source)
                continue

            for entry in feed.entries:
                title = clean_text(entry.get("title", ""))
                link = normalize_url(entry.get("link", ""))

                if not title or not link or link in seen_set:
                    continue

                summary = clean_text(
                    entry.get("summary", "")
                    or entry.get("description", "")
                )

                articles.append({
                    "source": source,
                    "title": title,
                    "link": link,
                    "summary": summary[:500],
                    "published": get_entry_date(entry),
                })

        except requests.RequestException as exc:
            log.warning("Could not fetch %s: %s", source, exc)
        except Exception:
            log.exception("Unexpected error reading %s", source)

    # Newest first; retain stable ordering for undated entries.
    articles.sort(
        key=lambda item: item["published"],
        reverse=True,
    )

    # Deduplicate articles across feeds by normalized URL.
    unique = []
    local_seen = set(seen_set)

    for article in articles:
        if article["link"] not in local_seen:
            unique.append(article)
            local_seen.add(article["link"])

    return unique


# --------------------------------------------------
# TELEGRAM PUBLISHING
# --------------------------------------------------

def format_post(article):
    """Build a readable Telegram post using the source's title."""
    title = article["title"]
    summary = article["summary"]

    # Keep posts concise and avoid duplicating the title.
    if summary.lower().strip() == title.lower().strip():
        summary = ""

    lines = [
        "⚽ TREQUARTISTA",
        "",
        title,
    ]

    if summary:
        lines.extend(["", summary])

    lines.extend([
        "",
        f"📰 Source: {article['source']}",
        f"🔗 Read more: {article['link']}",
        "",
        "BETWEEN THE LINES.",
    ])

    return "\n".join(lines)


def publish_to_telegram(message):
    """Publish a post and confirm Telegram accepted it."""
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured.")

    if not CHANNEL_ID:
        raise RuntimeError("TELEGRAM_CHANNEL_ID is not configured.")

    endpoint = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        endpoint,
        json={
            "chat_id": CHANNEL_ID,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram did not confirm the message was published."
        )

    log.info("Telegram accepted the post.")
    return result["result"]["message_id"]


# --------------------------------------------------
# MAIN PIPELINE
# --------------------------------------------------

def main():
    log.info("TREQUARTISTA news collector starting.")

    seen = load_seen_articles()
    articles = collect_articles(seen)

    if not articles:
        log.info("No new articles found.")
        return

    # Save each successful post immediately so a later failure
    # does not cause that same article to be posted again.
    processed = list(seen)
    published_count = 0

    for article in articles[:MAX_POSTS_PER_RUN]:
        try:
            message = format_post(article)
            message_id = publish_to_telegram(message)

            processed.append(article["link"])
            save_seen_articles(processed)
            published_count += 1

            log.info(
                "Published article %s (Telegram message %s)",
                article["title"],
                message_id,
            )

        except Exception as exc:
            log.error(
                "Publishing failed for '%s': %s",
                article["title"],
                exc,
            )
            save_seen_articles(processed)
            raise

    save_seen_articles(processed)

    log.info(
        "Finished. Published %s article(s).",
        published_count,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("TREQUARTISTA run failed.")
        sys.exit(1)
