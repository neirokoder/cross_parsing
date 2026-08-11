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
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.algorithm.html_to_json import html_to_document_json, _norm_for_key
from app.algorithm import pdf_extract
from app.algorithm.quality_metrics import compute_page_quality
from app.config import HTML_DIR, IMAGES_DIR, OUTPUT_DIR

logger = logging.getLogger(__name__)

_COLONTITUL_RE = re.compile(r'правила классификации и постройки морских судов')
_FRAG_MARK_RE = re.compile(r'^\d{1,2}\.\d{1,2}(?:\.\d{1,2})+\s')
_WORD_SPLIT_RE = re.compile(r'[\s,;:.()«»\-]+')


def _final_cleanup(json_result: Dict) -> None:
    """Финальные чистки результата кросс-парсинга (после PyMuPDF-добора)."""
    blocks = json_result.get('content', {}).get('document', {}).get('block', [])

    # 4.0: заголовок-одиночка в предложном регистре («Санкт-Петербург» на титуле) —
    #      Docling ошибочно типизирует его как heading; все настоящие заголовки
    #      документа — ВЕРХНИЙ РЕГИСТР
    _SINGLE_HEAD_RE = re.compile(r'^[А-ЯЁ][А-ЯЁа-яё\-]+$')
    for b in blocks:
        if b.get('type') == 'heading':
            c = (b.get('content') or '').strip()
            if _SINGLE_HEAD_RE.match(c) and c.upper() != c:
                b['type'] = 'paragraph'
                b.pop('heading level', None)

    # 4.1: колонтитулы «Правила классификации и постройки морских судов (часть XII)»
    blocks = [b for b in blocks
              if not (b.get('type') == 'paragraph'
                      and _COLONTITUL_RE.search((b.get('content') or '').lower()))]

    # 4.3: пустые формулы (без текста и без картинки)
    blocks = [b for b in blocks
              if not (b.get('type') == 'formula'
                      and not (b.get('content') or '').strip()
                      and not (b.get('latex') or '').strip()
                      and not (b.get('image_key') or ''))]

    # 4.4: склейка разорванных Docling-абзацев (фрагменты идут сразу за основным
    #      блоком на той же странице): «...на плотность всей» + «системы холодиль-
    #      ного агента...» → один абзац. Маркерный фрагмент «10.1.13 жилые...»
    #      склеивается без маркера (продолжение предложения предыдущего абзаца).
    # prev оканчивается на середине предложения (без точки/двоеточия/точки с запятой):
    # «...на плотность всей», «...прокладываться через», «...требованиям 8.1.2,».
    # Если prev оканчивается на «:»/«;» — это перечисление («...при следующих
    # условиях:», «...водяной завесой;»), склейка не нужна
    _PUNCT_END_RE = re.compile(r'[.!?…:;]\s*$')
    merged: List[Dict] = []
    for b in blocks:
        if b.get('type') == 'paragraph' and not b.get('_enriched'):
            prev = merged[-1] if merged else None
            if (prev and prev.get('type') == 'paragraph'
                    and prev.get('page number') == b.get('page number')):
                cur_norm = _norm_for_key(b)
                prev_norm = _norm_for_key(prev)
                m = _FRAG_MARK_RE.match(cur_norm)
                raw = (b.get('content') or '').strip()
                if m:
                    mr = re.match(_FRAG_MARK_RE, raw)
                    body_first = raw[len(mr.group(0)):] if mr else raw
                else:
                    body_first = raw
                # Первая буква фрагмента по ИСХОДНОМУ тексту должна быть строчной —
                # заглавная означает новое предложение («Санкт-Петербург» на титуле)
                if (body_first and body_first[0].islower() and prev_norm
                        and not _PUNCT_END_RE.search(prev_norm)):
                    text = (b.get('content') or '')
                    if m:
                        # Маркер фрагмента («10.1.13 жилые...») относится к началу
                        # продолжающегося предложения: «10.1.13 Приточные...жилые...»
                        if _FRAG_MARK_RE.match(prev_norm):
                            # prev уже с маркером: «8.1.4 и 8.1.5.» — это ссылка на
                            # существующий пункт (его блок есть на странице), а не
                            # новый пункт — склеиваем без маркера
                            marker_norm = re.match(_FRAG_MARK_RE, cur_norm).group(0)
                            marker_ref = any(
                                (o is not b and o.get('page number') == b.get('page number')
                                 and _FRAG_MARK_RE.match(_norm_for_key(o))
                                 and _norm_for_key(o).startswith(marker_norm))
                                for o in blocks)
                            if not marker_ref:
                                continue  # похоже на новый пункт — не склеиваем
                            prev['content'] = (prev.get('content') or '') + ' ' + text.strip()
                        else:
                            marker = re.match(_FRAG_MARK_RE, text).group(0)
                            text = re.sub(_FRAG_MARK_RE, '', text, count=1)
                            prev['content'] = (marker + (prev.get('content') or '')).strip() \
                                              + ' ' + text.strip()
                    else:
                        prev['content'] = (prev.get('content') or '') + ' ' + text.strip()
                    pb = prev.get('bounding box')
                    cb = b.get('bounding box')
                    if pb and cb and pb != [0, 0, 0, 0] and cb != [0, 0, 0, 0]:
                        prev['bounding box'] = [min(pb[0], cb[0]), min(pb[1], cb[1]),
                                                max(pb[2], cb[2]), max(pb[3], cb[3])]
                    continue
        merged.append(b)
    blocks = merged

    # 4.2: фрагменты и дубли — по нормализованному тексту в пределах страницы
    page_blocks: Dict[int, List[Dict]] = {}
    for b in blocks:
        page_blocks.setdefault(b.get('page number', 1), []).append(b)

    keep: List[Dict] = []
    for b in blocks:
        pno = b.get('page number', 1)
        norm = _norm_for_key(b)
        if not norm:
            keep.append(b)
            continue
        others = page_blocks.get(pno, [])
        enriched = bool(b.get('_enriched'))
        dropped = False

        # (a) точный дубль — оставляем первый (Docling-блок, enrich идёт позже)
        for o in others:
            if o is b:
                break
            if _norm_for_key(o) == norm:
                dropped = True
                break

        # (b) подстрока ≥12 символов и < 70% от текста контейнера
        #     (только для enrich-блоков: Docling-параграфы p9 «температуре морской
        #      воды...» сами являются эталонными блоками)
        if not dropped and enriched and len(norm) >= 12:
            for o in others:
                if o is b:
                    continue
                onorm = _norm_for_key(o)
                if onorm and norm in onorm and len(norm) < 0.7 * len(onorm):
                    dropped = True
                    break

        # (c) фрагмент с маркером пункта: «10.1.13 жилые...» vs «10.1.13 приточные...»
        if not dropped and len(norm) >= 12:
            m = _FRAG_MARK_RE.match(norm)
            if m:
                prefix = m.group(0)
                for o in others:
                    if o is b:
                        continue
                    onorm = _norm_for_key(o)
                    if onorm.startswith(prefix) and len(norm) < 0.4 * len(onorm):
                        dropped = True
                        break

        # (d) нечёткий дубль: ≥90% слов фрагмента встречаются в контейнере
        if not dropped and enriched and b.get('type') in ('paragraph', 'formula'):
            words = {w for w in _WORD_SPLIT_RE.split(norm) if len(w) >= 2}
            if len(words) >= 5:
                for o in others:
                    if o is b:
                        continue
                    onorm = _norm_for_key(o)
                    ow = {w for w in _WORD_SPLIT_RE.split(onorm) if len(w) >= 2}
                    if not ow or len(onorm) < len(norm) * 0.8:
                        continue
                    if len(words & ow) / len(words) >= 0.9:
                        dropped = True
                        break

        if not dropped:
            keep.append(b)

    json_result['content']['document']['block'] = keep
    logger.info("Cleanup: %d -> %d blocks", len(blocks), len(keep))


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

        # Титул: Docling «уплощает» заголовки в h2 — уровни из кегля PDF,
        # логотип издателя «РОССИЙСКИЙ МОРСКОЙ РЕГИСТР СУДОХОДСТВА» → paragraph
        pdf_extract.fix_title_page_headings(json_result, str(pdf_path))

        # bbox нужен ДО extract_formula_images (IoU-отсев таблиц-растров)
        matched = pdf_extract.restore_bboxes(json_result, str(pdf_path), bbox_map)
        logger.info("Bbox: restored %d block positions", matched)

        pdf_images = pdf_extract.save_images_from_pdf(str(pdf_path), str(use_dir))
        pdf_extract.update_image_keys(json_result, str(use_dir))
        pdf_extract.inject_missing_image_blocks(json_result, str(use_dir), pdf_images)
        pdf_extract.extract_formula_images(json_result, str(pdf_path), str(use_dir), bbox_map)
    finally:
        if clean_temp and not images_dir:
            shutil.rmtree(tmp_images, ignore_errors=True)

    # ---- Шаг 5: финальные чистки (колонтитулы, фрагменты, пустые формулы) ----
    _final_cleanup(json_result)

    # ---- Шаг 6: качество ----
    quality = _assess_quality(json_result)
    json_result["content"]["quality"].update(quality)

    # ---- Шаг 6: сохранение ----
    out_json = Path(out_json) if out_json else (OUTPUT_DIR / f"{stem}.json")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(json_result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Cross-parse result saved to %s", out_json)

    return json_result
