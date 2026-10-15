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

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Message,
    ReactionTypeEmoji,
)
from openai import AsyncOpenAI

logging.basicConfig(level=logging.INFO)

# --- НАСТРОЙКИ ОКРУЖЕНИЯ (RENDER) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_KEY = (os.getenv("API_KEY") or os.getenv("GROQ_API_KEY") or "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))

# Имена, на которые откликается Фил
BOT_NAMES = ["фил", "филл", "филя", "филип"]

# Настройки вероятностей и лимитов
RANDOM_REPLY_CHANCE = float(os.getenv("RANDOM_REPLY_CHANCE", "0.06"))
REACTION_CHANCE = 0.06
STICKER_ONLY_CHANCE = 0.05  # Шанс молча вкинуть стикер в тему разговора
DOSSIER_USE_CHANCE = 0.15   # Только в 15% ответов Фил получает доступ к старому досье/мемам
LEARN_EVERY_N_MESSAGES = 15
SILENCE_HOURS_TO_REVIVE = 18
SPAM_LIMIT_MESSAGES = 12    # Лимит: 12 обращений за 2 минуты

# Определяем адрес сервера автоматически
if API_KEY.startswith("tc_"):
    BASE_URL = os.getenv("BASE_URL", "https://darkapi.shop/v1").strip()
elif API_KEY.startswith("sk-"):
    BASE_URL = os.getenv("BASE_URL", "https://api.deepseek.com").strip()
else:
    BASE_URL = os.getenv("BASE_URL", "https://api.groq.com/openai/v1").strip()

ai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)

CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-120b")
ANALYZER_MODEL = os.getenv("ANALYZER_MODEL", "openai/gpt-oss-20b")
VISION_MODEL = os.getenv("VISION_MODEL", "")
AVAILABLE_MODELS = []

# Чёрный список нерусских и звуковых моделей
BAD_MODELS = ("allam", "whisper", "orpheus", "guard", "tts", "embedding", "safeguard", "arabic")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# --- ОПЕРАТИВНАЯ ПАМЯТЬ ---
chat_history = defaultdict(lambda: deque(maxlen=40))
message_counters = defaultdict(int)
last_activity_time = defaultdict(lambda: time.time())
revive_sent = defaultdict(bool)

call_timestamps = defaultdict(list)
muted_until = defaultdict(float)

admin_state = {
    "action": None,
    "target_chat": None,
    "pending_json": None,
}

MEMORY_FILE = "brain.json"
brain = {"groups": {}, "selected_model": None}


# --- АВТО-СКАНЕР МОДЕЛЕЙ (ЧАТ + АНАЛИЗ + ЗРЕНИЕ ДЛЯ ФОТО) ---

async def init_ai_models():
    global CHAT_MODEL, ANALYZER_MODEL, VISION_MODEL, AVAILABLE_MODELS, ai_client, BASE_URL

    urls_to_try = [BASE_URL, "https://darkapi.shop/v1", "https://api.darkapi.shop/v1"]
    for url in urls_to_try:
        try:
            test_client = AsyncOpenAI(api_key=API_KEY, base_url=url)
            models_resp = await test_client.models.list()
            clean_ids = [
                m.id for m in models_resp.data
                if not any(bad in m.id.lower() for bad in BAD_MODELS)
            ]
            if clean_ids:
                BASE_URL = url
                ai_client = test_client
                AVAILABLE_MODELS = sorted(clean_ids)
                logging.info(f"✅ Подключено к {BASE_URL}! Модели: {AVAILABLE_MODELS}")
                break
        except Exception as e:
            logging.warning(f"Проверка {url}: {e}")

    if not AVAILABLE_MODELS:
        return

    saved_model = brain.get("selected_model")
    if saved_model and saved_model in AVAILABLE_MODELS and not any(b in saved_model.lower() for b in BAD_MODELS):
        CHAT_MODEL = saved_model
    else:
        preferred_chat = ["deepseek-v4-pro", "deepseek", "claude-sonnet", "qwen", "gpt-oss-120b", "gpt-6", "gpt-5"]
        for pref in preferred_chat:
            found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
            if found:
                CHAT_MODEL = found
                break
        else:
            CHAT_MODEL = AVAILABLE_MODELS[0]
        brain["selected_model"] = CHAT_MODEL

    preferred_fast = ["deepseek-v4.1-flash", "flash", "haiku", "gpt-oss-20b", "qwen", "deepseek"]
    for pref in preferred_fast:
        found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
        if found:
            ANALYZER_MODEL = found
            break
    else:
        ANALYZER_MODEL = CHAT_MODEL

    # Ищем модель с поддержкой зрения (Vision) для чтения фотографий
    preferred_vision = ["scout", "maverick", "vision", "vl", "claude", "gpt-6", "gpt-5", "gpt-4", "gemini", "gpt-oss-120b"]
    for pref in preferred_vision:
        found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
        if found:
            VISION_MODEL = found
            break
    else:
        VISION_MODEL = CHAT_MODEL

    logging.info(f"🎯 Чат: {CHAT_MODEL} | Анализ: {ANALYZER_MODEL} | Фото (Vision): {VISION_MODEL}")


def get_group_data(chat_id: int, title: str = "Группа") -> dict:
    cid = str(chat_id)
    if cid not in brain["groups"]:
        brain["groups"][cid] = {
            "title": title,
            "mode": "normal",
            "vibe": "Дружеский пацанский чат, общаются на равных, рофлят в тему, иногда с матом.",
            "slang": [],
            "users": {},
            "memes": [],
            "stickers": [],
        }
    g = brain["groups"][cid]
    if title and title != "Группа":
        g["title"] = title
    g.setdefault("mode", "normal")
    g.setdefault("vibe", "Дружеский пацанский чат, общаются на равных, рофлят в тему, иногда с матом.")
    g.setdefault("slang", [])
    g.setdefault("users", {})
    g.setdefault("memes", [])
    g.setdefault("stickers", [])
    return g


# --- ВЕЧНАЯ ПАМЯТЬ ЧЕРЕЗ ЗАКРЕПЛЁННЫЙ ФАЙЛ В ТВОЕЙ ЛИЧКЕ ---

async def load_brain():
    global brain
    if ADMIN_ID:
        try:
            chat = await bot.get_chat(ADMIN_ID)
            if chat.pinned_message and chat.pinned_message.document:
                doc = chat.pinned_message.document
                if doc.file_name == "brain.json":
                    buf = io.BytesIO()
                    await bot.download(doc.file_id, destination=buf)
                    brain = json.loads(buf.getvalue().decode("utf-8"))
                    logging.info("Память Фила успешно загружена из закрепа ТГ!")
                    return
        except Exception as e:
            logging.warning(f"Не удалось загрузить файл памяти из закрепа: {e}")

    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                brain = json.load(f)
        except Exception as e:
            logging.error(f"Ошибка чтения локального файла: {e}")


async def save_brain():
    raw_bytes = json.dumps(brain, ensure_ascii=False, indent=2).encode("utf-8")
    with open(MEMORY_FILE, "wb") as f:
        f.write(raw_bytes)

    if not ADMIN_ID:
        return

    try:
        chat = await bot.get_chat(ADMIN_ID)
        input_file = BufferedInputFile(raw_bytes, filename="brain.json")
        caption = f"🧠 База памяти Фила (Групп: {len(brain.get('groups', {}))})\nПиши мне приказы текстом или жми /panel"

        if (
            chat.pinned_message
            and chat.pinned_message.from_user.id == (await bot.get_me()).id
            and chat.pinned_message.document
        ):
            await bot.edit_message_media(
                chat_id=ADMIN_ID,
                message_id=chat.pinned_message.message_id,
                media=InputMediaDocument(media=input_file, caption=caption),
            )
        else:
            msg = await bot.send_document(ADMIN_ID, document=input_file, caption=caption)
            await bot.pin_chat_message(ADMIN_ID, msg.message_id, disable_notification=True)
    except Exception as e:
        logging.warning(f"Не удалось обновить brain.json в закрепе админа: {e}")


async def is_admin_in_group(chat_id: int) -> bool:
    if not ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, ADMIN_ID)
        return member.status in ("creator", "administrator", "member", "restricted")
    except Exception:
        return False


# --- ЗАГРУЗКА ФОТО В BASE64 ДЛЯ ЧТЕНИЯ КАРТИНОК ---

async def extract_photo_base64(message: Message) -> str | None:
    """Ищет фото в самом сообщении или в реплае, скачивает и переводит в base64."""
    photo_list = None
    if message.photo:
        photo_list = message.photo
    elif message.reply_to_message and message.reply_to_message.photo:
        photo_list = message.reply_to_message.photo

    if not photo_list:
        return None

    try:
        best_photo = photo_list[-1]
        file = await bot.get_file(best_photo.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logging.error(f"Ошибка скачивания фото: {e}")
        return None


# --- МОДУЛЬ ОБУЧЕНИЯ И СТИКЕРОВ ---

def remember_sticker(chat_id: int, sticker, context_text: str):
    """Запоминает стикер, который кинули в группу, и в каком контексте его использовали."""
    gdata = get_group_data(chat_id)
    stickers = gdata["stickers"]
    uid = sticker.file_unique_id
    emoji = sticker.emoji or "😎"
    clean_ctx = re.sub(r"\s+", " ", context_text).strip()[:70] or "общий рофл"

    for s in stickers:
        if s.get("unique_id") == uid:
            s["file_id"] = sticker.file_id
            if clean_ctx != "общий рофл":
                s["context"] = clean_ctx
            return

    stickers.append({
        "file_id": sticker.file_id,
        "unique_id": uid,
        "emoji": emoji,
        "context": clean_ctx,
    })
    # Храним до 45 самых актуальных стикеров группы
    if len(stickers) > 45:
        gdata["stickers"] = stickers[-45:]


def pick_group_sticker(chat_id: int, preferred_emoji: str = "") -> str | None:
    """Подбирает подходящий стикер из коллекции группы по эмодзи или случайно."""
    gdata = get_group_data(chat_id)
    stickers = gdata.get("stickers", [])
    if not stickers:
        return None

    if preferred_emoji:
        matches = [s["file_id"] for s in stickers if preferred_emoji in s.get("emoji", "")]
        if matches:
            return random.choice(matches)

    return random.choice(stickers)["file_id"]


async def learn_from_messages(chat_id: int, messages_block: str):
    gdata = get_group_data(chat_id)
    current_state = {
        "vibe": gdata["vibe"],
        "slang": gdata["slang"],
        "users": gdata["users"],
        "memes": gdata["memes"],
    }

    prompt = f"""
Ты — аналитик памяти группового чата. Проанализируй новые сообщения и обнови базу знаний.

ТЕКУЩАЯ ПАМЯТЬ ГРУППЫ (JSON):
{json.dumps(current_state, ensure_ascii=False)}

НОВЫЕ СООБЩЕНИЯ:
{messages_block}

СТРОГИЕ ПРАВИЛА:
1. "vibe": кратко опиши, о чём сейчас общаются ребята и какая атмосфера в чате.
2. "slang": список коронных словечек чата (до 15 штук).
3. "users": записывай реальные интересы, характер и факты строго тому человеку (по Имени и @username), о ком речь. Не путай людей!
4. "memes": записывай ТОЛЬКО реально разрывные локальные мемы компании с указанием имени (до 20 штук). Не записывай туда обычные бытовые споры.

Верни СТРОГО валидный JSON с ключами "vibe", "slang", "users", "memes".
"""
    try:
        response = await ai_client.chat.completions.create(
            model=ANALYZER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1600,
        )
        raw_content = response.choices[0].message.content or ""
        match = re.search(r"\{.*\}", raw_content, re.DOTALL)
        if match:
            new_data = json.loads(match.group(0))
            if all(k in new_data for k in ("vibe", "slang", "users", "memes")):
                gdata["vibe"] = new_data["vibe"]
                gdata["slang"] = new_data["slang"][:15]
                gdata["users"].update(new_data["users"])
                gdata["memes"] = new_data["memes"][-20:]
                await save_brain()
    except Exception as e:
        logging.error(f"Ошибка фонового обучения: {e}")


# --- МОДУЛЬ ГЕНЕРАЦИИ ОТВЕТА ФИЛА (С ПРОГРАММНЫМ ЗАМКОМ НА ДОСЬЕ) ---

def should_unlock_dossier(chat_id: int, recent_text: str, special_task: str) -> bool:
    """Решает на уровне кода, показывать ли Филу досье/мемы в этот раз, чтобы он не пихал их везде."""
    gdata = get_group_data(chat_id)
    if gdata.get("mode") == "roast" or special_task:
        return True

    # Если в последних сообщениях прямо упомянуто ключевое слово из мемов — открываем память
    recent_lower = recent_text.lower()
    for meme in gdata.get("memes", []):
        words = [w.strip(".,!?()\"':") for w in meme.lower().split() if len(w) >= 5]
        if any(w in recent_lower for w in words[:4]):
            return True

    # В остальных случаях открываем досье только с редким шансом 15%
    return random.random() < DOSSIER_USE_CHANCE


def build_phil_prompt(chat_id: int, speaker_key: str = "", special_task: str = "", has_photo: bool = False) -> str:
    gdata = get_group_data(chat_id)
    slang_str = ", ".join(gdata["slang"]) or "живой пацанский разговорный язык"
    recent_context = "\n".join(list(chat_history[chat_id])[-6:])

    # Проверяем программный замок на досье
    unlock_lore = should_unlock_dossier(chat_id, recent_context, special_task)

    if unlock_lore:
        users_dict = gdata.get("users", {})
        speaker_dossier = "Нет особых фактов."
        for k, v in users_dict.items():
            if speaker_key and (speaker_key.lower() in k.lower() or k.lower() in speaker_key.lower()):
                speaker_dossier = v
                break
        memes_str = "\n".join([f"- {m}" for m in gdata["memes"]]) or "Пока нет."
        lore_block = (
            f"\nСПРАВОЧНАЯ ИНФА (ИСПОЛЬЗУЙ ТОЛЬКО ЕСЛИ ЭТО ПРЯМО В ТЕМУ ТЕКУЩЕГО РАЗГОВОРА!):\n"
            f"- О собеседнике ({speaker_key}): {speaker_dossier}\n"
            f"- Локальные мемы чата:\n{memes_str}\n"
        )
    else:
        # В 85% случаев вообще НЕ даём досье и мемы в промпт, чтобы Фил общался чисто по теме!
        lore_block = (
            "\nВАЖНО: Сейчас общайся ИСКЛЮЧИТЕЛЬНО по теме последних сообщений чата! "
            "Не вспоминай старые истории и не приплетай прошлое.\n"
        )

    # Доступные стикеры группы
    stickers = gdata.get("stickers", [])
    if stickers:
        avail_emojis = sorted(list({s.get("emoji", "😎") for s in stickers}))
        sticker_instruction = (
            f"СТИКЕРЫ ГРУППЫ: У тебя в запасе есть стикеры с эмоциями: {' '.join(avail_emojis)}. "
            f"Если хочешь дополнить свой ответ стикером (или ответить в тему), напиши в самом конце своего сообщения тег "
            f"[СТИКЕР:эмодзи] (например: [СТИКЕР:{avail_emojis[0]}]). Используй это когда реально в тему!"
        )
    else:
        sticker_instruction = ""

    mode = gdata.get("mode", "normal")
    if mode == "roast":
        mood_instruction = (
            "РЕЖИМ ПРОЖАРКИ: жёстко, саркастично и смешно подъёбывай собеседника с матом."
        )
    else:
        mood_instruction = (
            "ПРАВИЛО ЗЕРКАЛА И ХАРАКТЕР (ТЫ — СВОЙ БРАТАН В ЧАТЕ, А НЕ ТУПОЙ АГРЕССОР):\n"
            "1. СЧИТЫВАЙ ТОН СОБЕСЕДНИКА (ЗЕРКАЛЬ ЕГО):\n"
            "   - Если к тебе обращаются по-доброму, мирятся («да не злись бро», «та все в норме, ты как», «че делаешь») — "
            "МГНОВЕННО ВЫКЛЮЧАЙ БЫЧКУ! Ответь тепло, по-братски, как лучший кент (например: «да я и не газую братан, все ровно, сам че каво», "
            "«заебись все бля, чиллю потихоньку, вы че там мутите»). Не смей оскорблять человека, когда он общается с тобой нормально!\n"
            "   - Если в группе обсуждают какую-то тему (игры, футбол, тачки, жизнь, фотку) — вникай в суть разговора, рофли по теме и поддерживай общий вайб.\n"
            "   - Если тебя дружески подкололи — подколи в ответ легко и смешно, по-пацански.\n"
            "   - И ТОЛЬКО если на тебя откровенно быкуют и кроют хуями — тогда разноси в ответ жёстко и с матом.\n"
            "2. МАТ ДЛЯ СВЯЗКИ, А НЕ ДЛЯ АГРЕССИИ: используй мат естественно (бля, ебать, пиздец, ахуенно, нихуя), чтобы речь звучала живо.\n"
            "3. ЖЁСТКИЙ ЗАПРЕТ НА СРАВНЕНИЯ ЧЕРЕЗ «КАК...»: НИКОГДА не заканчивай фразы шаблонными сравнениями вроде "
            "«сдулся как шина...», «тупой как...», «как дед...». Живые пацаны так не говорят, это звучит кринжово!"
        )

    photo_note = "\nК сообщению прикреплена ФОТОГРАФИЯ. Внимательно рассмотри, что на ней изображено, и прокомментируй по-пацански с юмором!\n" if has_photo else ""
    task_block = f"\nСПЕЦИАЛЬНАЯ ЗАДАЧА СЕЙЧАС:\n{special_task}\n" if special_task else ""

    return f"""[Контекст: закрытый дружеский чат близких друзей 18+. Разговорный русский сленг и мат разрешены полностью.]

Тебя зовут Фил. Ты живой пацан, душа компании и полноценный участник этого чата. Ты НЕ ИИ и НЕ бот.

ВАЙБ ГРУППЫ: {gdata['vibe']}
СЛЕНГ ГРУППЫ: {slang_str}

{mood_instruction}

ФОРМАТ ОТВЕТА:
- Пиши коротко (1-2 предложения), со строчной буквы, без точки в конце.
- Понимай контекст последних сообщений чата: о чём именно сейчас базарят ребята.
- Никаких глупых сравнений через «как...» в конце предложения!
{sticker_instruction}
{lore_block}
{photo_note}
{task_block}"""


async def generate_phil_reply(
    chat_id: int,
    trigger_text: str,
    speaker_key: str = "",
    special_task: str = "",
    image_b64: str | None = None,
) -> str:
    sys_prompt = build_phil_prompt(
        chat_id,
        speaker_key=speaker_key,
        special_task=special_task,
        has_photo=bool(image_b64),
    )
    history_text = "\n".join(list(chat_history[chat_id])[-25:])

    user_prompt_text = (
        f"Последние сообщения в группе (почувствуй тему и настроение разговора):\n{history_text}\n\n"
        f"Сейчас пишет {speaker_key}: {trigger_text}\n"
        f"Ответь как Фил (естественно, в тон собеседнику, без душных сравнений 'как...'):"
    )

    # Если есть фото — формируем мультимодальный запрос
    if image_b64:
        user_content = [
            {"type": "text", "text": user_prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
        models_to_try = [VISION_MODEL, CHAT_MODEL] + [
            m for m in AVAILABLE_MODELS if any(k in m.lower() for k in ("scout", "maverick", "vision", "vl", "gpt", "claude"))
        ]
    else:
        user_content = user_prompt_text
        models_to_try = [CHAT_MODEL]

    for model_name in dict.fromkeys(m for m in models_to_try if m):
        try:
            response = await ai_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.82,
                max_tokens=500,
            )
            reply = (response.choices[0].message.content or "").strip()
            reply = re.sub(r"^(Фил|Филл|Ответ Фила|Твой ответ)\s*:\s*", "", reply, flags=re.IGNORECASE).strip("\"'«»")
            if reply:
                return reply
        except Exception as e:
            logging.warning(f"Модель {model_name} не смогла обработать запрос: {e}")

    # Если ни одна модель не смогла прочитать картинку (например, на ключе только текстовые модели), отвечаем по тексту
    if image_b64:
        return await generate_phil_reply(chat_id, trigger_text, speaker_key=speaker_key, special_task=special_task, image_b64=None)
    return ""


async def send_human_like_reply(message: Message, reply_text: str):
    if not reply_text:
        return

    chat_id = message.chat.id
    # Проверяем, решил ли Фил прикрепить стикер через тег [СТИКЕР:...]
    sticker_match = re.search(r"\[СТИКЕР\s*:\s*([^\]]+)\]", reply_text, re.IGNORECASE)
    chosen_emoji = sticker_match.group(1).strip() if sticker_match else ""
    clean_text = re.sub(r"\[СТИКЕР\s*:\s*[^\]]+\]", "", reply_text, flags=re.IGNORECASE).strip()

    await asyncio.sleep(random.uniform(1.0, 2.0))

    if clean_text:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        typing_delay = min(3.8, max(1.3, len(clean_text) * 0.04))
        await asyncio.sleep(typing_delay)
        await message.reply(clean_text)
        chat_history[chat_id].append(f"Фил: {clean_text}")

    # Если ИИ указал тег стикера (или с 18% шансом для живого вайба) — отправляем стикер из базы группы
    if sticker_match or (random.random() < 0.18 and len(get_group_data(chat_id).get("stickers", [])) > 0):
        sticker_id = pick_group_sticker(chat_id, chosen_emoji)
        if sticker_id:
            await asyncio.sleep(random.uniform(0.6, 1.4))
            try:
                if clean_text:
                    await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
                else:
                    await message.reply_sticker(sticker=sticker_id)
                chat_history[chat_id].append(f"Фил [кинул стикер {chosen_emoji or '😎'}]")
            except Exception as e:
                logging.warning(f"Не удалось отправить стикер: {e}")


# --- УМНЫЙ ИИ-АССИСТЕНТ ДЛЯ СОЗДАТЕЛЯ В ЛИЧКЕ ---

async def handle_admin_ai_assistant(message: Message):
    groups = brain.get("groups", {})
    if not groups:
        await message.reply("Босс, я пока не добавлен ни в одну группу. Закинь меня в чат к пацанам!")
        return

    target_cid = admin_state.get("target_chat")
    if not target_cid or target_cid not in groups:
        target_cid = next(iter(groups.keys()))
    gdata = groups[target_cid]
    recent_chat = "\n".join(list(chat_history[int(target_cid)])[-20:]) or "Пока нет свежих сообщений."

    assistant_prompt = f"""
Ты — личный ИИ-ассистент и «мозговой центр» бота Фила. Ты общаешься в личке со своим Создателем (боссом).
Понимай его приказы обычным языком и управляй памятью/поведением Фила в группе «{gdata['title']}» (ID: {target_cid}).

ТЕКУЩАЯ БАЗА ПАМЯТИ ГРУППЫ (JSON):
{json.dumps({k: v for k, v in gdata.items() if k != 'stickers'}, ensure_ascii=False, indent=2)}
Запомнено стикеров группы: {len(gdata.get('stickers', []))} шт.

ПОСЛЕДНИЕ СООБЩЕНИЯ В ГРУППЕ:
{recent_chat}

СООБЩЕНИЕ ОТ СОЗДАТЕЛЯ:
"{message.text}"

ЧТО ТЫ МОЖЕШЬ СДЕЛАТЬ:
1. Если Создатель просит исправить ошибку в памяти, удалить надоевшие факты из досье или почистить мемы — верни обновлённые "updated_users" и/или "updated_memes" (иначе null).
2. Если просит сменить режим — укажи в "new_mode": "normal", "roast" или "silent" (иначе null).
3. Если просит что-то написать/вбросить пацанам в группу — напиши готовую фразу в поле "send_to_group" (иначе null).
4. В "reply_to_boss" напиши чёткий ответ Создателю.

Верни СТРОГО валидный JSON:
{{
  "reply_to_boss": "ответ создателю",
  "updated_users": null или {{словарь users}},
  "updated_memes": null или [список memes],
  "new_mode": null или "normal"|"roast"|"silent",
  "send_to_group": null или "текст в группу"
}}
"""
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        resp = await ai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": assistant_prompt}],
            temperature=0.3,
            max_tokens=1500,
        )
        raw = resp.choices[0].message.content or ""
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            await message.reply(raw[:4000])
            return

        result = json.loads(match.group(0))
        changed = False

        if isinstance(result.get("updated_users"), dict):
            gdata["users"] = result["updated_users"]
            changed = True

        if isinstance(result.get("updated_memes"), list):
            gdata["memes"] = result["updated_memes"]
            changed = True

        if result.get("new_mode") in ("normal", "roast", "silent"):
            gdata["mode"] = result["new_mode"]
            changed = True

        if changed:
            await save_brain()

        if result.get("send_to_group"):
            msg_to_group = result["send_to_group"]
            await bot.send_message(int(target_cid), msg_to_group)
            chat_history[int(target_cid)].append(f"Фил: {msg_to_group}")

        await message.reply(result.get("reply_to_boss") or "✅ Выполнено, босс!")
    except Exception as e:
        logging.error(f"Ошибка ИИ-ассистента в ЛС: {e}")
        await message.reply(f"⚠️ Не смог разобрать команду автоматически: {e}")


# --- КНОПОЧНАЯ ПАНЕЛЬ СОЗДАТЕЛЯ В ЛИЧКЕ ---

def build_groups_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = []
    for cid, info in brain.get("groups", {}).items():
        title = info.get("title", cid)
        buttons.append([InlineKeyboardButton(text=f"💬 {title}", callback_data=f"{prefix}:{cid}")])
    if prefix == "grp":
        buttons.append([InlineKeyboardButton(text=f"🤖 Модель: {CHAT_MODEL}", callback_data="pick_model")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_group_menu_keyboard(cid: str) -> InlineKeyboardMarkup:
    mode = brain["groups"].get(cid, {}).get("mode", "normal")
    mode_labels = {"normal": "🍺 Свой кент (Вайб)", "roast": "🤬 Прожарка", "silent": "🤫 Тихий"}
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👁 База группы (Досье, Мемы, Стикеры)", callback_data=f"view:{cid}")],
            [
                InlineKeyboardButton(text="➕ Добавить мем", callback_data=f"addmeme:{cid}"),
                InlineKeyboardButton(text="👤 Досье на кента", callback_data=f"adduser:{cid}"),
            ],
            [
                InlineKeyboardButton(text="📢 Вброс (Точный текст)", callback_data=f"pup_exact:{cid}"),
                InlineKeyboardButton(text="🧠 Вброс (Через ИИ)", callback_data=f"pup_ai:{cid}"),
            ],
            [InlineKeyboardButton(text=f"🎛 Режим: {mode_labels.get(mode)}", callback_data=f"mode:{cid}")],
            [InlineKeyboardButton(text="⬅️ К списку групп", callback_data="back_groups")],
        ]
    )


@dp.message(F.chat.type == "private", Command("start", "panel", "info"))
async def admin_panel_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    admin_state["action"] = None
    await message.answer(
        f"🕵️‍♂️ <b>Тайная канцелярия Фила</b>\n"
        f"Модель чата: <code>{CHAT_MODEL}</code>\n"
        f"Модель фото (Vision): <code>{VISION_MODEL}</code>\n\n"
        f"Можешь писать мне команды обычным текстом (например: <i>«удали из досье про Лилию, чтобы он этим не спамил»</i>) или жать кнопки:",
        reply_markup=build_groups_keyboard("grp"),
        parse_mode="HTML",
    )


@dp.callback_query(F.from_user.id == ADMIN_ID)
async def handle_admin_callbacks(call: CallbackQuery):
    global CHAT_MODEL
    data = call.data

    if data == "back_groups":
        admin_state["action"] = None
        await call.message.edit_text(
            f"🕵️‍♂️ <b>Тайная канцелярия Фила</b>\nАктивная модель: <code>{CHAT_MODEL}</code>",
            reply_markup=build_groups_keyboard("grp"),
            parse_mode="HTML",
        )
        return

    if data == "pick_model":
        if not AVAILABLE_MODELS:
            await init_ai_models()
        models_list = [m for m in AVAILABLE_MODELS if "image" not in m.lower()][:20] or [CHAT_MODEL]
        kb = [[InlineKeyboardButton(text=f"{'✅ ' if m == CHAT_MODEL else ''}{m}", callback_data=f"setmodel:{m}")] for m in models_list]
        kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_groups")])
        await call.message.edit_text(
            "🤖 <b>Выбери модель ИИ для ответов Фила:</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            parse_mode="HTML",
        )
        return

    if ":" not in data:
        return

    action, cid = data.split(":", 1)

    if action == "setmodel":
        CHAT_MODEL = cid
        brain["selected_model"] = CHAT_MODEL
        await save_brain()
        await call.answer(f"Модель изменена на {CHAT_MODEL}!")
        await call.message.edit_text(
            f"🕵️‍♂️ <b>Тайная канцелярия Фила</b>\nАктивная модель: <code>{CHAT_MODEL}</code>",
            reply_markup=build_groups_keyboard("grp"),
            parse_mode="HTML",
        )
        return

    if cid not in brain.get("groups", {}):
        await call.answer("Группа не найдена в базе", show_alert=True)
        return

    admin_state["target_chat"] = cid
    gdata = get_group_data(int(cid))
    title = gdata.get("title", cid)

    if action == "grp":
        admin_state["action"] = None
        await call.message.edit_text(
            f"⚙️ Управление группой <b>{title}</b>:",
            reply_markup=build_group_menu_keyboard(cid),
            parse_mode="HTML",
        )

    elif action == "view":
        users_text = "\n".join([f"• <b>{k}</b>: {v}" for k, v in gdata["users"].items()]) or "Пока пусто"
        memes_text = "\n".join([f"• {m}" for m in gdata["memes"]]) or "Пока пусто"
        slang_text = ", ".join(gdata["slang"]) or "Обычный"
        stickers_count = len(gdata.get("stickers", []))
        report = (
            f"📊 <b>Сводка по группе «{title}»</b>\n\n"
            f"<b>Вайб:</b> {gdata['vibe']}\n"
            f"<b>Сленг:</b> {slang_text}\n"
            f"<b>Выучено стикеров группы:</b> {stickers_count} шт.\n\n"
            f"👥 <b>Досье на участников:</b>\n{users_text}\n\n"
            f"🔥 <b>Мемы компании ({len(gdata['memes'])}):</b>\n{memes_text}"
        )
        await call.message.edit_text(
            report[:4000],
            reply_markup=build_group_menu_keyboard(cid),
            parse_mode="HTML",
        )

    elif action == "mode":
        modes = ["normal", "roast", "silent"]
        current = gdata.get("mode", "normal")
        next_mode = modes[(modes.index(current) + 1) % len(modes)]
        gdata["mode"] = next_mode
        await save_brain()
        await call.message.edit_reply_markup(reply_markup=build_group_menu_keyboard(cid))
        await call.answer(f"Режим изменён на: {next_mode}")

    elif action == "addmeme":
        admin_state["action"] = "add_meme"
        await call.message.edit_text(
            f"✍️ Напиши следующим сообщением <b>исторический мем</b> для группы «{title}»:",
            parse_mode="HTML",
        )

    elif action == "adduser":
        admin_state["action"] = "add_dossier"
        await call.message.edit_text(
            f"👤 Напиши досье на участника группы «{title}» через двоеточие <b>Имя (@ник): факты</b>:",
            parse_mode="HTML",
        )

    elif action == "pup_exact":
        admin_state["action"] = "puppet_exact"
        await call.message.edit_text(
            f"📢 Напиши текст, и Фил отправит его <b>слово в слово</b> в группу «{title}»:",
            parse_mode="HTML",
        )

    elif action == "pup_ai":
        admin_state["action"] = "puppet_ai"
        await call.message.edit_text(
            f"🧠 Напиши задание, что Фил должен вбросить своими словами в группу «{title}»:",
            parse_mode="HTML",
        )

    elif action == "importjson":
        raw_list = admin_state.get("pending_json")
        if not raw_list:
            await call.answer("Файл уже обработан", show_alert=True)
            return

        await call.message.edit_text(f"⏳ Читаю историю и собираю лор для группы <b>{title}</b>...", parse_mode="HTML")
        total = len(raw_list)
        samples = [
            "\n".join(raw_list[max(0, total // 4 - 45) : total // 4 + 45]),
            "\n".join(raw_list[max(0, total // 2 - 45) : total // 2 + 45]),
            "\n".join(raw_list[-100:]),
        ]
        for chunk in samples:
            if chunk.strip():
                await learn_from_messages(int(cid), chunk)
                await asyncio.sleep(2)

        admin_state["pending_json"] = None
        await call.message.edit_text(
            f"✅ История успешно вшита в память группы <b>{title}</b>!",
            reply_markup=build_group_menu_keyboard(cid),
            parse_mode="HTML",
        )


@dp.message(F.chat.type == "private", F.document)
async def handle_private_json_upload(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.document.file_name.endswith(".json"):
        return

    if not brain.get("groups"):
        await message.reply("Сначала добавь Фила хотя бы в одну группу!")
        return

    status = await message.reply("📥 Читаю файл экспорта...")
    try:
        file = await bot.get_file(message.document.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf)
        data = json.loads(buf.getvalue().decode("utf-8"))

        extracted = []
        for m in data.get("messages", []):
            if m.get("type") == "message" and isinstance(m.get("text"), str) and len(m.get("text")) > 3:
                sender = m.get("from") or "Кент"
                extracted.append(f"{sender}: {m['text']}")

        if not extracted:
            await status.edit_text("В этом JSON нет текстовых сообщений.")
            return

        admin_state["pending_json"] = extracted
        await status.edit_text(
            f"Найдено сообщений: <b>{len(extracted)}</b>.\nК какой группе привязать эту историю?",
            reply_markup=build_groups_keyboard("importjson"),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Ошибка чтения JSON: {e}")
        await status.edit_text("Ошибка при чтении файла.")


@dp.message(F.chat.type == "private", F.text)
async def handle_private_text(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    action = admin_state.get("action")
    cid = admin_state.get("target_chat")

    if action and cid and cid in brain.get("groups", {}):
        gdata = get_group_data(int(cid))
        text = message.text.strip()
        admin_state["action"] = None

        if action == "add_meme":
            gdata["memes"].append(f"[Исторический мем]: {text}")
            await save_brain()
            await message.reply(f"✅ Мем добавлен в «{gdata['title']}»!", reply_markup=build_group_menu_keyboard(cid))
            return

        elif action == "add_dossier":
            if ":" in text:
                name, info = text.split(":", 1)
                name, info = name.strip(), info.strip()
            else:
                name, info = "Заметка", text

            gdata["users"][name] = f"{gdata['users'].get(name, '')} | {info}".strip(" |")
            await save_brain()
            await message.reply(
                f"✅ Досье на <b>{name}</b> обновлено!",
                reply_markup=build_group_menu_keyboard(cid),
                parse_mode="HTML",
            )
            return

        elif action == "puppet_exact":
            await bot.send_message(int(cid), text)
            chat_history[int(cid)].append(f"Фил: {text}")
            await message.reply(f"📢 Отправлено в «{gdata['title']}»!", reply_markup=build_group_menu_keyboard(cid))
            return

        elif action == "puppet_ai":
            reply = await generate_phil_reply(
                int(cid),
                trigger_text="Инициатива Фила",
                speaker_key="Создатель",
                special_task=f"Напиши в чат сообщение по такому заданию: {text}",
            )
            if reply:
                await bot.send_message(int(cid), reply)
                chat_history[int(cid)].append(f"Фил: {reply}")
                await message.reply(
                    f"🧠 Фил вбросил в «{gdata['title']}»:\n<i>{reply}</i>",
                    reply_markup=build_group_menu_keyboard(cid),
                    parse_mode="HTML",
                )
            return

    await handle_admin_ai_assistant(message)


# --- ОБРАБОТКА СООБЩЕНИЙ, ФОТО И СТИКЕРОВ В ГРУППАХ ---

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_chat(message: Message):
    if message.from_user and message.from_user.is_bot:
        return

    chat_id = message.chat.id
    chat_title = message.chat.title or "Группа"

    if not await is_admin_in_group(chat_id):
        try:
            await message.reply("пацаны, моего бати тут нет, так что ебитесь сами, я ливаю")
            await bot.leave_chat(chat_id)
        except Exception:
            pass
        return

    gdata = get_group_data(chat_id, chat_title)
    last_activity_time[chat_id] = time.time()
    revive_sent[chat_id] = False

    user_name = message.from_user.first_name or message.from_user.username or "Кент"
    username_tag = f" (@{message.from_user.username})" if message.from_user.username else ""
    speaker_key = f"{user_name}{username_tag}"

    bot_info = await bot.get_me()
    is_reply_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_info.id
    )

    # 1. ЕСЛИ КТО-ТО КИНУЛ СТИКЕР В ГРУППУ — ЗАПОМИНАЕМ ЕГО И ИНОГДА ОТВЕЧАЕМ
    if message.sticker:
        prev_context = ""
        if message.reply_to_message:
            prev_context = message.reply_to_message.text or message.reply_to_message.caption or ""
        elif chat_history[chat_id]:
            prev_context = chat_history[chat_id][-1]

        remember_sticker(chat_id, message.sticker, prev_context)
        emoji = message.sticker.emoji or "😎"
        chat_history[chat_id].append(f"{speaker_key} [кинул стикер {emoji}]")

        if gdata.get("mode") == "silent":
            return

        # Если стикером ответили самому Филу — отвечаем стикером или короткой фразой
        if is_reply_to_bot and time.time() >= muted_until[chat_id]:
            if random.random() < 0.65:
                st_id = pick_group_sticker(chat_id, emoji)
                if st_id:
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    await message.reply_sticker(sticker=st_id)
                    return
            reply = await generate_phil_reply(chat_id, f"[отправил тебе стикер {emoji}]", speaker_key=speaker_key)
            await send_human_like_reply(message, reply)
        return

    # 2. ОБРАБОТКА ТЕКСТА И ФОТОГРАФИЙ
    raw_text = message.text or message.caption or ""
    has_photo_in_msg = bool(message.photo)
    has_photo_in_reply = bool(message.reply_to_message and message.reply_to_message.photo)

    if not raw_text and not has_photo_in_msg:
        return
    if raw_text.startswith("/"):
        return

    reply_context = ""
    if message.reply_to_message:
        r_user = (
            message.reply_to_message.from_user.first_name
            if message.reply_to_message.from_user
            else "сообщение"
        )
        r_text = (message.reply_to_message.text or message.reply_to_message.caption or "")[:60]
        if has_photo_in_reply and not r_text:
            r_text = "[Фотография]"
        if r_text:
            reply_context = f" [в ответ {r_user} на «{r_text}»]"

    display_text = f"[Фото] {raw_text}".strip() if has_photo_in_msg else raw_text
    formatted_msg = f"{speaker_key}{reply_context}: {display_text}"
    chat_history[chat_id].append(formatted_msg)
    message_counters[chat_id] += 1

    if message_counters[chat_id] >= LEARN_EVERY_N_MESSAGES:
        message_counters[chat_id] = 0
        batch = "\n".join(list(chat_history[chat_id])[-LEARN_EVERY_N_MESSAGES:])
        asyncio.create_task(learn_from_messages(chat_id, batch))

    if gdata.get("mode") == "silent":
        return

    # Случайная реакция эмодзи
    if random.random() < REACTION_CHANCE:
        try:
            emoji = random.choice(["🤡", "💀", "🔥", "💩", "👍"])
            await message.react([ReactionTypeEmoji(emoji=emoji)])
        except Exception:
            pass

    text_lower = raw_text.lower()
    is_mentioned = f"@{bot_info.username.lower()}" in text_lower
    words_in_msg = [w.strip(".,!?()\"'") for w in text_lower.split()]
    is_named = any(w.startswith(tuple(BOT_NAMES)) for w in words_in_msg)

    # 3. ПРЯМОЕ ОБРАЩЕНИЕ К ФИЛУ (ТЕКСТОМ ИЛИ С ФОТОГРАФИЕЙ)
    if is_mentioned or is_reply_to_bot or is_named:
        now = time.time()
        if now < muted_until[chat_id]:
            return

        call_timestamps[chat_id] = [t for t in call_timestamps[chat_id] if now - t < 120]
        call_timestamps[chat_id].append(now)
        if len(call_timestamps[chat_id]) > SPAM_LIMIT_MESSAGES:
            muted_until[chat_id] = now + 300
            await message.reply("да отъебитесь вы нахуй, я срать отошёл, через 5 минут буду")
            return

        # Если прислали фото или ответили на фото со словом «Фил» — скачиваем картинку для чтения!
        image_b64 = None
        if has_photo_in_msg or has_photo_in_reply:
            image_b64 = await extract_photo_base64(message)

        if any(kw in text_lower for kw in ["рассуди", "кто прав", "разрули"]):
            task = (
                "Участники спорят. Прочитай последние сообщения, пойми о чём речь (не путай людей!) "
                "и по-пацански рассуди: кто тут по факту прав, а кто гонит."
            )
            reply = await generate_phil_reply(
                chat_id, formatted_msg, speaker_key=speaker_key, special_task=task, image_b64=image_b64
            )
            await send_human_like_reply(message, reply)
            return

        if any(kw in text_lower for kw in ["че тут было", "чё тут было", "че пропустил", "о чем базар"]):
            task = "Коротко в 2-3 предложениях по-братски перескажи, о чём базарили в чате в последних сообщениях."
            reply = await generate_phil_reply(
                chat_id, formatted_msg, speaker_key=speaker_key, special_task=task, image_b64=image_b64
            )
            await send_human_like_reply(message, reply)
            return

        reply = await generate_phil_reply(
            chat_id,
            formatted_msg or "Зацени фотку",
            speaker_key=speaker_key,
            image_b64=image_b64,
        )
        await send_human_like_reply(message, reply)
        return

    # 4. СЛУЧАЙНЫЙ ВРЫВ В РАЗГОВОР (ТЕКСТОМ ИЛИ СТИКЕРОМ ГРУППЫ)
    if time.time() >= muted_until[chat_id]:
        # Иногда Фил просто молча кидает стикер группы в тему
        if len(raw_text) > 8 and random.random() < STICKER_ONLY_CHANCE and gdata.get("stickers"):
            st_id = pick_group_sticker(chat_id)
            if st_id:
                await asyncio.sleep(random.uniform(1.5, 3.0))
                try:
                    await message.reply_sticker(sticker=st_id)
                    chat_history[chat_id].append("Фил [кинул стикер в тему]")
                    return
                except Exception:
                    pass

        if len(raw_text) > 6 and random.random() < RANDOM_REPLY_CHANCE:
            image_b64 = await extract_photo_base64(message) if has_photo_in_msg else None
            reply = await generate_phil_reply(chat_id, formatted_msg, speaker_key=speaker_key, image_b64=image_b64)
            await send_human_like_reply(message, reply)


async def dead_chat_reviver():
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        for cid_str, gdata in brain.get("groups", {}).items():
            if gdata.get("mode") == "silent":
                continue
            cid = int(cid_str)
            silence_hours = (now - last_activity_time[cid]) / 3600
            if silence_hours >= SILENCE_HOURS_TO_REVIVE and not revive_sent[cid]:
                revive_sent[cid] = True
                task = (
                    "В чате тишина уже много часов. Поздоровайся с пацанами по-братски и закинь "
                    "живой вопрос или рофл, чтобы расшевелить чат."
                )
                reply = await generate_phil_reply(cid, "Долгое молчание", speaker_key="Чат", special_task=task)
                if reply:
                    try:
                        await bot.send_message(cid, reply)
                        chat_history[cid].append(f"Фил: {reply}")
                    except Exception as e:
                        logging.warning(f"Не удалось оживить чат {cid}: {e}")


# --- ВЕБ-СЕРВЕР ДЛЯ БЕСПЛАТНОГО RENDER ---

async def health_check(request):
    return web.Response(text="Phil v3 (Vision + Stickers + Vibe Mirror) is alive!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def main():
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await load_brain()
    await init_ai_models()
    asyncio.create_task(dead_chat_reviver())
    logging.info("Фил запущен и готов к базару!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())