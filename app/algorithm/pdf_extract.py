"""
PyMuPDF-движок кросс-парсинга: добор пропущенного Docling'ом контента.

Работает поверх сохранённого HTML (JSON-блоков) и исходного PDF:
  - restore_bboxes            — восстановление bounding box блоков по тексту из PDF
  - enrich_blocks_from_pdf    — добор строк, пропущенных Docling (заголовки/футеры,
                                подписи "Рис.", параграфы) и детект формул
                                (math-шрифты, image-блоки)
  - save_images_from_pdf      — извлечение встроенных изображений из PDF
  - update_image_keys         — привязка image_key к image-блокам JSON
  - inject_missing_image_blocks — добавление картинок, не попавших в Docling
  - extract_formula_images    — вырезание formula-картинок по bbox
  - replace_not_decoded_formulas — подмена "Formula not decoded" текстом из PDF

Логика перенесена из parser_service (docling_mapper.enrich_docling_document и др.),
адаптирована на уровень JSON-блоков (без DoclingDocument).
"""
import io
import logging
import os
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import fitz

from app.algorithm.text_cleaner import soft_clean_line, is_garbage_line, remove_spaced_letters

logger = logging.getLogger(__name__)

# Фрагменты имён шрифтов, указывающие на математический набор
_MATH_FONT_INDICATORS = ('math', 'symbol', 'mt extra', 'mathematical',
                          'pi', 'greek', 'monotype')

_NUM_RE = re.compile(r'^[\d\s\-—\/]+$')
# Буквы (вкл. греческие и математические курсивные символы) — «G= qS/r» валидна
_LETTER_RE = re.compile(r'[А-Яа-яA-Za-z\u0370-\u03FF\U0001D400-\U0001D7FF]')
_NUM_SUFFIX_RE = re.compile(r'[\d\s]+\s*$')          # хвост «...формуле 2 2»
_FORMULA_NUM_RE = re.compile(r'^\(\d{1,2}(?:\.\d{1,2})+\)$')  # «(5.1.2)»
# Подпись таблицы: «Таблица 2.2.1 Группа холодильного» → «Таблица 2.2.1» + хвост
_TABLE_CAPT_RE = re.compile(r'^Таблица\s+\d{1,2}(?:\.\d{1,2}){1,2}($|\s)')


# ---------------------------------------------------------------------------
# Вспомогательные
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    text = re.sub(r'[\s\u00a0]+', ' ', text)
    text = re.sub(r'[\uf000-\uf8ff]', '', text)       # PUA-символы Docling — шум
    text = re.sub(r'[—–]', '-', text)                  # тире как в PDF («-»)
    text = re.sub(r'\s+([,.;:])', r'\1', text)         # «g , кг/с» → «g, кг/с»
    text = re.sub(r'\(\s+', '(', text)
    text = re.sub(r'\s+\)', ')', text)
    text = re.sub(r'(?<=[а-яёa-z0-9])\s+(?=[0-9])', '', text)  # «м 3» → «м3»
    return text.strip().lower()


def _span_is_math(span: Dict) -> bool:
    f = (span.get('font') or '').lower()
    return any(ind in f for ind in _MATH_FONT_INDICATORS)


def _line_math_share(spans: List[Dict]) -> float:
    total = sum(len(s['text']) for s in spans)
    if not total:
        return 0.0
    math_n = sum(len(s['text']) for s in spans if _span_is_math(s))
    return math_n / total


def _collect_doc_text_by_page(blocks: List[Dict]) -> Dict[int, str]:
    """Собирает полный текст Docling (из JSON-блоков) по страницам."""
    page_text: Dict[int, str] = {}
    for b in blocks:
        pno = b.get('page number', 1)
        texts = []
        btype = b.get('type', '')
        content = b.get('content', '') or ''
        if content.strip():
            texts.append(content)
        if btype == 'table':
            for row in b.get('rows', []):
                for cell in row.get('cells', []):
                    for cb in cell.get('block', []):
                        if cb.get('content'):
                            texts.append(cb['content'])
            for col in b.get('columns', []):
                if col:
                    texts.append(col)
        full = ' '.join(texts)
        if full.strip():
            page_text[pno] = page_text.get(pno, ' ') + _norm(full)
    return page_text


# ---------------------------------------------------------------------------
# Восстановление bounding box
# ---------------------------------------------------------------------------
def build_bbox_map(pdf_path: str, pages: Optional[List[int]] = None) -> Dict:
    """
    Строит карту bbox по тексту PyMuPDF: {(norm_text, pno): [x0,y0,x1,y1]}.
    Координаты в TOPLEFT (y растёт вниз) — как в итоговом JSON.
    """
    bbox_map: Dict = {}
    pdf = fitz.open(pdf_path)
    try:
        for pno in (pages or range(1, pdf.page_count + 1)):
            if pno < 1 or pno > pdf.page_count:
                continue
            page = pdf[pno - 1]
            for b in page.get_text('dict', sort=True)['blocks']:
                if b['type'] != 0:
                    continue
                line_texts = []
                for line in b['lines']:
                    t = ''.join(span['text'] for span in line['spans']).strip()
                    if t:
                        line_texts.append(t)
                if not line_texts:
                    continue
                ttext = ' '.join(line_texts)
                t_norm = _norm(ttext)
                if len(t_norm) < 3:
                    continue
                bbox_map[(t_norm, pno)] = [round(b['bbox'][0], 1), round(b['bbox'][1], 1),
                                           round(b['bbox'][2], 1), round(b['bbox'][3], 1)]
    finally:
        pdf.close()
    return bbox_map


def _match_bbox(bbox_map: Dict, pno: int, text: str) -> Optional[List[float]]:
    """Ищет bbox для текста в bbox_map. Возвращает [x0,y0,x1,y1] или None."""
    if not text.strip():
        return None
    norm = _norm(text)
    norm_clean = norm.rstrip('.')
    for candidate in [norm, norm_clean, norm_clean + '.']:
        key = (candidate, pno)
        if key in bbox_map:
            return bbox_map[key]
    for key, map_bbox in bbox_map.items():
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        map_text, map_page = key
        if map_page == pno:
            if len(norm) > 10 and norm in map_text:
                return map_bbox
            if len(map_text) > 10 and map_text in norm:
                return map_bbox
    return None


def _merge_bbox(bboxes: List[List[float]]) -> List[float]:
    if not bboxes:
        return [0, 0, 0, 0]
    return [min(b[0] for b in bboxes), min(b[1] for b in bboxes),
            max(b[2] for b in bboxes), max(b[3] for b in bboxes)]


def restore_bboxes(json_result: Dict, pdf_path: str,
                   bbox_map: Optional[Dict] = None) -> int:
    """Проставляет bounding box блокам JSON из карты bbox по тексту."""
    bbox_map = bbox_map or build_bbox_map(pdf_path)
    matched = 0
    blocks = json_result.get('content', {}).get('document', {}).get('block', [])
    for block in blocks:
        pno = block.get('page number', 1)
        btype = block.get('type', '')
        text = block.get('content', '') or ''

        if btype == 'table':
            # Для таблицы — объединяем bbox всех её ячеек по тексту
            cell_bboxes = []
            for row in block.get('rows', []):
                for cell in row.get('cells', []):
                    for cb in cell.get('block', []):
                        b = _match_bbox(bbox_map, pno, cb.get('content', ''))
                        if b:
                            cell_bboxes.append(b)
            if cell_bboxes:
                block['bounding box'] = _merge_bbox(cell_bboxes)
                matched += 1
            continue

        if btype == 'list':
            item_bboxes = []
            for item in block.get('items', []):
                item_text = item.get('content', '') if isinstance(item, dict) else str(item)
                b = _match_bbox(bbox_map, pno, item_text)
                if b:
                    item_bboxes.append(b)
                    if isinstance(item, dict):
                        item['bounding box'] = b
            if item_bboxes:
                block['bounding box'] = _merge_bbox(item_bboxes)
                matched += 1
            continue

        if not text.strip():
            continue
        b = _match_bbox(bbox_map, pno, text)
        if b:
            block['bounding box'] = b
            matched += 1
    return matched


def _flush_enriched_paragraph(blocks: List[Dict], bbox_map: Dict, pno: int,
                              x0: float, y0: float, x1: float, y1: float,
                              para_lines: List[str], counter: List[int]) -> None:
    """Склеивает строки PyMuPDF-блока в один параграф (фрагменты разорванных абзацев)."""
    content = ' '.join(l.strip() for l in para_lines if l.strip())
    if not content:
        return
    blocks.append({
        'type': 'paragraph',
        'page number': pno,
        'content': content,
        'bounding box': [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
        '_enriched': True,
    })
    counter[0] += 1
    bbox_map[(_norm(content), pno)] = [round(x0, 1), round(y0, 1),
                                       round(x1, 1), round(y1, 1)]


# ---------------------------------------------------------------------------
# Титульная страница: уровни заголовков из кегля PDF
# ---------------------------------------------------------------------------
_TITLE_LOGO_RE = re.compile(r'регистр судоходства')


def fix_title_page_headings(json_result: Dict, pdf_path: str) -> int:
    """
    Docling «уплощает» заголовки титульной страницы в h2; уровни восстанавливаем
    по кеглю шрифта из PDF: относительно максимального кегля страницы
    (≥0.7 → 1, ≥0.45 → 2, иначе → 3). На титуле РС-правил это 48→1, 24→2, 20/16→3.

    Логотип издателя «РОССИЙСКИЙ МОРСКОЙ РЕГИСТР СУДОХОДСТВА» — не заголовок,
    переводится в paragraph.
    """
    blocks = json_result.get('content', {}).get('document', {}).get('block', [])
    headings = [b for b in blocks
                if b.get('type') == 'heading' and b.get('page number') == 1]
    if not headings:
        return 0
    pdf = fitz.open(pdf_path)
    try:
        page = pdf[0]
        sizes: Dict[str, float] = {}
        for b in page.get_text('dict', sort=True)['blocks']:
            if b['type'] != 0:
                continue
            for line in b['lines']:
                t = ''.join(s['text'] for s in line['spans']).strip()
                if not t:
                    continue
                t_norm = _norm(t)
                sizes[t_norm] = max(sizes.get(t_norm, 0.0),
                                    max(s['size'] for s in line['spans']))
    finally:
        pdf.close()
    max_size = max(sizes.values()) if sizes else 0.0
    if not max_size:
        return 0
    changed = 0
    for b in headings:
        norm = _norm(b.get('content') or '')
        size = None
        if norm in sizes:
            size = sizes[norm]
        else:
            for key, s in sizes.items():
                if len(norm) > 12 and (norm in key or key in norm):
                    size = max(size or 0.0, s)
        if size is None:
            continue
        if _TITLE_LOGO_RE.search(norm):
            b['type'] = 'paragraph'
            b.pop('heading level', None)
            changed += 1
            continue
        ratio = size / max_size
        if ratio >= 0.7:
            b['heading level'] = 1
        elif ratio >= 0.45:
            b['heading level'] = 2
        else:
            b['heading level'] = 3
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# Enrich: добор пропущенного Docling'ом
# ---------------------------------------------------------------------------
def enrich_blocks_from_pdf(
    json_result: Dict,
    pdf_path: str,
) -> Dict:
    """
    Добор блоков, пропущенных Docling, из сырого PDF через PyMuPDF:
      - image-блоки PDF (небольшие, не в хедере/футере) → кандидаты в формулы,
        bbox сохраняется в bbox_map с ключом ('__formula_img__', pno, seq)
      - текстовые строки, отсутствующие в Docling → блоки paragraph/formula
        (math-шрифты → formula, 'рис' → caption, верх/низ страницы → header/footer)
      - bbox добавленных блоков проставляется сразу

    Returns:
        bbox_map: {(norm_text, pno): [l,t,r,b], ('__formula_img__', pno, seq): [...]}
    """
    pdf = fitz.open(pdf_path)
    blocks = json_result.get('content', {}).get('document', {}).get('block', [])
    bbox_map: Dict = {}
    added_cnt = [0]
    formula_img_seq = defaultdict(int)

    doc_text_by_page = _collect_doc_text_by_page(blocks)

    pages_in_doc = sorted(doc_text_by_page.keys()) or list(range(1, pdf.page_count + 1))

    for pno in pages_in_doc:
        if pno < 1 or pno > pdf.page_count:
            continue
        page = pdf[pno - 1]
        page_h = page.rect.height
        page_doc_text = doc_text_by_page.get(pno, '')
        page_doc_text_flat = page_doc_text.replace(' ', '')
        last_formula_idx = None

        for b in page.get_text('dict', sort=True)['blocks']:
            # ---------- ИЗОБРАЖЕНИЯ (кандидаты в формулы) ----------
            if b['type'] == 1:
                ix0, iy0, ix1, iy1 = b['bbox']
                img_w = ix1 - ix0
                img_h = iy1 - iy0
                if 10 < img_w < 400 and 10 < img_h < 200:
                    is_header = iy1 < page_h * 0.15
                    is_footer = iy0 > page_h * 0.85
                    if not is_header and not is_footer:
                        formula_img_seq[pno] += 1
                        key = ('__formula_img__', pno, formula_img_seq[pno])
                        bbox_map[key] = [round(ix0, 1), round(iy0, 1),
                                         round(ix1, 1), round(iy1, 1)]
                continue

            # ---------- ТЕКСТ ----------
            if b['type'] != 0:
                continue

            bx0, by0, bx1, by1 = b['bbox']

            lines = []
            for line in b['lines']:
                line_text = remove_spaced_letters(
                    ''.join(span['text'] for span in line['spans'])
                ).strip()
                if not line_text:
                    continue
                lines.append((line_text, list(line['spans'])))

            pending_paras: List[str] = []
            for line_text, spans in lines:
                t_norm = _norm(line_text)

                # Номер формулы «(5.1.2)» сразу после строки-формулы
                if _FORMULA_NUM_RE.match(line_text):
                    if last_formula_idx is not None:
                        blocks[last_formula_idx]['content'] += ' ' + line_text
                        last_formula_idx = None
                    continue

                # Хвостовые цифры-надстрочники: «...по формуле 2 2» → «...по формуле»
                t_without_digits = _NUM_SUFFIX_RE.sub('', line_text).rstrip()
                t_norm_nodig = _norm(t_without_digits) if t_without_digits else ''

                # Уже есть в Docling (в т.ч. без пробелов: «Жилыепомещения — ...»)
                if page_doc_text and (
                    t_norm in page_doc_text
                    or (t_norm_nodig and t_norm_nodig in page_doc_text)
                    or t_norm.replace(' ', '') in page_doc_text_flat
                ):
                    continue

                # Номера страниц и строки без букв («± 5 %», «2», «—»)
                if _NUM_RE.match(line_text.strip()) or not _LETTER_RE.search(line_text):
                    continue

                # Подпись таблицы «Таблица 2.2.1 Группа холодильного»:
                # «Таблица N.N.N» — отдельный параграф, остаток строки — в pending
                m = _TABLE_CAPT_RE.match(line_text)
                if m:
                    caption = m.group(0).strip()
                    if caption:
                        _flush_enriched_paragraph(blocks, bbox_map, pno, bx0, by0, bx1, by1,
                                                  [caption], added_cnt)
                    rest = line_text[m.end():].strip()
                    if rest:
                        pending_paras.append(soft_clean_line(rest))
                    continue

                math_share = _line_math_share(spans)
                is_math_line = math_share >= 0.3

                # «где ...» — пояснение к формуле, а не формула: даже в math-шрифтах
                # это обычный абзац (эталон типизирует такие строки как paragraph)
                if is_math_line and re.match(r'^где\s', line_text.strip()):
                    is_math_line = False

                if is_math_line:
                    if pending_paras:
                        _flush_enriched_paragraph(blocks, bbox_map, pno, bx0, by0, bx1, by1,
                                                  pending_paras, added_cnt)
                        pending_paras = []
                    new_block = {
                        'type': 'formula',
                        'page number': pno,
                        'content': line_text,
                        'latex': '',
                        'bounding box': [round(bx0, 1), round(by0, 1),
                                         round(bx1, 1), round(by1, 1)],
                    }
                    blocks.append(new_block)
                    added_cnt[0] += 1
                    last_formula_idx = len(blocks) - 1
                else:
                    cleaned = soft_clean_line(line_text)
                    if is_garbage_line(cleaned):
                        continue
                    pending_paras.append(cleaned)

            if pending_paras:
                _flush_enriched_paragraph(blocks, bbox_map, pno, bx0, by0, bx1, by1,
                                          pending_paras, added_cnt)

    pdf.close()
    logger.info("Enrich: added %d blocks (formula_img candidates: %d)",
                added_cnt[0], sum(formula_img_seq.values()))
    return bbox_map


# ---------------------------------------------------------------------------
# Картинки из PDF
# ---------------------------------------------------------------------------
def save_images_from_pdf(pdf_path: str, images_dir: str) -> List[Tuple[int, str, str, List[float]]]:
    """Извлекает ВСЕ встроенные изображения из PDF через PyMuPDF.

    Returns:
        List of (page_no, image_path, extension, [x0,y0,x1,y1]).
    """
    from PIL import Image as PILImage

    os.makedirs(images_dir, exist_ok=True)
    result: List[Tuple[int, str, str, List[float]]] = []
    counter_per_page: Dict[int, int] = {}

    pdf = fitz.open(pdf_path)
    try:
        for pno in range(pdf.page_count):
            page = pdf[pno]
            image_list = page.get_images(full=True)
            img_info_list = page.get_image_info()

            for img_info, img_info_detail in zip(image_list, img_info_list):
                xref = img_info[0]
                bbox = list(img_info_detail.get('bbox', [0, 0, 0, 0]))
                try:
                    base_image = pdf.extract_image(xref)
                except Exception:
                    continue
                img_bytes = base_image.get("image")
                if not img_bytes:
                    continue
                try:
                    pil_img = PILImage.open(io.BytesIO(img_bytes))
                    pno_1 = pno + 1
                    counter_per_page.setdefault(pno_1, 0)
                    counter_per_page[pno_1] += 1
                    fname = f"page_{pno_1}_{counter_per_page[pno_1]}.png"
                    fpath = os.path.join(images_dir, fname)
                    pil_img.save(fpath, format='PNG')
                    result.append((pno_1, fpath, ".png", bbox))
                except Exception as e:
                    logger.warning("Failed to save PDF image page=%d xref=%d: %s", pno + 1, xref, e)
    finally:
        pdf.close()

    logger.debug("Saved %d images from PDF", len(result))
    return result


def update_image_keys(json_result: Dict, images_dir: str) -> int:
    """Проставляет image_key в JSON-блоках 'image' по сохранённым файлам."""
    blocks = json_result.get('content', {}).get('document', {}).get('block', [])
    counter_per_page: Dict[int, int] = {}
    updated = 0
    for block in blocks:
        if block.get('type') != 'image':
            continue
        pno = block.get('page number', 1)
        counter_per_page.setdefault(pno, 0)
        counter_per_page[pno] += 1
        fname = f"page_{pno}_{counter_per_page[pno]}.png"
        fpath = os.path.join(images_dir, fname)
        if os.path.exists(fpath):
            block['image_key'] = 'images/' + fname
            block['_temp_path'] = fpath
            updated += 1
    return updated


def inject_missing_image_blocks(
    json_result: Dict,
    images_dir: str,
    pdf_images: List[Tuple[int, str, str, List[float]]],
) -> int:
    """Добавляет блоки изображений для файлов, извлечённых из PDF,
    но не привязанных ни к одному блоку в JSON."""
    if not pdf_images:
        return 0
    blocks = json_result.get('content', {}).get('document', {}).get('block', [])
    existing_ids = set()
    for blk in blocks:
        v = blk.get('_temp_path', '') or blk.get('image_key', '')
        if v:
            existing_ids.add(v)

    added = 0
    for pno, path, _ext, bbox in pdf_images:
        if path in existing_ids:
            continue
        fname = os.path.basename(path)
        new_block = {
            'type': 'image',
            'page number': pno,
            'content': '',
            'image_key': 'images/' + fname,
            'bounding box': bbox or [0, 0, 0, 0],
            '_temp_path': path,
        }
        blocks.append(new_block)
        added += 1
    return added


def _iou(a: List[float], b: List[float]) -> float:
    """IoU двух bbox [x0,y0,x1,y1]."""
    ix0 = max(a[0], b[0])
    iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2])
    iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def extract_formula_images(
    json_result: Dict,
    pdf_path: str,
    images_dir: str,
    bbox_map: Dict,
) -> int:
    """
    Извлекает formula-изображения из PDF по bbox ('__formula_img__', pno, seq).
    Сохраняет в images_dir, добавляет type: 'formula' блоки в json_result.
    Пропускает кандидатов, перекрывающих существующие текстовые блоки
    (растровые таблицы и т.п. не должны становиться формулами).
    """
    keys = [k for k in bbox_map if isinstance(k, tuple) and len(k) == 3 and k[0] == '__formula_img__']
    if not keys:
        return 0

    blocks = json_result.get('content', {}).get('document', {}).get('block', [])
    # bbox существующих блоков (после restore_bboxes), включая изображения —
    # логотип на титуле не должен становиться формулой
    existing_bboxes = [
        b.get('bounding box') for b in blocks
        if b.get('bounding box') and b.get('bounding box') != [0, 0, 0, 0]
    ]

    os.makedirs(images_dir, exist_ok=True)
    pdf = fitz.open(pdf_path)
    added = 0
    try:
        for key in sorted(keys, key=lambda x: (x[1], x[2])):
            _prefix, pno, seq = key
            img_bbox = bbox_map[key]
            if existing_bboxes and any(
                _iou(img_bbox, eb) >= 0.5 for eb in existing_bboxes
            ):
                logger.info("Formula image p%d seq %d skipped: overlaps text block", pno, seq)
                continue
            ix0, iy0, ix1, iy1 = img_bbox
            page = pdf[pno - 1]
            pix = page.get_pixmap(clip=fitz.Rect(ix0, iy0, ix1, iy1))
            fname = f"formula_p{pno}_{seq}.png"
            fpath = os.path.join(images_dir, fname)
            pix.save(fpath)
            blocks.append({
                'type': 'formula',
                'page number': pno,
                'content': '',
                'latex': '',
                'image_key': 'images/' + fname,
                'bounding box': [round(ix0, 1), round(iy0, 1),
                                 round(ix1, 1), round(iy1, 1)],
                '_temp_path': fpath,
            })
            added += 1
    finally:
        pdf.close()
    return added


# ---------------------------------------------------------------------------
# Подмена "Formula not decoded"
# ---------------------------------------------------------------------------
_NOT_DECODED_RE = re.compile(
    r'<div\s+class="formula-not-decoded">\s*Formula not decoded\s*</div>',
    re.IGNORECASE,
)


def replace_not_decoded_formulas(
    page_htmls: List[Tuple[int, str]],
    pdf_path: str,
    formula_bboxes: Dict[int, List[List[float]]],
) -> List[Tuple[int, str]]:
    """
    Подменяет 'Formula not decoded' в HTML на текст формулы, извлечённый
    из сырого PDF по bbox (координаты Docling — BOTTOMLEFT, конвертация в TOPLEFT).
    """
    if not formula_bboxes:
        return page_htmls
    pdf = fitz.open(pdf_path)
    result = []
    replaced = 0
    try:
        for pno, html_text in page_htmls:
            bboxes = list(formula_bboxes.get(pno, []))
            if not bboxes:
                result.append((pno, html_text))
                continue

            def _replace_ft(m: re.Match) -> str:
                nonlocal replaced
                if bboxes:
                    bx = bboxes.pop(0)
                    try:
                        page_obj = pdf[pno - 1]
                        page_h = page_obj.rect.height
                        # Docling bbox в BOTTOMLEFT: (l, t, r, b), t > b
                        sy0 = page_h - max(bx[1], bx[3])
                        sy1 = page_h - min(bx[1], bx[3])
                        pad = 3.0
                        clip = fitz.Rect(
                            bx[0], max(0, sy0 - pad),
                            bx[2], min(page_h, sy1 + pad),
                        )
                        ft_text = page_obj.get_text('text', clip=clip).strip()
                        ft_line = ft_text.split('\n')[0].strip() if ft_text else ''
                        if len(ft_line) >= 3 and not re.match(r'^[\d\s().,-]+$', ft_line):
                            replaced += 1
                            return f'<figure class="formula">{ft_line}</figure>'
                    except Exception:
                        pass
                return m.group(0)

            result.append((pno, _NOT_DECODED_RE.sub(_replace_ft, html_text)))
    finally:
        pdf.close()
    if replaced:
        logger.info("Replaced %d 'Formula not decoded' markers with PDF text", replaced)
    return result
