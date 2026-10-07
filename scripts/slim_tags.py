#!/usr/bin/env python3
"""Reduce the PubTator JSONL (one record per line, annotations in passages[].annotations[].infons)
to one small record per paper: pmid, date, and the set of identifiers for each entity type.

Usage:  python3 slim_tags.py input.jsonl[.gz] output.jsonl.gz
Output line: {"pmid": "36730047", "date": "2023-03-01", "Disease": {"MESH:D000086382": "COVID-19", ...}, "Chemical": {...}, ...}
Only distinct identifiers per paper are kept (a term mentioned ten times counts once), which is
what the co-occurrence analysis needs and makes the file a few percent of the original size.

Every paper is written, and every entity type in TYPES is present on every line (an empty {} when the paper has no
tag of that type), so downstream code can tell "paper without chemical tags" from "paper not in the file" and use
all papers, not only the tagged ones, as the denominator.  Any other type found in the input is added as well.
"""
import sys, json, gzip, collections

TYPES = ['Disease', 'Chemical', 'Gene', 'Species', 'Variant', 'CellLine', 'Chromosome', 'GenomicRegion']

def opener(p, mode):
    return gzip.open(p, mode + 't', encoding='utf-8') if p.endswith('.gz') else open(p, mode, encoding='utf-8')

def slim(obj):
    rec = obj.get('record', obj)
    tags = collections.defaultdict(dict)
    for ps in rec.get('passages', []):
        for a in ps.get('annotations', []):
            inf = a.get('infons', {})
            t, ident = inf.get('type'), inf.get('identifier')
            if not t or ident in (None, '', '-'):
                continue
            tags[t][str(ident)] = inf.get('name') or a.get('text', '')
    out = {'pmid': str(obj.get('pmid') or rec.get('id')), 'date': (rec.get('date') or '')[:10]}
    for t in TYPES:
        out[t] = {}
    out.update(tags)
    return out

def main(src, dst):
    n = 0
    types = collections.Counter()
    with opener(src, 'r') as f, opener(dst, 'w') as g:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = slim(json.loads(line))
            except Exception as e:
                print('skipped a line:', e, file=sys.stderr)
                continue
            for t, v in o.items():
                if t not in ('pmid', 'date') and v:
                    types[t] += 1
            g.write(json.dumps(o, separators=(',', ':'), ensure_ascii=False) + '\n')
            n += 1
    print(n, 'papers written; papers with each entity type:', dict(types))

if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
