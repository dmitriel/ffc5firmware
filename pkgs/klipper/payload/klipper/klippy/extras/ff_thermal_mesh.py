# Creator 5 thermal bed-mesh map
#
# Copyright (C) 2026
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Map how the bed's shape follows bed and chamber temperature, then pick
the right mesh for a print by probing four points instead of a full grid.

THREE STAGES

1. THERMAL_MESH_SWEEP starts at the hottest chamber target and works down.
   At each chamber target the bed goes from its hottest target down, leaving
   out any bed target below the chamber (the bed cannot be held below the air
   around it), so the bed only ever cools between pairs. The chamber is soaked
   once, at the start. Each later chamber set first waits for the chamber to
   cool to its lower target -- there is no active cooling -- and is skipped if
   it has not within COOL_TIMEOUT; CHAMBER_STEP_SOAK (0 by default) adds a
   chamber soak there. The first bed step of every set soaks for MAX_SOAK
   (or CHAMBER_STEP_SOAK, if longer), every other one for SOAK. At each pair it then probes full meshes,
   each one verifying the one before: the probe's precision is measured right
   after it, and the two must be identical at CONFIDENCE (verify_meshes:
   average error statistically 0, every node within the probe's noise). The
   verified pair's average is stored, with the bed and chamber temperatures
   measured around it.
   Every finished pair is written to the data file at once, so an abort or a
   power cut loses at most the pair in progress; RESUME=1 picks the sweep
   back up.

2. THERMAL_MESH_ANALYZE separates the two heaters' warp. For every mesh node
   it fits Z = base + b*(Tb - Tb0) + c*(Tc - Tc0) by least squares over every
   entry, using the MEASURED temperatures: b is the bed's warp with the
   chamber held, c the chamber's with the bed held, even though a hot bed
   also warms the chamber and the map is triangular rather than a full grid.
   It then picks the probe points that best pin down both temperatures (a
   greedy D-optimal choice, kept apart by min_point_spacing) and saves them.
   The sweep runs this when it finishes.

3. THERMAL_MESH_APPLY probes only those points. METHOD=model (the default)
   solves for the bed and chamber temperatures whose model mesh fits them,
   then adds back what the straight-line model misses near that state from
   the closest map entries. The older grid methods (bilinear, idw, nearest)
   compare with the map entries directly. None needs to know the
   temperatures: the bed's shape is the measurement.

The sweep runs as a reactor callback rather than inside one G-code command,
so the G-code queue is free between steps: THERMAL_MESH_STATUS and
THERMAL_MESH_ABORT work while it soaks. A full mesh probe still holds the
queue until it finishes, so an abort lands at the end of the current probe.

The module level functions hold all of the arithmetic and touch nothing in
klippy; qa/static/test_ff_thermal_mesh.py tests them directly.
"""

import json
import logging
import math
import os
import threading

from . import bed_mesh as bed_mesh_module
from . import probe as probe_module

DATA_VERSION = 1
METHODS = ('model', 'bilinear', 'idw', 'nearest', 'temperature')
CORRECTIONS = ('none', 'offset', 'plane')
# Far beyond any sweep; restored to the configured value when the sweep ends.
SWEEP_IDLE_TIMEOUT = 864000.
# THERMAL_MESH_APPLY REUSE_PROBE=1 takes an earlier APPLY's points if this
# recent: well inside one print start, well short of a changed bed.
REUSE_PROBE_SECONDS = 600.


class SweepAborted(Exception):
    pass


######################################################################
# Pure helpers: planning, convergence, analysis, matching
######################################################################

def temperature_steps(low, high, step):
    """low, low+step, ... and always high itself, ascending."""
    if high < low:
        raise ValueError("maximum %.1f is below minimum %.1f" % (high, low))
    if high == low:
        return [float(low)]
    if step <= 0.:
        raise ValueError("step must be positive")
    temps = []
    t = float(low)
    while t < high - 1e-6:
        temps.append(round(t, 3))
        t += step
    temps.append(float(high))
    return temps


def sweep_plan(bed_temps, chamber_temps):
    """(chamber, bed) pairs: hottest chamber first, each with the bed from
    its hottest target down.

    A bed target below the chamber target is left out: the bed cannot be
    held below the air around it. Chamber 0 is unheated and keeps every bed
    target. A chamber target left with no bed target is dropped.
    """
    plan = []
    for chamber in sorted(chamber_temps, reverse=True):
        for bed in sorted(bed_temps, reverse=True):
            if chamber <= 0. or bed >= chamber:
                plan.append((chamber, bed))
    return plan


def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function (Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1., a - 1.
    c, d = 1., 1. - qab * x / qap
    d = 1. / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1. + aa * d
        d = 1. / (d if abs(d) > tiny else tiny)
        c = 1. + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1. + aa * d
        d = 1. / (d if abs(d) > tiny else tiny)
        c = 1. + aa / c
        c = c if abs(c) > tiny else tiny
        step = d * c
        h *= step
        if abs(step - 1.) < 1e-14:
            break
    return h


def betainc(a, b, x):
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.:
        return 0.
    if x >= 1.:
        return 1.
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log(1. - x))
    if x < (a + 1.) / (a + b + 2.):
        return front * _betacf(a, b, x) / a
    return 1. - front * _betacf(b, a, 1. - x) / b


def f_cdf(x, d1, d2):
    if x <= 0.:
        return 0.
    return betainc(d1 / 2., d2 / 2., d1 * x / (d1 * x + d2))


def f_quantile(p, d1, d2):
    """x with P(F(d1, d2) <= x) = p, by bisection."""
    lo, hi = 0., 1.
    while f_cdf(hi, d1, d2) < p:
        hi *= 2.
    for _ in range(200):
        mid = (lo + hi) / 2.
        if f_cdf(mid, d1, d2) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10 * hi:
            break
    return (lo + hi) / 2.


def sample_std(values):
    """(standard deviation, degrees of freedom) of repeated readings."""
    n = len(values)
    if n < 2:
        raise ValueError("precision needs at least two readings")
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)), n - 1


def pooled_std(groups):
    """(standard deviation, degrees of freedom) pooled over several points,
    each read repeatedly: the spread of every point about its own mean.
    Points whose true heights differ do not inflate it."""
    ss, dof = 0., 0
    for values in groups:
        if len(values) < 2:
            continue
        mean = sum(values) / len(values)
        ss += sum((v - mean) ** 2 for v in values)
        dof += len(values) - 1
    if dof < 1:
        raise ValueError("precision needs a point read at least twice")
    return math.sqrt(ss / dof), dof


def precision_nodes(params):
    """Nine mesh nodes, a 3 x 3 spread over the mesh: its corners, edge
    middles and middle. A probe is not equally repeatable everywhere over
    the bed -- on the Creator 5 the edges read far noisier than the middle
    -- so its noise is measured where the mesh is, not at one spot."""
    rows = sorted(set([0, (params['y_count'] - 1) // 2,
                       params['y_count'] - 1]))
    cols = sorted(set([0, (params['x_count'] - 1) // 2,
                       params['x_count'] - 1]))
    return [node_xy(params, r, c) for r in rows for c in cols]


def verify_meshes(a, b, sigma, dof, confidence=0.95, shared_reference=False):
    """Are two meshes of the same plate the same, given the probe's noise?

    d = b - a at every one of the n nodes. If the plate did not move, each
    d is the difference of two readings with the probe's standard deviation
    sigma: mean 0, variance 2 sigma^2. sigma is itself measured, from dof+1
    repeated readings, so the statistics are F- rather than chi-square-
    distributed:

      bias   mean(d)^2 / (2 sigma^2 k)          ~ F(1, dof)
             the average error is 0; k = 1/n, or 1/n + 1 with a
             shared_reference (bed_mesh's zero_reference_position: one
             touch shifts every node of a mesh alike)
      shape  sum((d - mean)^2) / ((n-1) 2 sigma^2)  ~ F(n-1, dof)
             every node agrees once the average is taken out

    The two use independent parts of d. Each is tested at
    1 - (1 - confidence)/2 (Bonferroni), so together they hold at
    `confidence`: when both pass, nothing in the two meshes tells them apart
    from one plate probed twice. Also returns the two-sided confidence
    interval of the mean error. A sigma of zero (a perfectly repeatable
    probe) passes only an exact match.
    """
    d = [zb - za for ra, rb in zip(a, b) for za, zb in zip(ra, rb)]
    n = len(d)
    mean = sum(d) / n
    msq = sum(v * v for v in d) / n
    level = 1. - (1. - confidence) / 2.
    out = {'n': n, 'mean': mean, 'rms': math.sqrt(msq), 'sigma': sigma,
           'dof': dof, 'confidence': confidence,
           'shared_reference': bool(shared_reference)}
    var = 2. * sigma * sigma
    k = 1. / n + (1. if shared_reference else 0.)
    if var <= 0. or n < 2:
        exact = max(abs(v) for v in d) <= 1e-12
        out.update(bias=0. if exact else float('inf'), bias_limit=0.,
                   shape=0. if exact else float('inf'), shape_limit=0.,
                   mean_ci=0., identical=exact)
        return out
    bias_limit = f_quantile(level, 1, dof)
    shape_limit = f_quantile(level, n - 1, dof)
    bias = mean * mean / (var * k)
    shape = sum((v - mean) ** 2 for v in d) / ((n - 1) * var)
    # t(dof)^2 = F(1, dof): the half-width of the interval for the mean.
    t = math.sqrt(f_quantile(confidence, 1, dof))
    out.update(bias=bias, bias_limit=bias_limit, shape=shape,
               shape_limit=shape_limit, mean_ci=t * math.sqrt(var * k),
               identical=bias <= bias_limit and shape <= shape_limit)
    return out


def average_matrices(matrices, weights=None):
    if weights is None:
        weights = [1.] * len(matrices)
    total = sum(weights)
    rows, cols = len(matrices[0]), len(matrices[0][0])
    return [[sum(w * m[r][c] for m, w in zip(matrices, weights)) / total
             for c in range(cols)] for r in range(rows)]


def node_xy(params, row, col):
    x = params['min_x'] + col * (params['max_x'] - params['min_x']) / (
        params['x_count'] - 1)
    y = params['min_y'] + row * (params['max_y'] - params['min_y']) / (
        params['y_count'] - 1)
    return x, y


def _key(chamber, bed):
    return (round(float(chamber), 3), round(float(bed), 3))


def build_grid(entries):
    """Sorted chamber and bed targets, and {(chamber, bed): matrix}."""
    table = {_key(e['chamber'], e['bed']): e['matrix'] for e in entries}
    chambers = sorted(set(k[0] for k in table))
    beds = sorted(set(k[1] for k in table))
    return chambers, beds, table


def entry_temps(entry):
    """(bed, chamber) the plate was actually at while it was probed.

    The mean of the readings taken at the start and the end of sampling;
    the targets where there are none (an older map, a missing sensor).
    The chamber in particular is not its target: a hot bed carries it past.
    """
    def mean_of(values, fallback):
        vals = [float(v) for v in (values or []) if v is not None]
        return sum(vals) / len(vals) if vals else float(fallback)
    return (mean_of(entry.get('bed_measured'), entry['bed']),
            mean_of(entry.get('chamber_measured'), entry['chamber']))


def _solve(a, b):
    """x with a x = b, a small dense system (Gaussian elimination)."""
    n = len(b)
    m = [list(map(float, row)) + [float(v)] for row, v in zip(a, b)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(m[r][i]))
        if abs(m[piv][i]) < 1e-15:
            raise ValueError("singular system")
        m[i], m[piv] = m[piv], m[i]
        for r in range(n):
            if r != i:
                f = m[r][i] / m[i][i]
                for c in range(i, n + 1):
                    m[r][c] -= f * m[i][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def _correlation(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0. or syy <= 0.:
        return 0.
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(
        sxx * syy)


# |correlation| between the measured bed and chamber temperatures above which
# the map cannot tell the two apart.
MAX_CORRELATION = 0.995


def thermal_model(entries):
    """Separate the bed's and the chamber's share of the warp, node by node.

    Least squares over every entry, at every mesh node:

        Z = base + bed * (Tb - bed_ref) + chamber * (Tc - chamber_ref)

    with Tb and Tc the temperatures each entry was measured at (entry_temps),
    not its targets. 'bed' and 'chamber' are the partial slopes in mm/degC:
    the warp one heater causes with the other held still, whatever the sweep
    order and even where the two moved together (a hot bed warming the
    chamber), as long as they did not move in lockstep. It needs no full
    grid, so a map with skipped pairs or sets fits as well as a full one.

    An axis is fitted only if the sweep had more than one target on it.
    Returns None if neither had; raises ValueError if the measured bed and
    chamber temperatures are too closely correlated to separate.
    """
    chambers = set(round(float(e['chamber']), 3) for e in entries)
    beds = set(round(float(e['bed']), 3) for e in entries)
    axes = [a for a, n in (('bed', len(beds)), ('chamber', len(chambers)))
            if n > 1]
    if not axes:
        return None
    temps = [entry_temps(e) for e in entries]
    tb = [t[0] for t in temps]
    tc = [t[1] for t in temps]
    ref = {'bed': sum(tb) / len(tb), 'chamber': sum(tc) / len(tc)}
    corr = _correlation(tb, tc)
    if len(axes) == 2 and abs(corr) > MAX_CORRELATION:
        raise ValueError("bed and chamber temperatures moved together "
                         "(correlation %.3f); the map cannot separate them"
                         % (corr,))
    value = {'bed': tb, 'chamber': tc}
    design = [[1.] + [value[a][i] - ref[a] for a in axes]
              for i in range(len(entries))]
    k = len(design[0])
    xtx = [[sum(row[i] * row[j] for row in design) for j in range(k)]
           for i in range(k)]
    rows, cols = len(entries[0]['matrix']), len(entries[0]['matrix'][0])
    coef = [[[0.] * cols for _ in range(rows)] for _ in range(k)]
    for r in range(rows):
        for c in range(cols):
            xtz = [sum(row[i] * e['matrix'][r][c]
                       for row, e in zip(design, entries)) for i in range(k)]
            try:
                sol = _solve(xtx, xtz)
            except ValueError:
                raise ValueError("the map has too few distinct temperatures "
                                 "to fit")
            for i in range(k):
                coef[i][r][c] = sol[i]
    model = {'axes': axes, 'base': coef[0], 'bed': None, 'chamber': None,
             'bed_ref': ref['bed'], 'chamber_ref': ref['chamber'],
             'bed_range': [min(tb), max(tb)],
             'chamber_range': [min(tc), max(tc)],
             'correlation': corr}
    for i, a in enumerate(axes):
        model[a] = coef[i + 1]
    # What the straight-line model leaves out: curvature and noise.
    sq = []
    for e, (b, ch) in zip(entries, temps):
        pred = model_matrix(model, b, ch)
        sq.extend((pr - z) ** 2 for prow, zrow in zip(pred, e['matrix'])
                  for pr, z in zip(prow, zrow))
    model['fit_rms'] = math.sqrt(sum(sq) / len(sq))
    return model


def model_matrix(model, bed, chamber):
    """The model's mesh at a bed and chamber temperature."""
    out = [list(row) for row in model['base']]
    for axis, t in (('bed', bed), ('chamber', chamber)):
        slope = model[axis]
        if slope is None:
            continue
        dt = t - model[axis + '_ref']
        for r in range(len(out)):
            for c in range(len(out[r])):
                out[r][c] += slope[r][c] * dt
    return out


def sensitivity_maps(entries):
    """Per-node |dZ/dT| (mm/degC) for the chamber and the bed, decoupled.

    The absolute slopes of thermal_model. An axis with a single target is
    None, as are both for a map of one pair.
    """
    model = thermal_model(entries)
    if model is None:
        return None, None
    return tuple(None if model[a] is None else
                 [[abs(v) for v in row] for row in model[a]]
                 for a in ('chamber', 'bed'))


def _span(model, axis):
    lo, hi = model[axis + '_range']
    return max(hi - lo, 1.)


def _det(m):
    if len(m) == 1:
        return m[0][0]
    return m[0][0] * m[1][1] - m[0][1] * m[1][0]


def point_separation(model, points):
    """|cos| of the angle between the bed's and the chamber's warp as seen
    at the points: 0 tells them apart perfectly, 1 not at all. None with a
    single axis."""
    if model['bed'] is None or model['chamber'] is None:
        return None
    b = signature(model['bed'], points)
    c = signature(model['chamber'], points)
    nb = math.sqrt(sum(v * v for v in b))
    nc = math.sqrt(sum(v * v for v in c))
    if nb <= 0. or nc <= 0.:
        return 1.
    return abs(sum(x * y for x, y in zip(b, c))) / (nb * nc)


def select_points(params, model, count=4, min_spacing=60.):
    """Pick the mesh nodes that best tell the temperature state apart.

    Each node sees the warp over the swept range as a vector: (bed slope x
    bed span, chamber slope x chamber span), in mm. Probing a set of nodes
    is a least-squares fit of the two temperatures (fit_model), and its
    precision is the determinant of the sum of their outer products. Picks
    are greedy on that determinant: the first is the node that moves most
    overall, later ones the node that adds most information -- typically
    one where the bed and the chamber warp the plate differently, which the
    most sensitive nodes alone may not be when both bow the same middle.
    A node closer than min_spacing to an earlier pick is skipped; when that
    leaves none the spacing is halved.
    """
    axes = [a for a in ('bed', 'chamber') if model[a] is not None]
    if not axes:
        raise ValueError("the map needs at least two temperatures on one axis")
    span = dict((a, _span(model, a)) for a in axes)
    rows, cols = params['y_count'], params['x_count']

    def vec(r, c):
        return [model[a][r][c] * span[a] for a in axes]

    k = len(axes)
    eps = 1e-10
    info = [[eps if i == j else 0. for j in range(k)] for i in range(k)]
    chosen = []
    spacing = float(min_spacing)
    while len(chosen) < min(count, rows * cols):
        best = None
        for r in range(rows):
            for c in range(cols):
                if any(p['row'] == r and p['col'] == c for p in chosen):
                    continue
                x, y = node_xy(params, r, c)
                if any(math.hypot(x - p['x'], y - p['y']) < spacing
                       for p in chosen):
                    continue
                g = vec(r, c)
                m = [[info[i][j] + g[i] * g[j] for j in range(k)]
                     for i in range(k)]
                score = _det(m)
                if best is None or score > best[0] + 1e-18:
                    best = (score, r, c, x, y, g)
        if best is None:
            spacing /= 2.
            continue
        _, r, c, x, y, g = best
        info = [[info[i][j] + g[i] * g[j] for j in range(k)]
                for i in range(k)]
        share = dict(zip(axes, (abs(v) for v in g)))
        sens = dict((a, abs(model[a][r][c]) if model[a] is not None else 0.)
                    for a in ('bed', 'chamber'))
        chosen.append({'x': round(x, 3), 'y': round(y, 3), 'row': r, 'col': c,
                       'axis': max(axes, key=lambda a: share[a]),
                       'chamber_sens': sens['chamber'],
                       'bed_sens': sens['bed']})
    return chosen


def signature(matrix, points):
    return [matrix[p['row']][p['col']] for p in points]


def _centered(values):
    mean = sum(values) / len(values)
    return [v - mean for v in values]


def residual_rms(predicted, measured, relative=False):
    if relative:
        predicted, measured = _centered(predicted), _centered(measured)
    sq = [(p - m) ** 2 for p, m in zip(predicted, measured)]
    return math.sqrt(sum(sq) / len(sq))


def rank_entries(entries, points, measured, relative=False):
    """[(rms, entry)] closest first."""
    ranked = [(residual_rms(signature(e['matrix'], points), measured,
                            relative), e) for e in entries]
    ranked.sort(key=lambda t: t[0])
    return ranked


def idw_weights(ranked, k=4, power=2., epsilon=1e-4):
    """Inverse-distance weights over the k closest entries.

    An entry that matches to within epsilon takes all of the weight, so a
    perfect hit returns that mesh unchanged.
    """
    best = ranked[:max(1, k)]
    if best[0][0] <= epsilon:
        return [(1., best[0][1])]
    raw = [(1. / rms ** power, e) for rms, e in best]
    total = sum(w for w, _ in raw)
    return [(w / total, e) for w, e in raw]


def _cells(values):
    """Neighbouring index pairs; one value is a degenerate (i, i) cell."""
    if len(values) == 1:
        return [(0, 0)]
    return [(i, i + 1) for i in range(len(values) - 1)]


def _bilinear_corners(chambers, beds, ci, bi, u, v):
    """[(weight, (chamber, bed))] for a point u, v in [0, 1] inside a cell."""
    c0, c1 = chambers[ci[0]], chambers[ci[1]]
    b0, b1 = beds[bi[0]], beds[bi[1]]
    corners = {}
    for wc, c in ((1. - u, c0), (u, c1)):
        for wb, b in ((1. - v, b0), (v, b1)):
            corners[(c, b)] = corners.get((c, b), 0.) + wc * wb
    return [(w, k) for k, w in corners.items() if w > 0.]


def bilinear_weights(entries, chamber, bed):
    """Weights at a temperature pair, clamped to the swept range.

    Returns (weights, chamber_used, bed_used); weights is a list of
    (weight, entry). Raises ValueError where a corner was never sampled.
    """
    chambers, beds, table = build_grid(entries)
    by_key = {_key(e['chamber'], e['bed']): e for e in entries}

    def locate(values, t):
        t = min(max(t, values[0]), values[-1])
        for cell in _cells(values):
            lo, hi = values[cell[0]], values[cell[1]]
            if lo <= t <= hi:
                return cell, (0. if hi == lo else (t - lo) / (hi - lo)), t
        return _cells(values)[-1], 1., t
    ci, u, chamber = locate(chambers, chamber)
    bi, v, bed = locate(beds, bed)
    weights = []
    for w, k in _bilinear_corners(chambers, beds, ci, bi, u, v):
        if k not in table:
            raise ValueError("no mesh at chamber %.1f / bed %.1f" % k)
        weights.append((w, by_key[k]))
    return weights, chamber, bed


def _closest_on_line(base, slope, target):
    """t in [0, 1] minimising |base + t*slope - target|^2."""
    num = sum(sl * (tg - b) for b, sl, tg in zip(base, slope, target))
    den = sum(sl * sl for sl in slope)
    if den <= 0.:
        return 0.
    return min(1., max(0., num / den))


def fit_bilinear(entries, points, measured, relative=False, steps=20,
                 checkpoint=None):
    """Find the temperature pair whose interpolated mesh best fits the probe.

    The map is a grid over (chamber, bed). Inside every cell whose four
    corners were sampled, the interpolated signature is bilinear in the cell
    coordinates u (chamber) and v (bed): A + B*u + C*v + D*u*v, one value
    per point. For a fixed v that is a straight line in u, so the best u has
    a closed form; this scans v in `steps`, solves u at each, then refines
    by alternating between the two. Cheap enough for a slow host, and
    checkpoint() (if given) is called after every cell, which klippy uses to
    let the reactor run. Returns a dict with rms, chamber, bed and weights
    [(weight, entry)].
    """
    chambers, beds, table = build_grid(entries)
    by_key = {_key(e['chamber'], e['bed']): e for e in entries}
    sigs = {k: signature(m, points) for k, m in table.items()}
    target = _centered(measured) if relative else list(measured)

    def prep(values):
        return _centered(values) if relative else values

    best = None
    for ci in _cells(chambers):
        for bi in _cells(beds):
            keys = [(chambers[a], beds[b]) for a in ci for b in bi]
            if not set(keys).issubset(table):
                continue
            s00 = sigs[(chambers[ci[0]], beds[bi[0]])]
            s10 = sigs[(chambers[ci[1]], beds[bi[0]])]
            s01 = sigs[(chambers[ci[0]], beds[bi[1]])]
            s11 = sigs[(chambers[ci[1]], beds[bi[1]])]
            n = range(len(points))
            A = prep([s00[i] for i in n])
            B = prep([s10[i] - s00[i] for i in n])
            C = prep([s01[i] - s00[i] for i in n])
            D = prep([s11[i] - s10[i] - s01[i] + s00[i] for i in n])

            def at(u, v):
                pred = [A[i] + B[i] * u + C[i] * v + D[i] * u * v for i in n]
                return math.sqrt(sum((p - t) ** 2 for p, t in
                                     zip(pred, target)) / len(target))

            def best_u(v):
                if ci[0] == ci[1]:
                    return 0.
                return _closest_on_line([A[i] + C[i] * v for i in n],
                                        [B[i] + D[i] * v for i in n], target)

            def best_v(u):
                if bi[0] == bi[1]:
                    return 0.
                return _closest_on_line([A[i] + B[i] * u for i in n],
                                        [C[i] + D[i] * u for i in n], target)

            vs = [0.] if bi[0] == bi[1] else [k / float(steps)
                                              for k in range(steps + 1)]
            hit = None
            for v in vs:
                u = best_u(v)
                rms = at(u, v)
                if hit is None or rms < hit[0]:
                    hit = (rms, u, v)
            rms, u, v = hit
            for _ in range(8):
                v = best_v(u)
                u = best_u(v)
                rms = min(rms, at(u, v))
            rms = at(u, v)
            if best is None or rms < best[0]:
                best = (rms, u, v, ci, bi)
            if checkpoint is not None:
                checkpoint()
    if best is None:
        raise ValueError("no fully sampled cell in the map")
    rms, u, v, ci, bi = best
    corners = _bilinear_corners(chambers, beds, ci, bi, u, v)
    c0, c1 = chambers[ci[0]], chambers[ci[1]]
    b0, b1 = beds[bi[0]], beds[bi[1]]
    return {'rms': rms,
            'chamber': c0 + u * (c1 - c0),
            'bed': b0 + v * (b1 - b0),
            'weights': [(w, by_key[k]) for w, k in corners]}


def correction_surface(points, residuals, mode):
    """f(x, y) to add to the effective mesh so it meets the probed points.

    'offset' is the mean residual everywhere; 'plane' a least-squares plane
    through the residuals (it needs three points that are not in a line and
    falls back to 'offset' otherwise).
    """
    if mode == 'none':
        return lambda x, y: 0.
    mean = sum(residuals) / len(residuals)
    if mode == 'offset' or len(points) < 3:
        return lambda x, y: mean
    # Normal equations for r = a + b*x + c*y, centred for conditioning.
    mx = sum(p['x'] for p in points) / len(points)
    my = sum(p['y'] for p in points) / len(points)
    xs = [p['x'] - mx for p in points]
    ys = [p['y'] - my for p in points]
    sxx = sum(x * x for x in xs)
    syy = sum(y * y for y in ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxr = sum(x * r for x, r in zip(xs, residuals))
    syr = sum(y * r for y, r in zip(ys, residuals))
    det = sxx * syy - sxy * sxy
    if abs(det) < 1e-9:
        return lambda x, y: mean
    b = (sxr * syy - syr * sxy) / det
    c = (syr * sxx - sxr * sxy) / det
    return lambda x, y: mean + b * (x - mx) + c * (y - my)


def apply_correction(matrix, params, points=None, measured=None,
                     correct='none'):
    """Fold the residual at the probed points into the matrix, in place.

    Returns the residuals (probed minus matrix) before the correction.
    """
    residuals = []
    if points and measured is not None:
        residuals = [m - z for m, z in zip(measured,
                                           signature(matrix, points))]
        surface = correction_surface(points, residuals, correct)
        for r in range(len(matrix)):
            for c in range(len(matrix[r])):
                matrix[r][c] += surface(*node_xy(params, r, c))
    return residuals


def effective_mesh(weights, params, points=None, measured=None,
                   correct='none'):
    """Blend the weighted entries and apply the requested correction.

    Returns (matrix, residuals after blending, before correction).
    """
    matrix = average_matrices([e['matrix'] for _, e in weights],
                              [w for w, _ in weights])
    return matrix, apply_correction(matrix, params, points, measured,
                                    correct)


def fit_model(model, points, measured, relative=False):
    """The bed and chamber temperatures whose model mesh best fits the probe.

    Linear least squares for the temperature offsets from the model's
    reference: measured - base = bed_slope * dTb + chamber_slope * dTc at the
    probed points (each side centred first if relative). The result is
    clamped to the temperatures the map was measured at. A tiny ridge keeps
    it defined when the points cannot separate the axes; point_separation
    says how well they do.
    """
    axes = [a for a in ('bed', 'chamber') if model[a] is not None]
    target = [m - b for m, b in zip(measured, signature(model['base'],
                                                        points))]
    cols = [signature(model[a], points) for a in axes]
    if relative:
        target = _centered(target)
        cols = [_centered(col) for col in cols]
    k = len(axes)
    ata = [[sum(x * y for x, y in zip(cols[i], cols[j])) for j in range(k)]
           for i in range(k)]
    ridge = 1e-9 * (sum(ata[i][i] for i in range(k)) or 1.)
    for i in range(k):
        ata[i][i] += ridge
    atb = [sum(x * t for x, t in zip(cols[i], target)) for i in range(k)]
    sol = dict(zip(axes, _solve(ata, atb)))
    temps = {}
    for a in ('bed', 'chamber'):
        t = model[a + '_ref'] + sol.get(a, 0.)
        lo, hi = model[a + '_range']
        temps[a] = min(max(t, lo), hi)
    return temps['bed'], temps['chamber']


def model_mesh(model, entries, bed, chamber, k=4, power=2.):
    """The model's mesh at (bed, chamber), plus what the model misses there.

    The straight-line model gives the bed's and the chamber's warp apart;
    what is left in each map entry (curvature, a step the line smooths over)
    is blended back in from the k entries nearest in temperature, each axis
    scaled by its swept span, weighted 1/distance^power. An entry measured
    at exactly that state gives its own mesh back. Returns (matrix,
    [(weight, entry)]).
    """
    span = {'bed': _span(model, 'bed'), 'chamber': _span(model, 'chamber')}
    ranked = []
    for e in entries:
        tb, tc = entry_temps(e)
        d = math.hypot((tb - bed) / span['bed'],
                       (tc - chamber) / span['chamber'])
        ranked.append((d, e))
    ranked.sort(key=lambda t: t[0])
    best = ranked[:max(1, k)]
    if best[0][0] <= 1e-6:
        weights = [(1., best[0][1])]
    else:
        raw = [(1. / d ** power, e) for d, e in best]
        total = sum(w for w, _ in raw)
        weights = [(w / total, e) for w, e in raw]
    matrix = model_matrix(model, bed, chamber)
    for w, e in weights:
        pred = model_matrix(model, *entry_temps(e))
        for r in range(len(matrix)):
            for c in range(len(matrix[r])):
                matrix[r][c] += w * (e['matrix'][r][c] - pred[r][c])
    return matrix, weights


######################################################################
# Klipper glue
######################################################################

class FFThermalMesh:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.bed_heater = config.get('bed_heater', 'heater_bed')
        self.chamber_heater = config.get('chamber_heater', 'chamber_heater')
        self.chamber_sensor = config.get('chamber_sensor', None)
        self.bed_tolerance = config.getfloat('bed_tolerance', 1., above=0.)
        self.chamber_tolerance = config.getfloat(
            'chamber_tolerance', 2., above=0.)
        self.heat_timeout = config.getfloat('heat_timeout', 5400., above=0.)
        self.cool_timeout = config.getfloat('cool_timeout', 3600., above=0.)
        # A [fan_generic] run while the sweep waits for the chamber to cool,
        # then put back to its previous speed. Empty: none.
        self.cool_fan = (config.get('cool_fan', 'chamber_fan') or '').strip()
        self.cool_fan_speed = config.getfloat('cool_fan_speed', 1.,
                                              minval=0., maxval=1.)
        # 0: keep verifying until a pair of meshes passes (ABORT stops it).
        self.max_samples = config.getint('max_samples', 0, minval=0)
        if self.max_samples == 1:
            raise config.error("ff_thermal_mesh: max_samples is 0 (no limit) "
                               "or at least 2: a mesh needs one to verify it")
        # Readings of EACH of the nine precision nodes, per verification.
        self.precision_samples = config.getint('precision_samples', 4,
                                               minval=2)
        self.confidence = config.getfloat('confidence', 0.95, minval=0.5,
                                          maxval=0.999)
        self.max_probe_sigma = config.getfloat('max_probe_sigma', 0.01,
                                               above=0.)
        self.min_spacing = config.getfloat(
            'min_point_spacing', 60., minval=0.)
        data_file = config.get('data_file', None)
        if data_file is None:
            cfg = self.printer.get_start_args().get('config_file', '')
            data_file = os.path.join(os.path.dirname(cfg) or '.',
                                     'thermal_mesh.json')
        self.data_file = os.path.expanduser(data_file)
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.before_sample = gcode_macro.load_template(
            config, 'before_sample_gcode', 'G28 Z')
        self.after_sample = gcode_macro.load_template(
            config, 'after_sample_gcode', '')
        # Our own helper, not bed_mesh's, so the point list is ours; at
        # connect ff_bed_mesh lends it the same travel speed and lift.
        self.probe_helper = probe_module.ProbePointsHelper(
            config, self._probe_finalize, default_points=[(0., 0.)])
        self.probe_helper.use_xy_offsets(True)
        self._probed = None
        self.bed_mesh = None
        self.job = None
        self.status = {'state': 'idle', 'step': 0, 'total': 0, 'phase': '',
                       'bed_target': 0., 'chamber_target': 0.,
                       'last_delta': None, 'sigma': None, 'samples': 0,
                       'remaining': 0.,
                       'message': ''}
        self.last_apply = {}
        self._last_probe = None
        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:shutdown',
                                            self._handle_shutdown)
        self._precision = None
        self._precision_plan = []
        self._save_lock = threading.Lock()
        self._save_pending = None
        self._save_thread = None
        self.gcode.register_command('_THERMAL_MESH_PRECISION',
                                    self.cmd__THERMAL_MESH_PRECISION)
        for name in ('SWEEP', 'ABORT', 'STATUS', 'ANALYZE', 'APPLY'):
            cmd = 'THERMAL_MESH_' + name
            func = getattr(self, 'cmd_' + cmd)
            self.gcode.register_command(cmd, func,
                                        desc=getattr(self, func.__name__
                                                     + '_help'))

    def _handle_connect(self):
        self.bed_mesh = self.printer.lookup_object('bed_mesh', None)
        travel = self.printer.lookup_object('ff_bed_mesh', None)
        if travel is not None:
            travel.attach(self.probe_helper)

    def _handle_shutdown(self):
        if self.job is not None:
            self.job['abort'] = True

    def get_status(self, eventtime):
        status = dict(self.status)
        status['data_file'] = self.data_file
        status['last_apply'] = dict(self.last_apply)
        return status

    # -- data file -------------------------------------------------------
    def _load(self):
        self._wait_saved()
        try:
            with open(self.data_file) as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise self.gcode.error("thermal mesh: cannot read %s: %s"
                                   % (self.data_file, e))
        if data.get('version') != DATA_VERSION:
            raise self.gcode.error("thermal mesh: %s is version %s, expected %d"
                                   % (self.data_file, data.get('version'),
                                      DATA_VERSION))
        return data

    # -- saving the map ---------------------------------------------------
    #
    # OFF THE REACTOR. The whole map -- every pair's mesh -- is rewritten
    # after every pair and every skip, and it grows all sweep. json.dump with
    # an indent is CPython's pure-Python encoder; on the printer's CPU a
    # late-sweep map took long enough that klippy stopped servicing the MCU
    # ("Timer too close", right after a stored pair, a skip and the end of
    # the plan saved back to back). So the encode and the write run in a
    # worker thread: pure-Python work there gives the GIL back every few ms
    # and the reactor keeps running. Only the newest snapshot is written;
    # each lands whole (written to .tmp, then renamed over the map).

    def _save(self, data):
        # Shallow is enough: entries and skips are appended, never edited,
        # and the analysis is replaced whole.
        snap = dict((k, list(v) if isinstance(v, list) else v)
                    for k, v in data.items())
        with self._save_lock:
            self._save_pending = snap
            if self._save_thread is None:
                self._save_thread = threading.Thread(
                    target=self._save_worker, name='thermal-mesh-save')
                self._save_thread.daemon = True
                self._save_thread.start()

    def _save_worker(self):
        while True:
            with self._save_lock:
                snap, self._save_pending = self._save_pending, None
                if snap is None:
                    self._save_thread = None
                    return
            try:
                tmp = self.data_file + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(snap, f, indent=1)
                os.replace(tmp, self.data_file)
            except Exception as e:
                logging.exception("thermal mesh: saving %s", self.data_file)
                notify = getattr(self.reactor, 'register_async_callback',
                                 None)
                if notify is not None:
                    msg = ("thermal mesh: could not save %s: %s"
                           % (self.data_file, e))
                    notify(lambda et, m=msg: self.gcode.respond_info(m))

    def _wait_saved(self, yield_reactor=False):
        """Until every save so far is on disk. yield_reactor: wait in short
        reactor pauses (from the sweep), not by blocking the thread."""
        while True:
            with self._save_lock:
                thread = self._save_thread
            if thread is None:
                return
            if yield_reactor:
                self.reactor.pause(self.reactor.monotonic() + 0.05)
            else:
                thread.join()

    def _require_map(self, need_points=True):
        data = self._load()
        if not data or not data.get('entries'):
            raise self.gcode.error(
                "thermal mesh: no map in %s; run THERMAL_MESH_SWEEP first"
                % (self.data_file,))
        if need_points and not data.get('analysis', {}).get('points'):
            raise self.gcode.error(
                "thermal mesh: no probe points chosen; run THERMAL_MESH_ANALYZE")
        return data

    # -- heaters ---------------------------------------------------------
    def _has_chamber_heater(self):
        return self.printer.lookup_object(
            'heater_generic ' + self.chamber_heater, None) is not None

    def _read_temp(self, obj_name, eventtime):
        obj = self.printer.lookup_object(obj_name, None)
        if obj is None:
            return None
        return obj.get_status(eventtime).get('temperature')

    def _bed_temp(self, eventtime):
        return self._read_temp(self.bed_heater, eventtime)

    def _chamber_temp(self, eventtime):
        names = [self.chamber_sensor] if self.chamber_sensor else [
            'heater_generic ' + self.chamber_heater,
            'temperature_sensor chamber']
        for name in names:
            t = self._read_temp(name, eventtime)
            if t is not None:
                return t
        return None

    def _set_bed(self, target):
        self.gcode.run_script('SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f'
                              % (self.bed_heater, target))

    def _set_chamber(self, target):
        # Through ff-chamber.cfg's SET_HEATER_TEMPERATURE, which owns the
        # chamber_loop_fan coupling and the per-model gate.
        if target > 0. or self._has_chamber_heater():
            self.gcode.run_script(
                'SET_HEATER_TEMPERATURE HEATER=%s TARGET=%.1f'
                % (self.chamber_heater, target))

    # -- sweep job -------------------------------------------------------
    def _check_abort(self):
        job = self.job
        if job['abort'] or self.printer.is_shutdown():
            raise SweepAborted()
        stats = self.printer.lookup_object('print_stats', None)
        if stats is not None:
            state = stats.get_status(self.reactor.monotonic()).get('state')
            if state in ('printing', 'paused'):
                raise SweepAborted("a print started")

    def _sleep(self, seconds):
        end = self.reactor.monotonic() + seconds
        while True:
            self._check_abort()
            now = self.reactor.monotonic()
            self.status['remaining'] = max(0., end - now)
            if now >= end:
                return
            self.reactor.pause(min(end, now + 1.))

    def _wait_for_temps(self, bed, chamber):
        start = self.reactor.monotonic()
        while True:
            self._check_abort()
            now = self.reactor.monotonic()
            bed_now = self._bed_temp(now)
            ch = self._chamber_temp(now)
            # A bed has no cooling: air warmer than its target holds it
            # above the target, so as high as the chamber (+ tolerance) is
            # accepted. The temperatures actually measured are what the
            # map records and the analysis uses.
            upper = bed + self.bed_tolerance
            if ch is not None:
                upper = max(upper, ch + self.bed_tolerance)
            ok = (bed_now is not None
                  and bed - self.bed_tolerance <= bed_now <= upper)
            if chamber > 0.:
                # The chamber can only be heated: a hot bed may carry it past
                # the target, which is recorded rather than waited out.
                ok = ok and ch is not None and ch >= chamber - \
                    self.chamber_tolerance
            if ok:
                return
            if now - start > self.heat_timeout:
                raise self.gcode.error(
                    "thermal mesh: bed %.1f / chamber %.1f not reached in %ds"
                    % (bed, chamber, self.heat_timeout))
            self.status['remaining'] = 0.
            self.reactor.pause(now + 1.)

    def _set_cool_fan(self, speed):
        self.gcode.run_script('SET_FAN_SPEED FAN=%s SPEED=%.3f'
                              % (self.cool_fan, speed))

    def _cool_chamber(self, limit, timeout):
        """Wait for the chamber to fall to `limit` degC.

        True once it reads at or below limit, False if it is still above
        after timeout seconds. The chamber has no heat pump, only cool_fan
        (chamber_fan by default), which runs at cool_fan_speed for the wait
        and goes back to the speed it had afterwards.
        """
        fan = None
        if self.cool_fan and self.cool_fan_speed > 0.:
            fan = self.printer.lookup_object('fan_generic ' + self.cool_fan,
                                             None)
        if fan is None:
            return self._wait_chamber_down(limit, timeout)
        before = fan.get_status(self.reactor.monotonic()).get('speed', 0.)
        self._set_cool_fan(self.cool_fan_speed)
        try:
            return self._wait_chamber_down(limit, timeout)
        finally:
            self._set_cool_fan(before)

    def _wait_chamber_down(self, limit, timeout):
        start = self.reactor.monotonic()
        while True:
            self._check_abort()
            now = self.reactor.monotonic()
            ch = self._chamber_temp(now)
            if ch is None or ch <= limit:
                return True
            if now - start > timeout:
                return False
            self.status['remaining'] = max(0., timeout - (now - start))
            self.reactor.pause(now + 1.)

    def _probe_full_mesh(self):
        self.gcode.run_script(self.before_sample.render())
        self.gcode.run_script('BED_MESH_CALIBRATE')
        mesh = self.bed_mesh.get_mesh() if self.bed_mesh else None
        if mesh is None:
            raise self.gcode.error("thermal mesh: BED_MESH_CALIBRATE "
                                   "produced no mesh")
        matrix = mesh.get_probed_matrix()
        params = dict(mesh.get_mesh_params())
        self.gcode.run_script(self.after_sample.render())
        return matrix, params

    def _check_params(self, data, params):
        known = data.get('mesh_params')
        if known is None:
            data['mesh_params'] = params
            return
        for key in ('x_count', 'y_count', 'min_x', 'max_x', 'min_y', 'max_y'):
            if abs(float(known[key]) - float(params[key])) > 1e-3:
                raise self.gcode.error(
                    "thermal mesh: mesh %s changed from %s to %s during the "
                    "map; start a new sweep" % (key, known[key], params[key]))

    def cmd__THERMAL_MESH_PRECISION(self, gcmd):
        """Probe the points _measure_precision staged; the sweep reads the
        result. Takes NO probe parameters: the probe reads SAMPLES and the
        like off this command, and the readings must be taken exactly the
        way bed_mesh takes a node's."""
        self._precision = self._probe_points(
            gcmd, [{'x': x, 'y': y} for x, y in self._precision_plan],
            zero_ref=False)

    def _measure_precision(self, params):
        """(sigma, dof) of one mesh NODE reading, right after the mesh it
        judges: same plate, same heat, same probe settings.

        Each of precision_nodes() is read precision_samples times, visited
        in turn rather than touched in place, so every reading is a fresh
        approach like a mesh node's, and the spread is pooled over all nine.
        This used to touch the middle of the bed alone and pass the probe
        SAMPLES=<precision_samples>, which it took as touches per reading:
        each reading was then the median of ten touches at the bed's
        quietest spot, and sigma came out ~7x below a node's real noise."""
        nodes = precision_nodes(params)
        self._precision_plan = nodes * self.precision_samples
        self._precision = None
        self.gcode.run_script('_THERMAL_MESH_PRECISION')
        self.gcode.run_script(self.after_sample.render())
        if not self._precision or \
                len(self._precision) != len(self._precision_plan):
            raise self.gcode.error("thermal mesh: precision probe returned "
                                   "nothing")
        groups = [self._precision[i::len(nodes)] for i in range(len(nodes))]
        return pooled_std(groups)

    def _sample_until_stable(self, data, job, reached):
        """Probe meshes until one verifies the one before it.

        Each mesh after the first is a verification of the previous one:
        the probe's precision is measured straight after it, and the pair
        passes when verify_meshes finds them identical at job['confidence']
        -- average error indistinguishable from 0 and every node within the
        probe's noise -- and the probe itself is no noisier than
        job['max_sigma']. It goes on until a pair passes; max_samples, if
        set, caps it and keeps the last two unverified.

        `reached` is when both heaters reached their targets. Returns a dict:
        matrix (the node-by-node mean of the verified pair), samples, check,
        converged, soak_time (seconds from `reached` to the start of the
        first mesh of the pair: how long this pair needed to settle), and
        the bed and chamber readings at that start and at the end.
        """
        previous, check, n = None, None, 0
        while not self.max_samples or n < self.max_samples:
            n += 1
            self._check_abort()
            self.status['samples'] = n
            now = self.reactor.monotonic()
            start = {'time': now, 'bed': self._bed_temp(now),
                     'chamber': self._chamber_temp(now)}
            matrix, params = self._probe_full_mesh()
            self._check_params(data, params)
            if previous is not None:
                self.status['phase'] = 'verifying'
                sigma, dof = self._measure_precision(data['mesh_params'])
                # Every mesh here hangs off ONE reference touch, which moves
                # all of its nodes alike: before_sample_gcode's G28 Z homes on
                # the plate before each mesh, and a zero_reference_position,
                # if set, re-zeroes it on one probed point instead. Either
                # way the two meshes' averages differ by that touch's noise,
                # not by sigma/sqrt(n) -- tested without it, the average
                # failed on homing noise alone, mesh after mesh, however
                # long the plate had settled.
                check = verify_meshes(
                    previous[0], matrix, sigma, dof, job['confidence'],
                    shared_reference=True)
                check['sigma_ok'] = sigma <= job['max_sigma']
                self.status.update(phase='sampling', last_delta=check['mean'],
                                   sigma=sigma)
                verdict = ("identical" if check['identical'] else
                           "different")
                if not check['sigma_ok']:
                    verdict = "probe too noisy (sigma > %.4f)" % (
                        job['max_sigma'],)
                self.gcode.respond_info(
                    "thermal mesh: chamber %.1f bed %.1f sample %d: mean dZ "
                    "%+.4f +/- %.4f mm, rms %.4f, probe sigma %.4f (%d dof); "
                    "bias %.2f/%.2f, shape %.2f/%.2f: %s at %.0f%%"
                    % (job['chamber'], job['bed'], n, check['mean'],
                       check['mean_ci'], check['rms'], sigma, dof,
                       check['bias'], check['bias_limit'], check['shape'],
                       check['shape_limit'], verdict,
                       job['confidence'] * 100.))
                if check['identical'] and check['sigma_ok']:
                    break
            previous = (matrix, start)
        else:
            self.gcode.respond_info(
                "thermal mesh: chamber %.1f bed %.1f did not verify in %d "
                "samples; keeping the last two unverified"
                % (job['chamber'], job['bed'], n))
        converged = bool(check and check['identical'] and check['sigma_ok'])
        first = previous[1]
        now = self.reactor.monotonic()
        return {'matrix': average_matrices([previous[0], matrix]),
                'samples': n, 'check': check, 'converged': converged,
                'soak_time': first['time'] - reached,
                'bed_measured': [first['bed'], self._bed_temp(now)],
                'chamber_measured': [first['chamber'],
                                     self._chamber_temp(now)]}

    def _set_idle_timeout(self, value):
        self.gcode.run_script('SET_IDLE_TIMEOUT TIMEOUT=%.0f' % (value,))

    def _skip(self, data, chamber, bed, reason):
        data.setdefault('skipped', []).append(
            {'chamber': chamber, 'bed': bed, 'reason': reason})
        self._save(data)
        self.gcode.respond_info("thermal mesh: skipping chamber %.1f%s: %s"
                                % (chamber, '' if bed is None
                                   else ' bed %.1f' % (bed,), reason))

    def _run_sweep(self, eventtime):
        job = self.job
        data = job['data']
        idle = self.printer.lookup_object('idle_timeout', None)
        idle_restore = getattr(idle, 'idle_timeout', None)
        done = set(_key(e['chamber'], e['bed']) for e in data['entries'])
        state, message = 'done', ''
        try:
            self._set_idle_timeout(SWEEP_IDLE_TIMEOUT)
            first = True              # the one chamber soak, at the start
            current = None            # chamber target the heaters are set to
            set_started = False       # a pair of this chamber set has run
            skipped_chamber = None
            for idx, (chamber, bed) in enumerate(job['plan']):
                self.status['step'] = idx + 1
                if _key(chamber, bed) in done or chamber == skipped_chamber:
                    continue
                self._check_abort()
                job['chamber'], job['bed'] = chamber, bed
                self.status.update(phase='heating', bed_target=bed,
                                   chamber_target=chamber, last_delta=None,
                                   sigma=None, samples=0)
                if chamber != current:
                    set_started = False
                    self._set_chamber(chamber)
                    now = self.reactor.monotonic()
                    ch = self._chamber_temp(now)
                    if (chamber > 0. and ch is not None
                            and ch > chamber + self.chamber_tolerance):
                        # Down to a cooler set: the bed off so nothing
                        # holds the chamber up while it cools.
                        self.status['phase'] = 'cooling'
                        self._set_bed(0.)
                        if not self._cool_chamber(
                                chamber + self.chamber_tolerance,
                                job['cool_timeout']):
                            self._skip(data, chamber, None,
                                       "did not cool below %.1f in %ds"
                                       % (chamber + self.chamber_tolerance,
                                          job['cool_timeout']))
                            skipped_chamber = chamber
                            current = chamber
                            continue
                        self.status['phase'] = 'heating'
                    current = chamber
                now = self.reactor.monotonic()
                ch = self._chamber_temp(now)
                # The bed cannot be held more than bed_tolerance below the
                # air. A chamber a hair over the bed target still holds the
                # bed within that, and _wait_for_temps accepts it.
                limit = bed + self.bed_tolerance
                if ch is not None and ch > limit:
                    # Further over, on a heated set whose own target is at
                    # or below this bed target, the chamber is still coming
                    # down from the set before (the cool-down to a new set
                    # stops at target + chamber_tolerance, the bed == chamber
                    # pair needs target + bed_tolerance), or the previous,
                    # hotter bed is holding it up. Its heater is set lower,
                    # so it gets there: wait, with the bed at its own target,
                    # rather than skip a pair the plan says is reachable.
                    if chamber > 0. and bed >= chamber:
                        self.status['phase'] = 'cooling'
                        self._set_bed(bed)
                        if self._cool_chamber(limit, job['cool_timeout']):
                            ch = self._chamber_temp(self.reactor.monotonic())
                        self.status['phase'] = 'heating'
                    if ch is not None and ch > limit:
                        self._skip(data, chamber, bed,
                                   "more than %.1f below the chamber's %.1f"
                                   % (self.bed_tolerance, ch))
                        continue
                self._set_bed(bed)
                self._wait_for_temps(bed, chamber)
                reached = self.reactor.monotonic()
                self.status['phase'] = 'soaking'
                if first:
                    soak = max(job['chamber_soak'], job['max_soak'])
                elif not set_started:
                    # A later chamber set: the long bed soak, or the
                    # optional per-step chamber soak if that is longer.
                    soak = max(job['max_soak'], job['chamber_step_soak'])
                else:
                    soak = job['soak']
                self._sleep(soak)
                first, set_started = False, True
                self.status['phase'] = 'sampling'
                got = self._sample_until_stable(data, job, reached)
                check = got['check']
                data['entries'].append({
                    'chamber': chamber, 'bed': bed,
                    'bed_measured': got['bed_measured'],
                    'chamber_measured': got['chamber_measured'],
                    'samples': got['samples'],
                    'soak_time': got['soak_time'],
                    'delta': None if check is None else check['mean'],
                    'verification': check,
                    'converged': got['converged'], 'matrix': got['matrix']})
                self._save(data)
                self.gcode.respond_info(
                    "thermal mesh: chamber %.1f bed %.1f %s after %d meshes; "
                    "settled %.0f min after reaching temperature"
                    % (chamber, bed, 'verified' if got['converged']
                       else 'NOT verified', got['samples'],
                       got['soak_time'] / 60.))
            try:
                if self._analyze(data, job['points'], self.min_spacing,
                                 report=True) is None:
                    message = ("map has a single temperature; nothing to "
                               "analyze")
            except ValueError as e:
                # The map is kept; the points are what is missing.
                message = "analysis: %s" % (e,)
        except SweepAborted as e:
            state, message = 'aborted', str(e) or 'aborted'
        except Exception as e:
            logging.exception("thermal mesh sweep")
            state, message = 'error', str(e)
        finally:
            try:
                if not (job['keep_heat'] and state == 'done'):
                    self._set_bed(0.)
                    self._set_chamber(0.)
                if idle_restore is not None:
                    self._set_idle_timeout(idle_restore)
            except Exception:
                logging.exception("thermal mesh sweep cleanup")
            try:
                self._wait_saved(yield_reactor=True)
            except Exception:
                logging.exception("thermal mesh sweep: final save")
            skipped = len(data.get('skipped', []))
            if skipped and not message:
                message = "%d skipped (see 'skipped' in the map file)" % (
                    skipped,)
            self.status.update(state=state, phase='', remaining=0.,
                               message=message)
            self.job = None
            self.gcode.respond_info(
                "thermal mesh: sweep %s after %d/%d pairs (%d stored in %s)%s"
                % (state, self.status['step'], self.status['total'],
                   len(data['entries']), self.data_file,
                   ': ' + message if message else ''))

    cmd_THERMAL_MESH_SWEEP_help = (
        "Map the bed mesh over bed and chamber temperature")

    def cmd_THERMAL_MESH_SWEEP(self, gcmd):
        if self.job is not None:
            raise gcmd.error("thermal mesh: a sweep is already running")
        if self.bed_mesh is None:
            raise gcmd.error("thermal mesh: needs [bed_mesh]")
        homed = self.printer.lookup_object('toolhead').get_status(
            self.reactor.monotonic()).get('homed_axes', '')
        if 'x' not in homed or 'y' not in homed:
            raise gcmd.error("thermal mesh: home X and Y first (G28)")
        bed_min = gcmd.get_float('BED_MIN')
        bed_max = gcmd.get_float('BED_MAX', bed_min)
        bed_step = gcmd.get_float('BED_STEP', 10., above=0.)
        ch_min = gcmd.get_float('CHAMBER_MIN', 0., minval=0.)
        ch_max = gcmd.get_float('CHAMBER_MAX', ch_min)
        ch_step = gcmd.get_float('CHAMBER_STEP', 10., above=0.)
        soak = gcmd.get_float('SOAK', 600., minval=0.)
        max_soak = gcmd.get_float('MAX_SOAK', max(soak, 1800.), minval=0.)
        chamber_soak = gcmd.get_float('CHAMBER_SOAK', 3600., minval=0.)
        # Optional: a chamber soak at every later chamber step too.
        chamber_step_soak = gcmd.get_float('CHAMBER_STEP_SOAK', 0., minval=0.)
        cool_timeout = gcmd.get_float('COOL_TIMEOUT', self.cool_timeout,
                                      above=0.)
        confidence = gcmd.get_float('CONFIDENCE', self.confidence,
                                    minval=0.5, maxval=0.999)
        max_sigma = gcmd.get_float('MAX_SIGMA', self.max_probe_sigma,
                                   above=0.)
        resume = gcmd.get_int('RESUME', 0)
        try:
            beds = temperature_steps(bed_min, bed_max, bed_step)
            chambers = temperature_steps(ch_min, ch_max, ch_step)
        except ValueError as e:
            raise gcmd.error("thermal mesh: %s" % (e,))
        if chambers[-1] > 0. and not self._has_chamber_heater():
            raise gcmd.error("thermal mesh: this printer has no [heater_generic"
                             " %s]; leave CHAMBER_MIN/MAX at 0"
                             % (self.chamber_heater,))
        sweep = {'bed_temps': beds, 'chamber_temps': chambers, 'soak': soak,
                 'max_soak': max_soak, 'chamber_soak': chamber_soak,
                 'chamber_step_soak': chamber_step_soak,
                 'cool_timeout': cool_timeout, 'confidence': confidence,
                 'max_sigma': max_sigma}
        data = self._load() if resume else None
        if data is None:
            self._wait_saved()        # nothing may land after the move
            if not resume and os.path.exists(self.data_file):
                os.replace(self.data_file, self.data_file + '.bak')
                gcmd.respond_info("thermal mesh: previous map kept as %s.bak"
                                  % (self.data_file,))
            data = {'version': DATA_VERSION, 'mesh_params': None,
                    'entries': []}
        data['sweep'] = sweep
        data['skipped'] = []
        data.pop('analysis', None)
        plan = sweep_plan(beds, chambers)
        if not plan:
            raise gcmd.error("thermal mesh: every bed target is below every "
                             "chamber target")
        self.job = {'plan': plan, 'data': data, 'abort': False,
                    'soak': soak, 'max_soak': max_soak,
                    'chamber_soak': chamber_soak,
                    'chamber_step_soak': chamber_step_soak,
                    'cool_timeout': cool_timeout, 'confidence': confidence,
                    'max_sigma': max_sigma,
                    'keep_heat': gcmd.get_int('KEEP_HEAT', 0),
                    'points': gcmd.get_int('POINTS', 4, minval=1),
                    'chamber': None, 'bed': None}
        self.status.update(state='running', step=0, total=len(plan),
                           message='')
        stored = len(data['entries'])
        gcmd.respond_info(
            "thermal mesh: %d pairs (chamber %s x bed %s, hottest first, bed "
            "never below chamber), %d already stored; chamber soak %ds once "
            "and %ds per chamber step, soak %ds at each set's hottest bed and %ds per bed step, "
            "cool-down limit %ds, meshes verified identical at %.0f%% "
            "(probe sigma <= %.4f mm). "
            "THERMAL_MESH_STATUS to follow, THERMAL_MESH_ABORT to stop."
            % (len(plan), _fmt_list(list(reversed(chambers))),
               _fmt_list(list(reversed(beds))), stored, chamber_soak,
               chamber_step_soak, max_soak, soak, cool_timeout, confidence * 100., max_sigma))
        self.reactor.register_callback(self._run_sweep)

    cmd_THERMAL_MESH_ABORT_help = "Stop a running thermal mesh sweep"

    def cmd_THERMAL_MESH_ABORT(self, gcmd):
        if self.job is None:
            gcmd.respond_info("thermal mesh: no sweep is running")
            return
        self.job['abort'] = True
        gcmd.respond_info("thermal mesh: stopping after the current step")

    cmd_THERMAL_MESH_STATUS_help = "Show the thermal mesh sweep and map"

    def cmd_THERMAL_MESH_STATUS(self, gcmd):
        s = self.status
        lines = ["thermal mesh: %s" % (s['state'],)]
        if s['state'] == 'running':
            lines.append("pair %d/%d: chamber %.1f bed %.1f, %s"
                         % (s['step'], s['total'], s['chamber_target'],
                            s['bed_target'], s['phase']))
            if s['phase'] == 'soaking':
                lines.append("soak remaining %ds" % (s['remaining'],))
            if s['phase'] == 'cooling':
                lines.append("chamber cooling, up to %ds more"
                             % (s['remaining'],))
            if s['phase'] in ('sampling', 'verifying'):
                lines.append("sample %d, last mean dZ %s, probe sigma %s"
                             % (s['samples'], 'n/a' if s['last_delta'] is None
                                else '%+.4f mm' % s['last_delta'],
                                'n/a' if s['sigma'] is None
                                else '%.4f mm' % s['sigma']))
        elif s['message']:
            lines.append(s['message'])
        data = self._load()
        if data and data.get('entries'):
            lines.append("map: %d entries in %s"
                         % (len(data['entries']), self.data_file))
            for p in data.get('analysis', {}).get('points', []):
                lines.append("  point (%.1f, %.1f) %s" % (p['x'], p['y'],
                                                          p['axis']))
            for k in data.get('skipped', []):
                lines.append("  skipped chamber %.1f%s: %s" % (
                    k['chamber'], '' if k['bed'] is None
                    else ' bed %.1f' % (k['bed'],), k['reason']))
        gcmd.respond_info("\n".join(lines))

    # -- analysis --------------------------------------------------------
    def _analyze(self, data, count, min_spacing, report=False):
        entries = data['entries']
        model = thermal_model(entries)
        if model is None:
            return None
        params = data['mesh_params']
        points = select_points(params, model, count, min_spacing)
        separation = point_separation(model, points)
        chamber_map, bed_map = (
            None if model[a] is None else
            [[abs(v) for v in row] for row in model[a]]
            for a in ('chamber', 'bed'))
        data['analysis'] = {'chamber_sens': chamber_map, 'bed_sens': bed_map,
                            'model': model, 'points': points,
                            'separation': separation,
                            'min_spacing': min_spacing}
        self._save(data)
        if report:
            lines = ["thermal mesh: sensitivity, each heater with the other "
                     "held (um/degC, peak / median):"]
            for name, smap in (('chamber', chamber_map), ('bed', bed_map)):
                if smap is None:
                    lines.append("  %s: single target, not mapped" % (name,))
                    continue
                flat = sorted(v for row in smap for v in row)
                lines.append("  %s: %.2f / %.2f" % (
                    name, flat[-1] * 1000., flat[len(flat) // 2] * 1000.))
            if model['bed'] is not None and model['chamber'] is not None:
                lines.append("  measured bed/chamber correlation in the map: "
                             "%.2f" % (model['correlation'],))
            lines.append("  straight-line model off the map by %.1f um rms"
                         % (model['fit_rms'] * 1000.,))
            lines.append("probe points:")
            for p in points:
                lines.append(
                    "  (%.1f, %.1f) %s: chamber %.2f, bed %.2f um/degC"
                    % (p['x'], p['y'], p['axis'], p['chamber_sens'] * 1000.,
                       p['bed_sens'] * 1000.))
            if separation is not None:
                lines.append("bed vs chamber at these points: overlap %.2f "
                             "(0 fully separable, 1 indistinguishable)"
                             % (separation,))
            self.gcode.respond_info("\n".join(lines))
        return points

    cmd_THERMAL_MESH_ANALYZE_help = (
        "Separate the bed's and the chamber's warp and choose probe points")

    def cmd_THERMAL_MESH_ANALYZE(self, gcmd):
        if self.job is not None:
            raise gcmd.error("thermal mesh: a sweep is running")
        data = self._require_map(need_points=False)
        try:
            points = self._analyze(
                data, gcmd.get_int('POINTS', 4, minval=1),
                gcmd.get_float('MIN_SPACING', self.min_spacing, minval=0.),
                report=True)
        except ValueError as e:
            raise gcmd.error("thermal mesh: %s" % (e,))
        if points is None:
            raise gcmd.error("thermal mesh: the map has a single temperature "
                             "pair; there is nothing to compare")

    # -- apply -----------------------------------------------------------
    def _let_reactor_run(self):
        self.reactor.pause(self.reactor.monotonic() + 0.001)

    def _probe_finalize(self, offsets, positions):
        self._probed = (offsets, positions)

    def _zero_ref(self):
        mgr = getattr(getattr(self.bed_mesh, 'bmc', None), 'probe_mgr', None)
        if mgr is None:
            return bed_mesh_module.ZrefMode.DISABLED, None
        return mgr.get_zero_ref_mode(), mgr.get_zero_ref_pos()

    def _probe_points(self, gcmd, points, zero_ref=True):
        """Z at each point on the same basis as bed_mesh's probed matrix,
        or raw touches with zero_ref=False."""
        coords = [(p['x'], p['y']) for p in points]
        zref_mode, zref_pos = self._zero_ref()
        if not zero_ref:
            zref_mode = bed_mesh_module.ZrefMode.DISABLED
        # With a zero_reference_position bed_mesh shifts the whole mesh so
        # that point reads 0; probing it too puts these on the same footing.
        if zref_mode != bed_mesh_module.ZrefMode.DISABLED:
            coords.append(tuple(zref_pos))
        self._probed = None
        self.probe_helper.update_probe_points(coords, len(coords))
        self.probe_helper.start_probe(gcmd)
        if self._probed is None:
            raise gcmd.error("thermal mesh: probing returned no result")
        offsets, positions = self._probed
        z = [pos[2] - offsets[2] for pos in positions]
        if zref_mode != bed_mesh_module.ZrefMode.DISABLED:
            ref = z.pop()
            z = [v - ref for v in z]
        return z

    cmd_THERMAL_MESH_APPLY_help = (
        "Probe the chosen points and load the closest effective mesh")

    def cmd_THERMAL_MESH_APPLY(self, gcmd):
        if self.job is not None:
            raise gcmd.error("thermal mesh: a sweep is running")
        if self.bed_mesh is None:
            raise gcmd.error("thermal mesh: needs [bed_mesh]")
        method = gcmd.get('METHOD', 'model').lower()
        if method not in METHODS:
            raise gcmd.error("thermal mesh: METHOD must be one of %s"
                             % (', '.join(METHODS),))
        correct = gcmd.get('CORRECT', 'none').lower()
        if correct not in CORRECTIONS:
            raise gcmd.error("thermal mesh: CORRECT must be one of %s"
                             % (', '.join(CORRECTIONS),))
        relative = gcmd.get('MATCH', 'absolute').lower() == 'relative'
        fallback = gcmd.get('FALLBACK', None)
        max_rms = gcmd.get_float('MAX_RMS', None, above=0.)
        # Cleared first: a caller reading last_apply after this command must
        # never see the previous run's result.
        self.last_apply = {'accepted': False, 'reason': 'running'}

        def give_up(reason, **extra):
            """No map, or a match too far off. Without FALLBACK that is an
            error; FALLBACK=<profile> loads that profile instead (a print
            start: no map is no reason to refuse the print); FALLBACK=NONE
            loads nothing and leaves the decision to the caller, which reads
            printer.ff_thermal_mesh.last_apply."""
            self.last_apply = dict(extra, accepted=False, reason=reason)
            if not fallback:
                raise gcmd.error("thermal mesh: %s" % (reason,))
            if fallback.upper() == 'NONE':
                gcmd.respond_info("thermal mesh not used: %s" % (reason,))
                return
            gcmd.respond_info("thermal mesh not used: %s -- loading %s instead"
                              % (reason, fallback))
            self.gcode.run_script_from_command(
                'BED_MESH_PROFILE LOAD=%s' % (fallback,))

        try:
            data = self._require_map(need_points=method != 'temperature')
        except self.gcode.error as e:
            return give_up(str(e).replace("thermal mesh: ", ""))
        entries, params = data['entries'], data['mesh_params']
        points = data.get('analysis', {}).get('points', [])
        measured = None
        lines = []
        try:
            if method == 'temperature':
                now = self.reactor.monotonic()
                bed = gcmd.get_float('BED', self._bed_temp(now) or 0.)
                chamber = gcmd.get_float('CHAMBER',
                                         self._chamber_temp(now) or 0.)
                weights, chamber, bed = bilinear_weights(entries, chamber, bed)
                lines.append("lookup at chamber %.1f bed %.1f" % (chamber, bed))
                correct = 'none'
            else:
                coords = [[p['x'], p['y']] for p in points]
                last = self._last_probe
                now = self.reactor.monotonic()
                if (gcmd.get_int('REUSE_PROBE', 0) and last is not None
                        and last['coords'] == coords
                        and now - last['time'] < REUSE_PROBE_SECONDS):
                    # The same four points, probed moments ago by an
                    # earlier APPLY in this print start: no need to touch
                    # them again just to judge the match differently.
                    measured = list(last['measured'])
                    lines.append("reusing the points probed %.0f s ago"
                                 % (now - last['time'],))
                else:
                    if gcmd.get_int('PREPARE', 1):
                        self.gcode.run_script_from_command(
                            self.before_sample.render())
                    measured = self._probe_points(gcmd, points)
                    self.gcode.run_script_from_command(
                        self.after_sample.render())
                    self._last_probe = {'time': self.reactor.monotonic(),
                                        'coords': coords,
                                        'measured': list(measured)}
                # The matching below is pure Python on a slow host. With a
                # move still queued (after_sample_gcode's G1 Z10) a stall
                # of a second ends in "Timer too close", so empty the queue
                # first, and let the reactor run between map cells.
                self.printer.lookup_object('toolhead').wait_moves()
                if method == 'model':
                    model = thermal_model(entries)
                    if model is None:
                        raise ValueError("the map has a single temperature "
                                         "pair")
                    bed, chamber = fit_model(model, points, measured,
                                             relative)
                    matrix, weights = model_mesh(
                        model, entries, bed, chamber,
                        gcmd.get_int('K', 4, minval=1),
                        gcmd.get_float('POWER', 2., above=0.))
                    lines.append("fit chamber %.1f bed %.1f" % (chamber, bed))
                elif method == 'bilinear':
                    fit = fit_bilinear(entries, points, measured, relative,
                                       checkpoint=self._let_reactor_run)
                    weights = fit['weights']
                    lines.append("fit chamber %.1f bed %.1f, rms %.4f mm"
                                 % (fit['chamber'], fit['bed'], fit['rms']))
                else:
                    ranked = rank_entries(entries, points, measured, relative)
                    if method == 'nearest':
                        weights = [(1., ranked[0][1])]
                    else:
                        weights = idw_weights(
                            ranked, gcmd.get_int('K', 4, minval=1),
                            gcmd.get_float('POWER', 2., above=0.))
                    lines.append("closest: " + ", ".join(
                        "chamber %.0f bed %.0f %.4f mm" % (
                            e['chamber'], e['bed'], rms)
                        for rms, e in ranked[:3]))
        except ValueError as e:
            raise gcmd.error("thermal mesh: %s" % (e,))
        if method == 'model':
            residuals = apply_correction(matrix, params, points, measured,
                                         correct)
        else:
            matrix, residuals = effective_mesh(weights, params, points,
                                               measured, correct)
        # How far the map's best answer is from what was just probed, judged
        # the way MATCH compared them (relative: shape only).
        fit_rms = None
        if residuals:
            fit_rms = residual_rms([-r for r in residuals],
                                   [0.] * len(residuals), relative)
            lines.append("fit rms %.4f mm%s" % (
                fit_rms, "" if max_rms is None else
                " (limit %.4f)" % (max_rms,)))
        if max_rms is not None and fit_rms is not None and fit_rms > max_rms:
            gcmd.respond_info("thermal mesh (%s):\n%s" % (method,
                                                          "\n".join(lines)))
            return give_up("the probed points are %.4f mm rms off the map, "
                           "more than MAX_RMS %.4f" % (fit_rms, max_rms),
                           rms=fit_rms, measured=measured,
                           residuals=residuals)
        lines.append(("model, corrected toward: " if method == 'model'
                      else "blend: ") + ", ".join(
            "%.2f x (chamber %.0f bed %.0f)" % (w, e['chamber'], e['bed'])
            for w, e in sorted(weights, key=lambda t: -t[0])))
        if residuals:
            lines.append("residual at points (mm): " + " ".join(
                "%+.4f" % r for r in residuals) + " ; correction %s" % correct)
        name = gcmd.get('SAVE', None)
        if not gcmd.get_int('DRY_RUN', 0):
            zmesh = bed_mesh_module.ZMesh(dict(params),
                                          name or 'thermal-effective')
            try:
                zmesh.build_mesh(matrix)
            except bed_mesh_module.BedMeshError as e:
                raise gcmd.error(str(e))
            self.bed_mesh.set_mesh(zmesh)
            if name:
                self.bed_mesh.save_profile(name)
            lines.append("effective mesh loaded")
        else:
            lines.append("dry run: mesh not loaded")
        self.last_apply = {
            'accepted': True, 'reason': '', 'rms': fit_rms,
            'method': method, 'correct': correct,
            'measured': measured, 'residuals': residuals,
            'weights': [[w, e['chamber'], e['bed']] for w, e in weights]}
        gcmd.respond_info("thermal mesh (%s):\n%s" % (method,
                                                      "\n".join(lines)))


def _fmt_list(values):
    return "[%s]" % (", ".join("%g" % v for v in values),)


def load_config(config):
    return FFThermalMesh(config)
