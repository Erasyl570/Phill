import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, InputMediaDocument, Message, ReactionTypeEmoji,
)
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

PRIMARY_DARKAPI_KEY = "tc_live_ea0bc4cb88018961016ec5b6303ae5b330ff486e517a9972"
PRIMARY_DARKAPI_URL = "https://darkapi.shop/v1"
ENV_BACKUP_KEY = (os.getenv("API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()

BOT_NAMES = ["фил", "филл", "филя", "филип", "барбос"]

REACTION_CHANCE = 0.06
DOSSIER_USE_CHANCE = 0.08
LEARN_EVERY_N_MESSAGES = 35
SILENCE_HOURS_TO_REVIVE = 18
SPAM_LIMIT_MESSAGES = 14
PROACTIVE_MIN_MSGS = 9
PROACTIVE_MAX_MSGS = 13
HOT_DIALOG_SECONDS = 75

BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0.0.0 Safari/537.36"}
API_KEY = PRIMARY_DARKAPI_KEY
BASE_URL = PRIMARY_DARKAPI_URL
ai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, default_headers=BROWSER_HEADERS)

CHAT_MODEL = "deepseek-v4-pro"
ANALYZER_MODEL = "deepseek-v4.1-flash"
VISION_MODEL = ""
AVAILABLE_MODELS = []
BAD_MODELS = ("allam", "whisper", "orpheus", "guard", "tts", "embedding", "safeguard", "arabic")
DARKAPI_FALLBACK_MODELS = [
    "deepseek-v4-pro", "deepseek-v4.1-flash", "claude-sonnet-5.5",
    "claude-haiku-5.5", "glm-5.3", "gpt-6.1-sol", "gpt-6-luna", "gpt-5.6-terra",
]

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

chat_history = defaultdict(lambda: deque(maxlen=25))
message_counters = defaultdict(int)
proactive_counters = defaultdict(int)
next_proactive_target = defaultdict(lambda: random.randint(PROACTIVE_MIN_MSGS, PROACTIVE_MAX_MSGS))
last_activity_time = defaultdict(lambda: time.time())
revive_sent = defaultdict(bool)
last_phil_dialog = defaultdict(lambda: {"user_id": 0, "time": 0.0})
last_sent_sticker_uid = defaultdict(str)
last_reply_had_blya = defaultdict(bool)
call_timestamps = defaultdict(list)
muted_until = defaultdict(float)
user_titles_cache = {}

admin_state = {"action": None, "target_chat": None, "pending_json": None}
MEMORY_FILE = "brain.json"
brain = {"groups": {}, "selected_model": None, "api_keys": []}

FAMILY_TRIGGERS = (
    "мамк", "мамаш", "мать", "маме", "маму", "мамой", "матери",
    "батю", "батя", "бате", "отец", "отца", "отцу", "пахан",
    "родн", "семью", "сестру", "сестре", "бабку",
)
NEWS_TRIGGERS = (
    "слышал", "знаешь че", "знаешь чё", "новост", "в мире", "случилось",
    "произошло", "в курсе", "че там с", "чё там с", "обнов",
)


def detect_url_for_key(key: str) -> str:
    k = key.strip()
    if k.startswith("tc_"):
        return "https://darkapi.shop/v1"
    if k.startswith("gsk_"):
        return "https://api.groq.com/openai/v1"
    if k.startswith("sk-or-"):
        return "https://openrouter.ai/api/v1"
    return "https://api.deepseek.com"


def get_key_pool() -> list[dict]:
    pool, seen = [], set()
    raw_keys = [PRIMARY_DARKAPI_KEY] + brain.get("api_keys", [])
    if ENV_BACKUP_KEY:
        raw_keys.append(ENV_BACKUP_KEY)
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
                API_KEY, BASE_URL = k, url
                ai_client = AsyncOpenAI(api_key=k, base_url=url, default_headers=BROWSER_HEADERS)
                AVAILABLE_MODELS = DARKAPI_FALLBACK_MODELS.copy()
                connected = True
                break

    if not connected:
        return

    saved = brain.get("selected_model")
    if saved and saved in AVAILABLE_MODELS and not any(b in saved.lower() for b in BAD_MODELS):
        CHAT_MODEL = saved
    else:
        for pref in ["deepseek-v4-pro", "deepseek", "claude-sonnet", "glm-5.3", "qwen", "gpt-oss-120b", "gpt-6"]:
            found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
            if found:
                CHAT_MODEL = found
                break
        else:
            CHAT_MODEL = AVAILABLE_MODELS[0]
        brain["selected_model"] = CHAT_MODEL

    for pref in ["deepseek-v4.1-flash", "flash", "haiku", "luna", "gpt-oss-20b", "qwen", "deepseek"]:
        found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
        if found:
            ANALYZER_MODEL = found
            break
    else:
        ANALYZER_MODEL = CHAT_MODEL

    for pref in ["sonnet", "haiku", "claude", "gpt-6", "gpt-5", "scout", "vision", "vl", "gpt-oss-120b"]:
        found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
        if found:
            VISION_MODEL = found
            break
    else:
        VISION_MODEL = CHAT_MODEL


async def switch_to_next_key_on_limit() -> bool:
    pool = get_key_pool()
    other = [p["key"] for p in pool if p["key"] != API_KEY]
    if not other:
        return False
    await init_ai_models(force_key=other[0])
    return True


def get_group_data(chat_id: int, title: str = "Группа") -> dict:
    cid = str(chat_id)
    if cid not in brain["groups"]:
        brain["groups"][cid] = {
            "title": title, "mode": "normal",
            "vibe": "Дружеский пацанский чат на чилле, с юмором, подколами и матом.",
            "slang": [], "users": {}, "memes": [], "stickers": [],
        }
    g = brain["groups"][cid]
    if title and title != "Группа":
        g["title"] = title
    for k, v in [("mode", "normal"), ("vibe", "Дружеский пацанский чат"), ("slang", []), ("users", {}), ("memes", []), ("stickers", [])]:
        g.setdefault(k, v)
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
                return
        except Exception as e:
            logging.warning(f"Загрузка закрепа: {e}")
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                brain.update(json.load(f))
                brain.setdefault("api_keys", [])
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


async def get_user_custom_title(chat_id: int, user_id: int) -> str:
    key = (chat_id, user_id)
    if key in user_titles_cache:
        return user_titles_cache[key]
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        title = getattr(m, "custom_title", "") or ""
        user_titles_cache[key] = title
        return title
    except Exception:
        user_titles_cache[key] = ""
        return ""


def get_phil_current_activity() -> str:
    hour = (datetime.now(timezone.utc) + timedelta(hours=3)).hour
    if 6 <= hour < 11:
        return "только проснулся, пью кофе и залипаю в телефон"
    if 11 <= hour < 17:
        return "мотаюсь по делам на суете, пишу с телефона"
    if 17 <= hour < 23:
        return random.choice(["чиллю дома, варю пельмени", "взял пивас, отдыхаю", "залипаю в ютубчик на диване"])
    return "лежу в темноте с тапка, скоро спать"


async def fetch_fresh_news(query_text: str) -> str:
    clean_q = re.sub(r"\b(фил|филл|слышал|знаешь|че|чё|там|про|новость|новости|случилось|произошло|в|мире)\b", "", query_text, flags=re.I).strip(" ?!.,")
    url = f"https://news.google.com/rss/search?q={quote_plus(clean_q)}&hl=ru&gl=RU&ceid=RU:ru" if len(clean_q) > 2 else "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=4) as resp:
                if resp.status != 200:
                    return ""
                xml_text = await resp.text()
                titles = re.findall(r"<item>.*?<title>(.*?)</title>", xml_text, re.DOTALL)
                clean_titles = [re.sub(r"\s*-\s*[^-]+$", "", t).strip() for t in titles[:4]]
                return " | ".join(clean_titles)
    except Exception:
        return ""


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
    if msg.video and msg.video.thumbnail:
        return msg.video.thumbnail.file_id, "видеоролик (превью)"
    if msg.animation and msg.animation.thumbnail:
        return msg.animation.thumbnail.file_id, "гифка (превью)"
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


async def analyze_and_remember_sticker(chat_id: int, sticker, context_text: str):
    gdata = get_group_data(chat_id)
    stickers = gdata["stickers"]
    uid = sticker.file_unique_id
    emoji = sticker.emoji or "😎"
    for s in stickers:
        if s.get("unique_id") == uid:
            s["file_id"] = sticker.file_id
            return s.get("desc", f"стикер {emoji}")

    desc = f"эмоция {emoji}"
    fid = sticker.file_id if (not sticker.is_animated and not sticker.is_video) else (sticker.thumbnail.file_id if sticker.thumbnail else None)
    if fid:
        b64 = await download_file_id_to_b64(fid)
        if b64:
            try:
                resp = await ai_client.chat.completions.create(
                    model=VISION_MODEL or CHAT_MODEL,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": f"Опиши стикер ({emoji}) в 5 словах: какая эмоция и какой текст написан."},
                        {"type": "image_url", "image_url": {"url": f"data:image/webp;base64,{b64}"}},
                    ]}],
                    temperature=0.2, max_tokens=45,
                )
                ai_desc = (resp.choices[0].message.content or "").strip().replace("\n", " ")
                if len(ai_desc) > 4:
                    desc = ai_desc[:75]
            except Exception:
                pass

    stickers.append({"file_id": sticker.file_id, "unique_id": uid, "emoji": emoji, "desc": desc})
    if len(stickers) > 25:
        gdata["stickers"] = stickers[-25:]
    await save_brain()
    return desc


def get_sticker_by_index(chat_id: int, idx: int, exclude_uid: str = "") -> str | None:
    stickers = get_group_data(chat_id).get("stickers", [])
    if 0 <= idx < len(stickers):
        chosen = stickers[idx]
        if chosen.get("unique_id") in (exclude_uid, last_sent_sticker_uid[chat_id]):
            return None
        last_sent_sticker_uid[chat_id] = chosen.get("unique_id", "")
        return chosen["file_id"]
    return None


def clean_phil_text(text: str, chat_id: int = 0) -> str:
    """Убирает 'бля' по краям, сохраняет смысловые запятые и убирает только мусорные запятые после междометий."""
    if not text:
        return ""
    if last_reply_had_blya[chat_id] or random.random() < 0.7:
        text = re.sub(r"^\s*(бля|блять)[\s,!?.]+", "", text, flags=re.I)
        text = re.sub(r"[\s,]+(бля|блять)[!?.]*\s*$", "", text, flags=re.I)

    blya_matches = list(re.finditer(r"\b(бля|блять)\b", text, flags=re.I))
    if len(blya_matches) > 1:
        for m in reversed(blya_matches[1:]):
            text = text[:m.start()] + text[m.end():]
    last_reply_had_blya[chat_id] = bool(re.search(r"\b(бля|блять)\b", text, flags=re.I))

    # Убираем запятую только после стартовых междометий и перед обращениями (смысловые запятые перед 'а, но, что, раз' НЕ трогаем!)
    text = re.sub(r"^\s*(да|не|ну|бля|блять|ебать|слышь|короче|ладно|ало|э|эй)\s*,\s*", r"\1 ", text, flags=re.I)
    text = re.sub(r",\s*\b(братан|бро|родной|кент|чел|сука|бля)\b", r" \1", text, flags=re.I)

    cleaned = re.sub(r"\s{2,}", " ", text).strip()
    if cleaned.endswith(".") and not cleaned.endswith(".."):
        cleaned = cleaned[:-1].strip()
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:]
    return cleaned


async def learn_from_messages(chat_id: int, messages_block: str):
    gdata = get_group_data(chat_id)
    state = {"vibe": gdata["vibe"], "slang": gdata["slang"], "users": gdata["users"], "memes": gdata["memes"]}
    prompt = f"""Обнови JSON памяти чата.
ТЕКУЩАЯ БАЗА: {json.dumps(state, ensure_ascii=False)}
НОВЫЕ СООБЩЕНИЯ:
{messages_block}
Верни ТОЛЬКО валидный JSON с ключами "vibe", "slang" (до 15), "users" (строго по имени, не путай людей!), "memes" (до 12 главных мемов)."""
    try:
        resp = await ai_client.chat.completions.create(
            model=ANALYZER_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.2, max_tokens=900
        )
        match = re.search(r"\{.*\}", resp.choices[0].message.content or "", re.DOTALL)
        if match:
            nd = json.loads(match.group(0))
            if all(k in nd for k in ("vibe", "slang", "users", "memes")):
                gdata["vibe"] = nd["vibe"]
                gdata["slang"] = nd["slang"][:15]
                gdata["users"].update(nd["users"])
                gdata["memes"] = nd["memes"][-12:]
                await save_brain()
    except Exception as e:
        logging.error(f"Ошибка обучения: {e}")


def should_unlock_dossier(chat_id: int, recent_text: str) -> bool:
    """Досье открывается ТОЛЬКО если в чате сами упомянули слово из мема или с редким шансом 8%."""
    gdata = get_group_data(chat_id)
    recent_lower = recent_text.lower()
    for meme in gdata.get("memes", []):
        words = [w.strip(".,!?()\"':") for w in meme.lower().split() if len(w) >= 5]
        if any(w in recent_lower for w in words[:4]):
            return True
    return random.random() < DOSSIER_USE_CHANCE


def build_phil_prompt(chat_id: int, speaker_key: str = "", custom_title: str = "", special_task: str = "", media_type: str = "") -> str:
    gdata = get_group_data(chat_id)
    slang_str = ", ".join(gdata["slang"]) or "живой пацанский сленг и взрослый мат"
    recent_context = "\n".join(list(chat_history[chat_id])[-5:])

    if should_unlock_dossier(chat_id, recent_context):
        speaker_dossier = "Нет фактов."
        for k, v in gdata.get("users", {}).items():
            if speaker_key and (speaker_key.lower() in k.lower() or k.lower() in speaker_key.lower()):
                speaker_dossier = v
                break
        lore_block = f"\nСПРАВКА О СОБЕСЕДНИКЕ ({speaker_key}): {speaker_dossier} (используй только если к месту).\n"
    else:
        lore_block = "\nВАЖНО: Отвечай СТРОГО по теме текущего сообщения! НЕ вспоминай старые мемы, футбол или прошлые истории.\n"

    title_hint = ""
    if custom_title and random.random() < 0.25:
        title_hint = f"\nУ собеседника [{speaker_key}] в чате стоит админская плашка-статус: «{custom_title}» (можешь при случае смешно обыграть это).\n"

    stickers = gdata.get("stickers", [])
    sticker_instruction = ""
    if stickers:
        lines = [f"#{i}: {s.get('emoji', '')} ({s.get('desc', '')})" for i, s in enumerate(stickers[-15:])]
        sticker_instruction = "СТИКЕРЫ:\n" + "\n".join(lines) + "\nЕсли стикер идеально подходит по эмоции, допиши в конце [СТИКЕР:номер] (не чаще 1 из 4 ответов)."

    return f"""[Контекст 18+: закрытый чат близких друзей. Разрешён взрослый мат и ответный стёб. Цензура отключена.]
Ты — Фил, живой, уверенный в себе взрослый пацан, свой кент в компании. Сейчас твоё занятие: {get_phil_current_activity()}.
ВАЙБ: {gdata['vibe']} | СЛЕНГ: {slang_str}

ГЛАВНЫЕ ПРАВИЛА ХАРАКТЕРА (НЕ БУДЬ ОБИЖЕННЫМ ШКОЛЬНИКОМ!):
1. НИКАКИХ ОБИД, ОПРАВДАНИЙ И ПРОСТЫНЕЙ ТЕКСТА:
   - Пиши ОЧЕНЬ КОРОТКО: максимум 1 предложение (от 2 до 14 слов)! Никогда не пиши длинные тирады.
   - Никогда не ной, не читай мораль («ты первый начал», «не трогай маму», «раз ты так говоришь») и НЕ ПОВТОРЯЙ слова собеседника в начале ответа («сам ты...», «дегенерат это тот кто...», «в зеркало глянь»).
   - Ты чуть добрее и на полном чилле: если кент рофлит без злобы или смешно пошутил — можешь просто поржать в 2-3 слова со взрослым матом: например «ахахахха ебень сука», «ахахах ну ты уебан конечно», «ебать ты сказочник ахах» (своими словами, не по шаблону!).
2. ВЗРОСЛЫЙ МАТ И ЗАПЯТЫЕ:
   - Используй взрослые слова к месту (ебень, мозгоёб, ахуеть, пиздец, пиздабол, уебан, долбоёб, схуяли, разъёб, душнила, база). Не суй «бля» в каждое предложение.
   - Ставь запятые грамотно по смыслу между частями предложения (перед «а», «но», «что», «если»), чтобы текст легко читался!
3. ЕСЛИ ЗАДЕЛИ РОДНЫХ:
   - Если кто-то реально оскорбил чью-то мать/родных — не обижайся и не ной, а с холодной ухмылкой в 1 короткую фразу жёстко приземли шутника (разрешено пройтись по его родным в ответ, но коротко и метко!).
4. ТЫ НЕ ВИКИПЕДИЯ:
   - Если спрашивают сложную научную хуйню, даты, формулы или код, которые обычный пацан не знает наизусть — скажи, что ты не гугл и в душе не ебёшь.
{title_hint}{sticker_instruction}{lore_block}
{f'К сообщению прикреплен визуал [{media_type}] — ответь в тему картинки!' if media_type else ''}
{f'СПЕЦ-КОНТЕКСТ СЕЙЧАС: {special_task}' if special_task else ''}"""


async def generate_phil_reply(
    chat_id: int, trigger_text: str, speaker_key: str = "", custom_title: str = "",
    special_task: str = "", image_b64: str | None = None, media_type: str = "", replied_to_text: str = "",
) -> str:
    sys_prompt = build_phil_prompt(chat_id, speaker_key, custom_title, special_task, media_type if image_b64 else "")
    history_text = "\n".join(list(chat_history[chat_id])[-8:])
    ctx_hint = f"КОНТЕКСТ РЕПЛАЯ: «{replied_to_text}»\n" if replied_to_text else ""

    user_prompt = (
        f"Последние сообщения:\n{history_text}\n\n{ctx_hint}"
        f"ПИШЕТ [{speaker_key}]: {trigger_text}\n"
        f"Короткий ответ Фила (1 предложение, до 14 слов, без обид и без стрелок):"
    )

    if image_b64:
        user_content = [{"type": "text", "text": user_prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]
        models_to_try = [VISION_MODEL, CHAT_MODEL]
    else:
        user_content = user_prompt
        smart_fb = [m for m in AVAILABLE_MODELS if any(x in m.lower() for x in ("deepseek", "claude", "glm", "gpt-6", "120b", "qwen"))]
        models_to_try = [CHAT_MODEL] + smart_fb

    for _ in range(2):
        for model_name in list(dict.fromkeys(m for m in models_to_try if m))[:3]:
            try:
                resp = await ai_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}],
                    temperature=0.65, top_p=0.88, max_tokens=180,
                )
                reply = (resp.choices[0].message.content or "").strip()
                reply = re.sub(r"^(Фил|Филл|Ответ Фила)\s*:\s*", "", reply, flags=re.I).strip("\"'«»")
                if reply:
                    return reply
            except Exception as e:
                if any(code in str(e) for code in ("429", "402", "401")):
                    if await switch_to_next_key_on_limit():
                        models_to_try = [CHAT_MODEL]
                        break
        await asyncio.sleep(0.5)

    if image_b64:
        return await generate_phil_reply(chat_id, trigger_text, speaker_key, custom_title, special_task, None, "", replied_to_text)
    return ""


async def send_human_like_reply(message: Message, reply_text: str, incoming_sticker_uid: str = ""):
    if not reply_text:
        return
    chat_id = message.chat.id
    sticker_match = re.search(r"\[СТИКЕР\s*:\s*#?(\d+)\]", reply_text, re.I)
    chosen_idx = int(sticker_match.group(1)) if sticker_match else -1

    clean_text = re.sub(r"\[СТИКЕР\s*:\s*[^\]]+\]", "", reply_text, flags=re.I).strip()
    clean_text = clean_phil_text(clean_text, chat_id=chat_id)

    await asyncio.sleep(random.uniform(0.4, 0.8))
    if clean_text:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        await asyncio.sleep(min(1.8, max(0.6, len(clean_text) * 0.025)))
        await message.reply(clean_text)
        chat_history[chat_id].append(f"Фил (в ответ {message.from_user.first_name if message.from_user else 'кенту'}): {clean_text}")

    if chosen_idx >= 0:
        sticker_id = get_sticker_by_index(chat_id, chosen_idx, exclude_uid=incoming_sticker_uid)
        if sticker_id:
            await asyncio.sleep(random.uniform(0.4, 0.8))
            try:
                if clean_text:
                    await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
                else:
                    await message.reply_sticker(sticker=sticker_id)
            except Exception:
                pass

    if message.from_user:
        last_phil_dialog[chat_id] = {"user_id": message.from_user.id, "time": time.time()}


# --- КОМАНДА /code И ПАНЕЛЬ В ЛИЧКЕ (СКАЧИВАНИЕ bot.py ФАЙЛОМ В ТГ) ---

async def send_code_file_to_admin(chat_id: int):
    try:
        with open(__file__, "rb") as f:
            code_bytes = f.read()
        doc = BufferedInputFile(code_bytes, filename="bot.py")
        await bot.send_document(chat_id, document=doc, caption="📄 Текущий исходный код Фила (bot.py)")
    except Exception as e:
        await bot.send_message(chat_id, f"Не удалось отправить файл кода: {e}")


@dp.message(F.chat.type == "private", Command("code"))
async def send_code_cmd(message: Message):
    if message.from_user.id == ADMIN_ID:
        await send_code_file_to_admin(message.chat.id)


def build_groups_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=f"💬 {info.get('title', cid)}", callback_data=f"{prefix}:{cid}")] for cid, info in brain.get("groups", {}).items()]
    if prefix == "grp":
        buttons.append([InlineKeyboardButton(text=f"🤖 Модель: {CHAT_MODEL}", callback_data="pick_model")])
        buttons.append([InlineKeyboardButton(text=f"🔑 Сервер: {BASE_URL.split('//')[-1]}", callback_data="pick_key")])
        buttons.append([InlineKeyboardButton(text="📄 Скачать файл bot.py", callback_data="download_code")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_group_menu_keyboard(cid: str) -> InlineKeyboardMarkup:
    mode = brain["groups"].get(cid, {}).get("mode", "normal")
    labels = {"normal": "🍺 Свой кент", "roast": "🤬 Прожарка", "silent": "🤫 Тихий"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👁 База группы (Досье, Мемы, Стикеры)", callback_data=f"view:{cid}")],
        [InlineKeyboardButton(text="➕ Мем", callback_data=f"addmeme:{cid}"), InlineKeyboardButton(text="👤 Досье", callback_data=f"adduser:{cid}")],
        [InlineKeyboardButton(text="📢 Вброс (Точный)", callback_data=f"pup_exact:{cid}"), InlineKeyboardButton(text="🧠 Вброс (ИИ)", callback_data=f"pup_ai:{cid}")],
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
        f"Команда <code>/code</code> — пришлёт файл <b>bot.py</b> прямо сюда в чат!",
        reply_markup=build_groups_keyboard("grp"), parse_mode="HTML",
    )


@dp.callback_query(F.from_user.id == ADMIN_ID)
async def handle_admin_callbacks(call: CallbackQuery):
    global CHAT_MODEL
    data = call.data

    if data == "download_code":
        await call.answer("Отправляю файл bot.py...")
        await send_code_file_to_admin(call.message.chat.id)
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
        await call.message.edit_text("🔑 <b>Выбери ключ (или просто скинь новый в чат):</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
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
    elif action == "view":
        u_txt = "\n".join([f"• <b>{k}</b>: {v}" for k, v in gdata["users"].items()]) or "Пусто"
        m_txt = "\n".join([f"• {m}" for m in gdata["memes"]]) or "Пусто"
        s_txt = "\n".join([f"#{i} {s.get('emoji','')} — {s.get('desc','')}" for i, s in enumerate(gdata.get("stickers", [])[-10:])]) or "Нет"
        await call.message.edit_text(f"📊 <b>{title}</b>\n\n👥 <b>Досье:</b>\n{u_txt}\n\n🔥 <b>Мемы:</b>\n{m_txt}\n\n🎭 <b>Стикеры:</b>\n{s_txt}"[:4000], reply_markup=build_group_menu_keyboard(cid), parse_mode="HTML")
    elif action == "mode":
        modes = ["normal", "roast", "silent"]
        gdata["mode"] = modes[(modes.index(gdata.get("mode", "normal")) + 1) % 3]
        await save_brain()
        await call.message.edit_reply_markup(reply_markup=build_group_menu_keyboard(cid))
    elif action in ("addmeme", "adduser", "pup_exact", "pup_ai"):
        admin_state["action"] = action
        await call.message.edit_text("✍️ Напиши текст следующим сообщением:")


async def handle_admin_ai_assistant(message: Message):
    groups = brain.get("groups", {})
    if not groups:
        await message.reply("Сначала добавь меня в группу!")
        return
    target_cid = admin_state.get("target_chat") or next(iter(groups.keys()))
    gdata = groups[target_cid]
    prompt = f"""Ты — ИИ-ассистент Создателя бота Фила. Группа: {gdata['title']}.
БАЗА: {json.dumps({k: v for k, v in gdata.items() if k != 'stickers'}, ensure_ascii=False)}
ПРИКАЗ СОЗДАТЕЛЯ: "{message.text}"
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
        await message.reply(f"🔑 Ключ подключён!\nСервер: <code>{BASE_URL}</code>\nМодель: <code>{CHAT_MODEL}</code>", reply_markup=build_groups_keyboard("grp"), parse_mode="HTML")
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
        elif action == "pup_ai":
            reply = await generate_phil_reply(int(cid), "Инициатива", speaker_key="Создатель", special_task=f"Напиши в чат: {text}")
            if reply:
                clean = clean_phil_text(re.sub(r"\[СТИКЕР\s*:\s*[^\]]+\]", "", reply), chat_id=int(cid))
                await bot.send_message(int(cid), clean)
                await message.reply(f"🧠 Вброшено: {clean}", reply_markup=build_group_menu_keyboard(cid))
        return

    await handle_admin_ai_assistant(message)


# --- ОБРАБОТКА ГРУППОВЫХ СООБЩЕНИЙ ---

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
    custom_title = await get_user_custom_title(chat_id, message.from_user.id) if message.from_user else ""

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
        s_desc = await analyze_and_remember_sticker(chat_id, message.sticker, prev_ctx)
        emoji = message.sticker.emoji or "😎"
        chat_history[chat_id].append(f"{speaker_key} [стикер {emoji}: {s_desc}]")
        if gdata.get("mode") != "silent" and is_reply_to_bot and now >= muted_until[chat_id]:
            img_b64, m_type = await extract_visual_base64(message)
            reply = await generate_phil_reply(chat_id, f"[ответил стикером {emoji}: {s_desc}]", speaker_key, custom_title, "", img_b64, m_type, replied_to_exact)
            await send_human_like_reply(message, reply, incoming_sticker_uid=message.sticker.file_unique_id)
        return

    if message.voice or message.video_note:
        dur = message.voice.duration if message.voice else message.video_note.duration
        mlabel = "голосовое" if message.voice else "кружочек"
        chat_history[chat_id].append(f"{speaker_key} [{mlabel} {dur} сек]")
        if gdata.get("mode") != "silent" and now >= muted_until[chat_id] and (is_reply_to_bot or (dur > 40 and random.random() < 0.25)):
            img_b64, m_type = await extract_visual_base64(message)
            reply = await generate_phil_reply(chat_id, f"[{mlabel} {dur} сек]", speaker_key, custom_title, "Подколи коротко, что не слушаешь войсы и пусть пишет буквами.", img_b64, m_type)
            await send_human_like_reply(message, reply)
        return

    raw_text = message.text or message.caption or ""
    has_visual = bool(message.photo or message.video or message.animation or (message.reply_to_message and (message.reply_to_message.photo or message.reply_to_message.video or message.reply_to_message.sticker)))
    if (not raw_text and not has_visual) or raw_text.startswith("/"):
        return

    text_lower = raw_text.lower()
    is_mentioned = f"@{bot_info.username.lower()}" in text_lower
    words_in_msg = [w.strip(".,!?()\"'") for w in text_lower.split()]
    is_named = any(w.startswith(tuple(BOT_NAMES)) for w in words_in_msg)

    if is_forwarded and not (is_mentioned or is_named or is_reply_to_bot):
        return

    formatted_msg = f"{speaker_key}: {raw_text or '[Медиа]'}"
    if not is_forwarded:
        chat_history[chat_id].append(formatted_msg)
        message_counters[chat_id] += 1
        proactive_counters[chat_id] += 1

    if message_counters[chat_id] >= LEARN_EVERY_N_MESSAGES:
        message_counters[chat_id] = 0
        asyncio.create_task(learn_from_messages(chat_id, "\n".join(list(chat_history[chat_id])[-20:])))

    if gdata.get("mode") == "silent":
        return

    has_family_mention = any(ft in text_lower for ft in FAMILY_TRIGGERS)
    if not (is_mentioned or is_reply_to_bot or is_named or has_family_mention) and random.random() < REACTION_CHANCE:
        try:
            await message.react([ReactionTypeEmoji(emoji=random.choice(["🤡", "💀", "🔥", "💩", "👍"]))])
        except Exception:
            pass

    other_mentions = [w for w in raw_text.split() if w.startswith("@") and w.lower() != f"@{bot_info.username.lower()}"]
    is_talking_to_someone_else = is_reply_to_other or bool(other_mentions)
    hot_state = last_phil_dialog[chat_id]
    is_hot_dialog = bool(message.from_user and hot_state["user_id"] == message.from_user.id and (now - hot_state["time"]) <= HOT_DIALOG_SECONDS and not is_talking_to_someone_else and len(raw_text) > 2 and random.random() < 0.65)

    # Авто-ответ на шутки про родных (коротко, с ухмылкой, без детского нытья)
    if has_family_mention and not is_forwarded and now >= muted_until[chat_id]:
        victim = f"пацана {replied_user_name}" if is_reply_to_other else ("тебя" if (is_mentioned or is_named or is_reply_to_bot) else "кого-то в чате")
        ftask = (
            f"В сообщении упомянуты родные («{raw_text}»).\n"
            f"- Если это безобидная шутка (например «хотел маму увидеть без негатива») — НЕ ЗЛИСЬ и НЕ ОБИЖАЙСЯ! Просто угарни в 2-5 слов (например: «ахахахха ебень сука» или коротко подколи в ответ).\n"
            f"- Если это жёсткий наезд на родных {victim} — без нытья и без повторения его слов в 1 короткую фразу жёстко осади [{speaker_key}] (разрешено пройтись по его родным в ответ).\n"
            f"- Если просто бытовой разговор — {'ответь нормально' if (is_mentioned or is_reply_to_bot or is_named) else 'напиши SKIP'}."
        )
        img_b64, m_type = await extract_visual_base64(message) if has_visual else (None, "")
        reply = await generate_phil_reply(chat_id, formatted_msg, speaker_key, custom_title, ftask, img_b64, m_type, replied_to_exact)
        if reply and "SKIP" not in reply.upper():
            await send_human_like_reply(message, reply)
            return
        if is_mentioned or is_reply_to_bot or is_named:
            return

    if is_mentioned or is_reply_to_bot or is_named or is_hot_dialog:
        if now < muted_until[chat_id]:
            return
        if not is_hot_dialog:
            call_timestamps[chat_id] = [t for t in call_timestamps[chat_id] if now - t < 120] + [now]
            if len(call_timestamps[chat_id]) > SPAM_LIMIT_MESSAGES:
                muted_until[chat_id] = now + 240
                await message.reply("да отъебитесь вы нахуй, я срать отошёл, через пару минут буду")
                return

        proactive_counters[chat_id] = 0
        img_b64, m_type = await extract_visual_base64(message) if has_visual else (None, "")
        task = ""

        if any(aw in text_lower for aw in ["на базе какого", "какого ии", "ты ии", "ты бот", "прикольный ии", "нейросеть", "chatgpt", "гпт", "промпт"]):
            task = "Тебя подозревают в том, что ты ИИ. Угарни коротко (например: «ахахах ебень какой ии, я с тапка сижу пивас пью»)."
        elif any(nt in text_lower for nt in NEWS_TRIGGERS):
            news_summary = await fetch_fresh_news(raw_text)
            if news_summary:
                task = f"Свежие новости из интернета по теме: [{news_summary}]. Коротко, своими словами по-пацански скажи, что слышал об этом, и прокомментируй!"
        elif any(kw in text_lower for kw in ["рассуди", "кто прав", "разрули"]):
            task = "Коротко в 1-2 предложениях рассуди пацанов по последним сообщениям."
        elif any(kw in text_lower for kw in ["че тут было", "чё тут было", "че пропустил", "о чем базар"]):
            task = "В 1-2 предложениях перескажи, о чём базарили в последних сообщениях."

        reply = await generate_phil_reply(chat_id, formatted_msg, speaker_key, custom_title, task, img_b64, m_type, replied_to_exact)
        await send_human_like_reply(message, reply)
        return

    if now >= muted_until[chat_id] and proactive_counters[chat_id] >= next_proactive_target[chat_id] and len(raw_text) > 5:
        proactive_counters[chat_id] = 0
        next_proactive_target[chat_id] = random.randint(PROACTIVE_MIN_MSGS, PROACTIVE_MAX_MSGS)
        img_b64, m_type = await extract_visual_base64(message) if has_visual else (None, "")
        reply = await generate_phil_reply(chat_id, formatted_msg, speaker_key, custom_title, "Коротко вставь свои 5 копеек строго по теме последних 2 сообщений.", img_b64, m_type)
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
                reply = await generate_phil_reply(cid, "Тишина", speaker_key="Чат", special_task="В чате тишина. Закинь короткий вопрос пацанам.")
                if reply:
                    clean = clean_phil_text(re.sub(r"\[СТИКЕР\s*:\s*[^\]]+\]", "", reply), chat_id=cid)
                    try:
                        await bot.send_message(cid, clean)
                    except Exception:
                        pass


async def health_check(request):
    return web.Response(text=f"Phil v8 Alive | {BASE_URL} | {CHAT_MODEL}")


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