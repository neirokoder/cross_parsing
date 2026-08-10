import os, sys, json, re, html as htmlmod

sys.stdout.reconfigure(encoding='utf-8')
ROOT = r'C:\Projects\University\Cross_parsing'

def main():
    pages = [int(a) for a in sys.argv[1:]] or list(range(1, 42))
    raw_dir = os.path.join(ROOT, 'work', 'etalon_build', 'raw')
    out = json.load(open(os.path.join(ROOT, 'data', 'output', '2-020101-174-12.json'), encoding='utf-8'))
    blocks = out['content']['document']['block']
    by_page = {}
    for b in blocks:
        by_page.setdefault(b.get('page number', 1), []).append(b)
    buf = []
    for pno in pages:
        buf.append(f"{'='*20} PAGE {pno} {'='*20}")
        tf = os.path.join(raw_dir, f'p{pno:03d}_text.txt')
        if os.path.exists(tf):
            txt = open(tf, encoding='utf-8').read()
            buf.append('--- PDF text ---')
            buf.append(txt)
        hf = os.path.join(ROOT, 'data', 'html', '2-020101-174-12', f'page_{pno:04d}.html')
        if os.path.exists(hf):
            h = open(hf, encoding='utf-8').read()
            h = re.sub(r'<head>.*?</head>', '', h, flags=re.S)
            h = re.sub(r'\s+', ' ', h)
            buf.append('--- HTML ---')
            buf.append(h[:6000])
        buf.append('--- PARSER blocks ---')
        for b in by_page.get(pno, []):
            t = b.get('content', '')
            if b['type'] == 'table':
                rows = []
                for r in b.get('rows', []):
                    cells = [' '.join(cb.get('content','') for cb in c.get('block',[])) for c in r.get('cells',[])]
                    rows.append(' | '.join(cells))
                t = ' || '.join(rows)[:1500]
            buf.append(f"[{b['type']}] {t[:400]}")
        buf.append('')
    fn = os.path.join(ROOT, 'work', 'etalon_build', 'pages_dump.txt')
    with open(fn, 'w', encoding='utf-8') as f:
        f.write('\n'.join(buf))
    print('written', fn, 'pages', pages)

if __name__ == '__main__':
    main()
