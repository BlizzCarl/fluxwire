"""Новостной бот: собирает свежие статьи из RSS, отсеивает одинаковые сюжеты
и публикует их в Telegram-канал.

Запуск:
    python bot.py                — один пост (следующая категория по кругу): для GitHub Actions / cron
    python bot.py games          — один пост из конкретной категории (ключ из feeds.py)
    python bot.py --loop         — работать постоянно, пост раз в INTERVAL_MIN минут (сервер, Docker)
    python bot.py --check-feeds  — проверить, какие RSS-ленты работают
    python bot.py --check-dupes  — показать, какие свежие новости бот считает одинаковыми
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup

import dedup
from feeds import CATEGORIES, SKIP_TITLE, SKIP_URL

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
TRANSLATE_TO = os.getenv("TRANSLATE_TO", "ru").strip()  # пусто — не переводить
FULL_TEXT = os.getenv("FULL_TEXT", "1") == "1"  # полный текст в посте / в Telegraph
SHORT_ARTICLE_CHARS = int(os.getenv("SHORT_ARTICLE_CHARS", "3500"))  # короче — целиком в посте
LEAD_CHARS = int(os.getenv("LEAD_CHARS", "1500"))  # сколько текста длинной статьи показать в посте
POSTS_PER_RUN = int(os.getenv("POSTS_PER_RUN", "1"))
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "24"))  # старее — не публикуем
DEDUP_HOURS = int(os.getenv("DEDUP_HOURS", "48"))  # сколько помнить сюжеты для отсева дублей
INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "10"))
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("state.json")))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ---------- состояние (что уже опубликовано) ----------

def load_state():
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    state.setdefault("posted", [])   # ключи ссылок, которые уже видели
    state.setdefault("recent", [])   # недавние сюжеты для поиска дублей
    state.setdefault("next_category", 0)
    return state


def save_state(state):
    border = time.time() - DEDUP_HOURS * 3600
    state["recent"] = [r for r in state["recent"] if r["ts"] > border][-2000:]
    state["posted"] = state["posted"][-5000:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), "utf-8")


def link_key(url):
    # Одна и та же статья может прийти с разными utm-метками
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.startswith("utm_")])
    norm = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), query, ""))
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def mark_seen(state, seen, item):
    key = link_key(item["link"])
    if key not in seen:
        seen.add(key)
        state["posted"].append(key)


def remember(state, seen, members):
    now = time.time()
    for member in members:
        mark_seen(state, seen, member)
        state["recent"].append({"ts": now, "title": member["title"], "text": member["text"]})


# ---------- чтение источников ----------

def clean_text(raw):
    text = BeautifulSoup(raw or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def skip_entry(link, title, extra_skip=()):
    link, title = link.lower(), title.lower()
    return any(p in link for p in [*SKIP_URL, *extra_skip]) or any(w in title for w in SKIP_TITLE)


def entry_image(entry):
    for media in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        url = media.get("url", "")
        if url.startswith("http") and media.get("medium", "image") == "image" \
                and not media.get("type", "image/").startswith("video"):
            return url
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image"):
            return link.get("href")
    img = BeautifulSoup(entry.get("summary", ""), "html.parser").find("img")
    if img and img.get("src", "").startswith("http"):
        return img["src"]
    return None


def rss_paragraphs(entry):
    """Полный текст, если лента отдаёт его сама (как Politico)."""
    if not entry.get("content"):
        return []
    soup = BeautifulSoup(entry.content[0].get("value", ""), "html.parser")
    paragraphs = [re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
                  for tag in soup.find_all(["p", "h2", "h3", "li"])]
    return [p for p in paragraphs if len(p) > 1]


def fetch_entries(category):
    items = []
    for source in category["feeds"]:
        try:
            resp = requests.get(source["url"], headers=HEADERS, timeout=20)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"[!] {source['name']}: {exc}")
            continue
        for entry in feed.entries[:20]:
            link, title = entry.get("link"), clean_text(entry.get("title"))
            if not link or not title or skip_entry(link, title, category.get("skip_url", [])):
                continue
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            summary = clean_text(entry.get("summary"))
            items.append({
                "source": source["name"],
                "lang": source.get("lang"),
                "title": title,
                "link": link,
                "summary": summary,
                "image": entry_image(entry),
                "date": datetime(*ts[:6], tzinfo=timezone.utc) if ts else None,
                "rss_paragraphs": rss_paragraphs(entry),
                "text": dedup.story_text(title, summary),
            })
    return items


def fetch_article(url):
    """Скачивает страницу статьи: основной текст, картинку и описание."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[!] статья недоступна: {exc}")
        return {}
    page = resp.text
    soup = BeautifulSoup(page, "html.parser")

    def meta(name):
        tag = soup.find("meta", property=name) or soup.find("meta", attrs={"name": name})
        return tag.get("content") if tag else None

    text = trafilatura.extract(page, url=url, include_comments=False,
                               include_tables=False, favor_precision=True) or ""
    return {
        "paragraphs": [p.strip() for p in text.split("\n") if len(p.strip()) > 1],
        "image": meta("og:image"),
        "description": clean_text(meta("og:description")),
    }


# ---------- отбор: свежие, не дубли, важные первыми ----------

def pick_stories(category, state, seen, cutoff):
    items = [i for i in fetch_entries(category)
             if link_key(i["link"]) not in seen and (i["date"] is None or i["date"] > cutoff)]
    if not items:
        return []

    # 1. Такой сюжет уже публиковали (из любой категории и любого источника)
    recent = state["recent"]
    known = dedup.find_known([i["text"] for i in items], [r["text"] for r in recent])
    fresh = []
    for item, match in zip(items, known):
        if match is None:
            fresh.append(item)
        else:
            print(f"[дубль] {item['source']}: {item['title']}  ≈  {recent[match]['title']}")
            mark_seen(state, seen, item)

    # 2. Одинаковые сюжеты из разных источников — в один пост.
    #    Чем больше изданий пишет об одном и том же, тем выше новость в очереди.
    now = datetime.now(timezone.utc)
    stories = []
    for group in dedup.group_stories([i["text"] for i in fresh]):
        members = [fresh[k] for k in group]
        main = max(members, key=lambda i: (i["lang"] == TRANSLATE_TO, bool(i["image"]),
                                           len(i["summary"]), i["date"] or EPOCH))
        newest = max((i["date"] for i in members if i["date"]), default=None)
        age_hours = (now - newest).total_seconds() / 3600 if newest else 6
        sources = len({i["source"] for i in members})
        stories.append({"main": main, "members": members, "score": sources - age_hours / 6})
    stories.sort(key=lambda s: s["score"], reverse=True)
    return stories


# ---------- перевод ----------

class TranslationError(Exception):
    pass


def is_russian(text, share=0.5):
    letters = re.findall(r"[a-zа-яё]", text.lower())
    return not letters or sum(c >= "а" for c in letters) / len(letters) > share


def _translate_chunk(translator, text):
    for attempt in range(3):
        try:
            return translator.translate(text) or text
        except Exception as exc:
            if attempt == 2:
                raise TranslationError(exc)
            time.sleep(2 * (attempt + 1))


def translate(parts, lang):
    """Переводит список строк, сохраняя разбиение.
    Если перевести не удалось, бросает TranslationError: пост на чужом языке не выйдет,
    новость останется в очереди до следующего запуска."""
    if not TRANSLATE_TO or lang == TRANSLATE_TO or not parts:
        return parts
    if TRANSLATE_TO == "ru" and all(is_russian(p) for p in parts):
        return parts  # источник уже на русском, даже если это не отмечено в feeds.py
    from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source="auto", target=TRANSLATE_TO)

    # Абзацы склеиваются в куски до 4500 символов: меньше запросов, быстрее, без блокировок
    result, batch = [], []

    def flush():
        if not batch:
            return
        translated = _translate_chunk(translator, "\n".join(batch)).split("\n")
        if len(translated) == len(batch):
            result.extend(translated)
        else:  # переводчик изменил разбиение — переводим по одному абзацу
            result.extend(_translate_chunk(translator, p) for p in batch)
        batch.clear()

    for part in parts:
        part = part.replace("\n", " ")
        if not part.strip():
            flush()
            result.append(part)
            continue
        if len(part) > 4500:
            flush()
            result.append("".join(_translate_chunk(translator, part[i:i + 4500])
                                  for i in range(0, len(part), 4500)))
            continue
        if batch and len("\n".join(batch)) + len(part) > 4500:
            flush()
        batch.append(part)
    flush()

    # В переводе остаются латинские названия (iPhone, GTA), поэтому достаточно трети кириллицы
    if TRANSLATE_TO == "ru" and not is_russian(" ".join(result), share=0.3):
        raise TranslationError("перевод вернулся не на русском")
    return result


# ---------- Telegraph (полный текст внутри Telegram) ----------

def telegraph_token():
    token = os.getenv("TELEGRAPH_TOKEN", "").strip()
    if not token:
        # Временный аккаунт на этот запуск; лучше задать постоянный TELEGRAPH_TOKEN (см. README)
        resp = requests.post("https://api.telegra.ph/createAccount",
                             data={"short_name": "NewsBot", "author_name": "News"}, timeout=20).json()
        token = resp["result"]["access_token"]
        os.environ["TELEGRAPH_TOKEN"] = token
    return token


def telegraph_page(title, paragraphs, image, item):
    nodes = [{"tag": "img", "attrs": {"src": image}}] if image else []
    size = 0
    for paragraph in paragraphs:
        size += len(paragraph.encode()) + 40
        if size > 55000:  # лимит Telegraph — 64 КБ на страницу
            break
        nodes.append({"tag": "p", "children": [paragraph]})
    nodes.append({"tag": "p", "children": [
        {"tag": "a", "attrs": {"href": item["link"]}, "children": [f"Источник: {item['source']}"]}
    ]})
    try:
        resp = requests.post("https://api.telegra.ph/createPage", data={
            "access_token": telegraph_token(),
            "title": title[:256],
            "author_name": item["source"][:128],
            "author_url": item["link"],
            "content": json.dumps(nodes, ensure_ascii=False),
        }, timeout=30).json()
        if resp.get("ok"):
            return resp["result"]["url"]
        print(f"[!] Telegraph: {resp.get('error')}")
    except Exception as exc:
        print(f"[!] Telegraph: {exc}")
    return None


# ---------- публикация ----------

def tg(method, **params):
    params["chat_id"] = CHANNEL_ID
    data = requests.post(f"{TG_API}/{method}", data=params, timeout=30).json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description')}")
    return data["result"]


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:max(limit - 1, 0)].rsplit(" ", 1)[0].rstrip(",.;:—- ")
    return cut + "…"


def take_paragraphs(paragraphs, limit):
    """Первые целые абзацы, укладывающиеся в limit символов."""
    out, size = [], 0
    for paragraph in paragraphs:
        if out and size + len(paragraph) + 2 > limit:
            break
        out.append(paragraph)
        size += len(paragraph) + 2
    return "\n\n".join(out)


def build_post(category, title, body, source, limit, hot=(), footer_note=""):
    head = f"{category['emoji']} <b>{html.escape(title)}</b>\n\n"
    tail = f"{footer_note}\n\n"
    if hot:
        tail += f"🔥 <b>Об этом пишут:</b> {html.escape(', '.join(hot))}\n"
    tail += f"📰 <i>{html.escape(source)}</i>  ·  #{category['tag']}"
    room = limit - len(head) - len(tail) - 50  # запас на экранирование
    return head + html.escape(shorten(body, room)) + tail


def keyboard(first_row, extra_row):
    return json.dumps({"inline_keyboard": [first_row] + ([extra_row] if extra_row else [])})


def send_text(text, preview_url, markup):
    # Карточка ссылки над текстом: картинка статьи, у Telegraph — кнопка Instant View
    tg("sendMessage", parse_mode="HTML", reply_markup=markup, text=text,
       link_preview_options=json.dumps({"url": preview_url, "prefer_large_media": True,
                                        "show_above_text": True}))


def publish(category, story):
    item = story["main"]
    others = {}
    for member in story["members"]:
        if member["source"] != item["source"]:
            others.setdefault(member["source"], member)
    hot = [item["source"], *others] if others else []
    extra_row = [{"text": f"🔗 {m['source']}", "url": m["link"]} for m in list(others.values())[:3]]
    source_button = {"text": f"🔗 {item['source']}", "url": item["link"]}

    article = fetch_article(item["link"])
    paragraphs = article.get("paragraphs", [])
    if sum(map(len, item["rss_paragraphs"])) > sum(map(len, paragraphs)):
        paragraphs = item["rss_paragraphs"]
    if not FULL_TEXT:
        paragraphs = []
    image = item["image"] or article.get("image")

    summary = item["summary"]
    if len(summary) < 80:
        summary = article.get("description") or " ".join(paragraphs[:2]) or summary
    title, summary = translate([item["title"], summary], item["lang"])
    paragraphs = translate(paragraphs, item["lang"])
    full_text = "\n\n".join(paragraphs)

    # Короткая статья — целиком прямо в посте
    if paragraphs and len(full_text) <= SHORT_ARTICLE_CHARS:
        send_text(build_post(category, title, full_text, item["source"], 4096, hot),
                  item["link"], keyboard([source_button], extra_row))
        return

    # Длинная — начало в посте, полный текст в Telegraph (Instant View)
    if len(paragraphs) >= 3:
        page = telegraph_page(title, paragraphs, image, item)
        if page:
            lead = take_paragraphs(paragraphs, LEAD_CHARS)
            send_text(build_post(category, title, lead, item["source"], 4096, hot,
                                 footer_note="\n\n⚡ <b>Полная статья — в Instant View выше</b>"),
                      page, keyboard([{"text": "📖 Читать полностью", "url": page}, source_button],
                                     extra_row))
            return

    # Полный текст не удалось получить — фото и краткое содержание
    markup = keyboard([source_button], extra_row)
    if image:
        try:
            tg("sendPhoto", photo=image, parse_mode="HTML", reply_markup=markup,
               caption=build_post(category, title, summary, item["source"], 1024, hot))
            return
        except Exception as exc:
            print(f"[!] не удалось отправить с фото, отправляю текстом: {exc}")
    send_text(build_post(category, title, summary, item["source"], 1500, hot), item["link"], markup)


def post_category(category, state, seen, cutoff):
    sent, used_sources = 0, set()
    for story in pick_stories(category, state, seen, cutoff):
        if sent >= POSTS_PER_RUN:
            break
        main = story["main"]
        if main["source"] in used_sources:  # не больше одного поста с источника за запуск
            continue
        try:
            publish(category, story)
        except TranslationError as exc:
            print(f"[!] не удалось перевести, новость отложена до следующего запуска: {exc}")
            continue
        except Exception as exc:
            print(f"[!] {main['link']}: {exc}")
            continue
        remember(state, seen, story["members"])
        used_sources.add(main["source"])
        sent += 1
        extra = f" (+{len(story['members']) - 1} похожих)" if len(story["members"]) > 1 else ""
        print(f"[{category['key']}] опубликовано: {main['title']}{extra}")
    return sent


def run(only=None):
    if not BOT_TOKEN or not CHANNEL_ID:
        sys.exit("Задайте переменные окружения BOT_TOKEN и CHANNEL_ID (см. README)")
    state = load_state()
    seen = set(state["posted"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)

    if only:
        order = [c for c in CATEGORIES if c["key"] == only]
        if not order:
            sys.exit(f"Нет категории '{only}'. Доступны: {', '.join(c['key'] for c in CATEGORIES)}")
    else:
        start = state["next_category"] % len(CATEGORIES)
        order = CATEGORIES[start:] + CATEGORIES[:start]

    # Если в категории нет свежего, берём следующую, чтобы слот не пропадал
    for category in order:
        if not only:
            state["next_category"] = (CATEGORIES.index(category) + 1) % len(CATEGORIES)
        if post_category(category, state, seen, cutoff):
            break
        print(f"[{category['key']}] свежих новостей нет")
    save_state(state)


# ---------- проверки ----------

def check_feeds():
    for category in CATEGORIES:
        print(f"\n{category['emoji']} {category['title']}")
        for source in category["feeds"]:
            try:
                resp = requests.get(source["url"], headers=HEADERS, timeout=20)
                resp.raise_for_status()
                count = len(feedparser.parse(resp.content).entries)
                status = f"OK, статей: {count}" if count else "лента пустая"
            except Exception as exc:
                status = f"ОШИБКА: {exc}"
            print(f"  {source['name']:<26} {status}")


def check_dupes():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    items = [i for c in CATEGORIES for i in fetch_entries(c) if i["date"] is None or i["date"] > cutoff]
    groups = [g for g in dedup.group_stories([i["text"] for i in items])
              if len({items[k]["source"] for k in g}) > 1]
    print(f"Свежих новостей: {len(items)}. Сюжетов, о которых пишут несколько изданий: {len(groups)}\n")
    for group in groups:
        for k in group:
            print(f"  {items[k]['source']}: {items[k]['title']}")
        print()


if __name__ == "__main__":
    if "--check-feeds" in sys.argv:
        check_feeds()
    elif "--check-dupes" in sys.argv:
        check_dupes()
    elif "--loop" in sys.argv:
        while True:
            started = time.time()
            try:
                run()
            except Exception as exc:
                print(f"[!] ошибка запуска: {exc}")
            time.sleep(max(30, INTERVAL_MIN * 60 - (time.time() - started)))
    else:
        run(sys.argv[1] if len(sys.argv) > 1 else None)
