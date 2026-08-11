import re
import unicodedata


_SPACED_LETTERS_RE = re.compile(
    r'\b[А-ЯA-Zа-яa-z]\b(?: [А-ЯA-Zа-яa-z]\b){2,}'
)


def remove_spaced_letters(text: str) -> str:
    """Снимает разрядку: цепочки из >=3 одиночных букв через пробел
    склеиваются: 'Т а б л и ц а 2.2.1' -> 'Таблица 2.2.1', 'Ж и л ы е' -> 'Жилые'."""
    return _SPACED_LETTERS_RE.sub(lambda m: re.sub(r' ', '', m.group(0)), text)


def soft_clean_line(line: str) -> str:
    """Мягкая чистка строки из сырого PDF: управляющие символы, повторы /n/n, пробелы."""
    cleaned = []
    for ch in line:
        if unicodedata.category(ch).startswith('C') and ch not in '\t\n\r':
            continue
        cleaned.append(ch)
    s = ''.join(cleaned)

    s = re.sub(r'/\d+(?:/\d+)*', '', s)

    s = re.sub(r'\s+', ' ', s).strip()

    return s


ALLOWED_RE = re.compile(r'[a-zA-Zа-яёА-ЯЁ0-9\s.,!?:;()\-«»"“”\'–—/\\№%@&*#+<=>]')


def is_garbage_line(cleaned_line: str,
                    min_cyrillic_ratio=0.6,
                    min_allowed_ratio=0.4) -> bool:
    """Признак мусорной строки (шум, случайные символы)."""
    s = cleaned_line.strip()
    if not s:
        return True

    if s.lower() in {'image', 'pdf', 'doc', 'txt'}:
        return False

    if re.fullmatch(r'[A-Z\s]+', s):
        return True

    words = s.split()
    if words:
        avg_len = sum(len(w) for w in words) / len(words)
        if len(words) >= 3 and avg_len < 1.8:
            return True

    allowed = len(ALLOWED_RE.findall(s))
    total = len(s.replace(' ', ''))
    if total > 0 and allowed / total < min_allowed_ratio:
        return True

    letters = re.findall(r'[A-Za-zА-Яа-яёЁ]', s)
    if not letters:
        return True

    cyrillic = sum(1 for c in letters if 'А' <= c <= 'я' or c in 'ёЁ')
    cyrillic_ratio = cyrillic / len(letters)

    if cyrillic_ratio >= min_cyrillic_ratio:
        return False

    if cyrillic == 0 and not re.search(r'\d', s) and len(s) < 20:
        return True

    if len(letters) <= 2 and re.search(r'\d', s):
        return False

    return False


def process_extracted_text(raw_text: str) -> str:
    """Чистит многострочный текст, отбрасывая мусорные строки."""
    clean_lines = []
    for line in raw_text.split('\n'):
        cleaned = soft_clean_line(line)
        if not is_garbage_line(cleaned):
            clean_lines.append(cleaned)
    return '\n'.join(clean_lines)
