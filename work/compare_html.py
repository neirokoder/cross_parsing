import re, html as htmllib, json
from pathlib import Path

ROOT = Path(r'C:\Projects\University\Cross_parsing')
DOCLING = ROOT / 'data' / 'html' / '2-020101-174-12_docling'
EDGEPARSE = ROOT / 'work' / 'edgeparse.html'

def strip_html(text):
    text = re.sub(r'<script.*?</script>', ' ', text, flags=re.S)
    text = re.sub(r'<style.*?</style>', ' ', text, flags=re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = htmllib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def norm(s):
    s = s.replace('\u00a0', ' ')
    return re.sub(r'\s+', ' ', s).strip()

pages = {}
for f in sorted(DOCLING.glob('page_*.html')):
    pages[int(f.stem.split('_')[1])] = norm(strip_html(f.read_text(encoding='utf-8')))

edge_text = norm(strip_html(EDGEPARSE.read_text(encoding='utf-8')))

all_words = []
for pnum, txt in pages.items():
    words = re.findall(r'\S+', txt)
    all_words.append((pnum, words))

import collections
miss_by_page = collections.Counter()
tot_miss = 0
tot_words = 0
for pnum, words in all_words:
    seen = set()
    for w in words:
        if w in seen:
            continue
        seen.add(w)
        tot_words += 1
        if w not in edge_text:
            miss_by_page[pnum] += 1
            tot_miss += 1

print('unique words Docling:', tot_words, '| missing in EdgeParse:', tot_miss,
      '| loss rate: %.3f' % (tot_miss / tot_words))
print('missing by page:', json.dumps({str(k): v for k, v in sorted(miss_by_page.items())}))

sample = collections.Counter()
for pnum, words in all_words:
    seen = set()
    for w in words:
        if w in seen or w in edge_text:
            continue
        seen.add(w)
        if len(w) > 3 and not w.isdigit():
            sample[(pnum, w)] += 1
print('\nsample of lost words:')
for (p, w), c in list(sample.most_common())[:60]:
    print(f'p{p}: {w}')
