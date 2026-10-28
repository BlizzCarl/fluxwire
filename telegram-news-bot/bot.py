"""Новостной бот: собирает свежие статьи из RSS, отсеивает одинаковые сюжеты
и публикует в Telegram-канал только важное.

Как выбирается новость:
  1. Все статьи попадают в очередь «созревания». Одинаковые сюжеты из разных изданий
     склеиваются в один.
  2. Gemini оценивает важность каждого сюжета от 1 до 10, как выпускающий редактор.
  3. Каждый запуск бот проверяет популярность: сколько изданий подхватило сюжет, есть ли он
     в Google Trends, на главной Hacker News, в топах самых читаемых NYT и Guardian.
  4. Новость выходит, когда «созрела» (прошло HOLD_MINUTES с публикации) и набрала MIN_SCORE.
     Срочное (BREAKING_SCORE) выходит сразу. Не набравшее баллов за POOL_HOURS — выбрасывается.
  5. Рубрики идут по кругу; если в рубрике нет важного, берётся важное из следующей.

Запуск:
    python bot.py                — один пост: для GitHub Actions / cron
    python bot.py games          — один пост только из этой категории (ключ из feeds.py)
    python bot.py --loop         — работать постоянно, пост раз в INTERVAL_MIN минут (сервер, Docker)
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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import numpy as np
import requests
import trafilatura
from bs4 import BeautifulSoup

import dedup
import popularity
from feeds import CATEGORIES, SKIP_TITLE, SKIP_URL

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
TRANSLATE_TO = os.getenv("TRANSLATE_TO", "ru").strip()  # пусто — не переводить
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()  # переводчик и редактор-нейросеть
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
FULL_TEXT = os.getenv("FULL_TEXT", "1") == "1"  # полный перевод статьи в Telegraph по кнопке
MAX_ARTICLE_CHARS = int(os.getenv("MAX_ARTICLE_CHARS", "15000"))  # длиннее — в Telegraph идёт начало
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "12"))  # старее — не берём в очередь
POOL_HOURS = int(os.getenv("POOL_HOURS", "14"))  # сколько сюжет ждёт в очереди, потом выбрасывается
HOLD_MINUTES = int(os.getenv("HOLD_MINUTES", "90"))  # «созревание»: столько ждём перепечаток и топов
MIN_SCORE = float(os.getenv("MIN_SCORE", "9"))  # минимальная оценка для публикации
BREAKING_SCORE = float(os.getenv("BREAKING_SCORE", "13"))  # с такой оценкой — сразу, без ожидания
DEDUP_HOURS = int(os.getenv("DEDUP_HOURS", "48"))  # сколько помнить сюжеты для отсева дублей
INTERVAL_MIN = int(os.getenv("INTERVAL_MIN", "10"))
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


# ---------- состояние (что уже опубликовано и что ждёт в очереди) ----------

def load_state():
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    state.setdefault("posted", [])   # ключи ссылок, которые уже видели
    state.setdefault("recent", [])   # недавние опубликованные сюжеты для поиска дублей
    state.setdefault("pool", [])     # очередь «созревания»
    state.setdefault("next_category", 0)
    return state


def save_state(state):
    border = time.time() - DEDUP_HOURS * 3600
    state["recent"] = [r for r in state["recent"] if r["ts"] > border][-2000:]
    state["posted"] = state["posted"][-8000:]
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


def pack(item):
    data = dict(item)
    data["date"] = item["date"].timestamp() if item["date"] else None
    return data


def unpack(data):
    item = dict(data)
    item["date"] = datetime.fromtimestamp(data["date"], timezone.utc) if data.get("date") else None
    return item


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


def fetch_feed(category, source):
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as exc:
        print(f"[!] {source['name']}: {exc}")
        return []
    items = []
    for entry in feed.entries[:20]:
        link, title = entry.get("link"), clean_text(entry.get("title"))
        if not link or not title or skip_entry(link, title, category.get("skip_url", [])):
            continue
        ts = entry.get("published_parsed") or entry.get("updated_parsed")
        summary = clean_text(entry.get("summary"))
        items.append({
            "category": category["key"],
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


def fetch_entries(categories):
    """Все ленты читаются параллельно."""
    jobs = [(c, s) for c in categories for s in c["feeds"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda job: fetch_feed(*job), jobs)
    return [item for items in results for item in items]


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


# ---------- Gemini ----------

class TranslationError(Exception):
    pass


def gemini(prompt, schema):
    """Запрос к Gemini с JSON-ответом по схеме. Ключ бесплатный: https://aistudio.google.com/apikey"""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            # Строгая схема, иначе модель иногда ломает JSON на длинных текстах
            "responseSchema": schema,
            "temperature": 0.2,
            "maxOutputTokens": 65536,
        },
    }
    error = None
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY}, json=body, timeout=120,
            )
        except requests.RequestException as exc:
            error = f"Gemini: {exc}"
            continue
        if resp.status_code != 200:
            error = f"Gemini {resp.status_code}: {resp.text[:300]}"
            if resp.status_code in (400, 401, 403, 404):  # ключ или модель неверные — повтор не поможет
                break
            continue
        try:
            return json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, ValueError) as exc:
            error = f"Gemini вернул испорченный ответ: {exc}"
    raise TranslationError(error)


# ---------- оценка важности ----------

RATE_PROMPT = """Ты — выпускающий редактор международного новостного Telegram-канала \
(политика и мир, бизнес, наука, технологии и ИИ, игры, кино и сериалы, спорт: футбол, бокс, теннис).
Оцени каждую новость по шкале от 1 до 10: насколько она важна и интересна широкой аудитории.
10 — событие мирового масштаба, о котором говорят все.
8–9 — крупная новость, которую обсуждают во всём мире или во всей отрасли: громкий анонс \
или релиз, большая сделка, важное открытие, крупный трансфер, титульный бой, финал турнира.
5–7 — заметная новость своей отрасли.
3–4 — рядовая заметка.
1–2 — проходное: обзоры, подборки, гайды, скидки, слухи без источника, колонки мнений, \
мелкие локальные новости.
Ответь JSON-массивом целых чисел той же длины и в том же порядке, что и список.

"""


def rate_stories(pool):
    """Gemini оценивает важность новых сюжетов, пачками по 60."""
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
            scores = gemini(RATE_PROMPT + "\n".join(lines), {"type": "ARRAY", "items": {"type": "INTEGER"}})
            if not isinstance(scores, list) or len(scores) != len(batch):
                raise TranslationError("ответ не той длины")
            for story, value in zip(batch, scores):
                story["importance"] = max(1, min(10, int(value)))
        except Exception as exc:
            print(f"[!] не удалось оценить важность: {exc}")
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


def eligible(story, now):
    rated = story["importance"] is not None or story.get("rate_tries", 0) >= 3 or not GEMINI_API_KEY
    if not rated:
        return False
    score = story_score(story, now)
    matured = now - story_start(story) >= HOLD_MINUTES * 60
    return score >= MIN_SCORE and (matured or score >= BREAKING_SCORE)


# ---------- очередь «созревания» ----------

def update_pool(state, seen, cutoff):
    pool = state["pool"]
    in_pool = {link_key(m["link"]) for s in pool for m in s["members"]}
    items = {}
    for item in fetch_entries(CATEGORIES):
        key = link_key(item["link"])
        if key not in seen and key not in in_pool and (item["date"] is None or item["date"] > cutoff):
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
                     "rate_tries": 0, "signals": {}, "members": [pack(m) for m in members]})

    # 4. Не «выстрелившее» за POOL_HOURS — выбрасываем
    keep = []
    for story in pool:
        if now - story_start(story) > POOL_HOURS * 3600:
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
        print(f"  {mark} {story_score(story, now):5.1f}  важность {story['importance']}  "
              f"изданий {sources}  {story['signals'] or ''}  {age:.1f} ч  "
              f"[{story['category']}] {story['members'][0]['title']}")


def choose(pool, order, now):
    """Лучший готовый сюжет из первой по кругу рубрики, где есть важное."""
    for category in order:
        candidates = [s for s in pool if s["category"] == category["key"] and eligible(s, now)]
        if candidates:
            return max(candidates, key=lambda s: story_score(s, now))
    return None


# ---------- перевод ----------

def is_russian(text, share=0.5):
    letters = re.findall(r"[a-zа-яё]", text.lower())
    return not letters or sum(c >= "а" for c in letters) / len(letters) > share


def _gemini_translate(parts):
    """Перевод нейросетью Gemini: один запрос на всю статью, качество как у редактора."""
    prompt = (
        f"Переведи на язык «{TRANSLATE_TO}» каждую строку из JSON-массива. Это новостная статья: "
        "пиши естественно, в стиле качественного новостного издания, без пояснений и сокращений. "
        "Имена, названия компаний, игр и фильмов передавай так, как их принято писать в русских СМИ. "
        "Ответь только JSON-массивом строк той же длины и в том же порядке.\n\n"
        + json.dumps(parts, ensure_ascii=False)
    )
    result = gemini(prompt, {"type": "ARRAY", "items": {"type": "STRING"}})
    if isinstance(result, list) and len(result) == len(parts):
        return [str(r) for r in result]
    raise TranslationError("Gemini вернул ответ не того формата")


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
Сделай по статье ниже JSON с полями (всё на русском):
title — понятный заголовок до 100 символов. Сразу ясно, о ком или о чём речь: \
не «Альтман ответил критикам», а «Глава OpenAI Сэм Альтман ответил на обвинения в…». \
Малоизвестных людей, компании, игры, команды поясняй парой слов. Без кликбейта и вопросов.
lead — суть в 2–3 коротких предложениях, до 320 символов: что случилось и почему это важно. \
Без воды, вводных слов и повтора заголовка.
points — 0–3 ключевых факта (цифры, сроки, последствия), каждый до 100 символов. \
Не повторяй lead. Если добавить нечего — пустой массив.
paragraphs — полный перевод абзацев статьи, столько же элементов и в том же порядке. \
Пиши естественно, в стиле качественного издания. Имена и названия — как принято в русских СМИ.

"""

DIGEST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "lead": {"type": "STRING"},
        "points": {"type": "ARRAY", "items": {"type": "STRING"}},
        "paragraphs": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["title", "lead", "points", "paragraphs"],
}


def first_sentences(text, limit=320):
    out = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text.strip()):
        if out and len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    return shorten(out, limit)


def digest(item, summary, paragraphs):
    """Заголовок, суть в 2–3 предложениях, ключевые факты и полный перевод — одним запросом."""
    if GEMINI_API_KEY and TRANSLATE_TO:
        article = {"title": item["title"], "summary": summary, "paragraphs": paragraphs}
        try:
            data = gemini(DIGEST_PROMPT + json.dumps(article, ensure_ascii=False), DIGEST_SCHEMA)
            title, lead = str(data["title"]).strip(), str(data["lead"]).strip()
            points = [str(p).strip() for p in data.get("points", []) if str(p).strip()][:3]
            if title and lead and (TRANSLATE_TO != "ru" or is_russian(f"{title} {lead}", share=0.3)):
                full = [str(p) for p in data.get("paragraphs", [])]
                if paragraphs and len(full) < len(paragraphs) * 0.8:
                    try:  # перевод абзацев потерялся — переводим их отдельно
                        full = translate(paragraphs, item["lang"])
                    except TranslationError as exc:
                        print(f"[!] полный текст не переведён, пост без «Читать полностью»: {exc}")
                        full = []
                return {"title": title, "lead": lead, "points": points, "paragraphs": full}
            print("[!] выжимка вернулась не того формата, перевожу без неё")
        except Exception as exc:
            print(f"[!] не удалось сделать выжимку, перевожу без неё: {exc}")
    # Запасной вариант: обычный перевод, суть — первые предложения описания
    translated = translate([item["title"], summary, *paragraphs], item["lang"])
    return {"title": translated[0], "lead": first_sentences(translated[1]), "points": [],
            "paragraphs": translated[2:]}


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


def take_chars(paragraphs, limit):
    """Первые целые абзацы, укладывающиеся в limit символов."""
    out, size = [], 0
    for paragraph in paragraphs:
        if out and size + len(paragraph) > limit:
            break
        out.append(paragraph)
        size += len(paragraph)
    return out


def build_post(category, post, sources, limit):
    """Короткий пост: заголовок, суть, до трёх фактов, источники и рубрика. Без ссылок в тексте."""
    head = f"{category['emoji']} <b>{html.escape(shorten(post['title'], 160))}</b>\n\n"
    names = " · ".join(sources[:4])
    tail = f"\n\n<i>{html.escape(names)}</i>  #{category['tag']}"
    lead, points = post["lead"], list(post["points"])
    while True:
        body = html.escape(lead)
        if points:
            body += "\n\n" + "\n".join(f"▪️ {html.escape(p)}" for p in points)
        if len(head) + len(body) + len(tail) <= limit or (not points and len(lead) < 120):
            return head + body + tail
        if points:
            points.pop()  # сначала убираем факты, суть оставляем
        else:
            lead = shorten(lead, len(lead) - 40)


def keyboard(page, sources):
    rows = [[{"text": "📖 Читать полностью", "url": page}]] if page else []
    buttons = [{"text": f"🔗 {name}", "url": link} for name, link in sources[:4]]
    rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return json.dumps({"inline_keyboard": rows})


def send_post(category, post, sources, image, markup):
    names = [name for name, _ in sources]
    if image:
        try:
            # Сверху — только картинка; подпись к фото до 1024 символов
            tg("sendPhoto", photo=image, parse_mode="HTML", reply_markup=markup,
               caption=build_post(category, post, names, 1024))
            return
        except Exception as exc:
            if any(s in str(exc) for s in FATAL_TG_ERRORS):
                raise
            print(f"[!] не удалось отправить с фото, отправляю текстом: {exc}")
    tg("sendMessage", parse_mode="HTML", reply_markup=markup,
       text=build_post(category, post, names, 1500),
       link_preview_options=json.dumps({"is_disabled": True}))


def publish(category, story):
    item = story["main"]
    sources = {item["source"]: item["link"]}
    for member in story["members"]:
        sources.setdefault(member["source"], member["link"])
    sources = list(sources.items())

    article = fetch_article(item["link"])
    paragraphs = article.get("paragraphs", [])
    if sum(map(len, item["rss_paragraphs"])) > sum(map(len, paragraphs)):
        paragraphs = item["rss_paragraphs"]
    paragraphs = take_chars(paragraphs, MAX_ARTICLE_CHARS) if FULL_TEXT else []
    image = item["image"] or article.get("image")

    summary = item["summary"]
    if len(summary) < 80:
        summary = article.get("description") or " ".join(paragraphs[:2]) or summary
    post = digest(item, summary, paragraphs)

    # Полный перевод — в Telegraph, по кнопке «Читать полностью»
    page = None
    if len(post["paragraphs"]) >= 3:
        page = telegraph_page(post["title"], post["paragraphs"], image, item)
    send_post(category, post, sources, image, keyboard(page, sources))


def as_story(pooled):
    members = [unpack(m) for m in pooled["members"]]
    main = max(members, key=lambda i: (i["lang"] == TRANSLATE_TO, bool(i["image"]),
                                       len(i["summary"]), i["date"] or EPOCH))
    return {"main": main, "members": members}


def prepare(state, seen):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    update_pool(state, seen, cutoff)
    rate_stories(state["pool"])
    apply_signals(state["pool"])


def run(only=None):
    if not BOT_TOKEN or not CHANNEL_ID:
        sys.exit("Задайте переменные окружения BOT_TOKEN и CHANNEL_ID (см. README)")
    if not GEMINI_API_KEY:
        print("[!] GEMINI_API_KEY не задан — нет оценки важности, перевод через Google Translate")
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

    for _ in range(3):  # если статья не открылась, пробуем следующий сюжет
        pooled = choose(state["pool"], order, now)
        if pooled is None:
            print("Важных новостей сейчас нет — слот пропущен")
            break
        story = as_story(pooled)
        category = CATEGORY_BY_KEY[pooled["category"]]
        try:
            publish(category, story)
        except TranslationError as exc:
            # Переводчик недоступен — новость остаётся в очереди до следующего запуска
            print(f"[!] не удалось перевести, новость отложена до следующего запуска: {exc}")
            break
        except Exception as exc:
            if any(s in str(exc) for s in FATAL_TG_ERRORS):
                print(f"[!] Telegram: {exc}\n    Сделайте бота администратором канала "
                      "с правом «Публикация сообщений» и проверьте CHANNEL_ID (например, @fluxwirenews)")
                break
            print(f"[!] {story['main']['link']}: {exc}")
            state["pool"].remove(pooled)
            for member in story["members"]:
                mark_seen(state, seen, member)
            continue
        state["pool"].remove(pooled)
        remember(state, seen, story["members"])
        extra = f" (+{len(story['members']) - 1} похожих)" if len(story["members"]) > 1 else ""
        print(f"[{category['key']}] опубликовано ({story_score(pooled, now):.1f} баллов): "
              f"{story['main']['title']}{extra}")
        break
    save_state(state)


# ---------- проверки ----------

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
    items = [i for i in fetch_entries(CATEGORIES) if i["date"] is None or i["date"] > cutoff]
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
