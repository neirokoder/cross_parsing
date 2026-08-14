import os, sys, json, re
from difflib import SequenceMatcher

sys.stdout.reconfigure(encoding='utf-8')
ROOT = r'C:\Projects\University\Cross_parsing'

def norm(s):
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'(?<=\w) (?=\w)', '', s)
    return ' '.join(s.split()).strip().lower()

def load_pdf_lines(pdf_name):
    import fitz
    doc = fitz.open(os.path.join(ROOT, 'data', 'pdf', f'{pdf_name}.pdf'))
    pages = []
    for pno in range(doc.page_count):
        lines = []
        for b in doc[pno].get_text('dict')['blocks']:
            if b['type'] == 0:
                for l in b['lines']:
                    spans = [s for s in l['spans'] if s['text'].strip()]
                    if not spans:
                        continue
                    x0 = min(s['bbox'][0] for s in spans)
                    y0 = min(s['bbox'][1] for s in spans)
                    x1 = max(s['bbox'][2] for s in spans)
                    y1 = max(s['bbox'][3] for s in spans)
                    text = ''.join(s['text'] for s in spans)
                    math = any('Math' in s['font'] or 'Symbol' in s['font'] for s in spans)
                    size = max(s['size'] for s in spans)
                    font = spans[0]['font']
                    lines.append({'bbox': [round(x0,1), round(y0,1), round(x1,1), round(y1,1)],
                                  'text': text.strip(), 'math': math, 'size': size, 'font': font,
                                  'norm': norm(text)})
            else:
                r = [round(v,1) for v in b['bbox']]
                lines.append({'bbox': r, 'text': '', 'math': False, 'size': 0, 'font': 'IMG',
                              'norm': '', 'img': True})
        pages.append(lines)
    return pages

def bbox_union(a, b):
    if not a:
        return list(b)
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]

def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()

def find_lines(pdf_lines, text, page):
    tn = norm(text)
    words = tn.split()
    if not words:
        return []
    cands = []
    for i, l in enumerate(pdf_lines):
        if l.get('img'):
            continue
        r = similar(tn, l['norm'])
        if r >= 0.6:
            cands.append((r, i, l))
    cands.sort(key=lambda x: -x[0])
    # take best non-overlapping lines greedily
    used = set()
    out = []
    for r, i, l in cands[: min(8, len(cands))]:
        if i in used:
            continue
        used.add(i)
        out.append(l)
    # also add neighbors of first match (same block region)
    if out:
        best = cands[0][1]
        for i in range(max(0, best-6), min(len(pdf_lines), best+7)):
            if i in used:
                continue
            l = pdf_lines[i]
            if l.get('img') or not l['text']:
                continue
            if similar(tn, l['norm']) >= 0.3:
                used.add(i)
                out.append(l)
    return out

def main():
    name = sys.argv[1]
    out_json = json.load(open(os.path.join(ROOT, 'data', 'output', f'{name}.json'), encoding='utf-8'))
    blocks = out_json['content']['document']['block']
    pages = load_pdf_lines(name)
    markup = []
    for b in blocks:
        pno = b['page number']
        pl = pages[pno-1]
        m = None
        t = b['type']
        if t == 'paragraph':
            m = {'t': 'par', 'p': pno, 'c': b['content']}
        elif t == 'heading':
            m = {'t': 'h', 'p': pno, 'c': b['content'], 'lvl': b.get('heading level', 2)}
        elif t == 'list':
            m = {'t': 'li', 'p': pno, 'lt': b.get('list_type', 'bullet'),
                 'items': [it['content'] for it in b.get('items', [])],
                 'c': b['content']}
        elif t == 'table':
            cols = b.get('columns', [])
            rows = [[c['block'][0]['content'] for c in r['cells']] for r in b.get('rows', [])]
            m = {'t': 'table', 'p': pno, 'cols': cols, 'rows': rows}
            # bbox: union of all cells + columns via pdf text
            all_text = ' '.join(cols + [x for row in rows for x in row])
            hit = find_lines(pl, all_text, pno)
            if hit:
                bb = None
                for l in hit:
                    bb = bbox_union(bb, l['bbox'])
                if bb:
                    m['b'] = [round(v, 1) for v in bb]
        elif t == 'image':
            m = {'t': 'img', 'p': pno, 'key': b.get('image_key', ''), 'c': b.get('content', '')}
            if not m['c']:
                m.pop('c', None)
        elif t == 'formula':
            m = {'t': 'formula', 'p': pno, 'c': b['content'], 'latex': b.get('latex', '')}
            if not m['latex']:
                m.pop('latex', None)
        else:
            print('SKIP type', t, b.get('content', '')[:60])
            continue
        if 'b' not in m:
            bb = None
            for l in find_lines(pl, m.get('c') or ' '.join(m.get('items', [])) or m.get('key', ''), pno):
                bb = bbox_union(bb, l['bbox'])
            if bb:
                m['b'] = [round(v, 1) for v in bb]
        markup.append(m)
    dst = os.path.join(ROOT, 'work', 'etalon_build', 'markup', f'{name}.draft.json')
    with open(dst, 'w', encoding='utf-8') as f:
        json.dump(markup, f, ensure_ascii=False, indent=1)
    with_bbox = sum(1 for m in markup if 'b' in m)
    print(f'saved {dst} blocks={len(markup)} with_bbox={with_bbox}')

if __name__ == '__main__':
    main()
