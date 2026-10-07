#!/usr/bin/env python3
"""
build_dashboard_data.py  -  from PubTator JSONL(.gz) to the data behind the lifepath dashboard
=============================================================================================

One script, one input file, one output folder.

    python3 build_dashboard_data.py pubtator.jsonl.gz -o data
    python3 build_dashboard_data.py slim.jsonl.gz -o data --mesh-tree mesh_tree.json

Then put dashboard.html next to the data/ folder and open it (over http, e.g.
`python3 -m http.server`, or by double-clicking: a script-tag copy of the data is also written).

INPUT
    One paper per line, either
      * the full PubTator record   {"pmid":..,"record":{"date":..,"passages":[{"annotations":[{"infons":
                                    {"identifier":..,"type":..,"name":..}}]}]}}
      * or the slim form from slim_tags.py   {"pmid":..,"date":"2023-03-01","Disease":{"MESH:D0..":"COVID-19"},..}
    An entity counts once per paper, however often it is mentioned. Annotations with no identifier ('-')
    are skipped.
    Disease tags with an OMIM id (terms with no MeSH match, labelled with text fragments such as 'PCS' or '30') are folded
    into MeSH or dropped, see --omim.

WHAT IT DOES (stage -> what ends up in the output)
    1  time series      distinct papers per month for every entity of every type (Disease, Chemical, Variant, ...)
    2  collection gaps  off by default: counts are used as collected, the 2024 dip included. With --fill-gap a run of
                        months in which far fewer papers carry tags of a type is detected per type and diseases are
                        filled as max(observed, mean of the 3 months before and after); --gap 2024-04 2024-08 forces it.
    3  lifepaths        at each half-year snapshot (Jun, Dec, plus the last month) a logistic curve is fitted to the
                        zero-padded cumulative paper counts; the row is
                        [inflection_day, ln(k+.001), papers_so_far, phase, x0, k, saturation] with phase
                        0 Acceleration / 1 Inflection / 2 Deceleration / 3 Deactivation.
                        Diseases with a gap are fitted twice (as collected and gap-filled).
    4  wave groups      k-means (k=WAVE_K, default 4; --wave-k 2|3|4, or auto to pick the best mean silhouette; sklearn silhouette_score for k=2..8 goes to manifest.json) on each core term's quarterly volume scaled to its own peak, named
                        Acute early / Broad first wave / Late symptom / Chronic core from the shape of the mean curve;
    5  archetypes       DTW distance between lifepath sequences + Ward linkage, 3 groups (silhouette for k=2..8 goes to manifest.json)
                        (Early exit / Broad wave / Late settling, named by how often the path is Deactivated);
    6  small terms      terms below the core size (and all chemicals) are assigned to a wave group and an archetype by a
                        multinomial timing model and DTW to the core paths; certainty is stored and validated
                        by thinning core terms to n papers.
    7  MeSH categories (needs --mesh-tree) monthly counts of all terms under each 2nd/3rd level tree node, refitted.
    8  share clusters  hierarchical (Ward) clusters of the standardized monthly share of papers (or --clusters-csv).
    9  co-mentions     disease x chemical papers together, odds ratio, one-sided hypergeometric test, BH FDR,
                        spectral co-clustering of the positive log observed/expected matrix.
    10 network         papers that name a variant, cell line, chromosome or genomic region, with their genes, diseases and
                        chemicals: a network page and a cell-line-use page (skip with --no-network).

OUTPUT (folder given by -o)
    manifest.json            snapshots, months, gaps, page list, validation numbers
    <page>.json              one per page: diseases, categories, clusters, chemicals, variants, celllines,
                             chromosomes, cooc (+ genes, species with --types)
    <page>.js / manifest.js  the same data as <script>-loadable files (window.DASH_DATA[...]) so the dashboard
                             also works from file://  (skip with --no-js)
    tables/*.csv             flat tables (term groups, co-mention pairs) for papers and spreadsheets

Requires: numpy, scipy, scikit-learn.   Optional: --workers N for the curve fits.
"""
import argparse, collections, csv, datetime, gzip, json, math, os, re, sys, time, warnings
import numpy as np
from multiprocessing import Pool
from scipy.optimize import curve_fit
from scipy.special import logsumexp
from scipy.stats import hypergeom
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform
from sklearn.cluster import KMeans, SpectralCoclustering
from sklearn.metrics import silhouette_score, adjusted_rand_score

warnings.filterwarnings('ignore')
BASE_YEAR = 2020          # month 0 of every series is January of this year (COVID-19 literature)
MAXM = 12 * 12            # allocate up to December 2031, trimmed later
PAD = 12                  # zero-padding months before the first observation in every fit
K_BOUNDS, A_BOUNDS, X0_BOUNDS = (0.0, 500.0), (0.0, 50.0), (-2.0, 3.0)
WAVE_K = 4                                   # number of wave groups: 4 (Acute early / Broad first wave / Late symptom / Chronic core); the silhouette on the core terms is highest at k=2, see --wave-k and manifest.json wave_silhouette
WAVE_NAMES_BY_K = {2: ['Early wave', 'Sustained'], 3: ['Acute early', 'Broad first wave', 'Chronic core'],
                   4: ['Acute early', 'Broad first wave', 'Late symptom', 'Chronic core']}
WAVE_NAMES = WAVE_NAMES_BY_K[WAVE_K]


def set_wave_k(k):
    global WAVE_K, WAVE_NAMES
    WAVE_K = k
    WAVE_NAMES = WAVE_NAMES_BY_K[k] if k in WAVE_NAMES_BY_K else WAVE_NAMES_BY_K[2]   # 'auto' is resolved inside Groups
ARCH_NAMES_BY_K = {2: ['Early exit', 'Late settling'], 3: ['Early exit', 'Broad wave', 'Late settling']}   # named by how often a path ends Deactivated
ARCH_NAMES = ARCH_NAMES_BY_K[3]
NUM_WORD = {2: 'two', 3: 'three', 4: 'four'}
PAGE_TYPES = [  # (page id, file label in nav, entity types that go on the page, noun, nouns, placeholder, min-papers filter options)
    ('chemicals', 'Chemicals', ['Chemical'], 'chemical', 'chemicals', 'e.g. remdesivir, oxygen', [0, 10, 20, 50, 100]),
    ('variants', 'Variants', ['Variant'], 'variant', 'variants', 'e.g. D614G, rs1799752', [0, 2, 3]),
    ('celllines', 'Cell lines', ['CellLine'], 'cell line', 'cell lines', 'e.g. Vero E6, A549', [0, 2, 3, 5]),
    ('chromosomes', 'Chromosomes and regions', ['Chromosome', 'GenomicRegion'], 'term', 'terms', 'e.g. chromosome 12', [0, 2, 5]),
    ('genes', 'Genes', ['Gene'], 'gene', 'genes', 'e.g. ACE2, IL6', [0, 5, 10, 50]),
    ('species', 'Species', ['Species'], 'species', 'species', 'e.g. 9606', [0, 2, 5, 10]),
]
DEFAULT_TYPES = ['Disease', 'Chemical', 'Variant', 'CellLine', 'Chromosome', 'GenomicRegion']
DEFAULT_MIN = {'Disease': 1, 'Chemical': 5, 'Variant': 1, 'CellLine': 1, 'Chromosome': 1, 'GenomicRegion': 1, 'Gene': 5, 'Species': 2}


# Disease tags with an OMIM identifier are terms the tagger could not map to MeSH. Their names are phrases copied from the
# paper text ("PCS", "Morbidity", "30", "interval"...), so the OMIM entry behind the number is usually unrelated. By default
# (--omim fold) the clear matches are folded into the MeSH term they obviously mean (a paper counts once, even if it carries
# both tags) and the rest are dropped; --omim drop removes every OMIM disease tag; --omim keep leaves them as they are.
# Matches were made by hand from the phrase and use MeSH ids that already occur in the data; edit this table to change them.
OMIM_TO_MESH = {
    'OMIM:176430': ('MESH:D000094024', 'Post-Acute COVID-19 Syndrome'),     # PCS
    'OMIM:115700': ('MESH:D000094024', 'Post-Acute COVID-19 Syndrome'),     # PCC
    'OMIM:614878': ('MESH:D007154', 'Immune System Diseases'),              # immune dysregulation
    'OMIM:603663': ('MESH:D001523', 'Mental Disorders'),                    # mental health disorders
    'OMIM:608852': ('MESH:D008171', 'Lung Diseases'),                       # impairment of pulmonary function
    'OMIM:212500': ('MESH:D001145', 'Arrhythmias Cardiac'),                 # arrhythmic events
    'OMIM:613882': ('MESH:D008275', 'Magnesium Deficiency'),                # hypomagnesemia
    'OMIM:612862': ('MESH:D020246', 'Venous Thrombosis'),                   # DVT
    'OMIM:603933': ('MESH:D003930', 'Diabetic Retinopathy'),                # non-proliferative diabetic retinopathy
    'OMIM:614674': ('MESH:D008599', 'Menstruation Disturbances'),           # menstrual cycle impairments
    'OMIM:614756': ('MESH:D060825', 'Cognitive Dysfunction'),               # cognitive and behavioral abnormalities
    'OMIM:192950': ('MESH:D012851', 'Sinus Thrombosis Intracranial'),       # CVT
    'OMIM:151600': ('MESH:D009260', 'Nail Diseases'),                       # leukonychia
    'OMIM:608391': ('MESH:D001327', 'Autoimmune Diseases'),                 # autoimmune diseases.2
    'OMIM:613848': ('MESH:D015673', 'Fatigue Syndrome Chronic'),            # ME/CFS-OI
}
OMIM_MODE = 'fold'
OMIM_STATS = collections.Counter()


def clean_omim(dis):
    """Apply OMIM_MODE to one paper's {id: name} disease tags (in place)."""
    om = [k for k in dis if k.startswith('OMIM:')]
    if not om or OMIM_MODE == 'keep':
        return
    for k in om:
        name = dis.pop(k)
        if OMIM_MODE == 'fold' and k in OMIM_TO_MESH:
            tid, tname = OMIM_TO_MESH[k]
            dis.setdefault(tid, tname)          # a dict, so a paper that has both tags still counts once
            OMIM_STATS['folded'] += 1
        else:
            OMIM_STATS['dropped'] += 1


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, file=sys.stderr, flush=True)


def mlabel(k):
    return '%d-%02d' % (BASE_YEAR + k // 12, k % 12 + 1)


def mindex(ym):
    return (int(ym[:4]) - BASE_YEAR) * 12 + int(ym[5:7]) - 1


# ----------------------------------------------------------------------------------------------
# 1  reading and monthly counts
# ----------------------------------------------------------------------------------------------
def read_papers(path, start, end, with_pmid=False):
    """Yield (month_index, {type: {id: name}}) for every paper with a usable date (plus the pmid when with_pmid)."""
    op = gzip.open if path.endswith('.gz') else open
    n = bad = 0
    with op(path, 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                bad += 1
                continue
            rec = o.get('record', o)
            date = (rec.get('date') or o.get('date') or '')[:7]
            if not re.match(r'^\d{4}-\d{2}$', date):
                continue
            m = mindex(date)
            if m < start or (end is not None and m > end) or m >= MAXM:
                continue
            tags = collections.defaultdict(dict)
            if 'passages' in rec:
                for ps in rec['passages']:
                    for a in ps.get('annotations', []):
                        inf = a.get('infons', {})
                        t, ident = inf.get('type'), inf.get('identifier')
                        if not t or ident in (None, '', '-'):
                            continue
                        tags[t][str(ident)] = inf.get('name') or a.get('text', '') or str(ident)
            else:
                for t, v in o.items():
                    if isinstance(v, dict):
                        tags[t] = v
            if tags.get('Disease'):
                tags['Disease'] = dict(tags['Disease'])
                clean_omim(tags['Disease'])
            n += 1
            yield (m, tags, str(o.get('pmid') or rec.get('id') or '')) if with_pmid else (m, tags)
    if not with_pmid:
        log('read', n, 'papers;', bad, 'unreadable lines')


def variant_label(ident, name, gene_names):
    base = name if not re.match(r'^#\d+#$', name or '') and name else None
    if base is None:
        m = re.match(r'tmVar:p\|(?:SUB|Allele)\|([A-Z,]*)\|(\d*)\|?([A-Z]*)', ident)
        base = (m.group(1) + m.group(2) + (('>' + m.group(3)) if m.group(3) else '')) if m else 'variant'
    g = re.search(r'CorrespondingGene:(\d+)', ident)
    sym = gene_names.get(g.group(1)) if g else None
    return base + (' (' + sym + ')' if sym else '')


# ----------------------------------------------------------------------------------------------
# 2  collection gaps
# ----------------------------------------------------------------------------------------------
def detect_gap(tagged, frac=0.35, minlen=2):
    """tagged[m] = papers with >=1 tag of this type. Returns (start, end) month indices of the longest run of
    months whose tagged count is < frac x the median of the surrounding +-8 months, or None."""
    nm = len(tagged)
    low = np.zeros(nm, bool)
    for m in range(6, nm - 2):
        w = np.r_[tagged[max(0, m - 8):m], tagged[m + 1:min(nm, m + 9)]]
        if len(w) and np.median(w) >= 50:
            low[m] = tagged[m] < frac * np.median(w)
    best, m = None, 0
    while m < nm:
        if low[m]:
            e = m
            while e + 1 < nm and low[e + 1]:
                e += 1
            if e - m + 1 >= minlen and (best is None or e - m > best[1] - best[0]):
                best = (m, e)
            m = e + 1
        else:
            m += 1
    if best:                 # widen the run while the months on either side are still clearly low
        s_, e_ = best
        ref = lambda m: np.median(np.r_[tagged[max(0, m - 12):max(0, s_ - 1)], tagged[e_ + 2:min(nm, e_ + 14)]]) if nm > 20 else 0
        r0 = ref(0)
        while s_ - 1 >= 6 and tagged[s_ - 1] < 0.75 * r0:
            s_ -= 1
        while e_ + 1 < nm - 2 and tagged[e_ + 1] < 0.75 * r0:
            e_ += 1
        best = (s_, e_)
    return best


def fill_gap(v, gap, ctx=3):
    s, e = gap
    out = np.asarray(v, float).copy()
    around = np.r_[out[max(0, s - ctx):s], out[e + 1:e + 1 + ctx]]
    if len(around) == 0:
        return out
    base = around.mean()
    out[s:e + 1] = np.maximum(out[s:e + 1], base)
    return out


# ----------------------------------------------------------------------------------------------
# 3  lifepath fits
# ----------------------------------------------------------------------------------------------
def sigmoid(x, x0, k, A):
    return A / (1 + np.exp(-k * (x - x0)))


def fit_sigmoid(values, padding):
    cum = np.cumsum(np.asarray(values, float))
    if cum[-1] == 0:
        return None
    mx = cum.max()
    y = np.r_[np.zeros(padding), cum] / mx
    x = np.linspace(0, 1, len(y))
    try:
        (x0, k, A), _ = curve_fit(sigmoid, x, y, p0=(x[-1] / 2 + .25, 10., 1.),
                                  bounds=((X0_BOUNDS[0], K_BOUNDS[0], A_BOUNDS[0]), (X0_BOUNDS[1], K_BOUNDS[1], A_BOUNDS[1])),
                                  maxfev=2000)
    except (RuntimeError, ValueError):
        return None
    return x0, k, A, mx


def phase_of(sat, dm):
    return 3 if sat < 1.05 else (2 if dm > 1 else (0 if dm < -1 else 1))


_G = {}


def _init(snaps, nm):
    _G['snaps'], _G['nm'] = snaps, nm


def fit_all_snapshots(vals):
    """vals: monthly counts (len nm). Returns one fit row (or None) per snapshot."""
    snaps, nm = _G['snaps'], _G['nm']
    npts = nm + PAD
    out = []
    for (y, mo) in snaps:
        n = (y - BASE_YEAR) * 12 + mo
        v = vals[:n]
        r = fit_sigmoid(v, npts - n) if v.sum() > 0 else None
        if r is None:
            out.append(None)
            continue
        x0, k, A, mx = r
        off = min(x0, 1.32) * (npts - 1) - (npts - n)
        days = round(off * 30.4375)
        ana = (datetime.date(y + (mo == 12), mo % 12 + 1, 1) - datetime.timedelta(days=1) - datetime.date(BASE_YEAR, 1, 1)).days
        out.append([days, round(math.log(k + 0.001), 3), int(mx), phase_of(A, (ana - days) / 30), round(x0, 4), round(k, 2), round(min(A, 50.0), 6)])
    return out


def run_fits(arrays, snaps, nm, workers):
    with Pool(workers, initializer=_init, initargs=(snaps, nm)) as p:
        return p.map(fit_all_snapshots, arrays, chunksize=16)


def snapshots(nm):
    last = nm - 1
    out = []
    for y in range(BASE_YEAR, BASE_YEAR + nm // 12 + 2):
        for mo in (6, 12):
            if (y - BASE_YEAR) * 12 + mo - 1 <= last:
                out.append((y, mo))
    ly, lm = BASE_YEAR + last // 12, last % 12 + 1
    if (ly, lm) not in out:
        out.append((ly, lm))
    return out


# ----------------------------------------------------------------------------------------------
# 4-6  wave groups, archetypes, assignment of small terms
# ----------------------------------------------------------------------------------------------
def quarterly(m, nq):
    return np.asarray(m, float)[:3 * nq].reshape(nq, 3).sum(1)


def dtw(a, b):
    n, m = len(a), len(b)
    cost = np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(-1))
    D = np.full((n + 1, m + 1), np.inf)
    D[0, 0] = 0
    for i in range(1, n + 1):
        Di, Dp, ci = D[i], D[i - 1], cost[i - 1]
        for j in range(1, m + 1):
            Di[j] = ci[j - 1] + min(Dp[j], Di[j - 1], Dp[j - 1])
    return D[n, m] / (n + m)


class Groups:
    """Wave groups + archetypes learned on the core diseases, reusable for small terms and other types."""

    def __init__(self, ids, filled, rawm, fits, nq, gapq, min_core, seed=0, wave_k=None, arch_k=3):
        self.nq, self.gapq = nq, gapq
        self.keep = [q for q in range(nq) if q not in gapq]
        tot = {i: filled[i].sum() for i in ids}
        self.core = [i for i in ids if tot[i] >= min_core and quarterly(filled[i], nq).max() > 0]
        Q = np.array([quarterly(filled[i], nq) for i in self.core])
        V = Q / Q.max(1, keepdims=True)
        # silhouette scan (sklearn.metrics.silhouette_score, euclidean = the metric k-means minimises) over k = 2..8
        self.sil = {}
        for k_ in range(2, min(8, len(self.core) - 1) + 1):
            self.sil[k_] = round(float(silhouette_score(V, KMeans(k_, n_init=50, random_state=seed).fit_predict(V))), 4)
        wk = WAVE_K if wave_k is None else wave_k
        if wk == 'auto':                                             # best silhouette among the k that have group names
            wk = max(WAVE_NAMES_BY_K, key=lambda k_: self.sil.get(k_, -1))
        K = self.K = int(wk)
        self.wave_names = WAVE_NAMES_BY_K[K]
        km = KMeans(K, n_init=50, random_state=seed).fit(V)
        log('wave-group silhouette by k', self.sil, '| using k =', K)
        cur = np.array([V[km.labels_ == c].mean(0) for c in range(K)])
        peak, late = cur.argmax(1), cur[:, -4:].mean(1)
        chronic = int(late.argmax())                                # most sustained = last
        rem = [c for c in range(K) if c != chronic]
        if K == 2:
            order = rem + [chronic]
        elif K == 3:
            acute = min(rem, key=lambda c: peak[c])
            order = [acute, [c for c in rem if c != acute][0], chronic]
        else:
            lateS = max(rem, key=lambda c: peak[c])
            rem2 = [c for c in rem if c != lateS]
            acute = min(rem2, key=lambda c: peak[c])
            broad = [c for c in rem2 if c != acute][0]
            order = [acute, broad, lateS, chronic]                  # canonical order = WAVE_NAMES
        remap = {c: k for k, c in enumerate(order)}
        self.wave = {i: remap[l] for i, l in zip(self.core, km.labels_)}
        self.V, self.cur = V, cur[order]
        self.tot = tot
        # archetypes from lifepath sequences of core terms
        seqs = {}
        for i in self.core:
            pts = [d for d in fits[i] if d and d[2] >= 5]
            if len(pts) >= 2:
                seqs[i] = np.c_[[BASE_YEAR + d[0] / 365.25 for d in pts], [d[1] for d in pts]]
        allp = np.vstack(list(seqs.values()))
        self.mx, self.sx = allp[:, 0].mean(), allp[:, 0].std() or 1
        self.my, self.sy = allp[:, 1].mean(), allp[:, 1].std() or 1
        self.seqs = {i: (s - [self.mx, self.my]) / [self.sx, self.sy] for i, s in seqs.items()}
        names = list(self.seqs)
        M = np.zeros((len(names), len(names)))
        for a in range(len(names)):
            for b in range(a + 1, len(names)):
                M[a, b] = M[b, a] = dtw(self.seqs[names[a]], self.seqs[names[b]])
        Zw = linkage(squareform(M), 'ward')
        # silhouette of the DTW + Ward archetypes for k = 2..8 (precomputed DTW distances), the counterpart of the k-means scan above
        self.sil_arch = {k_: round(float(silhouette_score(M, fcluster(Zw, k_, 'maxclust'), metric='precomputed')), 4) for k_ in range(2, min(8, len(names) - 1) + 1)}
        ak = arch_k
        if ak == 'auto':                                             # best silhouette among the k that have archetype names
            ak = max(ARCH_NAMES_BY_K, key=lambda k_: self.sil_arch.get(k_, -1))
        self.arch_k = int(ak)
        self.arch_names = ARCH_NAMES_BY_K[self.arch_k]
        lab = fcluster(Zw, self.arch_k, 'maxclust')
        log('archetype silhouette by k', self.sil_arch, '| using k =', self.arch_k)
        deact = {k: np.mean([np.mean([d[3] == 3 for d in fits[i] if d]) for i, l in zip(names, lab) if l == k]) for k in range(1, self.arch_k + 1)}
        od = sorted(deact, key=deact.get)                            # low deactivation -> last name (Late settling)
        amap = {c_: self.arch_k - 1 - r_ for r_, c_ in enumerate(od)}   # index into self.arch_names
        self.arch = {i: amap[l] for i, l in zip(names, lab)}
        self.arch_groups = {a: [i for i in names if self.arch[i] == a] for a in range(self.arch_k)}
        self.dist = M
        self.seq_ids = names
        # timing-model profiles over the kept quarters
        self.Pw = self._prof(self.cur)
        self.prior_w = np.bincount([self.wave[i] for i in self.core], minlength=self.K) / len(self.core)
        Va = {i: v for i, v in zip(self.core, V)}
        self.Pa = self._prof([np.mean([Va[i] for i in self.arch_groups[a] if i in Va], 0) for a in range(self.arch_k)])
        pa = np.array([len(self.arch_groups[a]) for a in range(self.arch_k)], float)
        self.prior_a = pa / pa.sum()
        log('core terms', len(self.core), '| wave sizes', np.bincount(list(self.wave.values()), minlength=self.K).tolist(),
            '| archetype sizes', pa.astype(int).tolist())

    def nearest(self, k=5):
        """For every core term with a lifepath sequence: its k nearest core terms by DTW distance, and the p10/p50/p90 of all pairwise distances."""
        M, nm = self.dist, self.seq_ids
        near = {}
        for a_, i in enumerate(nm):
            o = [j for j in np.argsort(M[a_]) if j != a_][:k]
            near[i] = [[nm[j], round(float(M[a_, j]), 3)] for j in o]
        iu = M[np.triu_indices(len(nm), 1)]
        return near, [round(float(x), 3) for x in np.percentile(iu, [10, 50, 90])]

    def _prof(self, P):
        P = np.maximum(np.asarray(P)[:, self.keep], 0) + 1e-3
        return P / P.sum(1, keepdims=True)

    @staticmethod
    def _post(counts, P, prior, scale=None):
        ll = counts @ np.log(P).T
        return (ll * scale[:, None] if scale is not None else ll) + np.log(prior)

    def _softmax(self, ll):
        return np.exp(ll - logsumexp(ll, axis=1, keepdims=True))

    def validate(self, raw_q, seed=0, reps=20, ns=(5, 10, 30, 50)):
        """Thinning test: how often the timing model recovers the core label when a term is cut to n papers."""
        rng = np.random.default_rng(seed)
        res = {'wave': {}, 'archetype': {}}
        for n in ns:
            for key, labels, P, prior in (('wave', self.wave, self.Pw, self.prior_w), ('archetype', self.arch, self.Pa, self.prior_a)):
                acc = []
                for _ in range(reps):
                    C, y = [], []
                    for i, l in labels.items():
                        row = raw_q[i][self.keep]
                        if row.sum() < n:
                            continue
                        C.append(rng.multinomial(n, row / row.sum()))
                        y.append(l)
                    if C:
                        acc.append((self._post(np.array(C), P, prior).argmax(1) == np.array(y)).mean())
                res[key][n] = round(float(np.mean(acc)), 3) if acc else None
        return res

    def assign(self, ids, raw_q, fits, skip_wave=(), skip_arch=()):
        """TENT rows [wave, archetype, wave certainty %, archetype certainty %, papers, path(1)/timing(0)]."""
        out = {}
        N = np.array([raw_q[i][self.keep] for i in ids], float)
        n = N.sum(1)
        sc = np.minimum(1, 50 / np.maximum(n, 1))
        llw, lla = self._post(N, self.Pw, self.prior_w), self._post(N, self.Pa, self.prior_a)
        pw, pa = self._softmax(self._post(N, self.Pw, self.prior_w, sc)), self._softmax(self._post(N, self.Pa, self.prior_a, sc))
        for j, i in enumerate(ids):
            gw = -1 if i in skip_wave else int(llw[j].argmax())
            cw = 100 if i in skip_wave else round(float(pw[j].max()) * 100)
            pts = [d for d in fits[i] if d and d[2] >= 5]
            if i in skip_arch:
                ga, ca, mode = -1, 100, 1
            elif len(pts) >= 3 and self.arch_groups:
                sq = (np.c_[[BASE_YEAR + d[0] / 365.25 for d in pts], [d[1] for d in pts]] - [self.mx, self.my]) / [self.sx, self.sy]
                dist = {a: np.mean([dtw(sq, self.seqs[k]) for k in g]) for a, g in self.arch_groups.items()}
                ga, ca, mode = min(dist, key=dist.get), round(float(pa[j].max()) * 100), 1
            else:
                ga, ca, mode = int(lla[j].argmax()), round(float(pa[j].max()) * 100), 0
            out[i] = [gw, ga, cw, ca, int(n[j]), mode]
        return out


def arch_seqs(ids, fits):
    """Lifepath sequences (standardised inflection date and ln steepness at each snapshot with 5+ papers) for terms with 2+ points."""
    S = {}
    for i in ids:
        pts = [d for d in fits[i] if d and d[2] >= 5]
        if len(pts) >= 2:
            S[i] = np.c_[[BASE_YEAR + d[0] / 365.25 for d in pts], [d[1] for d in pts]]
    if not S:
        return {}
    allp = np.vstack(list(S.values()))
    mx, sx, my, sy = allp[:, 0].mean(), allp[:, 0].std() or 1, allp[:, 1].mean(), allp[:, 1].std() or 1
    return {i: (s_ - [mx, my]) / [sx, sy] for i, s_ in S.items()}


def silhouette_scan(ids, raw_q, fits, seed=0):
    """Mean silhouette by k (2..8) of the k-means wave grouping and of the DTW + Ward archetype grouping for a set of terms."""
    Q = np.array([raw_q[i] for i in ids])
    ok = Q.max(1) > 0
    V = Q[ok] / Q[ok].max(1, keepdims=True)
    wave = {k_: round(float(silhouette_score(V, KMeans(k_, n_init=30, random_state=seed).fit_predict(V))), 4) for k_ in range(2, min(8, len(V) - 1) + 1)}
    S = arch_seqs(ids, fits)
    arch = {}
    if len(S) >= 8:
        nm_ = list(S)
        M_ = np.zeros((len(nm_), len(nm_)))
        for a_ in range(len(nm_)):
            for b_ in range(a_ + 1, len(nm_)):
                M_[a_, b_] = M_[b_, a_] = dtw(S[nm_[a_]], S[nm_[b_]])
        Z_ = linkage(squareform(M_), 'ward')
        arch = {k_: round(float(silhouette_score(M_, fcluster(Z_, k_, 'maxclust'), metric='precomputed')), 4) for k_ in range(2, min(8, len(nm_) - 1) + 1)}
    return {'n': int(ok.sum()), 'wave': wave, 'arch': arch}


def surprising_neighbours(G, dsets, mcat, k_pool=15, k_out=3, min_expected=10, alpha=0.05):
    """For each core term: of its k_pool nearest lifepaths (DTW), the ones in a different MeSH category that are mentioned together in
    clearly fewer papers than chance would give: at least min_expected papers expected and a Poisson lower-tail probability of the observed count
    below alpha. Entry: [id, dtw distance, papers together, expected papers]."""
    from scipy import sparse
    from scipy.stats import poisson
    idx = {i: j for j, i in enumerate(G.seq_ids)}
    r_, c_ = [], []
    for p_, ds in enumerate(dsets):
        for t in ds:
            j = idx.get(t)
            if j is not None:
                r_.append(p_); c_.append(j)
    N = len(dsets)
    X = sparse.csr_matrix((np.ones(len(r_)), (r_, c_)), shape=(N, len(idx)))
    C = (X.T @ X).toarray()
    n = np.diag(C)
    out = {}
    for a_, i in enumerate(G.seq_ids):
        res = []
        for j in [j for j in np.argsort(G.dist[a_]) if j != a_][:k_pool]:
            exp = n[a_] * n[j] / N
            if exp >= min_expected and poisson.cdf(C[a_, j], exp) < alpha and not (set(mcat.get(i, [])) & set(mcat.get(G.seq_ids[j], []))):
                res.append([G.seq_ids[j], round(float(G.dist[a_, j]), 3), int(C[a_, j]), round(float(exp), 1)])
        out[i] = res[:k_out]
    return out


def cutoff_scan(raw, nq, cands, K, reps=20, seed=0):
    """For each minimum-papers cutoff: number of terms, share of all mentions, silhouette of the K-group k-means split, and how stable
    that split is: split-half ARI (papers halved at random, both halves clustered), bootstrap ARI (papers redrawn from the term's own
    monthly profile, against the full-data split) and seed ARI (different k-means starts)."""
    rng = np.random.default_rng(seed)
    tot = {i: float(v.sum()) for i, v in raw.items()}
    allm = sum(tot.values())
    quart = lambda m: (lambda q: q / q.max() if q.max() > 0 else q)(quarterly(m, nq))
    out = []
    for c in cands:
        ids = [i for i in raw if tot[i] >= c and quarterly(raw[i], nq).max() > 0]
        if len(ids) <= K + 1:
            continue
        V = np.array([quart(raw[i]) for i in ids])
        ref = KMeans(K, n_init=30, random_state=seed).fit(V)
        sil = float(silhouette_score(V, ref.labels_))
        seedari = np.mean([adjusted_rand_score(ref.labels_, KMeans(K, n_init=30, random_state=s_).fit_predict(V)) for s_ in range(1, 7)])
        sh, bs = [], []
        for rep_ in range(reps):
            A_, B_, T_ = [], [], []
            for i in ids:
                m = raw[i].astype(int)
                h = rng.binomial(m, 0.5)
                A_.append(quart(h)); B_.append(quart(m - h)); T_.append(quart(rng.multinomial(int(m.sum()), m / m.sum())))
            sh.append(adjusted_rand_score(KMeans(K, n_init=10, random_state=rep_).fit_predict(np.array(A_)), KMeans(K, n_init=10, random_state=rep_).fit_predict(np.array(B_))))
            bs.append(adjusted_rand_score(ref.labels_, KMeans(K, n_init=10, random_state=rep_).fit_predict(np.array(T_))))
        out.append({'c': c, 'n': len(ids), 'cov': round(sum(tot[i] for i in ids) / allm, 4), 'sil': round(sil, 4),
                    'split': round(float(np.mean(sh)), 3), 'boot': round(float(np.mean(bs)), 3), 'seed': round(float(seedari), 3)})
    return out


# ----------------------------------------------------------------------------------------------
# helpers to build rows
# ----------------------------------------------------------------------------------------------
def make_rows(ids, names, filled, raw, fits_f, fits_o, snaps, gap):
    rows = []
    for i in ids:
        if gap:
            s = gap[0]
            orig = [([d[0], d[6]] if d else None) for d in fits_o[i]]
        else:
            orig = [([d[0], d[6]] if d else None) for d in fits_f[i]]
        rows.append([i, names[i], [int(round(x)) for x in filled[i]], fits_f[i], orig, [int(x) for x in raw[i]]])
    rows.sort(key=lambda r: -sum(r[5]))
    return rows


def write_page(outdir, name, obj, js):
    with open(os.path.join(outdir, name + '.json'), 'w', encoding='utf-8') as f:
        json.dump(obj, f, separators=(',', ':'), ensure_ascii=False)
    if js:
        with open(os.path.join(outdir, name + '.js'), 'w', encoding='utf-8') as f:
            f.write('window.DASH_DATA=window.DASH_DATA||{};window.DASH_DATA[%s]=' % json.dumps(name))
            json.dump(obj, f, separators=(',', ':'), ensure_ascii=False)
            f.write(';')


def write_csv(path, header, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ----------------------------------------------------------------------------------------------
# 10  network of variants, cell lines, chromosomes / regions, genes and symptoms/comorbidities
# ----------------------------------------------------------------------------------------------
NET_CORE = ['Variant', 'CellLine', 'Chromosome', 'GenomicRegion']
NET_TYPES = NET_CORE + ['Gene', 'Disease', 'Chemical']


def build_network(path, start, end, nm=None):
    """Papers that name at least one variant, cell line, chromosome or genomic region, with every gene, disease and
    chemical they name. The page links two terms when they are named in the same paper(s); the edge weight is the number
    of papers. Returns {'nodes': [[type, id, name, papers]], 'papers': [[pmid, 'YYYY-MM', [node index, ...]]], 'N': ...}."""
    idx, nodes, rows, gene_names = {}, [], [], {}
    keep = []
    for m, tags, pmid in read_papers(path, start, end, with_pmid=True):
        if nm is not None and m >= nm:
            continue
        for k, v in tags.get('Gene', {}).items():
            gene_names[k] = v
        if any(tags.get(t) for t in NET_CORE):
            keep.append((m, pmid, {t: dict(tags[t]) for t in NET_TYPES if tags.get(t)}))
    cnt = collections.Counter()
    for _, _, tg in keep:
        for t, d in tg.items():
            for ident in d:
                cnt[(t, ident)] += 1
    for m, pmid, tg in keep:
        ids = []
        for t in NET_TYPES:
            for ident, name in tg.get(t, {}).items():
                key = (t, ident)
                if key not in idx:
                    idx[key] = len(nodes)
                    label = variant_label(ident, name, gene_names) if t == 'Variant' else name
                    nodes.append([t, ident, label, cnt[key]])
                ids.append(idx[key])
        rows.append([pmid, mlabel(m), ids])
    log('network:', len(rows), 'papers,', len(nodes), 'terms')
    return {'nodes': nodes, 'papers': rows, 'N': len(rows)}


def mon_name(k):
    return datetime.date(BASE_YEAR + k // 12, k % 12 + 1, 1).strftime('%B %Y')


# ----------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input')
    ap.add_argument('-o', '--out', default='data')
    ap.add_argument('--start', default='%d-01' % BASE_YEAR, help='first month (default 2020-01; the dashboard axis starts there)')
    ap.add_argument('--end', default=None, help='last month YYYY-MM (default: latest month in the file)')
    ap.add_argument('--types', default=','.join(DEFAULT_TYPES), help='entity types to build pages for (Disease is always needed for groups)')
    ap.add_argument('--min-papers', action='append', default=[], metavar='Type=N', help='override minimum papers per type')
    ap.add_argument('--gap', nargs=2, metavar=('START', 'END'), help='force a collection gap for diseases, e.g. 2024-04 2024-08')
    ap.add_argument('--fill-gap', action='store_true', help='detect a collection gap (e.g. the 2024 dip in tagged papers) and fill it from the months around it; off by default, so the data are shown as collected, gap included')
    ap.add_argument('--no-gap', action='store_true', help='kept for compatibility: gap filling is already off unless --fill-gap is given')
    ap.add_argument('--wave-k', default='4', choices=('2', '3', '4', 'auto'), help='number of wave groups (k-means); default 4; "auto" picks the best mean silhouette (sklearn silhouette_score) among 2-4, which is usually k=2')
    ap.add_argument('--omim', default='fold', choices=('fold', 'drop', 'keep'), help='disease tags with an OMIM id (terms the tagger could not map to MeSH): fold the clear matches into their MeSH term and drop the rest (default), drop all, or keep them as tagged; see OMIM_TO_MESH')
    ap.add_argument('--min-core', type=int, default=100, help='papers a disease needs to be a core term for wave groups / archetypes')
    ap.add_argument('--min-core-chem', type=int, default=30, help='papers a chemical needs to be a core term for its own wave groups / archetypes (chemicals are rarer than diseases; the cutoff scan is written to manifest.json chemical_groups)')
    ap.add_argument('--chem-wave-k', default='auto', choices=('2', '3', '4', 'auto'), help='number of wave groups for chemicals; default auto = best mean silhouette among 2-4')
    ap.add_argument('--chem-arch-k', default='auto', choices=('2', '3', 'auto'), help='number of lifepath archetypes for chemicals; default auto = best mean silhouette among 2-3')
    ap.add_argument('--final-month-complete', action='store_true', help='treat the last month as complete (default: it is partial and left out of quarterly analyses)')
    ap.add_argument('--mesh-tree', help='MeSH tree JSON ({"tree":[{"mesh_id":..,"first":"Diseases.Infections.Respiratory Tract Infections..."}]}) for the category page')
    ap.add_argument('--clusters-csv', help='CSV with columns term,cluster to use instead of the built-in share clustering')
    ap.add_argument('--k-share', type=int, default=6, help='number of share-profile clusters')
    ap.add_argument('--k-cooc', type=int, default=6, help='number of disease-chemical co-clusters')
    ap.add_argument('--labels', help='JSON with optional names: {"cooc":{"0":{"name":..,"desc":..}},"share":{"1":"name"}}')
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 0))
    ap.add_argument('--no-network', action='store_true', help='skip the network and cell-line-use pages')
    ap.add_argument('--no-js', action='store_true')
    a = ap.parse_args()
    set_wave_k(a.wave_k if a.wave_k == 'auto' else int(a.wave_k))
    global OMIM_MODE
    OMIM_MODE = a.omim
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(os.path.join(a.out, 'tables'), exist_ok=True)
    types = [t for t in a.types.split(',') if t]
    if 'Disease' not in types:
        types.insert(0, 'Disease')
    minp = dict(DEFAULT_MIN)
    for s in a.min_papers:
        k, v = s.split('=')
        minp[k] = int(v)
    labels = json.load(open(a.labels)) if a.labels else {}

    # ---- 1 read + monthly counts --------------------------------------------------------------
    start = mindex(a.start)
    assert start == 0, 'the dashboard axis starts in January %d (see BASE_YEAR)' % BASE_YEAR
    end = mindex(a.end) if a.end else None
    counts = collections.defaultdict(lambda: collections.defaultdict(lambda: np.zeros(MAXM, np.int32)))
    names = collections.defaultdict(dict)
    per_month = np.zeros(MAXM, np.int64)
    tagged = collections.defaultdict(lambda: np.zeros(MAXM, np.int64))
    papers = []         # for co-mentions: (month, disease ids, chemical ids)
    dsets = []          # disease ids of every paper that carries a disease tag (for the surprising-neighbours lists)
    want = set(types) | {'Gene'}
    for m, tags in read_papers(a.input, start, end):
        per_month[m] += 1
        if tags.get('Disease'):
            dsets.append(tuple(tags['Disease']))
        for t, d in tags.items():
            if t not in want or not d:
                continue
            tagged[t][m] += 1
            ct, nt = counts[t], names[t]
            for ident, nm_ in d.items():
                ct[ident][m] += 1
                if ident not in nt:
                    nt[ident] = nm_
        if tags.get('Disease') and tags.get('Chemical') is not None:
            papers.append((m, tuple(tags.get('Disease', {})), tuple(tags.get('Chemical', {}))))
        elif tags.get('Chemical'):
            papers.append((m, (), tuple(tags['Chemical'])))
    if end is None:          # latest month, ignoring stray future-dated records after a run of 2+ empty months
        nz = np.nonzero(per_month)[0]
        last = int(nz[0])
        for m_ in nz[1:]:
            if m_ - last > 2:
                break
            last = int(m_)
    else:
        last = end
    nm = last + 1
    per_month = per_month[:nm]
    log('months', mlabel(0), '..', mlabel(last), '=', nm, '| papers', int(per_month.sum()))
    log('OMIM disease tags (%s): %d folded into MeSH, %d dropped' % (OMIM_MODE, OMIM_STATS['folded'], OMIM_STATS['dropped']))
    snaps = snapshots(nm)
    partial = not a.final_month_complete
    am = nm - 1 if partial else nm
    nq = am // 3
    # ---- 2 gaps -----------------------------------------------------------------------------------
    gaps = {}
    if a.fill_gap and not a.no_gap:
        if a.gap:
            gaps['Disease'] = (mindex(a.gap[0]), mindex(a.gap[1]))
        for t in types:
            if t in tagged and t not in gaps:
                g = detect_gap(tagged[t][:nm])
                if g:
                    gaps[t] = g
    for t, g in gaps.items():
        log('collection gap in', t, 'tags:', mlabel(g[0]), '..', mlabel(g[1]), '(filled)' if t == 'Disease' else '(detected; filled too)')
    dip = {}                                                      # dips in tagged papers are reported on the pages whether or not they are filled
    for t in ('Disease', 'Chemical'):
        if t in tagged:
            g_ = gaps.get(t) or detect_gap(tagged[t][:nm])
            if g_:
                dip[t] = g_
    gapq = set()
    if 'Disease' in gaps:
        gapq = set(range(gaps['Disease'][0] // 3, gaps['Disease'][1] // 3 + 1))

    manifest = {'version': 1, 'generated': datetime.datetime.now().isoformat(timespec='seconds'), 'source': os.path.basename(a.input),
                'base_year': BASE_YEAR, 'first_month': mlabel(0), 'last_month': mlabel(last), 'nm': nm, 'pad': PAD, 'nq': nq,
                'snaps': [list(s) for s in snaps], 'papers': int(per_month.sum()), 'papers_per_month': per_month.tolist(),
                'gaps': {t: [g[0], g[1]] for t, g in gaps.items()}, 'tag_dips': {t: [mlabel(g[0]), mlabel(g[1])] for t, g in dip.items()}, 'final_month_partial': partial, 'omim': {'mode': OMIM_MODE, 'folded_tags': OMIM_STATS['folded'], 'dropped_tags': OMIM_STATS['dropped']}, 'pages': []}

    # ---- entity arrays, fits -----------------------------------------------------------------------
    ents = {}
    for t in set(types) | {'Gene'}:
        if t not in counts:
            continue
        ids = [i for i, v in counts[t].items() if v[:nm].sum() >= minp.get(t, 5)]
        if t == 'Gene' and t not in types:
            continue
        ents[t] = ids
    gene_names = dict(names.get('Gene', {}))
    page_data = {}
    filled_all, raw_all, fits_all, orig_all = {}, {}, {}, {}
    for t, ids in ents.items():
        raw = {i: counts[t][i][:nm].astype(float) for i in ids}
        g = gaps.get(t)
        filled = {i: fill_gap(raw[i], g) for i in ids} if g else raw
        nmap = {i: (variant_label(i, names[t][i], gene_names) if t == 'Variant' else names[t][i]) for i in ids}
        log('fitting', t, len(ids), 'entities x', len(snaps), 'snapshots' + (' x 2 runs' if g else ''))
        fits_f = dict(zip(ids, run_fits([filled[i] for i in ids], snaps, nm, a.workers)))
        fits_o = None
        if g:
            fo = run_fits([raw[i] for i in ids], snaps, nm, a.workers)
            fits_o = dict(zip(ids, fo))
            for i in ids:          # snapshots before the gap are identical by construction
                for k, (y, mo) in enumerate(snaps):
                    if (y - BASE_YEAR) * 12 + mo <= g[0]:
                        fits_o[i][k] = fits_f[i][k]
        filled_all[t], raw_all[t], fits_all[t], orig_all[t] = filled, raw, fits_f, fits_o
        page_data[t] = (ids, nmap)

    # ---- 4-6 groups ---------------------------------------------------------------------------------
    dids, dnames = page_data['Disease']
    arch_fits = orig_all['Disease'] or fits_all['Disease']      # archetypes are built on the fits as collected (before any gap fill)
    G = Groups(dids, filled_all['Disease'], raw_all['Disease'], arch_fits, nq, gapq, a.min_core)
    set_wave_k(G.K)                                              # resolves --wave-k auto for the rest of the run
    rawq = {t: {i: quarterly(raw_all[t][i], nq) for i in ents[t]} for t in ents}
    validation = G.validate(rawq['Disease'])
    log('thinning validation', validation)
    manifest['validation'] = validation
    manifest['wave_silhouette'] = {'metric': 'sklearn.metrics.silhouette_score, euclidean, k-means on peak-scaled quarterly volumes of core terms', 'k_used': WAVE_K, 'by_k': G.sil}
    manifest['archetype_silhouette'] = {'metric': 'sklearn.metrics.silhouette_score, metric=precomputed, DTW distances between core-term lifepath sequences, Ward linkage cut at k', 'k_used': 3, 'by_k': G.sil_arch}
    small = [i for i in dids if i not in G.wave and sum(raw_all['Disease'][i]) >= 5]
    tent_d = G.assign(small, rawq['Disease'], arch_fits, skip_wave=set(), skip_arch=set(G.arch))
    # core terms that have no archetype (too few fits) still need a row
    wave_d = {i: [G.wave[i], G.arch.get(i, -1)] for i in G.core}
    tent_d = {i: v for i, v in tent_d.items()}
    for i in G.core:
        if i not in G.arch:
            tent_d[i] = [-1, *G.assign([i], rawq['Disease'], arch_fits)[i][1:]]
    qn = lambda v: (np.asarray(v, float) / max(np.max(v), 1e-9))
    top4 = lambda idl, nmap, totals: [nmap[i] for i in sorted(idl, key=lambda i: -totals[i])[:4]]
    tot_f = {i: float(filled_all['Disease'][i].sum()) for i in dids}
    wcurve = [[round(float(x), 3) for x in G.cur[c]] for c in range(WAVE_K)]
    wmeta = [{'n': sum(1 for v in G.wave.values() if v == c), 'ex': top4([i for i, v in G.wave.items() if v == c], dnames, tot_f)} for c in range(WAVE_K)]
    # wave / archetype csv
    rows_csv = []
    for i in dids:
        if i in G.wave:
            wv, wc, an, ac, basis = WAVE_NAMES[G.wave[i]], 100, (ARCH_NAMES[G.arch[i]] if i in G.arch else ''), 100, 'core'
        elif i in tent_d:
            e = tent_d[i]
            wv, wc, an, ac, basis = WAVE_NAMES[e[0]] if e[0] >= 0 else '', e[2], ARCH_NAMES[e[1]] if e[1] >= 0 else '', e[3], 'tentative'
        else:
            continue
        rows_csv.append((i, dnames[i], int(sum(raw_all['Disease'][i])), wv, wc, an, ac, basis))
    rows_csv.sort(key=lambda r: -r[2])
    write_csv(os.path.join(a.out, 'tables', 'disease_groups.csv'), ['entity_id', 'term', 'papers', 'wave_group', 'wave_certainty_pct', 'archetype', 'archetype_certainty_pct', 'basis'], rows_csv)

    # ---- pages: diseases ----------------------------------------------------------------------------------
    gd = gaps.get('Disease')
    # MeSH categories
    mcat, catpage = None, None
    if a.mesh_tree:
        T = json.load(open(a.mesh_tree))
        T = T['tree'] if isinstance(T, dict) else T
        paths = collections.defaultdict(set)
        for x in T:
            p = x['first'].split('.')
            if p[0] == 'Diseases' and len(p) > 1:
                paths[x['mesh_id']].add(tuple(p[1:]))
        UN = 'Outside the Diseases tree (mental disorders, OMIM and supplementary concepts)'
        mcat, L2, L3, n2, n3 = {}, {}, {}, collections.Counter(), collections.Counter()
        for i in dids:
            ps = paths.get(i.replace('MESH:', ''))
            m = filled_all['Disease'][i]
            if not ps:
                mcat[i] = [UN]
                L2[UN] = L2.get(UN, 0) + m
                n2[UN] += 1
                continue
            c2 = sorted({p[0] for p in ps})
            mcat[i] = c2
            for c in c2:
                L2[c] = L2.get(c, 0) + m
                n2[c] += 1
            for c in {(p[0], p[1]) for p in ps if len(p) >= 2}:
                L3[c] = L3.get(c, 0) + m
        cats = [('L2:' + c, c, 2, v) for c, v in L2.items()] + [('L3:%s|%s' % c, '%s › %s' % c, 3, v) for c, v in L3.items() if v.sum() >= 30]
        log('fitting', len(cats), 'MeSH categories')
        fc = run_fits([v for _, _, _, v in cats], snaps, nm, a.workers)
        rows = [[k, n_, [int(round(x)) for x in v], f, lv] for (k, n_, lv, v), f in zip(cats, fc)]
        rows.sort(key=lambda r: -sum(r[2]))
        catpage = rows

    sub_gap = ''
    if not gd and 'Disease' in dip:
        s_, e_ = dip['Disease']
        gtag, pm = tagged['Disease'][:nm], per_month[:nm]
        mid = (s_ + e_) // 2
        typ = int(np.median(np.r_[gtag[max(0, s_ - 6):s_], gtag[e_ + 1:e_ + 7]]))
        ptyp = int(np.median(np.r_[pm[max(0, s_ - 6):s_], pm[e_ + 1:e_ + 7]]))
        sub_gap = ('Disease counts dip from %s to %s. The collection holds about %s papers a month before and after, but only %d papers in %s carry a disease tag (against about %s in a normal month)%s. '
                   'So the dip is in the tagging, which looks like a gap in the PubTator disease annotations, not a drop in publishing. Counts are shown as collected, so every symptom and comorbidity curve has a dip there, and fits that cross it can read as saturating early. '
                   'Run build_dashboard_data.py with --fill-gap to fill those months from the months around them. ' %
                   (mon_name(s_), mon_name(e_), '{:,}'.format(ptyp), int(gtag[mid]), mon_name(mid).split()[0], '{:,}'.format(typ),
                    '; chemical tags show no such dip' if 'Chemical' not in dip else ''))
    if gd:
        s_, e_ = gd
        gtag = tagged['Disease'][:nm]
        typical = int(np.median(np.r_[gtag[max(0, s_ - 6):s_], gtag[e_ + 1:e_ + 7]]))
        mid = (s_ + e_) // 2
        sub_gap = ('Papers carrying disease tags dip sharply from %s to %s (%d in %s against about %s in a normal month), which looks like a collection gap. '
                   'Here those %d months are filled from each disease’s own rate in the months around the gap, and every snapshot is refit. '
                   'Dashed rings mark diseases whose fit-rule phase differs from the original run; nothing before %s changes. ' %
                   (mon_name(s_), mon_name(e_), int(gtag[mid]), mon_name(mid).split()[0], '{:,}'.format(typical), e_ - s_ + 1, mon_name(s_)))
    near_d, dref_d = G.nearest()
    surp_d = surprising_neighbours(G, dsets, mcat or {})
    cfg_d = {'id': 'diseases', 'kind': 'diseases', 'title': 'Long COVID lifepaths', 'noun': 'disease', 'nouns': 'diseases', 'ph': 'e.g. fatigue, long covid, myalgia', 'wn': WAVE_NAMES, 'an': ARCH_NAMES, 
             'sub': sub_gap + '<b>Color by</b> switches to the wave groups from the paper: terms grouped by how their publication volume moved (' + {2:'two',3:'three',4:'four'}[WAVE_K] + ' count-based groups' +
             (', built on the gap-filled counts' if gd else '') + ') or by the shape of their lifepath (three archetypes, built on the %s fits).' % ('original' if gd else 'snapshot'),
             'minopts': [0, 10, 100, 1000], 'minP': 10, 'gap': list(gd) if gd else None, 'waves': True, 'core': True, 'cats': bool(mcat), 'minCore': a.min_core, 'rx': [2, 10], 'x1': 3100, 'dref': dref_d}
    disease_rows = make_rows(dids, dnames, filled_all['Disease'], raw_all['Disease'], fits_all['Disease'], orig_all['Disease'], snaps, gd)
    write_page(a.out, 'diseases', {'cfg': cfg_d, 'rows': disease_rows, 'mcat': mcat or {}, 'wave': wave_d, 'tent': tent_d, 'wcurve': wcurve, 'wmeta': wmeta, 'near': near_d, 'surp': surp_d}, not a.no_js)
    manifest['pages'].append({'id': 'diseases', 'nav': 'Symptoms/comorbidities', 'kind': 'diseases', 'n': len(disease_rows)})

    if catpage:
        ncat = sum(1 for r in catpage if r[4] == 2)
        cfg_c = {'id': 'categories', 'kind': 'categories', 'title': 'Top MeSH category lifepaths', 'noun': 'category', 'nouns': 'categories', 'ph': 'e.g. Infections, Nervous System',
                 'sub': 'Each line with dots is one higher-level MeSH category, made by adding up the monthly mentions of every term that sits under it in the MeSH tree. The categories are the second level of the tree path (the level below "Diseases"), for example Infections or Nervous System Diseases, and the subcategories are the third level. '
                        'A term with several tree paths is counted in each of its categories, so categories overlap; terms that are not in the Diseases tree are added up as one separate category. Phases, thresholds and views work as for single symptoms/comorbidities. A category curve is a sum, so it mixes the timing of its member terms: read it as when the area as a whole drew attention.',
                 'minopts': [0, 100, 1000, 5000], 'minP': 0, 'gap': None, 'waves': False, 'levels': True, 'rx': [0, 8], 'x1': 3300}
        write_page(a.out, 'categories', {'cfg': cfg_c, 'rows': [r[:4] for r in catpage], 'lvl': [r[4] for r in catpage]}, not a.no_js)
        manifest['pages'].append({'id': 'categories', 'nav': 'MeSH categories', 'kind': 'categories', 'n': len(catpage)})

    # ---- share-profile clusters ----------------------------------------------------------------------------
    clus = None
    core = G.core
    if a.clusters_csv:
        byname = {dnames[i]: i for i in dids}
        cl = collections.OrderedDict()
        for r in csv.DictReader(open(a.clusters_csv, encoding='utf-8')):
            t = byname.get(r.get('term')) or (r.get('term') if r.get('term') in dnames else None)
            if t:
                cl.setdefault(r['cluster'], []).append(t)
    else:
        ok = np.ones(nm, bool)
        if gd:
            ok[gd[0]:gd[1] + 1] = False
        ok[:3] = False
        ok[am:] = False
        pm = np.maximum(per_month.astype(float), 1)
        S = np.array([raw_all['Disease'][i] / pm for i in core])[:, ok]
        k5 = np.ones(5) / 5
        S = np.array([np.convolve(r, k5, 'same') for r in S])
        Z = (S - S.mean(1, keepdims=True)) / np.maximum(S.std(1, keepdims=True), 1e-12)
        lab = fcluster(linkage(Z, 'ward'), a.k_share, 'maxclust')
        cl = collections.OrderedDict()
        order = sorted(set(lab), key=lambda c: int(np.argmax(Z[lab == c].mean(0))))   # by timing of the mean peak
        for c in order:
            cl['tmp%d' % c] = [core[j] for j in np.where(lab == c)[0]]
    if cl:
        crow, cextra = [], {}
        pmv = np.maximum(per_month.astype(float), 1)
        clnames = {}
        for n_, (k, mem) in enumerate(cl.items()):
            mem = sorted(mem, key=lambda i: -raw_all['Disease'][i].sum())
            nmc = labels.get('share', {}).get(str(n_)) or ('%d. %s' % (n_ + 1, dnames[mem[0]]) if k.startswith('tmp') else k)
            clnames[k] = nmc
            v = sum(filled_all['Disease'][i] for i in mem)
            crow.append([nmc, nmc, [int(round(x)) for x in v], None, 0])
        fc = run_fits([np.asarray(r[2], float) for r in crow], snaps, nm, a.workers)
        for r, f, (k, mem) in zip(crow, fc, cl.items()):
            mem = sorted(mem, key=lambda i: -raw_all['Disease'][i].sum())
            r[3] = f
            cextra[r[0]] = {'share': [round(float(x), 4) for x in np.array(r[2]) / pmv], 'mem': [[dnames[i], int(raw_all['Disease'][i].sum())] for i in mem], 'n': len(mem)}
        cfg_s = {'id': 'clusters', 'kind': 'clusters', 'title': 'Share-profile clusters', 'noun': 'cluster', 'nouns': 'clusters', 'ph': 'e.g. acute, persistent',
                 'sub': 'Terms were grouped by the shape of their share of all papers over time (the fraction of the papers of each month that mention the term), using the %d symptom and comorbidity terms (PubTator disease tags) with %d or more papers. Chemicals, genes and other tag types are not included. Each line with dots here is one of the resulting clusters, with the monthly mentions of its member terms added up and run through the same sigmoid fit and phase rule as single symptoms/comorbidities. The chart below the plot shows how each group\'s share of the literature moved over time and which terms belong to it. A cluster curve is a sum, so its phase describes the group as a whole, not any one member.' % (sum(len(m) for m in cl.values()), a.min_core),
                 'minopts': [0], 'minP': 0, 'gap': None, 'waves': False, 'extra': True, 'rx': [6, 11], 'x1': 3300}
        write_page(a.out, 'clusters', {'cfg': cfg_s, 'rows': crow, 'extra': cextra, 'months': nm, 'papers_per_month': per_month.tolist()}, not a.no_js)
        manifest['pages'].append({'id': 'clusters', 'nav': 'Share clusters', 'kind': 'clusters', 'n': len(crow)})
        write_csv(os.path.join(a.out, 'tables', 'share_clusters.csv'), ['cluster', 'term', 'papers'], [(r[0], t, n_) for r in crow for t, n_ in cextra[r[0]]['mem']])

    # ---- other entity pages ---------------------------------------------------------------------------------------
    for pid, nav_, tlist, noun, nouns, ph, mopts in PAGE_TYPES:
        tl = [t for t in tlist if t in ents and t in types]
        if not tl:
            continue
        rows, ids_all, nm_all = [], [], {}
        for t in tl:
            ids, nmap = page_data[t]
            rows += make_rows(ids, nmap, filled_all[t], raw_all[t], fits_all[t], orig_all[t], snaps, gaps.get(t))
        if not rows:
            continue
        tots = sum(sum(r[5]) for r in rows)
        cfg = {'wn': WAVE_NAMES, 'an': ARCH_NAMES, 'id': pid, 'kind': 'entities', 'title': nav_ + ' lifepaths' if pid != 'chromosomes' else 'Chromosome and genomic-region lifepaths', 'noun': noun, 'nouns': nouns, 'ph': ph,
               'minopts': mopts, 'minP': 0, 'gap': None, 'waves': pid == 'chemicals', 'rx': [1.2, 10] if pid == 'chemicals' else [0, 8], 'x1': 3100 if pid == 'chemicals' else 3300}
        if pid == 'chemicals':
            cfg.update({'lists': True, 'minN': minp['Chemical']})
        sparse = ' Each term appears in very few papers, so a fitted curve rests on a handful of points: read these as when a term turned up, not as a mature growth curve. Most terms sit in the “Deactivated” phase simply because the fit saturates after one or two papers.'
        if pid == 'chemicals':
            cids = [r[0] for r in rows]
            nmc = {r[0]: r[1] for r in rows}
            tc = {r[0]: sum(r[5]) for r in rows}
            ckw = {'wave_k': a.chem_wave_k if a.chem_wave_k == 'auto' else int(a.chem_wave_k),
                   'arch_k': a.chem_arch_k if a.chem_arch_k == 'auto' else int(a.chem_arch_k)}
            GC = Groups(cids, filled_all['Chemical'], raw_all['Chemical'], orig_all['Chemical'] or fits_all['Chemical'], nq, gapq, a.min_core_chem, **ckw)
            cval = GC.validate(rawq['Chemical'], ns=(5, 10, 20, 30))
            log('chemical thinning validation', cval)
            cands = sorted({10, 20, 30, 50, 100, a.min_core_chem})
            cscan = cutoff_scan(raw_all['Chemical'], nq, cands, GC.K)
            log('chemical cutoff scan', cscan)
            small_c = [i for i in cids if i not in GC.wave]
            tent_c = GC.assign(small_c, rawq['Chemical'], fits_all['Chemical'], skip_wave=set(), skip_arch=set(GC.arch))
            for i in GC.core:
                if i not in GC.arch:
                    tent_c[i] = [-1, *GC.assign([i], rawq['Chemical'], fits_all['Chemical'])[i][1:]]
            wave_c = {i: [GC.wave[i], GC.arch.get(i, -1)] for i in GC.core}
            wc = [[round(float(x), 3) for x in GC.cur[c]] for c in range(GC.K)]
            wm = [{'n': sum(1 for v in GC.wave.values() if v == c), 'ex': top4([i for i, v in GC.wave.items() if v == c], nmc, tc)} for c in range(GC.K)]
            cshare = round(sum(tc[i] for i in GC.core) / max(sum(tc.values()), 1), 3)
            manifest['chemical_groups'] = {'min_core': a.min_core_chem, 'n_core': len(GC.core), 'share_of_mentions': cshare,
                                           'wave_k': GC.K, 'arch_k': GC.arch_k, 'wave_names': GC.wave_names, 'arch_names': GC.arch_names,
                                           'wave_silhouette': GC.sil, 'archetype_silhouette': GC.sil_arch, 'validation': cval, 'cutoffs': cscan,
                                           'wave_sizes': [m['n'] for m in wm], 'arch_sizes': [len(GC.arch_groups[x]) for x in range(GC.arch_k)]}
            near_c, dref_c = GC.nearest()
            cfg.update({'wn': GC.wave_names, 'an': GC.arch_names, 'core': True, 'minCore': a.min_core_chem, 'rx': [1.2, 10], 'dref': dref_c})
            cfg['sub'] = ('Each line with dots is one chemical (MeSH substance); each dot is its position at one half-year snapshot. Counts are small (%s mentions across the %d chemicals with %d or more papers), so most fits rest on few papers. '
                          '<b>Color by</b> switches to wave groups and lifepath archetypes learned on the chemicals themselves: the %d chemicals with %d or more papers (%d%% of all chemical mentions) were grouped with the same k-means and DTW procedures as the symptoms/comorbidities, '
                          'and the number of groups was chosen by silhouette (%s wave groups, %s archetypes; the table below shows how the cutoff was chosen). Chemicals below the cutoff are assigned to the nearest group by a timing model; fainter means less certain. '
                          'Thinning tests on the core chemicals put this at about %s%% correct at 5 papers and %s%% at 30 (%d%% by chance), so read the small-term groups as tentative.%s '
                          'Mentions tagged without an identifier are left out.') % ('{:,}'.format(sum(tc.values())), len(rows), minp['Chemical'], len(GC.core), a.min_core_chem, round(100 * cshare),
                          NUM_WORD[GC.K], NUM_WORD[GC.arch_k], round(100 * (cval['wave'].get(5) or 0)), round(100 * (cval['wave'].get(30) or 0)), round(100 / GC.K),
                          ' Chemical tagging shows no dip.' if 'Chemical' not in dip else '')
            write_page(a.out, pid, {'cfg': cfg, 'rows': [r[:4] + [r[4], r[5]] for r in rows], 'wave': wave_c, 'tent': tent_c, 'wcurve': wc, 'wmeta': wm, 'near': near_c}, not a.no_js)
            write_csv(os.path.join(a.out, 'tables', 'chemical_groups.csv'), ['entity_id', 'name', 'papers', 'wave_group', 'wave_certainty_pct', 'archetype', 'archetype_certainty_pct', 'basis'],
                      [(i, nmc[i], tc[i], GC.wave_names[GC.wave[i]] if i in GC.wave else (GC.wave_names[tent_c[i][0]] if tent_c[i][0] >= 0 else ''), 100 if i in GC.wave else tent_c[i][2],
                        GC.arch_names[GC.arch[i]] if i in GC.arch else (GC.arch_names[tent_c[i][1]] if tent_c[i][1] >= 0 else ''), 100 if i in GC.arch else tent_c[i][3], 'core' if i in GC.wave else 'tentative')
                       for i in sorted(cids, key=lambda i: -tc[i]) if i in tent_c or i in GC.wave])
        else:
            cfg['sub'] = 'Each line with dots is one %s, and each dot is its position at one half-year snapshot: when its publication growth peaks (left to right), how steep it is (lower is steeper), and how many papers mention it so far (size). %s mentions across %d %s.%s' % (
                noun, '{:,}'.format(tots), len(rows), nouns, sparse)
            write_page(a.out, pid, {'cfg': cfg, 'rows': rows}, not a.no_js)
        manifest['pages'].append({'id': pid, 'nav': nav_, 'kind': 'entities', 'n': len(rows)})

    # ---- 9 disease x chemical co-mentions ------------------------------------------------------------------------------
    if 'Chemical' in ents:
        ok = np.ones(nm, bool)
        if gd:
            ok[gd[0]:gd[1] + 1] = False
        R = [p for p in papers if p[0] < nm and ok[p[0]]]
        N = len(R)
        dc, cc = collections.Counter(), collections.Counter()
        for _, d, c in R:
            dc.update(d)
            cc.update(c)
        D = [k for k, v in dc.items() if v >= a.min_core]
        C = [k for k, v in cc.items() if v >= 15]
        di, ci = {k: j for j, k in enumerate(D)}, {k: j for j, k in enumerate(C)}
        X = np.zeros((len(D), len(C)))
        for _, d, c in R:
            dj = [di[k] for k in d if k in di]
            cj = [ci[k] for k in c if k in ci]
            if dj and cj:
                X[np.ix_(dj, cj)] += 1
        nd, nc = np.array([dc[k] for k in D], float), np.array([cc[k] for k in C], float)
        b, c_, d_ = nd[:, None] - X, nc[None, :] - X, N - nd[:, None] - nc[None, :] + X
        OR = ((X + .5) * (d_ + .5)) / ((b + .5) * (c_ + .5))
        Pv = hypergeom.sf(X - 1, N, nd[:, None], nc[None, :])
        flat = Pv.ravel()
        o = np.argsort(flat)
        mm = len(flat)
        q = np.empty(mm)
        q[o] = np.minimum.accumulate((flat[o] * mm / np.arange(1, mm + 1))[::-1])[::-1]
        Qv = q.reshape(Pv.shape)
        sig = (Qv < .05) & (X >= 5) & (OR > 1)
        E = np.outer(nd, nc) / N
        PP = np.clip(np.log((X + .5) / (E + .5)), 0, None)
        rk, ck = PP.sum(1) > 0, PP.sum(0) > 0
        sc = SpectralCoclustering(a.k_cooc, random_state=0, n_init=20).fit(PP[rk][:, ck])
        rl, cl_ = np.full(len(D), -1), np.full(len(C), -1)
        rl[rk], cl_[ck] = sc.row_labels_, sc.column_labels_
        rl[rl < 0], cl_[cl_ < 0] = 0, 0
        dnm, cnm = dnames, page_data['Chemical'][1]
        dn_all = {k: names['Disease'][k] for k in D}
        cn_all = {k: names['Chemical'][k] for k in C}
        ordg = sorted(range(a.k_cooc), key=lambda g: -X[np.ix_(rl == g, cl_ == g)].sum())
        gname, gdesc = {}, {}
        for g in ordg:
            dd = [D[j] for j in sorted(np.where(rl == g)[0], key=lambda j: -nd[j])][:3]
            cc_ = [C[j] for j in sorted(np.where(cl_ == g)[0], key=lambda j: -nc[j])][:3]
            gname[g] = (labels.get('cooc', {}).get(str(g), {}) or {}).get('name') or ('Group %d: %s / %s' % (ordg.index(g) + 1, dn_all[dd[0]] if dd else '-', cn_all[cc_[0]] if cc_ else '-'))
            gdesc[g] = (labels.get('cooc', {}).get(str(g), {}) or {}).get('desc') or ('Symptoms/comorbidities such as %s with chemicals such as %s.' % (', '.join(dn_all[k] for k in dd), ', '.join(cn_all[k] for k in cc_)))
        ro, co = [], []
        for g in ordg:
            ro += sorted(np.where(rl == g)[0], key=lambda j: -nd[j])
            co += sorted(np.where(cl_ == g)[0], key=lambda j: -nc[j])
        yrs = sorted({BASE_YEAR + p[0] // 12 for p in R})
        yrs = [str(y) for y in yrs if (y - BASE_YEAR) * 12 + 11 < am]
        cg = {k: int(cl_[j]) for k, j in ci.items()}
        cnt, tot_y = collections.defaultdict(collections.Counter), collections.Counter()
        for m, d, c in R:
            y = str(BASE_YEAR + m // 12)
            if not c or y not in yrs:
                continue
            tot_y[y] += 1
            for g in {cg[k] for k in c if k in cg}:
                cnt[y][g] += 1
        groups = [{'g': int(g), 'name': gname[g], 'desc': gdesc[g], 'share': [round(100 * cnt[y][g] / max(tot_y[y], 1), 1) for y in yrs]} for g in ordg]
        Dl = [dict(n=dn_all[D[j]], id=D[j], p=int(nd[j]), g=int(rl[j])) for j in ro]
        Cl = [dict(n=cn_all[C[j]], id=C[j], p=int(nc[j]), g=int(cl_[j])) for j in co]
        Xo, ORo, Qo = X[np.ix_(ro, co)], OR[np.ix_(ro, co)], Qv[np.ix_(ro, co)]
        cooc = dict(D=Dl, C=Cl, X=Xo.astype(int).tolist(), OR=np.round(ORo, 1).tolist(), S=((Qo < .05) & (Xo >= 5) & (ORo > 1)).astype(int).tolist(),
                    Q=np.round(-np.log10(np.clip(Qo, 1e-300, 1)), 1).tolist(), G=groups, yrs=yrs, N=N, ntag=int(sum(tot_y.values())),
                    cfg={'id': 'cooc', 'kind': 'cooc', 'title': 'Symptom/comorbidity and chemical links', 'min_disease': a.min_core, 'min_chemical': 15, 'gap': list(gd) if gd else None,
                            'sub': 'Each cell counts the papers in which a symptom/comorbidity and a chemical are both mentioned, compared with what chance would give if the two were unrelated. Rows are symptoms/comorbidities, columns are chemicals, and both are grouped by a co-clustering into %d joint groups. Darker means the pair appears together more often than expected. %sChemicals are mentioned in about %s of papers, so this describes what is written together and not which treatments were given. Several "chemicals" are tests or contrast agents (for example carbon monoxide is the lung-function test DLCO, gadolinium is MRI contrast, creatinine is a blood test).' % (a.k_cooc, ('Only papers outside the %s to %s collection gap are used, and ' % (mon_name(gd[0]), mon_name(gd[1]))) if gd else '', ('%d%%' % round(100 * sum(1 for p in R if p[2]) / max(int(per_month[ok].sum()), 1))))})
        write_page(a.out, 'cooc', cooc, not a.no_js)
        manifest['pages'].append({'id': 'cooc', 'nav': 'Symptom/comorbidity–chemical links', 'kind': 'cooc', 'n': int(sig.sum())})
        pairs = sorted(np.argwhere(sig), key=lambda t: -X[t[0], t[1]])
        write_csv(os.path.join(a.out, 'tables', 'disease_chemical_pairs.csv'), ['disease', 'disease_id', 'chemical', 'chemical_id', 'papers_together', 'disease_papers', 'chemical_papers', 'odds_ratio', 'q_value', 'joint_group'],
                  [(dn_all[D[i]], D[i], cn_all[C[j]], C[j], int(X[i, j]), int(nd[i]), int(nc[j]), round(OR[i, j], 2), '%.3g' % Qv[i, j], gname[int(rl[i])] if rl[i] == cl_[j] else '') for i, j in pairs])
        log('co-mentions:', len(D), 'diseases x', len(C), 'chemicals,', int(sig.sum()), 'significant pairs')

    # ---- 10 network and cell-line uses (papers that name a variant, cell line, chromosome or genomic region) ------------------
    if not a.no_network and any(t in types for t in NET_CORE):
        net = build_network(a.input, start, end, nm)
        if net['papers']:
            nn = collections.Counter(n[0] for n in net['nodes'])
            sub_n = ('Each dot is a term named in %d papers that mention at least one variant, cell line, chromosome or genomic region (%s). '
                     'Two terms are joined when they are named in the same paper; the line is thicker the more papers share them. '
                     'Very common terms such as COVID-19 are hidden by default because they would join everything to everything.') % (
                net['N'], ', '.join('%d %s' % (v, {'Disease': 'symptoms/comorbidities'}.get(k, k.lower() + 's')) for k, v in nn.most_common()))
            write_page(a.out, 'net', dict(net, cfg={'id': 'net', 'kind': 'net', 'title': 'Network of variants, cell lines, genes and symptoms/comorbidities', 'sub': sub_n}), not a.no_js)
            sub_c = ('Which cell lines are used with which topics? Cell lines are grouped by tissue and species with a small hand-made lookup, and the table (or matrix) lists the symptoms/comorbidities, chemicals or genes '
                     'named in the same papers as each cell line. Only %d papers name a cell line, so read it as a map, not as a result.' % sum(1 for r in net['papers'] if any(net['nodes'][i][0] == 'CellLine' for i in r[2])))
            write_page(a.out, 'celluse', dict(net, cfg={'id': 'celluse', 'kind': 'celluse', 'title': 'Cell line uses', 'sub': sub_c}), not a.no_js)
            manifest['pages'] += [{'id': 'net', 'nav': 'Network', 'kind': 'net', 'n': len(net['nodes'])}, {'id': 'celluse', 'nav': 'Cell line uses', 'kind': 'celluse', 'n': net['N']}]

    # ---- manifest in nav order ----------------------------------------------------------------------------------------
    navorder = ['diseases', 'categories', 'clusters', 'chemicals', 'cooc', 'variants', 'celllines', 'chromosomes', 'genes', 'species', 'net', 'celluse']
    manifest['pages'].sort(key=lambda p: navorder.index(p['id']))
    write_page(a.out, 'manifest', manifest, not a.no_js)
    log('done ->', os.path.abspath(a.out), [p['id'] for p in manifest['pages']])


if __name__ == '__main__':
    main()
