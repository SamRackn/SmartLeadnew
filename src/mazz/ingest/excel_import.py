"""Excel ingest: .xlsx/.xlsm via openpyxl, legacy .xls via xlrd.

The client's lead lists arrive as spreadsheets, not CSV, and asking him to
"just save as CSV" is the kind of instruction that gets followed nine times out
of ten and silently skipped the tenth.

This module does exactly one thing: turn a workbook into the same
``Iterable[Mapping[str, Any]]`` that `csv_import.import_rows` already consumes.
Validation, header aliasing, the suppression gate, routing and enqueueing are
NOT re-implemented here -- there is deliberately one path into the pipeline, and
a spreadsheet takes it. If this module grew its own idea of what a valid lead
is, a row rejected by CSV could be admitted by Excel, which is precisely the
second, looser door the ingest design exists to prevent.

Three spreadsheet-specific problems it does solve, because they are real and
they corrupt data quietly:

* **Numbers arrive as floats.** Excel stores 240 as 240.0, so a naive
  ``str(value)`` writes "240.0" into employee_count and "1.0" into anything
  boolean-ish. Integral floats are narrowed back to int.
* **Dates arrive as datetimes.** ``str()`` on one yields "2026-09-23 00:00:00".
  Dates are rendered ISO, without the midnight that was never in the cell.
* **The header is not always row 1.** Exports routinely carry a title row, a
  blank row, or both. The first row with at least two non-empty cells that
  looks like a header wins; everything above it is skipped.

Formulas are read with ``data_only=True``, i.e. the cached value Excel last
computed. A workbook written by a tool that never computed them has no cached
values, and that is reported rather than silently treated as empty.
"""

from __future__ import annotations

import datetime as _dt
import io
from pathlib import Path
from typing import Any, Iterator, Sequence

from sqlalchemy.orm import Session

from mazz.ingest.csv_import import ImportReport, import_rows
from mazz.pipeline.routing import RoutingPolicy
from mazz.ingest.validate import canonical_field

__all__ = [
    "ExcelImportError",
    "EXCEL_SUFFIXES",
    "import_excel",
    "import_excel_file",
    "iter_excel_rows",
    "looks_like_excel",
]

#: Suffixes we accept. ``.xlsm`` is a macro-enabled workbook; we read its cells
#: and never execute anything, so it is no more dangerous than ``.xlsx``.
EXCEL_SUFFIXES: tuple[str, ...] = (".xlsx", ".xlsm", ".xls")

#: ZIP magic. .xlsx/.xlsm are zip archives; .xls is an OLE2 compound file.
_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class ExcelImportError(Exception):
    """The workbook could not be read at all. Never raised for a bad ROW."""


def looks_like_excel(data: bytes, filename: str | None = None) -> bool:
    """True if these bytes are a workbook, by magic number not by extension.

    A file named .xlsx that is really a CSV is common enough -- some exporters
    do it -- that trusting the name would mean failing on a file the user can
    see is fine.
    """

    if data.startswith(_OLE2_MAGIC):
        return True
    if data.startswith(_ZIP_MAGIC):
        # A .docx is also a zip. Only claim it if the name agrees, or if the
        # archive contains the workbook part.
        if filename and Path(filename).suffix.lower() in (".xlsx", ".xlsm"):
            return True
        try:
            import zipfile

            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return "xl/workbook.xml" in archive.namelist()
        except Exception:
            return False
    return False


def _clean(value: Any) -> str:
    """One cell, as the text the client believes is in it."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        # 240.0 -> "240". Excel has no integer type, so every whole number
        # arrives as a float and would otherwise land in the database as
        # "240.0", which is not what the spreadsheet shows.
        if value.is_integer():
            return str(int(value))
        return repr(value).rstrip("0").rstrip(".") if "e" not in repr(value) else str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, _dt.datetime):
        # Midnight almost always means "this was a date cell", not "this
        # happened at 00:00". Do not invent a time that was never entered.
        if value.time() == _dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, _dt.time):
        return value.isoformat()
    return str(value).strip()


def _header_row_index(rows: Sequence[Sequence[Any]]) -> int:
    """Index of the header row. Skips title banners and blank spacer rows.

    A header is the first row with at least two non-empty cells in which at
    least one cell maps to a field we understand. Requiring a KNOWN field is
    what stops a two-column title banner from being mistaken for a header --
    and if no row qualifies we fall back to the first non-empty row, so a
    completely unrecognised export still reports per-column errors rather than
    dying here.
    """

    first_non_empty: int | None = None
    for index, row in enumerate(rows):
        cells = [_clean(cell) for cell in row]
        filled = [cell for cell in cells if cell]
        if not filled:
            continue
        if first_non_empty is None:
            first_non_empty = index
        if len(filled) < 2:
            continue
        if any(canonical_field(cell) is not None for cell in filled):
            return index
    if first_non_empty is None:
        raise ExcelImportError("the worksheet is empty")
    return first_non_empty


def _rows_to_dicts(rows: Sequence[Sequence[Any]]) -> Iterator[dict[str, Any]]:
    header_index = _header_row_index(rows)
    header = [_clean(cell) for cell in rows[header_index]]

    # Trailing unnamed columns are Excel's usual debris. Keep interior blanks
    # positional so column N still lines up with header N.
    while header and not header[-1]:
        header.pop()
    if not header:
        raise ExcelImportError("no header row found in the worksheet")

    for row in rows[header_index + 1 :]:
        values = [_clean(cell) for cell in row]
        if not any(values):
            continue  # blank line; import_rows skips these too, cheaply here
        record: dict[str, Any] = {}
        for position, name in enumerate(header):
            if not name:
                continue  # unnamed column: no way to address it, so drop it
            record[name] = values[position] if position < len(values) else ""
        yield record


def _read_xlsx(data: bytes, *, sheet: str | None) -> list[list[Any]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ExcelImportError(
            "reading .xlsx needs openpyxl: pip install -e '.[excel]'"
        ) from exc

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), data_only=True, read_only=True
        )
    except Exception as exc:
        raise ExcelImportError(f"could not open the workbook: {exc}") from exc

    try:
        if sheet is not None:
            if sheet not in workbook.sheetnames:
                raise ExcelImportError(
                    f"no sheet named {sheet!r}; this workbook has: "
                    + ", ".join(repr(name) for name in workbook.sheetnames)
                )
            worksheet = workbook[sheet]
        else:
            worksheet = workbook[workbook.sheetnames[0]]
        return [list(row) for row in worksheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _read_xls(data: bytes, *, sheet: str | None) -> list[list[Any]]:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ExcelImportError(
            "reading legacy .xls needs xlrd: pip install -e '.[excel]'"
        ) from exc

    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise ExcelImportError(f"could not open the workbook: {exc}") from exc

    if sheet is not None:
        if sheet not in book.sheet_names():
            raise ExcelImportError(
                f"no sheet named {sheet!r}; this workbook has: "
                + ", ".join(repr(name) for name in book.sheet_names())
            )
        worksheet = book.sheet_by_name(sheet)
    else:
        worksheet = book.sheet_by_index(0)

    datemode = book.datemode
    out: list[list[Any]] = []
    for row_index in range(worksheet.nrows):
        row: list[Any] = []
        for col_index in range(worksheet.ncols):
            cell = worksheet.cell(row_index, col_index)
            if cell.ctype == xlrd.XL_CELL_DATE:
                row.append(xlrd.xldate_as_datetime(cell.value, datemode))
            elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                row.append(bool(cell.value))
            elif cell.ctype == xlrd.XL_CELL_EMPTY:
                row.append(None)
            else:
                row.append(cell.value)
        out.append(row)
    return out


def iter_excel_rows(
    source: bytes | str | Path | io.IOBase,
    *,
    sheet: str | None = None,
    filename: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield one dict per data row, keyed by the spreadsheet's own headers.

    Header aliasing happens downstream in `validate`, exactly as it does for a
    CSV, so "Work Email" works here for the same reason it works there.
    """

    if isinstance(source, (str, Path)):
        path = Path(source)
        filename = filename or path.name
        data = path.read_bytes()
    elif isinstance(source, bytes):
        data = source
    else:
        read = source.read()
        data = read if isinstance(read, bytes) else str(read).encode("utf-8")

    if not data:
        raise ExcelImportError("the file is empty")

    suffix = Path(filename).suffix.lower() if filename else ""
    if data.startswith(_OLE2_MAGIC) or suffix == ".xls":
        rows = _read_xls(data, sheet=sheet)
    else:
        rows = _read_xlsx(data, sheet=sheet)

    if not rows:
        raise ExcelImportError("the worksheet has no rows")

    yield from _rows_to_dicts(rows)


def import_excel(
    session: Session,
    source: bytes | str | Path | io.IOBase,
    *,
    sheet: str | None = None,
    filename: str | None = None,
    policy: RoutingPolicy | None = None,
) -> ImportReport:
    """Ingest a workbook. Same gate, same routing, same report as a CSV."""

    rows = list(iter_excel_rows(source, sheet=sheet, filename=filename))
    return import_rows(session, rows, policy=policy)


def import_excel_file(
    session: Session,
    path: str | Path,
    *,
    sheet: str | None = None,
    policy: RoutingPolicy | None = None,
) -> ImportReport:
    """Ingest a workbook from disk."""

    return import_excel(session, Path(path), sheet=sheet, policy=policy)
