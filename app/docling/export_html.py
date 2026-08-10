"""
Генерация постраничного HTML из PDF через Docling Serve (Docker).

Этот шаг выполняется ОДИН РАЗ на документ: тяжёлый парсинг Docling'ом
происходит в докере, а результат (HTML постранично + metadata) сохраняется
в data/html/. Дальнейшая доработка алгоритма кросс-парсинга работает
с сохранённым HTML и PDF локально, без повторного вызова Docling.

Настройки Docling: без OCR (do_ocr=false) и без OCR-парсинга формул
(do_formula_enrichment=false) — формулы остаются как "Formula not decoded"
(bbox сохраняются в metadata для восстановления текста из PDF локально).
"""
import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from docling_core.types.doc.base import ImageRefMode

from app.config import DOCLING_SERVE_URL, HTML_DIR
from app.docling.serve_client import request_docling_document

logger = logging.getLogger(__name__)


def _collect_not_decoded_formula_bboxes(doc) -> Dict[int, List[List[float]]]:
    """
    Собирает bbox формул, которые Docling не смог декодировать
    (FormulaItem с пустым text). Они экспортируются в HTML как
    'Formula not decoded' — по bbox текст можно восстановить из PDF (PyMuPDF).
    """
    result: Dict[int, List[List[float]]] = {}
    for item, _level in doc.iterate_items():
        label = str(getattr(item, 'label', ''))
        text = getattr(item, 'text', '') or ''
        if text or 'formula' not in label.lower():
            continue
        prov = getattr(item, 'prov', None)
        if isinstance(prov, list):
            prov = prov[0] if prov else None
        if prov is None:
            continue
        pno = prov.page_no
        bbox = [round(prov.bbox.l, 1), round(prov.bbox.t, 1),
                round(prov.bbox.r, 1), round(prov.bbox.b, 1)]
        result.setdefault(pno, []).append(bbox)
    return result


def _save_serve_media(raw: dict, images_dir: Path) -> int:
    """Сохраняет изображения из media-ответа docling-serve (если есть)."""
    saved = 0
    try:
        media = raw.get("document", {}).get("media") or []
        for i, item in enumerate(media):
            mimetype = item.get("mimetype", "image/png")
            b64 = item.get("bytes") or item.get("base64")
            if not b64:
                continue
            ext = mimetype.split("/")[-1].replace("jpeg", "jpg")
            data = base64.b64decode(b64)
            (images_dir / f"media_{i}.{ext}").write_bytes(data)
            saved += 1
    except Exception as e:
        logger.warning("Failed to save serve media images: %s", e)
    return saved


def generate_html_from_pdf(
    pdf_path: str,
    out_dir: Optional[Path] = None,
    max_pages: Optional[int] = None,
) -> Dict:
    """
    PDF → docling-serve → DoclingDocument → постраничный HTML + metadata.

    Сохраняет в out_dir (по умолчанию data/html/{stem}/):
      - docling_document.json  (полный DoclingDocument, для отладки/перегенерации)
      - page_0001.html ...     (HTML каждой страницы)
      - images/                (картинки из media, если пришли)
      - metadata.json          (source, страницы, bbox не-декодированных формул)

    Args:
        pdf_path: путь к PDF
        out_dir: каталог сохранения
        max_pages: ограничение числа страниц (None = все)

    Returns:
        metadata.json (словарь)
    """
    t0 = time.time()
    pdf_path = str(Path(pdf_path))
    file_name = Path(pdf_path).name
    file_bytes = Path(pdf_path).read_bytes()
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    doc, raw = request_docling_document(pdf_path, max_pages=max_pages)

    out_dir = out_dir or (HTML_DIR / Path(pdf_path).stem)
    out_dir = Path(out_dir)
    images_dir = out_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    # 1. Сохраняем DoclingDocument (JSON) — для отладки и перегенерации HTML
    doc_json = doc.model_dump()
    (out_dir / "docling_document.json").write_text(
        json.dumps(doc_json, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    # 2. Картинки из media-ответа (если есть)
    saved_media = _save_serve_media(raw, images_dir)

    # 3. bbox не-декодированных формул
    formula_bboxes = _collect_not_decoded_formula_bboxes(doc)

    # 4. Постраничный HTML
    page_htmls: List[Tuple[int, str]] = []
    for pno in sorted(doc.pages.keys()):
        html_text = doc.export_to_html(
            page_no=pno,
            image_mode=ImageRefMode.REFERENCED,
        )
        if html_text and html_text.strip():
            page_htmls.append((pno, html_text.strip()))
            (out_dir / f"page_{pno:04d}.html").write_text(
                html_text.strip(), encoding="utf-8"
            )

    if not page_htmls:
        raise RuntimeError("No HTML generated")

    # 5. Размеры страниц
    pages_info = [
        {
            "page": pno,
            "width": round(doc.pages[pno].size.width, 1),
            "height": round(doc.pages[pno].size.height, 1),
        }
        for pno in sorted(doc.pages.keys())
    ]

    metadata = {
        "source": {
            "file_name": file_name,
            "file_hash_sha256": file_hash,
            "page_count": len(page_htmls),
        },
        "pages": pages_info,
        "formula_bboxes": formula_bboxes,
        "docling": {
            "do_ocr": False,
            "do_formula_enrichment": False,
            "do_table_structure": True,
            "image_mode": "referenced",
            "serve_url": DOCLING_SERVE_URL,
        },
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "html_files": [f"page_{pno:04d}.html" for pno, _ in page_htmls],
        "saved_media_images": saved_media,
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info(
        "Docling HTML saved to %s: pages=%d, formula_bboxes=%d, media=%d (%.1fs)",
        out_dir, len(page_htmls), sum(len(v) for v in formula_bboxes.values()),
        saved_media, time.time() - t0,
    )
    return metadata
