import json
import sys
from pathlib import Path

def to_markup(block):
    t = block['type']
    p = block['page number']
    b = block['bounding box']
    if t == 'paragraph':
        return {'t': 'par', 'p': p, 'c': block['content'], 'b': b}
    if t == 'heading':
        return {'t': 'h', 'p': p, 'c': block['content'], 'lvl': block['heading level'], 'b': b}
    if t == 'image':
        m = {'t': 'img', 'p': p, 'key': block['image_key'], 'b': b}
        if block.get('content'):
            m['c'] = block['content']
        return m
    if t == 'table':
        return {'t': 'table', 'p': p, 'cols': block['columns'],
                'rows': [[cell['block'][0]['content'] for cell in row['cells']] for row in block['rows']],
                'b': b}
    if t == 'list':
        return {'t': 'li', 'p': p, 'lt': block['list_type'],
                'items': [it['content'] for it in block['items']],
                'c': block['content'], 'b': b}
    if t == 'formula':
        m = {'t': 'formula', 'p': p, 'c': block['content'], 'b': b}
        if block.get('latex'):
            m['latex'] = block['latex']
        return m
    raise ValueError('unknown block type: ' + t)

def to_etalon(m):
    t = m['t']
    p = m['p']
    b = m['b']
    if t == 'par':
        return {'type': 'paragraph', 'page number': p, 'content': m['c'], 'bounding box': b}
    if t == 'h':
        return {'type': 'heading', 'page number': p, 'content': m['c'],
                'heading level': m['lvl'], 'bounding box': b}
    if t == 'img':
        return {'type': 'image', 'page number': p, 'content': m.get('c', ''),
                'image_key': m['key'], 'bounding box': b}
    if t == 'table':
        return {'type': 'table', 'page number': p, 'columns': m['cols'],
                'rows': [{'cells': [{'block': [{'type': 'paragraph', 'content': c}]} for c in row]}
                         for row in m['rows']],
                'bounding box': b}
    if t == 'li':
        return {'type': 'list', 'page number': p, 'list_type': m['lt'],
                'content': m['c'], 'items': [{'content': it} for it in m['items']],
                'bounding box': b}
    if t == 'formula':
        e = {'type': 'formula', 'page number': p, 'content': m['c'], 'latex': '', 'bounding box': b}
        if m.get('latex'):
            e['latex'] = m['latex']
        return e
    raise ValueError('unknown markup type: ' + t)

def main():
    if len(sys.argv) != 4:
        print('usage: convert.py to_markup <etalon.json> <markup.json> | to_etalon <markup.json> <etalon.json>')
        sys.exit(1)
    direction, src, dst = sys.argv[1:4]
    data = json.loads(Path(src).read_text(encoding='utf-8'))
    if direction == 'to_markup':
        blocks = data['content']['document']['block']
        out = [to_markup(x) for x in blocks]
    elif direction == 'to_etalon':
        out = {'content': {'document': {'block': [to_etalon(x) for x in data]}}}
    else:
        print('unknown direction: ' + direction)
        sys.exit(1)
    Path(dst).write_text(json.dumps(out, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    print(f'saved {dst} blocks: {len(out) if isinstance(out, list) else len(out["content"]["document"]["block"])}')

if __name__ == '__main__':
    main()
