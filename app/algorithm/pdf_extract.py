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
import json
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
# Символьные паттерны формул (эвристика): мат. операторы и символы,
# греческие буквы. Без '-', '/', скобок, '+' — они часты в обычной прозе
# и в индексах формул («x1j+1»)
_FORMULA_HINT_RE = re.compile(
    r'[=×÷±·≤≥≈≠∑∫√∞∂∆^_]'
    r'|[αβγδεζηθικλμνξπρστυφχψωΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ]'
)
# Демоут формулы в параграф: начинается с кириллицы, ≥4 кириллических
# слов или условие «если …» (guide.md п.6; для enrich-строк фильтр
# html_to_json не применяется)
_CYR_TOKEN_RE = re.compile(r'(?:^|\s)([а-яёА-ЯЁ]+)(?=\s|$|[,.;:])')
_CYR_START_RE = re.compile(r'^[а-яёА-ЯЁ«]')


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
    Индексируются СТРОКИ (line), а не блоки: у блоков PyMuPDF несколько
    независимых формул могут оказаться в одном bbox, что ломает
    геометрическую привязку (см. слияние многострочных формул).
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
                for line in b['lines']:
                    t = ''.join(span['text'] for span in line['spans']).strip()
                    if not t:
                        continue
                    t_norm = _norm(t)
                    if len(t_norm) < 3:
                        continue
                    lx0, ly0, lx1, ly1 = line['bbox']
                    bbox_map[(t_norm, pno)] = [round(lx0, 1), round(ly0, 1),
                                               round(lx1, 1), round(ly1, 1)]
    finally:
        pdf.close()
    return bbox_map


def _match_bbox(bbox_map: Dict, pno: int, text: str) -> Optional[List[float]]:
    """Ищет bbox для текста в bbox_map. Возвращает [x0,y0,x1,y1] или None.

    Только ТОЧНОЕ совпадение по нормализованному тексту: substring-фолбэки
    давали ложные bbox (строка «протяженность» в длинной формуле → неверная
    геометрия и ложные слияния многострочных формул). Карта построчная,
    поэтому для однострочных блоков точного совпадения достаточно;
    многострочные добираются из docling_document.json (restore_docling_bboxes).
    """
    if not text.strip():
        return None
    norm = _norm(text)
    norm_clean = norm.rstrip('.')
    for candidate in [norm, norm_clean, norm_clean + '.']:
        key = (candidate, pno)
        if key in bbox_map:
            return bbox_map[key]
    return None


def _merge_bbox(bboxes: List[List[float]]) -> List[float]:
    if not bboxes:
        return [0, 0, 0, 0]
    return [min(b[0] for b in bboxes), min(b[1] for b in bboxes),
            max(b[2] for b in bboxes), max(b[3] for b in bboxes)]


def restore_bboxes(json_result: Dict, pdf_path: str,
                   bbox_map: Optional[Dict] = None) -> int:
    """Проставляет bounding box блокам JSON из карты bbox по тексту."""
    if bbox_map:
        # Карта enrich неполна (строки, уже присутствующие в Docling, в неё
        # не попадают) — дополняем полной картой PDF; приоритет — enrich-записи
        # (более точный bbox строки-абзаца).
        full_map = build_bbox_map(pdf_path)
        full_map.update(bbox_map)
        bbox_map = full_map
    else:
        bbox_map = build_bbox_map(pdf_path)
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
        # Формулы, созданные enrich, уже имеют ТОЧНЫЙ строчный bbox —
        # переприсвоение по карте опасно: одинаковые строки («∗L∗/𝐿𝑠;» в двух
        # формулах) имеют один norm-ключ, и обе получают bbox последней строки.
        if btype == 'formula' and block.get('bounding box') and any(
                abs(v) > 0.001 for v in block['bounding box']):
            continue
        b = _match_bbox(bbox_map, pno, text)
        if b:
            block['bounding box'] = b
            matched += 1
    return matched


def restore_docling_bboxes(json_result: Dict, docling_json_path: str) -> int:
    """Проставляет bbox блокам из docling_document.json (prov текстов Docling).

    Docling хранит координаты в BOTTOMLEFT (y от низа страницы) — конвертируем
    в TOPLEFT через высоту страницы из pages. Заполняются только блоки без
    bbox (отсутствует или [0,0,0,0]) — PDF-координаты из restore_bboxes
    приоритетнее. Формулы не трогаются: их текст после подмены из PDF
    не совпадает с текстом Docling.
    """
    if not docling_json_path or not os.path.exists(docling_json_path):
        return 0
    try:
        with open(docling_json_path, encoding='utf-8') as f:
            doc = json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning('Docling json read failed: %s', e)
        return 0

    pages = doc.get('pages') or {}
    if not isinstance(pages, dict):
        pages = {}

    def page_height(pno: int) -> Optional[float]:
        p = pages.get(str(pno))
        if not p:
            return None
        size = p.get('size') or {}
        return size.get('height')

    # pno -> norm_text -> [x0, y0, x1, y1] (TOPLEFT)
    text_bbox: Dict[int, Dict[str, List[float]]] = defaultdict(dict)
    for item in doc.get('texts') or []:
        item_norm = _norm(item.get('text') or '')
        if not item_norm:
            continue
        provs = item.get('prov') or []
        if not provs:
            continue
        provs = provs[0] if provs and isinstance(provs[0], list) else provs
        per_page: Dict[int, List[List[float]]] = defaultdict(list)
        for prov in provs:
            if not prov:
                continue
            pno = prov.get('page_no')
            bb = prov.get('bbox') or {}
            if not pno or not bb or 'l' not in bb or 'r' not in bb \
                    or 't' not in bb or 'b' not in bb:
                continue
            h = page_height(pno)
            if not h:
                continue
            top_bbox = [round(float(bb['l']), 1),
                        round(float(h) - float(bb['b']), 1),
                        round(float(bb['r']), 1),
                        round(float(h) - float(bb['t']), 1)]
            per_page[pno].append(top_bbox)
        for pno, bboxes in per_page.items():
            text_bbox[pno][item_norm] = _merge_bbox(bboxes)

    # Таблицы: bbox из prov по порядку следования на странице (Docling-порядок
    # совпадает с порядком блоков JSON — оба из одного прогона Docling).
    table_bbox: Dict[int, List[List[float]]] = defaultdict(list)
    for tbl in doc.get('tables') or []:
        bboxes = []
        provs = tbl.get('prov') or []
        if provs and isinstance(provs[0], list):
            provs = provs[0]
        for prov in provs:
            if not prov:
                continue
            pno = prov.get('page_no')
            bb = prov.get('bbox') or {}
            if not pno or not bb or 'l' not in bb or 'r' not in bb \
                    or 't' not in bb or 'b' not in bb:
                continue
            h = page_height(pno)
            if not h:
                continue
            bboxes.append([round(float(bb['l']), 1),
                           round(float(h) - float(bb['b']), 1),
                           round(float(bb['r']), 1),
                           round(float(h) - float(bb['t']), 1)])
        if bboxes:
            table_bbox[max(prov.get('page_no', 1) for prov in provs
                           if prov and prov.get('page_no'))].append(_merge_bbox(bboxes))

    blocks = json_result.get('content', {}).get('document', {}).get('block', [])
    matched = 0
    table_idx: Dict[int, int] = defaultdict(int)

    def is_empty(bb) -> bool:
        return not bb or all(abs(v) < 0.001 for v in bb)

    for block in blocks:
        pno = block.get('page number', 1)
        btype = block.get('type', '')
        if btype == 'table':
            if is_empty(block.get('bounding box')):
                tb = table_bbox.get(pno) or []
                idx = table_idx[pno]
                if idx < len(tb):
                    block['bounding box'] = tb[idx]
                    matched += 1
                table_idx[pno] += 1
            continue
        if btype not in ('paragraph', 'heading', 'list'):
            continue
        pmap = text_bbox.get(pno)
        if not pmap:
            continue
        if btype == 'list':
            item_bboxes = []
            for item in block.get('items', []):
                if not isinstance(item, dict):
                    continue
                if is_empty(item.get('bounding box')):
                    b = pmap.get(_norm(item.get('content') or ''))
                    if b:
                        item['bounding box'] = list(b)
                        item_bboxes.append(b)
            if item_bboxes and is_empty(block.get('bounding box')):
                block['bounding box'] = _merge_bbox(item_bboxes)
                matched += 1
            continue
        if not is_empty(block.get('bounding box')):
            continue
        b = pmap.get(_norm(block.get('content') or ''))
        if b:
            block['bounding box'] = list(b)
            matched += 1
    return matched


def _flush_enriched_paragraph(blocks: List[Dict], bbox_map: Dict, pno: int,
                              para_lines: List[str], para_bboxes: List[List[float]],
                              counter: List[int]) -> None:
    """Склеивает строки PyMuPDF-блока в один параграф (фрагменты разорванных абзацев).

    bbox параграфа: для одной строки — bbox строки (иначе блок PyMuPDF, в котором
    лежат несколько независимых визуальных областей, даёт общий bbox, ломающий
    геометрические правила типа склейки формулы с условием «для ...»); для
    нескольких строк — объединение строк.
    """
    content = ' '.join(l.strip() for l in para_lines if l.strip())
    if not content:
        return
    if len(para_lines) == 1:
        bb = para_bboxes[0]
    else:
        bb = [min(pb[0] for pb in para_bboxes),
              min(pb[1] for pb in para_bboxes),
              max(pb[2] for pb in para_bboxes),
              max(pb[3] for pb in para_bboxes)]
    blocks.append({
        'type': 'paragraph',
        'page number': pno,
        'content': content,
        'bounding box': [round(bb[0], 1), round(bb[1], 1),
                         round(bb[2], 1), round(bb[3], 1)],
        '_enriched': True,
    })
    counter[0] += 1
    bbox_map[(_norm(content), pno)] = [round(bb[0], 1), round(bb[1], 1),
                                       round(bb[2], 1), round(bb[3], 1)]


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

    Классификация строки как формулы (по эвристикам):
      1) математический шрифт (доля math-спанов >= 0.3)
      2) символьные паттерны: мат. операторы/символы, греческие буквы
         (кроме «-», «/», скобок, «+» — часты в прозе и в индексах формул);
         с демоутом в параграф: начинается с кириллицы, ≥4 кириллических
         слова или условие «если …» (guide.md п.6)

    Returns:
        bbox_map: {(norm_text, pno): [l,t,r,b], ('__formula_img__', pno, seq): [...]}
    """
    pdf = fitz.open(pdf_path)
    blocks = json_result.get('content', {}).get('document', {}).get('block', [])
    bbox_map: Dict = {}
    added_cnt = [0]
    formula_img_seq = defaultdict(int)

    doc_text_by_page = _collect_doc_text_by_page(blocks)

    # Все страницы PDF: страницы без Docling-текста (только картинки, напр.
    # «Рис. N.N.N-x»-подписи) иначе выпадают из добора
    pages_in_doc = list(range(1, pdf.page_count + 1))

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
                lines.append((line_text, list(line['spans']), list(line['bbox'])))

            pending_paras: List[str] = []
            pending_pboxes: List[List[float]] = []
            for line_text, spans, line_bbox in lines:
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
                        _flush_enriched_paragraph(blocks, bbox_map, pno,
                                                  [caption], [line_bbox], added_cnt)
                    rest = line_text[m.end():].strip()
                    if rest:
                        pending_paras.append(soft_clean_line(rest))
                        pending_pboxes.append(line_bbox)
                    continue

                math_share = _line_math_share(spans)
                is_math_line = math_share >= 0.3

                # «где ...» — пояснение к формуле, а не формула: даже в math-шрифтах
                # это обычный абзац (эталон типизирует такие строки как paragraph)
                if is_math_line and re.match(r'^где\s', line_text.strip()):
                    is_math_line = False

                # Демоут в параграф (guide.md п.6): предложение, начинающееся
                # с кириллицы, ≥4 кириллических слов или условие «если …».
                # Только для НЕ-математических шрифтов (символьная эвристика):
                # строки math-шрифта — реальные формулы («v(H,d) = 0,8(H−d)/7,8,
                # если (Hm−d) менее или равно 7,8 м;» — эталон держит формулой),
                # а «K = 1, если θe ≤ θmin»-подобные демоутятся в html_to_json
                if not is_math_line and _FORMULA_HINT_RE.search(line_text):
                    cyr_tokens = _CYR_TOKEN_RE.findall(line_text)
                    if not (_CYR_START_RE.match(line_text) or len(cyr_tokens) >= 4
                            or 'если' in line_text):
                        # символьные паттерны: мат. операторы/символы, греческие
                        is_math_line = True

                if is_math_line:
                    if pending_paras:
                        _flush_enriched_paragraph(blocks, bbox_map, pno,
                                                  pending_paras, pending_pboxes, added_cnt)
                        pending_paras = []
                        pending_pboxes = []
                    new_block = {
                        'type': 'formula',
                        'page number': pno,
                        'content': line_text,
                        'latex': '',
                        # bbox СТРОКИ, а не блока: у блока PyMuPDF несколько
                        # независимых формул могут иметь общий bbox, что ломает
                        # геометрическое слияние многострочных формул
                        'bounding box': [round(line_bbox[0], 1), round(line_bbox[1], 1),
                                         round(line_bbox[2], 1), round(line_bbox[3], 1)],
                    }
                    blocks.append(new_block)
                    added_cnt[0] += 1
                    last_formula_idx = len(blocks) - 1
                else:
                    cleaned = soft_clean_line(line_text)
                    if is_garbage_line(cleaned):
                        continue
                    pending_paras.append(cleaned)
                    pending_pboxes.append(line_bbox)

            if pending_paras:
                _flush_enriched_paragraph(blocks, bbox_map, pno,
                                          pending_paras, pending_pboxes, added_cnt)

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
                        # Некодированная Docling-формула нередко покрывает
                        # НЕСКОЛЬКО визуальных строк (группа формул «θr= θg»,
                        # «θr= 0», ...). get_text разбивает строки по-своему
                        # (склеивает куски в одну строку) — такие клипы НЕ
                        # подменяем: строки точнее доберёт enrich (у него
                        # разбиение на line'ы PyMuPDF), а пустой figure
                        # уберётся в cleanup. Подменяем только однострочные.
                        parts = []
                        for ft_line in ft_text.split('\n'):
                            ft_line = ft_line.strip()
                            if len(ft_line) >= 3 \
                                    and not re.match(r'^[\d\s().,-]+$', ft_line):
                                parts.append(ft_line)
                        if len(parts) == 1:
                            replaced += 1
                            return f'<figure class="formula">{parts[0]}</figure>'
                    except Exception:
                        pass
                return m.group(0)

            result.append((pno, _NOT_DECODED_RE.sub(_replace_ft, html_text)))
    finally:
        pdf.close()
    if replaced:
        logger.info("Replaced %d 'Formula not decoded' markers with PDF text", replaced)
    return result
