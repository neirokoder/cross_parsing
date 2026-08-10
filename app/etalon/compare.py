"""
Контроль эталона: сравнение результата кросс-парсинга с эталонным JSON
(формат opendataloader / raw_ocr_v4).

Метрики:
  - precision: доля блоков результата, нашедших пару в эталоне
  - recall:    доля блоков эталона, нашедших пару в результате
  - f1:        гармоническое среднее
  - text_coverage: доля символов эталона, покрытая результатом
По типам блоков и постранично, плюс списки расхождений (missed/extra).
"""
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_SIM_THRESHOLD = 0.6


def _norm_text(text: str) -> str:
    text = text or ''
    text = text.replace('\uf0d7', '\u00b7').replace('\uf044', '\u0394')
    text = re.sub(r'[\s\u00a0]+', ' ', text)
    return text.strip().lower()


def block_text(block: Dict) -> str:
    """Полный текст блока (для таблиц — содержимое всех ячеек)."""
    btype = block.get('type', '')
    if btype == 'table':
        parts = []
        for row in block.get('rows', []):
            for cell in row.get('cells', []):
                for cb in cell.get('block', []):
                    t = cb.get('content', '')
                    if t:
                        parts.append(t)
        return ' | '.join(parts)
    if btype == 'list':
        items = block.get('items', [])
        return ' '.join(
            item.get('content', '') if isinstance(item, dict) else str(item)
            for item in items
        )
    return block.get('content', '') or ''


def _extract_blocks(data: Dict) -> List[Dict]:
    """Извлекает список блоков из JSON любой поддерживаемой структуры."""
    if isinstance(data, dict):
        if 'block' in data:
            return data['block']
        if 'kids' in data:
            return data['kids']
        if 'content' in data and isinstance(data['content'], dict):
            inner = data['content']
            if 'block' in inner:
                return inner['block']
            if 'document' in inner and isinstance(inner['document'], dict):
                if 'block' in inner['document']:
                    return inner['document']['block']
                if 'kids' in inner['document']:
                    return inner['document']['kids']
    return []


def load_blocks(path: str) -> List[Dict]:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return _extract_blocks(data)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm_text(a), _norm_text(b)).ratio()


def _greedy_match(
    etalon: List[Dict],
    result: List[Dict],
    threshold: float,
) -> Tuple[List[Optional[int]], List[bool], List[bool], List[Optional[str]]]:
    """
    Жадное сопоставление блоков эталона и результата.

    Returns:
        (matches_for_etalon, used_result, type_ok)
        matches_for_etalon[i] = индекс блока результата или None
        type_ok[i] = совпал ли тип
    """
    matches: List[Optional[int]] = [None] * len(etalon)
    used: List[bool] = [False] * len(result)
    type_ok: List[bool] = [False] * len(etalon)

    # Сначала — точное совпадение текста на той же странице (лучшие кандидаты)
    for i, eb in enumerate(etalon):
        ep = eb.get('page number', 1)
        et = _norm_text(block_text(eb))
        if not et:
            # Без текста (image/formula с пустым content) — матч по image_key
            if eb.get('type') == 'image' and eb.get('image_key'):
                for j, rb in enumerate(result):
                    if not used[j] and rb.get('type') == 'image' \
                            and rb.get('page number', 1) == ep \
                            and rb.get('image_key') == eb.get('image_key'):
                        matches[i] = j
                        used[j] = True
                        type_ok[i] = True
                        break
            continue
        best_j, best_sim = None, 0.0
        for j, rb in enumerate(result):
            if used[j] or rb.get('page number', 1) != ep:
                continue
            rt = _norm_text(block_text(rb))
            if not rt:
                continue
            if rt == et:
                best_j, best_sim = j, 1.0
                break
            sim = _similarity(et, rt)
            if sim > best_sim:
                best_j, best_sim = j, sim
        if best_j is not None and best_sim >= threshold:
            matches[i] = best_j
            used[best_j] = True
            type_ok[i] = eb.get('type') == result[best_j].get('type')

    return matches, used, type_ok


def compare(
    etalon_path: str,
    result_path: str,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
) -> Dict:
    """
    Сравнивает результат кросс-парсинга с эталоном.

    Args:
        etalon_path: путь к эталонному JSON
        result_path: путь к JSON результата (или сам словарь)
        sim_threshold: порог сходства текста для пары блоков

    Returns:
        Словарь отчёта: summary, by_type, by_page, missed, extra
    """
    if isinstance(result_path, dict):
        result_blocks = _extract_blocks(result_path)
    else:
        result_blocks = load_blocks(result_path)
    etalon_blocks = load_blocks(etalon_path)

    matches, used, type_ok = _greedy_match(etalon_blocks, result_blocks, sim_threshold)

    matched = sum(1 for m in matches if m is not None)
    etalon_n = len(etalon_blocks)
    result_n = len(result_blocks)

    precision = matched / result_n if result_n else 0.0
    recall = matched / etalon_n if etalon_n else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    # Text coverage: доля символов эталона, покрытая результатом
    etalon_chars = sum(len(_norm_text(block_text(b))) for b in etalon_blocks)
    matched_text = 0
    for i, m in enumerate(matches):
        if m is not None:
            et = _norm_text(block_text(etalon_blocks[i]))
            rt = _norm_text(block_text(result_blocks[m]))
            n = min(len(et), len(rt))
            common = sum(1 for a, b in zip(et, rt) if a == b) if n else 0
            matched_text += common
    text_coverage = matched_text / etalon_chars if etalon_chars else 0.0

    # ---- by_type ----
    types = set(b.get('type', 'unknown') for b in etalon_blocks) | \
            set(b.get('type', 'unknown') for b in result_blocks)
    by_type: Dict[str, Dict] = {}
    for t in sorted(types):
        e_idx = [i for i, b in enumerate(etalon_blocks) if b.get('type', 'unknown') == t]
        r_idx = [j for j, b in enumerate(result_blocks) if b.get('type', 'unknown') == t]
        m = sum(1 for i in e_idx if matches[i] is not None)
        p = m / len(r_idx) if r_idx else 0.0
        r = m / len(e_idx) if e_idx else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        by_type[t] = {
            'etalon': len(e_idx),
            'result': len(r_idx),
            'matched': m,
            'precision': round(p, 3),
            'recall': round(r, 3),
            'f1': round(f, 3),
        }

    # ---- by_page ----
    pages = set(b.get('page number', 1) for b in etalon_blocks) | \
            set(b.get('page number', 1) for b in result_blocks)
    by_page: Dict[int, Dict] = {}
    for p in sorted(pages):
        e_idx = [i for i, b in enumerate(etalon_blocks) if b.get('page number', 1) == p]
        r_idx = [j for j, b in enumerate(result_blocks) if b.get('page number', 1) == p]
        m = sum(1 for i in e_idx if matches[i] is not None)
        pv = m / len(r_idx) if r_idx else 0.0
        rv = m / len(e_idx) if e_idx else 0.0
        by_page[p] = {
            'etalon': len(e_idx),
            'result': len(r_idx),
            'matched': m,
            'precision': round(pv, 3),
            'recall': round(rv, 3),
        }

    # ---- расхождения ----
    missed = []
    for i, b in enumerate(etalon_blocks):
        if matches[i] is None:
            missed.append({
                'page': b.get('page number', 1),
                'type': b.get('type', 'unknown'),
                'content': block_text(b)[:300],
            })
    extra = []
    for j, b in enumerate(result_blocks):
        if not used[j]:
            extra.append({
                'page': b.get('page number', 1),
                'type': b.get('type', 'unknown'),
                'content': block_text(b)[:300],
            })
    type_mismatch = sum(1 for i, b in enumerate(etalon_blocks)
                        if matches[i] is not None and not type_ok[i])

    report = {
        'summary': {
            'etalon_blocks': etalon_n,
            'result_blocks': result_n,
            'matched': matched,
            'precision': round(precision, 3),
            'recall': round(recall, 3),
            'f1': round(f1, 3),
            'text_coverage': round(text_coverage, 3),
            'type_mismatch': type_mismatch,
            'sim_threshold': sim_threshold,
        },
        'by_type': by_type,
        'by_page': by_page,
        'missed': missed,
        'extra': extra,
    }
    return report
