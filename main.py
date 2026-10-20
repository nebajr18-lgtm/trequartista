
import os
import re
import json
import html
import time
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
import feedparser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNEL_ID = os.environ.get(
    "TELEGRAM_CHANNEL_ID",
    "@Trequartista_official",
)

STATE_FILE = Path("seen_articles.json")
MAX_POSTS_PER_RUN = 3
MAX_SUMMARY_CHARS = 600

GROQ_MODEL = os.environ.get(
    "GROQ_MODEL",
    "openai/gpt-oss-20b",
)

RSS_FEEDS = [
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("The Guardian", "https://www.theguardian.com/football/rss"),
]

session = requests.Session()
session.headers.update({
    "User-Agent": "TREQUARTISTA-FootballNews/1.0"
})


def groq_keys():
    """Read Groq keys from GitHub Actions secrets."""
    keys = []
    for name in [
        "GROQ_API_KEY",
        "GROQ_API_KEY_1",
        "GROQ_API_KEY_2",
        "GROQ_API_KEY_3",
        "GROQ_API_KEY_4",
        "GROQ_API_KEY_5",
    ]:
        value = os.environ.get(name, "").strip()
        if value and value not in keys:
            keys.append(value)
    return keys


def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url):
    """Keep the URL for internal duplicate tracking only."""
    parts = urlsplit(url or "")
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        "",
        "",
    ))


def load_history():
    if not STATE_FILE.exists():
        return []

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        logging.exception("Could not read article history.")

    return []


def save_history(history):
    STATE_FILE.write_text(
        json.dumps(history[-2000:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def extract_image(entry):
    """Return a feed-provided image URL, if available."""
    candidates = []

    for item in entry.get("media_content", []) or []:
        if item.get("url"):
            candidates.append(item["url"])

    for item in entry.get("media_thumbnail", []) or []:
        if item.get("url"):
            candidates.append(item["url"])

    for item in entry.get("enclosures", []) or []:
        url = item.get("href") or item.get("url")
        media_type = item.get("type", "")
        if url and media_type.startswith("image/"):
            candidates.append(url)

    image = entry.get("image")
    if isinstance(image, dict) and image.get("href"):
        candidates.append(image["href"])

    for url in candidates:
        if isinstance(url, str) and url.startswith("https://"):
            return url

    return None


def make_ai_summary(title, description, source):
    """Use Groq to summarize supplied reporting, not invent facts."""
    keys = groq_keys()
    if not keys:
        return None

    prompt = f"""
You are the editor of TREQUARTISTA, a professional football
intelligence publication.

Rewrite the supplied football news as a concise English summary.
Use only facts contained in the supplied text. Do not add outside
facts, scores, quotes, transfer claims, or speculation.
Do not claim to have independently verified the story.
Use 2 or 3 short sentences, ideally under 400 characters.
Return only the summary, with no heading or bullet points.

Publisher: {source}
Headline: {title}
Article description:
{description[:5000]}
"""

    for key in keys:
        try:
            response = session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a careful football news editor. "
                                "Never invent information."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 180,
                },
                timeout=25,
            )

            if response.status_code in (401, 403, 429):
                logging.warning(
                    "Groq key unavailable or rate-limited (%s).",
                    response.status_code,
                )
                continue

            response.raise_for_status()
            result = response.json()["choices"][0]["message"]["content"]
            result = clean_text(result)

            if result:
                return result[:MAX_SUMMARY_CHARS]

        except requests.RequestException:
            logging.warning("Groq request failed; trying another key.")
        except (KeyError, IndexError, ValueError):
            logging.warning("Groq returned an unexpected response.")

    return None


def get_date(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return time.strftime("%Y-%m-%d %H:%M", parsed)
    return ""


def collect_articles(seen):
    articles = []

    for source, feed_url in RSS_FEEDS:
        try:
            response = session.get(feed_url, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            for entry in feed.entries:
                title = clean_text(entry.get("title", ""))
                url = normalize_url(entry.get("link", ""))
                description = clean_text(
                    entry.get("summary", "")
                    or entry.get("description", "")
                )

                if not title or not url or url in seen:
                    continue

                articles.append({
                    "source": source,
                    "title": title,
                    "url": url,
                    "description": description,
                    "image": extract_image(entry),
                    "date": get_date(entry),
                })

        except Exception:
            logging.exception("Could not read feed: %s", source)

    articles.sort(
        key=lambda article: article["date"],
        reverse=True,
    )
    return articles


def telegram_request(method, payload, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    for attempt in range(3):
        try:
            response = session.post(
                url,
                data=payload,
                files=files,
                timeout=35,
            )

            if response.status_code == 429 and attempt < 2:
                try:
                    delay = response.json().get(
                        "parameters", {}
                    ).get("retry_after", 3)
                except ValueError:
                    delay = 3
                time.sleep(min(int(delay), 20))
                continue

            response.raise_for_status()
            result = response.json()

            if not result.get("ok"):
                raise RuntimeError(
                    "Telegram rejected the message."
                )

            return result

        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))

    raise RuntimeError("Telegram request failed.")


def publish_article(article):
    title = article["title"]
    source = article["source"]
    description = article["description"]

    summary = make_ai_summary(title, description, source)

    if not summary:
        summary = description[:MAX_SUMMARY_CHARS].strip()

    if not summary:
        summary = "Read the latest football report from this publisher."

    caption = (
        "⚽ TREQUARTISTA\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"{html.escape(summary)}\n\n"
        f"📰 Source: {html.escape(source)}\n\n"
        "<i>BETWEEN THE LINES.</i>"
    )

    # Telegram photo captions have a 1024-character limit.
    if len(caption) > 1000:
        summary = summary[:max(100, 1000 - len(title) - 150)].rstrip()
        caption = (
            "⚽ TREQUARTISTA\n\n"
            f"<b>{html.escape(title[:220])}</b>\n\n"
            f"{html.escape(summary)}\n\n"
            f"📰 Source: {html.escape(source)}\n\n"
            "<i>BETWEEN THE LINES.</i>"
        )

    image_url = article.get("image")

    if image_url:
        try:
            image_response = session.get(
                image_url,
                timeout=20,
                stream=True,
            )
            image_response.raise_for_status()

            content_type = image_response.headers.get(
                "Content-Type", ""
            ).lower()

            if not content_type.startswith("image/"):
                raise ValueError("Feed image URL did not return an image.")

            image_bytes = image_response.content
            image_response.close()

            if len(image_bytes) > 9 * 1024 * 1024:
                raise ValueError("Image exceeds Telegram upload limit.")

            telegram_request(
                "sendPhoto",
                {
                    "chat_id": CHANNEL_ID,
                    "photo": "article.jpg",
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={
                    "photo": (
                        "article.jpg",
                        image_bytes,
                        content_type,
                    )
                },
            )
            logging.info("Published image post: %s", title)
            return

        except Exception:
            logging.warning(
                "Image unavailable; publishing text instead: %s",
                title,
            )

    telegram_request(
        "sendMessage",
        {
            "chat_id": CHANNEL_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )
    logging.info("Published text post: %s", title)


def main():
    history = load_history()
    seen = set(history)
    articles = collect_articles(seen)

    if not articles:
        logging.info("No new articles found.")
        save_history(history)
        return

    published = 0

    for article in articles:
        if published >= MAX_POSTS_PER_RUN:
            break

        try:
            publish_article(article)

            # Mark as seen only after Telegram confirms publishing.
            seen.add(article["url"])
            history.append(article["url"])
            published += 1

        except Exception:
            logging.exception(
                "Could not publish article: %s",
                article["title"],
            )

    save_history(history)
    logging.info("Published %d new article(s).", published)


if __name__ == "__main__":
    main()
