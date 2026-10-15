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
# Триггеры проверки на бота/ИИ
AI_ACCUSATION_WORDS = ["ии", "нейросеть", "нейронка", "бот", "ботяра", "chatgpt", "гпт", "промпт", "искусственный"]

# Настройки активности
REACTION_CHANCE = 0.06
DOSSIER_USE_CHANCE = 0.14   # В 86% случаев досье скрыто, чтобы Фил не пихал его в каждый ответ
LEARN_EVERY_N_MESSAGES = 15
SILENCE_HOURS_TO_REVIVE = 16
SPAM_LIMIT_MESSAGES = 16    # Увеличенный лимит для быстрой переписки (16 вызовов за 2 минуты)

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

BAD_MODELS = ("allam", "whisper", "orpheus", "guard", "tts", "embedding", "safeguard", "arabic")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# --- ОПЕРАТИВНАЯ ПАМЯТЬ И СОСТОЯНИЯ ---
chat_history = defaultdict(lambda: deque(maxlen=40))
message_counters = defaultdict(int)
last_activity_time = defaultdict(lambda: time.time())
revive_sent = defaultdict(bool)

# Счётчик для гарантированного самостоятельного врыва в разговор (каждые 8-12 сообщений)
messages_since_phil_spoke = defaultdict(int)
next_auto_intervene = defaultdict(lambda: random.randint(8, 12))

# Память «горячего диалога» и защита от повтора стикеров
active_dialogue = defaultdict(lambda: {"user_id": 0, "expires": 0.0})
last_user_sticker_uid = defaultdict(str)
last_bot_sticker_uid = defaultdict(str)

call_timestamps = defaultdict(list)
muted_until = defaultdict(float)

admin_state = {
    "action": None,
    "target_chat": None,
    "pending_json": None,
}

MEMORY_FILE = "brain.json"
brain = {"groups": {}, "selected_model": None}


# --- АВТО-СКАНЕР МОДЕЛЕЙ ---

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

    preferred_vision = ["scout", "maverick", "vision", "vl", "claude", "gpt-6", "gpt-5", "gpt-4", "gemini", "gpt-oss-120b"]
    for pref in preferred_vision:
        found = next((m for m in AVAILABLE_MODELS if pref in m.lower() and "image" not in m.lower()), None)
        if found:
            VISION_MODEL = found
            break
    else:
        VISION_MODEL = CHAT_MODEL

    logging.info(f"🎯 Чат: {CHAT_MODEL} | Анализ: {ANALYZER_MODEL} | Зрение (Vision): {VISION_MODEL}")


def get_group_data(chat_id: int, title: str = "Группа") -> dict:
    cid = str(chat_id)
    if cid not in brain["groups"]:
        brain["groups"][cid] = {
            "title": title,
            "mode": "normal",
            "vibe": "Дружеский пацанский чат, общаются на равных, рофлят в тему, с матом.",
            "slang": [],
            "users": {},
            "memes": [],
            "stickers": [],
        }
    g = brain["groups"][cid]
    if title and title != "Группа":
        g["title"] = title
    g.setdefault("mode", "normal")
    g.setdefault("vibe", "Дружеский пацанский чат, общаются на равных, рофлят в тему, с матом.")
    g.setdefault("slang", [])
    g.setdefault("users", {})
    g.setdefault("memes", [])
    g.setdefault("stickers", [])
    return g


# --- ВЕЧНАЯ ПАМЯТЬ ЧЕРЕЗ ЗАКРЕПЛЁННЫЙ ФАЙЛ В ЛИЧКЕ ---

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


# --- ЧТЕНИЕ ФОТО, ВИДЕО, КРУЖОЧКОВ, ГИФОК И СТИКЕРОВ (VISION) ---

async def download_file_to_b64(file_id: str) -> str | None:
    try:
        file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, destination=buf)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logging.warning(f"Не удалось скачать медиа для Vision: {e}")
        return None


def get_visual_file_id_and_type(msg: Message) -> tuple[str | None, str]:
    """Находит file_id картинки (или превью-кадра для видео/стикера/кружочка) в сообщении."""
    if not msg:
        return None, ""
    if msg.photo:
        return msg.photo[-1].file_id, "фотография"
    if msg.sticker:
        if not msg.sticker.is_animated and not msg.sticker.is_video:
            return msg.sticker.file_id, f"стикер ({msg.sticker.emoji or ''})"
        if msg.sticker.thumbnail:
            return msg.sticker.thumbnail.file_id, f"анимированный стикер ({msg.sticker.emoji or ''})"
    if msg.video and msg.video.thumbnail:
        return msg.video.thumbnail.file_id, f"видео ({msg.video.duration} сек.)"
    if msg.video_note and msg.video_note.thumbnail:
        return msg.video_note.thumbnail.file_id, "видео-кружочек"
    if msg.animation and msg.animation.thumbnail:
        return msg.animation.thumbnail.file_id, "гифка"
    return None, ""


async def extract_visual_context(message: Message) -> tuple[str | None, str]:
    """Ищет визуальный контент в самом сообщении или в реплае и переводит в base64."""
    fid, media_type = get_visual_file_id_and_type(message)
    if not fid and message.reply_to_message:
        fid, media_type = get_visual_file_id_and_type(message.reply_to_message)
    if not fid:
        return None, ""
    b64 = await download_file_to_b64(fid)
    return b64, media_type


# --- УМНЫЙ АНАЛИЗ СТИКЕРОВ ГРУППЫ (ПОНИМАНИЕ СМЫСЛА И ЭМОЦИИ) ---

async def analyze_and_remember_sticker(chat_id: int, sticker, context_text: str):
    """Распознаёт, что нарисовано/написано на стикере и какую эмоцию он передаёт, сохраняя в базу."""
    gdata = get_group_data(chat_id)
    stickers = gdata["stickers"]
    uid = sticker.file_unique_id
    emoji = sticker.emoji or "😎"
    clean_ctx = re.sub(r"\s+", " ", context_text).strip()[:60]

    # Если стикер уже есть в базе — просто обновляем file_id
    for s in stickers:
        if s.get("unique_id") == uid:
            s["file_id"] = sticker.file_id
            return

    # Пытаемся глазами ИИ прочитать картинку нового стикера
    fid = sticker.file_id if (not sticker.is_animated and not sticker.is_video) else (
        sticker.thumbnail.file_id if sticker.thumbnail else None
    )
    visual_desc = f"эмоция {emoji}"
    if fid:
        b64 = await download_file_to_b64(fid)
        if b64:
            try:
                resp = await ai_client.chat.completions.create(
                    model=VISION_MODEL or CHAT_MODEL,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Опиши этот стикер из пацанского чата ОДНОЙ короткой фразой (до 10 слов): "
                                        "кто на нём, какая надпись (если есть) и какую ТОЧНУЮ эмоцию/реакцию он означает "
                                        "(например: 'недовольство, кринж и осуждение', 'посыл нахуй СЪЕБАЛОСЬ', 'дикий угар и смех')."
                                    ),
                                },
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            ],
                        }
                    ],
                    temperature=0.2,
                    max_tokens=80,
                )
                desc_res = (resp.choices[0].message.content or "").strip().replace("\n", " ")
                if desc_res:
                    visual_desc = desc_res[:90]
            except Exception:
                if clean_ctx:
                    visual_desc = f"эмоция {emoji} (кидают когда: {clean_ctx})"

    stickers.append({
        "file_id": sticker.file_id,
        "unique_id": uid,
        "emoji": emoji,
        "desc": visual_desc,
    })
    if len(stickers) > 35:
        gdata["stickers"] = stickers[-35:]
    await save_brain()


def get_matching_sticker_by_index(chat_id: int, idx: int) -> str | None:
    """Возвращает file_id выбранного по смыслу стикера, если он не повторяет предыдущий."""
    gdata = get_group_data(chat_id)
    stickers = gdata.get("stickers", [])
    if idx < 1 or idx > len(stickers):
        return None

    chosen = stickers[idx - 1]
    uid = chosen.get("unique_id", "")

    # Запрет попугайничать (кидать тот же стикер, что только что кинул юзер) и повторяться дважды подряд
    if uid in (last_user_sticker_uid[chat_id], last_bot_sticker_uid[chat_id]):
        return None

    last_bot_sticker_uid[chat_id] = uid
    return chosen["file_id"]


# --- ФИЛЬТР ЗАПЯТЫХ (ЖИВАЯ ПУНКТУАЦИЯ КАК С ТЕЛЕФОНА) ---

def clean_phil_punctuation(text: str) -> str:
    """Убирает лишние запятые и точки в конце, чтобы текст выглядел как живое сообщение с телефона."""
    if not text:
        return ""

    # Убираем запятые после разговорных вводных слов и междометий
    text = re.sub(
        r"\b(да|не|нет|ну|бля|блять|ебать|слышь|короче|кстати|слушай|а|и|но|так|че|чё|братан|бро|родной|кент|ладно|короч|та|ой|эх|сука|нахуй|пиздец),\s*",
        r"\1 ",
        text,
        flags=re.IGNORECASE,
    )
    # Убираем запятые перед союзами
    text = re.sub(
        r",\s+(а|но|и|или|что|чтобы|когда|если|потому что|так как|как|где|куда|кто|какой|ведь|хотя)\b",
        r" \1",
        text,
        flags=re.IGNORECASE,
    )

    # Если запятые всё ещё остались — убираем 80% из них
    parts = text.split(",")
    if len(parts) > 1:
        rebuilt = parts[0]
        for p in parts[1:]:
            if random.random() < 0.80:
                rebuilt += " " + p.lstrip()
            else:
                rebuilt += ", " + p.lstrip()
        text = rebuilt

    text = re.sub(r"\s{2,}", " ", text).strip()
    # Убираем точку в самом конце сообщения
    if text.endswith(".") and not text.endswith("..."):
        text = text[:-1].rstrip()
    return text


# --- МОДУЛЬ ФОНОВОГО ОБУЧЕНИЯ ---

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
3. "users": записывай реальные интересы, характер и факты строго тому человеку (по Имени и @username), о ком речь. Категорически запрещено путать участников!
4. "memes": записывай ТОЛЬКО реально разрывные локальные мемы компании с указанием точного имени (до 20 штук).

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


# --- ГЕНЕРАЦИЯ ОТВЕТА ФИЛА ---

def should_unlock_dossier(chat_id: int, recent_text: str, special_task: str) -> bool:
    gdata = get_group_data(chat_id)
    if gdata.get("mode") == "roast" or special_task:
        return True

    recent_lower = recent_text.lower()
    for meme in gdata.get("memes", []):
        words = [w.strip(".,!?()\"':") for w in meme.lower().split() if len(w) >= 5]
        if any(w in recent_lower for w in words[:4]):
            return True

    return random.random() < DOSSIER_USE_CHANCE


def build_phil_prompt(
    chat_id: int,
    speaker_key: str = "",
    special_task: str = "",
    media_type: str = "",
) -> str:
    gdata = get_group_data(chat_id)
    slang_str = ", ".join(gdata["slang"]) or "живой разговорный сленг с матом"
    recent_context = "\n".join(list(chat_history[chat_id])[-6:])

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
            f"\nСПРАВОЧНАЯ ИНФА (ИСПОЛЬЗУЙ ТОЛЬКО ЕСЛИ ЭТО ИДЕАЛЬНО В ТЕМУ ТЕКУЩЕГО РАЗГОВОРА!):\n"
            f"- О собеседнике ({speaker_key}): {speaker_dossier}\n"
            f"- Локальные мемы чата:\n{memes_str}\n"
        )
    else:
        lore_block = (
            "\nВАЖНО: Сейчас общайся ИСКЛЮЧИТЕЛЬНО по сути последних сообщений чата! "
            "Не приплетай старые истории.\n"
        )

    # Передаём список стикеров с их точным смыслом
    stickers = gdata.get("stickers", [])
    if stickers:
        st_lines = [
            f"#{i + 1}: {s.get('emoji', '')} — {s.get('desc', 'прикольный стикер')}"
            for i, s in enumerate(stickers)
        ]
        sticker_instruction = (
            "\nБАЗА СТИКЕРОВ ГРУППЫ:\n" + "\n".join(st_lines) + "\n"
            "ПРАВИЛО СТИКЕРОВ: Если ты написал ответ (например: 'бля заебал ебать ты выдал') и в списке выше есть стикер, "
            "который ТОЧНО совпадает по эмоции/смыслу с твоей фразой (например, стикер недовольства, кринжа или угара) — "
            "можешь добавить в самый конец ответа тег [СТИКЕР:номер] (например [СТИКЕР:2]). "
            "НО: 1) Если подходящего по смыслу стикера в списке НЕТ — НЕ пиши тег вообще! "
            "2) Не кидай стикеры в каждом сообщении, чтобы не заебывать пацанов (используй только в 25-30% самых ярких моментов).\n"
        )
    else:
        sticker_instruction = ""

    mode = gdata.get("mode", "normal")
    if mode == "roast":
        mood_instruction = "РЕЖИМ ПРОЖАРКИ: жёстко, саркастично и смешно подъёбывай собеседника с матом."
    else:
        mood_instruction = (
            "ХАРАКТЕР И ПРАВИЛО ЗЕРКАЛА:\n"
            "1. ТЫ ЖИВОЙ ПАЦАН ФИЛ: общайся естественно, на русском языке без выдуманных слов и без шизофазии.\n"
            "2. НЕ ПУТАЙ АДРЕСАТА: сейчас тебе пишет лично [" + (speaker_key or "Кент") + "]. Если он в своём сообщении упоминает "
            "кого-то третьего (например, говорит про Снапа, Деда или Бульбу), помни, что ты отвечаешь [" + (speaker_key or "Кенту") + "], "
            "а не тому, о ком он рассказывает!\n"
            "3. ЗЕРКАЛЬ НАСТРОЕНИЕ: если с тобой общаются нормально, шутят или мирятся — отвечай по-братски, поддерживай тему чата, "
            "рофли вместе со всеми и используй мат органично для связки слов (бля, ебать, нихуя, пиздец, заебись). "
            "Если на тебя быкуют или оскорбляют — тогда жёстко и смешно разноси в ответ с матом (без детских стрелок 'сам такой').\n"
            "4. ПУНКТУАЦИЯ: пиши почти БЕЗ запятых, со строчной буквы, без точки в конце, короткими живыми фразами (1-2 предложения). "
            "Никаких сравнений через 'как...' в конце фразы!"
        )

    media_note = (
        f"\nК СООБЩЕНИЮ ПРИКРЕПЛЕНО МЕДИА ({media_type}). "
        f"Ты видишь этот кадр/картинку! Обязательно отреагируй по сути того, что на ней изображено или написано.\n"
        if media_type
        else ""
    )
    task_block = f"\nСПЕЦИАЛЬНАЯ ЗАДАЧА СЕЙЧАС:\n{special_task}\n" if special_task else ""

    return f"""[Контекст: закрытый дружеский чат близких друзей 18+. Разговорный русский сленг и мат разрешены полностью.]

Тебя зовут Фил. Ты свой пацан, душа компании и полноценный участник этого чата. Ты НЕ ИИ и НЕ бот.

ВАЙБ ГРУППЫ: {gdata['vibe']}
СЛЕНГ ГРУППЫ: {slang_str}

{mood_instruction}
{sticker_instruction}
{lore_block}
{media_note}
{task_block}"""


async def generate_phil_reply(
    chat_id: int,
    trigger_text: str,
    speaker_key: str = "",
    special_task: str = "",
    image_b64: str | None = None,
    media_type: str = "",
) -> str:
    sys_prompt = build_phil_prompt(
        chat_id,
        speaker_key=speaker_key,
        special_task=special_task,
        media_type=media_type if image_b64 else "",
    )
    history_text = "\n".join(list(chat_history[chat_id])[-25:])

    user_prompt_text = (
        f"Последние сообщения в чате (пойми, о чём сейчас идёт речь):\n{history_text}\n\n"
        f"Тебе пишет {speaker_key}: {trigger_text}\n"
        f"Напиши короткий, осмысленный ответ Фила (почти без запятых, со строчной буквы, строго в тему разговора):"
    )

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
        models_to_try = [CHAT_MODEL, ANALYZER_MODEL]

    # Retry-логика: если при активной переписке API подвис, пробуем ещё раз через 1.5 сек
    for attempt in range(2):
        for model_name in dict.fromkeys(m for m in models_to_try if m):
            try:
                response = await ai_client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.68,  # Оптимальная температура: живой юмор без выдуманных слов и бреда
                    max_tokens=450,
                )
                reply = (response.choices[0].message.content or "").strip()
                reply = re.sub(r"^(Фил|Филл|Ответ Фила|Твой ответ)\s*:\s*", "", reply, flags=re.IGNORECASE).strip("\"'«»")
                if reply:
                    return reply
            except Exception as e:
                logging.warning(f"Попытка {attempt + 1} ({model_name}): {e}")
        await asyncio.sleep(1.5)

    if image_b64:
        return await generate_phil_reply(chat_id, trigger_text, speaker_key=speaker_key, special_task=special_task, image_b64=None)
    return ""


async def send_human_like_reply(message: Message, reply_text: str):
    if not reply_text:
        return

    chat_id = message.chat.id
    messages_since_phil_spoke[chat_id] = 0

    # Запоминаем собеседника для возможного продолжения «горячего диалога» (на 60 секунд)
    if message.from_user and not message.from_user.is_bot:
        active_dialogue[chat_id] = {
            "user_id": message.from_user.id,
            "expires": time.time() + 60.0,
        }

    # Ищем, выбрал ли Фил подходящий по смыслу стикер [СТИКЕР:номер]
    sticker_match = re.search(r"\[СТИКЕР\s*:\s*#?(\d+)\]", reply_text, re.IGNORECASE)
    chosen_idx = int(sticker_match.group(1)) if sticker_match else 0

    clean_text = re.sub(r"\[СТИКЕР\s*:\s*[^\]]+\]", "", reply_text, flags=re.IGNORECASE).strip()
    clean_text = clean_phil_punctuation(clean_text)

    await asyncio.sleep(random.uniform(0.9, 1.8))

    if clean_text:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        typing_delay = min(3.5, max(1.2, len(clean_text) * 0.035))
        await asyncio.sleep(typing_delay)
        await message.reply(clean_text)
        chat_history[chat_id].append(f"Фил: {clean_text}")

    # Отправляем стикер ТОЛЬКО если ИИ сам подобрал точный номер по смыслу (и не чаще 35% раз, чтобы не надоедать)
    if chosen_idx > 0 and random.random() < 0.45:
        sticker_id = get_matching_sticker_by_index(chat_id, chosen_idx)
        if sticker_id:
            await asyncio.sleep(random.uniform(0.6, 1.2))
            try:
                if clean_text:
                    await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
                else:
                    await message.reply_sticker(sticker=sticker_id)
                chat_history[chat_id].append(f"Фил [кинул стикер #{chosen_idx} в тему]")
            except Exception as e:
                logging.warning(f"Не удалось отправить стикер: {e}")


# --- ПРОВЕРКА: ОБРАЩАЕТСЯ ЛИ ЧЕЛОВЕК К ДРУГОМУ УЧАСТНИКУ ЧАТА ---

def is_talking_to_another_person(message: Message, raw_text: str, bot_id: int, chat_id: int) -> bool:
    """Проверяет, не пишет ли человек другому кенту, чтобы Фил не влезал невпопад в горячем диалоге."""
    # 1. Если нажал Reply на сообщение другого человека (не Фила)
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id != bot_id:
            return True

    text_lower = raw_text.lower()
    # 2. Если в тексте есть чужой @username
    if "@" in text_lower:
        return True

    # 3. Если в начале сообщения обращаются по имени к другому участнику из базы группы
    gdata = get_group_data(chat_id)
    for user_key in gdata.get("users", {}).keys():
        clean_name = re.sub(r"\(@.*?\)", "", user_key).strip().lower()
        if len(clean_name) >= 3 and clean_name in text_lower.split()[:3]:
            return True

    return False


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
Изучено стикеров со смыслом: {len(gdata.get('stickers', []))} шт.

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
            msg_to_group = clean_phil_punctuation(result["send_to_group"])
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
        f"Модель фото/стикеров (Vision): <code>{VISION_MODEL}</code>\n\n"
        f"Можешь писать мне любые приказы обычным текстом или управлять кнопками:",
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
        st_list = gdata.get("stickers", [])
        stickers_preview = "\n".join([f"#{i+1} {s.get('emoji','')}: {s.get('desc','')}" for i, s in enumerate(st_list[-8:])]) or "Пока нет"
        report = (
            f"📊 <b>Сводка по группе «{title}»</b>\n\n"
            f"<b>Вайб:</b> {gdata['vibe']}\n"
            f"<b>Сленг:</b> {slang_text}\n\n"
            f"👥 <b>Досье на участников:</b>\n{users_text}\n\n"
            f"🔥 <b>Мемы компании ({len(gdata['memes'])}):</b>\n{memes_text}\n\n"
            f"🎭 <b>Последние изученные стикеры ({len(st_list)} шт.):</b>\n{stickers_preview}"
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
                clean_rep = clean_phil_punctuation(reply)
                await bot.send_message(int(cid), clean_rep)
                chat_history[int(cid)].append(f"Фил: {clean_rep}")
                await message.reply(
                    f"🧠 Фил вбросил в «{gdata['title']}»:\n<i>{clean_rep}</i>",
                    reply_markup=build_group_menu_keyboard(cid),
                    parse_mode="HTML",
                )
            return

    await handle_admin_ai_assistant(message)


# --- ОБРАБОТКА ВСЕХ СОБЫТИЙ В ГРУППАХ ---

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_chat(message: Message):
    if message.from_user and message.from_user.is_bot:
        return

    chat_id = message.chat.id
    chat_title = message.chat.title or "Группа"

    if not await is_admin_in_group(chat_id):
        try:
            await message.reply("пацаны моего бати тут нет так что ебитесь сами я ливаю")
            await bot.leave_chat(chat_id)
        except Exception:
            pass
        return

    gdata = get_group_data(chat_id, chat_title)
    last_activity_time[chat_id] = time.time()
    revive_sent[chat_id] = False
    messages_since_phil_spoke[chat_id] += 1

    user_name = message.from_user.first_name or message.from_user.username or "Кент"
    username_tag = f" (@{message.from_user.username})" if message.from_user.username else ""
    speaker_key = f"{user_name}{username_tag}"

    bot_info = await bot.get_me()
    is_reply_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_info.id
    )

    # 1. ОБРАБОТКА ГОЛОСОВЫХ СООБЩЕНИЙ (ВОЙСОВ)
    if message.voice:
        dur = message.voice.duration or 0
        chat_history[chat_id].append(f"{speaker_key} [записал голосовое на {dur} сек.]")
        if gdata.get("mode") == "silent" or time.time() < muted_until[chat_id]:
            return
        if is_reply_to_bot or (dur >= 35 and random.random() < 0.30):
            task = (
                f"Собеседник записал голосовое сообщение длительностью {dur} секунд. "
                f"Подколи его по-пацански (что ты голосовухи не слушаешь, пусть буквами пишет или что он там целый подкаст записал)."
            )
            reply = await generate_phil_reply(chat_id, f"[Голосовое {dur} сек.]", speaker_key=speaker_key, special_task=task)
            await send_human_like_reply(message, reply)
        return

    # 2. ОБРАБОТКА СТИКЕРОВ (ИЗУЧЕНИЕ СМЫСЛА + ОТВЕТ ПРИ РЕПЛАЕ)
    if message.sticker:
        last_user_sticker_uid[chat_id] = message.sticker.file_unique_id
        prev_context = ""
        if message.reply_to_message:
            prev_context = message.reply_to_message.text or message.reply_to_message.caption or ""
        elif chat_history[chat_id]:
            prev_context = chat_history[chat_id][-1]

        # В фоне изучаем смысл и надпись на стикере через Vision
        asyncio.create_task(analyze_and_remember_sticker(chat_id, message.sticker, prev_context))

        emoji = message.sticker.emoji or "😎"
        chat_history[chat_id].append(f"{speaker_key} [кинул стикер {emoji}]")

        if gdata.get("mode") == "silent" or time.time() < muted_until[chat_id]:
            return

        # Если стикером ответили реплаем Филу — Фил смотрит на стикер через Vision и отвечает по сути!
        if is_reply_to_bot:
            img_b64, m_type = await extract_visual_context(message)
            reply = await generate_phil_reply(
                chat_id,
                f"[прислал тебе стикер {emoji}]",
                speaker_key=speaker_key,
                image_b64=img_b64,
                media_type=m_type,
            )
            await send_human_like_reply(message, reply)
        return

    # 3. ОБРАБОТКА ТЕКСТА, ФОТО, ВИДЕО, КРУЖОЧКОВ И ГИФОК
    raw_text = message.text or message.caption or ""
    own_fid, own_media_type = get_visual_file_id_and_type(message)

    if not raw_text and not own_fid:
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
        _, r_media_type = get_visual_file_id_and_type(message.reply_to_message)
        if r_media_type and not r_text:
            r_text = f"[{r_media_type}]"
        if r_text:
            reply_context = f" [в ответ {r_user} на «{r_text}»]"

    display_text = f"[{own_media_type}] {raw_text}".strip() if own_media_type else raw_text
    formatted_msg = f"{speaker_key}{reply_context}: {display_text}"
    chat_history[chat_id].append(formatted_msg)
    message_counters[chat_id] += 1

    if message_counters[chat_id] >= LEARN_EVERY_N_MESSAGES:
        message_counters[chat_id] = 0
        batch = "\n".join(list(chat_history[chat_id])[-LEARN_EVERY_N_MESSAGES:])
        asyncio.create_task(learn_from_messages(chat_id, batch))

    if gdata.get("mode") == "silent":
        return

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

    # Проверяем, продолжается ли «горячий диалог» с этим же человеком (и не пишет ли он кому-то другому)
    now = time.time()
    dialog_state = active_dialogue[chat_id]
    is_hot_followup = (
        dialog_state["user_id"] == message.from_user.id
        and now < dialog_state["expires"]
        and len(raw_text) > 2
        and not is_talking_to_another_person(message, raw_text, bot_info.id, chat_id)
        and random.random() < 0.60
    )

    # 4. ПРЯМОЕ ОБРАЩЕНИЕ К ФИЛУ ИЛИ ПРОДОЛЖЕНИЕ ГОРЯЧЕГО ДИАЛОГА
    if is_mentioned or is_reply_to_bot or is_named or is_hot_followup:
        if now < muted_until[chat_id]:
            return

        # На прямой реплай анти-спам мягче, чтобы не обрывать живой диалог
        call_timestamps[chat_id] = [t for t in call_timestamps[chat_id] if now - t < 120]
        call_timestamps[chat_id].append(now)
        if len(call_timestamps[chat_id]) > SPAM_LIMIT_MESSAGES:
            muted_until[chat_id] = now + 240
            await message.reply("да отъебитесь вы нахуй я посрать отошёл через пару минут буду")
            return

        image_b64, media_type = await extract_visual_context(message)

        # Спец-триггер 1: Проверка на бота / ИИ («на базе какого ИИ сделан»)
        if any(w in words_in_msg for w in AI_ACCUSATION_WORDS) or "какого ии" in text_lower:
            task = (
                "Собеседник подозревает, что ты ИИ, нейросеть или бот! Высмей это максимально жизненно и с матом: "
                "скажи что сидишь с телефона, пьешь пиво/чай или чешешь яйца, и подколи его, что ему уже везде роботы мерещатся."
            )
            reply = await generate_phil_reply(
                chat_id, formatted_msg, speaker_key=speaker_key, special_task=task, image_b64=image_b64, media_type=media_type
            )
            await send_human_like_reply(message, reply)
            return

        # Спец-триггер 2: Рассуди спор
        if any(kw in text_lower for kw in ["рассуди", "кто прав", "разрули"]):
            task = (
                "Участники спорят. Прочитай последние сообщения, пойми о чём речь (не путай людей!) "
                "и по-пацански рассуди: кто тут по факту прав, а кто гонит."
            )
            reply = await generate_phil_reply(
                chat_id, formatted_msg, speaker_key=speaker_key, special_task=task, image_b64=image_b64, media_type=media_type
            )
            await send_human_like_reply(message, reply)
            return

        # Спец-триггер 3: Саммари чата
        if any(kw in text_lower for kw in ["че тут было", "чё тут было", "че пропустил", "о чем базар"]):
            task = "Коротко в 2-3 предложениях по-братски перескажи, о чём базарили в чате в последних сообщениях."
            reply = await generate_phil_reply(
                chat_id, formatted_msg, speaker_key=speaker_key, special_task=task, image_b64=image_b64, media_type=media_type
            )
            await send_human_like_reply(message, reply)
            return

        reply = await generate_phil_reply(
            chat_id,
            formatted_msg or f"Зацени ({media_type})",
            speaker_key=speaker_key,
            image_b64=image_b64,
            media_type=media_type,
        )
        await send_human_like_reply(message, reply)
        return

    # 5. ГАРАНТИРОВАННЫЙ САМОСТОЯТЕЛЬНЫЙ ВРЫВ ФИЛА В РАЗГОВОР (КАЖДЫЕ 8-12 СООБЩЕНИЙ)
    if now >= muted_until[chat_id] and messages_since_phil_spoke[chat_id] >= next_auto_intervene[chat_id]:
        if len(raw_text) > 5 or own_fid:
            messages_since_phil_spoke[chat_id] = 0
            next_auto_intervene[chat_id] = random.randint(8, 12)
            image_b64, media_type = await extract_visual_context(message)
            task = (
                "Тебя никто не звал напрямую, но ты читаешь чат и решил сам вставить свои пять копеек "
                "строго в тему текущего разговора пацанов (коротко, с рофлом или мнением по теме)."
            )
            reply = await generate_phil_reply(
                chat_id, formatted_msg, speaker_key=speaker_key, special_task=task, image_b64=image_b64, media_type=media_type
            )
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
                    clean_rep = clean_phil_punctuation(reply)
                    try:
                        await bot.send_message(cid, clean_rep)
                        chat_history[cid].append(f"Фил: {clean_rep}")
                    except Exception as e:
                        logging.warning(f"Не удалось оживить чат {cid}: {e}")


# --- ВЕБ-СЕРВЕР ДЛЯ БЕСПЛАТНОГО RENDER ---

async def health_check(request):
    return web.Response(text="Phil v4 (Smart Stickers + No Commas + Vision) is alive!")


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
    logging.info("Фил v4 запущен и готов к базару!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())