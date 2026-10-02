"""Telegram-бот: вы присылаете скриншот, чек или текст и говорите, что сделать, бот сам правит таблицу."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import settings
from agent import IMAGE_TYPES, PDF_TYPE, Agent, AgentError, Attachment, Usage
from table import Table, TableError

log = logging.getLogger("expense-bot")
HERE = Path(__file__).parent

HELP = (
    "Пришлите скриншот из банка, фото чека или PDF и напишите, что сделать. Можно и просто текстом: "
    "«добавь такси 850 вчера», «исправь сумму в последней записи на 368», «сколько сейчас итого?».\n\n"
    "/table присылает ссылку на таблицу и закрепляет её вверху чата\n"
    "/undo отменяет изменения последнего сообщения\n"
    "/new начинает разговор заново\n"
    "/cost показывает расход на Claude API за месяц"
)


@dataclass
class Access:
    ids: set[int] = field(default_factory=set)
    usernames: set[str] = field(default_factory=set)

    @classmethod
    def parse(cls, raw: str) -> "Access":
        access = cls()
        for item in raw.replace(";", ",").split(","):
            item = item.strip()
            if not item:
                continue
            if item.lstrip("-").isdigit():
                access.ids.add(int(item))
            else:
                access.usernames.add(item.lstrip("@").lower())
        return access

    def allows(self, user) -> bool:
        if user is None:
            return False
        return user.id in self.ids or (user.username or "").lower() in self.usernames


class AllowedFilter(filters.MessageFilter):
    """Пропускает сообщения только от владельца бота."""

    def __init__(self, access: Access):
        super().__init__()
        self.access = access

    def filter(self, message) -> bool:
        return self.access.allows(message.from_user)


@dataclass
class State:
    """Всё, что бот помнит между сообщениями."""

    access: Access
    agent: Agent
    usage_file: Path
    history: dict[int, list[dict]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    albums: dict[str, dict] = field(default_factory=dict)
    background: set = field(default_factory=set)


def state_of(context: ContextTypes.DEFAULT_TYPE) -> State:
    return context.bot_data["state"]


# --- учёт расхода ---


def record_usage(path: Path, usage: Usage) -> None:
    month = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    entry = data.setdefault(month, {"messages": 0, "requests": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "dollars": 0.0})
    entry["messages"] += 1
    entry["requests"] += usage.calls
    entry["input"] += usage.input
    entry["output"] += usage.output
    entry["cache_write"] += usage.cache_write
    entry["cache_read"] += usage.cache_read
    entry["dollars"] = round(entry["dollars"] + usage.dollars, 4)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --- основной путь ---


async def keep_typing(bot, chat_id: int) -> None:
    while True:
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        await asyncio.sleep(4)


async def handle(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, files: list[Attachment]) -> None:
    """Передать сообщение агенту и отправить его ответ."""
    state = state_of(context)
    async with state.lock:  # сообщения обрабатываются строго по одному, чтобы записи не перемешались
        typing = asyncio.create_task(keep_typing(context.bot, chat_id))
        try:
            history = state.history.setdefault(chat_id, [])
            result = await state.agent.run(history, text, files)
        except (AgentError, TableError) as exc:
            await context.bot.send_message(chat_id, f"⚠️ {exc}")
            return
        except Exception:
            log.exception("Не удалось обработать сообщение")
            await context.bot.send_message(chat_id, "⚠️ Что-то сломалось. Подробности в журнале бота, /undo отменит изменения.")
            return
        finally:
            typing.cancel()

        said = text.strip() or "(файл без подписи)"
        if files and text.strip():
            said += f" (приложено файлов: {len(files)})"
        history += [{"role": "user", "content": said}, {"role": "assistant", "content": result.reply}]
        del history[: -settings.HISTORY_MESSAGES]
        try:
            record_usage(state.usage_file, result.usage)
        except OSError:
            log.exception("Не удалось записать расход")
        log.info(
            "Ответ за %d запросов: вход %d, выход %d, кэш запись %d, кэш чтение %d, $%.4f",
            result.usage.calls, result.usage.input, result.usage.output,
            result.usage.cache_write, result.usage.cache_read, result.usage.dollars,
        )
        await context.bot.send_message(chat_id, result.reply[:4000])
        if result.pin_link:
            await pin_table_link(context, chat_id)


async def pin_table_link(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Отправить сообщение со ссылкой на таблицу и закрепить его вверху чата."""
    url = state_of(context).agent.table.url
    if not url:
        await context.bot.send_message(chat_id, "Не знаю ссылку на таблицу.")
        return
    sent = await context.bot.send_message(chat_id, f"📌 Таблица: {url}", link_preview_options=LinkPreviewOptions(is_disabled=True))
    try:
        await context.bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
    except Exception:
        log.exception("Не удалось закрепить сообщение")
        await context.bot.send_message(chat_id, "Ссылку отправил, но закрепить не получилось. Закрепите сообщение вручную.")


async def flush_album(context: ContextTypes.DEFAULT_TYPE, group_id: str) -> None:
    """Дождаться всех фото альбома и обработать их одним сообщением."""
    state = state_of(context)
    entry = state.albums[group_id]
    while time.monotonic() - entry["last"] < 1.5:
        await asyncio.sleep(0.3)
    del state.albums[group_id]
    await handle(context, entry["chat"], entry["text"], entry["files"])


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    state = state_of(context)
    if message.photo:
        source, media_type = message.photo[-1], "image/jpeg"
    else:
        source, media_type = message.document, message.document.mime_type or ""
        if media_type not in IMAGE_TYPES and media_type != PDF_TYPE:
            await message.reply_text("Этот формат я не читаю. Подойдут фото, PNG, JPEG, WebP и PDF.")
            return

    group_id = message.media_group_id
    entry = None
    if group_id:
        entry = state.albums.get(group_id)
        if entry is None:
            entry = state.albums[group_id] = {"chat": message.chat_id, "text": "", "files": [], "last": time.monotonic()}
            task = asyncio.create_task(flush_album(context, group_id))
            state.background.add(task)
            task.add_done_callback(state.background.discard)

    try:
        file = await source.get_file()
        attachment = Attachment(bytes(await file.download_as_bytearray()), media_type)
    except Exception:
        log.exception("Не удалось скачать файл")
        await message.reply_text("⚠️ Не получилось скачать файл из Telegram. Пришлите ещё раз.")
        return

    if entry is not None:
        entry["files"].append(attachment)
        entry["text"] = entry["text"] or (message.caption or "")
        entry["last"] = time.monotonic()
    else:
        await handle(context, message.chat_id, message.caption or "", [attachment])


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle(context, update.message.chat_id, update.message.text, [])


# --- команды ---


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if state_of(context).access.allows(user):
        await update.message.reply_text(HELP)
        if update.message.text.startswith("/start"):
            await pin_table_link(context, update.message.chat_id)
    else:
        await update.message.reply_text(
            f"Это личный бот, доступ закрыт.\nВаш Telegram ID: {user.id}\n"
            "Если бот ваш, впишите этот ID в ALLOWED_USERS в файле .env и перезапустите бота."
        )


async def table_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await pin_table_link(context, update.message.chat_id)


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = state_of(context)
    async with state.lock:
        try:
            undone = await asyncio.to_thread(state.agent.table.undo)
        except Exception:
            log.exception("Не удалось отменить")
            await update.message.reply_text("⚠️ Не получилось отменить, таблица вернула ошибку.")
            return
    await update.message.reply_text("Отменил." if undone else "Отменять нечего.")


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state_of(context).history.pop(update.message.chat_id, None)
    await update.message.reply_text("Начали заново.")


async def cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    month = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m")
    try:
        entry = json.loads(state_of(context).usage_file.read_text(encoding="utf-8")).get(month)
    except (OSError, ValueError):
        entry = None
    if not entry:
        await update.message.reply_text("В этом месяце расхода ещё не было.")
        return
    average = entry["dollars"] / entry["messages"]
    await update.message.reply_text(
        f"За {month}: сообщений {entry['messages']}, примерно ${entry['dollars']:.2f}, "
        f"в среднем ${average:.3f} за сообщение.\nТочная сумма видна в Claude Console."
    )


# --- сборка и запуск ---


def build_application(token: str, state: State, builder=None) -> Application:
    app = (builder or ApplicationBuilder()).token(token).build()
    app.bot_data["state"] = state
    allowed = AllowedFilter(state.access)
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("table", table_link, filters=allowed))
    app.add_handler(CommandHandler("undo", undo, filters=allowed))
    app.add_handler(CommandHandler("new", new, filters=allowed))
    app.add_handler(CommandHandler("cost", cost, filters=allowed))
    app.add_handler(MessageHandler(allowed & (filters.PHOTO | filters.Document.ALL), on_file))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    return app


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Не задана переменная {name}. Добавьте её в переменные окружения на хостинге или в файл .env.")
    return value


def google_credentials():
    """Ключ Google: текст JSON из переменной окружения или файл рядом с main.py.

    В переменной ключ может лежать как есть или в base64: так он проходит через любые
    поля ввода и загрузчики .env без искажений.
    """
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip().strip("'").strip()
    if not raw:
        return os.environ.get("GOOGLE_CREDENTIALS_FILE", "google-credentials.json")
    try:
        if not raw.startswith("{"):
            raw = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        return json.loads(raw, strict=False)  # strict=False: переносы строк внутри ключа допустимы
    except (ValueError, UnicodeDecodeError):
        sys.exit("В GOOGLE_CREDENTIALS_JSON должен быть целиком текст файла ключа Google (или он же в base64).")


def main() -> None:
    load_dotenv(HERE / ".env")  # переменные, заданные в панели хостинга, важнее файла
    os.chdir(HERE)  # чтобы файл ключа Google находился при любом способе запуска
    data = HERE / "data"  # на Bothost это /app/data, папка переживает обновления кода
    data.mkdir(exist_ok=True)
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе токен бота попадает в журнал

    token = os.environ.get("BOT_TOKEN", "").strip() or require("TELEGRAM_BOT_TOKEN")
    api_key = require("ANTHROPIC_API_KEY")
    access = Access.parse(os.environ.get("ALLOWED_USERS", ""))
    if not access.ids and not access.usernames:
        log.warning("ALLOWED_USERS пуст: бот никому не отвечает, только показывает ID по команде /start.")

    try:
        table = Table.connect(google_credentials(), require("GOOGLE_SHEET_URL"))
    except TableError as exc:
        sys.exit(str(exc))

    agent = Agent(
        api_key,
        os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        os.environ.get("CLAUDE_EFFORT", "medium"),
        table,
        data / "notes.md",
    )
    app = build_application(token, State(access=access, agent=agent, usage_file=data / "usage.json"))
    log.info("Бот запущен, модель %s, усилие %s.", agent.model, agent.effort)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
