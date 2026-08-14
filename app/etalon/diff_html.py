"""
Отдельный HTML-файл, показывающий ТОЛЬКО расхождения между результатом
кросс-парсинга и эталоном:

  - missed:  блоки эталона, не найденные в результате (красный)
  - extra:   блоки результата, которых нет в эталоне (жёлтый)
  - structure_mismatches: совпавшие пары с разной структурой JSON

Источник данных — отчёт compare() (report['etalon_status'],
report['result_status'], report['structure_mismatches']).
"""
import html
from pathlib import Path
from typing import Dict, List, Optional

from app.algorithm.json_to_html import _block_html

_IMG_PLACEHOLDER_DIR = Path("__no_images__")


def _blocks_of(data: Dict) -> List[Dict]:
    return ((data.get("content") or {}).get("document") or {}).get("block") or []


def _page_items(blocks: List[Dict], indices: List[int]) -> Dict[int, List[Dict]]:
    """Индексы блоков → {номер страницы: [блок, ...]} с сохранением порядка."""
    by_page: Dict[int, List[Dict]] = {}
    for i in indices:
        b = blocks[i]
        by_page.setdefault(b.get("page number", 1), []).append(b)
    return by_page


def render_diff_html(
    report: Dict,
    etalon_data: Dict,
    result_data: Dict,
    out_path: Path,
) -> Path:
    """Собирает HTML только с расхождениями и сохраняет его.

    Args:
        report: отчёт compare()
        etalon_data: эталонный JSON (raw_ocr_v4)
        result_data: JSON результата (raw_ocr_v4)
        out_path: путь к файлу HTML

    Returns:
        путь к сохранённому HTML
    """
    etalon_blocks = _blocks_of(etalon_data)
    result_blocks = _blocks_of(result_data)

    e_status = report.get("etalon_status") or []
    r_status = report.get("result_status") or []
    missed_idx = [i for i, s in enumerate(e_status) if s == "missed"]
    extra_idx = [j for j, s in enumerate(r_status) if s == "extra"]

    missed_pages = _page_items(etalon_blocks, missed_idx)
    extra_pages = _page_items(result_blocks, extra_idx)

    struct_pages = sorted({s.get("page", 1) for s in report.get("structure_mismatches") or []})
    pages = sorted(set(missed_pages) | set(extra_pages) | set(struct_pages))

    summary = report["summary"]
    n_missed = len(missed_idx)
    n_extra = len(extra_idx)
    n_struct = len(report.get("structure_mismatches") or [])

    esc = html.escape
    sections: List[str] = []
    for pno in pages:
        items: List[str] = []
        for b in missed_pages.get(pno, []):
            items.append(
                '<div class="diff-item missed"><span class="badge">не найдено в результате'
                " ({})</span>{}</div>".format(b.get("type", "?"), _block_html(b, _IMG_PLACEHOLDER_DIR)))
        for b in extra_pages.get(pno, []):
            items.append(
                '<div class="diff-item extra"><span class="badge">лишнее, нет в эталоне'
                " ({})</span>{}</div>".format(b.get("type", "?"), _block_html(b, _IMG_PLACEHOLDER_DIR)))
        sections.append('<section class="page" data-page="{}"><h2>Стр. {}</h2>{}</section>'.format(
            pno, pno, "".join(items)))

    struct_items = []
    for s in report.get("structure_mismatches") or []:
        struct_items.append(
            '<div class="struct-item">стр. {} [{}]: эталон <code>{}</code>'
            ' vs результат <code>{}</code></div>'.format(
                s.get("page", 1), s.get("type", "?"),
                esc(str(s.get("etalon"))), esc(str(s.get("result")))))
    if struct_items:
        sections.append(
            '<section class="struct"><h2>Расхождения структуры JSON ({})</h2>{}</section>'.format(
                n_struct, "".join(struct_items)))

    doc_html = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Расхождения: {stem}</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; margin: 0; background: #f4f4f4; }}
  header.doc {{ padding: 12px 24px; background: #fff; border-bottom: 2px solid #666; }}
  header.doc h1 {{ font-size: 18px; margin: 0; }}
  div.summary {{ margin-top: 6px; font-size: 14px; }}
  div.legend {{ margin-top: 4px; font-size: 13px; }}
  section.page, section.struct {{ max-width: 800px; margin: 16px auto; padding: 16px 24px;
                  background: #fff; border: 1px solid #ccc; page-break-after: always; }}
  section h2 {{ font-size: 15px; margin: 4px 0 10px; color: #444;
                border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  div.diff-item {{ margin: 10px 0; padding: 8px 10px; }}
  div.diff-item.missed {{ background: #fde4e4; border-left: 4px solid #c22; }}
  div.diff-item.extra {{ background: #fff3cf; border-left: 4px solid #c90; }}
  span.badge {{ display: inline-block; font-size: 12px; margin-bottom: 4px;
                padding: 1px 6px; border-radius: 3px; background: rgba(0,0,0,.08); }}
  div.diff-item p {{ margin: 6px 0; line-height: 1.4; }}
  div.diff-item ul, div.diff-item ol {{ margin: 6px 0; }}
  div.diff-item li {{ line-height: 1.35; }}
  div.diff-item div.formula {{ margin: 6px 0; padding: 4px 8px; background: #faf6ec;
                border-left: 3px solid #b8860b; font-style: italic; }}
  div.diff-item table {{ border-collapse: collapse; margin: 6px 0; font-size: 0.9em; }}
  div.diff-item th, div.diff-item td {{ border: 1px solid #888; padding: 3px 5px; }}
  div.diff-item th {{ background: #ececec; }}
  div.diff-item h1,h2,h3,h4,h5,h6 {{ margin: 6px 0; }}
  div.struct-item {{ margin: 6px 0; padding: 6px 10px; background: #eef3fb;
                     border-left: 4px solid #36a; }}
  code {{ background: #f2f2f2; padding: 1px 4px; }}
</style>
</head>
<body>
<header class="doc">
<h1>Расхождения: результат vs эталон ({stem})</h1>
<div class="summary">Совпало {matched} из {result_blocks} (F1={f1}); не найдено в результате: {missed},
лишних: {extra}, расхождений структуры: {struct}</div>
<div class="legend"><span style="color:#c22">■</span> не найдено в результате
&nbsp;&nbsp;<span style="color:#c90">■</span> лишнее, нет в эталоне</div>
</header>
{sections}
</body>
</html>""".format(
        stem=esc(Path(out_path).stem.replace("report_", "").replace("_diff", "")),
        matched=summary["matched"],
        result_blocks=summary["result_blocks"],
        f1=summary["f1"],
        missed=n_missed,
        extra=n_extra,
        struct=n_struct,
        sections="\n".join(sections),
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc_html, encoding="utf-8")
    return out_path
