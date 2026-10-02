"""Проверка бота без сети: Telegram, Claude API и Google Таблица подменены.

Запуск:  python tests/test_offline.py
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from telegram import Update
from telegram.ext import ApplicationBuilder
from telegram.request import BaseRequest

from agent import Agent
from main import Access, State, build_application
from fake_sheet import FakeBook
from table import Table, TableError, col_letter, parse_range, render_grid

OWNER = {"id": 111, "is_bot": False, "first_name": "Owner", "username": "OwnerName"}
STRANGER = {"id": 999, "is_bot": False, "first_name": "Stranger", "username": "someone"}
SHEET = "Отчёт"


def make_book() -> FakeBook:
    """Маленькая копия отчёта о расходах: шапка, строки, итог с формулой, справочник."""
    book = FakeBook()
    cells = book.add_sheet(SHEET)["cells"]
    for col, title in ((2, "Date"), (4, "Expense Type"), (7, "Amount"), (9, "Currency"), (11, "Details")):
        cells[(1, col)] = title
    cells.update({(2, 1): 1, (2, 2): "2026-09-13", (2, 4): "Meals", (2, 7): 211, (2, 9): "CNY", (2, 11): "Lunch"})
    cells.update({(5, 2): "Total:", (5, 7): "=SUM(G2:G4)", (8, 4): "Meals", (9, 4): "Taxi"})
    book.add_sheet("Пустой")
    return book


class FakeTelegram(BaseRequest):
    """Отвечает вместо серверов Telegram и запоминает, что бот отправил."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        if "/file/bot" in url:
            return 200, b"\xff\xd8fake-jpeg"
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Bot", "username": "test_bot"}
        elif name == "getFile":
            result = {"file_id": params["file_id"], "file_unique_id": "u", "file_path": "photos/1.jpg"}
        elif name == "sendMessage":
            result = {"message_id": 1000 + len(self.calls), "date": int(time.time()),
                      "chat": {"id": params["chat_id"], "type": "private"}, "text": params["text"]}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def texts(self) -> list[str]:
        return [p["text"] for n, p in self.calls if n == "sendMessage"]


class FakeClaude:
    """Клиент Claude API, который проигрывает заранее написанный сценарий ответов."""

    def __init__(self):
        self.script: list = []
        self.requests: list[dict] = []
        self.messages = self

    def tool(self, name, **args):
        block = SimpleNamespace(type="tool_use", id=f"tu{len(self.script)}", name=name, input=args)
        self.script.append(SimpleNamespace(content=[block], stop_reason="tool_use", usage=self._usage()))

    def say(self, text):
        block = SimpleNamespace(type="text", text=text)
        self.script.append(SimpleNamespace(content=[block], stop_reason="end_turn", usage=self._usage()))

    @staticmethod
    def _usage():
        return SimpleNamespace(input_tokens=10, output_tokens=100, cache_creation_input_tokens=4000, cache_read_input_tokens=4000)

    async def create(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=lambda o: vars(o))))
        return self.script.pop(0)


def message(update_id, user, **content):
    return {"update_id": update_id, "message": {
        "message_id": update_id, "date": int(time.time()), "chat": {"id": user["id"], "type": "private"}, "from": user, **content,
    }}


def command(update_id, user, text):
    return message(update_id, user, text=text, entities=[{"type": "bot_command", "offset": 0, "length": len(text)}])


def check_table():
    assert col_letter(1) == "A" and col_letter(27) == "AA" and col_letter(703) == "AAA"
    assert parse_range("K27:B14") == (14, 2, 27, 11) and parse_range("g5") == (5, 7, 5, 7)
    assert render_grid([["a", "", "3.00"]], [["a", "", "=SUM(A1)"]], 5, 2) == ["5: B=a | D=3.00 {=SUM(A1)}"]

    book = make_book()
    table = Table(book)
    overview = table.overview()
    assert "5: B=Total: | G=211.00 {=SUM(G2:G4)}" in overview and "Лист «Пустой»" in overview and "пустой" in overview

    table.begin()
    table.write(SHEET, [{"cell": "B3", "value": "2026-09-14"}, {"cell": "G3", "value": 100.5}])
    assert "G=311.50" in table.read(SHEET, "A5:K5")
    try:
        table.write(SHEET, [{"cell": "G5", "value": 0}])
    except TableError as exc:
        assert "=SUM(G2:G4)" in str(exc), "при отказе модель должна видеть, что лежит в ячейке"
    else:
        raise AssertionError("формулу нельзя затирать без overwrite")
    assert book.cell(SHEET, "G5") == "=SUM(G2:G4)"
    table.write(SHEET, [{"cell": "G3", "value": 50}], overwrite=True)
    table.insert_rows(SHEET, 5, 2)
    assert book.cell(SHEET, "B7") == "Total:" and book.cell(SHEET, "B5") == ""
    assert table.commit()

    assert "G3: 50" in table.find("50") or "Отчёт!G3: 50" in table.find("50")
    try:
        table.read("Нет такого", "A1")
    except TableError as exc:
        assert "«Отчёт»" in str(exc)
    else:
        raise AssertionError

    assert table.undo()
    assert book.cell(SHEET, "B5") == "Total:" and book.cell(SHEET, "G3") == "" and book.cell(SHEET, "B3") == ""
    assert book.cell(SHEET, "G2") == 211 and not table.undo()


async def check_bot():
    workdir = Path(tempfile.mkdtemp())
    book, claude, telegram = make_book(), FakeClaude(), FakeTelegram()
    agent = Agent("test", "claude-opus-5-5", "medium", Table(book), workdir / "notes.md", client=claude)
    state = State(access=Access.parse("@ownername"), agent=agent, usage_file=workdir / "usage.json")
    builder = ApplicationBuilder().request(telegram).get_updates_request(telegram)
    app = build_application("123:ABC", state, builder=builder)
    await app.initialize()

    async def feed(data):
        await app.process_update(Update.de_json(data, app.bot))

    photo = [{"file_id": "f1", "file_unique_id": "u1", "width": 800, "height": 1200}]

    # 1. Чужой человек: сообщения игнорируются, /start называет его ID, Claude не вызывается.
    await feed(message(1, STRANGER, photo=photo))
    await feed(message(2, STRANGER, text="добавь 100"))
    await feed(command(3, STRANGER, "/undo"))
    await feed(command(4, STRANGER, "/start"))
    assert not claude.requests and telegram.texts() == [telegram.texts()[0]] and "999" in telegram.texts()[0]

    # 2. Владелец: фото с подписью. Модель пишет в таблицу и отвечает «Готово».
    claude.tool("write_cells", sheet=SHEET, cells=[
        {"cell": "A3", "value": 2}, {"cell": "B3", "value": "2026-09-14"}, {"cell": "D3", "value": "Meals"},
        {"cell": "G3", "value": 386}, {"cell": "I3", "value": "CNY"}, {"cell": "K3", "value": "Dinner"},
    ])
    claude.say("Готово")
    await feed(message(5, OWNER, photo=photo, caption="ужин с делегацией"))
    assert telegram.texts()[-1] == "Готово"
    assert book.cell(SHEET, "G3") == 386 and str(book.cell(SHEET, "B3")) == "2026-09-14"
    first, second = claude.requests
    assert first["model"] == "claude-opus-5-5" and first["output_config"] == {"effort": "medium"}
    content = first["messages"][-1]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text"]
    assert "{=SUM(G2:G4)}" in content[0]["text"] and "ужин с делегацией" in content[2]["text"]
    assert second["messages"][-1]["content"][0]["type"] == "tool_result"
    assert second["messages"][-1]["content"][0]["is_error"] is False

    # 3. Продолжение разговора: модель видит прошлый обмен и журнал своих действий.
    claude.tool("write_cells", sheet=SHEET, cells=[{"cell": "G3", "value": 368}], overwrite=True)
    claude.say("Готово")
    await feed(message(6, OWNER, text="сумма 368"))
    request = claude.requests[2]
    assert [m["role"] for m in request["messages"]] == ["user", "assistant", "user"]
    assert request["messages"][1]["content"] == "Готово" and "G3=386" in request["messages"][2]["content"][0]["text"]
    assert book.cell(SHEET, "G3") == 368

    # 4. Модель пытается затереть формулу: таблица отказывает, модель получает ошибку.
    claude.tool("write_cells", sheet=SHEET, cells=[{"cell": "G5", "value": 999}])
    claude.say("Не стал менять итог: там формула.")
    await feed(message(7, OWNER, text="поставь итог 999"))
    refusal = claude.requests[-1]["messages"][-1]["content"][0]
    assert refusal["is_error"] is True and "уже заняты" in refusal["content"]
    assert book.cell(SHEET, "G5") == "=SUM(G2:G4)" and telegram.texts()[-1].startswith("Не стал")

    # 5. /undo отменяет последнее изменение (сумму 368), следующий /undo отменяет запись целиком.
    await feed(command(8, OWNER, "/undo"))
    assert telegram.texts()[-1] == "Отменил." and book.cell(SHEET, "G3") == 386
    await feed(command(9, OWNER, "/undo"))
    assert book.cell(SHEET, "G3") == "" and book.cell(SHEET, "A3") == ""
    await feed(command(10, OWNER, "/undo"))
    assert telegram.texts()[-1] == "Отменять нечего."

    # 6. Альбом из двух фото уходит модели одним сообщением.
    claude.say("Готово")
    before = len(claude.requests)
    await feed(message(11, OWNER, photo=photo, media_group_id="g1", caption="два чека"))
    await feed(message(12, OWNER, photo=photo, media_group_id="g1"))
    await asyncio.gather(*state.background)
    assert len(claude.requests) == before + 1
    kinds = [b["type"] for b in claude.requests[-1]["messages"][-1]["content"]]
    assert kinds == ["text", "image", "image", "text"]

    # 7. Неподходящий файл не уходит в Claude, PDF уходит документом.
    before = len(claude.requests)
    await feed(message(13, OWNER, document={"file_id": "d1", "file_unique_id": "du", "mime_type": "image/heic"}))
    assert len(claude.requests) == before and "не читаю" in telegram.texts()[-1]
    claude.say("Готово")
    await feed(message(14, OWNER, document={"file_id": "d2", "file_unique_id": "du2", "mime_type": "application/pdf"}))
    assert claude.requests[-1]["messages"][-1]["content"][1]["type"] == "document"

    # 8. Правило на будущее попадает в постоянные инструкции.
    claude.tool("remember", note="Обеды записывать как Meals")
    claude.say("Готово")
    await feed(message(15, OWNER, text="запомни: обеды записывать как Meals"))
    claude.say("Да")
    await feed(message(16, OWNER, text="помнишь?"))
    assert "Обеды записывать как Meals" in claude.requests[-1]["system"]

    # 9. /cost считает расход, /new очищает память разговора.
    await feed(command(17, OWNER, "/cost"))
    assert "сообщений 7" in telegram.texts()[-1], telegram.texts()[-1]
    await feed(command(18, OWNER, "/new"))
    claude.say("Готово")
    await feed(message(19, OWNER, text="привет"))
    assert len(claude.requests[-1]["messages"]) == 1 and not claude.script

    await app.shutdown()


if __name__ == "__main__":
    check_table()
    asyncio.run(check_bot())
    print("OK: все проверки пройдены")
