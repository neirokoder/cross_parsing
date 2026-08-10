"""
Конфигурация app.
Все значения можно переопределить переменными окружения или флагами CLI.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Docling Serve (Docker)
DOCLING_SERVE_URL = os.getenv("DOCLING_SERVE_URL", "http://localhost:5001")
DOCLING_SERVE_TIMEOUT = int(os.getenv("DOCLING_SERVE_TIMEOUT", "3600"))

# Парсинг Docling: без OCR и без OCR-парсинга формул (formula enrichment).
DOCLING_DO_OCR = False
DOCLING_DO_FORMULA_ENRICHMENT = False
DOCLING_TABLE_STRUCTURE = True

# Каталоги данных
DATA_DIR = Path(os.getenv("CROSS_PARSING_DATA", BASE_DIR / "data"))
PDF_DIR = DATA_DIR / "pdf"
HTML_DIR = DATA_DIR / "html"
ETALON_DIR = DATA_DIR / "etalon"
OUTPUT_DIR = DATA_DIR / "output"
IMAGES_DIR = DATA_DIR / "images"
