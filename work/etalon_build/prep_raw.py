import os, sys, json
import fitz

sys.stdout.reconfigure(encoding='utf-8')
ROOT = r'C:\Projects\University\Cross_parsing'
raw = os.path.join(ROOT, 'work', 'etalon_build', 'raw')
os.makedirs(raw, exist_ok=True)
pdf = fitz.open(os.path.join(ROOT, 'data', 'pdf', '2-020101-174-12.pdf'))

for pno in range(pdf.page_count):
    p = pdf[pno]
    lines = []
    for b in p.get_text('dict')['blocks']:
        if b['type'] == 0:
            for l in b['lines']:
                for s in l['spans']:
                    t = s['text'].strip()
                    if t:
                        lines.append(f"{s['bbox'][0]:7.1f} {s['bbox'][1]:7.1f} {s['bbox'][2]:7.1f} {s['bbox'][3]:7.1f} {s['font']:14s} {s['size']:5.2f} {t}")
        else:
            r = b['bbox']
            lines.append(f"IMG  {r[0]:7.1f} {r[1]:7.1f} {r[2]:7.1f} {r[3]:7.1f} ext={b.get('ext','')}")
    with open(os.path.join(raw, f'p{pno+1:03d}_text.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

html_dir = os.path.join(ROOT, 'data', 'html', '2-020101-174-12')
meta = json.load(open(os.path.join(html_dir, 'metadata.json'), encoding='utf-8'))
os.makedirs(os.path.join(raw, 'html'), exist_ok=True)
import shutil
for fn in os.listdir(html_dir):
    if fn.startswith('page_') and fn.endswith('.html'):
        shutil.copy2(os.path.join(html_dir, fn), os.path.join(raw, 'html', fn))
shutil.copy2(os.path.join(html_dir, 'metadata.json'), os.path.join(raw, 'metadata.json'))
print('prep done', pdf.page_count, 'pages;', len(meta.get('formula_bboxes', {})) if isinstance(meta, dict) else meta)
