import fitz

pdf = fitz.open(r'C:\Projects\University\Cross_parsing\data\pdf\2-020101-174-12.pdf')

def dump(pno, y0, y1):
    page = pdf[pno - 1]
    print(f'===== page {pno} y {y0:.0f}-{y1:.0f} =====')
    for b in page.get_text('dict', sort=True)['blocks']:
        if b['type'] != 0:
            print('  [image block]', b['bbox'])
            continue
        for line in b['lines']:
            t = ''.join(s['text'] for s in line['spans']).strip()
            if not t:
                continue
            fonts = sorted({s.get('font','') for s in line['spans']})
            sizes = sorted({round(s.get('size',0),1) for s in line['spans']})
            print(f'  y={line["bbox"][1]:.1f} sz={sizes} fonts={fonts}')
            print(f'    {t[:90]}')

dump(23, 0, 900)
dump(37, 0, 900)
