"""Поиск одинаковых новостей из разных источников, в том числе на разных языках.

Основной способ — сравнение по смыслу многоязычной моделью
paraphrase-multilingual-MiniLM-L12-v2 (через fastembed, ~220 МБ, скачивается один раз).
Она понимает, что «Peter Medak dies at 88» и «Умер Питер Медак» — одно и то же.
Если модель недоступна, работает упрощённое сравнение по словам, именам и числам.
"""
import os
import re
from functools import lru_cache

import numpy as np

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
THRESHOLD = float(os.getenv("DUP_THRESHOLD", "0.80"))  # одинаковые по смыслу
SOFT_THRESHOLD = float(os.getenv("DUP_SOFT_THRESHOLD", "0.65"))  # для разных языков + общие имена/числа
CACHE_DIR = os.getenv("MODEL_CACHE", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  ".cache", "fastembed"))

STOP = set("""
the a an and or of to in on for with at by from as is are was were be been it its this that these
those after before over into about than new says say said will can could how why what who when where
his her their our your not but has have had more most just one two first last watch review here
и в во на с со по к ко о об от до за из у не но что как это для при после над под или же ли бы его её
их она они он мы вы уже ещё также стал стала стали будет может
""".split())

_model = None
_model_failed = False


def _get_model():
    global _model, _model_failed
    if _model is None and not _model_failed:
        try:
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=CACHE_DIR)
        except Exception as exc:
            print(f"[!] модель для поиска дублей недоступна, сравниваю по словам: {exc}")
            _model_failed = True
    return _model


def story_text(title, summary):
    return f"{title}. {summary[:200]}".strip()


def _is_cyrillic(text):
    letters = re.findall(r"[a-zа-яё]", text.lower())
    return bool(letters) and sum(c >= "а" for c in letters) / len(letters) > 0.5


@lru_cache(maxsize=10000)
def anchors(text):
    """Имена собственные и числа — по ним видно, об одном ли событии речь.
    Берутся первые 5 букв, чтобы «Медак» и «Медака» совпадали."""
    nums = {n for n in re.findall(r"\d{2,}", text) if not re.fullmatch(r"(19|20)\d\d", n)}
    names = {w.lower()[:5] for w in re.findall(r"\b[A-ZА-ЯЁ][\w'’-]{2,}", text)
             if w.lower() not in STOP}
    return frozenset(nums | names)


@lru_cache(maxsize=10000)
def _words(text):
    return frozenset(w[:5] for w in re.findall(r"\w{3,}", text.lower()) if w not in STOP)


def _overlap(a, b):
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0, 0
    common = len(wa & wb)
    return common / min(len(wa), len(wb)), common


def same_story(sim, a, b, semantic):
    shared = len(anchors(a) & anchors(b))
    if _is_cyrillic(a) != _is_cyrillic(b):
        # Разные языки: смысл должен быть близок, и должны совпасть хотя бы два имени/числа
        if semantic:
            return sim >= 0.88 or (sim >= SOFT_THRESHOLD and shared >= 2)
        return shared >= 3
    if anchors(a) and anchors(b) and shared == 0:
        return False  # «ФРС сохранила ставку» и «Банк Англии сохранил ставку» — разные события
    if semantic and sim >= THRESHOLD:
        return True
    overlap, common = _overlap(a, b)
    return overlap >= 0.6 and common >= 4


def _embed(texts):
    model = _get_model()
    if model is None or not texts:
        return None
    vecs = np.array(list(model.embed(texts)), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    return vecs


def similarity(a_texts, b_texts):
    """Матрица сходства len(a) × len(b) и признак, что сравнение смысловое."""
    if not a_texts or not b_texts:
        return np.zeros((len(a_texts), len(b_texts))), True
    vecs = _embed(list(a_texts) + list(b_texts))
    if vecs is not None:
        return vecs[:len(a_texts)] @ vecs[len(a_texts):].T, True
    return np.array([[_overlap(a, b)[0] for b in b_texts] for a in a_texts]), False


def group_stories(texts):
    """Объединяет одинаковые сюжеты. Возвращает список групп индексов."""
    sims, semantic = similarity(texts, texts)
    groups, assigned = [], [False] * len(texts)
    for i in range(len(texts)):
        if assigned[i]:
            continue
        group, assigned[i] = [i], True
        for j in range(i + 1, len(texts)):
            if not assigned[j] and same_story(sims[i, j], texts[i], texts[j], semantic):
                group.append(j)
                assigned[j] = True
        groups.append(group)
    return groups


def find_known(texts, known_texts):
    """Для каждого текста — индекс такой же уже опубликованной новости или None."""
    if not known_texts:
        return [None] * len(texts)
    sims, semantic = similarity(texts, known_texts)
    result = []
    for i, text in enumerate(texts):
        match = None
        for j in np.argsort(-sims[i])[:5]:
            if same_story(sims[i, j], text, known_texts[j], semantic):
                match = int(j)
                break
        result.append(match)
    return result
