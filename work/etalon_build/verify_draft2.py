import os, sys, json, re
from difflib import SequenceMatcher

sys.stdout.reconfigure(encoding='utf-8')
ROOT = r'C:\Projects\University\Cross_parsing'

def norm(s):
    s = re.sub(r'[\s\u00a0]+', ' ', s)
    s = re.sub(r'[^а-яёa-z0-9]+', '', s.lower(), flags=0)
    return s

def contains(hay, needle, thr=0.85):
    hn, nn = norm(hay), norm(needle)
    if not nn:
        return True
    if nn in hn:
        return True
    m = SequenceMatcher(None, hn, nn, autojunk=False).find_longest_match(0, len(hn), 0, len(nn))
    return m.size / len(nn) >= thr

def block_text(m):
    t = m.get('c') or ''
    if m['t'] == 'li':
        t = '\n'.join(m['items'])
    if m['t'] == 'table':
        t = ' '.join(m['cols'] + [x for r in m['rows'] for x in r])
    return t

def main():
    name = sys.argv[1]
    pages = [int(a) for a in sys.argv[2:]] if len(sys.argv) > 2 else None
    draft = json.load(open(os.path.join(ROOT, 'work', 'etalon_build', 'markup', f'{name}.draft.json'), encoding='utf-8'))
    import fitz
    doc = fitz.open(os.path.join(ROOT, 'data', 'pdf', f'{name}.pdf'))
    by_page = {}
    for m in draft:
        by_page.setdefault(m['p'], []).append(m)
    pnos = pages or sorted(by_page)
    for pno in pnos:
        plines = []
        for b in doc[pno-1].get_text('dict')['blocks']:
            if b['type'] == 0:
                for l in b['lines']:
                    t = ''.join(s['text'] for s in l['spans']).strip()
                    if t:
                        plines.append(t)
        ptext = ' '.join(plines)
        blocks = by_page.get(pno, [])
        pblocks = [block_text(m) for m in blocks]
        missing = [t for t in plines if re.fullmatch(r'\d{1,2}', t) is None and not contains(' '.join(pblocks), t)]
        extra = [f"[{m['t']}] {block_text(m)}" for m in blocks
                 if len(block_text(m).strip()) >= 20 and not contains(ptext, block_text(m))]
        if missing or extra:
            print(f"== PAGE {pno}: blocks={len(blocks)}")
            for x in missing:
                print(f"  MISSING: {x}")
            for x in extra:
                print(f"  EXTRA: {x[:200]}")

if __name__ == '__main__':
    main()
