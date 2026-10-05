"""Loads college data supplied as spreadsheets / PDFs into retrievable text.

Two sources, both optional, both feeding the same retriever as the markdown
files (so typed chat, English voice and Tamil voice all see the same data):

1. Files dropped in knowledge_base/data/ — .csv, .xlsx, .pdf.
2. A Google Sheet (KNOWLEDGE_SHEET_ID), read with the same service account
   that leads.py uses. Every tab is loaded, except tabs whose name starts with
   "_" (use that for admissions-only scratch/notes). Edit the sheet, then hit
   POST /admin/reload-knowledge — no redeploy needed.

Tables are turned into one self-contained line per row ("Sheet: col: value; ..."),
so a retrieved chunk is understandable without its header row. Rows are grouped
into chunks of roughly CHUNK_TOKENS.

Everything loaded here can be read aloud to anonymous website visitors. Only put
visitor-safe data in these sources (fee tables, seat matrix, cutoffs, schedules)
— never per-student or per-applicant records.
"""
import csv
import logging
import os
from pathlib import Path

logger = logging.getLogger("rag_backend.data_loader")

DATA_DIR = Path(__file__).parent / "knowledge_base" / "data"
ROWS_PER_CHUNK_MAX = 25


def _rows_to_chunks(title: str, header: list[str], rows: list[list[str]]) -> list[str]:
    header = [h.strip() for h in header]
    lines = []
    for row in rows:
        cells = [
            f"{h}: {str(v).strip()}"
            for h, v in zip(header, row)
            if h and v is not None and str(v).strip()
        ]
        if cells:
            lines.append("; ".join(cells))
    chunks = []
    for i in range(0, len(lines), ROWS_PER_CHUNK_MAX):
        body = "\n".join(lines[i : i + ROWS_PER_CHUNK_MAX])
        chunks.append(f"Data table — {title}\n{body}")
    return chunks


def _table_from_grid(title: str, grid: list[list]) -> list[str]:
    grid = [r for r in grid if any(str(c).strip() for c in r if c is not None)]
    if len(grid) < 2:
        return []
    return _rows_to_chunks(title, [str(c or "") for c in grid[0]], grid[1:])


def _load_csv(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return _table_from_grid(path.stem, list(csv.reader(f)))


def _load_xlsx(path: Path) -> list[str]:
    import openpyxl

    chunks = []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        if ws.title.startswith("_"):
            continue
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        chunks.extend(_table_from_grid(f"{path.stem} / {ws.title}", grid))
    wb.close()
    return chunks


def _load_pdf(path: Path) -> list[str]:
    """Text PDFs only. A scanned/image PDF yields no text — logged, not fatal."""
    import pdfplumber

    pages = []
    with pdfplumber.open(path) as pdf:
        for n, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(f"Document — {path.stem}, page {n}\n{text}")
    if not pages:
        logger.warning("No extractable text in %s (scanned PDF? needs OCR)", path.name)
    return pages


_LOADERS = {".csv": _load_csv, ".xlsx": _load_xlsx, ".pdf": _load_pdf}


def load_data_files() -> list[str]:
    chunks: list[str] = []
    if not DATA_DIR.exists():
        return chunks
    for path in sorted(DATA_DIR.iterdir()):
        loader = _LOADERS.get(path.suffix.lower())
        if loader is None or path.name.startswith(("~", ".", "_")):
            continue
        try:
            got = loader(path)
            logger.info("Loaded %d chunk(s) from %s", len(got), path.name)
            chunks.extend(got)
        except Exception:
            logger.exception("Failed to load data file %s — skipping it", path.name)
    return chunks


def load_google_sheet() -> list[str]:
    sheet_id = os.environ.get("KNOWLEDGE_SHEET_ID")
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sheet_id or not creds_path:
        return []
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        book = gspread.authorize(creds).open_by_key(sheet_id)
        chunks: list[str] = []
        for ws in book.worksheets():
            if ws.title.startswith("_"):
                continue
            chunks.extend(_table_from_grid(f"{book.title} / {ws.title}", ws.get_all_values()))
        logger.info("Loaded %d chunk(s) from Google Sheet %r", len(chunks), book.title)
        return chunks
    except Exception:
        logger.exception(
            "Failed to read knowledge Google Sheet %s — check that it is shared "
            "with the service account's email", sheet_id
        )
        return []


def load_all_data() -> list[str]:
    """Blocking (file + network I/O) — call via asyncio.to_thread."""
    return load_data_files() + load_google_sheet()
