"""Признаки популярности: что люди прямо сейчас ищут, читают и обсуждают.

Все источники необязательные: если какой-то недоступен, бот просто его не учитывает.
  Google Trends      — что ищут прямо сейчас, вместе со статьями по каждому запросу (без ключа)
  Hacker News        — главная страница с баллами, сильный сигнал для технологий и науки (без ключа)
  NYT Most Popular   — самые просматриваемые статьи New York Times (бесплатный NYT_API_KEY)
  The Guardian       — самые читаемые статьи Guardian (бесплатный GUARDIAN_API_KEY)
"""
import html
import os
import re

import requests

TRENDS_GEOS = [g.strip() for g in os.getenv("TRENDS_GEOS", "US,GB").split(",") if g.strip()]
NYT_API_KEY = os.getenv("NYT_API_KEY", "").strip()
GUARDIAN_API_KEY = os.getenv("GUARDIAN_API_KEY", "").strip()
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def _get(url, **params):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp


def _tags(block, name):
    values = re.findall(rf"<{name}>(.*?)</{name}>", block, re.S)
    return [html.unescape(re.sub(r"^<!\[CDATA\[|\]\]>$", "", v.strip())).strip() for v in values]


def google_trends():
    out = []
    for geo in TRENDS_GEOS:
        try:
            raw = _get("https://trends.google.com/trending/rss", geo=geo).text
        except Exception as exc:
            print(f"[!] Google Trends {geo}: {exc}")
            continue
        for block in re.findall(r"<item>(.*?)</item>", raw, re.S):
            query = (_tags(block, "title") or [""])[0]
            traffic = int(re.sub(r"\D", "", (_tags(block, "ht:approx_traffic") or ["0"])[0]) or 0)
            for title, url in zip(_tags(block, "ht:news_item_title"), _tags(block, "ht:news_item_url")):
                out.append({"kind": "trends", "title": title, "url": url, "weight": traffic, "label": query})
    return out


def hacker_news():
    try:
        hits = _get("https://hn.algolia.com/api/v1/search", tags="front_page", hitsPerPage=50).json()["hits"]
    except Exception as exc:
        print(f"[!] Hacker News: {exc}")
        return []
    return [{"kind": "hn", "title": h["title"], "url": h.get("url") or "", "weight": h.get("points") or 0}
            for h in hits if h.get("title")]


def nyt_most_viewed():
    if not NYT_API_KEY:
        return []
    try:
        results = _get("https://api.nytimes.com/svc/mostpopular/v2/viewed/1.json",
                       **{"api-key": NYT_API_KEY}).json()["results"]
    except Exception as exc:
        print(f"[!] NYT Most Popular: {exc}")
        return []
    return [{"kind": "top", "title": r.get("title", ""), "url": r.get("url", ""), "weight": 1}
            for r in results if r.get("title")]


def guardian_most_viewed():
    if not GUARDIAN_API_KEY:
        return []
    out = []
    for section in ("world", "business", "science", "technology", "games", "film", "sport"):
        try:
            data = _get(f"https://content.guardianapis.com/{section}",
                        **{"show-most-viewed": "true", "api-key": GUARDIAN_API_KEY}).json()["response"]
        except Exception as exc:
            print(f"[!] Guardian most viewed ({section}): {exc}")
            continue
        out += [{"kind": "top", "title": r.get("webTitle", ""), "url": r.get("webUrl", ""), "weight": 1}
                for r in data.get("mostViewed", []) if r.get("webTitle")]
    return out


def collect():
    signals = google_trends() + hacker_news() + nyt_most_viewed() + guardian_most_viewed()
    kinds = {}
    for s in signals:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    print(f"Сигналы популярности: {kinds or 'нет'}")
    return signals


def bonus(signal):
    """Сколько баллов добавляет совпадение с сигналом."""
    if signal["kind"] == "trends":
        return 4 if signal["weight"] >= 20000 else 3
    if signal["kind"] == "hn":
        return 4 if signal["weight"] >= 300 else 2 if signal["weight"] >= 100 else 1
    return 3  # в списке самых читаемых
