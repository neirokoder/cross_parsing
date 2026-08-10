"""
HTTP-клиент для Docling Serve (Docker).
Отправляет PDF в docling-serve и возвращает DoclingDocument.
Без OCR (do_ocr=false) и без OCR-парсинга формул (do_formula_enrichment=false).
"""
import logging
import time
from pathlib import Path
from typing import Optional

import httpx
import fitz

from docling_core.types.doc import DoclingDocument

from app.config import DOCLING_SERVE_URL, DOCLING_SERVE_TIMEOUT

logger = logging.getLogger(__name__)

SERVE_URL = DOCLING_SERVE_URL.rstrip("/")


def _build_multipart_body(
    params: list[tuple[str, str]],
    pdf_path: str,
) -> tuple[bytes, str]:
    """Собирает multipart/form-data тело вручную (обход бага httpx 0.28.x)."""
    boundary = "----DoclingFormBoundary7MA4YWxk"
    parts = []

    for name, value in params:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )

    file_name = Path(pdf_path).name
    with open(pdf_path, "rb") as f:
        file_bytes = f.read()
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{file_name}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def request_docling_document(
    pdf_path: str,
    max_pages: Optional[int] = None,
) -> tuple[DoclingDocument, dict]:
    """Отправляет PDF в docling-serve, возвращает (DoclingDocument, raw_response)."""
    t_start = time.time()

    url = f"{SERVE_URL}/v1/convert/file"

    params: list[tuple[str, str]] = [
        ("to_formats", "json"),
        ("do_ocr", "false"),
        ("do_table_structure", "true"),
        ("table_mode", "accurate"),
        ("table_cell_matching", "false"),
        ("do_formula_enrichment", "false"),
        ("include_images", "true"),
    ]
    # Всегда передаём page_range — docling-serve по умолчанию ограничен 50 страницами.
    try:
        pdf_doc = fitz.open(pdf_path)
        total_pages = pdf_doc.page_count
        pdf_doc.close()
    except Exception:
        total_pages = 9999
    page_end = total_pages if max_pages is None else min(max_pages, total_pages)
    params.append(("page_range", "1"))
    params.append(("page_range", str(page_end)))

    body, boundary = _build_multipart_body(params, pdf_path)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

    with httpx.Client(timeout=DOCLING_SERVE_TIMEOUT) as client:
        response = client.post(url, content=body, headers=headers)
        response.raise_for_status()
        raw = response.json()

    if raw.get("status") == "failure":
        raise RuntimeError(
            f"docling-serve failed: {raw.get('errors', 'unknown error')}"
        )

    doc_data = raw.get("document", {})
    json_content = doc_data.get("json_content")
    if not json_content:
        raise RuntimeError("docling-serve returned no json_content")

    doc = DoclingDocument.model_validate(json_content)
    logger.debug("TIMING request_docling_document: total=%.3fs", time.time() - t_start)
    return doc, raw
