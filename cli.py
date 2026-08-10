"""
cross_parsing — CLI.

Команды:
  docling-html   PDF → Docling Serve (Docker, без OCR) → постраничный HTML (один раз)
  parse          PDF + сохранённый HTML → JSON (raw_ocr_v4) кросс-парсингом
  compare        parse + контроль эталона (метрики и отчёт по блокам)

Примеры:
  python cli.py docling-html --pdf data/pdf/doc.pdf
  python cli.py parse --pdf data/pdf/doc.pdf
  python cli.py compare --pdf data/pdf/doc.pdf --etalon data/etalon/doc.json
"""
import argparse
import logging
import sys
from pathlib import Path

from app.algorithm.cross_parser import cross_parse
from app.config import ETALON_DIR, HTML_DIR, OUTPUT_DIR
from app.docling.export_html import generate_html_from_pdf
from app.etalon.compare import compare
from app.etalon.report import save_json_report, save_markdown_report


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )


def cmd_docling_html(args):
    """PDF → Docling Serve → постраничный HTML + metadata (без OCR и без OCR формул)."""
    metadata = generate_html_from_pdf(
        pdf_path=args.pdf,
        out_dir=args.out,
        max_pages=args.max_pages,
    )
    print(f"OK: HTML сохранён для {metadata['source']['file_name']} "
          f"({metadata['source']['page_count']} стр.) -> {args.out or HTML_DIR / Path(args.pdf).stem}")
    return 0


def cmd_parse(args):
    json_result = cross_parse(
        pdf_path=args.pdf,
        html_dir=args.html_dir,
        out_json=args.out,
    )
    blocks = json_result["content"]["document"]["block"]
    print(f"OK: {len(blocks)} блоков, pages={json_result['content']['quality']['pages_processed']}, "
          f"confidence={json_result['content']['quality']['confidence']}")
    return 0


def cmd_compare(args):
    result_path = Path(args.result) if args.result else OUTPUT_DIR / f"{Path(args.pdf).stem}.json"
    json_result = cross_parse(
        pdf_path=args.pdf,
        html_dir=args.html_dir,
        out_json=str(result_path),
    )

    etalon_path = Path(args.etalon)
    report = compare(str(etalon_path), str(result_path), sim_threshold=args.threshold)

    out_dir = Path(args.out) if args.out else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"report_{Path(args.pdf).stem}"
    save_json_report(report, base.with_suffix(".json"))
    save_markdown_report(report, base.with_suffix(".md"))

    s = report["summary"]
    print(f"OK: эталон={s['etalon_blocks']} блоков, результат={s['result_blocks']}, "
          f"совпало={s['matched']}")
    print(f"    Precision={s['precision']} | Recall={s['recall']} | F1={s['f1']} | "
          f"coverage={s['text_coverage']}")
    print(f"    Отчёт: {base}.md")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cross_parsing",
        description="Кросс-парсинг на двух движках (Docling HTML + PyMuPDF) с контролем эталона",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="детальные логи")
    sub = parser.add_subparsers(dest="command", required=True)

    # docling-html
    p = sub.add_parser("docling-html", help="PDF → Docling (Docker) → HTML (один раз)")
    p.add_argument("--pdf", required=True, help="путь к PDF")
    p.add_argument("--out", default=None, help="каталог сохранения HTML (по умолчанию data/html/{stem}/)")
    p.add_argument("--max-pages", type=int, default=None, help="ограничение числа страниц")
    p.set_defaults(func=cmd_docling_html)

    # parse
    p = sub.add_parser("parse", help="PDF + HTML → JSON кросс-парсингом")
    p.add_argument("--pdf", required=True, help="путь к PDF")
    p.add_argument("--html-dir", default=None, help="каталог с page_*.html (по умолчанию data/html/{stem}/)")
    p.add_argument("--out", default=None, help="путь к JSON результата")
    p.set_defaults(func=cmd_parse)

    # compare
    p = sub.add_parser("compare", help="parse + контроль эталона")
    p.add_argument("--pdf", required=True, help="путь к PDF")
    p.add_argument("--etalon", required=True, help="путь к эталонному JSON")
    p.add_argument("--html-dir", default=None, help="каталог с page_*.html")
    p.add_argument("--result", default=None, help="путь к JSON результата (если не задан — сохраняется в output)")
    p.add_argument("--out", default=None, help="каталог отчётов (по умолчанию data/output/)")
    p.add_argument("--threshold", type=float, default=0.6, help="порог сходства текста блоков (0..1)")
    p.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
