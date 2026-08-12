import edgeparse, json

pdf = r'C:\Projects\University\Cross_parsing\data\pdf\2-020101-174-12.pdf'
data = json.loads(edgeparse.convert(pdf, format='json'))

for needle in ('11.3', 'испытани', 'ИСПЫТАНИЯ', 'Испытания'):
    hits = []
    for el in data['kids']:
        c = el.get('content') or ''
        if needle.lower() in c.lower():
            hits.append((el.get('page number'), el.get('type'), el.get('id'), c[:80]))
    print(needle, '->', hits if hits else 'NOT FOUND')
