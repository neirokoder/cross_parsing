# cross_parsing

Приложение для доработки алгоритма кросс-парсинга PDF на **двух движках** с контролем эталона.

Алгоритм перенесён из `PKB_develop/backend/parser_service` (docling_mapper, html_to_json, quality_metrics, text_cleaner) и адаптирован: тяжёлый парсинг Docling выполняется **один раз** через Docling Serve (Docker), его результат — постраничный HTML — сохраняется локально, а весь цикл доработки алгоритма идёт на сохранённом HTML + PDF без повторного вызова Docling.

## Два движка

| Движок | Вход | Что делает |
|--------|------|------------|
| **Docling** (HTML) | сохранённый постраничный HTML | структура документа: heading / paragraph / table / list / image / formula |
| **PyMuPDF** (PDF) | исходный PDF | добор текста, пропущенного Docling (заголовки, футеры, подписи «Рис.»), детект формул (math-шрифты и image-блоки), восстановление `bounding box`, извлечение изображений и формул-картинок |

Результат — JSON в формате opendataloader (`raw_ocr_v4`): `content.document.block[]`, `content.quality.per_page[]`.

Docling запускается **без OCR** (`do_ocr=false`) и **без OCR-парсинга формул** (`do_formula_enrichment=false`). Нераспознанные формулы остаются в HTML как `Formula not decoded` — их bbox сохраняются в `metadata.json`, а текст восстанавливается из PDF через PyMuPDF на этапе кросс-парсинга.

## Структура

```
cross_parsing/
├── cli.py                        # CLI: docling-html / parse / compare
├── docker-compose.docling.yml    # Docling Serve (CPU)
├── requirements.txt
├── cross_parsing/
│   ├── config.py                 # URL docling, каталоги данных
│   ├── docling/
│   │   ├── serve_client.py       # HTTP-клиент Docling Serve
│   │   └── export_html.py        # PDF → Docling → постраничный HTML (один раз)
│   ├── algorithm/
│   │   ├── html_to_json.py       # HTML → блоки (SAX-парсер)
│   │   ├── pdf_extract.py        # PyMuPDF: enrich, формулы, bbox, картинки
│   │   ├── cross_parser.py       # конвейер кросс-парсинга
│   │   ├── quality_metrics.py    # оценка качества страниц
│   │   └── text_cleaner.py       # чистка текста из сырого PDF
│   └── etalon/
│       ├── compare.py            # сравнение с эталоном (precision/recall/F1)
│       └── report.py             # отчёты (JSON + Markdown)
└── data/
    ├── pdf/      # входные PDF
    ├── html/     # постраничный HTML от Docling (+ metadata.json, docling_document.json)
    ├── etalon/   # эталонные JSON (raw_ocr_v4)
    ├── images/   # извлечённые изображения
    └── output/   # результаты парсинга и отчёты
```

## Установка

```bash
pip install -r requirements.txt
```

## Запуск Docling Serve (Docker)

```bash
docker compose -f docker-compose.docling.yml up -d
# health: http://localhost:5001/health
```

## Использование

```bash
# 1. Один раз: PDF → Docling (Docker) → постраничный HTML в data/html/<имя>/
python cli.py docling-html --pdf data/pdf/doc.pdf

# 2. Кросс-парсинг: PDF + сохранённый HTML → JSON
python cli.py parse --pdf data/pdf/doc.pdf
# результат: data/output/doc.json

# 3. Кросс-парсинг + контроль эталона
python cli.py compare --pdf data/pdf/doc.pdf --etalon data/etalon/doc.json
# метрики в консоль, отчёты: data/output/report_doc.{json,md}
```

Дополнительные опции: `--html-dir`, `--out`, `--max-pages`, `--threshold` (порог сходства текста блоков, по умолчанию 0.6), `-v` (детальные логи).

## Контроль эталона

`compare` сопоставляет блоки результата с эталоном по странице и сходству текста и считает:
- **precision / recall / F1** — по блокам (общие и по типам),
- **text coverage** — долю символов эталона, покрытую результатом,
- **расхождения**: `missed` (есть в эталоне, нет в результате), `extra` (есть в результате, нет в эталоне), несовпадения типов — постранично.

Эталон — JSON в формате `raw_ocr_v4` (поддерживаются структуры с `block`, `kids`, `content.document.block`).

## Цикл доработки алгоритма

1. `docling-html` — сгенерировать HTML (Docling вызывается один раз).
2. `parse` — получить JSON.
3. `compare` — сравнить с эталоном, посмотреть `missed`/`extra` в отчёте.
4. Правки — в `cross_parsing/algorithm/` (html_to_json.py — структура блоков; pdf_extract.py — enrich, формулы, bbox).
5. Повторить шаги 2–3 — Docling повторно **не** запускается.
