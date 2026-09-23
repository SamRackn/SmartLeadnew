"""Upload a spreadsheet of cold-email leads and see, row by row, what happened.

Until now the only way into the pipeline was the CLI. That is fine for us and
useless to the client, whose leads arrive as a spreadsheet someone exported ten
minutes ago.

One path in, not two
--------------------
This route parses a file and hands the rows to `csv_import.import_rows` -- the
same function the CLI calls. It does not validate, does not normalise headers,
does not check suppression and does not decide routing. Every one of those is
somebody else's job precisely so a row that the CLI would reject cannot be
admitted by the browser. A second, looser door into the lead table is exactly
the failure this arrangement exists to prevent.

What the operator sees
----------------------
Every row's outcome, grouped: created, updated, suppressed, invalid, errored.
Suppressed rows are shown as prominently as accepted ones -- a silent import
that quietly dropped nine people is indistinguishable from one that worked, and
the client needs to know when his list overlaps his own do-not-contact record.

Nothing here sends anything. An import enqueues research, and the pipeline chain
stops at `verify` as it always does.
"""

from __future__ import annotations

import io
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from mazz.db import get_session
from mazz.ingest.csv_import import ImportReport, import_csv
from mazz.ingest.excel_import import (
    EXCEL_SUFFIXES,
    ExcelImportError,
    import_excel,
    looks_like_excel,
)
from mazz.ingest.validate import LEAD_COLUMNS
from mazz.web.auth import current_user

router = APIRouter()

#: Refuse anything larger rather than letting a stray 400MB export take the
#: process down. Roughly a quarter-million lead rows; far past any real list.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

ACCEPTED_SUFFIXES: tuple[str, ...] = (".csv", *EXCEL_SUFFIXES)


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _page(
    request: Request,
    user: str,
    *,
    report: ImportReport | None = None,
    error: str | None = None,
    filename: str | None = None,
    status_code: int = 200,
):
    return _templates(request).TemplateResponse(
        request,
        "import.html",
        {
            "user": user,
            "report": report,
            "error": error,
            "filename": filename,
            "lead_columns": LEAD_COLUMNS,
            "accepted_suffixes": ACCEPTED_SUFFIXES,
        },
        status_code=status_code,
    )


@router.get("/leads/import", response_class=HTMLResponse)
async def import_form(
    request: Request,
    user: str = Depends(current_user),
):
    """The upload form. Nothing has happened yet."""

    return _page(request, user)


@router.get("/leads/import/template.csv", response_class=PlainTextResponse)
async def import_template(user: str = Depends(current_user)):
    """A header-only CSV in the canonical column order.

    Offered because "what columns do you need?" is the first question every
    client asks, and a downloadable answer beats a paragraph of documentation.
    Header spelling is aliased anyway, so this is a convenience, not a contract.
    """

    return PlainTextResponse(
        ",".join(LEAD_COLUMNS) + "\n",
        headers={"Content-Disposition": 'attachment; filename="lead-template.csv"'},
    )


@router.post("/leads/import", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: str = Depends(current_user),
):
    """Parse the upload and ingest it. Renders the per-row report.

    A bad FILE is a message on the page, not a 500 -- the operator picked the
    wrong thing and should be told so. A bad ROW is already handled downstream
    and lands in the report's rejected list, which is why nothing here tries to
    validate rows itself.
    """

    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()

    data = await file.read()
    if not data:
        return _page(
            request, user, error=f"{filename} is empty.", filename=filename,
            status_code=400,
        )
    if len(data) > MAX_UPLOAD_BYTES:
        return _page(
            request,
            user,
            error=(
                f"{filename} is {len(data) / 1024 / 1024:.1f}MB, over the "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB limit. Split it, or "
                "import it from the command line."
            ),
            filename=filename,
            status_code=413,
        )

    # Sniff the CONTENT, not the extension. Exporters mislabel files often
    # enough that trusting the name means failing on a file the client can
    # plainly see is fine.
    is_excel = looks_like_excel(data, filename)

    if not is_excel and suffix in EXCEL_SUFFIXES:
        # Named .xlsx but is not a workbook. Usually a CSV with the wrong
        # extension, so try it as one rather than refusing outright.
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return _page(
                request,
                user,
                error=(
                    f"{filename} has a spreadsheet extension but is not a "
                    "workbook, and is not readable as text either. Re-export it "
                    "from the source tool."
                ),
                filename=filename,
                status_code=400,
            )

    if not is_excel and suffix not in ACCEPTED_SUFFIXES and suffix:
        return _page(
            request,
            user,
            error=(
                f"{filename} is a {suffix} file. Accepted: "
                + ", ".join(ACCEPTED_SUFFIXES)
                + "."
            ),
            filename=filename,
            status_code=400,
        )

    try:
        if is_excel:
            report = import_excel(session, data, filename=filename)
        else:
            report = import_csv(session, data)
    except ExcelImportError as exc:
        return _page(
            request, user, error=f"Could not read {filename}: {exc}",
            filename=filename, status_code=400,
        )
    except UnicodeDecodeError:
        return _page(
            request,
            user,
            error=(
                f"{filename} is not valid UTF-8 text. If it came out of Excel, "
                "save it as .xlsx and upload that instead of a CSV."
            ),
            filename=filename,
            status_code=400,
        )

    return _page(request, user, report=report, filename=filename)
