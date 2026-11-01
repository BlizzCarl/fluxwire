# Категории публикуются по кругу. При запуске раз в 10 минут
# каждая из 7 категорий получает слот примерно раз в 70 минут.
# Только зарубежные источники: всё переводится на русский автоматически.
# Ссылки на RSS иногда меняются: нерабочие источники бот просто пропускает
# (смотрите строки с [!] в логах), а если лента молчит 3 часа подряд — пишет вам в личку.
# Проверить все сразу: python bot.py --check-feeds

# Ссылки и заголовки с этими фрагментами пропускаются: видео-страницы, онлайн-трансляции,
# фотогалереи, колонки мнений, служебные публикации, рассылки, реклама мероприятий.
# У категории можно задать свой дополнительный список "skip_url".
SKIP_URL = ["/video/", "/videos/", "/watch/", "/live/", "/live-blog/", "/gallery/",
            "/podcast", "/commentisfree/", "/bitesize/", "/sounds/", "scores-schedule"]
SKIP_TITLE = ["author correction", "publisher correction", "daily briefing",
              "save up to", "podcast", "sign up for", "quiz"]
# Рекламные материалы под видом статей (например, у MIT Technology Review)
SKIP_TEXT = ["this content was produced by", "sponsored content", "paid content"]

# Новости про ИИ из общих лент технологий уходят в рубрику «Искусственный интеллект»
AI_PATTERN = (r"\b(ai|a\.i\.|artificial intelligence|openai|chatgpt|gpt-?\d\w*|gemini|claude|"
              r"anthropic|deepmind|copilot|llms?|large language models?|chatbots?|machine learning|"
              r"grok|xai|mistral|deepseek|midjourney|sora|agi|superintelligence|ии|нейросет\w*)\b")

CATEGORIES = [
    {
        "key": "world",
        "title": "Политика и мир",
        "emoji": "🌍",
        "tag": "политика",
        "skip_url": ["/sport/", "/sports/"],  # спорт идёт в свою категорию
        "feeds": [
            {"name": "BBC News", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
            {"name": "The Guardian", "url": "https://www.theguardian.com/world/rss"},
            {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
            {"name": "Politico", "url": "https://rss.politico.com/politics-news.xml"},
        ],
    },
    {
        "key": "science",
        "title": "Наука и медицина",
        "emoji": "🔬",
        "tag": "наука",
        "feeds": [
            {"name": "Quanta Magazine", "url": "https://www.quantamagazine.org/feed/"},
            {"name": "ScienceDaily", "url": "https://www.sciencedaily.com/rss/all.xml"},
            {"name": "Phys.org", "url": "https://phys.org/rss-feed/"},
            {"name": "ScienceAlert", "url": "https://www.sciencealert.com/feed"},
            {"name": "Nature", "url": "https://www.nature.com/nature.rss"},
            # Медицина: только главные новости дня, без потока мелких исследований
            {"name": "Medical Xpress", "url": "https://medicalxpress.com/rss-feed/breaking/"},
        ],
    },
    {
        "key": "tech",
        "title": "Технологии",
        "emoji": "💻",
        "tag": "технологии",
        "move": {"to": "ai", "pattern": AI_PATTERN},
        "feeds": [
            {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
            {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
            {"name": "TechCrunch", "url": "https://techcrunch.com/feed/"},
            {"name": "Wired", "url": "https://www.wired.com/feed/rss"},
            {"name": "MIT Technology Review", "url": "https://www.technologyreview.com/feed/"},
        ],
    },
    {
        "key": "ai",
        "title": "Искусственный интеллект",
        "emoji": "🤖",
        "tag": "ии",
        "feeds": [
            {"name": "The Verge AI", "url": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"},
            {"name": "MIT Technology Review AI",
             "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed/"},
            {"name": "OpenAI", "url": "https://openai.com/news/rss.xml"},
            {"name": "Google AI", "url": "https://blog.google/innovation-and-ai/technology/ai/rss/"},
        ],
    },
    {
        "key": "games",
        "title": "Игры",
        "emoji": "🎮",
        "tag": "игры",
        "feeds": [
            {"name": "IGN", "url": "https://feeds.ign.com/ign/all"},
            {"name": "Eurogamer", "url": "https://www.eurogamer.net/feed"},
            {"name": "PC Gamer", "url": "https://www.pcgamer.com/rss/"},
            {"name": "Polygon", "url": "https://www.polygon.com/rss/index.xml"},
            {"name": "GamesIndustry.biz", "url": "https://www.gamesindustry.biz/feed"},
        ],
    },
    {
        "key": "movies",
        "title": "Кино и сериалы",
        "emoji": "🎬",
        "tag": "кино",
        "feeds": [
            {"name": "Variety", "url": "https://variety.com/feed/"},
            {"name": "Deadline", "url": "https://deadline.com/feed/"},
            {"name": "The Hollywood Reporter", "url": "https://www.hollywoodreporter.com/feed/"},
            {"name": "IndieWire", "url": "https://www.indiewire.com/feed/"},
        ],
    },
    {
        "key": "sport",
        "title": "Спорт",
        "emoji": "⚽",
        "tag": "спорт",
        "feeds": [
            # Футбол
            {"name": "BBC Sport", "url": "https://feeds.bbci.co.uk/sport/football/rss.xml"},
            {"name": "The Guardian Football", "url": "https://www.theguardian.com/football/rss"},
            {"name": "ESPN FC", "url": "https://www.espn.com/espn/rss/soccer/news"},
            # Бокс
            {"name": "BBC Sport Boxing", "url": "https://feeds.bbci.co.uk/sport/boxing/rss.xml"},
            {"name": "The Guardian Boxing", "url": "https://www.theguardian.com/sport/boxing/rss"},
            # Теннис
            {"name": "Sky Sports Tennis", "url": "https://www.skysports.com/rss/12110"},
            {"name": "BBC Sport Tennis", "url": "https://feeds.bbci.co.uk/sport/tennis/rss.xml"},
            {"name": "The Guardian Tennis", "url": "https://www.theguardian.com/sport/tennis/rss"},
        ],
    },
]
