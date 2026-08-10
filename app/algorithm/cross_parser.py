"""
Кросс-парсинг документа на двух движках:

  1. Docling (HTML, сохранённый заранее — см. docling/export_html.py):
     структура документа — heading/paragraph/table/list/image/formula.
  2. PyMuPDF (исходный PDF): добор пропущенного текста, детект формул
     (math-шрифты и image-блоки), восстановление bbox, извлечение картинок.

Результат — JSON в формате opendataloader (raw_ocr_v4):
  content.document.block[] / content.quality.per_page[].
"""
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.algorithm.html_to_json import html_to_document_json
from app.algorithm import pdf_extract
from app.algorithm.quality_metrics import compute_page_quality
from app.config import HTML_DIR, IMAGES_DIR, OUTPUT_DIR

logger = logging.getLogger(__name__)


def load_page_htmls(html_dir: Path) -> Tuple[List[Tuple[int, str]], Optional[Dict]]:
    """Загружает постраничный HTML из каталога (page_XXXX.html) + metadata.json."""
    html_dir = Path(html_dir)
    page_files = sorted(html_dir.glob("page_*.html"))
    if not page_files:
        raise FileNotFoundError(f"No page_*.html found in {html_dir}")

    page_htmls: List[Tuple[int, str]] = []
    for f in page_files:
        pno = int(f.stem.split("_")[1])
        page_htmls.append((pno, f.read_text(encoding="utf-8")))

    metadata = None
    meta_file = html_dir / "metadata.json"
    if meta_file.exists():
        metadata = json.loads(meta_file.read_text(encoding="utf-8"))

    return page_htmls, metadata


def _assess_quality(json_result: Dict) -> Dict:
    """Считает качество по страницам из блоков JSON."""
    blocks = json_result.get("content", {}).get("document", {}).get("block", [])
    by_page: Dict[int, List[Dict]] = {}
    for b in blocks:
        pno = b.get("page number", 1)
        by_page.setdefault(pno, []).append(b)

    reports = {}
    for pno in sorted(by_page.keys()):
        reports[pno] = compute_page_quality({"page": pno, "blocks": by_page[pno]})

    per_page = []
    for pno in sorted(reports.keys()):
        r = reports[pno]
        per_page.append({
            "page": pno,
            "confidence": round(r.overall_score, 3),
            "status": "low_confidence" if r.is_problematic else "ok",
        })
    confidence = round(sum(r.overall_score for r in reports.values()) / len(reports), 3) if reports else 0.0
    return {
        "confidence": confidence,
        "pages_processed": len(reports),
        "per_page": per_page,
    }


def cross_parse(
    pdf_path: str,
    html_dir: Optional[str] = None,
    images_dir: Optional[str] = None,
    out_json: Optional[str] = None,
    clean_temp: bool = True,
) -> Dict:
    """
    Кросс-парсинг PDF + сохранённого HTML Docling → JSON (raw_ocr_v4).

    Args:
        pdf_path: путь к исходному PDF (движок PyMuPDF)
        html_dir: каталог с page_XXXX.html от Docling (см. docling-html);
                  если None — ищется data/html/{stem}/
        images_dir: куда сохранять изображения (по умолчанию data/images/{stem}_tmp/)
        out_json: путь для сохранения результата (по умолчанию data/output/{stem}.json)
        clean_temp: удалять временный каталог изображений после обработки

    Returns:
        JSON документа (содержит content.document, content.quality)
    """
    pdf_path = Path(pdf_path)
    stem = pdf_path.stem
    html_dir = Path(html_dir) if html_dir else (HTML_DIR / stem)

    page_htmls, metadata = load_page_htmls(html_dir)
    file_name = pdf_path.name

    # ---- Шаг 1: подмена "Formula not decoded" текстом из PDF (bbox из metadata) ----
    formula_bboxes = (metadata or {}).get("formula_bboxes", {}) or {}
    # ключи JSON — строки, нормализуем в int
    formula_bboxes = {
        int(pno): [list(map(float, b)) for b in bboxes]
        for pno, bboxes in formula_bboxes.items()
    }
    page_htmls = pdf_extract.replace_not_decoded_formulas(page_htmls, str(pdf_path), formula_bboxes)

    # ---- Шаг 2: HTML → JSON (структура Docling) ----
    json_result = html_to_document_json(page_htmls, file_name)

    # ---- Шаг 3: размеры страниц из metadata, если есть ----
    if metadata and metadata.get("pages"):
        json_result["content"]["document"]["pages"] = metadata["pages"]

    # ---- Шаг 4: PyMuPDF-движок: добор, формулы, картинки, bbox ----
    tmp_images = Path(tempfile.mkdtemp(prefix=f"cross_parse_{stem}_"))
    use_dir = Path(images_dir) if images_dir else tmp_images
    use_dir.mkdir(parents=True, exist_ok=True)

    try:
        bbox_map = pdf_extract.enrich_blocks_from_pdf(json_result, str(pdf_path))

        pdf_images = pdf_extract.save_images_from_pdf(str(pdf_path), str(use_dir))
        pdf_extract.update_image_keys(json_result, str(use_dir))
        pdf_extract.inject_missing_image_blocks(json_result, str(use_dir), pdf_images)
        pdf_extract.extract_formula_images(json_result, str(pdf_path), str(use_dir), bbox_map)

        matched = pdf_extract.restore_bboxes(json_result, str(pdf_path), bbox_map)
        logger.info("Bbox: restored %d block positions", matched)
    finally:
        if clean_temp and not images_dir:
            shutil.rmtree(tmp_images, ignore_errors=True)

    # ---- Шаг 5: качество ----
    quality = _assess_quality(json_result)
    json_result["content"]["quality"].update(quality)

    # ---- Шаг 6: сохранение ----
    out_json = Path(out_json) if out_json else (OUTPUT_DIR / f"{stem}.json")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(json_result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Cross-parse result saved to %s", out_json)

    return json_result
