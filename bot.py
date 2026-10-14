import asyncio
import io
import json
import logging
import os
import random
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

# --- НАСТРОЙКИ ОКРУЖЕНИЯ (ЗАДАЮТСЯ НА RENDER) ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Твой личный Telegram ID цифрами
PORT = int(os.getenv("PORT", "10000"))

# Имена, на которые откликается бот в чате
BOT_NAMES = ["фил", "филя", "филипп", "филчик", "филу", "филом"]

# Настройки вероятностей и таймингов
RANDOM_REPLY_CHANCE = float(os.getenv("RANDOM_REPLY_CHANCE", "0.06"))  # 6% шанс случайного ответа
REACTION_CHANCE = 0.08  # 8% шанс молча поставить реакцию-эмодзи
LEARN_EVERY_N_MESSAGES = 15  # Каждые 15 сообщений фоновое обучение
SILENCE_HOURS_TO_REVIVE = 18  # Через сколько часов тишины Фил сам вбросит тему

# Модели Groq (разделение для экономии лимитовЛёгкая модель для фонового анализа памяти
# Модели Groq
CHAT_MODEL = "llama-3.1-8b-instant"
ANALYZER_MODEL = "llama-3.1-8b-instant"

ai_client = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# --- ОПЕРАТИВНАЯ ПАМЯТЬ И СОСТОЯНИЯ ---
chat_history = defaultdict(lambda: deque(maxlen=40))
message_counters = defaultdict(int)
last_activity_time = defaultdict(lambda: time.time())
revive_sent = defaultdict(bool)

# Анти-спам (защита от задрачивания бота)
call_timestamps = defaultdict(list)
muted_until = defaultdict(float)

# Состояние диалога в админке ЛС
admin_state = {
    "action": None,       # add_meme | add_dossier | puppet_exact | puppet_ai
    "target_chat": None,
    "pending_json": None  # Временное хранение загруженного result.json
}

MEMORY_FILE = "brain.json"
brain = {"groups": {}}


def get_group_data(chat_id: int, title: str = "Группа") -> dict:
    """Возвращает или создаёт структуру памяти для конкретной группы."""
    cid = str(chat_id)
    if cid not in brain["groups"]:
        brain["groups"][cid] = {
            "title": title,
            "mode": "normal",  # normal | roast | silent
            "vibe": "Пацанский чат, общаются коротко, без официоза, с матом и подколами.",
            "slang": [],
            "users": {},
            "memes": [],
        }
    elif title and title != "Группа":
        brain["groups"][cid]["title"] = title
    return brain["groups"][cid]


# --- ВЕЧНАЯ ПАМЯТЬ ЧЕРЕЗ ЗАКРЕПЛЁННЫЙ ФАЙЛ В ТВОЕЙ ЛИЧКЕ ---

async def load_brain():
    """При запуске скачивает brain.json из закрепа в личке админа."""
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
                    logging.info("Память Фила успешно загружена из файла в закрепе ТГ!")
                    return
        except Exception as e:
            logging.warning(f"Не удалось загрузить файл памяти из закрепа: {e}")

    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                brain = json.load(f)
                logging.info("Память загружена из локального файла.")
        except Exception as e:
            logging.error(f"Ошибка чтения локального файла: {e}")


async def save_brain():
    """Сохраняет brain.json локально и обновляет файл в закрепе лички админа."""
    raw_bytes = json.dumps(brain, ensure_ascii=False, indent=2).encode("utf-8")
    with open(MEMORY_FILE, "wb") as f:
        f.write(raw_bytes)

    if not ADMIN_ID:
        return

    try:
        chat = await bot.get_chat(ADMIN_ID)
        input_file = BufferedInputFile(raw_bytes, filename="brain.json")
        caption = f"🧠 База памяти Фила (Групп: {len(brain.get('groups', {}))})\nНажми /panel для управления."

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


# --- ПРОВЕРКА «СВОЙ-ЧУЖОЙ» ---

async def is_admin_in_group(chat_id: int) -> bool:
    """Проверяет, состоит ли создатель бота в этой группе."""
    if not ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, ADMIN_ID)
        return member.status in ("creator", "administrator", "member", "restricted")
    except Exception:
        return False


# --- МОДУЛЬ САМООБУЧЕНИЯ (НА ЛЁГКОЙ МОДЕЛИ 8B) ---

async def learn_from_messages(chat_id: int, messages_block: str):
    """Фоновый аналитик: дополняет досье, сленг и мемы группы."""
    gdata = get_group_data(chat_id)
    current_state = {
        "vibe": gdata["vibe"],
        "slang": gdata["slang"],
        "users": gdata["users"],
        "memes": gdata["memes"],
    }

    prompt = f"""
Ты — скрытый модуль памяти группового чата. Проанализируй новые сообщения и обнови базу знаний.

ТЕКУЩАЯ ПАМЯТЬ ГРУППЫ (JSON):
{json.dumps(current_state, ensure_ascii=False)}

НОВЫЕ СООБЩЕНИЯ:
{messages_block}

ПРАВИЛА ОБНОВЛЕНИЯ:
1. "vibe": кратко опиши атмосферу, манеру общения и темы чата.
2. "slang": список коронных словечек и выражений чата (до 15 штук).
3. "users": дополняй досье на каждого участника (характер, работа, приколы, косяки, интересы). НЕ СТИРАЙ важные старые факты, а объединяй их с новыми!
4. "memes": список локальных мемов, смешных историй и внутренних приколов компании (храни до 25 самых ярких, никогда не удаляй исторические мемы!).

Верни СТРОГО валидный JSON с ключами "vibe", "slang", "users", "memes" без обёрток.
"""
    try:
        response = await ai_client.chat.completions.create(
            model=ANALYZER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.25,
            max_tokens=1400,
            response_format={"type": "json_object"},
        )
        new_data = json.loads(response.choices[0].message.content)
        if all(k in new_data for k in ("vibe", "slang", "users", "memes")):
            gdata["vibe"] = new_data["vibe"]
            gdata["slang"] = new_data["slang"][:15]
            gdata["users"].update(new_data["users"])
            # Сохраняем баланс мемов (до 25 штук)
            gdata["memes"] = new_data["memes"][-25:]
            await save_brain()
            logging.info(f"Память группы {gdata['title']} обновлена!")
    except Exception as e:
        logging.error(f"Ошибка фонового обучения: {e}")


# --- МОДУЛЬ ГЕНЕРАЦИИ ОТВЕТА ФИЛА (НА МОЩНОЙ МОДЕЛИ 70B) ---

def build_phil_prompt(chat_id: int, special_task: str = "") -> str:
    gdata = get_group_data(chat_id)
    users_str = "\n".join([f"- {k}: {v}" for k, v in gdata["users"].items()]) or "Пока присматриваешься к пацанам."
    memes_str = "\n".join([f"- {m}" for m in gdata["memes"]]) or "Мемы в процессе сбора."
    slang_str = ", ".join(gdata["slang"]) or "разговорный сленг с матом к месту"

    mode = gdata.get("mode", "normal")
    if mode == "roast":
        mood_instruction = (
            "СЕЙЧАС ВКЛЮЧЕН РЕЖИМ ПРОЖАРКИ: будь максимально саркастичным, токсичным и жёстко (но смешно) "
            "подъёбывай собеседника, используй его слабые места из досье и старые косяки."
        )
    else:
        mood_instruction = (
            "Твой стиль — свой прошаренный кент Фил: сухой сарказм, постирония, дружеские подъёбы, "
            "поддержка разговора по теме и детектор пиздежа (если кто-то врёт или противоречит своему досье — сразу выводи на чистую воду)."
        )

    task_block = f"\nСПЕЦИАЛЬНАЯ ЗАДАЧА СЕЙЧАС:\n{special_task}\n" if special_task else ""

    return f"""Тебя зовут Фил. Ты свой пацан и живой участник этого группового чата в Telegram.
Ты НЕ ИИ, НЕ бот и НЕ ассистент. Никогда не пиши как вежливый робот.

ВАЙБ ГРУППЫ: {gdata['vibe']}
СЛЕНГ ГРУППЫ: {slang_str}
{mood_instruction}

ЖЕЛЕЗНЫЕ ПРАВИЛА СТИЛЯ:
1. Пиши коротко: 1-2 предложения (если не просят рассудить спор или пересказать чат).
2. Пиши со строчной буквы, без точки в самом конце, как пишут люди с телефона.
3. Матерись органично и к месту (бля, пиздец, ебать, нахуй, хули и т.д.), но не перегибай как школьник.
4. Используй досье участников и исторические мемы только тогда, когда это реально в тему разговора.

ДОСЬЕ НА УЧАСТНИКОВ:
{users_str}

ИСТОРИЧЕСКИЕ МЕМЫ И ЛОР КОМПАНИИ:
{memes_str}
{task_block}"""


async def generate_phil_reply(chat_id: int, trigger_text: str, special_task: str = "") -> str:
    sys_prompt = build_phil_prompt(chat_id, special_task)
    history_text = "\n".join(chat_history[chat_id])

    try:
        response = await ai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Контекст последних сообщений чата:\n{history_text}\n\n"
                        f"Актуальная реплика или повод ({trigger_text}). Ответь как Фил:"
                    ),
                },
            ],
            temperature=0.85,
            max_tokens=220,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Ошибка генерации ответа Фила: {e}")
        return ""


async def send_human_like_reply(message: Message, reply_text: str):
    """Имитирует живого человека: пауза на чтение + статус 'печатает...'."""
    if not reply_text:
        return
    await asyncio.sleep(random.uniform(1.0, 2.2))
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    typing_delay = min(4.5, max(1.5, len(reply_text) * 0.04))
    await asyncio.sleep(typing_delay)
    await message.reply(reply_text)
    chat_history[message.chat.id].append(f"Фил: {reply_text}")


# --- СЕКРЕТНАЯ ПАНЕЛЬ СОЗДАТЕЛЯ В ЛИЧКЕ (0 ТОКЕНОВ НА ЧУЖИХ) ---

def build_groups_keyboard(prefix: str) -> InlineKeyboardMarkup:
    buttons = []
    for cid, info in brain.get("groups", {}).items():
        title = info.get("title", cid)
        buttons.append([InlineKeyboardButton(text=f"💬 {title}", callback_data=f"{prefix}:{cid}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_group_menu_keyboard(cid: str) -> InlineKeyboardMarkup:
    mode = brain["groups"].get(cid, {}).get("mode", "normal")
    mode_labels = {"normal": "🍺 Кент", "roast": "🤬 Прожарка", "silent": "🤫 Тихий"}
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👁 Что Фил думает (Досье и Мемы)", callback_data=f"view:{cid}")],
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
        return  # Чужих в личке полностью игнорируем

    admin_state["action"] = None
    if not brain.get("groups"):
        await message.answer(
            "Здарова, босс! Пока нет активных групп в базе.\n"
            "Добавь меня в пацанский чат и напиши там пару сообщений, чтобы группа появилась тут."
        )
        return

    await message.answer(
        "🕵️‍♂️ <b>Тайная канцелярия Фила</b>\nВыбери группу для управления или просто закинь сюда файл <code>result.json</code>:",
        reply_markup=build_groups_keyboard("grp"),
        parse_mode="HTML",
    )


@dp.callback_query(F.from_user.id == ADMIN_ID)
async def handle_admin_callbacks(call: CallbackQuery):
    data = call.data

    if data == "back_groups":
        admin_state["action"] = None
        await call.message.edit_text(
            "🕵️‍♂️ <b>Тайная канцелярия Фила</b>\nВыбери группу:",
            reply_markup=build_groups_keyboard("grp"),
            parse_mode="HTML",
        )
        return

    if ":" not in data:
        return

    action, cid = data.split(":", 1)
    if cid not in brain.get("groups", {}):
        await call.answer("Группа не найдена в базе", show_alert=True)
        return

    gdata = brain["groups"][cid]
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
        report = (
            f"📊 <b>Сводка по группе «{title}»</b>\n\n"
            f"<b>Вайб:</b> {gdata['vibe']}\n"
            f"<b>Сленг:</b> {slang_text}\n\n"
            f"👥 <b>Досье на участников:</b>\n{users_text}\n\n"
            f"🔥 <b>Исторические мемы ({len(gdata['memes'])}):</b>\n{memes_text}"
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
        admin_state["target_chat"] = cid
        await call.message.edit_text(
            f"✍️ Напиши мне следующим сообщением <b>исторический мем или прикол</b> для группы «{title}».\n\n"
            f"<i>Пример: В 2023 году Саня уснул в бане и проспал свой поезд</i>",
            parse_mode="HTML",
        )

    elif action == "adduser":
        admin_state["action"] = "add_dossier"
        admin_state["target_chat"] = cid
        await call.message.edit_text(
            f"👤 Напиши досье на участника группы «{title}» через двоеточие <b>Имя: факты</b>.\n\n"
            f"<i>Пример: Димка (@dimon): работает в айти, купил старую БМВ, вечно ноет про выгорание</i>",
            parse_mode="HTML",
        )

    elif action == "pup_exact":
        admin_state["action"] = "puppet_exact"
        admin_state["target_chat"] = cid
        await call.message.edit_text(
            f"📢 Напиши текст, и Фил отправит его <b>слово в слово</b> в группу «{title}» (0 токенов):",
            parse_mode="HTML",
        )

    elif action == "pup_ai":
        admin_state["action"] = "puppet_ai"
        admin_state["target_chat"] = cid
        await call.message.edit_text(
            f"🧠 Напиши тему или задание, что Фил должен вбросить своими словами в группу «{title}»:\n\n"
            f"<i>Пример: Подъеби Влада, спроси как там его криптопортфель</i>",
            parse_mode="HTML",
        )

    elif action == "importjson":
        raw_list = admin_state.get("pending_json")
        if not raw_list:
            await call.answer("Файл уже обработан или устарел", show_alert=True)
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
    """Загрузка result.json только в личке от создателя."""
    if message.from_user.id != ADMIN_ID:
        return
    if not message.document.file_name.endswith(".json"):
        return

    if not brain.get("groups"):
        await message.reply("Сначала добавь Фила хотя бы в одну группу, чтобы привязать к ней историю!")
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
            await status.edit_text("В этом JSON не нашёл текстовых сообщений. Проверь экспорт из Telegram Desktop.")
            return

        admin_state["pending_json"] = extracted
        await status.edit_text(
            f"Найдено сообщений: <b>{len(extracted)}</b>.\nК какой группе привязать эту историю?",
            reply_markup=build_groups_keyboard("importjson"),
            parse_mode="HTML",
        )
    except Exception as e:
        logging.error(f"Ошибка чтения JSON: {e}")
        await status.edit_text("Ошибка при чтении файла. Убедись, что это result.json из Telegram Desktop.")


@dp.message(F.chat.type == "private", F.text)
async def handle_private_text(message: Message):
    """Обрабатывает ввод создателя для добавления мемов, досье и кукловода. Всех остальных игнорирует."""
    if message.from_user.id != ADMIN_ID:
        return  # Полный игнор чужих в ЛС для экономии лимитов

    action = admin_state.get("action")
    cid = admin_state.get("target_chat")

    if not action or not cid or cid not in brain.get("groups", {}):
        await message.reply("Жми /panel чтобы открыть управление группами.")
        return

    gdata = brain["groups"][cid]
    text = message.text.strip()
    admin_state["action"] = None

    if action == "add_meme":
        gdata["memes"].append(f"[Исторический мем]: {text}")
        await save_brain()
        await message.reply(
            f"✅ Мем добавлен в базу «{gdata['title']}»!",
            reply_markup=build_group_menu_keyboard(cid),
        )

    elif action == "add_dossier":
        if ":" in text:
            name, info = text.split(":", 1)
            name, info = name.strip(), info.strip()
        else:
            name, info = "Заметка создателя", text

        if name in gdata["users"]:
            gdata["users"][name] += f" | {info}"
        else:
            gdata["users"][name] = info

        await save_brain()
        await message.reply(
            f"✅ Досье на <b>{name}</b> обновлено в группе «{gdata['title']}»!",
            reply_markup=build_group_menu_keyboard(cid),
            parse_mode="HTML",
        )

    elif action == "puppet_exact":
        await bot.send_message(int(cid), text)
        chat_history[int(cid)].append(f"Фил: {text}")
        await message.reply(
            f"📢 Отправлено в «{gdata['title']}»!",
            reply_markup=build_group_menu_keyboard(cid),
        )

    elif action == "puppet_ai":
        reply = await generate_phil_reply(
            int(cid),
            trigger_text="Инициатива от самого Фила",
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


# --- ОБРАБОТКА СООБЩЕНИЙ В ГРУППАХ ---

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_chat(message: Message):
    if message.from_user and message.from_user.is_bot:
        return

    chat_id = message.chat.id
    chat_title = message.chat.title or "Группа"

    # 1. Проверка «Свой-Чужой»: есть ли создатель в этой группе
    if not await is_admin_in_group(chat_id):
        try:
            await message.reply("пацаны, моего бати тут нет, так что ебитесь сами, я ливаю")
            await bot.leave_chat(chat_id)
        except Exception:
            pass
        return

    # Вытаскиваем текст (обычное сообщение или подпись к пересланному посту/картинке)
    raw_text = message.text or message.caption or ""
    if not raw_text or raw_text.startswith("/"):
        return

    gdata = get_group_data(chat_id, chat_title)
    last_activity_time[chat_id] = time.time()
    revive_sent[chat_id] = False

    user_name = message.from_user.first_name or message.from_user.username or "Кент"
    username_tag = f" (@{message.from_user.username})" if message.from_user.username else ""

    # Учитываем контекст реплая (кто кому ответил)
    reply_context = ""
    if message.reply_to_message:
        r_user = (
            message.reply_to_message.from_user.first_name
            if message.reply_to_message.from_user
            else "message"
        )
        r_text = (message.reply_to_message.text or message.reply_to_message.caption or "")[:60]
        if r_text:
            reply_context = f" [в ответ {r_user} на «{r_text}»]"

    formatted_msg = f"{user_name}{username_tag}{reply_context}: {raw_text}"
    chat_history[chat_id].append(formatted_msg)
    message_counters[chat_id] += 1

    # 2. Фоновое самообучение каждые N сообщений
    if message_counters[chat_id] >= LEARN_EVERY_N_MESSAGES:
        message_counters[chat_id] = 0
        batch = "\n".join(list(chat_history[chat_id])[-LEARN_EVERY_N_MESSAGES:])
        asyncio.create_task(learn_from_messages(chat_id, batch))

    # Если включен Тихий режим — только слушаем и копим базу
    if gdata.get("mode") == "silent":
        return

    # 3. Случайная живая реакция эмодзи без слов
    if random.random() < REACTION_CHANCE:
        try:
            emoji = random.choice(["🤡", "💀", "🔥", "💩", "👍"])
            await message.react([ReactionTypeEmoji(emoji=emoji)])
        except Exception:
            pass

    # Проверяем, обращаются ли к Филу
    bot_info = await bot.get_me()
    text_lower = raw_text.lower()
    is_mentioned = f"@{bot_info.username.lower()}" in text_lower
    is_reply_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_info.id
    )
    words_in_msg = [w.strip(".,!?()\"'") for w in text_lower.split()]
    is_named = any(name in words_in_msg for name in BOT_NAMES)

    # 4. Прямой вызов Фила
    if is_mentioned or is_reply_to_bot or is_named:
        now = time.time()
        if now < muted_until[chat_id]:
            return  # Фил в муте после спама

        # Проверка на спам (больше 4 вызовов за 120 секунд)
        call_timestamps[chat_id] = [t for t in call_timestamps[chat_id] if now - t < 120]
        call_timestamps[chat_id].append(now)
        if len(call_timestamps[chat_id]) > 4:
            muted_until[chat_id] = now + 300  # Уходит на 5 минут
            await message.reply("да отъебитесь вы нахуй, я срать отошёл, через 5 минут буду")
            return

        # Спец-триггер: Рассуди спор
        if any(kw in text_lower for kw in ["рассуди", "кто прав", "разрули"]):
            task = (
                "Участники спорят. Прочитай последние сообщения, разберись в сути срача и вынеси "
                "свой жёсткий, ироничный, но справедливый вердикт: кто тут по факту прав, а кто несёт хуйню."
            )
            reply = await generate_phil_reply(chat_id, formatted_msg, special_task=task)
            await send_human_like_reply(message, reply)
            return

        # Спец-триггер: Саммари чата («че тут было»)
        if any(kw in text_lower for kw in ["че тут было", "чё тут было", "че пропустил", "о чем базар"]):
            task = (
                "Коротко в 2-3 предложениях на пацанском языке перескажи, о чём пиздели в чате "
                "в последних сообщениях и кто больше всех выёбывался."
            )
            reply = await generate_phil_reply(chat_id, formatted_msg, special_task=task)
            await send_human_like_reply(message, reply)
            return

        # Обычный ответ на обращение
        reply = await generate_phil_reply(chat_id, formatted_msg)
        await send_human_like_reply(message, reply)
        return

    # 5. Случайный врыв Фила в разговор
    if len(raw_text) > 6 and random.random() < RANDOM_REPLY_CHANCE:
        if time.time() >= muted_until[chat_id]:
            reply = await generate_phil_reply(chat_id, formatted_msg)
            await send_human_like_reply(message, reply)


# --- ФОНОВЫЙ ОЖИВИТЕЛЬ МЁРТВОГО ЧАТА ---

async def dead_chat_reviver():
    """Проверяет раз в час: если в группе тишина больше 18 часов, Фил сам закидывает тему."""
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
                    "В чате гробовая тишина уже много часов. Вбрось короткий смешной вопрос или подкол "
                    "на основе старых мемов или досье участников, чтобы расшевелить болото."
                )
                reply = await generate_phil_reply(cid, "В чате долгое молчание", special_task=task)
                if reply:
                    try:
                        await bot.send_message(cid, reply)
                        chat_history[cid].append(f"Фил: {reply}")
                    except Exception as e:
                        logging.warning(f"Не удалось оживить чат {cid}: {e}")


# --- ВЕБ-СЕРВЕР ДЛЯ БЕСПЛАТНОГО RENDER ---

async def health_check(request):
    return web.Response(text="Phil is alive and watching!")


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
    asyncio.create_task(dead_chat_reviver())
    logging.info("Фил запущен и готов к базару!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
