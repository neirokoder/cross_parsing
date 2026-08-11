"""
Отчёты по контролю эталона: JSON + Markdown.
"""
import json
from pathlib import Path
from typing import Dict


def save_json_report(report: Dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def render_markdown(report: Dict) -> str:
    """Формирует Markdown-отчёт по блокам и метрикам."""
    s = report['summary']
    lines = [
        "# Отчёт контроля эталона",
        "",
        f"- Эталон: **{s['etalon_blocks']}** блоков, Результат: **{s['result_blocks']}** блоков, Совпало: **{s['matched']}**",
        f"- Precision: **{s['precision']}** | Recall: **{s['recall']}** | F1: **{s['f1']}**",
        f"- Text coverage: **{s['text_coverage']}** | Несовпадений типа: **{s['type_mismatch']}** | Расхождений структуры: **{s.get('structure_mismatch', 0)}** (порог сходства: {s['sim_threshold']})",
        "",
        "## По типам блоков",
        "",
        "| Тип | Эталон | Результат | Совпало | Precision | Recall | F1 |",
        "|-----|-------:|----------:|--------:|----------:|-------:|---:|",
    ]
    for t, v in report['by_type'].items():
        lines.append(f"| {t} | {v['etalon']} | {v['result']} | {v['matched']} | {v['precision']} | {v['recall']} | {v['f1']} |")

    lines += [
        "",
        "## По страницам",
        "",
        "| Стр. | Эталон | Результат | Совпало | Precision | Recall |",
        "|-----:|-------:|----------:|--------:|----------:|-------:|",
    ]
    for p, v in report['by_page'].items():
        lines.append(f"| {p} | {v['etalon']} | {v['result']} | {v['matched']} | {v['precision']} | {v['recall']} |")

    lines += ["", "## Расхождения"]

    lines += ["", "### Missed (есть в эталоне, нет в результате)", ""]
    if report['missed']:
        for b in report['missed'][:50]:
            lines.append(f"- стр.{b['page']} [{b['type']}]: {b['content'][:150]}")
        if len(report['missed']) > 50:
            lines.append(f"- ... и ещё {len(report['missed']) - 50}")
    else:
        lines.append("_нет_")

    lines += ["", "### Extra (есть в результате, нет в эталоне)", ""]
    if report['extra']:
        for b in report['extra'][:50]:
            lines.append(f"- стр.{b['page']} [{b['type']}]: {b['content'][:150]}")
        if len(report['extra']) > 50:
            lines.append(f"- ... и ещё {len(report['extra']) - 50}")
    else:
        lines.append("_нет_")

    return "\n".join(lines)


def save_markdown_report(report: Dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8")
