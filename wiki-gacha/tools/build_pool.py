#!/usr/bin/env python3
"""Wikiガチャの記事プールを Wikipedia 日本語版のダンプから作る。

入力（同じフォルダに置く。GitHub Actions がダウンロードする）:
  jawiki-latest-page.sql.gz        記事一覧（ID・名前空間・タイトル・リダイレクトか）
  jawiki-latest-linktarget.sql.gz  リンク先の一覧（リンク先ID → 名前空間・タイトル）
  jawiki-latest-pagelinks.sql.gz   リンク（リンク元ID・リンク元名前空間・リンク先ID）
  jawiki-latest-page_props.sql.gz  ページ属性（曖昧さ回避の判定に使う）

出力（wiki-gacha/pool/）:
  meta.json                  各レアの件数・ファイル数・区切り・作成日
  UR.json / SSR.json         秀逸な記事（＋秀逸な一覧）／良質な記事のページID
  SR/0.json, R/0.json ...    被リンク数で仕分けたページIDを CHUNK 件ずつ
"""
import gzip, json, os, re, sys, time, urllib.parse, urllib.request
from collections import Counter

SRC = sys.argv[1] if len(sys.argv) > 1 else '.'
OUT = sys.argv[2] if len(sys.argv) > 2 else 'wiki-gacha/pool'
CHUNK = 10000
# C〜SR の中での割合（全体では SR 4% / R 10% / UC 30% / C 55%）
TIERS = [('SR', 4), ('R', 10), ('UC', 30), ('C', 55)]

def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)

def lines(name):
    with gzip.open(os.path.join(SRC, name), 'rb') as f:
        for line in f:
            if line.startswith(b'INSERT INTO'):
                yield line

def unesc(b):
    return re.sub(rb'\\(.)', rb'\1', b).decode('utf-8', 'replace')

# 1. 記事（標準名前空間・リダイレクト以外）
log('page')
RX_PAGE = re.compile(rb"\((\d+),(-?\d+),'((?:[^'\\]|\\.)*)',(\d),")
title2id, article = {}, set()
for line in lines('jawiki-latest-page.sql.gz'):
    for pid, ns, title, redir in RX_PAGE.findall(line):
        if ns == b'0' and redir == b'0':
            i = int(pid); title2id[title] = i; article.add(i)
log('articles', len(article))

# 2. 曖昧さ回避を除く
log('page_props')
RX_DIS = re.compile(rb"\((\d+),'disambiguation',")
dis = set()
for line in lines('jawiki-latest-page_props.sql.gz'):
    for pid in RX_DIS.findall(line):
        dis.add(int(pid))
article -= dis
log('disambiguation', len(dis), '-> articles', len(article))

# 3. リンク先ID → 記事ID
log('linktarget')
RX_LT = re.compile(rb"\((\d+),0,'((?:[^'\\]|\\.)*)'\)")
lt2page = {}
for line in lines('jawiki-latest-linktarget.sql.gz'):
    for lt, title in RX_LT.findall(line):
        p = title2id.get(title)
        if p is not None and p in article:
            lt2page[lt] = p
del title2id
log('linktargets to articles', len(lt2page))

# 4. 被リンク数（標準名前空間からのリンクだけ数える）
log('pagelinks')
RX_PL = re.compile(rb"\(\d+,0,(\d+)\)")
cnt_lt = Counter()
n = 0
for line in lines('jawiki-latest-pagelinks.sql.gz'):
    cnt_lt.update(RX_PL.findall(line)); n += 1
    if n % 500 == 0: log('  lines', n)
links = Counter()
for lt, c in cnt_lt.items():
    p = lt2page.get(lt)
    if p is not None: links[p] += c
del cnt_lt
log('pagelinks done')

# 5. 秀逸・良質（API でカテゴリを取る。ノートページに貼られている場合はノートを本体に読み替える）
def api(params):
    q = urllib.parse.urlencode({**params, 'format': 'json', 'formatversion': 2})
    req = urllib.request.Request('https://ja.wikipedia.org/w/api.php?' + q, headers={'User-Agent': 'wiki-gacha-pool/1.0 (https://cceight.github.io/wiki-gacha/)'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def cat_ids(cat):
    titles, cont = [], {}
    while True:
        j = api({'action': 'query', 'list': 'categorymembers', 'cmtitle': 'Category:' + cat, 'cmlimit': 500, **cont})
        for m in j['query']['categorymembers']:
            if m['ns'] in (0, 1):
                titles.append(re.sub(r'^ノート:', '', m['title']))
        if 'continue' not in j: break
        cont = {'cmcontinue': j['continue']['cmcontinue']}
        time.sleep(0.5)
    ids = []
    for i in range(0, len(titles), 50):
        j = api({'action': 'query', 'titles': '|'.join(titles[i:i + 50]), 'redirects': 1})
        ids += [p['pageid'] for p in j['query']['pages'] if 'pageid' in p and p.get('ns') == 0]
        time.sleep(0.5)
    return sorted(set(ids))

log('categories')
ur = sorted(set(cat_ids('秀逸な記事')) | set(cat_ids('秀逸な一覧')))
ssr = [i for i in cat_ids('良質な記事') if i not in set(ur)]
log('UR', len(ur), 'SSR', len(ssr))

# 6. 仕分け（被リンク数の多い順。同数はID順で安定させる）
special = set(ur) | set(ssr)
pool = sorted((i for i in article if i not in special), key=lambda i: (-links.get(i, 0), i))
total, tot_w = len(pool), sum(w for _, w in TIERS)
meta = {'built': time.strftime('%Y-%m-%d'), 'tiers': {}}
start = 0
os.makedirs(OUT, exist_ok=True)
for k, (name, w) in enumerate(TIERS):
    end = total if k == len(TIERS) - 1 else start + round(total * w / tot_w)
    ids = sorted(pool[start:end])
    lo, hi = links.get(pool[end - 1], 0), links.get(pool[start], 0)
    d = os.path.join(OUT, name); os.makedirs(d, exist_ok=True)
    for f in os.listdir(d): os.remove(os.path.join(d, f))
    chunks = 0
    for c in range(0, len(ids), CHUNK):
        with open(os.path.join(d, f'{chunks}.json'), 'w') as f: json.dump(ids[c:c + CHUNK], f, separators=(',', ':'))
        chunks += 1
    meta['tiers'][name] = {'count': len(ids), 'chunks': chunks, 'minLinks': lo, 'maxLinks': hi}
    log(name, len(ids), 'links', lo, '-', hi)
    start = end
for name, ids in (('UR', ur), ('SSR', ssr)):
    with open(os.path.join(OUT, f'{name}.json'), 'w') as f: json.dump(ids, f, separators=(',', ':'))
    meta['tiers'][name] = {'count': len(ids)}
with open(os.path.join(OUT, 'meta.json'), 'w') as f: json.dump(meta, f, ensure_ascii=False, indent=1)
log('done', json.dumps(meta, ensure_ascii=False))
