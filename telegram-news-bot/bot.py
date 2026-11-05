"""Новостной бот: собирает свежие статьи из RSS, отсеивает одинаковые сюжеты
и публикует в Telegram-канал только важное.

Как выбирается новость:
  1. Все статьи попадают в очередь «созревания». Одинаковые сюжеты из разных изданий
     склеиваются в один. Статьи без даты публикации отбрасываются.
  2. Gemini оценивает важность каждого сюжета от 1 до 10, как выпускающий редактор.
  3. Каждый запуск бот проверяет популярность: сколько изданий подхватило сюжет, есть ли он
     в Google Trends, на главной Hacker News, в топах самых читаемых NYT и Guardian.
  4. Новость выходит, когда «созрела» (прошло HOLD_MINUTES с публикации) и набрала MIN_SCORE.
     Срочное (BREAKING_SCORE) выходит сразу. Не набравшее баллов за POOL_HOURS — выбрасывается.
  5. Рубрики идут по кругу; если в рубрике нет важного, берётся важное из следующей.
  6. Раз в день в DIGEST_HOUR выходит дайджест: заголовки всех новостей с прошлого дайджеста.
  7. Если что-то сломалось, бот пишет об этом в личку (ADMIN_CHAT_ID).
  8. Новые факты об уже опубликованном сюжете выходят «🔄 Обновлением» — ответом на старый пост.
  9. Срочное (URGENT_SCORE) приходит с пометкой ⚡️ и звуком, остальное — без звука.
 10. В закрепе — только свежий дайджест.

Запуск:
    python bot.py                — один пост: для GitHub Actions / cron
    python bot.py games          — один пост только из этой категории (ключ из feeds.py)
    python bot.py --loop         — работать постоянно, пост раз в INTERVAL_MIN минут (сервер, Docker)
    python bot.py --digest       — опубликовать дайджест прямо сейчас (проверка, вечерний не сбивает)
    python bot.py --test-alert   — прислать пробное предупреждение в личку
    python bot.py --rank         — показать очередь с оценками, ничего не публикуя
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import feedparser
import numpy as np
import requests
import trafilatura
from bs4 import BeautifulSoup

import dedup
import popularity
from feeds import CATEGORIES, SKIP_TEXT, SKIP_TITLE, SKIP_URL

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()  # ваш личный чат: сюда приходят предупреждения
TRANSLATE_TO = os.getenv("TRANSLATE_TO", "ru").strip()  # пусто — не переводить
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()  # переводчик и редактор-нейросеть
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()  # оценка важности, повторы
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.8-flash").strip()  # текст поста и перевод
FULL_TEXT = os.getenv("FULL_TEXT", "1") == "1"  # полный перевод статьи в Telegraph по кнопке
MAX_ARTICLE_CHARS = int(os.getenv("MAX_ARTICLE_CHARS", "15000"))  # длиннее — в Telegraph идёт начало
TRANSLATE_CHUNK = int(os.getenv("TRANSLATE_CHUNK", "7000"))  # статья переводится кусками: меньше ошибок
MAX_VIDEOS = int(os.getenv("MAX_VIDEOS", "3"))  # сколько видео из статьи встраивать в Telegraph
VIDEO_HEADER = os.getenv("VIDEO_HEADER", "1") == "1"  # видео из статьи — в шапку поста вместо картинки
MAX_VIDEO_MB = int(os.getenv("MAX_VIDEO_MB", "45"))  # больше бот загрузить в Telegram не может (лимит 50 МБ)
MIN_IMAGE_WIDTH = int(os.getenv("MIN_IMAGE_WIDTH", "600"))  # миниатюры уже этого из ленты не берём
MIN_IMAGE_BYTES = int(os.getenv("MIN_IMAGE_BYTES", "30000"))  # картинка легче — скорее всего мутная
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "12"))  # старее — не берём в очередь
POOL_HOURS = int(os.getenv("POOL_HOURS", "14"))  # сколько сюжет ждёт в очереди, потом выбрасывается
HOLD_MINUTES = int(os.getenv("HOLD_MINUTES", "90"))  # «созревание»: столько ждём перепечаток и топов
MIN_SCORE = float(os.getenv("MIN_SCORE", "9"))  # порог для рубрик, о которых ещё нет данных
# Порог публикации считается для каждой рубрики сам, по её собственной истории: проходит
# доля AUTO_SHARE лучших сюжетов рубрики за AUTO_DAYS дней. В политике перепечаток много и
# баллы выше — порог у неё выше; в играх и науке ниже. Так у каждой рубрики равные шансы:
# сильная игровая новость соревнуется с сильной политической, а не со средней.
AUTO_SHARE = float(os.getenv("AUTO_SHARE", "0.12"))  # какая доля лучших сюжетов рубрики проходит
AUTO_DAYS = float(os.getenv("AUTO_DAYS", "3"))  # за сколько дней считать
AUTO_MIN_STORIES = int(os.getenv("AUTO_MIN_STORIES", "30"))  # меньше сюжетов — стартовый порог ниже
AUTO_FLOOR = float(os.getenv("AUTO_FLOOR", "6"))  # ниже этого порог не опускается: мелочь не пройдёт
AUTO_CEIL = float(os.getenv("AUTO_CEIL", "12"))  # выше не поднимается
# Стартовые пороги, пока у рубрики не накопилось AUTO_MIN_STORIES сюжетов (обычно первые сутки).
# Поменять одну рубрику вручную, без автоподбора: MIN_SCORE_GAMES: "6.5" и т. п. в news.yml
CATEGORY_MIN_SCORE = {"world": 9, "tech": 8, "ai": 8, "science": 8, "games": 8, "movies": 8, "sport": 8}
RATING_V = 2  # версия шкалы оценок: при смене старые оценки пересчитываются
BREAKING_SCORE = float(os.getenv("BREAKING_SCORE", "13"))  # с такой оценкой — сразу, без ожидания
URGENT_SCORE = float(os.getenv("URGENT_SCORE", "11"))  # с такой оценкой — пометка ⚡️ и звук уведомления
SILENT_POSTS = os.getenv("SILENT_POSTS", "1") == "1"  # обычные посты приходят подписчикам без звука
UPDATES = os.getenv("UPDATES", "1") == "1"  # новые факты о вышедшем сюжете — ответом на старый пост
UPDATE_MAX = int(os.getenv("UPDATE_MAX", "3"))  # не больше стольких обновлений на один сюжет
UPDATE_GAP_MIN = int(os.getenv("UPDATE_GAP_MIN", "60"))  # и не чаще раза в столько минут
NAVIGATION = os.getenv("NAVIGATION", "0") == "1"  # закреплённое сообщение с рубриками (выключено)
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "FLUX WIRE").strip()  # название канала в навигации
DEDUP_HOURS = int(os.getenv("DEDUP_HOURS", "48"))  # сколько помнить сюжеты для отсева дублей
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "21"))  # час вечернего дайджеста; -1 — выключить
DIGEST_TZ = os.getenv("DIGEST_TZ", "Europe/Moscow").strip()  # часовой пояс дайджеста
ALERT_HOURS = float(os.getenv("ALERT_HOURS", "6"))  # об одной и той же поломке — не чаще раза в N часов
QUIET_HOURS = float(os.getenv("QUIET_HOURS", "8"))  # столько часов без постов — повод предупредить
FEED_ALERT_HOURS = float(os.getenv("FEED_ALERT_HOURS", "3"))  # лента молчит столько часов — сказать
INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "15"))  # как часто запускается бот (cron-job.org — раз в 15 минут)
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("state.json")))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
EPOCH = datetime.min.replace(tzinfo=timezone.utc)
# Ошибки Telegram, при которых публиковать бесполезно, пока не исправить настройки
FATAL_TG_ERRORS = ("administrator rights", "chat not found", "Unauthorized", "bot was kicked",
                   "not enough rights")
CATEGORY_BY_KEY = {c["key"]: c for c in CATEGORIES}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]


# ---------- состояние (что уже опубликовано и что ждёт в очереди) ----------

def load_state():
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    state.setdefault("posted", [])   # ключи ссылок, которые уже видели
    state.setdefault("recent", [])   # недавние опубликованные сюжеты для поиска дублей
    state.setdefault("pool", [])     # очередь «созревания»
    state.setdefault("digest", [])   # опубликованное с прошлого дайджеста
    state.setdefault("alerts", {})   # когда о какой поломке уже писали
    state.setdefault("feed_fails", {})
    state.setdefault("threads", {})  # сколько обновлений вышло к каждому посту
    state.setdefault("history", {})  # лучшие баллы ушедших из очереди сюжетов, по рубрикам
    state.setdefault("last_post", time.time())
    state.setdefault("next_category", 0)
    # Сюжеты из удалённых рубрик (например, «Бизнес») больше не публикуем
    state["pool"] = [s for s in state["pool"] if s["category"] in CATEGORY_BY_KEY]
    # Оценки по старой шкале (одна мировая) — заново, по двум шкалам; история баллов — с нуля
    for story in state["pool"]:
        if story.get("rating_v") != RATING_V:
            story.update(importance=None, world=None, rate_tries=0, rating_v=RATING_V)
            story.pop("peak", None)
    if state.get("rating_v") != RATING_V:
        state["history"], state["rating_v"] = {}, RATING_V
    return state


def save_state(state):
    border = time.time() - DEDUP_HOURS * 3600
    state["recent"] = [r for r in state["recent"] if r["ts"] > border][-2000:]
    state["threads"] = {k: v for k, v in state["threads"].items() if v["ts"] > border}
    since = time.time() - AUTO_DAYS * 86400
    state["history"] = {k: [h for h in v if h[0] > since][-1500:] for k, v in state["history"].items()}
    state["posted"] = state["posted"][-8000:]
    state["digest"] = [d for d in state["digest"] if d["ts"] > time.time() - 36 * 3600][-300:]
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


def remember(state, seen, members, msg=None):
    """msg — номер поста в канале об этом сюжете: к нему будут приходить обновления."""
    now = time.time()
    for member in members:
        mark_seen(state, seen, member)
        state["recent"].append({"ts": now, "title": member["title"], "text": member["text"], "msg": msg})


def pack(item):
    data = dict(item)
    data["date"] = item["date"].timestamp() if item["date"] else None
    return data


def unpack(data):
    item = dict(data)
    item["date"] = datetime.fromtimestamp(data["date"], timezone.utc) if data.get("date") else None
    return item


# ---------- предупреждения в личку ----------

PROBLEMS = {}  # что сломалось за этот запуск: ключ → текст


def problem(key, text):
    print(f"[!] {text}")
    PROBLEMS.setdefault(key, text)


def send_alerts(state):
    """Отправляет в личку новые поломки. Об одной и той же — не чаще раза в ALERT_HOURS."""
    found = dict(PROBLEMS)
    PROBLEMS.clear()
    if not found or not ADMIN_CHAT_ID or not BOT_TOKEN:
        return
    now = time.time()
    sent = state.setdefault("alerts", {})
    for key in [k for k, ts in sent.items() if now - ts > 7 * 24 * 3600]:
        del sent[key]
    fresh = [(k, t) for k, t in found.items() if now - sent.get(k, 0) >= ALERT_HOURS * 3600]
    if not fresh:
        return
    text = "⚠️ <b>Новостной бот: нужна проверка</b>\n\n" + "\n\n".join(
        f"• {html.escape(t)}" for _, t in fresh)
    try:
        tg("sendMessage", chat_id=ADMIN_CHAT_ID, parse_mode="HTML", text=text[:4000])
        for key, _ in fresh:
            sent[key] = now
    except Exception as exc:
        print(f"[!] не удалось отправить предупреждение в личку: {exc}\n"
              f"    Напишите своему боту /start и проверьте ADMIN_CHAT_ID")


def report_crash(exc):
    problem("crash", f"Запуск упал с ошибкой: {exc}. Подробности — в журнале запуска на GitHub (Actions).")
    try:
        state = load_state()
        send_alerts(state)
        save_state(state)
    except Exception as inner:
        print(f"[!] не удалось сообщить о сбое: {inner}")


# ---------- чтение источников ----------

def clean_text(raw):
    text = BeautifulSoup(raw or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def skip_entry(link, title, extra_skip=()):
    link, title = link.lower(), title.lower()
    return any(p in link for p in [*SKIP_URL, *extra_skip]) or any(w in title for w in SKIP_TITLE)


def _width(value):
    value = str(value or "")
    return int(value) if value.isdigit() else 0


def entry_image(entry):
    """Самая большая картинка из ленты: (адрес, ширина; 0 — неизвестна)."""
    best = (None, -1)
    for media in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        url = media.get("url", "")
        if url.startswith("http") and media.get("medium", "image") == "image" \
                and not media.get("type", "image/").startswith("video"):
            width = _width(media.get("width"))
            if width > best[1]:
                best = (url, width)
    if best[0]:
        return best
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image"):
            return link.get("href"), 0
    img = BeautifulSoup(entry.get("summary", ""), "html.parser").find("img")
    if img and img.get("src", "").startswith("http"):
        return img["src"], _width(img.get("width"))
    return None, 0


def rss_paragraphs(entry):
    """Полный текст, если лента отдаёт его сама (как Politico)."""
    if not entry.get("content"):
        return []
    soup = BeautifulSoup(entry.content[0].get("value", ""), "html.parser")
    paragraphs = [re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
                  for tag in soup.find_all(["p", "h2", "h3", "li"])]
    return [p for p in paragraphs if len(p) > 1]


# ---------- видео из статей ----------

YOUTUBE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/|live/|v/)|youtu\.be/)([\w-]{11})")
YOUTUBE_ATTR = re.compile(r"""(?:videoid|data-youtube-id)=["']([\w-]{11})["']""")
VIMEO = re.compile(r"player\.vimeo\.com/video/(\d+)")


def find_videos(markup, extra_files=()):
    """Видео в HTML: YouTube и Vimeo встраиваются в Telegraph, файлы .mp4 — ссылкой."""
    videos = []
    for match in [*YOUTUBE.finditer(markup), *YOUTUBE_ATTR.finditer(markup)]:
        videos.append({"kind": "youtube", "url": f"https://www.youtube.com/watch?v={match.group(1)}"})
    for match in VIMEO.finditer(markup):
        videos.append({"kind": "vimeo", "url": f"https://vimeo.com/{match.group(1)}"})
    for url in extra_files:
        if url and url.startswith("http") and ".mp4" in url.lower():
            videos.append({"kind": "file", "url": url})
    return unique_videos(videos)


def unique_videos(videos):
    out, urls = [], set()
    for video in videos:
        if video["url"] not in urls:
            urls.add(video["url"])
            out.append(video)
    return out[:MAX_VIDEOS]


def entry_videos(entry):
    markup = " ".join([entry.get("summary", "")] + [c.get("value", "") for c in entry.get("content", [])])
    files = [m.get("url") for m in entry.get("media_content", [])
             if m.get("medium") == "video" or m.get("type", "").startswith("video")]
    return find_videos(markup, files)


def fetch_feed(category, source):
    """Статьи одной ленты и текст ошибки (None — лента ответила)."""
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:
        print(f"[!] {source['name']}: {exc}")
        return [], str(exc)[:150]
    items = []
    move = category.get("move")
    for entry in feed.entries[:20]:
        link, title = entry.get("link"), clean_text(entry.get("title"))
        if not link or not title or skip_entry(link, title, category.get("skip_url", [])):
            continue
        ts = entry.get("published_parsed") or entry.get("updated_parsed")
        if not ts:
            continue  # без даты не понять, свежая ли новость, — не берём
        summary = clean_text(entry.get("summary"))
        paragraphs = rss_paragraphs(entry)
        text = f"{summary} {' '.join(paragraphs[-3:])}".lower()
        if any(s in text for s in SKIP_TEXT):
            continue
        key = category["key"]
        if move and re.search(move["pattern"], title, re.I):
            key = move["to"]
        image, image_width = entry_image(entry)
        items.append({
            "category": key,
            "source": source["name"],
            "lang": source.get("lang"),
            "title": title,
            "link": link,
            "summary": summary,
            "image": image,
            "image_width": image_width,
            "videos": entry_videos(entry),
            "date": datetime(*ts[:6], tzinfo=timezone.utc),
            "rss_paragraphs": paragraphs,
            "text": dedup.story_text(title, summary),
        })
    return items, None


def fetch_entries(categories):
    """Все ленты читаются параллельно. Возвращает статьи и {лента: ошибка} для неответивших."""
    jobs = [(c, s) for c in categories for s in c["feeds"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda job: fetch_feed(*job), jobs))
    items = [item for found, _ in results for item in found]
    failures = {s["name"]: error for (_, s), (_, error) in zip(jobs, results) if error}
    return items, failures


def track_feeds(state, failures):
    """Считает, сколько запусков подряд лента не отвечает, и предупреждает, если долго."""
    fails = state["feed_fails"]
    limit = max(1, round(FEED_ALERT_HOURS * 60 / INTERVAL_MIN))
    for name in list(fails):
        if name not in failures:
            del fails[name]
    for name, error in failures.items():
        fails[name] = fails.get(name, 0) + 1
        if fails[name] >= limit:
            problem(f"feed:{name}", f"Лента «{name}» не отвечает уже {fails[name]} запусков подряд "
                                    f"(примерно {fails[name] * INTERVAL_MIN // 60} ч): {error}. "
                                    f"Если так и останется, её стоит заменить в feeds.py.")


def fetch_article(url):
    """Скачивает страницу статьи: основной текст, картинку, описание и видео."""
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
    # Видео ищем только в теле статьи, чтобы не захватить ролики из боковых колонок
    root = soup.find("article") or soup.find("main") or soup
    files = [meta("og:video"), meta("og:video:url"), meta("og:video:secure_url")]
    for video in root.find_all("video"):
        files.append(video.get("src"))
        files += [s.get("src") for s in video.find_all("source")]
    return {
        "paragraphs": [p.strip() for p in text.split("\n") if len(p.strip()) > 1],
        # Картинки для соцсетей у изданий обычно 1200 px и больше
        "images": [u for u in (meta("og:image"), meta("twitter:image"), meta("twitter:image:src"))
                   if u and u.startswith("http")],
        "description": clean_text(meta("og:description")),
        "videos": find_videos(str(root) + " " + " ".join(f for f in files if f), files),
    }


# ---------- Gemini ----------

class TranslationError(Exception):
    pass


class Overloaded(TranslationError):
    """Gemini перегружен (503) или исчерпан лимит (429): это временно."""


def gemini(prompt, schema, model=None, attempts=3, temperature=0.2):
    """Запрос к Gemini с JSON-ответом по схеме. Ключ бесплатный: https://aistudio.google.com/apikey"""
    model = model or GEMINI_MODEL
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            # Строгая схема, иначе модель иногда ломает JSON на длинных текстах
            "responseSchema": schema,
            "temperature": temperature,
            "maxOutputTokens": 65536,
        },
    }
    error, busy, wait = None, False, 0
    for attempt in range(attempts):
        if wait:
            time.sleep(wait)
        wait = 5 * (attempt + 1)
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=120,
            )
        except requests.RequestException as exc:
            error, busy = f"Gemini ({model}): {exc}", False
            continue
        if resp.status_code != 200:
            error = f"Gemini ({model}) {resp.status_code}: {resp.text[:300]}"
            busy = resp.status_code in (429, 503)
            if busy:
                wait = 20 * (attempt + 1)  # перегрузка проходит не за секунды
            if resp.status_code == 404:
                problem(f"gemini404:{model}", f"Модель Gemini «{model}» не найдена: похоже, Google снял её "
                                              f"с поддержки. Поменяйте её название в news.yml или bot.py.")
                break
            if resp.status_code in (400, 401, 403):  # ключ неверный — повтор не поможет
                problem("gemini-key", f"Gemini не принимает ключ или запрос ({resp.status_code}). "
                                      f"Проверьте секрет GEMINI_API_KEY.")
                break
            continue
        try:
            return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, ValueError) as exc:
            error, busy = f"Gemini ({model}) вернул испорченный ответ: {exc}", False
    raise (Overloaded if busy else TranslationError)(error)


# Слово, где перемешаны кириллица и латиница («мaшина»), — верный признак сбоя нейросети
MIXED_WORD = re.compile(r"\b(?=\w*[а-яё])(?=\w*[a-z])\w+\b", re.I)


def garbled(text):
    return bool(MIXED_WORD.search(text))


# ---------- оценка важности ----------

RATE_PROMPT = """Ты — выпускающий редактор международного новостного Telegram-канала \
(политика и мир, наука и медицина, технологии, искусственный интеллект, игры, кино и сериалы, \
спорт: футбол, бокс, теннис). Для каждой новости поставь две оценки от 1 до 10.

topic — важность по меркам её рубрики: насколько это важно тем, кто следит за этой темой. \
Каждую рубрику оценивай по её собственной шкале, не сравнивая с другими рубриками:
- Политика и мир: 10 — начало войны, смена власти в крупной стране; 5 — рядовое заявление политика.
- Наука и медицина: 10 — прорыв уровня новой вакцины или нобелевского открытия; 5 — рядовое исследование.
- Технологии: 10 — новый iPhone, крупнейшая сделка, сбой мирового масштаба; 5 — обновление приложения.
- Искусственный интеллект: 10 — новая флагманская модель OpenAI, Google или Anthropic; 5 — мелкая функция.
- Игры: 10 — анонс или перенос GTA, новая консоль, закрытие крупной студии; 5 — рядовой патч или трейлер.
- Кино и сериалы: 10 — «Оскар», кассовый рекорд, анонс крупной франшизы; 5 — рядовой кастинг.
- Спорт: 10 — финал Лиги чемпионов или чемпионата мира, титульный бой, победа на турнире \
Большого шлема; 5 — рядовой матч.

world — важность для широкой аудитории, одна шкала для всех рубрик: 10 — событие мирового \
масштаба, о котором говорят все; новости игр, кино и спорта редко выше 7.

В обеих оценках 1–2 — проходное: обзоры, подборки, гайды, скидки, слухи без источника, \
колонки мнений, реклама, мелкие локальные новости, исследования на мышах без практического значения.
Ответь JSON-массивом объектов {"topic": число, "world": число} той же длины и в том же порядке, \
что и список.

"""

RATE_SCHEMA = {"type": "ARRAY", "items": {
    "type": "OBJECT",
    "properties": {"topic": {"type": "INTEGER"}, "world": {"type": "INTEGER"}},
    "required": ["topic", "world"],
}}


def rate_stories(pool):
    """Gemini оценивает новые сюжеты пачками по 60: важность в рубрике и для всех."""
    if not GEMINI_API_KEY:
        return
    todo = [s for s in pool if s["importance"] is None and s.get("rate_tries", 0) < 3][:180]
    for start in range(0, len(todo), 60):
        batch = todo[start:start + 60]
        lines = []
        for n, story in enumerate(batch, 1):
            m = story["members"][0]
            lines.append(f"{n}. [{CATEGORY_BY_KEY[story['category']]['title']}] {m['title']} — {m['summary'][:200]}")
        try:
            scores = gemini(RATE_PROMPT + "\n".join(lines), RATE_SCHEMA)
            if not isinstance(scores, list) or len(scores) != len(batch):
                raise TranslationError("ответ не той длины")
            for story, value in zip(batch, scores):
                story["importance"] = max(1, min(10, int(value["topic"])))
                story["world"] = max(1, min(10, int(value["world"])))
        except Exception as exc:
            problem("rate", f"Gemini не смог оценить важность новостей: {exc}. "
                            f"Пока это так, бот публикует почти без отбора.")
            for story in batch:
                story["rate_tries"] = story.get("rate_tries", 0) + 1
            break
        time.sleep(2)


def apply_signals(pool):
    """Отмечает сюжеты, которые попали в тренды, на главную Hacker News или в топы читаемого."""
    signals = popularity.collect()
    if not signals or not pool:
        return
    by_url = {}
    for signal in signals:
        if signal["url"]:
            by_url.setdefault(link_key(signal["url"]), []).append(signal)
    titles, owner, links = [], [], []
    for index, story in enumerate(pool):
        for member in story["members"]:
            titles.append(member["title"])
            links.append(member["link"])
            owner.append(index)
    signal_titles = [s["title"] for s in signals]
    sims, semantic = dedup.similarity(titles, signal_titles)
    for row, title in enumerate(titles):
        hits = list(by_url.get(link_key(links[row]), []))
        for col in np.argsort(-sims[row])[:3]:
            if dedup.same_story(sims[row, col], title, signal_titles[col], semantic):
                hits.append(signals[col])
        story = pool[owner[row]]
        for signal in hits:
            story["signals"][signal["kind"]] = max(story["signals"].get(signal["kind"], 0),
                                                   popularity.bonus(signal))


def story_start(story):
    dates = [m["date"] for m in story["members"] if m.get("date")]
    return min(dates) if dates else story["first_seen"]


def story_score(story, now):
    sources = len({m["source"] for m in story["members"]})
    importance = story["importance"] if story["importance"] is not None else 5
    age_hours = (now - story_start(story)) / 3600
    # Перепечатки — главный признак важности; свежесть начинает отнимать баллы через 6 часов
    return (importance + 2 * min(sources - 1, 4) + sum(story["signals"].values())
            - max(0.0, age_hours - 6) / 3)


def world_score(story, now):
    """Тот же балл, но с мировой важностью вместо важности в рубрике: по нему решается,
    срочно ли это для всех (⚡️, звук, публикация без ожидания)."""
    if story.get("world") is None or story["importance"] is None:
        return story_score(story, now)
    return story_score(story, now) - story["importance"] + story["world"]


THRESHOLDS = {}  # порог каждой рубрики на этот запуск: ключ → (порог, по скольким сюжетам)


def track_peaks(pool, now):
    """Запоминает лучший балл каждого сюжета: со временем свежесть отнимает баллы."""
    for story in pool:
        if story["importance"] is not None:
            story["peak"] = max(story.get("peak", 0), story_score(story, now))


def retire(state, story):
    """Сюжет ушёл из очереди (вышел, оказался повтором или устарел) — его лучший балл в историю рубрики."""
    if story.get("importance") is None:
        return
    peak = story.get("peak") or story_score(story, time.time())
    state["history"].setdefault(story["category"], []).append([round(time.time()), round(peak, 2)])


def compute_thresholds(state):
    """Порог рубрики — балл, который набирает доля AUTO_SHARE лучших её сюжетов за AUTO_DAYS дней."""
    since = time.time() - AUTO_DAYS * 86400
    THRESHOLDS.clear()
    for category in CATEGORIES:
        key = category["key"]
        scores = sorted(s for ts, s in state["history"].get(key, []) if ts > since)
        if len(scores) < AUTO_MIN_STORIES:
            continue
        value = scores[min(int(len(scores) * (1 - AUTO_SHARE)), len(scores) - 1)]
        THRESHOLDS[key] = (round(min(AUTO_CEIL, max(AUTO_FLOOR, value)), 1), len(scores))
    print("Пороги рубрик: " + ", ".join(
        f"{c['key']} {min_score(c['key']):g}"
        + (f" (по {THRESHOLDS[c['key']][1]} сюжетам)" if c["key"] in THRESHOLDS else " (стартовый)")
        for c in CATEGORIES))


def min_score(key):
    """Порог публикации для рубрики: ручной из news.yml (MIN_SCORE_<РУБРИКА>), иначе посчитанный
    по истории рубрики, а пока данных мало — стартовый из CATEGORY_MIN_SCORE."""
    value = os.getenv(f"MIN_SCORE_{key.upper()}", "").strip()
    if value:
        return float(value)
    if key in THRESHOLDS:
        return THRESHOLDS[key][0]
    return CATEGORY_MIN_SCORE.get(key, MIN_SCORE)


def eligible(story, now):
    rated = story["importance"] is not None or story.get("rate_tries", 0) >= 3 or not GEMINI_API_KEY
    if not rated:
        return False
    score = story_score(story, now)
    matured = now - story_start(story) >= HOLD_MINUTES * 60
    return score >= min_score(story["category"]) and (matured or world_score(story, now) >= BREAKING_SCORE)


# ---------- очередь «созревания» ----------

def update_pool(state, seen, cutoff):
    pool = state["pool"]
    in_pool = {link_key(m["link"]) for s in pool for m in s["members"]}
    found, failures = fetch_entries(CATEGORIES)
    track_feeds(state, failures)
    items = {}
    for item in found:
        key = link_key(item["link"])
        if key not in seen and key not in in_pool and item["date"] > cutoff:
            items.setdefault(key, item)
    items = list(items.values())

    # 1. Такой сюжет уже публиковали
    recent = state["recent"]
    known = dedup.find_known([i["text"] for i in items], [r["text"] for r in recent]) if items else []
    fresh = []
    for item, match in zip(items, known):
        if match is None:
            fresh.append(item)
        else:
            print(f"[дубль] {item['source']}: {item['title']}  ≈  {recent[match]['title']}")
            mark_seen(state, seen, item)

    # 2. Новая статья о сюжете, который уже ждёт в очереди, — добавляется к нему
    texts, owner = [], []
    for index, story in enumerate(pool):
        for member in story["members"]:
            texts.append(member["text"])
            owner.append(index)
    attach = dedup.find_known([i["text"] for i in fresh], texts) if fresh else []
    new = []
    for item, match in zip(fresh, attach):
        if match is None:
            new.append(item)
        else:
            pool[owner[match]]["members"].append(pack(item))

    # 3. Новые сюжеты
    now = time.time()
    for group in (dedup.group_stories([i["text"] for i in new]) if new else []):
        members = [new[k] for k in group]
        pool.append({"category": members[0]["category"], "first_seen": now, "importance": None,
                     "world": None, "rating_v": RATING_V,
                     "rate_tries": 0, "signals": {}, "members": [pack(m) for m in members]})

    # 4. Не «выстрелившее» за POOL_HOURS — выбрасываем
    keep = []
    for story in pool:
        if now - story_start(story) > POOL_HOURS * 3600:
            retire(state, story)
            for member in story["members"]:
                mark_seen(state, seen, member)
        else:
            keep.append(story)
    state["pool"] = keep
    print(f"В очереди сюжетов: {len(keep)} (новых: {len(new)})")


def show_top(pool, now, limit=8):
    ranked = sorted(pool, key=lambda s: story_score(s, now), reverse=True)[:limit]
    for story in ranked:
        sources = len({m["source"] for m in story["members"]})
        mark = "✅" if eligible(story, now) else "⏳"
        age = (now - story_start(story)) / 3600
        print(f"  {mark} {story_score(story, now):5.1f}/{min_score(story['category']):g}  "
              f"важность {story['importance']} (для всех {story.get('world')})  "
              f"изданий {sources}  {story['signals'] or ''}  {age:.1f} ч  "
              f"[{story['category']}] {story['members'][0]['title']}")


def choose(pool, order, now):
    """Лучший готовый сюжет из первой по кругу рубрики, где есть важное."""
    for category in order:
        candidates = [s for s in pool if s["category"] == category["key"] and eligible(s, now)]
        if candidates:
            return max(candidates, key=lambda s: story_score(s, now))
    return None


REPEAT_PROMPT = """Ты — выпускающий редактор новостного канала. Ниже новая новость и список \
уже опубликованных за последние двое суток.
Повтор — если новость о том же событии или том же инфоповоде, даже под другим углом: \
обзор, первые впечатления, цены, характеристики, реакция, подробности. \
Например, «Apple представила новый MacBook» и «Обзор нового MacBook» — повтор.
Не повтор — только если произошло действительно новое событие.
Ответь JSON: {"repeat": номер опубликованной новости, которую она повторяет, или 0}.

"""


def is_repeat(story, recent):
    """Запись об уже опубликованной новости о том же событии ({title, msg, …}) или None.
    Сначала быстрое сравнение по смыслу, в спорных случаях решает Gemini."""
    if not recent:
        return None
    titles = [m["title"] for m in story["members"]]
    recent = recent[-500:]  # последние сюжеты — этого хватает и не замедляет запуск
    old = [r["title"] for r in recent]
    sims, semantic = dedup.similarity(titles, old)
    best = sims.max(axis=0)  # сходство каждой опубликованной новости с ближайшей статьёй сюжета
    loose = 0.45 if semantic else 0.25  # мягкий порог: кандидаты на проверку
    close = [int(j) for j in np.argsort(-best)[:8] if best[j] >= loose]
    if not close:
        return None
    for j in close:
        for i, title in enumerate(titles):
            if dedup.same_story(sims[i, j], title, old[j], semantic):
                return recent[j]
    if not GEMINI_API_KEY:
        return None
    main = story["members"][0]
    text = (f"Новая: {main['title']} — {main['summary'][:300]}\n\nОпубликованные:\n"
            + "\n".join(f"{n}. {old[j]}" for n, j in enumerate(close, 1)))
    try:
        answer = gemini(REPEAT_PROMPT + text, {"type": "OBJECT", "properties": {"repeat": {"type": "INTEGER"}},
                                               "required": ["repeat"]})
        number = int(answer.get("repeat", 0))
    except Exception as exc:
        print(f"[!] не удалось проверить повтор: {exc}")
        return None
    return recent[close[number - 1]] if 1 <= number <= len(close) else None


# ---------- устойчивость к перегрузке Gemini (503/429) ----------

class Overloaded(TranslationError):
    """Gemini перегружен (503) или исчерпан лимит (429): это временно."""


def gemini(prompt, schema, model=None, attempts=3, temperature=0.2):
    """Запрос к Gemini с JSON-ответом по схеме. При перегрузке ждёт дольше."""
    model = model or GEMINI_MODEL
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": temperature,
            "maxOutputTokens": 65536,
        },
    }
    error, busy, wait = None, False, 0
    for attempt in range(attempts):
        if wait:
            time.sleep(wait)
        wait = 5 * (attempt + 1)
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=120,
            )
        except requests.RequestException as exc:
            error, busy = f"Gemini ({model}): {exc}", False
            continue
        if resp.status_code != 200:
            error = f"Gemini ({model}) {resp.status_code}: {resp.text[:300]}"
            busy = resp.status_code in (429, 503)
            if busy:
                wait = 20 * (attempt + 1)  # перегрузка проходит не за секунды
            if resp.status_code == 404:
                problem(f"gemini404:{model}", f"Модель Gemini «{model}» не найдена: похоже, Google снял её "
                                              f"с поддержки. Поменяйте её название в news.yml или bot.py.")
                break
            if resp.status_code in (400, 401, 403):  # ключ неверный — повтор не поможет
                problem("gemini-key", f"Gemini не принимает ключ или запрос ({resp.status_code}). "
                                      f"Проверьте секрет GEMINI_API_KEY.")
                break
            continue
        try:
            return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, ValueError) as exc:
            error, busy = f"Gemini ({model}) вернул испорченный ответ: {exc}", False
    raise (Overloaded if busy else TranslationError)(error)


def gemini_any(prompt, schema):
    """Оценки и проверка повторов: сначала лёгкая модель, если она перегружена — сильная."""
    error = None
    for model in dict.fromkeys([GEMINI_MODEL, GEMINI_TEXT_MODEL]):
        try:
            return gemini(prompt, schema, model, attempts=2)
        except TranslationError as exc:
            error = exc
            print(f"[!] {exc}")
    raise error


def rate_stories(pool):
    """Gemini оценивает новые сюжеты пачками по 60: важность в рубрике и для всех."""
    if not GEMINI_API_KEY:
        return
    todo = [s for s in pool if s["importance"] is None and s.get("rate_tries", 0) < 3][:180]
    for start in range(0, len(todo), 60):
        batch = todo[start:start + 60]
        lines = []
        for n, story in enumerate(batch, 1):
            m = story["members"][0]
            lines.append(f"{n}. [{CATEGORY_BY_KEY[story['category']]['title']}] {m['title']} — {m['summary'][:200]}")
        try:
            scores = gemini_any(RATE_PROMPT + "\n".join(lines), RATE_SCHEMA)
            if not isinstance(scores, list) or len(scores) != len(batch):
                raise TranslationError("ответ не той длины")
            for story, value in zip(batch, scores):
                story["importance"] = max(1, min(10, int(value["topic"])))
                story["world"] = max(1, min(10, int(value["world"])))
        except Overloaded as exc:
            # Временная перегрузка: попытку не считаем, сюжеты ждут оценки, а не выходят без отбора
            problem("rate", f"Gemini перегружен и не смог оценить новости: {exc}. "
                            f"Новости ждут оценки, бот попробует в следующий запуск.")
            break
        except Exception as exc:
            problem("rate", f"Gemini не смог оценить важность новостей: {exc}. "
                            f"Пока это так, бот публикует почти без отбора.")
            for story in batch:
                story["rate_tries"] = story.get("rate_tries", 0) + 1
            break
        time.sleep(2)


def is_repeat(story, recent):
    """Запись об уже опубликованной новости о том же событии ({title, msg, …}) или None.
    Сначала быстрое сравнение по смыслу, в спорных случаях решает Gemini."""
    if not recent:
        return None
    titles = [m["title"] for m in story["members"]]
    recent = recent[-500:]  # последние сюжеты — этого хватает и не замедляет запуск
    old = [r["title"] for r in recent]
    sims, semantic = dedup.similarity(titles, old)
    best = sims.max(axis=0)  # сходство каждой опубликованной новости с ближайшей статьёй сюжета
    loose = 0.45 if semantic else 0.25  # мягкий порог: кандидаты на проверку
    close = [int(j) for j in np.argsort(-best)[:8] if best[j] >= loose]
    if not close:
        return None
    for j in close:
        for i, title in enumerate(titles):
            if dedup.same_story(sims[i, j], title, old[j], semantic):
                return recent[j]
    if not GEMINI_API_KEY:
        return None
    main = story["members"][0]
    text = (f"Новая: {main['title']} — {main['summary'][:300]}\n\nОпубликованные:\n"
            + "\n".join(f"{n}. {old[j]}" for n, j in enumerate(close, 1)))
    try:
        answer = gemini_any(REPEAT_PROMPT + text, {"type": "OBJECT", "properties": {"repeat": {"type": "INTEGER"}},
                                                   "required": ["repeat"]})
        number = int(answer.get("repeat", 0))
    except Exception as exc:
        print(f"[!] не удалось проверить повтор: {exc}")
        return None
    return recent[close[number - 1]] if 1 <= number <= len(close) else None


def gemini_text(prompt, schema):
    """Тексты для читателя пишет более сильная модель; если она недоступна или исчерпан
    её лимит — та, что оценивает важность."""
    models = list(dict.fromkeys([GEMINI_TEXT_MODEL, GEMINI_MODEL]))
    error = None
    for n, model in enumerate(models):
        try:
            return gemini(prompt, schema, model, attempts=2 if n < len(models) - 1 else 3)
        except TranslationError as exc:
            error = exc
            print(f"[!] {exc}")
    raise TranslationError(error)


# ---------- перевод ----------

def is_russian(text, share=0.5):
    letters = re.findall(r"[a-zа-яё]", text.lower())
    return not letters or sum(c >= "а" for c in letters) / len(letters) > share


TRANSLATE_PROMPT = (
    "Переведи на язык «{lang}» каждую строку из JSON-массива. Это новостная статья: "
    "пиши естественно и грамотно, в стиле качественного новостного издания, без пояснений и сокращений. "
    "Имена, названия компаний, игр и фильмов передавай так, как их принято писать в русских СМИ. "
    "Перед ответом перечитай перевод: в нём не должно быть опечаток, пропущенных или лишних букв, "
    "склеенных слов и слов, где смешаны русские и латинские буквы. "
    "Ответь только JSON-массивом строк той же длины и в том же порядке.\n\n"
)


def chunks(parts, limit):
    """Делит абзацы на куски до limit символов, не разрывая абзацы."""
    batch, size = [], 0
    for part in parts:
        if batch and size + len(part) > limit:
            yield batch
            batch, size = [], 0
        batch.append(part)
        size += len(part)
    if batch:
        yield batch


def _gemini_translate(parts):
    """Перевод нейросетью Gemini кусками: на коротких ответах модель почти не ошибается в буквах."""
    result = []
    for batch in chunks(parts, TRANSLATE_CHUNK):
        prompt = TRANSLATE_PROMPT.format(lang=TRANSLATE_TO) + json.dumps(batch, ensure_ascii=False)
        for attempt in range(2):
            translated = gemini_text(prompt, {"type": "ARRAY", "items": {"type": "STRING"}})
            if not isinstance(translated, list) or len(translated) != len(batch):
                raise TranslationError("Gemini вернул ответ не того формата")
            translated = [str(r) for r in translated]
            if attempt == 0 and any(garbled(t) for t in translated):
                print("[!] в переводе слова со смешанными буквами — перевожу этот кусок заново")
                continue
            break
        result += translated
    return result


def _translate_chunk(translator, text):
    for attempt in range(2):
        try:
            return translator.translate(text) or text
        except Exception as exc:
            if attempt == 1:
                raise TranslationError(exc)
            time.sleep(3)


def _google_translate(parts):
    """Бесплатный Google Translate — запасной вариант (с GitHub его часто блокируют)."""
    from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source="auto", target=TRANSLATE_TO)

    # Абзацы склеиваются в куски до 4500 символов: меньше запросов
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
        time.sleep(0.5)

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
    return result


def translate(parts, lang):
    """Переводит список строк, сохраняя разбиение.
    Сначала Gemini (если задан GEMINI_API_KEY), при ошибке — бесплатный Google Translate.
    Если не сработало ничего, бросает TranslationError: пост на чужом языке не выйдет,
    новость останется в очереди до следующего запуска."""
    if not TRANSLATE_TO or lang == TRANSLATE_TO or not parts:
        return parts
    if TRANSLATE_TO == "ru" and all(is_russian(p) for p in parts):
        return parts  # источник уже на русском, даже если это не отмечено в feeds.py

    result, errors = None, []
    if GEMINI_API_KEY:
        try:
            result = _gemini_translate(parts)
        except Exception as exc:
            errors.append(f"Gemini: {exc}")
    if result is None:
        try:
            result = _google_translate(parts)
        except Exception as exc:
            errors.append(f"Google: {exc}")
    if result is None:
        raise TranslationError("; ".join(errors))

    # В переводе остаются латинские названия (iPhone, GTA), поэтому достаточно трети кириллицы
    if TRANSLATE_TO == "ru" and not is_russian(" ".join(result), share=0.3):
        raise TranslationError("перевод вернулся не на русском")
    return result


# ---------- выжимка для поста ----------

DIGEST_PROMPT = """Ты — редактор новостного Telegram-канала. Читатель листает ленту: \
за 10 секунд он должен понять, что произошло и почему это важно, даже если впервые слышит об этой теме.
Сделай по статье ниже JSON с полями (всё на русском, грамотно, без опечаток):
title — понятный заголовок до 100 символов. Сразу ясно, о ком или о чём речь: \
не «Альтман ответил критикам», а «Глава OpenAI Сэм Альтман ответил на обвинения в…». \
Малоизвестных людей, компании, игры, команды поясняй парой слов. Без кликбейта и вопросов.
lead — что случилось: 2–3 коротких предложения, до 350 символов, одним связным абзацем. \
Самые важные цифры, сроки и имена вплети прямо в текст, без отдельных списков. \
Без воды, вводных слов и повтора заголовка.
meaning — ответ на вопрос «Что это значит?», до 380 символов. Длину выбирай по сложности: \
простая новость (вышла игра, кто-то выиграл матч) — одно предложение; сложная (политика, \
экономика, война, наука, медицина) — до четырёх. Объясни, что эта новость меняет и почему она важна, \
так, будто рассказываешь другу, который совсем не разбирается в теме: без терминов и канцелярита, \
без повтора lead. Если нужен контекст (почему это случилось, что было до этого), дай его коротко. \
Если есть практическое следствие (что изменится, для кого, когда), назови его. \
Если в статье есть следующий шаг или дата (голосование, релиз, матч, решение суда), \
закончи объяснение им одной короткой фразой. Если нет — не гадай.
Везде: если факт известен только со слов одной стороны (правительства, армии, компании, \
полиции) или не подтверждён, так и пиши: «по данным…», «как заявил…», «по неподтверждённым данным». \
Не выдавай заявления за установленные факты. Не выдумывай того, чего нет в статье.

"""

DIGEST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "lead": {"type": "STRING"},
        "meaning": {"type": "STRING"},
    },
    "required": ["title", "lead", "meaning"],
}


def first_sentences(text, limit=320):
    out = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text.strip()):
        if out and len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    return shorten(out, limit)


def digest(item, summary, paragraphs):
    """Заголовок, краткий пересказ, факты, «Что это значит?» и полный перевод для Telegraph."""
    full = []
    if paragraphs:
        try:
            full = translate(paragraphs, item["lang"])
        except TranslationError as exc:
            print(f"[!] полный текст не переведён, пост без «Читать полностью»: {exc}")
    if GEMINI_API_KEY and TRANSLATE_TO:
        article = {"title": item["title"], "summary": summary,
                   "text": "\n".join(take_chars(paragraphs, 8000))}
        prompt = DIGEST_PROMPT + json.dumps(article, ensure_ascii=False)
        for attempt in range(2):
            try:
                data = gemini_text(prompt, DIGEST_SCHEMA)
            except Exception as exc:
                print(f"[!] не удалось сделать выжимку, перевожу без неё: {exc}")
                break
            title, lead = str(data.get("title", "")).strip(), str(data.get("lead", "")).strip()
            meaning = str(data.get("meaning", "")).strip()
            text = " ".join([title, lead, meaning])
            if not title or not lead or (TRANSLATE_TO == "ru" and not is_russian(text, share=0.3)):
                print("[!] выжимка вернулась не того формата")
                continue
            if attempt == 0 and garbled(text):
                print("[!] в выжимке слова со смешанными буквами — прошу переписать")
                continue
            return {"title": title, "lead": lead, "meaning": meaning, "paragraphs": full}
    # Запасной вариант: обычный перевод, суть — первые предложения описания
    translated = translate([item["title"], summary], item["lang"])
    return {"title": translated[0], "lead": first_sentences(translated[1]), "meaning": "",
            "paragraphs": full}


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


def video_node(video):
    """YouTube и Vimeo Telegraph показывает встроенным плеером, остальное — ссылкой."""
    if video["kind"] in ("youtube", "vimeo"):
        src = f"/embed/{video['kind']}?url={quote(video['url'], safe='')}"
        return {"tag": "figure", "children": [{"tag": "iframe", "attrs": {"src": src}}]}
    return {"tag": "p", "children": [
        {"tag": "a", "attrs": {"href": video["url"]}, "children": ["▶️ Смотреть видео из статьи"]}]}


def telegraph_page(title, paragraphs, image, item, videos):
    nodes = [{"tag": "img", "attrs": {"src": image}}] if image else []
    nodes += [video_node(v) for v in videos]
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

def tg(method, files=None, **params):
    params.setdefault("chat_id", CHANNEL_ID)
    data = requests.post(f"{TG_API}/{method}", data=params, files=files, timeout=60).json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description')}")
    return data["result"]


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:max(limit - 1, 0)].rsplit(" ", 1)[0].rstrip(",.;:—- ")
    return cut + "…"


def take_chars(paragraphs, limit):
    """Первые целые абзацы, укладывающиеся в limit символов."""
    out, size = [], 0
    for paragraph in paragraphs:
        if out and size + len(paragraph) > limit:
            break
        out.append(paragraph)
        size += len(paragraph)
    return out


def build_post(category, post, sources, limit, urgent=False):
    """Короткий пост: заголовок, что случилось, «Что это значит?», источники и рубрика."""
    mark = "⚡️ " if urgent else ""
    head = f"{mark}{category['emoji']} <b>{html.escape(shorten(post['title'], 160))}</b>\n\n"
    names = " · ".join(sources[:4])
    tail = f"\n\n<i>{html.escape(names)}</i>  {hashtag(category['tag'])}"
    lead, meaning = post["lead"], post.get("meaning", "")
    while True:
        body = html.escape(lead)
        if meaning:
            body += f"\n\n❓ <b>Что это значит?</b>\n{html.escape(meaning)}"
        if len(head) + len(body) + len(tail) <= limit:
            return head + body + tail
        # Сначала сокращаем объяснение, потом пересказ
        if len(meaning) > 160:
            meaning = shorten(meaning, len(meaning) - 40)
        elif len(lead) > 120:
            lead = shorten(lead, len(lead) - 40)
        elif meaning:
            meaning = ""
        else:
            return head + body + tail


def sound(urgent):
    """Срочное приходит со звуком, остальное — тихо: подписчики не устают от уведомлений."""
    return {} if urgent or not SILENT_POSTS else {"disable_notification": "true"}


def keyboard(page, sources):
    rows = [[{"text": "📖 Читать полностью", "url": page}]] if page else []
    buttons = [{"text": f"🔗 {name}", "url": link} for name, link in sources[:4]]
    rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return json.dumps({"inline_keyboard": rows})


def image_candidates(item, article):
    """Картинка со страницы статьи (крупная) — первой; миниатюра из ленты — только если не мелкая."""
    out = list(article.get("images", []))
    width = item.get("image_width", 0)
    if item.get("image") and (width == 0 or width >= MIN_IMAGE_WIDTH):
        out.append(item["image"])
    return list(dict.fromkeys(out))


def download_image(urls):
    """Первая картинка, которая скачалась и не слишком мала: (адрес, байты, тип) или None."""
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[!] картинка недоступна: {exc}")
            continue
        kind = resp.headers.get("Content-Type", "").split(";")[0].strip()
        # Миниатюры весят единицы килобайт, нормальное фото — от 30 КБ
        if kind in ("image/jpeg", "image/png", "image/webp") and MIN_IMAGE_BYTES <= len(resp.content) <= 10_000_000:
            return url, resp.content, kind
    return None


def download_video(url):
    """Файл .mp4 из статьи для шапки поста: байты или None, если не скачался или слишком большой."""
    limit = MAX_VIDEO_MB * 1024 * 1024
    data = bytearray()
    try:
        with requests.get(url, headers=HEADERS, timeout=30, stream=True) as resp:
            resp.raise_for_status()
            if int(resp.headers.get("Content-Length") or 0) > limit:
                print("[!] видео больше лимита Telegram, в шапке будет картинка")
                return None
            for part in resp.iter_content(1 << 20):
                data += part
                if len(data) > limit:
                    print("[!] видео больше лимита Telegram, в шапке будет картинка")
                    return None
    except Exception as exc:
        print(f"[!] видео недоступно: {exc}")
        return None
    return bytes(data) or None


def send_video_post(category, post, names, markup, videos, urgent=False):
    """Пост с видео в шапке вместо картинки. None — ни одно видео не подошло."""
    for video in videos:
        try:
            if video["kind"] == "file":
                data = download_video(video["url"])
                if not data:
                    continue
                # Ролик загружаем сами: в шапке он играет прямо в Telegram
                return tg("sendVideo", files={"video": ("video.mp4", data, "video/mp4")},
                          supports_streaming="true", parse_mode="HTML", reply_markup=markup,
                          caption=build_post(category, post, names, 1024, urgent), **sound(urgent))
            # YouTube и Vimeo: Telegram сам ставит плеер над текстом, ролик смотрится не выходя из канала
            return tg("sendMessage", parse_mode="HTML", reply_markup=markup,
                      text=build_post(category, post, names, 1500, urgent), **sound(urgent),
                      link_preview_options=json.dumps({"url": video["url"], "prefer_large_media": True,
                                                       "show_above_text": True}))
        except Exception as exc:
            if any(s in str(exc) for s in FATAL_TG_ERRORS):
                raise
            print(f"[!] не удалось отправить с видео, пробую дальше: {exc}")
    return None


def send_post(category, post, sources, image, markup, videos=(), urgent=False):
    """Публикует пост и возвращает сообщение Telegram."""
    names = [name for name, _ in sources]
    if VIDEO_HEADER and videos:
        message = send_video_post(category, post, names, markup, videos, urgent)
        if message:
            return message
    if image:
        _, data, kind = image
        try:
            # Сверху — только картинка; файл загружаем сами, без пережатия чужим сервером
            return tg("sendPhoto", files={"photo": ("photo." + kind.split("/")[1], data, kind)},
                      parse_mode="HTML", reply_markup=markup, **sound(urgent),
                      caption=build_post(category, post, names, 1024, urgent))
        except Exception as exc:
            if any(s in str(exc) for s in FATAL_TG_ERRORS):
                raise
            print(f"[!] не удалось отправить с фото, отправляю текстом: {exc}")
    return tg("sendMessage", parse_mode="HTML", reply_markup=markup, **sound(urgent),
              text=build_post(category, post, names, 1500, urgent),
              link_preview_options=json.dumps({"is_disabled": True}))


def post_link(message):
    """Ссылка на пост в канале: https://t.me/канал/номер."""
    number = message.get("message_id") if isinstance(message, dict) else None
    if not number:
        return None
    if CHANNEL_ID.startswith("@"):
        return f"https://t.me/{CHANNEL_ID[1:]}/{number}"
    if CHANNEL_ID.startswith("-100"):
        return f"https://t.me/c/{CHANNEL_ID[4:]}/{number}"
    return None


_USERNAME = []


def channel_username():
    """Имя публичного канала без @ (например, fluxwirenews)."""
    if not _USERNAME:
        name = CHANNEL_ID[1:] if CHANNEL_ID.startswith("@") else ""
        if not name:
            try:
                name = tg("getChat").get("username") or ""
            except Exception as exc:
                print(f"[!] не удалось узнать имя канала: {exc}")
        _USERNAME.append(name)
    return _USERNAME[0]


def hashtag(tag):
    """#тег@канал: по нажатию Telegram ищет только в этом канале, а не по всем публичным."""
    name = channel_username()
    return f"#{tag}@{name}" if name else f"#{tag}"


def story_sources(story):
    item = story["main"]
    sources = {item["source"]: item["link"]}
    for member in story["members"]:
        sources.setdefault(member["source"], member["link"])
    return list(sources.items())


def publish(category, story, urgent=False):
    """Публикует сюжет. Возвращает заголовок и ссылку для вечернего дайджеста и номер поста."""
    item = story["main"]
    sources = story_sources(story)

    article = fetch_article(item["link"])
    paragraphs = article.get("paragraphs", [])
    if sum(map(len, item["rss_paragraphs"])) > sum(map(len, paragraphs)):
        paragraphs = item["rss_paragraphs"]
    paragraphs = take_chars(paragraphs, MAX_ARTICLE_CHARS) if FULL_TEXT else []
    image = download_image(image_candidates(item, article))
    videos = unique_videos(article.get("videos", []) + item.get("videos", []))
    if videos:
        print(f"Видео в статье: {', '.join(v['url'] for v in videos)}")

    summary = item["summary"]
    if len(summary) < 80:
        summary = article.get("description") or " ".join(paragraphs[:2]) or summary
    post = digest(item, summary, paragraphs)

    # Полный перевод и видео — в Telegraph, по кнопке «Читать полностью»; видео ещё и в шапке поста
    page = None
    if len(post["paragraphs"]) >= 3:
        page = telegraph_page(post["title"], post["paragraphs"], image[0] if image else None, item, videos)
    message = send_post(category, post, sources, image, keyboard(page, sources), videos, urgent)
    return {"title": post["title"], "url": page or post_link(message) or item["link"],
            "msg": message.get("message_id") if isinstance(message, dict) else None}


# ---------- обновления: новые факты о вышедшем сюжете ----------

UPDATE_PROMPT = """Ты — выпускающий редактор новостного Telegram-канала. Канал уже опубликовал \
новость (published). Пришла новая статья о том же сюжете (new).
Реши, есть ли в новой статье существенный новый факт — развитие событий, о котором читателю \
стоит узнать: принято решение, известен итог, изменилось число жертв или сумма, \
пришло официальное подтверждение или опровержение, в историю вступила новая сторона, \
то, что было обещано, произошло.
Не новый факт: пересказ того же, обзор, анализ, мнения, реакция без последствий, те же цифры.
Ответь JSON:
new_fact — true или false;
update — если true: обновление на русском, 1–2 коротких предложения, до 250 символов, \
сразу с нового факта, без повтора того, что уже было в published. Грамотно, без опечаток. \
Если факт известен со слов одной стороны или не подтверждён, так и пиши: «по данным…», «как заявил…». \
Если false — пустая строка.

"""

UPDATE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"new_fact": {"type": "BOOLEAN"}, "update": {"type": "STRING"}},
    "required": ["new_fact", "update"],
}


def maybe_update(state, category, story, old, urgent=False):
    """Если в сюжете, который уже был в канале, появились новые факты, — публикует
    «🔄 Обновление» ответом на старый пост. True — обновление вышло."""
    root = old.get("msg")
    if not UPDATES or not root or not GEMINI_API_KEY:
        return False
    now = time.time()
    thread = state["threads"].get(str(root), {"count": 0, "ts": 0})
    if thread["count"] >= UPDATE_MAX or now - thread["ts"] < UPDATE_GAP_MIN * 60:
        return False
    item = story["main"]
    paragraphs = item["rss_paragraphs"] or fetch_article(item["link"]).get("paragraphs", [])
    data = {"published": old["title"],
            "new": {"title": item["title"], "summary": item["summary"],
                    "text": "\n".join(take_chars(paragraphs, 4000))}}
    try:
        answer = gemini_text(UPDATE_PROMPT + json.dumps(data, ensure_ascii=False), UPDATE_SCHEMA)
    except Exception as exc:
        print(f"[!] не удалось проверить, есть ли новые факты: {exc}")
        return False
    text = str(answer.get("update", "")).strip()
    if not answer.get("new_fact") or not text:
        return False
    if (TRANSLATE_TO == "ru" and not is_russian(text, share=0.3)) or garbled(text):
        print("[!] обновление вернулось с ошибками, не публикую")
        return False
    sources = story_sources(story)
    names = " · ".join(name for name, _ in sources[:4])
    mark = "⚡️ " if urgent else ""
    body = (f"{mark}🔄 <b>Обновление</b>\n\n{html.escape(shorten(text, 400))}\n\n"
            f"<i>{html.escape(names)}</i>  {hashtag(category['tag'])}")
    try:
        # Ответом на исходный пост: сюжет развивается в одной ветке. Если тот пост удалён — не публикуем
        tg("sendMessage", parse_mode="HTML", text=body, reply_markup=keyboard(None, sources),
           link_preview_options=json.dumps({"is_disabled": True}), **sound(urgent),
           reply_parameters=json.dumps({"message_id": root, "allow_sending_without_reply": False}))
    except Exception as exc:
        if any(s in str(exc) for s in FATAL_TG_ERRORS):
            problem("telegram", f"Telegram не даёт публиковать в канал: {exc}. Проверьте права бота.")
        else:
            print(f"[!] обновление не отправлено: {exc}")
        return False
    state["threads"][str(root)] = {"count": thread["count"] + 1, "ts": now}
    return True


def as_story(pooled):
    members = [unpack(m) for m in pooled["members"]]
    main = max(members, key=lambda i: (i["lang"] == TRANSLATE_TO, bool(i["image"]),
                                       len(i["summary"]), i["date"] or EPOCH))
    return {"main": main, "members": members}


# ---------- вечерний дайджест ----------

def local_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(DIGEST_TZ)
    except Exception:
        print(f"[!] часовой пояс {DIGEST_TZ} не найден, считаю по Москве (UTC+3)")
        return timezone(timedelta(hours=3))


def build_digest(items, now_local):
    """Сообщения дайджеста: кликабельные заголовки по рубрикам. Длинный делится на части."""
    head = (f"🗞 <b>Главное за день · {now_local.day} {MONTHS[now_local.month - 1]}</b>\n"
            f"<i>Нажмите на заголовок, чтобы прочитать новость полностью</i>")
    lines = []
    for category in CATEGORIES:
        own = [d for d in items if d["category"] == category["key"]]
        if not own:
            continue
        lines.append("")
        lines.append(f"{category['emoji']} <b>{html.escape(category['title'])}</b>")
        for d in own:
            lines.append(f'▫️ <a href="{html.escape(d["url"], quote=True)}">'
                         f'{html.escape(shorten(d["title"], 110))}</a>')
    messages, current, links = [], head, 0
    for line in lines:
        # Лимит Telegram — 4096 символов; ссылок в одном сообщении держим не больше 40
        if len(current) + len(line) > 3800 or (line.startswith("▫️") and links >= 40):
            messages.append(current)
            current, links = "🗞 <b>Главное за день (продолжение)</b>", 0
        current += "\n" + line
        links += line.startswith("▫️")
    messages.append(current + "\n\n" + hashtag("дайджест"))
    return messages


def pin_digest(state, message_id):
    """Закрепляет свежий дайджест и открепляет прошлый: в шапке канала всегда последний."""
    try:
        tg("pinChatMessage", message_id=message_id, disable_notification="true")
    except Exception as exc:
        problem("pin", f"Не удалось закрепить дайджест: {exc}. Дайте боту в канале право "
                       f"изменять чужие публикации: без него Telegram не разрешает закреплять.")
        return
    old = state.get("digest_pinned")
    if old and old != message_id:
        try:
            tg("unpinChatMessage", message_id=old)
        except Exception as exc:
            print(f"[!] не удалось открепить прошлый дайджест: {exc}")
    state["digest_pinned"] = message_id


# ---------- навигация по каналу ----------

def navigation_text():
    lines = [f"🧭 <b>Навигация {html.escape(CHANNEL_NAME)}</b>",
             "<i>Нажмите на хэштег, чтобы увидеть новости только этой рубрики</i>", ""]
    lines += [f"{c['emoji']} {hashtag(c['tag'])} — {html.escape(c['title'])}" for c in CATEGORIES]
    if DIGEST_HOUR >= 0:
        lines += ["", f"🗞 {hashtag('дайджест')} — главное за день, каждый вечер в {DIGEST_HOUR}:00"]
    lines += ["", "<b>Значки в постах</b>",
              "⚡️ — срочная новость, приходит со звуком",
              "🔄 — обновление сюжета: ответ на прошлый пост о нём",
              "❓ — что эта новость значит, простыми словами",
              "📖 — полный перевод статьи прямо в Telegram"]
    return "\n".join(lines)


def ensure_navigation(state):
    """Публикует и закрепляет навигацию. Если рубрики изменились — заменяет старую.
    Если навигация выключена — убирает уже опубликованную: в закрепе остаётся только дайджест."""
    if not NAVIGATION:
        old = state.pop("nav_msg", None)
        state.pop("nav_sign", None)
        if old:
            for method in ("unpinChatMessage", "deleteMessage"):
                try:
                    tg(method, message_id=old)
                except Exception as exc:
                    print(f"[!] навигация: {method}: {exc}")
            print("Навигация откреплена и удалена")
        return
    text = navigation_text()
    sign = hashlib.sha1(text.encode()).hexdigest()[:12]
    if state.get("nav_sign") == sign:
        return
    try:
        message = tg("sendMessage", parse_mode="HTML", text=text, disable_notification="true",
                     link_preview_options=json.dumps({"is_disabled": True}))
    except Exception as exc:
        problem("nav", f"Не удалось опубликовать навигацию: {exc}")
        return
    # Запоминаем сразу: даже если закрепить не вышло, навигация не будет публиковаться каждый запуск
    old, state["nav_msg"], state["nav_sign"] = state.get("nav_msg"), message["message_id"], sign
    try:
        tg("pinChatMessage", message_id=message["message_id"], disable_notification="true")
    except Exception as exc:
        problem("pin", f"Не удалось закрепить навигацию: {exc}. Дайте боту в канале право "
                       f"изменять чужие публикации: без него Telegram не разрешает закреплять.")
    if old:
        for method in ("unpinChatMessage", "deleteMessage"):
            try:
                tg(method, message_id=old)
            except Exception as exc:
                print(f"[!] старая навигация: {method}: {exc}")
    print("Навигация опубликована и закреплена")


def maybe_digest(state, force=False):
    """Раз в день после DIGEST_HOUR публикует дайджест всего, что вышло с прошлого раза.
    force — опубликовать сейчас для проверки, не сбивая вечерний."""
    if DIGEST_HOUR < 0 and not force:
        return
    now_local = datetime.now(local_tz())
    today = now_local.date().isoformat()
    if not force and (now_local.hour < DIGEST_HOUR or state.get("digest_date") == today):
        return
    items = list(state["digest"])
    if not items:
        print("Дайджест: с прошлого раза ничего не опубликовано")
        if not force:
            state["digest_date"] = today
        return
    first = None
    try:
        for text in build_digest(items, now_local):
            message = tg("sendMessage", parse_mode="HTML", text=text,
                         link_preview_options=json.dumps({"is_disabled": True}))
            first = first or message
    except Exception as exc:
        problem("digest", f"Не удалось опубликовать вечерний дайджест: {exc}. Бот попробует ещё раз "
                          f"при следующем запуске.")
        return
    print(f"Дайджест опубликован: {len(items)} новостей")
    if first:
        pin_digest(state, first["message_id"])
    if not force:
        state["digest_date"] = today
        state["digest"] = []


# ---------- запуск ----------

def prepare(state, seen):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    update_pool(state, seen, cutoff)
    rate_stories(state["pool"])
    apply_signals(state["pool"])
    track_peaks(state["pool"], time.time())
    compute_thresholds(state)


def run(only=None):
    if not BOT_TOKEN or not CHANNEL_ID:
        sys.exit("Задайте переменные окружения BOT_TOKEN и CHANNEL_ID (см. README)")
    if not GEMINI_API_KEY:
        problem("no-gemini", "Не задан GEMINI_API_KEY: нет оценки важности и выжимок, "
                             "перевод через Google Translate.")
    state = load_state()
    seen = set(state["posted"])
    prepare(state, seen)
    now = time.time()
    show_top(state["pool"], now)

    if only:
        order = [c for c in CATEGORIES if c["key"] == only]
        if not order:
            sys.exit(f"Нет категории '{only}'. Доступны: {', '.join(c['key'] for c in CATEGORIES)}")
    else:
        start = state["next_category"] % len(CATEGORIES)
        order = CATEGORIES[start:] + CATEGORIES[:start]
        state["next_category"] = (start + 1) % len(CATEGORIES)

    for _ in range(6):  # повтор или статья не открылась — пробуем следующий сюжет
        pooled = choose(state["pool"], order, now)
        if pooled is None:
            print("Важных новостей сейчас нет — слот пропущен")
            break
        story = as_story(pooled)
        category = CATEGORY_BY_KEY[pooled["category"]]
        urgent = world_score(pooled, now) >= URGENT_SCORE
        repeat = is_repeat(pooled, state["recent"])
        if repeat:
            # Тот же сюжет уже был в канале. Есть новые факты — короткое обновление ответом на тот пост,
            # нет — выбрасываем. Запоминаем в обоих случаях, чтобы ловить следующие пересказы
            print(f"[повтор] {story['main']['source']}: {story['main']['title']}  ≈  {repeat['title']}")
            state["pool"].remove(pooled)
            retire(state, pooled)
            updated = maybe_update(state, category, story, repeat, urgent)
            remember(state, seen, story["members"], repeat.get("msg"))
            if updated:
                state["last_post"] = time.time()
                print(f"[{category['key']}] опубликовано обновление к посту {repeat['msg']}")
                break
            continue
        try:
            info = publish(category, story, urgent)
        except TranslationError as exc:
            # Переводчик недоступен — новость остаётся в очереди до следующего запуска
            problem("translate", f"Не получается перевести новости, посты отложены: {exc}")
            break
        except Exception as exc:
            if any(s in str(exc) for s in FATAL_TG_ERRORS):
                problem("telegram", f"Telegram не даёт публиковать в канал: {exc}. Проверьте, что бот — "
                                    f"администратор канала с правом «Публикация сообщений», и CHANNEL_ID.")
                break
            print(f"[!] {story['main']['link']}: {exc}")
            state["pool"].remove(pooled)
            retire(state, pooled)
            for member in story["members"]:
                mark_seen(state, seen, member)
            continue
        state["pool"].remove(pooled)
        retire(state, pooled)
        msg = info.pop("msg", None)
        remember(state, seen, story["members"], msg)
        state["last_post"] = time.time()
        state["digest"].append({"ts": time.time(), "category": category["key"], **info})
        extra = f" (+{len(story['members']) - 1} похожих)" if len(story["members"]) > 1 else ""
        print(f"[{category['key']}] опубликовано{' ⚡️ срочно' if urgent else ''} "
              f"({story_score(pooled, now):.1f} баллов): {story['main']['title']}{extra}")
        break

    quiet = (time.time() - state["last_post"]) / 3600
    if quiet >= QUIET_HOURS:
        problem("quiet", f"Уже {quiet:.0f} ч в канале нет ни одного поста. Если это не тихая ночь, "
                         f"загляните в журнал запусков на GitHub.")
    ensure_navigation(state)
    maybe_digest(state)
    send_alerts(state)
    save_state(state)


# ---------- проверки ----------

def digest_now():
    state = load_state()
    maybe_digest(state, force=True)
    send_alerts(state)
    save_state(state)


def test_alert():
    if not ADMIN_CHAT_ID:
        sys.exit("ADMIN_CHAT_ID не задан (см. README, раздел «Предупреждения»)")
    tg("sendMessage", chat_id=ADMIN_CHAT_ID,
       text="✅ Проверка связи: если что-то сломается, бот напишет сюда.")
    print("Пробное предупреждение отправлено")


def rank():
    state = load_state()
    seen = set(state["posted"])
    prepare(state, seen)
    show_top(state["pool"], time.time(), limit=30)
    save_state(state)


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
    items = [i for i in fetch_entries(CATEGORIES)[0] if i["date"] > cutoff]
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
    elif "--rank" in sys.argv:
        rank()
    elif "--digest" in sys.argv:
        digest_now()
    elif "--test-alert" in sys.argv:
        test_alert()
    elif "--loop" in sys.argv:
        while True:
            started = time.time()
            try:
                run()
            except Exception as exc:
                print(f"[!] ошибка запуска: {exc}")
                report_crash(exc)
            time.sleep(max(30, INTERVAL_MIN * 60 - (time.time() - started)))
    else:
        try:
            run(sys.argv[1] if len(sys.argv) > 1 else None)
        except Exception as exc:
            report_crash(exc)
            raise
