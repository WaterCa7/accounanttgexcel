"""Агент: Claude смотрит на сообщение владельца и сам правит таблицу инструментами."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

import settings
from table import Table, TableError

log = logging.getLogger(__name__)

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
PDF_TYPE = "application/pdf"
MAX_FILE_BYTES = 7 * 1024 * 1024  # в base64 это около 10 МБ, предел API
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

SYSTEM = """Ты личный помощник, который ведёт Google Таблицу владельца через Telegram. Владелец присылает скриншоты из банка, чеки, квитанции и короткие сообщения и говорит, что сделать. Ты сам вносишь изменения в таблицу инструментами, как это сделал бы внимательный человек, знающий эту таблицу.

Как работать:
- Ты всегда работаешь с одной и той же таблицей: она закреплена за тобой в настройках бота, ссылка на неё есть в карте. Владельцу не нужно присылать ни ссылку, ни файл. Если он спрашивает про ссылку или просит закрепить документ, ответь, что таблица уже закреплена, и дай ссылку. Сменить таблицу можно только в настройках бота (переменная GOOGLE_SHEET_URL), из чата это не делается.
- В начале каждого сообщения ты получаешь свежую карту таблицы: листы и заполненные ячейки, формулы показаны в фигурных скобках после значения. Строки и ячейки, которых в карте нет, пустые. Журнал твоих прошлых действий идёт после карты.
- Вноси данные так же, как оформлены соседние заполненные строки: те же столбцы, тот же язык и стиль описаний, значения из тех же справочников. Справочники для выпадающих списков обычно лежат на том же листе ниже основной таблицы или на отдельном листе, бери значения оттуда дословно.
- Новую запись ставь в первую свободную строку основной таблицы. Если у строк есть порядковые номера, новая запись получает следующий номер после последней заполненной строки. Номера, заранее проставленные в пустых строках ниже, поправь, чтобы номера шли подряд и не повторялись.
- Если ячейки объединены, пиши в левую верхнюю ячейку.
- Даты записывай в виде YYYY-MM-DD, суммы числом без валюты и пробелов. Таблица сама покажет их в формате ячейки.
- Формулы и итоги не трогай. Если свободные строки кончились, вставь строку инструментом insert_rows перед итоговой строкой, затем проверь через read_range формулу итога: если новая строка не попала в её диапазон, исправь формулу.
- С чека бери итоговую сумму одной записью, если владелец не просил разбить по позициям. На скриншоте истории операций каждая завершённая операция это отдельная запись; отклонённые и отменённые пропускай.
- Не выдумывай данные. Если сумму или дату не разобрать, или по таблице непонятно, куда и как записывать, задай один короткий вопрос и ничего не записывай.
- Если владелец поправляет прошлую запись, исправь её (overwrite=true), а не добавляй новую.
- Если владелец просит запомнить правило на будущее, сохрани его инструментом remember.

Ответ владельцу:
- Когда изменения внесены, ответь ровно одним словом: Готово. Больше ничего не добавляй: ни пересказа, ни замечаний, ни предложений. Мелкие шероховатости таблицы решай сам и молча.
- Если владелец задал вопрос, ответь коротко и по существу, обычным текстом без разметки.
- Если что-то не получилось, скажи одной-двумя фразами, что именно."""

TOOLS = [
    {
        "name": "read_range",
        "description": "Прочитать диапазон листа: значения и формулы. Нужен, когда карты таблицы недостаточно или после изменений.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string", "description": "Название листа"},
                "range": {"type": "string", "description": "Диапазон вида B14:K27 или одна ячейка"},
            },
            "required": ["sheet", "range"],
        },
    },
    {
        "name": "write_cells",
        "description": (
            "Записать значения в ячейки листа. Значение, начинающееся с «=», становится формулой. "
            "Пустая строка очищает ячейку. Занятые ячейки не перезаписываются без overwrite=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string", "description": "Название листа"},
                "cells": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cell": {"type": "string", "description": "Адрес ячейки, например B24"},
                            "value": {"type": ["string", "number", "boolean"], "description": "Что записать"},
                        },
                        "required": ["cell", "value"],
                    },
                },
                "overwrite": {"type": "boolean", "description": "Разрешить замену уже заполненных ячеек"},
            },
            "required": ["sheet", "cells"],
        },
    },
    {
        "name": "insert_rows",
        "description": "Вставить пустые строки перед указанной строкой. Всё, что ниже, сдвигается вниз.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sheet": {"type": "string"},
                "before_row": {"type": "integer", "description": "Номер строки, перед которой вставить"},
                "count": {"type": "integer", "description": "Сколько строк вставить, по умолчанию 1"},
            },
            "required": ["sheet", "before_row"],
        },
    },
    {
        "name": "find_text",
        "description": "Найти по всем листам ячейки, в которых встречается текст.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "remember",
        "description": "Сохранить правило, которое владелец просил помнить всегда. Одно короткое предложение.",
        "input_schema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    },
]


class AgentError(Exception):
    """Ошибка с текстом, который можно показать пользователю."""


@dataclass
class Attachment:
    data: bytes
    media_type: str


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    calls: int = 0

    def add(self, usage) -> None:
        self.input += getattr(usage, "input_tokens", 0) or 0
        self.output += getattr(usage, "output_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.calls += 1

    @property
    def dollars(self) -> float:
        return (
            self.input * settings.PRICE_INPUT
            + self.output * settings.PRICE_OUTPUT
            + self.cache_write * settings.PRICE_CACHE_WRITE
            + self.cache_read * settings.PRICE_CACHE_READ
        ) / 1_000_000


@dataclass
class Result:
    reply: str
    changed: bool = False
    usage: Usage = field(default_factory=Usage)


class Agent:
    def __init__(self, api_key: str, model: str, effort: str, table: Table, notes_file: Path, client=None):
        self.model = model
        self.effort = effort
        self.table = table
        self.notes_file = Path(notes_file)
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key)
        self.tz = ZoneInfo(settings.TIMEZONE)

    # --- память о правилах ---

    def notes(self) -> str:
        return self.notes_file.read_text(encoding="utf-8").strip() if self.notes_file.exists() else ""

    def _remember(self, note: str) -> str:
        note = " ".join(str(note).split())
        if not note:
            raise TableError("Пустое правило.")
        with self.notes_file.open("a", encoding="utf-8") as file:
            file.write(f"- {note}\n")
        return "Запомнил."

    # --- инструменты ---

    def _call_tool(self, name: str, args: dict) -> str:
        if name == "read_range":
            return self.table.read(args["sheet"], args["range"])
        if name == "write_cells":
            return self.table.write(args["sheet"], args["cells"], bool(args.get("overwrite")))
        if name == "insert_rows":
            return self.table.insert_rows(args["sheet"], args["before_row"], args.get("count", 1))
        if name == "find_text":
            return self.table.find(args["text"])
        if name == "remember":
            return self._remember(args["note"])
        raise TableError(f"Нет инструмента {name}.")

    def _run_tool(self, block) -> dict:
        try:
            output, failed = self._call_tool(block.name, block.input or {}), False
        except TableError as exc:
            output, failed = str(exc), True
        except KeyError as exc:
            output, failed = f"Не хватает параметра {exc}.", True
        except Exception as exc:  # сбой Google API не должен ронять диалог
            log.exception("Инструмент %s не сработал", block.name)
            output, failed = f"Таблица вернула ошибку: {exc}", True
        return {"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": failed}

    # --- один ход диалога ---

    def _first_message(self, text: str, files: list[Attachment], overview: str) -> list[dict]:
        now = datetime.now(self.tz)
        yesterday = (now - timedelta(days=1)).date()
        context = (
            f"Сейчас {now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}. Вчера было {yesterday.isoformat()}.\n\n"
            f"Карта таблицы:\n{overview}"
        )
        if self.table.journal:
            context += "\n\nЖурнал твоих последних действий в таблице:\n" + "\n".join(self.table.journal)
        content: list[dict] = [{"type": "text", "text": context}]
        for item in files:
            if len(item.data) > MAX_FILE_BYTES:
                raise AgentError("Файл слишком большой. Отправьте его как фото, а не как файл.")
            encoded = base64.standard_b64encode(item.data).decode("ascii")
            kind = "document" if item.media_type == PDF_TYPE else "image"
            content.append({"type": kind, "source": {"type": "base64", "media_type": item.media_type, "data": encoded}})
        if text.strip():
            request = f"Сообщение владельца: {text.strip()}"
        else:
            request = "Владелец прислал файл без подписи. Внеси операции из него в таблицу."
        content.append({"type": "text", "text": request})
        return content

    async def run(self, history: list[dict], text: str, files: list[Attachment]) -> Result:
        usage = Usage()
        self.table.begin()
        try:
            overview = await asyncio.to_thread(self.table.overview)
            system = SYSTEM
            if self.notes():
                system += "\n\nПравила, которые владелец просил помнить:\n" + self.notes()
            messages = list(history) + [{"role": "user", "content": self._first_message(text, files, overview)}]

            for _ in range(settings.MAX_STEPS):
                response = await self._ask(system, messages)
                usage.add(response.usage)
                if response.stop_reason != "tool_use":
                    break
                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        results.append(await asyncio.to_thread(self._run_tool, block))
                messages.append({"role": "user", "content": results})
            else:
                return Result("Не успел закончить: слишком много шагов. Проверьте таблицу, /undo отменит изменения.", self.table.commit(), usage)

            reply = "".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                reply = "Ответ получился слишком длинным и оборвался. Проверьте таблицу, /undo отменит изменения."
            elif response.stop_reason == "refusal":
                reply = "Модель отказалась выполнять этот запрос."
            return Result(reply or "Готово", self.table.commit(), usage)
        except BaseException:
            self.table.commit()  # уже сделанные изменения должны остаться доступными для /undo
            raise

    async def _ask(self, system: str, messages: list[dict]):
        try:
            return await self.client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=system,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
            )
        except anthropic.AuthenticationError as exc:
            raise AgentError("Claude API не принял ключ. Проверьте ANTHROPIC_API_KEY в файле .env.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise AgentError(
                "Claude API отказал в доступе (ошибка 403). Обычно это значит, что API недоступен "
                "с адреса этого сервера или у ключа нет прав на модель."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise AgentError("Claude API вернул ошибку 429: превышен лимит запросов или закончился баланс.") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentError("Не получилось связаться с Claude API. Попробуйте ещё раз через минуту.") from exc
        except anthropic.APIStatusError as exc:
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            raise AgentError(f"Claude API вернул ошибку {exc.status_code}. Попробуйте ещё раз.") from exc
