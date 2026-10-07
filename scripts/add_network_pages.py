#!/usr/bin/env python3
"""add_network_pages.py - add the Network and Cell line uses pages to a data folder that build_dashboard_data.py already made,
without re-running the whole pipeline.

    python3 add_network_pages.py slim.jsonl.gz data

Reads the same input file, writes net.json/.js and celluse.json/.js into the data folder and adds the two pages to
manifest.json/.js. Keep build_dashboard_data.py in the same folder as this script. Open dashboard.html as usual afterwards
(over http, or from file:// where the .js copies are used). Cell-line names come from the input; a slim file made before the
names were kept shows codes instead."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_dashboard_data as B

if len(sys.argv) != 3:
    sys.exit(__doc__)
src, out = sys.argv[1], sys.argv[2]
mp = os.path.join(out, 'manifest.json')
man = json.load(open(mp, encoding='utf-8'))
net = B.build_network(src, 0, None, man['nm'])
if not net['papers']:
    sys.exit('no paper in the input names a variant, cell line, chromosome or genomic region')
nn = {}
for n in net['nodes']:
    nn[n[0]] = nn.get(n[0], 0) + 1
sub_n = ('Each dot is a term named in %d papers that mention at least one variant, cell line, chromosome or genomic region (%s). '
         'Two terms are joined when they are named in the same paper; the line is thicker the more papers share them. '
         'Very common terms such as COVID-19 are hidden by default because they would join everything to everything.') % (
    net['N'], ', '.join('%d %s' % (v, k.lower() + 's') for k, v in sorted(nn.items(), key=lambda kv: -kv[1])))
ncl = sum(1 for r in net['papers'] if any(net['nodes'][i][0] == 'CellLine' for i in r[2]))
sub_c = ('Which cell lines are used with which topics? Cell lines are grouped by tissue and species with a small hand-made lookup, and the table (or matrix) lists the diseases, chemicals or genes '
                     'named in the same papers as each cell line. Only %d papers name a cell line, so read it as a map, not as a result.' % ncl)
B.write_page(out, 'net', dict(net, cfg={'id': 'net', 'kind': 'net', 'title': 'Network of variants, cell lines, genes and diseases', 'sub': sub_n}), True)
B.write_page(out, 'celluse', dict(net, cfg={'id': 'celluse', 'kind': 'celluse', 'title': 'Cell line uses', 'sub': sub_c}), True)
man['pages'] = [p for p in man['pages'] if p['id'] not in ('net', 'celluse')] + [
    {'id': 'net', 'nav': 'Network', 'kind': 'net', 'n': len(net['nodes'])}, {'id': 'celluse', 'nav': 'Cell line uses', 'kind': 'celluse', 'n': net['N']}]
B.write_page(out, 'manifest', man, True)
print('added Network and Cell line uses to', os.path.abspath(out))
