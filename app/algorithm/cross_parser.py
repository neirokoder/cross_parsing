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
from app.algorithm.json_to_html import json_to_html
from app.algorithm.quality_metrics import compute_page_quality
from app.config import HTML_DIR, IMAGES_DIR, OUTPUT_DIR

logger = logging.getLogger(__name__)

_COLONTITUL_RE = re.compile(r'правила классификации и постройки морских судов')
_FRAG_MARK_RE = re.compile(r'^\d{1,2}\.\d{1,2}(?:\.\d{1,2})+\s')
_WORD_SPLIT_RE = re.compile(r'[\s,;:.()«»\-]+')
_PUNCT_STRIP_RE = re.compile(r'[),;:.]+$')


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

    # 4.1a: «где ...» внутри формул Docling → paragraph (пояснение, а не формула)
    _WHERE_RE = re.compile(r'^где\s')
    for b in blocks:
        if (b.get('type') == 'formula'
                and _WHERE_RE.match((b.get('content') or '').strip())):
            b['type'] = 'paragraph'

    # 4.7: Docling режет пояснение «где Np — максимальное число …» на три блока:
    #      <p>где</p> + <p>Np</p> (обозначение стало формулой) + <p>— описание…</p>.
    #      Собираем: «где» + формула-одиночка (без «=», без кириллицы и пробелов)
    #      + параграф, начинающийся с «—-», на той же странице → один параграф.
    _WHERE_SOLE = re.compile(r'^[—-]')
    merged_where: List[Dict] = []
    j = 0
    while j < len(blocks):
        b = blocks[j]
        n1 = blocks[j + 1] if j + 1 < len(blocks) else None
        n2 = blocks[j + 2] if j + 2 < len(blocks) else None
        if (b.get('type') == 'paragraph'
                and (b.get('content') or '').strip() == 'где'
                and n1 and n1.get('type') == 'formula'
                and not n1.get('image_key')
                and n1.get('page number') == b.get('page number')
                and n2 and n2.get('type') == 'paragraph'
                and n2.get('page number') == b.get('page number')):
            ftext = (n1.get('content') or '').strip()
            if (ftext and ' ' not in ftext and '=' not in ftext
                    and len(ftext) <= 20
                    and not re.search(r'[а-яёА-ЯЁ]', ftext)
                    and _WHERE_SOLE.match(n2.get('content') or '')):
                b['content'] = 'где {} {}'.format(ftext,
                                                   (n2.get('content') or '').strip())
                j += 3
                merged_where.append(b)
                continue
        merged_where.append(b)
        j += 1
    blocks = merged_where

    # 4.1c: пустые списки — Docling-артефакты без текста и пунктов
    blocks = [b for b in blocks
              if not (b.get('type') == 'list'
                      and not ((b.get('content') or '').strip())
                      and not b.get('items'))]

    # 4.1b: склейка фрагментов формул, добавленных PyMuPDF-добором:
    #       enrich-фрагмент без завершающего «.;:» + следующая enrich-формула,
    #       начинающаяся с оператора/цифры (продолжение строки) — одна формула.
    #       Docling-блоки НЕ склеиваются: эталон повторяет их разметку.
    _TERM_END_RE = re.compile(r'[.;:]\s*$')
    _CONT_START_RE = re.compile(r'^[0-9+\-−=×()⁄,/]')
    page_formulas: Dict[int, List[Dict]] = {}
    for b in blocks:
        page_formulas.setdefault(b.get('page number', 1), []).append(b)
    merged_fragments: List[Dict] = []
    for pno, pbs in page_formulas.items():
        cur = None
        for b in pbs:
            if b.get('type') != 'formula' or (b.get('image_key') or ''):
                cur = None
                merged_fragments.append(b)
                continue
            text = (b.get('content') or '').strip()
            prev_text = (cur.get('content') or '').rstrip() if cur else ''
            if (cur is not None and cur.get('_enriched') and b.get('_enriched')
                    and prev_text
                    and not _TERM_END_RE.search(prev_text)
                    and _CONT_START_RE.match(text)):
                cur['content'] = prev_text + ' ' + text
                pb = cur.get('bounding box')
                cb = b.get('bounding box')
                if pb and cb and pb != [0, 0, 0, 0] and cb != [0, 0, 0, 0]:
                    cur['bounding box'] = [min(pb[0], cb[0]), min(pb[1], cb[1]),
                                           max(pb[2], cb[2]), max(pb[3], cb[3])]
            else:
                merged_fragments.append(b)
                cur = b
    blocks = merged_fragments

    # 4.3: пустые формулы (без текста и без картинки)
    blocks = [b for b in blocks
              if not (b.get('type') == 'formula'
                      and not (b.get('content') or '').strip()
                      and not (b.get('latex') or '').strip()
                      and not (b.get('image_key') or ''))]

    # 4.5: многострочные формулы — слияние по геометрии. Блоки формул одной
    #      страницы, чьи bbox перекрываются по вертикали и горизонтали (строки
    #      одной визуальной формулы: Docling-голова + enrich-фрагменты),
    #      сливаются в первый по порядку блок (порядок фрагментов = порядок
    #      строк PDF, эталон хранит такие формулы одним блоком).
    def _geo_overlap(a: List[float], b: List[float]) -> bool:
        # Вертикальное пересечение: строки одной визуальной формулы лежат в
        # одной горизонтальной полосе (продолжения смещены вправо, поэтому
        # по x они могут не пересекаться вовсе).
        return min(a[3], b[3]) - max(a[1], b[1]) > 0.5

    merged_formulas: List[Dict] = []
    skip_ids: set = set()
    for i, b in enumerate(blocks):
        if i in skip_ids:
            continue
        if b.get('type') == 'formula':
            bb = b.get('bounding box') or []
            if bb and any(abs(v) > 0.001 for v in bb):
                pno = b.get('page number', 1)
                group = [b]
                for j in range(i + 1, len(blocks)):
                    if j in skip_ids:
                        continue
                    o = blocks[j]
                    obb = o.get('bounding box') or []
                    if (o.get('type') == 'formula'
                            and o.get('page number') == pno
                            and obb and any(abs(v) > 0.001 for v in obb)
                            and _geo_overlap(bb, obb)):
                        group.append(o)
                        skip_ids.add(j)
                if len(group) > 1:
                    parts = [g.get('content') or '' for g in group]
                    b['content'] = ' '.join(p.strip() for p in parts if p.strip())
                    b['bounding box'] = [min(g['bounding box'][0] for g in group),
                                         min(g['bounding box'][1] for g in group),
                                         max(g['bounding box'][2] for g in group),
                                         max(g['bounding box'][3] for g in group)]
        merged_formulas.append(b)
    blocks = merged_formulas

    # 4.6: Docling режет строку формулы пополам — «θ𝑟= θ𝑔» (formula) + «для
    #      ρ ≤1400 кг/м3 (жидкий груз);» (paragraph). Приклеиваем к формуле
    #      короткий параграф-хвост условия, лежащий на той же строке правее
    #      формулы (эталон хранит формулу с условием одним блоком).
    def _is_formula_tail(t: str) -> bool:
        t = t.strip()
        if not t or len(t) >= 70 or not t[0].islower():
            return False
        return t.split(maxsplit=1)[0] in ('для', 'при', 'если')

    merged_tails: List[Dict] = []
    skip_tails: set = set()
    for i, b in enumerate(blocks):
        if i in skip_tails:
            continue
        if b.get('type') == 'formula':
            bb = b.get('bounding box') or []
            if bb and any(abs(v) > 0.001 for v in bb):
                pno = b.get('page number', 1)
                for j in range(len(blocks)):
                    if j == i or j in skip_tails:
                        continue
                    o = blocks[j]
                    obb = o.get('bounding box') or []
                    if (o.get('type') == 'paragraph'
                            and o.get('page number') == pno
                            and obb and any(abs(v) > 0.001 for v in obb)
                            and obb[0] >= bb[0]
                            and _geo_overlap(bb, obb)
                            and _is_formula_tail(o.get('content') or '')):
                        b['content'] = '{} {}'.format(
                            (b.get('content') or '').strip(), (o.get('content') or '').strip())
                        b['bounding box'] = [min(b['bounding box'][0], obb[0]),
                                             min(b['bounding box'][1], obb[1]),
                                             max(b['bounding box'][2], obb[2]),
                                             max(b['bounding box'][3], obb[3])]
                        skip_tails.add(j)
                        break
        merged_tails.append(b)
    blocks = merged_tails

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
                    # «где …» после формулы («…s мом.і ), где sпромеж.і …») —
                    # пояснение условных обозначений, а не продолжение предложения
                    _FORMULA_END_RE = re.compile(r'[)\]]\s*\d?\s*,?\s*$')
                    if body_first.startswith('где ') and _FORMULA_END_RE.search(prev_norm):
                        merged.append(b)
                        continue
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

    # 4.10: Docling переставляет фрагменты разорванной строки «X = 0, если …»:
    #       голова X и хвост фразы свёрнуты в один блок-формулу («sпромеж.𝑖 грузовых
    #       судов.»), середина «= 0, если …» — отдельным параграфом (вырезана
    #       _split_zero_if). Восстанавливаем: формула «X = 0» + параграф «если …».
    _ZERO_EQ_RE = re.compile(r'^=\s*0\s*,')
    rebuilt: List[Dict] = []
    skip_rebuild: set = set()
    for i, b in enumerate(blocks):
        if i in skip_rebuild:
            continue
        if b.get('type') == 'paragraph' and not b.get('_enriched'):
            text = (b.get('content') or '').strip()
            m = _ZERO_EQ_RE.match(text)
            if m:
                rest = text[m.end():].strip()
                if rest.startswith('если '):
                    f = blocks[i + 1] if i + 1 < len(blocks) else None
                    if (f and f.get('type') == 'formula' and not f.get('image_key')
                            and f.get('page number') == b.get('page number')):
                        ftext = (f.get('content') or '').strip()
                        head, sep, tail = ftext.partition(' ')
                        if (sep and tail
                                and re.search(r'[а-яёА-ЯЁ]', tail)
                                and re.search(r'[\u0370-\u03FF\U0001D400-\U0001D7FF0-9]', head)
                                and len(head) <= 20
                                and tail.rstrip().endswith('.')):
                            rebuilt.append({
                                'type': 'formula',
                                'page number': b.get('page number'),
                                'content': '{} = 0'.format(head),
                                'latex': '{} = 0'.format(head),
                                'bounding box': b.get('bounding box') or [0, 0, 0, 0],
                                '_rebuilt': True,
                            })
                            pb = dict(b)
                            pb['content'] = '{} {}'.format(rest, tail)
                            rebuilt.append(pb)
                            skip_rebuild.add(i + 1)
                            continue
        rebuilt.append(b)
    blocks = rebuilt

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

        # (i) префикс-дубль enrich-параграфа: первые N-1 слов фрагмента совпадают
        #     по порядку с началом параграфа-контейнера той же страницы («Суда,
        #     указанные в 1.1.1.7 … настоящего» — Docling унёс слово «настоящего» в
        #     конец предыдущего абзаца, строка не распознана как уже-есть и добавлена
        #     enrich'ом) → фрагмент дублирует начало абзаца, удаляем
        if not dropped and enriched and b.get('type') == 'paragraph':
            if not re.search(r'[.!?…:;]\s*$', norm):
                wb = [w for w in _WORD_SPLIT_RE.split(norm) if len(w) >= 2]
                if len(wb) >= 5:
                    for o in others:
                        if o is b or o.get('type') != 'paragraph':
                            continue
                        wo = [w for w in _WORD_SPLIT_RE.split(_norm_for_key(o)) if len(w) >= 2]
                        if len(wo) > len(wb) and wo[:len(wb) - 1] == wb[:-1]:
                            dropped = True
                            break

        # (e) дубль-формула на странице: норм-тексты различаются лишь
        #     пробелами/завершающей пунктуацией/дефисом («−» vs «-»)
        #     или потерянным символом (≤3) — enrich-копия = Docling-версия.
        #     Нечёткое сравнение по словам (≥0.9) тут опасно: «сиамские»
        #     формулы p20 различаются цифрами индексов.
        if not dropped and b.get('type') == 'formula':
            f_flat = re.sub(r'\s+', '', norm)
            f_core = _PUNCT_STRIP_RE.sub('', f_flat).replace('−', '-')
            for o in others:
                if o is b:
                    break
                if o.get('type') != 'formula':
                    continue
                onorm = _norm_for_key(o)
                if not onorm:
                    continue
                o_flat = re.sub(r'\s+', '', onorm)
                o_core = _PUNCT_STRIP_RE.sub('', o_flat).replace('−', '-')
                if f_core == o_core:
                    dropped = True
                    break
                # Подстрока по НОРМАЛИЗОВАННЫМ текстам («−» из PDF → «-» Docling):
                # «= √(θmax −θ𝑒)/(θmax −θmin)» — хвост «К = √(θmax - θ𝑒 )/(θmax -θmin )»
                long_c, short_c = (o_core, f_core) if len(o_core) > len(f_core) else (f_core, o_core)
                if (len(f_flat) < len(o_flat) and short_c in long_c
                        and len(long_c) - len(short_c) <= 3
                        and len(short_c) >= 0.5 * len(long_c)):
                    dropped = True
                    break

        # (f) фрагмент-формула: норм-текст — подстрока другого блока страницы
        #     (контейнер ≥1.5× длины фрагмента): «4);» внутри
        #     «где 𝐶 = 12𝐽𝑏 (– 45 𝐽𝑏 + 4);», «= √(θmax−θ𝑒)/(θmax−θmin)»
        #     внутри «К = √(...) ...». _rebuilt (4.10) исключаем: восстановленная
        #     формула «sпромеж.𝑖 = 0» — подстрока старого абзаца-дубля, который
        #     удаляется отдельно
        if not dropped and b.get('type') == 'formula' and not b.get('_rebuilt'):
            f_flat = re.sub(r'\s+', '', norm)
            if 3 <= len(f_flat) <= 40:
                for o in others:
                    if o is b:
                        continue
                    onorm = _norm_for_key(o)
                    o_flat = re.sub(r'\s+', '', onorm) if onorm else ''
                    if o_flat and f_flat in o_flat and len(o_flat) >= 1.5 * len(f_flat):
                        dropped = True
                        break

        # (g) префикс-фрагмент: короткая формула, обрывающаяся на «-»/«=»/«,»,
        #     повторяющая начало следующей формулы страницы: «θ𝑟 -» перед
        #     «θ𝑟= θ𝑔 для ρ ≤1400 кг/м3 ...»
        if not dropped and b.get('type') == 'formula':
            f_flat = re.sub(r'\s+', '', norm)
            if len(f_flat) <= 12:
                core = re.sub(r'[-=,;)]+$', '', f_flat)
                if len(core) >= 2 and core != f_flat:
                    for o in others:
                        if o is b or o.get('type') != 'formula':
                            continue
                        onorm = _norm_for_key(o)
                        o_flat = re.sub(r'\s+', '', onorm) if onorm else ''
                        if o_flat and o_flat.startswith(core) and len(o_flat) > len(core):
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

        # Добор bbox из docling_document.json (prov текстов Docling, BOTTOMLEFT)
        matched = pdf_extract.restore_docling_bboxes(
            json_result, str(html_dir / 'docling_document.json'))
        logger.info("Docling bbox: restored %d block positions", matched)

        pdf_images = pdf_extract.save_images_from_pdf(str(pdf_path), str(use_dir))
        pdf_extract.update_image_keys(json_result, str(use_dir))
        pdf_extract.inject_missing_image_blocks(json_result, str(use_dir), pdf_images)
        pdf_extract.extract_formula_images(json_result, str(pdf_path), str(use_dir), bbox_map)
    except Exception:
        if clean_temp and not images_dir:
            shutil.rmtree(tmp_images, ignore_errors=True)
        raise

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

    # ---- Шаг 7: HTML из JSON — сразу после создания JSON ----
    json_to_html(json_result, out_json.with_suffix(".html"))

    if clean_temp and not images_dir:
        shutil.rmtree(tmp_images, ignore_errors=True)

    return json_result
