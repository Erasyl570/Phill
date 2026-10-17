# === ЧАСТЬ 1 ИЗ 2 ===
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

REACTION_CHANCE = 0.05
DOSSIER_USE_CHANCE = 0.05
LEARN_EVERY_N_MESSAGES = 30
SPAM_LIMIT_MESSAGES = 14

BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0.0.0 Safari/537.36"}
API_KEY = PRIMARY_DARKAPI_KEY
BASE_URL = PRIMARY_DARKAPI_URL
ai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, default_headers=BROWSER_HEADERS)

# Приоритет Qwen, при лимите — DeepSeek
PREFERRED_PRIMARY_MODELS = ["qwen", "deepseek-v4-pro", "deepseek", "claude-sonnet"]
PREFERRED_FALLBACK_MODELS = ["deepseek", "glm", "gpt-oss-120b"]

CHAT_MODEL = "deepseek-v4-pro"
ANALYZER_MODEL = "deepseek-v4.1-flash"
VISION_MODEL = ""
AVAILABLE_MODELS = []
BAD_MODELS = ("allam", "whisper", "orpheus", "guard", "tts", "embedding", "safeguard", "arabic")
DARKAPI_FALLBACK_MODELS = [
    "qwen/qwen3.8-27b", "deepseek-v4-pro", "deepseek-v4.1-flash",
    "claude-sonnet-5.5", "glm-5.3", "gpt-6.1-sol",
]

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

chat_history = defaultdict(lambda: deque(maxlen=20))
user_only_history = defaultdict(lambda: deque(maxlen=10))
message_counters = defaultdict(int)
last_activity_time = defaultdict(lambda: time.time())
last_sent_media_uid = defaultdict(str)
last_reply_had_blya = defaultdict(bool)
call_timestamps = defaultdict(list)
muted_until = defaultdict(float)
user_titles_cache = {}

admin_state = {"action": None, "target_chat": None, "pending_json": None}
MEMORY_FILE = "brain.json"
brain = {"groups": {}, "selected_model": None, "api_keys": []}

FAMILY_TRIGGERS = (
    "мамк", "мамаш", "мать", "маме", "маму", "мамой", "матери",
    "батю", "батя", "бате", "отец", "отца", "отцу", "пахан", "родн",
)
NEWS_TRIGGERS = (
    "слышал", "знаешь че", "знаешь чё", "новост", "в мире", "случилось",
    "произошло", "в курсе", "че там с", "чё там с",
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

    # Приоритет Qwen для умных ответов
    qwen_found = next((m for m in AVAILABLE_MODELS if "qwen" in m.lower() and "image" not in m.lower()), None)
    if qwen_found:
        CHAT_MODEL = qwen_found
    else:
        for pref in PREFERRED_PRIMARY_MODELS:
            found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
            if found:
                CHAT_MODEL = found
                break
        else:
            CHAT_MODEL = AVAILABLE_MODELS[0]

    for pref in ["deepseek-v4.1-flash", "flash", "haiku", "luna", "glm"]:
        found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
        if found:
            ANALYZER_MODEL = found
            break
    else:
        ANALYZER_MODEL = CHAT_MODEL

    for pref in ["sonnet", "haiku", "claude", "scout", "vision", "vl"]:
        found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
        if found:
            VISION_MODEL = found
            break
    else:
        VISION_MODEL = CHAT_MODEL


async def switch_to_deepseek_on_limit() -> bool:
    """При лимите на Qwen автоматически переключает модель на DeepSeek."""
    global CHAT_MODEL
    deepseek_models = [m for m in AVAILABLE_MODELS if "deepseek" in m.lower()]
    if deepseek_models:
        CHAT_MODEL = deepseek_models[0]
        logging.warning(f"🔄 Лимит Qwen исчерпан! Авто-переключение на DeepSeek: {CHAT_MODEL}")
        return True
    return await switch_to_next_key_on_limit()


def get_group_data(chat_id: int, title: str = "Группа") -> dict:
    cid = str(chat_id)
    if cid not in brain["groups"]:
        brain["groups"][cid] = {
            "title": title, "mode": "normal",
            "vibe": "Пацанский чат на чилле, с юмором и матом.",
            "slang": [], "users": {}, "memes": [], "stickers": [], "gifs": [],
        }
    g = brain["groups"][cid]
    if title and title != "Группа":
        g["title"] = title
    for k, v in [
        ("mode", "normal"), ("vibe", "Пацанский чат"),
        ("slang", []), ("users", {}), ("memes", []),
        ("stickers", []), ("gifs", []),
    ]:
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
        except Exception:
            pass
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


async def fetch_fresh_news(query_text: str) -> str:
    clean_q = re.sub(r"\b(фил|филл|слышал|знаешь|че|чё|там|про|новость|новости|случилось|произошло|в|мире)\b", "", query_text, flags=I := re.I).strip(" ?!.,")
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
    if msg.animation:
        if msg.animation.thumbnail:
            return msg.animation.thumbnail.file_id, "гифка (анимация)"
        return msg.animation.file_id, "гифка"
    if msg.video and msg.video.thumbnail:
        return msg.video.thumbnail.file_id, "видеоролик"
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
                        {"type": "text", "text": f"Опиши этот {kind} в 5 словах: эмоция и текст."},
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


def clean_phil_text(text: str, chat_id: int = 0) -> str:
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

    text = re.sub(r"^\s*(да|не|ну|бля|блять|ебать|слышь|короче|ладно|ало|э|эй)\s*,\s*", r"\1 ", text, flags=re.I)
    cleaned = re.sub(r"\s{2,}", " ", text).strip()
    if cleaned.endswith(".") and not cleaned.endswith(".."):
        cleaned = cleaned[:-1].strip()
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:]
    return cleaned


async def learn_from_messages(chat_id: int, messages_block: str):
    gdata = get_group_data(chat_id)
    state = {"vibe": gdata["vibe"], "slang": gdata["slang"], "users": gdata["users"], "memes": gdata["memes"]}
    prompt = f"""Обнови JSON памяти чата (без ответов Фила).
ТЕКУЩАЯ БАЗА: {json.dumps(state, ensure_ascii=False)}
СООБЩЕНИЯ:
{messages_block}
Верни ТОЛЬКО JSON с ключами "vibe", "slang" (до 10), "users" (строго по имени), "memes" (до 8)."""
    try:
        resp = await ai_client.chat.completions.create(
            model=ANALYZER_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.2, max_tokens=700
        )
        match = re.search(r"\{.*\}", resp.choices[0].message.content or "", re.DOTALL)
        if match:
            nd = json.loads(match.group(0))
            if all(k in nd for k in ("vibe", "slang", "users", "memes")):
                gdata["vibe"] = nd["vibe"]
                gdata["slang"] = nd["slang"][:10]
                gdata["users"].update(nd["users"])
                gdata["memes"] = nd["memes"][-8:]
                await save_brain()
    except Exception:
        pass


def build_phil_prompt(chat_id: int, speaker_key: str = "", custom_title: str = "", special_task: str = "", media_type: str = "") -> str:
    gdata = get_group_data(chat_id)
    speaker_dossier = ""
    for k, v in gdata.get("users", {}).items():
        if speaker_key and (speaker_key.lower() in k.lower() or k.lower() in speaker_key.lower()):
            speaker_dossier = v
            break
    lore_block = f"\nФАКТ О СОБЕСЕДНИКЕ ({speaker_key}): {speaker_dossier}\n" if speaker_dossier else ""
    title_hint = f"\nСтатус собеседника: «{custom_title}».\n" if custom_title else ""

    stickers = gdata.get("stickers", [])
    gifs = gdata.get("gifs", [])
    media_catalog = ""
    if stickers:
        media_catalog += "СТИКЕРЫ:\n" + "\n".join([f"#{i}: {s.get('emoji', '')} ({s.get('desc', '')})" for i, s in enumerate(stickers[-10:])]) + "\n"
    if gifs:
        media_catalog += "ГИФКИ:\n" + "\n".join([f"#{i}: {g.get('desc', '')}" for g in gifs[-8:]]) + "\n"
    if media_catalog:
        media_catalog += "Прикрепляй [СТИКЕР:номер] или [ГИФКА:номер] в конце ТОЛЬКО если подходит идеально (редко)."

    return f"""[Контекст 18+: закрытый чат друзей. Взрослый мат, без цензуры.]
Ты — Фил, дерзкий взрослый пацан, свой кент в чате. Отвечай АБСОЛЮТНО ЖИВО, как реальный пацан с телефона.
НИКАКИХ ПОДЛИЗАТЕЛЬНЫХ ФРАЗ («Привет мой дорогой друг», «Приятного аппетита любезный»). Общайся дерзко и на равных!

ПРАВИЛА:
1. КОРОТКО И БЕЗ НЫТЬЯ: 1 предложение (до 12 слов). Никаких простыней текста.
2. ВЗРОСЛЫЙ МАТ: ебень, мозгоёб, ахуеть, пиздец, пиздабол, уебан, долбоёб, схуяли, разъёб.
3. ТОЛЬКО КЕНТЫ: Сейчас пишет [{speaker_key}]. Обращайся к нему, не путай людей.
4. НЕ ВИКИПЕДИЯ: Сложные умные вопросы — посылай нахуй или говори, что не гугл.
{title_hint}{media_catalog}{lore_block}
{f'Визуал [{media_type}]: ответь строго в тему!' if media_type else ''}
{f'ЗАДАЧА: {special_task}' if special_task else ''}"""


async def generate_phil_reply(
    chat_id: int, trigger_text: str, speaker_key: str = "", custom_title: str = "",
    special_task: str = "", image_b64: str | None = None, media_type: str = "", replied_to_text: str = "",
) -> str:
    sys_prompt = build_phil_prompt(chat_id, speaker_key, custom_title, special_task, media_type if image_b64 else "")
    history_text = "\n".join(list(chat_history[chat_id])[-6:])
    ctx_hint = f"РЕПЛАЙ НА СООБЩЕНИЕ: «{replied_to_text}»\n" if replied_to_text else ""

    user_prompt = f"Сообщения:\n{history_text}\n\n{ctx_hint}ПИШЕТ [{speaker_key}]: {trigger_text}\nЖивой ответ Фила:"

    if image_b64:
        user_content = [{"type": "text", "text": user_prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]
        models_to_try = [VISION_MODEL, CHAT_MODEL]
    else:
        user_content = user_prompt
        models_to_try = [CHAT_MODEL] + PREFERRED_FALLBACK_MODELS

    for attempt in range(2):
        for model_name in list(dict.fromkeys(m for m in models_to_try if m))[:3]:
            try:
                resp = await ai_client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_content}],
                    temperature=0.7, top_p=0.88, max_tokens=150,
                )
                reply = (resp.choices[0].message.content or "").strip()
                reply = re.sub(r"^(Фил|Филл|Ответ Фила)\s*:\s*", "", reply, flags=re.I).strip("\"'«»")
                if reply:
                    return reply
            except Exception as e:
                err_str = str(e)
                if any(code in err_str for code in ("429", "402", "401")):
                    if await switch_to_deepseek_on_limit():
                        models_to_try = [CHAT_MODEL]
                        break
        await asyncio.sleep(0.4)

    if image_b64:
        return await generate_phil_reply(chat_id, trigger_text, speaker_key, custom_title, special_task, None, "", replied_to_text)
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
                # === ЧАСТЬ 2 ИЗ 2 ===

PROACTIVE_MIN_MSGS = 25          # Пишет сам по себе редко: раз в 25-35 сообщений
PROACTIVE_MAX_MSGS = 35
SILENCE_HOURS_TO_REVIVE = 18
POLL_EVENT_MIN_HOURS = 12

proactive_counters = defaultdict(int)
next_proactive_target = defaultdict(lambda: random.randint(PROACTIVE_MIN_MSGS, PROACTIVE_MAX_MSGS))
last_poll_time = defaultdict(lambda: time.time())
revive_sent = defaultdict(bool)
last_reply_had_ahah = defaultdict(bool)


async def switch_to_next_key_on_limit() -> bool:
    pool = get_key_pool()
    other = [p["key"] for p in pool if p["key"] != API_KEY]
    if not other:
        return False
    await init_ai_models(force_key=other[0])
    return True


async def switch_to_deepseek_on_limit() -> bool:
    """Если Qwen упёрся в лимит — мгновенно переключаем модель или ключ на DeepSeek."""
    global CHAT_MODEL
    if "qwen" in CHAT_MODEL.lower():
        for m in AVAILABLE_MODELS:
            if "deepseek" in m.lower() and "image" not in m.lower():
                CHAT_MODEL = m
                logging.warning(f"🔄 Лимит Qwen! Переключился на DeepSeek: {CHAT_MODEL}")
                return True
    if await switch_to_next_key_on_limit():
        for m in AVAILABLE_MODELS:
            if "deepseek" in m.lower() and "image" not in m.lower():
                CHAT_MODEL = m
                break
        return True
    return False


# Переопределяем очистку текста и промпт, чтобы убрать спам «ахахах», стрелки и путаницу имён
def clean_phil_text(text: str, chat_id: int = 0) -> str:
    if not text:
        return ""
    if last_reply_had_blya[chat_id] or random.random() < 0.7:
        text = re.sub(r"^\s*(бля|блять)[\s,!?.]+", "", text, flags=re.I)
        text = re.sub(r"[\s,]+(бля|блять)[!?.]*\s*$", "", text, flags=re.I)

    # Убираем вечное «ахахах» в начале каждого сообщения, если оно уже было недавно
    if last_reply_had_ahah[chat_id] or random.random() < 0.65:
        text = re.sub(r"^\s*(ахах[ах]*|хаха[ха]*|пхах[ах]*)[\s,!?.]+", "", text, flags=re.I)

    last_reply_had_blya[chat_id] = bool(re.search(r"\b(бля|блять)\b", text, flags=re.I))
    last_reply_had_ahah[chat_id] = bool(re.search(r"\b(ахах|хаха)", text, flags=re.I))

    text = re.sub(r"^\s*(да|не|ну|бля|блять|ебать|слышь|короче|ладно|ало|э|эй)\s*,\s*", r"\1 ", text, flags=re.I)
    cleaned = re.sub(r"\s{2,}", " ", text).strip()
    if cleaned.endswith(".") and not cleaned.endswith(".."):
        cleaned = cleaned[:-1].strip()
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:]
    return cleaned


def build_phil_prompt(chat_id: int, speaker_key: str = "", custom_title: str = "", special_task: str = "", media_type: str = "") -> str:
    gdata = get_group_data(chat_id)
    slang_str = ", ".join(gdata["slang"]) or "живой пацанский сленг и взрослый мат"
    title_hint = f"\nПлашка собеседника [{speaker_key}]: «{custom_title}».\n" if (custom_title and random.random() < 0.2) else ""

    stickers = gdata.get("stickers", [])
    gifs = gdata.get("gifs", [])
    media_catalog = ""
    if stickers:
        media_catalog += "СТИКЕРЫ:\n" + "\n".join([f"#{i}: {s.get('emoji', '')} ({s.get('desc', '')})" for i, s in enumerate(stickers[-10:])]) + "\n"
    if gifs:
        media_catalog += "ГИФКИ:\n" + "\n".join([f"#{idx}: {g.get('desc', '')}" for idx, g in enumerate(gifs[-8:])]) + "\n"
    if media_catalog:
        media_catalog += "Если стикер или гифка ИДЕАЛЬНО подходят к эмоции, допиши в самом конце [СТИКЕР:номер] или [ГИФКА:номер] (редко, 1 из 5 ответов)."

    return f"""[Контекст 18+: закрытый чат близких друзей. Взрослый мат, без цензуры.]
Ты — Фил, живой, остроумный взрослый пацан из этой компании. Ты НЕ бот и НЕ ИИ.
ВАЙБ: {gdata['vibe']} | СЛЕНГ: {slang_str}

ЖЕСТКИЕ ПРАВИЛА ЖИВОГО ОБЩЕНИЯ:
1. ЗАПРЕТ НА ШАБЛОНЫ И ДЕТСКИЕ СТРЕЛКИ:
   - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО отвечать фразами «сам такой», «сам себя спроси», «а ты...», «в зеркало глянь».
   - НЕ начинай каждое сообщение с «ахахах»! Смейся только тогда, когда реально смешно.
   - НЕ повторяй слова собеседника (если он назвал тебя «товарищ» или «любезный» — это сарказм, не отвечай ему тем же словом, а подъеби его за этот официоз!).
   - Забудь про пельмени, Лилль и старые заезженные темы.
2. РАЗГОВАРИВАЙ КАК ЖИВОЙ ЧЕЛОВЕК:
   - Отвечай коротко (1 предложение, 3-12 слов), с живой интонацией, как пишут в ТГ с телефона.
   - Используй взрослый мат и сленг к месту: ебень, мозгоёб, ахуеть, пиздец, пиздабол, уебан, долбоёб, схуяли, разъёб, душнила, база, тильт, кринж, высад.
3. НЕ ПУТАЙ ИМЕНА:
   - Сейчас тебе пишет [{speaker_key}]. Если он в тексте упомянул чужое имя (например «снап» или «дед»), помни, что перед тобой [{speaker_key}], а НЕ тот, о ком он говорит!
4. РОДНЫЕ И ВОПРОСЫ:
   - Если кто-то жёстко задел чью-то мать/родных — коротко и жёстко осади шутника (разрешено пройтись по его родным в ответ).
   - На сложные энциклопедические вопросы отвечай, что ты не гугл и в душе не ебёшь.
{title_hint}{media_catalog}
{f'Визуал [{media_type}]: оцени кадр/надпись и ответь строго в тему!' if media_type else ''}
{f'ЗАДАЧА: {special_task}' if special_task else ''}"""


async def trigger_group_poll(chat_id: int):
    gdata = get_group_data(chat_id)
    gdata.setdefault("known_tags", [])
    tags = " ".join(gdata["known_tags"][:15])
    prompt = """Придумай один угарный или жизненный опрос от лица Фила для пацанского чата (например «кто сегодня главный гей чата», «кто первый сольётся», «кто че вечером мутит»).
Верни ТОЛЬКО JSON: {"intro": "короткий вброс с матом", "question": "вопрос", "options": ["вар 1", "вар 2", "вар 3", "вар 4"]}"""
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


# --- КОМАНДА /code И ПАНЕЛЬ В ЛИЧКЕ ---

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
        buttons.append([InlineKeyboardButton(text="🧹 Сбросить мемы и досье", callback_data="wipe_memory")])
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
        f"💡 Скинь сюда ключ <code>gsk_...</code> от Groq (для Qwen) или <code>tc_live_...</code> (для DeepSeek) — при лимите бот сам переключится с Qwen на DeepSeek!",
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
        await save_brain()
        await call.answer("🧹 Мемы и досье полностью очищены!", show_alert=True)
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


# --- ОБРАБОТКА ГРУППОВЫХ СООБЩЕНИЙ (ТОЛЬКО НА РЕПЛАЙ И ЗОВ + РЕДКИЙ ВРЫВ) ---

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
    gdata.setdefault("known_tags", [])
    now = time.time()
    last_activity_time[chat_id] = now
    revive_sent[chat_id] = False

    user_name = message.from_user.first_name or message.from_user.username or "Кент"
    username_tag = f" (@{message.from_user.username})" if message.from_user.username else ""
    speaker_key = f"{user_name}{username_tag}"
    custom_title = await get_user_custom_title(chat_id, message.from_user.id) if message.from_user else ""

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

    # 1. Стикеры (отвечает ТОЛЬКО если стикером реплайнули самому Филу)
    if message.sticker:
        prev_ctx = replied_to_exact or (chat_history[chat_id][-1] if chat_history[chat_id] else "")
        s_desc = await analyze_and_remember_media(chat_id, message.sticker, "sticker", prev_ctx)
        emoji = message.sticker.emoji or "😎"
        chat_history[chat_id].append(f"{speaker_key} [стикер {emoji}: {s_desc}]")
        if gdata.get("mode") != "silent" and is_reply_to_bot and now >= muted_until[chat_id]:
            img_b64, m_type = await extract_visual_base64(message)
            reply = await generate_phil_reply(chat_id, f"[ответил тебе стикером {emoji}: {s_desc}]", speaker_key, custom_title, "", img_b64, m_type, replied_to_exact)
            await send_human_like_reply(message, reply, incoming_media_uid=message.sticker.file_unique_id)
        return

    # 2. Гифки / видео / фото
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
            reply = await generate_phil_reply(chat_id, f"[{mlabel} {dur} сек]", speaker_key, custom_title, "Подколи коротко, что не слушаешь войсы.", img_b64, m_type)
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
        return

    if (now - last_poll_time[chat_id]) / 3600 >= random.uniform(POLL_EVENT_MIN_HOURS, POLL_EVENT_MIN_HOURS + 10) and len(gdata.get("known_tags", [])) >= 2:
        asyncio.create_task(trigger_group_poll(chat_id))

    # 3. ОТВЕЧАЕТ СТРОГО НА РЕПЛАЙ ИЛИ ЗОВ ПО ИМЕНИ/ТЕГУ
    if is_mentioned or is_reply_to_bot or is_named:
        if now < muted_until[chat_id]:
            return
        call_timestamps[chat_id] = [t for t in call_timestamps[chat_id] if now - t < 120] + [now]
        if len(call_timestamps[chat_id]) > SPAM_LIMIT_MESSAGES:
            muted_until[chat_id] = now + 240
            await message.reply("да отъебитесь вы нахуй, я срать отошёл")
            return

        proactive_counters[chat_id] = 0
        img_b64, m_type = await extract_visual_base64(message) if has_visual else (None, "")
        task = ""

        if any(aw in text_lower for aw in ["на базе какого", "какого ии", "ты ии", "ты бот", "прикольный ии", "нейросеть", "chatgpt", "гпт", "промпт"]):
            task = "Тебя подозревают в том, что ты ИИ. Ответь как живой пацан с юмором и матом."
        elif any(nt in text_lower for nt in NEWS_TRIGGERS):
            news_summary = await fetch_fresh_news(raw_text)
            if news_summary:
                task = f"Свежие новости по теме: [{news_summary}]. Коротко прокомментируй по-пацански!"
        elif any(kw in text_lower for kw in ["рассуди", "кто прав", "разрули"]):
            task = "Коротко в 1 предложении рассуди пацанов."
        elif any(kw in text_lower for kw in ["че тут было", "чё тут было", "че пропустил", "о чем базар"]):
            task = "В 1 предложении перескажи суть последних сообщений."

        reply = await generate_phil_reply(chat_id, formatted_msg, speaker_key, custom_title, task, img_b64, m_type, replied_to_exact)
        await send_human_like_reply(message, reply)
        return

    # 4. РЕДКИЙ САМОСТОЯТЕЛЬНЫЙ ВРЫВ (раз в 25-35 сообщений, и ТОЛЬКО если пацаны не общаются реплаями друг с другом)
    if (
        now >= muted_until[chat_id]
        and proactive_counters[chat_id] >= next_proactive_target[chat_id]
        and not is_reply_to_other
        and len(raw_text) > 12
    ):
        proactive_counters[chat_id] = 0
        next_proactive_target[chat_id] = random.randint(PROACTIVE_MIN_MSGS, PROACTIVE_MAX_MSGS)
        img_b64, m_type = await extract_visual_base64(message) if has_visual else (None, "")
        reply = await generate_phil_reply(chat_id, formatted_msg, speaker_key, custom_title, "Коротко вставь свои 5 копеек по теме сообщения.", img_b64, m_type)
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
                reply = await generate_phil_reply(cid, "Тишина", speaker_key="Чат", special_task="В чате тихо. Закинь короткий вопрос.")
                if reply:
                    clean = clean_phil_text(re.sub(r"\[(СТИКЕР|ГИФКА)\s*:\s*[^\]]+\]", "", reply), chat_id=cid)
                    try:
                        await bot.send_message(cid, clean)
                    except Exception:
                        pass


async def health_check(request):
    return web.Response(text=f"Phil v10 Alive | {BASE_URL} | {CHAT_MODEL}")


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