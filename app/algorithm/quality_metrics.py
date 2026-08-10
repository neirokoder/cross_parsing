# -*- coding: utf-8 -*-
"""
Модуль оценки качества распознавания страниц PDF.
Содержит все эвристики и вычисление скора страницы.
Перенесено из parser_service (app/services/parsers/docling/quality_metrics.py).
"""
import os
import re
import json
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Конфигурация метрик (можно менять без изменения кода)
# -------------------------------------------------------------------
QUALITY_THRESHOLD = 0.50

# Веса для итоговой оценки (сумма = 1.0)
WEIGHTS = {
    "text_length": 0.20,       # нормализовано до 500 символов
    "block_count": 0.15,       # нормализовано до 20 блоков
    "structural_score": 0.20,
    "text_coherence": 0.25,
    "complexity_bonus": 0.10,  # за таблицы/формулы
}


# -------------------------------------------------------------------
# Структура отчёта
# -------------------------------------------------------------------
@dataclass
class PageQualityReport:
    page_num: int
    text_length: int
    block_count: int
    chars_per_block: float
    bbox_ratio: float
    has_blocks: bool
    avg_block_depth: float
    structural_score: float
    alnum_ratio: float
    whitespace_ratio: float
    unique_words_ratio: float
    text_coherence_score: float
    has_tables: bool
    has_formulas: bool
    overall_score: float
    is_problematic: bool
    average_score: float = 0.0  # дублирует overall_score

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -------------------------------------------------------------------
# Эвристики для таблиц и формул
# -------------------------------------------------------------------
def detect_tables(text: str) -> bool:
    lines = text.split('\n')
    if len(lines) < 2:
        return False
    delimiter_pattern = r'[|\t]{2,}| {2,}'
    delimited_lines = sum(1 for line in lines if re.search(delimiter_pattern, line))
    return delimited_lines >= 2


def detect_formulas(text: str) -> bool:
    patterns = [
        r'\$[^\$]+\$',
        r'\\[a-zA-Z]+',
        r'[=+\-*/^]',
        r'[0-9]+[ ]*[xX*][ ]*[0-9]+',
    ]
    for pattern in patterns:
        if re.search(pattern, text):
            return True
    return False


# -------------------------------------------------------------------
# Вычисление метрик для одной страницы
# -------------------------------------------------------------------
def compute_page_quality(page_obj: Dict[str, Any]) -> PageQualityReport:
    """Вычисляет все метрики для одной страницы."""
    page_num = page_obj.get("page") or page_obj.get("page_num") or page_obj.get("number", 0)
    blocks = page_obj.get("blocks") or page_obj.get("elements") or page_obj.get("content") or page_obj.get("items") or []

    if not blocks:
        return PageQualityReport(
            page_num=page_num, text_length=0, block_count=0, chars_per_block=0.0,
            bbox_ratio=0.0, has_blocks=False, avg_block_depth=0.0, structural_score=0.0,
            alnum_ratio=0.0, whitespace_ratio=0.0, unique_words_ratio=0.0,
            text_coherence_score=0.0, has_tables=False, has_formulas=False,
            overall_score=0.0, is_problematic=True, average_score=0.0
        )

    all_text = ""
    bbox_count = 0
    depths = []
    word_set = set()
    total_words = 0

    for block in blocks:
        text = str(block.get("text") or block.get("content") or "")
        all_text += text
        if "bounding box" in block or "bbox" in block or "bounding_box" in block:
            bbox_count += 1
        depth = block.get("level") or block.get("depth") or 0
        if depth is not None:
            try:
                depths.append(float(depth))
            except (ValueError, TypeError):
                pass
        words = re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9]+', text)
        total_words += len(words)
        word_set.update(w.lower() for w in words)

    text_length = len(all_text.strip())
    block_count = len(blocks)
    chars_per_block = text_length / block_count if block_count > 0 else 0.0
    bbox_ratio = bbox_count / block_count if block_count > 0 else 0.0
    has_blocks = block_count > 0
    avg_block_depth = sum(depths) / len(depths) if depths else 0.0
    depth_score = min(avg_block_depth / 3.0, 1.0) if avg_block_depth > 0 else 0.5
    structural_score = 0.5 * bbox_ratio + 0.3 * (1.0 if has_blocks else 0.0) + 0.2 * depth_score

    if text_length > 0:
        alnum_chars = len(re.findall(r'[a-zA-Zа-яА-ЯёЁ0-9]', all_text))
        whitespace_chars = len(re.findall(r'\s', all_text))
        alnum_ratio = alnum_chars / text_length
        whitespace_ratio = whitespace_chars / text_length
        unique_words_ratio = len(word_set) / max(total_words, 1)
    else:
        alnum_ratio = 0.0
        whitespace_ratio = 0.0
        unique_words_ratio = 0.0

    text_coherence_score = 0.5 * alnum_ratio + 0.3 * unique_words_ratio + 0.2 * (1.0 - abs(whitespace_ratio - 0.15))
    text_coherence_score = max(0.0, min(1.0, text_coherence_score))

    has_tables = detect_tables(all_text)
    has_formulas = detect_formulas(all_text)

    # Композитная оценка с весами
    overall_score = (
        WEIGHTS["text_length"] * min(text_length / 500, 1.0) +
        WEIGHTS["block_count"] * min(block_count / 20, 1.0) +
        WEIGHTS["structural_score"] * structural_score +
        WEIGHTS["text_coherence"] * text_coherence_score +
        WEIGHTS["complexity_bonus"] * (1.0 if has_tables or has_formulas else 0.0)
    )
    overall_score = max(0.0, min(1.0, overall_score))
    is_problematic = overall_score < QUALITY_THRESHOLD

    return PageQualityReport(
        page_num=page_num,
        text_length=text_length,
        block_count=block_count,
        chars_per_block=round(chars_per_block, 2),
        bbox_ratio=round(bbox_ratio, 3),
        has_blocks=has_blocks,
        avg_block_depth=round(avg_block_depth, 2),
        structural_score=round(structural_score, 3),
        alnum_ratio=round(alnum_ratio, 3),
        whitespace_ratio=round(whitespace_ratio, 3),
        unique_words_ratio=round(unique_words_ratio, 3),
        text_coherence_score=round(text_coherence_score, 3),
        has_tables=has_tables,
        has_formulas=has_formulas,
        overall_score=round(overall_score, 3),
        is_problematic=is_problematic,
        average_score=round(overall_score, 3)
    )


# -------------------------------------------------------------------
# Основная функция оценки качества по JSON-файлу
# -------------------------------------------------------------------
def assess_quality_from_json(json_path: str) -> Dict[int, PageQualityReport]:
    """
    Анализирует JSON-вывод opendataloader и возвращает словарь {page_num: PageQualityReport}.
    Поддерживает структуры с ключом 'kids' (плоский список блоков) или список страниц.
    """
    if not os.path.exists(json_path):
        logger.warning(f"JSON файл не найден: {json_path}")
        return {}

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"Ошибка чтения JSON: {e}")
        return {}

    pages_data = []

    if isinstance(data, dict):
        if "kids" in data and isinstance(data["kids"], list):
            raw_blocks = data["kids"]
            pages_dict = {}
            for block in raw_blocks:
                page_num = block.get("page number") or block.get("page_num") or block.get("page")
                if page_num is None:
                    continue
                if not isinstance(page_num, int):
                    try:
                        page_num = int(page_num)
                    except (ValueError, TypeError):
                        continue
                pages_dict.setdefault(page_num, []).append(block)
            for pnum in sorted(pages_dict.keys()):
                pages_data.append({"page": pnum, "blocks": pages_dict[pnum]})
            logger.info(f"Найдены блоки в 'kids', сгруппировано {len(pages_data)} страниц")
        else:
            for key in ["pages", "document", "content", "data"]:
                if key in data:
                    candidate = data[key]
                    if isinstance(candidate, list):
                        pages_data = candidate
                        logger.info(f"Найден список страниц по ключу '{key}'")
                        break
                    elif isinstance(candidate, dict):
                        items = list(candidate.values())
                        if items and isinstance(items[0], dict):
                            pages_data = items
                            logger.info(f"Найден словарь страниц по ключу '{key}'")
                            break
            if not pages_data:
                logger.warning("Не удалось найти страницы по известным ключам")
                return {}
    elif isinstance(data, list):
        if data and isinstance(data[0], dict):
            pages_data = data
            logger.info("Корневой JSON является списком страниц")
        else:
            logger.warning("Корневой список не содержит словарей")
            return {}
    else:
        logger.warning(f"Неподдерживаемый тип JSON: {type(data).__name__}")
        return {}

    if not pages_data:
        logger.warning("Массив страниц пуст")
        return {}

    reports = {}
    for page_obj in pages_data:
        report = compute_page_quality(page_obj)
        reports[report.page_num] = report

    return reports
