"""
JSON (raw_ocr_v4, результат кросс-парсинга) → единый HTML для просмотра.

Каждый блок конвертируется по типу:
  heading → h1..h6, paragraph → p, list → ul/ol+li,
  formula → div.formula, table → table (шапка из columns),
  image → img (файл копируется в {stem}_images/ рядом с HTML).
Страницы выносятся в отдельные <section data-page="N">.
"""
import html
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _cell_text(cell: Dict) -> str:
    """Текст ячейки таблицы (склейка вложенных блоков)."""
    parts = []
    for b in cell.get("block") or []:
        t = (b.get("content") or "").strip()
        if t:
            parts.append(t)
    return " ".join(parts)


def _image_html(b: Dict, images_out_dir: Path) -> str:
    """image-блок → <img>, файл копируется рядом с HTML."""
    copied = None
    src_path = b.get("_temp_path") or ""
    if src_path and Path(src_path).is_file():
        fname = Path(src_path).name
        shutil.copy2(src_path, images_out_dir / fname)
        copied = fname
    if not copied:
        ik = (b.get("image_key") or "").replace("\\", "/")
        if ik.startswith("images/"):
            fname = Path(ik).name
            if (images_out_dir / fname).is_file():
                copied = fname
    if copied:
        return '<img src="{}/{}" alt="">'.format(images_out_dir.name, html.escape(copied))
    return '<span class="missing">[image: {}]</span>'.format(html.escape((b.get("image_key") or "")))


def _table_html(b: Dict) -> str:
    header = ""
    cols = b.get("columns") or []
    if cols:
        header = "<tr>" + "".join("<th>{}</th>".format(html.escape(c or "")) for c in cols) + "</tr>"
    rows = []
    for r in b.get("rows") or []:
        rows.append("<tr>" + "".join("<td>{}</td>".format(html.escape(_cell_text(c)))
                                     for c in r.get("cells") or []) + "</tr>")
    return "<table>{}{}</table>".format(header, "".join(rows))


def _block_html(b: Dict, images_out_dir: Path) -> str:
    """Один блок JSON → HTML-фрагмент."""
    btype = b.get("type")
    esc = html.escape
    text = (b.get("content") or "").strip()

    if btype == "heading":
        level = min(max(int(b.get("heading level", 2) or 2), 1), 6)
        return "<h{0}>{1}</h{0}>".format(level, esc(text))

    if btype == "paragraph":
        return "<p>{}</p>".format(esc(text))

    if btype == "formula":
        latex = (b.get("latex") or "").strip()
        body = esc(text or latex)
        title = ' title="{}"'.format(esc(latex)) if latex else ""
        return '<div class="formula"{0}>{1}</div>'.format(title, body)

    if btype == "list":
        tag = "ol" if b.get("list_type") == "ordered" else "ul"
        items = b.get("items") or []
        if not items:
            items = [{"content": line.strip()} for line in text.split("\n") if line.strip()]
        li = "".join("<li>{}</li>".format(esc((it.get("content") or "").strip())) for it in items)
        return "<{0}>{1}</{0}>".format(tag, li)

    if btype == "table":
        return _table_html(b)

    if btype == "image":
        return '<div class="image-block">{}</div>'.format(_image_html(b, images_out_dir))

    return "<div>{}</div>".format(esc(text))


def json_to_html(json_result: Dict, out_html: Path) -> Path:
    """
    Собирает единый HTML из JSON результата кросс-парсинга и сохраняет его.

    Args:
        json_result: JSON (raw_ocr_v4) с content.document.block[]
        out_html: путь к файлу HTML; картинки копируются в {stem}_images/

    Returns:
        путь к сохранённому HTML
    """
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    images_out_dir = out_html.parent / "{}_images".format(out_html.stem)
    images_out_dir.mkdir(parents=True, exist_ok=True)

    document = (json_result.get("content") or {}).get("document") or {}
    blocks: List[Dict] = document.get("block") or []
    title = ((document.get("source") or {}).get("file_name")) or out_html.stem

    pages: List[str] = []
    cur_page: Optional[int] = None
    buf: List[str] = []
    for b in blocks:
        pno = b.get("page number", 1)
        if pno != cur_page:
            if buf:
                pages.append('<section class="page" data-page="{}">{}</section>'.format(cur_page, "".join(buf)))
            cur_page = pno
            buf = []
        buf.append(_block_html(b, images_out_dir))
    if buf:
        pages.append('<section class="page" data-page="{}">{}</section>'.format(cur_page, "".join(buf)))

    if not any(images_out_dir.iterdir()):
        shutil.rmtree(images_out_dir, ignore_errors=True)

    doc_html = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; margin: 0; background: #f4f4f4; }}
  header.doc {{ padding: 12px 24px; background: #fff; border-bottom: 2px solid #666; }}
  header.doc h1 {{ font-size: 18px; margin: 0; }}
  div.legend {{ margin-top: 6px; font-size: 13px; }}
  section.page {{ max-width: 800px; margin: 16px auto; padding: 24px 32px;
                  background: #fff; border: 1px solid #ccc; page-break-after: always; }}
  section.page:target {{ outline: 2px solid #2b7; }}
  h1,h2,h3,h4,h5,h6 {{ margin: 14px 0 6px; line-height: 1.25; }}
  p {{ margin: 8px 0; line-height: 1.4; }}
  ul, ol {{ margin: 8px 0; }}
  li {{ margin: 3px 0; line-height: 1.35; }}
  div.formula {{ margin: 8px 0; padding: 6px 10px; background: #faf6ec;
                 border-left: 3px solid #b8860b; font-style: italic; font-size: 1.05em; }}
  table {{ border-collapse: collapse; margin: 8px 0; width: 100%; font-size: 0.92em; }}
  th, td {{ border: 1px solid #888; padding: 4px 6px; vertical-align: top; }}
  th {{ background: #ececec; }}
  div.image-block {{ text-align: center; margin: 8px 0; }}
  div.image-block img {{ max-width: 90%; border: 1px solid #ccc; }}
  span.missing {{ color: #a33; font-style: italic; }}
</style>
</head>
<body>
<header class="doc"><h1>{title}</h1></header>
{pages}
</body>
</html>""".format(title=html.escape(title), pages="\n".join(pages))

    out_html.write_text(doc_html, encoding="utf-8")
    logger.info("Result HTML saved to %s (%d pages)", out_html, len(pages))
    return out_html
