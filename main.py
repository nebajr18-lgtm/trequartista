
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

BOT_TOKEN = 8805548182:AAG4JpSrVSIXNqq_F7zdZRT34kRESro3Qpw
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
    ("BBC Sport", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("The Guardian", "https://www.theguardian.com/football/rss"),
    ("Sky Sports", "https://www.skysports.com/rss/11095"),
    ("ESPN Soccer", "https://news.google.com/rss/search?q=site%3Aespn.com%2Fsoccer&hl=en-US&gl=US&ceid=US:en"),
    ("L'Équipe Football", "https://dwh.lequipe.fr/api/edito/rss?path=/Football/"),
]

# Full source registry. Registration does not imply live API access.
# Sources marked "needs_api_or_approved_feed" require a verified,
# permitted connection before they can be used for automatic collection.
SOURCE_REGISTRY = [
    {"name": "Premier League", "type": "official_competition", "access": "needs_api_or_approved_feed"},
    {"name": "LaLiga", "type": "official_competition", "access": "needs_api_or_approved_feed"},
    {"name": "Serie A", "type": "official_competition", "access": "needs_api_or_approved_feed"},
    {"name": "Bundesliga", "type": "official_competition", "access": "needs_api_or_approved_feed"},
    {"name": "Ligue 1", "type": "official_competition", "access": "needs_api_or_approved_feed"},
    {"name": "UEFA", "type": "official_competition", "access": "needs_api_or_approved_feed"},
    {"name": "FIFA", "type": "official_competition", "access": "needs_api_or_approved_feed"},
    {"name": "BBC Sport", "type": "news_rss", "access": "active_rss"},
    {"name": "Sky Sports", "type": "news_rss", "access": "active_rss"},
    {"name": "ESPN", "type": "news_rss_bridge", "access": "active_rss_bridge"},
    {"name": "The Athletic", "type": "news_rss", "access": "inactive"},
    {"name": "Reuters", "type": "news", "access": "needs_api_or_approved_feed"},
    {"name": "The Guardian", "type": "news_rss", "access": "active_rss"},
    {"name": "AP Sports", "type": "news", "access": "needs_api_or_approved_feed"},
    {"name": "GOAL", "type": "news", "access": "needs_verified_feed"},
    {"name": "FourFourTwo", "type": "news", "access": "needs_verified_feed"},
    {"name": "90min", "type": "news", "access": "needs_verified_feed"},
    {"name": "Fabrizio Romano", "type": "transfer_reporter", "access": "needs_approved_feed"},
    {"name": "Gianluca Di Marzio", "type": "transfer_reporter", "access": "needs_verified_feed"},
    {"name": "MARCA", "type": "news", "access": "needs_verified_feed"},
    {"name": "AS", "type": "news", "access": "needs_verified_feed"},
    {"name": "Mundo Deportivo", "type": "news", "access": "needs_verified_feed"},
    {"name": "La Gazzetta dello Sport", "type": "news", "access": "needs_verified_feed"},
    {"name": "kicker", "type": "news", "access": "needs_verified_feed"},
    {"name": "L'Équipe", "type": "news_rss", "access": "active_rss"},
    {"name": "Transfermarkt", "type": "transfer_data", "access": "needs_approved_data_access"},
    {"name": "FBref", "type": "statistics", "access": "needs_permitted_data_access"},
    {"name": "Sofascore", "type": "live_scores_statistics", "access": "needs_api_or_approved_feed"},
    {"name": "FotMob", "type": "live_scores_statistics", "access": "needs_api_or_approved_feed"},
    {"name": "Understat", "type": "expected_goals_statistics", "access": "needs_permitted_data_access"},
    {"name": "11v11", "type": "historical_statistics", "access": "needs_permitted_data_access"},
    {"name": "RSSSF", "type": "historical_statistics", "access": "needs_permitted_data_access"},
    {"name": "WorldFootball.net", "type": "historical_statistics", "access": "needs_permitted_data_access"},
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
    candidates = []
    def add(url, width=0, height=0, priority=0):
        if isinstance(url, str) and url.strip().startswith(("https://", "http://")):
            candidates.append((int(width or 0)*int(height or 0), priority, url.strip()))
    for item in getattr(entry, "media_content", []) or []:
        add(item.get("url"), item.get("width"), item.get("height"), 5)
    for item in getattr(entry, "media_thumbnail", []) or []:
        add(item.get("url"), item.get("width"), item.get("height"), 1)
    for item in getattr(entry, "enclosures", []) or []:
        if "image" in str(item.get("type", "")).lower():
            add(item.get("href", item.get("url")), item.get("width"), item.get("height"), 4)
    image = getattr(entry, "image", None)
    if image:
        add(image.get("href", image.get("url")), priority=3)
    for field in ("content", "summary", "description", "summary_detail"):
        value = getattr(entry, field, "")
        values = [x.get("value", "") for x in value] if isinstance(value, list) else [value.get("value", "") if isinstance(value, dict) else value]
        for text in values:
            if not isinstance(text, str):
                continue
            for tag in re.findall("<img[^>]*>", text, re.I):
                match = re.search(r"src=[\"\x27]([^\"\x27]+)", tag, re.I)
                if match:
                    w = re.search(r"width=[\"\x27](\d+)", tag, re.I)
                    h = re.search(r"height=[\"\x27](\d+)", tag, re.I)
                    add(match.group(1), int(w.group(1)) if w else 0, int(h.group(1)) if h else 0, 3)
    candidates.sort(reverse=True)
    for pixels, priority, url in candidates:
        if pixels == 0 or pixels >= 60000:
            return url
    return None

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
                        **(
                        {
                            "max_completion_tokens": 1024,
                            "reasoning_effort": "low",
                        }
                        if model.startswith("openai/gpt-oss-")
                        else {"max_tokens": 220}
                    ),
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


def is_football_article(title, description=""):
    text = (title + " " + description).casefold()
    nonfootball = [
        "nba ", "mlb ", "world series", "formula 1", "formula one",
        "f1 academy", "nfl ", "nfl:", "college football",
        "american football", "cricket", "rugby", "tennis",
        "basketball", "baseball", "ice hockey", "golf tournament"
    ]
    return not any(term in text for term in nonfootball)


def collect_articles(seen):
    """Collect articles while suppressing duplicate URLs and headlines."""
    articles = []
    seen_titles = set()

    def title_key(title):
        return " ".join(re.findall(r"\w+", title.casefold()))

    for source, feed_url in RSS_FEEDS:
        try:
            response = session.get(feed_url, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            for entry in feed.entries:
                title = clean_text(entry.get("title", ""))
                url = normalize_url(entry.get("link", ""))
                description = clean_text(
                    entry.get("summary", "") or entry.get("description", "")
                )
                key = title_key(title)

                if not is_football_article(title, description):
                    logging.info("Skipped non-football article: %s", title)
                    continue

                if not title or not url or url in seen or not key:
                    continue
                if key in seen_titles:
                    logging.info("Skipped duplicate headline: %s", title)
                    continue

                seen_titles.add(key)
                articles.append({
                    "source": source,
                    "title": title,
                    "url": url,
                    "description": description,
                    "image": extract_image(entry),
                    "date": get_date(entry),
                })
        except Exception:
            logging.exception("Could not collect feed from %s.", source)

    articles.sort(key=lambda article: article["date"], reverse=True)
    return articles



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

            try:
                result = response.json()
            except ValueError:
                result = {}

            if not response.ok or not result.get("ok"):
                description = result.get(
                    "description", response.text[:500]
                )
                raise RuntimeError(
                    f"Telegram API error {response.status_code}: "
                    f"{description}"
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



# FOOTBALL DATA API INTEGRATION
API_STATE_FILE = Path("seen_football_events.json")

def api_get(url, token, headers=None, params=None):
    try:
        h = dict(headers or {})
        if token and not h:
            h["Authorization"] = token
        r = session.get(url, headers=h, params=params or {}, timeout=20)
        if r.status_code in (401, 403, 429):
            logging.warning("Football API returned HTTP %s for %s", r.status_code, url.split("?")[0])
            return {}
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError):
        logging.warning("Football API request failed: %s", url.split("?")[0])
        return {}

def run_football_apis():
    try:
        history = json.loads(API_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(history, dict): history = {}
    except (OSError, ValueError):
        history = {}
    items = []

    # Sportmonks: in-play fixtures, scores and events.
    token = os.environ.get("SPORTMONKS_API_TOKEN", "").strip()
    if token:
        data = api_get("https://api.sportmonks.com/v3/football/livescores/inplay", token,
                       params={"include": "participants;scores;events;state;league;xGFixture"})
        major_league_names = (
            "premier league", "la liga", "serie a", "bundesliga", "ligue 1",
            "champions league", "europa league", "conference league",
            "world cup", "european championship", "copa america",
            "mls", "eredivisie", "primeira liga", "fa cup", "copa del rey"
        )
        for f in data.get("data", []) if isinstance(data.get("data", []), list) else []:
            league = f.get("league") or {}
            league_name = str(league.get("name", "")).casefold()
            if not any(name in league_name for name in major_league_names):
                continue
            teams = f.get("participants") or []
            home, away = "Home", "Away"
            for team in teams:
                loc = str((team.get("meta") or {}).get("location", "")).lower()
                if loc == "home": home = team.get("name", home)
                elif loc == "away": away = team.get("name", away)
            state = (f.get("state") or {}).get("name", "In play")
            score_bits = []
            for sc in f.get("scores") or []:
                if isinstance(sc.get("score"), dict):
                    score_bits.append(str(sc["score"].get("goals", "")))
            score = " - ".join(score_bits[:2]) if score_bits else "score pending"
            ev = []
            for e in (f.get("events") or [])[-6:]:
                typ = (e.get("type") or {}).get("name", "Event")
                minute = e.get("minute")
                player = e.get("player_name", "")
                ev.append(f"{minute or ''}' {typ}" + (f": {player}" if player else ""))
            xg = f.get("xGFixture")
            body = f"Status: {state}. Score: {score}."
            if ev: body += "\n" + "\n".join(ev)
            if xg: body += "\nExpected-goals data supplied by Sportmonks."
            items.append((f"sm:{f.get('id')}", f"🔴 LIVE | {home} vs {away}", body, "Sportmonks"))

    # API-Football: live scores plus recent event changes.
    key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if key:
        headers = {"x-apisports-key": key}
        data = api_get("https://v3.football.api-sports.io/fixtures", "", headers=headers, params={"live": "all"})
        major_leagues = {
            2, 3, 848, 1, 4, 9, 39, 140, 135, 78, 61,
            45, 143, 88, 94, 253, 71, 128, 262, 307
        }
        for f in data.get("response", []) if isinstance(data.get("response", []), list) else []:
            league = f.get("league") or {}
            if league.get("id") not in major_leagues:
                continue
            fx, teams, goals = f.get("fixture") or {}, f.get("teams") or {}, f.get("goals") or {}
            mid = fx.get("id")
            home = (teams.get("home") or {}).get("name", "Home")
            away = (teams.get("away") or {}).get("name", "Away")
            minute = (fx.get("status") or {}).get("elapsed")
            title = f"🔴 LIVE | {home} {goals.get('home', '?')}-{goals.get('away', '?')} {away}"
            if minute is not None: title += f" ({minute}')"
            ed = api_get("https://v3.football.api-sports.io/fixtures/events", "", headers=headers, params={"fixture": mid})
            ev = []
            for e in (ed.get("response") or [])[-6:]:
                tm = e.get("time") or {}
                player = (e.get("player") or {}).get("name", "")
                ev.append(f"{tm.get('elapsed', '')}' {e.get('detail', 'Event')}" + (f": {player}" if player else ""))
            items.append((f"af:{mid}", title, "\n".join(ev) or "Score and match status update; no event details returned yet.", "API-Football"))

    # football-data.org: recent finished matches and supported live statuses.
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
    if token:
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc).date()
        data = api_get("https://api.football-data.org/v4/matches", "", headers={"X-Auth-Token": token},
                       params={"dateFrom": (today-timedelta(days=2)).isoformat(), "dateTo": today.isoformat()})
        for m in data.get("matches", []) if isinstance(data.get("matches", []), list) else []:
            status = m.get("status", "")
            if status not in ("FINISHED", "IN_PLAY", "PAUSED", "LIVE"): continue
            score = (m.get("score") or {}).get("fullTime") or {}
            h = (m.get("homeTeam") or {}).get("name", "Home")
            a = (m.get("awayTeam") or {}).get("name", "Away")
            title = f"{'🔴 LIVE | ' if status in ('IN_PLAY','PAUSED','LIVE') else ''}{h} {score.get('home','?')}-{score.get('away','?')} {a}"
            items.append((f"fd:{m.get('id')}:{status}:{score.get('home')}:{score.get('away')}", title,
                          f"Status: {status.replace('_',' ').title()}.", "football-data.org"))

    for ident, title, body, source in items:
        fingerprint = re.sub(r"\s+", " ", title + " " + body).strip()
        if history.get(ident) == fingerprint: continue
        caption = ("⚽ TREQUARTISTA | FOOTBALL INTELLIGENCE\n\n<b>" + html.escape(title) +
                   "</b>\n\n" + html.escape(body[:2200]) + "\n\n📊 Data: " +
                   html.escape(source) + "\n<i>BETWEEN THE LINES.</i>")
        try:
            telegram_request("sendMessage", {"chat_id": CHANNEL_ID, "text": caption[:3900],
                                             "parse_mode": "HTML", "disable_web_page_preview": "true"})
            history[ident] = fingerprint
        except Exception:
            logging.exception("Could not publish football API update.")
    if len(history) > 3000: history = dict(list(history.items())[-2500:])
    API_STATE_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# MAIN
# ============================================================

def main():
    try:
        run_football_apis()
    except Exception:
        logging.exception("Football API integration failed; continuing news collection.")
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
