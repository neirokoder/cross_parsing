"""
HTML → JSON (формат opendataloader/raw_ocr_v4).

SAX-парсер HTML-страницы, экспортированной Docling'ом (export_to_html):
извлекает блоки heading, paragraph, table, list, image, formula.
Перенесено из parser_service (app/services/parsers/docling/html_to_json.py).
"""
import json
import re
from html.parser import HTMLParser
from typing import List, Dict, Any, Optional, Tuple

from app.algorithm.text_cleaner import remove_spaced_letters


class _PageHtmlParser(HTMLParser):
    """SAX-парсер HTML-страницы от Docling export_to_html()."""

    def __init__(self):
        super().__init__()
        self.blocks: List[Dict[str, Any]] = []
        self._in_page = False
        self._page_depth = 0

        self._text_buf: List[str] = []
        self._current_tag: Optional[str] = None

        self._in_table = False
        self._table_rows: List[List[str]] = []
        self._current_row_cells: List[str] = []
        self._current_cell_text: List[str] = []
        self._table_has_th = False
        self._current_row_is_th = False
        self._current_row_all_th = True
        self._table_header_rows: List[int] = []

        self._list_type: Optional[str] = None
        self._list_items: List[str] = []
        self._in_list_item = False

        self._in_figure = False
        self._figure_class = ''
        self._figcaption = ''
        self._in_figcaption = False
        self._formula_text: List[str] = []

    def _flush_text(self):
        text = ''.join(self._text_buf).strip()
        if text:
            if self._current_tag == 'p':
                self.blocks.append({'type': 'paragraph', 'text': text})
            elif self._current_tag and self._current_tag.startswith('h'):
                level = int(self._current_tag[1])
                self.blocks.append({'type': 'heading', 'level': level, 'text': text})
        self._text_buf = []
        self._current_tag = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == 'div' and attrs_dict.get('class') == 'page':
            self._in_page = True
            self._page_depth = 1
            return

        if not self._in_page:
            return

        if tag == 'div':
            self._page_depth += 1

        self._in_figcaption = False

        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._flush_text()
            self._current_tag = tag
            self._text_buf = []

        elif tag == 'p':
            self._flush_text()
            self._current_tag = 'p'
            self._text_buf = []

        elif tag == 'table':
            self._flush_text()
            self._in_table = True
            self._table_rows = []
            self._table_has_th = False

        elif tag == 'tr':
            if self._in_table:
                self._current_row_cells = []
                self._current_row_is_th = False
                self._current_row_all_th = True

        elif tag == 'th':
            if self._in_table:
                self._current_row_is_th = True
                self._table_has_th = True
                self._current_cell_text = []

        elif tag == 'td':
            if self._in_table:
                self._current_cell_text = []
                self._current_row_all_th = False

        elif tag == 'figure':
            self._flush_text()
            self._in_figure = True
            self._figure_class = attrs_dict.get('class', '')
            self._figcaption = ''
            self._in_figcaption = False
            self._formula_text = []

        elif tag == 'img':
            if self._in_figure:
                self._img_alt = attrs_dict.get('alt', '')

        elif tag == 'figcaption':
            self._in_figcaption = True

        elif tag in ('ul', 'ol'):
            self._flush_text()
            self._list_type = 'bullet' if tag == 'ul' else 'numbered'
            self._list_items = []

        elif tag == 'li':
            self._in_list_item = True
            self._list_items.append('')

    def handle_endtag(self, tag):
        if not self._in_page:
            return

        if tag == 'div':
            if self._page_depth <= 1:
                self._in_page = False
            self._page_depth -= 1
            return

        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            if self._current_tag == tag:
                self._flush_text()
            else:
                self._text_buf = []
                self._current_tag = None

        elif tag == 'p':
            if self._current_tag == 'p':
                self._flush_text()
            else:
                self._text_buf = []
                self._current_tag = None

        elif tag == 'table':
            if self._in_table:
                self._in_table = False
                if self._table_rows:
                    self.blocks.append({
                        'type': 'table',
                        'rows': self._table_rows,
                        'has_header': self._table_has_th,
                        'header_rows': list(self._table_header_rows),
                    })

        elif tag == 'tr':
            if self._in_table and self._current_row_cells:
                ri = len(self._table_rows)
                if self._current_row_is_th and self._current_row_all_th:
                    self._table_header_rows.append(ri)
                self._table_rows.append(self._current_row_cells)
                self._current_row_cells = []

        elif tag in ('td', 'th'):
            if self._in_table:
                cell_text = ''.join(self._current_cell_text).strip()
                self._current_row_cells.append(cell_text)
                self._current_cell_text = []

        elif tag == 'figure':
            if self._in_figure:
                self._in_figure = False
                if self._figure_class == 'formula':
                    formula_content = ''.join(self._formula_text).strip() or self._figcaption
                    self.blocks.append({'type': 'formula', 'text': formula_content})
                else:
                    caption = self._figcaption or ''
                    self.blocks.append({'type': 'image', 'text': caption})

        elif tag == 'figcaption':
            self._in_figcaption = False

        elif tag in ('ul', 'ol'):
            if self._list_items:
                self.blocks.append({
                    'type': 'list',
                    'list_type': self._list_type,
                    'items': self._list_items,
                })
            self._list_items = []
            self._list_type = None

        elif tag == 'li':
            self._in_list_item = False

    def handle_data(self, data):
        if not self._in_page:
            return

        if self._in_table:
            self._current_cell_text.append(data)
        elif self._in_figcaption:
            self._figcaption += data
        elif self._in_list_item and self._list_items is not None:
            self._list_items[-1] += data
        elif self._in_figure and self._figure_class == 'formula':
            self._formula_text.append(data)
        elif self._current_tag in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._text_buf.append(data)


def parse_table_from_html(rows: List[List[str]], has_header: bool = False,
                          header_rows: Optional[List[int]] = None) -> Dict[str, Any]:
    """Парсит HTML-таблицу (список списков ячеек) в структурированный JSON.

    columns выбирается из header_rows (последняя полная строка) или rows[0].
    """
    if not rows:
        return {
            'type': 'table',
            'number_of_rows': 0,
            'number_of_columns': 0,
            'columns': [],
            'rows': [],
        }

    num_cols = max(len(r) for r in rows) if rows else 0

    _SWAP_CODE_RE = re.compile(r'^[A-Z]\d[\d,()\sA-Z-]+$')
    _SWAP_FORMULA_RE = re.compile(r'[·×≤≥±−\uf0d7\uf044]')
    _HEADER_MATH_RE = re.compile(r'[·×≤≥±−\uf0d7\uf044φθα-ω∆∇∑∫√∞π]', re.IGNORECASE)

    # Определяем, разрешён ли per-row swap: заголовки должны показать, что
    # колонка 1 — текст/код, а колонка 2 — формула (или наоборот).
    _allow_row_swap = False
    if num_cols == 2 and has_header and header_rows:
        hdr_role = [None, None]
        for ri in sorted(header_rows):
            if ri < len(rows):
                for ci in range(min(2, len(rows[ri]))):
                    txt = rows[ri][ci].strip()
                    if not txt:
                        continue
                    if hdr_role[ci] != 'math':
                        hdr_role[ci] = 'math' if _HEADER_MATH_RE.search(txt) else 'text'
        if hdr_role[0] == 'text' and hdr_role[1] == 'math':
            _allow_row_swap = True
        elif hdr_role[0] == 'math' and hdr_role[1] == 'text':
            _allow_row_swap = True

    result_rows = []
    for ri, row_cells in enumerate(rows):
        cells = list(row_cells)
        while len(cells) < num_cols:
            cells.append('')
        cell_objects = []
        for ci, cell_text in enumerate(cells[:num_cols]):
            cell_objects.append({
                'type': 'table cell',
                'row_number': ri + 1,
                'column_number': ci + 1,
                'row_span': 1,
                'column_span': 1,
                'page': 1,
                'bbox': [0, 0, 0, 0],
                'block': [{'content': cell_text.strip(), 'font': {}}],
            })
        if _allow_row_swap and len(cell_objects) == 2:
            t0 = (cell_objects[0]['block'][0]['content'] if cell_objects[0]['block'] else '').strip()
            t1 = (cell_objects[1]['block'][0]['content'] if cell_objects[1]['block'] else '').strip()
            if _SWAP_FORMULA_RE.search(t0) and _SWAP_CODE_RE.match(t1):
                cell_objects[0], cell_objects[1] = cell_objects[1], cell_objects[0]
        result_rows.append({
            'type': 'table row',
            'row_number': ri + 1,
            'cells': cell_objects,
        })

    columns = []
    final_rows = result_rows

    if has_header and rows:
        if header_rows:
            sorted_h = sorted(header_rows)
            # Блок consecutive заголовков от начала таблицы
            consecutive = []
            for ri in sorted_h:
                if not consecutive or ri == consecutive[-1] + 1:
                    consecutive.append(ri)
                else:
                    break
            if consecutive:
                last_h = consecutive[-1]
                if last_h < len(rows):
                    columns = list(rows[last_h])
                    while len(columns) < num_cols:
                        columns.append('')
                    # Заполняем пустые ячейки из строк выше (нижний слой)
                    for ri in reversed(consecutive[:-1]):
                        for ci in range(num_cols):
                            if not columns[ci].strip() and ci < len(rows[ri]):
                                columns[ci] = rows[ri][ci]
                hdr_count = len(consecutive)
                if hdr_count < len(result_rows):
                    final_rows = result_rows[hdr_count:]
                else:
                    final_rows = []
        if not columns:
            columns = rows[0] if rows else []
            if len(result_rows) > 1:
                final_rows = result_rows[1:]

    return {
        'type': 'table',
        'number_of_rows': len(final_rows),
        'number_of_columns': num_cols,
        'columns': columns,
        'rows': final_rows,
    }


def html_to_json_blocks(page_html: str, page_number: int = 1) -> List[Dict[str, Any]]:
    """Конвертирует HTML страницы в список блоков opendataloader-формата."""
    parser = _PageHtmlParser()
    parser.feed(page_html)
    raw_blocks = parser.blocks

    result = []
    for block in raw_blocks:
        bt = block['type']

        if bt == 'heading':
            result.append({
                'type': 'heading',
                'page number': page_number,
                'heading level': block['level'],
                'content': block['text'],
                'bounding box': [0, 0, 0, 0],
            })

        elif bt == 'paragraph':
            result.append({
                'type': 'paragraph',
                'page number': page_number,
                'content': block['text'],
                'bounding box': [0, 0, 0, 0],
            })

        elif bt == 'list':
            items = block.get('items', [])
            result.append({
                'type': 'list',
                'page number': page_number,
                'list_type': block.get('list_type', 'bullet'),
                'content': '\n'.join(items),
                'items': [{'content': item} for item in items],
                'bounding box': [0, 0, 0, 0],
            })

        elif bt == 'table':
            parsed = parse_table_from_html(
                block['rows'],
                has_header=block.get('has_header', False),
                header_rows=block.get('header_rows', []),
            )
            parsed['page number'] = page_number
            parsed['bounding box'] = [0, 0, 0, 0]
            result.append(parsed)

        elif bt == 'image':
            result.append({
                'type': 'image',
                'page number': page_number,
                'content': block['text'] or '',
                'image_key': '',
                'bounding box': [0, 0, 0, 0],
            })

        elif bt == 'formula':
            formula_text = block.get('text', '')
            result.append({
                'type': 'formula',
                'page number': page_number,
                'content': formula_text,
                'latex': formula_text,
                'bounding box': [0, 0, 0, 0],
            })

    return result


def _group_captions_with_images(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Группирует caption-блоки с image-блоками на основе близости в потоке.
    Если caption идет сразу после image, объединяет их.
    """
    if not blocks:
        return blocks

    result = []
    i = 0
    while i < len(blocks):
        current = blocks[i]

        # Если это image блок, проверяем следующий блок на caption
        if current.get('type') == 'image':
            if i + 1 < len(blocks):
                next_block = blocks[i + 1]
                next_content = (next_block.get('content') or '').strip()

                # Caption обычно короткий и содержит "рис" или "fig"
                is_caption = (
                    next_block.get('type') == 'paragraph' and
                    len(next_content) < 100 and
                    ('рис' in next_content.lower() or 'fig' in next_content.lower())
                )

                if is_caption:
                    # Объединяем caption с image
                    current['content'] = next_content
                    result.append(current)
                    i += 2  # Пропускаем caption блок
                    continue

        result.append(current)
        i += 1

    return result


# ---------------------------------------------------------------------------
# Постобработка Docling-блоков (дефекты A–I)
# ---------------------------------------------------------------------------

# Маркер пункта: «1.3.2», «8.1.4», «10.1.13» (минимум 2 уровня)
_MARK_RE = re.compile(r'^\d{1,2}\.\d{1,2}(?:\.\d{1,2})+\s')
# Подпункт: «.1», «.1.2» (ведущая точка + цифры)
_SUBMARK_RE = re.compile(r'^\.\d{1,3}(?:\.\d{1,3})?(\s|$)')

# Склейка параграфов Docling: «...изолированы. 8.1.2 Изоляционные...»
# и «...СОСТАВА ГАЗОВОЙ СРЕДЫ 11.3.1 Должны быть...» (без точки перед маркером)
_MERGE_SPLIT_RE = re.compile(
    r'(?<=\.)\s+(?=\d{1,2}\.\d{1,2}(?:\.\d{1,2})+\s+[А-ЯA-Z«(])'
    r'|\s+(?=\d{1,2}\.\d{1,2}\.\d{1,2}\s+[А-ЯA-Z«(])'
    r'|(?<=\.)\s+(?=Приточные и вытяжные каналы)'
)

# Фрагмент склейки, похожий на заголовок: «11.3 ИСПЫТАНИЯ УСТАНОВОК...»
_HEADING_LIKE_RE = re.compile(r'^\d{1,2}(?:\.\d{1,2})?\s+[А-ЯA-Z][А-ЯA-Z\s\-—]*$')


def _apply_spaced_letters(blocks: List[Dict[str, Any]]) -> None:
    """Снимает разрядку «Т а б л и ц а 2.2.1» → «Таблица 2.2.1» во всех текстах."""
    for b in blocks:
        bt = b.get('type', '')
        if bt in ('paragraph', 'heading'):
            b['content'] = remove_spaced_letters(b.get('content') or '')
        elif bt == 'list':
            for item in b.get('items', []):
                if isinstance(item, dict):
                    item['content'] = remove_spaced_letters(item.get('content') or '')
        elif bt == 'table':
            for row in b.get('rows', []):
                for cell in row.get('cells', []):
                    for cb in cell.get('block', []):
                        cb['content'] = remove_spaced_letters(cb.get('content', ''))


def _trim_heading_suffix(blocks: List[Dict[str, Any]]) -> None:
    """Убирает хвостовую « 1» в верхнерегистровых заголовках
    («ПЕРЕЧЕНЬ ИЗМЕНЕНИЙ 1» → «ПЕРЕЧЕНЬ ИЗМЕНЕНИЙ»)."""
    for b in blocks:
        if b.get('type') != 'heading':
            continue
        c = b.get('content') or ''
        if c and c.upper() == c:
            b['content'] = re.sub(r'\s+1$', '', c).strip()


def _split_merged_paragraphs(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Разрезает склеенные Docling параграфы по маркерам пунктов:
    «8.1.1 ... 8.1.2 ...» → отдельные параграфы; фрагменты-заголовки перетипируются."""
    result: List[Dict[str, Any]] = []
    for b in blocks:
        if b.get('type') != 'paragraph':
            result.append(b)
            continue
        content = b.get('content') or ''
        pieces = _MERGE_SPLIT_RE.split(content)
        if len(pieces) == 1:
            result.append(b)
            continue
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            nb = dict(b)
            if _HEADING_LIKE_RE.match(piece):
                nb['type'] = 'heading'
                nb['heading level'] = 2
                nb.pop('bounding box', None)
            nb['content'] = piece
            result.append(nb)
    return result


def _split_lists(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Разрезает списки вида «подпункты → пункт → пункт → подпункты»:
    1.3.2 + .1..7 | 1.3.3 | 1.3.4 + .1..8 (безопасно для 7.2.2..7.2.7 — там
    подпунктов до F-F пары нет)."""
    result: List[Dict[str, Any]] = []
    for b in blocks:
        if b.get('type') != 'list':
            result.append(b)
            continue
        items = b.get('items', [])
        texts = [it.get('content', '') if isinstance(it, dict) else str(it)
                 for it in items]
        n = len(texts)
        if n < 3:
            result.append(b)
            continue
        is_f = [bool(_MARK_RE.match(t)) for t in texts]
        is_sub = [bool(_SUBMARK_RE.match(t)) for t in texts]
        pairs = [(i, i + 1) for i in range(n - 1) if is_f[i] and is_f[i + 1]]
        if not pairs:
            result.append(b)
            continue
        if not any(is_sub[k] for i, _j in pairs for k in range(0, i)):
            result.append(b)
            continue
        cut = set()
        for i, j in pairs:
            cut.add(i)
            cut.add(j)
        start = 0
        for c in sorted(cut):
            result.append({**b, 'items': items[start:c],
                           'content': '\n'.join(texts[start:c])})
            start = c
        result.append({**b, 'items': items[start:],
                       'content': '\n'.join(texts[start:])})
    return result


def _split_formula_where(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """R1: «формула, где пояснение» → формула + параграф «где …».
    Режет по «, где», если левая часть содержит «=» (признак формулы).
    Обобщается: любая формула с пояснением условных обозначений."""
    _WHERE_RE = re.compile(r',\s+где\b')
    result: List[Dict[str, Any]] = []
    for b in blocks:
        if b.get('type') != 'paragraph':
            result.append(b)
            continue
        content = (b.get('content') or '').strip()
        m = _WHERE_RE.search(content)
        if not m or '=' not in content[:m.start()]:
            result.append(b)
            continue
        left = content[:m.start()].rstrip().strip()
        right = content[m.end():].strip()
        nb = dict(b)
        nb['content'] = left
        nb['type'] = 'formula'
        nb['_where_formula'] = True
        result.append(nb)
        if right:
            nb2 = dict(b)
            nb2['content'] = 'где ' + right
            result.append(nb2)
    return result


def _merge_fragments(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """R2: склеивает обрывки строк: «Протяженность доп» + «= 16° в остальных случаях;»
    или «θr -» + «угол смещения…». Фрагмент-продолжение не начинается с маркера
    пункта и не содержит собственного сказуемого; предыдущий блок при этом
    оканчивается НЕ на терминатор предложения."""
    _FRAG_START_RE = re.compile(r'^(=|\d{1,2}\s*[+\-−)]|1/2|−|[)\]])')
    _TERM_RE = re.compile(r'[.!?;:]\s*$')
    result: List[Dict[str, Any]] = []
    for b in blocks:
        if not result:
            result.append(b)
            continue
        bt = b.get('type')
        content = (b.get('content') or '').strip()
        if not content:
            result.append(b)
            continue
        prev = result[-1]
        prev_text = (prev.get('content') or '').rstrip()
        prev_term = _TERM_RE.search(prev_text)
        if (bt in ('paragraph', 'formula', 'list')
                and prev.get('type') in ('paragraph', 'formula', 'list')
                and prev.get('page number') == b.get('page number')
                and not prev_term
                and _FRAG_START_RE.match(content)):
            if prev.get('type') == 'list' and bt == 'formula':
                pass
            prev['content'] = prev_text + ' ' + content
            continue
        result.append(b)
    return result


def _split_list_before_mark(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """R3: режет список перед элементом с маркером пункта, если предыдущий
    элемент без маркера и завершён («Стадия затопления — … равновесия.»
    → «2.5.1.1 Фактор s …»). Обобщается: немаркированный вводный элемент
    списка не может предшествовать новому пункту."""
    _TERM_RE = re.compile(r'[.;:]\s*$')
    result: List[Dict[str, Any]] = []
    for b in blocks:
        if b.get('type') != 'list':
            result.append(b)
            continue
        items = b.get('items', [])
        texts = [it.get('content', '') if isinstance(it, dict) else str(it)
                 for it in items]
        cuts = [i for i in range(1, len(texts))
                if _MARK_RE.match(texts[i])
                and not (_MARK_RE.match(texts[i - 1]) or _SUBMARK_RE.match(texts[i - 1]))
                and _TERM_RE.search(texts[i - 1].rstrip())]
        if not cuts:
            result.append(b)
            continue
        start = 0
        for c in cuts:
            result.append({**b, 'items': items[start:c],
                           'content': '\n'.join(texts[start:c])})
            start = c
        result.append({**b, 'items': items[start:],
                       'content': '\n'.join(texts[start:])})
    return result


def _split_zero_if(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """R4: «…°. = 0, если …» → отдельный параграф. Обобщается: условие
    обнуления фактора после завершённого предложения начинается с «= 0, если»."""
    _ZERO_IF_RE = re.compile(r'(?<=\.)\s+(?==\s*0,\s*если\b)')
    result: List[Dict[str, Any]] = []
    for b in blocks:
        if b.get('type') != 'paragraph':
            result.append(b)
            continue
        content = (b.get('content') or '')
        m = _ZERO_IF_RE.search(content)
        if not m:
            result.append(b)
            continue
        left = content[:m.start()].strip()
        right = content[m.end():].strip()
        if left:
            result.append({**b, 'content': left})
        if right:
            result.append({**b, 'content': right})
    return result


def _merge_adjacent_lists(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Склеивает соседние списки одной страницы, когда следующий начинается
    с маркера пункта («4.1.2 ...» → «4.1.3 ...») или подпункта («.7 ...» → «1.4.7 ...»),
    а предыдущий оканчивается на «.;:». Docling режет такие последовательности
    на отдельные <ul>, эталон держит одним; списки с незавершённым последним
    пунктом («...точкой ил») НЕ склеиваются."""
    _END_RE = re.compile(r'[.;:]\s*$')
    result: List[Dict[str, Any]] = []
    for b in blocks:
        if result and b.get('type') == 'list':
            prev = result[-1]
            items = b.get('items', [])
            first = items[0].get('content', '') if items and isinstance(items[0], dict) else (str(items[0]) if items else '')
            prev_text = prev.get('content') or ''
            if (prev.get('type') == 'list'
                    and prev.get('page number') == b.get('page number')
                    and _END_RE.search(prev_text.rstrip())
                    and (_MARK_RE.match(first) or _SUBMARK_RE.match(first))):
                prev_items = prev.get('items', [])
                prev_items.extend(items)
                prev['content'] = '\n'.join(
                    it.get('content', '') if isinstance(it, dict) else str(it)
                    for it in prev_items)
                continue
        result.append(b)
    return result


def _norm_for_key(b: Dict[str, Any]) -> str:
    """Нормализованный текст блока для дедупликации (как в compare.py)."""
    bt = b.get('type', '')
    content = b.get('content') or ''
    if bt == 'list':
        parts = []
        for it in b.get('items', []):
            parts.append(it.get('content', '') if isinstance(it, dict) else str(it))
        content = ' '.join(parts)
    elif bt == 'table':
        cells = []
        for row in b.get('rows', []):
            for cell in row.get('cells', []):
                for cb in cell.get('block', []):
                    cells.append(cb.get('content', ''))
        for col in b.get('columns', []):
            if col:
                cells.append(col)
        content = ' '.join(cells)
    return re.sub(r'\s+', ' ', content).strip().lower()


def _dedup_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Убирает точные дубли (тип+страница+текст), оставляя последний."""
    kept: List[Dict[str, Any]] = []
    seen: Dict[Tuple, Dict[str, Any]] = {}
    for b in blocks:
        norm = _norm_for_key(b)
        if not norm:
            kept.append(b)
            continue
        key = (b.get('type'), b.get('page number'), norm)
        if key in seen:
            kept.remove(seen[key])
        kept.append(b)
        seen[key] = b
    return kept


def html_to_document_json(page_htmls: List[Tuple[int, str]],
                           file_name: str = 'document.pdf') -> Dict[str, Any]:
    """
    Конвертирует список (page_number, html_text) в полный JSON документа.

    Args:
        page_htmls: список пар (номер_страницы, HTML-текст)
        file_name: имя файла

    Returns:
        JSON в структуре opendataloader
    """
    # Fallback: "Formula not decoded" → пустой figure.formula
    # (основная замена на реальный текст происходит в cross_parser
    #  по bbox из metadata, если HTML был сгенерирован Docling'ом)
    _ND_RE = re.compile(
        r'<div\s+class="formula-not-decoded">\s*Formula not decoded\s*</div>',
        re.IGNORECASE,
    )
    page_htmls = [
        (pno, _ND_RE.sub('<figure class="formula"></figure>', html))
        for pno, html in page_htmls
    ]

    all_blocks = []
    total_pages = len(page_htmls)
    has_tables = False

    for pno, html_text in page_htmls:
        blocks = html_to_json_blocks(html_text, pno)
        for b in blocks:
            if b.get('type') == 'table':
                has_tables = True
        all_blocks.extend(blocks)

    # Постобработка: группируем caption с изображениями
    all_blocks = _group_captions_with_images(all_blocks)

    # Постобработка Docling-блоков (дефекты A–I):
    # разрядка → склейки параграфов → списки → хвостовые «1» → дубли
    _apply_spaced_letters(all_blocks)
    all_blocks = _split_merged_paragraphs(all_blocks)
    all_blocks = _split_lists(all_blocks)
    all_blocks = _split_list_before_mark(all_blocks)
    all_blocks = _split_formula_where(all_blocks)
    all_blocks = _merge_fragments(all_blocks)
    all_blocks = _split_zero_if(all_blocks)
    _trim_heading_suffix(all_blocks)
    all_blocks = _dedup_blocks(all_blocks)

    # PUA-символы из Docling: замена на стандартные.
    _PUA_MAP = {
        '\uf0d7': '\u00b7',  # → · (middle dot)
        '\uf044': '\u0394',  # → Δ (Greek Delta)
    }
    for b in all_blocks:
        bt = b.get('type', '')
        if bt in ('paragraph', 'formula', 'list', 'heading'):
            content = b.get('content', '')
            if content:
                for old, new in _PUA_MAP.items():
                    content = content.replace(old, new)
                b['content'] = content
            if 'latex' in b:
                latex = b['latex']
                for old, new in _PUA_MAP.items():
                    latex = latex.replace(old, new)
                b['latex'] = latex
        elif bt == 'table':
            for row in b.get('rows', []):
                if not isinstance(row, dict):
                    continue
                for cell in row.get('cells', []):
                    cell_blocks = cell.get('block') or []
                    for cb in cell_blocks:
                        ctext = cb.get('content', '')
                        if ctext:
                            for old, new in _PUA_MAP.items():
                                ctext = ctext.replace(old, new)
                            cb['content'] = ctext

    # Regex fallback: перетипировать параграфы с LaTeX-нотацией или матем. Unicode в формулу.
    _FORMULA_RE = re.compile(
        r'\$\$|\$|\\\(|\\\[|'
        r'[\u0370-\u03FF]|'       # Greek and Coptic
        r'[\u2200-\u22FF]|'       # Mathematical Operators
        r'[\u2100-\u214F]|'       # Letterlike Symbols
        r'[\U0001D400-\U0001D7FF]' # Mathematical Alphanumeric Symbols
    )
    # Символы, которые точно не могут быть в формуле (кириллица, CJK, арабское, иврит, эмодзи).
    _FORMULA_FORBIDDEN_RE = re.compile(
        r'[а-яА-ЯёЁ]'  # Кириллица — основной источник ошибочных формул
        r'|[\u0600-\u06FF]'  # Арабское письмо
        r'|[\u0590-\u05FF]'  # Иврит
        r'|[\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF]'  # CJK
        r'|[\uAC00-\uD7AF]'  # Корейское
        r'|[\uFE00-\uFE0F]'  # Variation selectors (эмодзи)
    )
    # Строка из ≥4 кириллических слов (токенов) — признак предложения, а не формулы;
    # «если …» — условие (эталон держит условия параграфами); отдельные обозначения
    # внутри формулы («…или s кон.𝑖…», «…( Протяженность /7)…») допустимы.
    _CYR_TOKEN_RE = re.compile(r'(?:^|\s)([а-яёА-ЯЁ]+)(?=\s|$|[,.;:])')
    _CYR_START_RE = re.compile(r'^[а-яёА-ЯЁ«]')
    for b in all_blocks:
        if b.get('type') not in ('paragraph', 'formula'):
            continue
        content = b.get('content', '')
        has_math = _FORMULA_RE.search(content) if content else False
        if has_math:
            b['type'] = 'formula'
        # Фильтр: сбрасываем формулу, если это предложение — начинается с кириллицы,
        # содержит ≥4 кириллических слова или условие «если …» (демоут не трогает
        # формулы, выделенные из «формула, где …»)
        if b.get('type') == 'formula' and content and not b.get('_where_formula'):
            cyr_tokens = _CYR_TOKEN_RE.findall(content)
            if _CYR_START_RE.match(content) or len(cyr_tokens) >= 4 or 'если' in content:
                b['type'] = 'paragraph'
        b.pop('_where_formula', None)

    # Финальный фильтр: контент только из цифр/разделителей (без букв, без точки — коды классификации не трогать)
    all_blocks = [
        b for b in all_blocks
        if not re.match(r'^[\d\s\-—\/]+$', (b.get('content') or '').strip())
    ]

    document = {
        'source': {
            'file_name': file_name,
            'file_hash_sha256': '',
            'page_count': total_pages,
        },
        'pages': [{'page': i + 1, 'width': 595.0, 'height': 842.0}
                  for i in range(total_pages)],
        'block': all_blocks,
    }

    return {
        'content': {
            'document': document,
            'quality': {
                'confidence': 0.75,
                'pages_processed': total_pages,
                'per_page': [],
            },
            'errors': [],
            'status': 'completed',
            'metadata': {
                'total_pages': total_pages,
                'has_tables': has_tables,
            },
        }
    }
