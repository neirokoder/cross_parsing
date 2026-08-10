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

from app.algorithm.text_cleaner import soft_clean_line, is_garbage_line

logger = logging.getLogger(__name__)

# Фрагменты имён шрифтов, указывающие на математический набор
_MATH_FONT_INDICATORS = ('math', 'symbol', 'mt extra', 'mathematical',
                          'pi', 'greek', 'monotype')

_NUM_RE = re.compile(r'^[\d\s\-—\/]+$')


# ---------------------------------------------------------------------------
# Вспомогательные
# ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    return re.sub(r'[\s\u00a0]+', ' ', text).strip().lower()


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
    added = 0
    formula_img_seq = defaultdict(int)

    doc_text_by_page = _collect_doc_text_by_page(blocks)

    pages_in_doc = sorted(doc_text_by_page.keys()) or list(range(1, pdf.page_count + 1))

    for pno in pages_in_doc:
        if pno < 1 or pno > pdf.page_count:
            continue
        page = pdf[pno - 1]
        page_h = page.rect.height
        page_doc_text = doc_text_by_page.get(pno, '')

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

            line_texts = []
            line_fonts = set()
            for line in b['lines']:
                line_text = ''.join(span['text'] for span in line['spans']).strip()
                if not line_text or len(line_text) < 3:
                    continue
                line_texts.append(line_text)
                for span in line['spans']:
                    line_fonts.add((span.get('font', '') or '').lower())

            if not line_texts:
                continue
            ttext = ' '.join(line_texts)
            t_norm = _norm(ttext)

            # Уже есть в Docling
            if page_doc_text and t_norm in page_doc_text:
                continue

            # Номера страниц
            if _NUM_RE.match(ttext.strip()):
                continue

            bx0, by0, bx1, by1 = b['bbox']

            is_caption = 'рис' in ttext.lower() and len(ttext) <= 80
            is_header = by1 < page_h * 0.15
            is_footer = by0 > page_h * 0.85
            is_math_font = any(
                any(ind in f for ind in _MATH_FONT_INDICATORS)
                for f in line_fonts
            )

            if is_math_font:
                label = 'formula'
            elif is_caption or is_header or is_footer:
                label = 'paragraph'
            else:
                label = 'paragraph'

            if label != 'formula':
                ttext_cleaned = soft_clean_line(ttext)
                if is_garbage_line(ttext_cleaned):
                    continue
                ttext_final = ttext_cleaned
            else:
                ttext_final = ttext

            new_block = {
                'type': label,
                'page number': pno,
                'content': ttext_final,
                'bounding box': [round(bx0, 1), round(by0, 1),
                                 round(bx1, 1), round(by1, 1)],
            }
            if label == 'formula':
                new_block['latex'] = ''
            blocks.append(new_block)
            added += 1

            bbox_map[(t_norm, pno)] = [round(bx0, 1), round(by0, 1),
                                       round(bx1, 1), round(by1, 1)]

    pdf.close()
    logger.info("Enrich: added %d blocks (formula_img candidates: %d)",
                added, sum(formula_img_seq.values()))
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


def extract_formula_images(
    json_result: Dict,
    pdf_path: str,
    images_dir: str,
    bbox_map: Dict,
) -> int:
    """
    Извлекает formula-изображения из PDF по bbox ('__formula_img__', pno, seq).
    Сохраняет в images_dir, добавляет type: 'formula' блоки в json_result.
    """
    keys = [k for k in bbox_map if isinstance(k, tuple) and len(k) == 3 and k[0] == '__formula_img__']
    if not keys:
        return 0

    os.makedirs(images_dir, exist_ok=True)
    pdf = fitz.open(pdf_path)
    blocks = json_result.get('content', {}).get('document', {}).get('block', [])
    added = 0
    try:
        for key in sorted(keys, key=lambda x: (x[1], x[2])):
            _prefix, pno, seq = key
            ix0, iy0, ix1, iy1 = bbox_map[key]
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
