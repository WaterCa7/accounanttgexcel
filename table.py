"""Работа с Google Таблицей: карта листов, чтение, запись, вставка строк, отмена."""

from __future__ import annotations

import re
from datetime import datetime

import gspread
import requests
from google.auth.exceptions import GoogleAuthError, RefreshError

import settings

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$")


class TableError(Exception):
    """Ошибка с текстом, который можно показать модели или пользователю."""


def col_letter(number: int) -> str:
    letters = ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def col_number(letters: str) -> int:
    number = 0
    for char in letters.upper():
        number = number * 26 + ord(char) - 64
    return number


def parse_cell(cell: str) -> tuple[int, int]:
    match = CELL_RE.match(str(cell).strip())
    if not match:
        raise TableError(f"Не понимаю адрес ячейки «{cell}». Нужен вид B24.")
    return int(match.group(2)), col_number(match.group(1))


def parse_range(rng: str) -> tuple[int, int, int, int]:
    """«B14:K27» или «B14» -> (строка1, столбец1, строка2, столбец2)."""
    parts = str(rng).strip().split(":")
    if len(parts) > 2:
        raise TableError(f"Не понимаю диапазон «{rng}». Нужен вид B14:K27.")
    row1, col1 = parse_cell(parts[0])
    row2, col2 = parse_cell(parts[-1])
    return min(row1, row2), min(col1, col2), max(row1, row2), max(col1, col2)


def quote(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _short(value) -> str:
    text = str(value).replace("\n", " ⏎ ")
    limit = settings.MAP_MAX_CELL_CHARS
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_grid(shown: list[list], formulas: list[list], first_row: int = 1, first_col: int = 1) -> list[str]:
    """Строки вида «14: A=1 | B=13-Sep-26 | G=211.00». Пустые ячейки и строки пропускаются."""
    lines = []
    for offset in range(max(len(shown), len(formulas))):
        shown_row = shown[offset] if offset < len(shown) else []
        formula_row = formulas[offset] if offset < len(formulas) else []
        cells = []
        for index in range(max(len(shown_row), len(formula_row))):
            value = shown_row[index] if index < len(shown_row) else ""
            formula = formula_row[index] if index < len(formula_row) else ""
            is_formula = isinstance(formula, str) and formula.startswith("=")
            if value == "" and not is_formula:
                continue
            text = _short(value)
            if is_formula:
                text += " {" + _short(formula) + "}"
            cells.append(f"{col_letter(first_col + index)}={text}")
        if cells:
            lines.append(f"{first_row + offset}: " + " | ".join(cells))
    return lines


class Table:
    def __init__(self, book):
        self.book = book
        self.undo_stack: list[list[tuple]] = []
        self.journal: list[str] = []  # что бот менял, человеческим языком
        self._changes: list[tuple] = []

    @classmethod
    def connect(cls, credentials, link_or_id: str) -> "Table":
        """credentials: путь к файлу ключа Google или уже прочитанный ключ (словарь)."""
        try:
            if isinstance(credentials, dict):
                client = gspread.service_account_from_dict(credentials, scopes=SCOPES)
            else:
                client = gspread.service_account(filename=credentials, scopes=SCOPES)
            book = client.open_by_url(link_or_id) if "/" in link_or_id else client.open_by_key(link_or_id)
            book.fetch_sheet_metadata({"fields": "properties.title"})
        except FileNotFoundError as exc:
            raise TableError(
                f"Не найден файл ключа Google: {credentials}. Положите его рядом с main.py "
                "или вставьте его содержимое в переменную GOOGLE_CREDENTIALS_JSON."
            ) from exc
        except (ValueError, KeyError) as exc:
            raise TableError("Ключ Google повреждён или неполон. Скачайте файл ключа заново.") from exc
        except gspread.exceptions.NoValidUrlKeyFound as exc:
            raise TableError("В GOOGLE_SHEET_URL должна быть ссылка на Google Таблицу или её ID.") from exc
        except (GoogleAuthError, requests.exceptions.RequestException) as exc:
            if isinstance(exc, RefreshError):
                raise TableError("Google не принял ключ служебного аккаунта. Создайте новый ключ и замените его.") from exc
            raise TableError("Не получилось связаться с Google. Проверьте, что с хостинга доступны адреса googleapis.com.") from exc
        except (gspread.exceptions.APIError, gspread.exceptions.SpreadsheetNotFound, PermissionError) as exc:
            raise TableError(
                "Нет доступа к таблице. Откройте её в браузере, нажмите «Настройки доступа» и дайте права "
                "редактора адресу из строки client_email в файле ключа Google."
            ) from exc
        return cls(book)

    # --- чтение ---

    def _sheets(self) -> list[dict]:
        meta = self.book.fetch_sheet_metadata(
            {"fields": "properties(title,locale,timeZone),sheets.properties(sheetId,title,hidden,gridProperties)"}
        )
        self._title = meta.get("properties", {}).get("title", "")
        self._locale = meta.get("properties", {}).get("locale", "")
        return [s["properties"] for s in meta.get("sheets", [])]

    def _sheet(self, title: str) -> dict:
        sheets = self._sheets()
        for props in sheets:
            if props["title"] == title:
                return props
        names = ", ".join(f"«{p['title']}»" for p in sheets)
        raise TableError(f"Листа «{title}» нет. Есть листы: {names}.")

    def _get(self, ranges: list[str], render: str) -> list[list[list]]:
        response = self.book.values_batch_get(ranges, params={"valueRenderOption": render})
        return [item.get("values", []) for item in response.get("valueRanges", [])]

    def overview(self) -> str:
        """Карта всей таблицы для модели."""
        sheets = [p for p in self._sheets() if not p.get("hidden")]
        ranges = [quote(p["title"]) for p in sheets]
        shown = self._get(ranges, "FORMATTED_VALUE")
        formulas = self._get(ranges, "FORMULA")
        parts = [f"Таблица «{self._title}», региональные настройки: {self._locale or 'не указаны'}."]
        url = getattr(self.book, "url", "")
        if url:
            parts.append(f"Ссылка на таблицу: {url}")
        for props, shown_grid, formula_grid in zip(sheets, shown, formulas):
            lines = render_grid(shown_grid, formula_grid)
            grid = props.get("gridProperties", {})
            header = f"\nЛист «{props['title']}» ({grid.get('rowCount', '?')} строк, {grid.get('columnCount', '?')} столбцов):"
            if not lines:
                parts.append(header + " пустой.")
                continue
            if len(lines) > settings.MAP_MAX_ROWS:
                skipped = len(lines) - settings.MAP_HEAD_ROWS - settings.MAP_TAIL_ROWS
                lines = (
                    lines[: settings.MAP_HEAD_ROWS]
                    + [f"… пропущено заполненных строк: {skipped}, их можно прочитать через read_range …"]
                    + lines[-settings.MAP_TAIL_ROWS :]
                )
            parts.append(header + "\n" + "\n".join(lines))
        return "\n".join(parts)

    def read(self, sheet: str, rng: str) -> str:
        self._sheet(sheet)
        row1, col1, row2, col2 = parse_range(rng)
        if (row2 - row1 + 1) * (col2 - col1 + 1) > 3000:
            raise TableError("Слишком большой диапазон, читай не больше 3000 ячеек за раз.")
        address = f"{quote(sheet)}!{col_letter(col1)}{row1}:{col_letter(col2)}{row2}"
        shown = self._get([address], "FORMATTED_VALUE")[0]
        formulas = self._get([address], "FORMULA")[0]
        lines = render_grid(shown, formulas, row1, col1)
        return "\n".join(lines) if lines else "В этом диапазоне пусто."

    def find(self, text: str) -> str:
        needle = str(text).casefold().strip()
        if not needle:
            raise TableError("Пустой запрос.")
        sheets = [p for p in self._sheets() if not p.get("hidden")]
        grids = self._get([quote(p["title"]) for p in sheets], "FORMATTED_VALUE")
        hits = []
        for props, grid in zip(sheets, grids):
            for row_index, row in enumerate(grid, start=1):
                for col_index, value in enumerate(row, start=1):
                    if needle in str(value).casefold():
                        hits.append(f"{props['title']}!{col_letter(col_index)}{row_index}: {_short(value)}")
        if not hits:
            return "Ничего не найдено."
        more = f"\n… и ещё {len(hits) - 30}" if len(hits) > 30 else ""
        return "\n".join(hits[:30]) + more

    # --- запись ---

    def write(self, sheet: str, cells: list[dict], overwrite: bool = False) -> str:
        self._sheet(sheet)
        if not cells:
            raise TableError("Список ячеек пуст.")
        targets = []
        for item in cells:
            row, col = parse_cell(item.get("cell", ""))
            value = item.get("value")
            targets.append((f"{col_letter(col)}{row}", "" if value is None else value))
        addresses = [f"{quote(sheet)}!{cell}" for cell, _ in targets]

        current = self._get(addresses, "FORMULA")
        old = [(grid[0][0] if grid and grid[0] else "") for grid in current]
        occupied = [(cell, was) for (cell, _), was in zip(targets, old) if was != ""]
        if occupied and not overwrite:
            listing = "; ".join(f"{cell} содержит «{_short(was)}»" for cell, was in occupied)
            raise TableError(
                f"Ничего не записано: ячейки уже заняты ({listing}). Если их действительно нужно заменить, "
                "повтори вызов с overwrite=true."
            )

        self.book.values_batch_update({
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": address, "values": [[value]]} for address, (_, value) in zip(addresses, targets)],
        })
        self._changes.append(("write", sheet, [(cell, was) for (cell, _), was in zip(targets, old)]))
        summary = ", ".join(f"{cell}={_short(value)}" for cell, value in targets)
        self.journal.append(f"{datetime.now():%d.%m %H:%M} лист «{sheet}»: {summary}")
        replaced = "".join(f" В {cell} раньше было «{_short(was)}»." for cell, was in occupied)
        return f"Записано: {summary}.{replaced}"

    def insert_rows(self, sheet: str, before_row: int, count: int = 1) -> str:
        props = self._sheet(sheet)
        before_row, count = int(before_row), int(count)
        if before_row < 1 or not 1 <= count <= 50:
            raise TableError("before_row должен быть от 1, count от 1 до 50.")
        start, end = before_row - 1, before_row - 1 + count
        self.book.batch_update({"requests": [{"insertDimension": {
            "range": {"sheetId": props["sheetId"], "dimension": "ROWS", "startIndex": start, "endIndex": end},
            "inheritFromBefore": before_row > 1,
        }}]})
        self._changes.append(("insert", props["sheetId"], start, end))
        self.journal.append(f"{datetime.now():%d.%m %H:%M} лист «{sheet}»: вставлено строк {count} перед строкой {before_row}")
        return (
            f"Вставлено пустых строк: {count}, теперь это строки {before_row}–{before_row + count - 1}. "
            "Всё, что было ниже, сдвинулось вниз. Оформление взято у строки выше, объединения ячеек не копируются."
        )

    # --- отмена ---

    def begin(self) -> None:
        self._changes = []

    def commit(self) -> bool:
        """Закрыть набор изменений одного сообщения. True, если что-то менялось."""
        changed = bool(self._changes)
        if changed:
            self.undo_stack.append(self._changes)
            del self.undo_stack[:-20]
        self._changes = []
        del self.journal[:-15]
        return changed

    def undo(self) -> bool:
        """Откатить изменения последнего сообщения."""
        if not self.undo_stack:
            return False
        for change in reversed(self.undo_stack.pop()):
            if change[0] == "write":
                _, sheet, cells = change
                self.book.values_batch_update({
                    "valueInputOption": "USER_ENTERED",
                    "data": [{"range": f"{quote(sheet)}!{cell}", "values": [[was]]} for cell, was in cells],
                })
            else:
                _, sheet_id, start, end = change
                self.book.batch_update({"requests": [{"deleteDimension": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": start, "endIndex": end},
                }}]})
        self.journal.append(f"{datetime.now():%d.%m %H:%M} отменены изменения последнего сообщения")
        return True
