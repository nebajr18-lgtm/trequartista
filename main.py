
import os
import re
import html
import json
import time
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
import feedparser

# ============================================================
# TREQUARTISTA — BETWEEN THE LINES.
# Football news collector, AI summarizer and Telegram publisher
# ============================================================

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

# Keep the complete model configuration from Lambadina.
GROQ_MODEL = "openai/gpt-oss-120b"

GROQ_FALLBACK_MODELS = [
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "qwen/qwen3.6-27b",
    "groq/compound",
]

RSS_FEEDS = [
    (
        "BBC Sport",
        "https://feeds.bbci.co.uk/sport/football/rss.xml",
    ),
    (
        "The Guardian",
        "https://www.theguardian.com/football/rss",
    ),
]

session = requests.Session()
session.headers.update({
    "User-Agent": "TREQUARTISTA-FootballNews/1.0"
})


# ============================================================
# GROQ API KEYS
# ============================================================

def get_groq_keys():
    """Read all configured Groq keys without exposing them."""
    names = [
        "GROQ_API_KEY",
        "GROQ_API_KEY_1",
        "GROQ_API_KEY_2",
        "GROQ_API_KEY_3",
        "GROQ_API_KEY_4",
        "GROQ_API_KEY_5",
    ]

    keys = []

    for name in names:
        value = os.environ.get(name, "").strip()

        if value and value not in keys:
            keys.append(value)

    return keys


def get_models():
    """Primary model first, followed by every configured fallback."""
    return [GROQ_MODEL] + GROQ_FALLBACK_MODELS


# ============================================================
# TEXT AND URL UTILITIES
# ============================================================

def clean_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url):
    """Normalize article URLs for duplicate detection."""
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
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        if isinstance(data, list):
            return data

    except (OSError, json.JSONDecodeError):
        logging.exception("Could not read article history.")

    return []


def save_history(history):
    STATE_FILE.write_text(
        json.dumps(
            history[-2000:],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# ARTICLE IMAGES
# ============================================================

def extract_image(entry):
    """Find an image URL provided by the RSS entry."""
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
        if (
            isinstance(url, str)
            and url.startswith("https://")
        ):
            return url

    return None


# ============================================================
# GROQ AI SUMMARIZATION WITH ALL MODEL FALLBACKS
# ============================================================

def make_ai_summary(title, description, source):
    """
    Try the primary model, then every fallback model.
    Try configured API keys if a key is unavailable or limited.
    Return None if AI summarization cannot be completed.
    """
    keys = get_groq_keys()

    if not keys:
        logging.info(
            "No Groq API keys configured; using RSS description."
        )
        return None

    models = get_models()

    prompt = f"""
You are the football news editor for TREQUARTISTA,
a professional English-language football intelligence brand.

Write a concise, accurate summary of the supplied report.

Rules:
- Use only facts contained in the supplied text.
- Never invent scores, statistics, quotations, transfers,
  injuries, dates or other information.
- Do not present unverified claims as confirmed facts.
- Preserve the original meaning and attribution.
- Write 2 to 3 short sentences.
- Aim for 250 to 400 characters.
- Return only the summary, without a heading or bullet points.

Publisher: {source}

Headline:
{title}

Supplied article text:
{description[:6000]}
"""

    for model in models:
        model_failed = False

        for key_index, key in enumerate(keys, start=1):
            try:
                response = session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are a careful football editor. "
                                    "Never invent or embellish facts."
                                ),
                            },
                            {
                                "role": "user",
                                "content": prompt,
                            },
                        ],
                        "temperature": 0.2,
                        "max_tokens": 220,
                    },
                    timeout=30,
                )

                if response.status_code in (401, 403):
                    logging.warning(
                        "Groq key %s rejected; trying another key.",
                        key_index,
                    )
                    continue

                if response.status_code == 429:
                    logging.warning(
                        "Groq rate limit for model %s, key %s.",
                        model,
                        key_index,
                    )
                    continue

                if response.status_code in (400, 404, 410):
                    logging.warning(
                        "Model %s unavailable or request unsupported.",
                        model,
                    )
                    model_failed = True
                    break

                response.raise_for_status()

                data = response.json()
                summary = clean_text(
                    data["choices"][0]["message"]["content"]
                )

                if summary:
                    logging.info(
                        "AI summary generated using model %s.",
                        model,
                    )
                    return summary[:MAX_SUMMARY_CHARS]

                logging.warning(
                    "Model %s returned an empty summary.",
                    model,
                )

            except requests.Timeout:
                logging.warning(
                    "Groq timeout using model %s, key %s.",
                    model,
                    key_index,
                )

            except requests.RequestException:
                logging.warning(
                    "Groq request failed using model %s.",
                    model,
                )

            except (KeyError, IndexError, ValueError):
                logging.warning(
                    "Unexpected Groq response from model %s.",
                    model,
                )

        if model_failed:
            continue

    logging.warning(
        "All Groq model/key attempts failed. "
        "Using the original RSS description."
    )

    return None


# ============================================================
# RSS COLLECTION
# ============================================================

def get_date(entry):
    parsed = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if parsed:
        return time.strftime("%Y-%m-%d %H:%M", parsed)

    return ""


def collect_articles(seen):
    articles = []

    for source, feed_url in RSS_FEEDS:
        try:
            response = session.get(
                feed_url,
                timeout=20,
            )
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
            logging.exception(
                "Could not collect feed from %s.",
                source,
            )

    articles.sort(
        key=lambda article: article["date"],
        reverse=True,
    )

    return articles


# ============================================================
# TELEGRAM PUBLISHING
# ============================================================

def telegram_request(method, payload, files=None):
    url = (
        f"https://api.telegram.org/bot"
        f"{BOT_TOKEN}/{method}"
    )

    for attempt in range(3):
        try:
            response = session.post(
                url,
                data=payload,
                files=files,
                timeout=40,
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
                    "Telegram rejected the request."
                )

            return result

        except requests.RequestException:
            if attempt == 2:
                raise

            time.sleep(2 * (attempt + 1))

    raise RuntimeError("Telegram request failed.")


def build_caption(article, summary):
    title = article["title"]
    source = article["source"]

    # Deliberately do not include the article URL.
    caption = (
        "⚽ TREQUARTISTA\n\n"
        f"<b>{html.escape(title)}</b>\n\n"
        f"{html.escape(summary)}\n\n"
        f"📰 Source: {html.escape(source)}\n\n"
        "<i>BETWEEN THE LINES.</i>"
    )

    # Keep safely below Telegram's 1024-character photo caption limit.
    if len(caption) > 1000:
        available = max(
            100,
            1000 - len(title[:220]) - len(source) - 100,
        )

        summary = summary[:available].rstrip()

        caption = (
            "⚽ TREQUARTISTA\n\n"
            f"<b>{html.escape(title[:220])}</b>\n\n"
            f"{html.escape(summary)}\n\n"
            f"📰 Source: {html.escape(source)}\n\n"
            "<i>BETWEEN THE LINES.</i>"
        )

    return caption


def publish_article(article):
    title = article["title"]
    description = article["description"]
    source = article["source"]

    summary = make_ai_summary(
        title,
        description,
        source,
    )

    if not summary:
        summary = description[:MAX_SUMMARY_CHARS].strip()

    if not summary:
        summary = (
            "A new football report has been published "
            "by this source."
        )

    caption = build_caption(article, summary)
    image_url = article.get("image")

    if image_url:
        try:
            image_response = session.get(
                image_url,
                timeout=20,
            )
            image_response.raise_for_status()

            content_type = image_response.headers.get(
                "Content-Type", ""
            ).lower()

            if not content_type.startswith("image/"):
                raise ValueError(
                    "Image URL did not return an image."
                )

            image_bytes = image_response.content

            if len(image_bytes) > 9 * 1024 * 1024:
                raise ValueError(
                    "Image exceeds the upload size limit."
                )

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

            logging.info(
                "Published image post: %s",
                title,
            )
            return

        except Exception:
            logging.exception(
                "Could not publish image for: %s",
                title,
            )

    # Text-only fallback if the image is missing or unusable.
    telegram_request(
        "sendMessage",
        {
            "chat_id": CHANNEL_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )

    logging.info(
        "Published text post: %s",
        title,
    )


# ============================================================
# MAIN
# ============================================================

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

            # Record an article only after Telegram confirms
            # the publication succeeded.
            seen.add(article["url"])
            history.append(article["url"])
            published += 1

        except Exception:
            logging.exception(
                "Could not publish: %s",
                article["title"],
            )

    save_history(history)

    logging.info(
        "Finished. Published %s new article(s).",
        published,
    )


if __name__ == "__main__":
    main()
