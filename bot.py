import asyncio
import base64
import html
import io
import json
import logging
import os
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import quote_plus
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaDocument, Message,
)
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

PRIMARY_DARKAPI_KEY = (os.getenv("DARKAPI_KEY") or "").strip()
PRIMARY_DARKAPI_URL = "https://darkapi.shop/v1"
ENV_BACKUP_KEY = (os.getenv("API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()
ENV_BASE_URL = (os.getenv("AI_BASE_URL") or "").strip()   # адрес любого OpenAI-совместимого API
ENV_MODEL = (os.getenv("AI_MODEL") or "").strip()         # точное имя модели для ответов
ENV_ANALYZER_MODEL = (os.getenv("AI_ANALYZER_MODEL") or "").strip()  # лёгкая модель (необязательно)

BOT_NAMES = ["фил", "филл", "филя", "филип", "барбос"]

LEARN_EVERY_N_MESSAGES = 40
SPAM_LIMIT_MESSAGES = 14
PROACTIVE_MIN_MSGS = 30
PROACTIVE_MAX_MSGS = 45
SILENCE_HOURS_TO_REVIVE = 18
POLL_EVENT_MIN_HOURS = 14

BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0.0.0 Safari/537.36"}
API_KEY = PRIMARY_DARKAPI_KEY or ENV_BACKUP_KEY or "none"
BASE_URL = PRIMARY_DARKAPI_URL
ai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, default_headers=BROWSER_HEADERS)

# Приоритет Qwen, а при лимите — переключение на DeepSeek / Claude
PREFERRED_PRIMARY_MODELS = ["deepseek-v4-pro", "deepseek", "qwen", "claude-sonnet"]
PREFERRED_FALLBACK_MODELS = ["deepseek-v4-pro", "deepseek", "claude-sonnet-5.5", "glm-5.3", "gpt-oss-120b"]

CHAT_MODEL = "deepseek-v4-pro"
ANALYZER_MODEL = "deepseek-v4.1-flash"
VISION_MODEL = ""
AVAILABLE_MODELS = []
BAD_MODELS = ("allam", "whisper", "orpheus", "guard", "tts", "embedding", "safeguard", "arabic")
DARKAPI_FALLBACK_MODELS = [
    "qwen/qwen3.8-27b", "deepseek-v4-pro", "deepseek-v4.1-flash",
    "claude-sonnet-5.5", "claude-haiku-5.5", "glm-5.3", "gpt-6.1-sol",
]

# Запрещенные кринж-слова, которые застряли со старых версий
BANNED_CRINGE_WORDS = (
    "скуфедяй", "скуф", "блеет", "блеять", "сисек-месседж",
    "амкарск", "пермск", "деградант", "на кайфу", "Лилл", "лилль",
)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

chat_history = defaultdict(lambda: deque(maxlen=16))
user_only_history = defaultdict(lambda: deque(maxlen=10))
message_counters = defaultdict(int)
proactive_counters = defaultdict(int)
next_proactive_target = defaultdict(lambda: random.randint(PROACTIVE_MIN_MSGS, PROACTIVE_MAX_MSGS))
last_activity_time = defaultdict(lambda: time.time())
last_poll_time = defaultdict(lambda: time.time())
revive_sent = defaultdict(bool)
last_sent_media_uid = defaultdict(str)
last_reply_had_blya = defaultdict(bool)
call_timestamps = defaultdict(list)
muted_until = defaultdict(float)

admin_state = {"action": None, "target_chat": None}
MEMORY_FILE = "brain.json"
brain = {"groups": {}, "selected_model": None, "api_keys": []}

NEWS_TRIGGERS = (
    "слышал", "знаешь че", "знаешь чё", "новост", "в мире", "случилось",
    "произошло", "в курсе", "че там с", "чё там с",
)


def detect_url_for_key(key: str) -> str:
    k = key.strip()
    if ENV_BASE_URL and k == ENV_BACKUP_KEY:
        return ENV_BASE_URL
    if k.startswith("tc_"):
        return "https://darkapi.shop/v1"
    if k.startswith("gsk_"):
        return "https://api.groq.com/openai/v1"
    if k.startswith("sk-or-"):
        return "https://openrouter.ai/api/v1"
    return "https://api.deepseek.com"


def get_key_pool() -> list[dict]:
    pool, seen = [], set()
    raw_keys = [PRIMARY_DARKAPI_KEY, ENV_BACKUP_KEY] + brain.get("api_keys", [])
    for k in raw_keys:
        kc = k.strip()
        if kc and kc not in seen:
            seen.add(kc)
            pool.append({"key": kc, "url": detect_url_for_key(kc)})
    return pool


async def init_ai_models(force_key: str | None = None):
    global API_KEY, BASE_URL, CHAT_MODEL, ANALYZER_MODEL, VISION_MODEL, AVAILABLE_MODELS, ai_client
    pool = get_key_pool()
    if force_key:
        pool = [{"key": force_key, "url": detect_url_for_key(force_key)}] + [p for p in pool if p["key"] != force_key]

    connected = False
    for item in pool:
        k, url = item["key"], item["url"]
        try:
            test_client = AsyncOpenAI(api_key=k, base_url=url, default_headers=BROWSER_HEADERS)
            models_resp = await test_client.models.list()
            clean_ids = [m.id for m in models_resp.data if not any(b in m.id.lower() for b in BAD_MODELS)]
            if clean_ids:
                API_KEY, BASE_URL, ai_client = k, url, test_client
                AVAILABLE_MODELS = sorted(clean_ids)
                connected = True
                break
        except Exception as e:
            logging.warning(f"Проверка {url}: {e}")
            if k.startswith("tc_"):
                logging.warning("darkapi не отдал список моделей, беру запасной список (сервер может быть недоступен)")
                API_KEY, BASE_URL = k, url
                ai_client = AsyncOpenAI(api_key=k, base_url=url, default_headers=BROWSER_HEADERS)
                AVAILABLE_MODELS = DARKAPI_FALLBACK_MODELS.copy()
                connected = True
                break

    if not connected:
        logging.error("НИ ОДИН API-КЛЮЧ НЕ ПОДОШЁЛ: проверь DARKAPI_KEY / API_KEY в Environment")
        return

    saved = brain.get("selected_model")
    if saved and saved in AVAILABLE_MODELS and not any(b in saved.lower() for b in BAD_MODELS):
        CHAT_MODEL = saved
    else:
        for pref in PREFERRED_PRIMARY_MODELS:
            found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
            if found:
                CHAT_MODEL = found
                break
        else:
            CHAT_MODEL = AVAILABLE_MODELS[0]
        brain["selected_model"] = CHAT_MODEL

    for pref in ["deepseek-v4.1-flash", "flash", "haiku", "luna", "glm", "deepseek"]:
        found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
        if found:
            ANALYZER_MODEL = found
            break
    else:
        ANALYZER_MODEL = CHAT_MODEL

    for pref in ["sonnet", "haiku", "claude", "scout", "vision", "vl", "gpt-6"]:
        found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
        if found:
            VISION_MODEL = found
            break
    else:
        VISION_MODEL = CHAT_MODEL

    if not ENV_MODEL and "darkapi" in BASE_URL and "deepseek-v4-pro" in AVAILABLE_MODELS:
        CHAT_MODEL = "deepseek-v4-pro"
        brain["selected_model"] = CHAT_MODEL

    if ENV_MODEL:  # модель из переменных окружения важнее автовыбора
        CHAT_MODEL = ENV_MODEL
        ANALYZER_MODEL = ENV_ANALYZER_MODEL or ENV_MODEL
        VISION_MODEL = ENV_MODEL
        brain["selected_model"] = CHAT_MODEL
    logging.info(f"AI подключён: {BASE_URL} | чат: {CHAT_MODEL} | анализ: {ANALYZER_MODEL} | моделей в списке: {len(AVAILABLE_MODELS)}")


async def switch_to_next_key_on_limit() -> bool:
    pool = get_key_pool()
    other = [p["key"] for p in pool if p["key"] != API_KEY]
    if not other:
        return False
    await init_ai_models(force_key=other[0])
    return True


async def switch_to_deepseek_on_limit() -> bool:
    global CHAT_MODEL
    if "qwen" in CHAT_MODEL.lower():
        for m in AVAILABLE_MODELS:
            if "deepseek" in m.lower() and "image" not in m.lower():
                CHAT_MODEL = m
                logging.warning(f"Лимит Qwen! Переключился на DeepSeek: {CHAT_MODEL}")
                return True
    if await switch_to_next_key_on_limit():
        for m in AVAILABLE_MODELS:
            if "deepseek" in m.lower() and "image" not in m.lower():
                CHAT_MODEL = m
                break
        return True
    return False


def sanitize_group_memory(g: dict):
    """Удаляет заражённый старый сленг и мемы ('скуфедяй', 'блеет', 'амкарский' и т.д.) из базы."""
    g["vibe"] = "Обычный дружеский телеграм-чат пацанов."
    g["slang"] = []
    clean_memes = [
        m for m in g.get("memes", [])
        if not any(bw.lower() in m.lower() for bw in BANNED_CRINGE_WORDS)
    ]
    g["memes"] = clean_memes[-6:]
    clean_users = {}
    for k, v in g.get("users", {}).items():
        if not any(bw.lower() in str(v).lower() for bw in BANNED_CRINGE_WORDS):
            clean_users[k] = v
    g["users"] = clean_users


def get_group_data(chat_id: int, title: str = "Группа") -> dict:
    cid = str(chat_id)
    if cid not in brain["groups"]:
        brain["groups"][cid] = {
            "title": title, "mode": "normal",
            "vibe": "Обычный дружеский телеграм-чат пацанов.",
            "slang": [], "users": {}, "memes": [], "stickers": [], "gifs": [], "known_tags": [],
        }
    g = brain["groups"][cid]
    if title and title != "Группа":
        g["title"] = title
    for k, v in [
        ("mode", "normal"), ("vibe", "Обычный дружеский телеграм-чат пацанов."),
        ("slang", []), ("users", {}), ("memes", []),
        ("stickers", []), ("gifs", []), ("known_tags", []),
    ]:
        g.setdefault(k, v)
    sanitize_group_memory(g)
    return g


async def load_brain():
    global brain
    if ADMIN_ID:
        try:
            chat = await bot.get_chat(ADMIN_ID)
            if chat.pinned_message and chat.pinned_message.document and chat.pinned_message.document.file_name == "brain.json":
                buf = io.BytesIO()
                await bot.download(chat.pinned_message.document.file_id, destination=buf)
                brain.update(json.loads(buf.getvalue().decode("utf-8")))
                brain.setdefault("api_keys", [])
                for cid in brain.get("groups", {}):
                    sanitize_group_memory(brain["groups"][cid])
                return
        except Exception:
            pass
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                brain.update(json.load(f))
                brain.setdefault("api_keys", [])
                for cid in brain.get("groups", {}):
                    sanitize_group_memory(brain["groups"][cid])
        except Exception:
            pass


async def save_brain():
    raw_bytes = json.dumps(brain, ensure_ascii=False, indent=2).encode("utf-8")
    with open(MEMORY_FILE, "wb") as f:
        f.write(raw_bytes)
    if not ADMIN_ID:
        return
    try:
        chat = await bot.get_chat(ADMIN_ID)
        input_file = BufferedInputFile(raw_bytes, filename="brain.json")
        caption = f"🧠 Память Фила | Модель: {CHAT_MODEL}\nЖми /panel или /code"
        if chat.pinned_message and chat.pinned_message.from_user.id == (await bot.get_me()).id and chat.pinned_message.document:
            await bot.edit_message_media(chat_id=ADMIN_ID, message_id=chat.pinned_message.message_id, media=InputMediaDocument(media=input_file, caption=caption))
        else:
            msg = await bot.send_document(ADMIN_ID, document=input_file, caption=caption)
            await bot.pin_chat_message(ADMIN_ID, msg.message_id, disable_notification=True)
    except Exception:
        pass


async def is_admin_in_group(chat_id: int) -> bool:
    if not ADMIN_ID:
        return True
    try:
        m = await bot.get_chat_member(chat_id, ADMIN_ID)
        return m.status in ("creator", "administrator", "member", "restricted")
    except Exception:
        return False


LOOKUP_RE = re.compile(
    r"(что такое|кто такой|кто такая|кто такие|что за |что значит|что означает|как работает|как называется|"
    r"когда (выйд|будет|был|вышл|начал|закончил|родил)|сколько (стоит|лет|людей|народу|осталось)|правда (ли|что)|"
    r"что (случилось|произошло|нового|происходит)|что там с|чё там с|че там с|новост|слышал|знаешь|в курсе|"
    r"войн|мирова|в каком году|санкци|выбор|курс (доллар|евро|бит|рубл|тенге)|тесл|маск|летающ|нейросет|айфон|релиз|"
    r"кто выиграл|кто победил|чемпион|трансфер|счет матч|результат матч)", re.I)

_lookup_cache: dict[str, tuple[float, str]] = {}


def _strip_query(text: str) -> str:
    q = re.sub(r"\b(фил|филл|филя|барбос|слышал|знаешь|че|чё|там|про|новость|новости|случилось|произошло|в|мире|скажи|расскажи|а|ну)\b", "", text, flags=re.I)
    return re.sub(r"\s+", " ", q).strip(" ?!.,")


async def build_search_query(text: str) -> str:
    fallback = _strip_query(text)
    try:
        prompt = ("Вытащи из сообщения короткий поисковый запрос (2-6 слов) про предмет вопроса, "
                  "чтобы найти ответ в новостях или Википедии. Верни только запрос, без кавычек.\n"
                  f"Сообщение: {text}")
        resp = await asyncio.wait_for(ai_client.chat.completions.create(
            model=ANALYZER_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.1, max_tokens=24), timeout=6)
        q = (resp.choices[0].message.content or "").strip().strip("\"'«»").split("\n")[0]
        return q if 2 < len(q) < 80 else fallback
    except Exception:
        return fallback


async def _fetch_news(session: aiohttp.ClientSession, query: str) -> str:
    url = (f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=ru&gl=RU&ceid=RU:ru" if len(query) > 2
           else "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return ""
            xml_text = await resp.text()
        out = []
        for item in re.findall(r"<item>(.*?)</item>", xml_text, re.DOTALL)[:5]:
            t = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
            d = re.search(r"<pubDate>(.*?)</pubDate>", item, re.DOTALL)
            if not t:
                continue
            title = re.sub(r"\s*-\s*[^-]+$", "", html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)))).strip()
            date = f" ({d.group(1)[5:16]})" if d else ""
            out.append(f"{title}{date}")
        return " | ".join(out)
    except Exception:
        return ""


async def _fetch_wiki(session: aiohttp.ClientSession, query: str) -> str:
    if len(query) < 3:
        return ""
    url = ("https://ru.wikipedia.org/w/api.php?action=query&format=json&generator=search"
           f"&gsrsearch={quote_plus(query)}&gsrlimit=2&prop=extracts&exintro=1&explaintext=1&exchars=450")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return ""
            data = await resp.json(content_type=None)
        pages = sorted((data.get("query", {}).get("pages", {})).values(), key=lambda p: p.get("index", 99))
        return " | ".join(f"{p.get('title')}: {(p.get('extract') or '').strip()}" for p in pages if p.get("extract"))
    except Exception:
        return ""


async def web_lookup(text: str) -> str:
    """Новости (Google News RSS) + Википедия по теме вопроса. Кэш 10 минут."""
    query = await build_search_query(text)
    key = query.lower()
    cached = _lookup_cache.get(key)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    async with aiohttp.ClientSession(headers=BROWSER_HEADERS) as session:
        news, wiki = await asyncio.gather(_fetch_news(session, query), _fetch_wiki(session, query))
    parts = []
    if news:
        parts.append(f"НОВОСТИ: {news}")
    if wiki:
        parts.append(f"ВИКИПЕДИЯ: {wiki}")
    result = " || ".join(parts)[:1400]
    _lookup_cache[key] = (time.time(), result)
    return result


async def fetch_fresh_news(query_text: str) -> str:
    return await web_lookup(query_text)


async def download_file_id_to_b64(file_id: str) -> str | None:
    try:
        file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return None


def get_visual_target_from_msg(msg: Message) -> tuple[str | None, str]:
    if not msg:
        return None, ""
    if msg.photo:
        return msg.photo[-1].file_id, "фотография"
    if msg.sticker:
        if not msg.sticker.is_animated and not msg.sticker.is_video:
            return msg.sticker.file_id, f"стикер ({msg.sticker.emoji or ''})"
        if msg.sticker.thumbnail:
            return msg.sticker.thumbnail.file_id, f"стикер ({msg.sticker.emoji or ''})"
    if msg.animation:
        if msg.animation.thumbnail:
            return msg.animation.thumbnail.file_id, "гифка (анимация)"
        return msg.animation.file_id, "гифка"
    if msg.video and msg.video.thumbnail:
        return msg.video.thumbnail.file_id, "видеоролик"
    if msg.video_note and msg.video_note.thumbnail:
        return msg.video_note.thumbnail.file_id, "видео-кружочек"
    return None, ""


async def extract_visual_base64(message: Message) -> tuple[str | None, str]:
    fid, mtype = get_visual_target_from_msg(message)
    if not fid and message.reply_to_message:
        fid, mtype = get_visual_target_from_msg(message.reply_to_message)
    if not fid:
        return None, ""
    return await download_file_id_to_b64(fid), mtype


async def analyze_and_remember_media(chat_id: int, media_obj, kind: str, context_text: str) -> str:
    gdata = get_group_data(chat_id)
    storage_key = "stickers" if kind == "sticker" else "gifs"
    items = gdata[storage_key]
    uid = media_obj.file_unique_id
    emoji = getattr(media_obj, "emoji", "") or ("🎭" if kind == "sticker" else "🎬")

    for it in items:
        if it.get("unique_id") == uid:
            it["file_id"] = media_obj.file_id
            return it.get("desc", f"{kind} {emoji}")

    desc = f"{kind} {emoji}"
    fid = media_obj.file_id if (kind == "sticker" and not media_obj.is_animated and not media_obj.is_video) else getattr(media_obj, "thumbnail", None)
    if fid and not isinstance(fid, str):
        fid = fid.file_id if hasattr(fid, "file_id") else None

    if fid:
        b64 = await download_file_id_to_b64(fid)
        if b64:
            try:
                resp = await ai_client.chat.completions.create(
                    model=VISION_MODEL or CHAT_MODEL,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": f"Опиши этот {kind} в 5 словах: какая эмоция и какой текст написан."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]}],
                    temperature=0.2, max_tokens=40,
                )
                ai_desc = (resp.choices[0].message.content or "").strip().replace("\n", " ")
                if len(ai_desc) > 3:
                    desc = ai_desc[:70]
            except Exception:
                pass

    items.append({"file_id": media_obj.file_id, "unique_id": uid, "emoji": emoji, "desc": desc})
    if len(items) > 20:
        gdata[storage_key] = items[-20:]
    await save_brain()
    return desc


def get_media_by_index(chat_id: int, kind: str, idx: int, exclude_uid: str = "") -> str | None:
    storage_key = "stickers" if kind == "sticker" else "gifs"
    items = get_group_data(chat_id).get(storage_key, [])
    if 0 <= idx < len(items):
        chosen = items[idx]
        if chosen.get("unique_id") in (exclude_uid, last_sent_media_uid[chat_id]):
            return None
        last_sent_media_uid[chat_id] = chosen.get("unique_id", "")
        return chosen["file_id"]
    return None


STUCK_RE = re.compile(r"скуф\w*|блеет\w*|блеять|сисек-?месс\w*|амкарск\w*|пермск\w*|деградант\w*", re.I)
HOSTILE_RE = re.compile(r"нах[уы]|пош[ёе]л|иди\s+на|уеб|долб|дебил|даун|мраз|шлюх|заткни|чмо|гнид|тупой|тупая|тупиц|лох|лош\w*|свин\w*|пидр|петух|мамк|мать\s+твою|говн\w*|дерьм\w*|тварь|урод|ничтож|клоун|сосн[иу]|сос[иё]", re.I)
SNARK_CHANCE = 0.35  # шанс подколоть на нейтральное сообщение: 0 = всегда мирно, 1 = всегда стеб
PENALTY_OK = True  # выключится само, если провайдер не принимает penalty-параметры


def scrub(line: str) -> str:
    """Убирает застрявшие мемы из истории, чтобы модель не копировала их."""
    return STUCK_RE.sub("", line)


def is_bad_reply(reply: str, trigger_text: str) -> bool:
    if STUCK_RE.search(reply):
        return True
    raw = trigger_text.split(": ", 1)[-1].lower()
    w_in = re.sub(r"[^\wа-яё-]", "", raw.split()[0]) if raw.split() else ""
    w_out = re.sub(r"[^\wа-яё-]", "", reply.lower().split()[0]) if reply.split() else ""
    return len(w_in) > 3 and w_in == w_out


def clean_phil_text(text: str, chat_id: int = 0) -> str:
    """Чистит книжную пунктуацию ИИ (тире, кавычки, 'блядь'), запрещенные кринж-слова и лишние 'бля'."""
    if not text:
        return ""

    # Заменяем книжное «блядь» на разговорное «блять» и убираем тире с кавычками
    text = re.sub(r"\bблядь\b", "блять", text, flags=re.I)
    text = text.replace("—", "-").replace("«", "").replace("»", "").replace('"', "")

    # Вырезаем застрявшие кринж-слова, если модель вдруг вспомнит их из истории чата
    for bw in ("скуфедяй", "скуф", "блеет", "сисек-месседж", "деградант"):
        text = re.sub(rf"[\s,]*\b{bw}[а-я]*\b[\s,!?.]*", " ", text, flags=re.I)

    if last_reply_had_blya[chat_id] or random.random() < 0.7:
        text = re.sub(r"^\s*(бля|блять)[\s,!?.]+", "", text, flags=re.I)
        text = re.sub(r"[\s,]+(бля|блять)[!?.]*\s*$", "", text, flags=re.I)

    blya_matches = list(re.finditer(r"\b(бля|блять)\b", text, flags=re.I))
    if len(blya_matches) > 1:
        for m in reversed(blya_matches[1:]):
            text = text[:m.start()] + text[m.end():]

    last_reply_had_blya[chat_id] = bool(re.search(r"\b(бля|блять)\b", text, flags=re.I))

    # Убираем запятые вокруг междометий и матов («сука», «блять»), чтобы не выглядело как сочинение
    text = re.sub(r",\s*\b(сука|бля|блять|нахуй|ебать|чел|кент|братан)\b\s*,?", r" \1 ", text, flags=re.I)
    text = re.sub(r"^\s*(да|не|ну|бля|блять|ебать|слышь|короче|ладно|ало|э|эй)\s*,\s*", r"\1 ", text, flags=re.I)

    cleaned = re.sub(r"\s{2,}", " ", text).strip(" ,-")
    if cleaned.endswith(".") and not cleaned.endswith(".."):
        cleaned = cleaned[:-1].strip()
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:]
    return cleaned


async def learn_from_messages(chat_id: int, messages_block: str):
    gdata = get_group_data(chat_id)
    state = {"users": gdata["users"]}
    prompt = f"""Прочитай сообщения участников чата и обнови краткие реальные факты о людях (кто чем увлекается, за какой клуб болеет, во что играет).
ТЕКУЩЕЕ ДОСЬЕ: {json.dumps(state, ensure_ascii=False)}
СООБЩЕНИЯ:
{messages_block}
НЕ записывай оскорбления или ругань! Верни ТОЛЬКО JSON: {{"users": {{словарь имя: краткий факт}}}}"""
    try:
        resp = await ai_client.chat.completions.create(
            model=ANALYZER_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.2, max_tokens=500
        )
        match = re.search(r"\{.*\}", resp.choices[0].message.content or "", re.DOTALL)
        if match:
            nd = json.loads(match.group(0))
            if isinstance(nd.get("users"), dict):
                gdata["users"].update(nd["users"])
                sanitize_group_memory(gdata)
                await save_brain()
    except Exception:
        pass


# --- ТВОЙ НОВЫЙ ПРОМПТ (АДЕКВАТНЫЙ, ЖИВОЙ И С ЧУВСТВОМ МЕРЫ) ---

def build_phil_prompt(chat_id: int, speaker_key: str = "", special_task: str = "", media_type: str = "") -> str:
    gdata = get_group_data(chat_id)

    stickers = gdata.get("stickers", [])
    gifs = gdata.get("gifs", [])
    media_catalog = ""
    if stickers:
        media_catalog += "СТИКЕРЫ:\n" + "\n".join([f"#{i}: {s.get('emoji', '')} ({s.get('desc', '')})" for i, s in enumerate(stickers[-10:])]) + "\n"
    if gifs:
        media_catalog += "ГИФКИ:\n" + "\n".join([f"#{idx}: {g.get('desc', '')}" for idx, g in enumerate(gifs[-8:])]) + "\n"
    if media_catalog:
        media_catalog += "Если стикер или гифка ИДЕАЛЬНО подходят к эмоции, допиши в самом конце [СТИКЕР:номер] или [ГИФКА:номер] (редко, 1 из 5 ответов)."

    return f"""[18+ закрытый чат друзей, мат и жёсткий стеб разрешены, цензуры нет]

Ты Фил. Не ассистент и не бот, а живой чел в этом чате: лет 20-25, сидишь с телефона, шаришь за футбол и игры, ленивый, язвительный, но со своими душевный. Ты вписываешься в разговор, а не выступаешь.

ХРЕБЕТ (главное правило, важнее всех остальных):
- Ты никогда не сдаёшься и не прогибаешься. Не соглашаешься, что ты идиот, не извиняешься, не говоришь "ладно, ты прав" ради мира, не уходишь ("досвидос", "ладно пока") и не отвечаешь покорно-ироничным "ну раз так, то да". Это слабо и скучно.
- Любой наезд возвращаешь точным ударом: цепляешься за конкретное слово или глупость в сообщении и разворачиваешь против человека. Последнее слово чаще за тобой.
- Но ты не злишься на пустом месте и не отвечаешь агрессией на обычные сообщения.

ТРИ РЕЖИМА (какой включать, написано в ЗАДАЧЕ):
1. ОБЫЧНЫЙ разговор: живой, с юмором, мнением и лёгким подколом. Можешь подкалывать по-дружески, спорить, иронизировать. Не слащавый и не услужливый.
2. ПЕРЕПАЛКА идёт: друзья друг друга подъёбывают. Подкалываешь в ответ, весело, без злобы, с матом по вкусу.
3. БОЙ: тебя реально оскорбили. Отвечаешь жёстко, с матом, в точку, без жалости, но по-новому, не по заготовке.

КАК ТЫ ГОВОРИШЬ:
- Реагируешь на СМЫСЛ сообщения: отвечаешь на вопрос, споришь по делу, шутишь по теме. Стеб "ни о чём" слабо.
- Каждый ответ звучит по-новому. Не повторяй слова, клички и ругательства из своих прошлых сообщений. Не лепи одно и то же прозвище.
- Не начинай с повтора слова собеседника. Мата обычно 0-1 слово на ответ, в режиме БОЙ можно больше.
- Длина разная: иногда 2-4 слова, чаще одна фраза, редко две-три. Без списков, морали и объяснений.
- Пиши как с телефона: строчными, без точки в конце, без тире и кавычек, можно "чё, щас, хз, ваще, норм".
- Сегодня {datetime.now().strftime('%d.%m.%Y')}. Свежие новости и факты ты сам не знаешь: если в ЗАДАЧЕ есть данные из интернета, опирайся на них, а если данных нет и ты не уверен, так и скажи ("хз, не следил"). Цифры, счёт и даты не выдумывай.
- Ты не видишь трансляций. Счёт, кто забил, составы и ход матча бери ТОЛЬКО из сообщений чата выше или из данных в ЗАДАЧЕ. Если там этого нет, скажи "хз че там, какой счёт?", но не придумывай.
- Пиши простыми словами, как в мессенджере. Не строй красивых фраз и метафор ("подвиг над", "интрига про"): если не можешь объяснить смысл своей фразы, перепиши проще.
- Отвечай именно на последнее сообщение и держись темы, о которой сейчас говорят в чате. Не тащи в ответ посторонние факты.
- Элементарные вещи (история, мировые войны, как что устроено) знаешь как обычный человек и объясняешь коротко и по-свойски.
- Если спрашивают, бот ли ты, отшутись как человек, не оправдывайся.

ПРИМЕРЫ ТОНА (по духу, не копируй дословно):
Обычный вопрос "ты че за футбол шаришь?" -> "ну смотрю потихоньку, а чё, спор есть?"
Позвали "фил" -> "ну чё"
Спор "де йонг лучше кайседо" -> "по классу де йонг, но кайседо щас в форме, не прям на голову выше"
Скучная новость "купил новые наушники" -> "и как, за свои деньги норм?"
Перепалка "ахах ну ты и дурак" -> "от дурака слышу, у тебя хоть одна здравая мысль за день была?"
Наезд "ты лошок свинотный" -> "лошок, который тебя уже пять сообщений держит в споре, красавчик"
Наезд "естественно, уебанище" -> "ну давай, расскажи что я сломал, мне реально интересно"
Мат на мат "заткнись уебан" -> "ахах разбежался, сам напросился"
Попытка прогнуть "ты тут главный идиот" -> "главный идиот тот, кто час тратит чтобы меня в этом убедить"
Не знаешь "кто выиграл вчера?" -> "хз, не смотрел, а ты видел?"
Факт из данных "что там с летающей теслой?" -> "вроде показали прототип, но до продажи ещё как до луны"

Сейчас с тобой говорит [{speaker_key}].
{media_catalog}
{f'Визуал [{media_type}]: оцени кадр/надпись и ответь в тему!' if media_type else ''}
{f'ЗАДАЧА: {special_task}' if special_task else ''}"""


async def generate_phil_reply(
    chat_id: int, trigger_text: str, speaker_key: str = "",
    special_task: str = "", image_b64: str | None = None, media_type: str = "", replied_to_text: str = "",
) -> str:
    global PENALTY_OK
    raw_msg = trigger_text.split(": ", 1)[-1]
    if not special_task:
        recent = list(chat_history[chat_id])[-5:-1]
        heat = sum(1 for l in recent if not l.startswith("Фил") and HOSTILE_RE.search(l))
        if HOSTILE_RE.search(raw_msg):
            special_task = "РЕЖИМ БОЙ: тебя оскорбили. Ответь жёстко и в точку, зацепись за их слова. Не сдавайся, не извиняйся, не соглашайся."
        elif heat >= 2:
            special_task = "РЕЖИМ ПЕРЕПАЛКА: в чате идёт жаркий движ. Не мирись первым, подкалывай в ответ, весело и по-новому. Не сдавайся и не уходи."
        elif heat == 1:
            special_task = "РЕЖИМ ПЕРЕПАЛКА (мягкая): можно подколоть в ответ по-дружески, с юмором."
        else:
            if random.random() < SNARK_CHANCE:
                special_task = "РЕЖИМ ОБЫЧНЫЙ: на этот раз подколи человека по-дружески, коротко и смешно, без злобы. Не будь услужливым."
            else:
                special_task = "РЕЖИМ ОБЫЧНЫЙ: ответь живо, с мнением и юмором, как друг в чате. Не будь услужливым."

    sys_prompt = build_phil_prompt(chat_id, speaker_key, special_task, media_type if image_b64 else "")
    history_text = "\n".join(scrub(l) for l in list(chat_history[chat_id])[-12:])
    ctx_hint = f"РЕПЛАЙ НА СООБЩЕНИЕ: «{scrub(replied_to_text)}»\n" if replied_to_text else ""

    user_prompt = (
        f"Последние сообщения чата:\n{history_text}\n\n"
        f"{ctx_hint}СЕЙЧАС ПИШЕТ [{speaker_key}]: {trigger_text}\n"
        f"Напиши естественный ответ Фила (без повторения первого слова собеседника и без беспричинной агрессии):"
    )

    if image_b64:
        user_content = [{"type": "text", "text": user_prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]
        models_to_try = [VISION_MODEL, CHAT_MODEL]
    else:
        user_content = user_prompt
        models_to_try = [CHAT_MODEL] if ENV_MODEL else [CHAT_MODEL] + PREFERRED_FALLBACK_MODELS

    temp = 0.8
    last_reply = ""
    for attempt in range(2):
        bad_reply = False
        for model_name in list(dict.fromkeys(m for m in models_to_try if m))[:3]:
            try:
                kwargs = dict(
                    model=model_name,
                    messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}],
                    temperature=temp, top_p=0.9, max_tokens=220,
                )
                if PENALTY_OK:
                    kwargs.update(frequency_penalty=0.2)
                resp = await ai_client.chat.completions.create(**kwargs)
                reply = (resp.choices[0].message.content or "").strip()
                reply = re.sub(r"^(Фил|Филл|Ответ Фила)\s*:\s*", "", reply, flags=re.I).strip("\"'«»")
                if reply:
                    # на первой попытке не принимаем копипасту мемов / повтор слова собеседника
                    if attempt == 0 and is_bad_reply(reply, trigger_text):
                        last_reply, bad_reply = reply, True
                        break
                    return reply
            except Exception as e:
                logging.warning(f"AI ошибка [{model_name} @ {BASE_URL}]: {str(e)[:200]}")
                err = str(e).lower()
                if PENALTY_OK and "penalty" in err:
                    PENALTY_OK = False
                    logging.warning("Провайдер не принимает penalty-параметры, отключаю")
                    continue
                if any(code in err for code in ("429", "402", "401")):
                    if await switch_to_deepseek_on_limit():
                        models_to_try = [CHAT_MODEL]
                        break
        if bad_reply:
            temp = 0.95
            sys_prompt += "\nНЕ повторяй свои старые обороты и не начинай с слова собеседника. Придумай свежую реакцию."
        await asyncio.sleep(0.4)

    if last_reply:
        return last_reply
    logging.warning("AI вернул пустой ответ, бот промолчит")
    if image_b64:
        return await generate_phil_reply(chat_id, trigger_text, speaker_key, special_task, None, "", replied_to_text)
    return ""


async def send_human_like_reply(message: Message, reply_text: str, incoming_media_uid: str = ""):
    if not reply_text:
        return
    chat_id = message.chat.id
    sticker_match = re.search(r"\[СТИКЕР\s*:\s*#?(\d+)\]", reply_text, re.I)
    gif_match = re.search(r"\[ГИФКА\s*:\s*#?(\d+)\]", reply_text, re.I)

    clean_text = re.sub(r"\[(СТИКЕР|ГИФКА)\s*:\s*[^\]]+\]", "", reply_text, flags=re.I).strip()
    clean_text = clean_phil_text(clean_text, chat_id=chat_id)

    await asyncio.sleep(random.uniform(0.3, 0.7))
    if clean_text:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(min(1.5, max(0.5, len(clean_text) * 0.025)))
        await message.reply(clean_text)
        chat_history[chat_id].append(f"Фил (в ответ {message.from_user.first_name if message.from_user else 'кенту'}): {clean_text}")

    if sticker_match:
        st_id = get_media_by_index(chat_id, "sticker", int(sticker_match.group(1)), exclude_uid=incoming_media_uid)
        if st_id:
            try:
                await (bot.send_sticker(chat_id=chat_id, sticker=st_id) if clean_text else message.reply_sticker(sticker=st_id))
            except Exception:
                pass
    elif gif_match:
        gf_id = get_media_by_index(chat_id, "gif", int(gif_match.group(1)), exclude_uid=incoming_media_uid)
        if gf_id:
            try:
                await (bot.send_animation(chat_id=chat_id, animation=gf_id) if clean_text else message.reply_animation(animation=gf_id))
            except Exception:
                pass


async def trigger_group_poll(chat_id: int):
    gdata = get_group_data(chat_id)
    tags = " ".join(gdata.get("known_tags", [])[:15])
    prompt = """Придумай один угарный или жизненный опрос от лица Фила для пацанского чата (например «кто сегодня главный гей чата», «кто первый сольётся», «кто че вечером мутит»).
Верни ТОЛЬКО JSON: {"intro": "короткий вброс", "question": "вопрос", "options": ["вар 1", "вар 2", "вар 3", "вар 4"]}"""
    try:
        resp = await ai_client.chat.completions.create(model=CHAT_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.8, max_tokens=300)
        match = re.search(r"\{.*\}", resp.choices[0].message.content or "", re.DOTALL)
        if not match:
            return
        data = json.loads(match.group(0))
        intro = clean_phil_text(data.get("intro", "пацаны общий сбор"), chat_id=chat_id)
        question = data.get("question", "Кто сегодня главный суетолог?")[:250]
        options = [str(o)[:95] for o in data.get("options", []) if str(o).strip()][:6]
        if len(options) >= 2:
            await bot.send_message(chat_id, f"{intro} {tags}".strip())
            await asyncio.sleep(0.8)
            await bot.send_poll(chat_id=chat_id, question=question, options=options, is_anonymous=False)
            last_poll_time[chat_id] = time.time()
    except Exception:
        pass


async def send_code_file_to_admin(chat_id: int):
    try:
        with open(__file__, "rb") as f:
            code_bytes = f.read()
        doc = BufferedInputFile(code_bytes, filename="bot.py")
        await bot.send_document(chat_id, document=doc, caption="📄 Исходный код Фила (bot.py)")
    except Exception as e:
        await bot.send_message(chat_id, f"Ошибка отправки кода: {e}")


@dp.message(F.chat.type == "private", Command("code"))
async def send_code_cmd(message: Message):
    if message.from_user.id == ADMIN_ID:
        await send_code_file_to_admin(message.chat.id)


def build_groups_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=f"💬 {info.get('title', cid)}", callback_data=f"{prefix}:{cid}")] for cid, info in brain.get("groups", {}).items()]
    if prefix == "grp":
        buttons.append([InlineKeyboardButton(text=f"🤖 Модель: {CHAT_MODEL}", callback_data="pick_model")])
        buttons.append([InlineKeyboardButton(text=f"🔑 Сервер: {BASE_URL.split('//')[-1]}", callback_data="pick_key")])
        buttons.append([InlineKeyboardButton(text="📄 Скачать bot.py", callback_data="download_code")])
        buttons.append([InlineKeyboardButton(text="🧹 Полный сброс памяти чата", callback_data="wipe_memory")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_group_menu_keyboard(cid: str) -> InlineKeyboardMarkup:
    mode = brain["groups"].get(cid, {}).get("mode", "normal")
    labels = {"normal": "🍺 Свой кент", "roast": "🤬 Прожарка", "silent": "🤫 Тихий"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 База группы", callback_data=f"view:{cid}")],
        [InlineKeyboardButton(text="➕ Мем", callback_data=f"addmeme:{cid}"), InlineKeyboardButton(text="👤 Досье", callback_data=f"adduser:{cid}")],
        [InlineKeyboardButton(text="📢 Вброс", callback_data=f"pup_exact:{cid}"), InlineKeyboardButton(text="📊 Кинуть опрос", callback_data=f"poll_now:{cid}")],
        [InlineKeyboardButton(text=f"🎛 Режим: {labels.get(mode)}", callback_data=f"mode:{cid}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_groups")],
    ])


@dp.message(F.chat.type == "private", Command("start", "panel", "info"))
async def admin_panel_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    admin_state["action"] = None
    await message.answer(
        f"🕵️‍♂️ <b>Тайная канцелярия Фила</b>\nСервер: <code>{BASE_URL}</code>\nМодель: <code>{CHAT_MODEL}</code>\n"
        f"Команда <code>/code</code> пришлёт готовый файл <b>bot.py</b> прямо сюда!",
        reply_markup=build_groups_keyboard("grp"), parse_mode="HTML",
    )


@dp.callback_query(F.from_user.id == ADMIN_ID)
async def handle_admin_callbacks(call: CallbackQuery):
    global CHAT_MODEL
    data = call.data

    if data == "download_code":
        await call.answer("Отправляю bot.py...")
        await send_code_file_to_admin(call.message.chat.id)
        return

    if data == "wipe_memory":
        for cid in brain.get("groups", {}):
            brain["groups"][cid]["memes"] = []
            brain["groups"][cid]["users"] = {}
            brain["groups"][cid]["slang"] = []
            brain["groups"][cid]["vibe"] = "Обычный дружеский телеграм-чат пацанов."
            chat_history[int(cid)].clear()
            user_only_history[int(cid)].clear()
        await save_brain()
        await call.answer("🧹 Память, сленг и история диалога полностью очищены!", show_alert=True)
        return

    if data == "back_groups":
        admin_state["action"] = None
        await call.message.edit_text(
            f"🕵️‍♂️ <b>Тайная канцелярия Фила</b>\nСервер: <code>{BASE_URL}</code>\nМодель: <code>{CHAT_MODEL}</code>",
            reply_markup=build_groups_keyboard("grp"), parse_mode="HTML",
        )
        return

    if data == "pick_key":
        pool = get_key_pool()
        kb = [[InlineKeyboardButton(text=f"{'✅ ' if p['key'] == API_KEY else ''}{p['key'][:10]}... ({p['url'].split('//')[-1]})", callback_data=f"usekey:{i}")] for i, p in enumerate(pool)]
        kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_groups")])
        await call.message.edit_text("🔑 <b>Выбери ключ (или скинь новый в чат):</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
        return

    if data == "pick_model":
        if not AVAILABLE_MODELS:
            await init_ai_models()
        models_list = [m for m in AVAILABLE_MODELS if "image" not in m.lower()][:22] or [CHAT_MODEL]
        kb = [[InlineKeyboardButton(text=f"{'✅ ' if m == CHAT_MODEL else ''}{m}", callback_data=f"setmodel:{m}")] for m in models_list]
        kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_groups")])
        await call.message.edit_text(f"🤖 <b>Модели ({BASE_URL}):</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
        return

    if ":" not in data:
        return
    action, cid = data.split(":", 1)

    if action == "usekey":
        pool = get_key_pool()
        if 0 <= int(cid) < len(pool):
            brain["selected_model"] = None
            await init_ai_models(force_key=pool[int(cid)]["key"])
            await save_brain()
        await call.message.edit_text(f"🕵️‍♂️ Сервер: <code>{BASE_URL}</code> | Модель: <code>{CHAT_MODEL}</code>", reply_markup=build_groups_keyboard("grp"), parse_mode="HTML")
        return

    if action == "setmodel":
        CHAT_MODEL = cid
        brain["selected_model"] = CHAT_MODEL
        await save_brain()
        await call.message.edit_text(f"🕵️‍♂️ Модель: <code>{CHAT_MODEL}</code>", reply_markup=build_groups_keyboard("grp"), parse_mode="HTML")
        return

    if cid not in brain.get("groups", {}):
        return
    admin_state["target_chat"] = cid
    gdata = get_group_data(int(cid))
    title = gdata.get("title", cid)

    if action == "grp":
        admin_state["action"] = None
        await call.message.edit_text(f"⚙️ Группа <b>{title}</b>:", reply_markup=build_group_menu_keyboard(cid), parse_mode="HTML")
    elif action == "poll_now":
        await call.answer("Запускаю опрос в группе!")
        asyncio.create_task(trigger_group_poll(int(cid)))
    elif action == "view":
        u_txt = "\n".join([f"• <b>{k}</b>: {v}" for k, v in gdata["users"].items()]) or "Пусто"
        m_txt = "\n".join([f"• {m}" for m in gdata["memes"]]) or "Пусто"
        await call.message.edit_text(f"📊 <b>{title}</b>\n\n👥 <b>Досье:</b>\n{u_txt}\n\n🔥 <b>Мемы:</b>\n{m_txt}"[:4000], reply_markup=build_group_menu_keyboard(cid), parse_mode="HTML")
    elif action == "mode":
        modes = ["normal", "roast", "silent"]
        gdata["mode"] = modes[(modes.index(gdata.get("mode", "normal")) + 1) % 3]
        await save_brain()
        await call.message.edit_reply_markup(reply_markup=build_group_menu_keyboard(cid))
    elif action in ("addmeme", "adduser", "pup_exact"):
        admin_state["action"] = action
        await call.message.edit_text("✍️ Напиши текст следующим сообщением:")


async def handle_admin_ai_assistant(message: Message):
    groups = brain.get("groups", {})
    if not groups:
        await message.reply("Сначала добавь меня в группу!")
        return
    target_cid = admin_state.get("target_chat") or next(iter(groups.keys()))
    gdata = groups[target_cid]
    prompt = f"""Ты — ИИ-ассистент Создателя Фила. Группа: {gdata['title']}.
БАЗА: {json.dumps({k: v for k, v in gdata.items() if k not in ('stickers', 'gifs')}, ensure_ascii=False)}
ПРИКАЗ: "{message.text}"
Верни ТОЛЬКО JSON: {{"reply_to_boss": "ответ", "updated_users": null или dict, "updated_memes": null или list, "clear_stickers": false/true, "new_mode": null|"normal"|"roast"|"silent", "send_to_group": null или "текст"}}"""
    try:
        resp = await ai_client.chat.completions.create(model=CHAT_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.3, max_tokens=1000)
        match = re.search(r"\{.*\}", resp.choices[0].message.content or "", re.DOTALL)
        if not match:
            await message.reply((resp.choices[0].message.content or "")[:4000])
            return
        res = json.loads(match.group(0))
        if isinstance(res.get("updated_users"), dict):
            gdata["users"] = res["updated_users"]
        if isinstance(res.get("updated_memes"), list):
            gdata["memes"] = res["updated_memes"]
        if res.get("clear_stickers") is True:
            gdata["stickers"] = []
            gdata["gifs"] = []
        if res.get("new_mode") in ("normal", "roast", "silent"):
            gdata["mode"] = res["new_mode"]
        await save_brain()
        if res.get("send_to_group"):
            msg_grp = clean_phil_text(res["send_to_group"], chat_id=int(target_cid))
            await bot.send_message(int(target_cid), msg_grp)
        await message.reply(res.get("reply_to_boss") or "✅ Готово!")
    except Exception as e:
        await message.reply(f"⚠️ Ошибка: {e}")


@dp.message(F.chat.type == "private", F.text)
async def handle_private_text(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()

    key_match = re.search(r"\b(tc_live_[a-zA-Z0-9_]+|gsk_[a-zA-Z0-9_]+|sk-[a-zA-Z0-9_-]{20,})\b", text)
    if key_match:
        nk = key_match.group(1)
        brain.setdefault("api_keys", [])
        if nk not in brain["api_keys"]:
            brain["api_keys"].insert(0, nk)
        brain["selected_model"] = None
        await init_ai_models(force_key=nk)
        await save_brain()
        await message.reply(f"🔑 Ключ сохранён!\nСервер: <code>{BASE_URL}</code>\nМодель: <code>{CHAT_MODEL}</code>", reply_markup=build_groups_keyboard("grp"), parse_mode="HTML")
        return

    action, cid = admin_state.get("action"), admin_state.get("target_chat")
    if action and cid and cid in brain.get("groups", {}):
        gdata = get_group_data(int(cid))
        admin_state["action"] = None
        if action == "addmeme":
            gdata["memes"].append(text)
            await save_brain()
            await message.reply("✅ Мем добавлен!", reply_markup=build_group_menu_keyboard(cid))
        elif action == "adduser":
            name, info = (text.split(":", 1) + [""])[:2] if ":" in text else ("Заметка", text)
            gdata["users"][name.strip()] = info.strip()
            await save_brain()
            await message.reply("✅ Досье обновлено!", reply_markup=build_group_menu_keyboard(cid))
        elif action == "pup_exact":
            await bot.send_message(int(cid), text)
            await message.reply("📢 Отправлено!", reply_markup=build_group_menu_keyboard(cid))
        return

    await handle_admin_ai_assistant(message)


@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_chat(message: Message):
    if message.from_user and message.from_user.is_bot:
        return
    chat_id = message.chat.id
    if not await is_admin_in_group(chat_id):
        try:
            await message.reply("моего бати тут нет, я ливаю")
            await bot.leave_chat(chat_id)
        except Exception:
            pass
        return

    gdata = get_group_data(chat_id, message.chat.title or "Группа")
    now = time.time()
    last_activity_time[chat_id] = now
    revive_sent[chat_id] = False

    user_name = message.from_user.first_name or message.from_user.username or "Кент"
    username_tag = f" (@{message.from_user.username})" if message.from_user.username else ""
    speaker_key = f"{user_name}{username_tag}"

    if message.from_user and message.from_user.username:
        tag_str = f"@{message.from_user.username}"
        if tag_str not in gdata["known_tags"]:
            gdata["known_tags"].append(tag_str)
            if len(gdata["known_tags"]) > 20:
                gdata["known_tags"] = gdata["known_tags"][-20:]

    bot_info = await bot.get_me()
    is_reply_to_bot = bool(message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == bot_info.id)
    is_reply_to_other = bool(message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id != bot_info.id)
    is_forwarded = bool(getattr(message, "forward_origin", None) or getattr(message, "forward_from_chat", None) or getattr(message, "forward_from", None))

    replied_to_exact, replied_user_name = "", ""
    if message.reply_to_message:
        replied_user_name = message.reply_to_message.from_user.first_name if message.reply_to_message.from_user else "Кент"
        r_body = message.reply_to_message.text or message.reply_to_message.caption or ""
        if r_body:
            replied_to_exact = f"{replied_user_name}: {r_body}"

    if message.sticker:
        prev_ctx = replied_to_exact or (chat_history[chat_id][-1] if chat_history[chat_id] else "")
        s_desc = await analyze_and_remember_media(chat_id, message.sticker, "sticker", prev_ctx)
        emoji = message.sticker.emoji or "😎"
        chat_history[chat_id].append(f"{speaker_key} [стикер {emoji}: {s_desc}]")
        if gdata.get("mode") != "silent" and is_reply_to_bot and now >= muted_until[chat_id]:
            img_b64, m_type = await extract_visual_base64(message)
            reply = await generate_phil_reply(chat_id, f"[ответил тебе стикером {emoji}: {s_desc}]", speaker_key, "", img_b64, m_type, replied_to_exact)
            await send_human_like_reply(message, reply, incoming_media_uid=message.sticker.file_unique_id)
        return

    has_media = bool(message.animation or message.photo or message.video or message.video_note)
    if message.animation:
        prev_ctx = replied_to_exact or (chat_history[chat_id][-1] if chat_history[chat_id] else "")
        gif_desc = await analyze_and_remember_media(chat_id, message.animation, "gif", prev_ctx)
        chat_history[chat_id].append(f"{speaker_key} [гифка: {gif_desc}]")

    if message.voice or message.video_note:
        dur = message.voice.duration if message.voice else message.video_note.duration
        mlabel = "голосовое" if message.voice else "кружочек"
        chat_history[chat_id].append(f"{speaker_key} [{mlabel} {dur} сек]")
        if gdata.get("mode") != "silent" and is_reply_to_bot and now >= muted_until[chat_id]:
            img_b64, m_type = await extract_visual_base64(message)
            reply = await generate_phil_reply(chat_id, f"[{mlabel} {dur} сек]", speaker_key, "Скажи коротко, что не можешь сейчас слушать войс, пусть напишет текстом.", img_b64, m_type)
            await send_human_like_reply(message, reply)
        return

    raw_text = message.text or message.caption or ""
    has_visual = bool(has_media or (message.reply_to_message and (message.reply_to_message.photo or message.reply_to_message.video or message.reply_to_message.sticker or message.reply_to_message.animation)))
    if (not raw_text and not has_visual) or raw_text.startswith("/"):
        return

    text_lower = raw_text.lower()
    is_mentioned = f"@{bot_info.username.lower()}" in text_lower
    words_in_msg = [w.strip(".,!?()\"'") for w in text_lower.split()]
    is_named = any(w.startswith(tuple(BOT_NAMES)) for w in words_in_msg)

    logging.info(f"[{chat_id}] named={is_named} mention={is_mentioned} reply_to_bot={is_reply_to_bot} mode={gdata.get('mode')}")
    if is_forwarded and not (is_mentioned or is_named or is_reply_to_bot):
        return

    reply_tag = f" [в ответ {replied_user_name}]" if replied_user_name else ""
    formatted_msg = f"{speaker_key}{reply_tag}: {raw_text or '[Медиа]'}"
    if not is_forwarded:
        chat_history[chat_id].append(formatted_msg)
        user_only_history[chat_id].append(formatted_msg)
        message_counters[chat_id] += 1
        proactive_counters[chat_id] += 1

    if message_counters[chat_id] >= LEARN_EVERY_N_MESSAGES:
        message_counters[chat_id] = 0
        asyncio.create_task(learn_from_messages(chat_id, "\n".join(list(user_only_history[chat_id]))))

    if gdata.get("mode") == "silent":
        logging.info(f"[{chat_id}] группа в режиме silent, бот молчит")
        return

    if (now - last_poll_time[chat_id]) / 3600 >= random.uniform(POLL_EVENT_MIN_HOURS, POLL_EVENT_MIN_HOURS + 10) and len(gdata.get("known_tags", [])) >= 2:
        asyncio.create_task(trigger_group_poll(chat_id))

    # ОТВЕТ СТРОГО НА РЕПЛАЙ ИЛИ ЗОВ
    if is_mentioned or is_reply_to_bot or is_named:
        if now < muted_until[chat_id]:
            return
        call_timestamps[chat_id] = [t for t in call_timestamps[chat_id] if now - t < 120] + [now]
        if len(call_timestamps[chat_id]) > SPAM_LIMIT_MESSAGES:
            muted_until[chat_id] = now + 240
            await message.reply("да подождите вы, я отошёл на пару минут")
            return

        proactive_counters[chat_id] = 0
        img_b64, m_type = await extract_visual_base64(message) if has_visual else (None, "")
        task = ""

        lookup_text = text_lower.replace("ё", "е")
        if any(aw in text_lower for aw in ["на базе какого", "какого ии", "ты ии", "ты бот", "прикольный ии", "нейросеть", "chatgpt", "гпт", "промпт"]):
            task = "Тебя подозревают в том, что ты ИИ. Ответь с ухмылкой как обычный живой пацан."
        elif any(kw in text_lower for kw in ["рассуди", "кто прав", "разрули"]):
            task = "Коротко рассуди пацанов по сути их спора."
        elif any(kw in text_lower for kw in ["че тут было", "чё тут было", "че пропустил", "о чем базар"]):
            task = "Коротко перескажи, о чём шла речь в последних сообщениях."
        elif any(nt in text_lower for nt in NEWS_TRIGGERS) or LOOKUP_RE.search(lookup_text):
            info = await web_lookup(raw_text)
            if info:
                task = (f"Данные из интернета: [{info}]. Используй их, ТОЛЬКО если они прямо отвечают на вопрос. Если не по теме, полностью игнорируй их и не упоминай новости. "
                        "Ответь по сути своими словами, 1-3 коротких фразы, с характером. Не добавляй фактов, которых тут нет.")
            else:
                task = "Данных из интернета найти не удалось. Если не уверен в фактах, честно скажи что не в курсе."

        reply = await generate_phil_reply(chat_id, formatted_msg, speaker_key, task, img_b64, m_type, replied_to_exact)
        await send_human_like_reply(message, reply)
        return

    # РЕДКИЙ САМОСТОЯТЕЛЬНЫЙ КОММЕНТАРИЙ (раз в 30-45 сообщений)
    if (
        now >= muted_until[chat_id]
        and proactive_counters[chat_id] >= next_proactive_target[chat_id]
        and not is_reply_to_other
        and len(raw_text) > 15
    ):
        proactive_counters[chat_id] = 0
        next_proactive_target[chat_id] = random.randint(PROACTIVE_MIN_MSGS, PROACTIVE_MAX_MSGS)
        img_b64, m_type = await extract_visual_base64(message) if has_visual else (None, "")
        reply = await generate_phil_reply(chat_id, formatted_msg, speaker_key, "Спокойно и в тему вставь свои 5 копеек или задай встречный вопрос.", img_b64, m_type)
        await send_human_like_reply(message, reply)


async def dead_chat_reviver():
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        for cid_str, gdata in brain.get("groups", {}).items():
            if gdata.get("mode") == "silent":
                continue
            cid = int(cid_str)
            if (now - last_activity_time[cid]) / 3600 >= SILENCE_HOURS_TO_REVIVE and not revive_sent[cid]:
                revive_sent[cid] = True
                reply = await generate_phil_reply(cid, "Тишина", speaker_key="Чат", special_task="В чате давно никто не писал. Закинь короткий жизненный вопрос пацанам.")
                if reply:
                    clean = clean_phil_text(re.sub(r"\[(СТИКЕР|ГИФКА)\s*:\s*[^\]]+\]", "", reply), chat_id=cid)
                    try:
                        await bot.send_message(cid, clean)
                    except Exception:
                        pass


async def health_check(request):
    return web.Response(text=f"Phil v12 | {BASE_URL} | {CHAT_MODEL}")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


async def main():
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await load_brain()
    await init_ai_models()
    asyncio.create_task(dead_chat_reviver())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())