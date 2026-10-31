# Creator 5 thermal bed-mesh map
#
# Copyright (C) 2026
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Map how the bed's shape follows bed and chamber temperature, then pick
the right mesh for a print by probing four points instead of a full grid.

THREE STAGES

1. THERMAL_MESH_SWEEP walks every (chamber, bed) target pair in a serpentine:
   bed up at the lowest chamber target, chamber up one step, bed down, chamber
   up, bed up, ... so each move changes one heater by one step. At every pair
   it waits for both heaters, heat-soaks, then probes full meshes until the
   mean |dZ| between the last two is under THRESHOLD. The average of those
   two is stored. Every finished pair is written to the data file at once, so
   an abort or a power cut loses at most the pair in progress; RESUME=1 picks
   the sweep back up.

2. THERMAL_MESH_ANALYZE turns the map into two sensitivity maps: for every
   mesh node, the RMS of dZ/dT between neighbouring chamber targets at a fixed
   bed target, and the same for bed steps at a fixed chamber target. It picks
   the nodes that move the most -- alternating chamber and bed, kept apart by
   min_point_spacing -- and saves them. The sweep runs this when it finishes.

3. THERMAL_MESH_APPLY probes only those points, compares them with every map
   entry and builds an "effective" mesh from the closest matches. It does not
   need to know the temperatures: the bed's shape is the measurement.

The sweep runs as a reactor callback rather than inside one G-code command,
so the G-code queue is free between steps: THERMAL_MESH_STATUS and
THERMAL_MESH_ABORT work while it soaks. A full mesh probe still holds the
queue until it finishes, so an abort lands at the end of the current probe.

The module level functions hold all of the arithmetic and touch nothing in
klippy; qa/static/test_ff_thermal_mesh.py tests them directly.
"""

import math

DATA_VERSION = 1
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
    """(chamber, bed) pairs, bed alternating up and down per chamber step."""
    plan = []
    for i, chamber in enumerate(chamber_temps):
        beds = bed_temps if i % 2 == 0 else list(reversed(bed_temps))
        for bed in beds:
            plan.append((chamber, bed))
    return plan


def mean_abs_delta(a, b):
    diffs = [abs(za - zb) for ra, rb in zip(a, b) for za, zb in zip(ra, rb)]
    return sum(diffs) / len(diffs)


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


def _axis_rms(pairs, rows, cols):
    if not pairs:
        return None
    out = [[0.] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            sq = [((m2[r][c] - m1[r][c]) / dt) ** 2 for m1, m2, dt in pairs]
            out[r][c] = math.sqrt(sum(sq) / len(sq))
    return out


def sensitivity_maps(entries):
    """Per-node RMS of dZ/dT (mm/degC) along each temperature axis.

    The chamber map compares neighbouring chamber targets at the same bed
    target; the bed map neighbouring bed targets at the same chamber target.
    An axis with a single target has no pairs and returns None.
    """
    chambers, beds, table = build_grid(entries)
    first = entries[0]['matrix']
    rows, cols = len(first), len(first[0])
    chamber_pairs = []
    for bed in beds:
        for c1, c2 in zip(chambers, chambers[1:]):
            m1, m2 = table.get((c1, bed)), table.get((c2, bed))
            if m1 is not None and m2 is not None:
                chamber_pairs.append((m1, m2, c2 - c1))
    bed_pairs = []
    for chamber in chambers:
        for b1, b2 in zip(beds, beds[1:]):
            m1, m2 = table.get((chamber, b1)), table.get((chamber, b2))
            if m1 is not None and m2 is not None:
                bed_pairs.append((m1, m2, b2 - b1))
    return (_axis_rms(chamber_pairs, rows, cols),
            _axis_rms(bed_pairs, rows, cols))


def select_points(params, chamber_map, bed_map, chamber_points=2,
                  bed_points=2, min_spacing=60.):
    """Pick the most temperature-sensitive mesh nodes, kept apart.

    Picks alternate between the axes (chamber, bed, chamber, ...), so neither
    axis takes all of the best nodes when both peak in the same region. A
    node closer than min_spacing to an earlier pick is skipped; if the spacing
    leaves too few nodes it is halved until the count is reached.
    """
    if chamber_map is None:
        bed_points += chamber_points
        chamber_points = 0
    if bed_map is None:
        chamber_points += bed_points
        bed_points = 0
    if chamber_map is None and bed_map is None:
        raise ValueError("the map needs at least two temperatures on one axis")
    rows, cols = params['y_count'], params['x_count']
    nodes = [(r, c) for r in range(rows) for c in range(cols)]
    ranked = {}
    for axis, smap in (('chamber', chamber_map), ('bed', bed_map)):
        if smap is not None:
            ranked[axis] = sorted(nodes, key=lambda n: -smap[n[0]][n[1]])
    queue = []
    remaining = {'chamber': chamber_points, 'bed': bed_points}
    while remaining['chamber'] or remaining['bed']:
        for axis in ('chamber', 'bed'):
            if remaining[axis]:
                queue.append(axis)
                remaining[axis] -= 1
    spacing = float(min_spacing)
    while True:
        chosen = []
        for axis in queue:
            for r, c in ranked[axis]:
                if any(p['row'] == r and p['col'] == c for p in chosen):
                    continue
                x, y = node_xy(params, r, c)
                if all(math.hypot(x - p['x'], y - p['y']) >= spacing
                       for p in chosen):
                    smap = chamber_map if axis == 'chamber' else bed_map
                    chosen.append({
                        'x': round(x, 3), 'y': round(y, 3), 'row': r, 'col': c,
                        'axis': axis,
                        'chamber_rms': (chamber_map[r][c]
                                        if chamber_map is not None else 0.),
                        'bed_rms': bed_map[r][c] if bed_map is not None else 0.,
                        'score': smap[r][c]})
                    break
        if len(chosen) == len(queue) or spacing < 1e-3:
            return chosen
        spacing /= 2.


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


def fit_bilinear(entries, points, measured, relative=False, steps=20):
    """Find the temperature pair whose interpolated mesh best fits the probe.

    The map is a grid over (chamber, bed). Inside every cell whose four
    corners were sampled, the interpolated signature is bilinear in the cell
    coordinates u, v, so a coarse grid search over u, v followed by a finer
    one around the best hit finds the minimum without any solver. Returns a
    dict with rms, chamber, bed and weights [(weight, entry)].
    """
    chambers, beds, table = build_grid(entries)
    by_key = {_key(e['chamber'], e['bed']): e for e in entries}
    sigs = {k: signature(m, points) for k, m in table.items()}

    def evaluate(ci, bi, u, v):
        corners = _bilinear_corners(chambers, beds, ci, bi, u, v)
        pred = [sum(w * sigs[k][i] for w, k in corners)
                for i in range(len(points))]
        return residual_rms(pred, measured, relative), corners

    def search(ci, bi, u0, u1, v0, v1, n):
        best = None
        us = [u0 + (u1 - u0) * i / n for i in range(n + 1)]
        vs = [v0 + (v1 - v0) * i / n for i in range(n + 1)]
        if ci[0] == ci[1]:
            us = [0.]
        if bi[0] == bi[1]:
            vs = [0.]
        for u in us:
            for v in vs:
                rms, corners = evaluate(ci, bi, u, v)
                if best is None or rms < best[0]:
                    best = (rms, u, v, corners)
        return best

    best = None
    for ci in _cells(chambers):
        for bi in _cells(beds):
            keys = set((chambers[i], beds[j]) for i in ci for j in bi)
            if not keys.issubset(table):
                continue
            hit = search(ci, bi, 0., 1., 0., 1., steps)
            if best is None or hit[0] < best[0][0]:
                best = (hit, ci, bi)
    if best is None:
        raise ValueError("no fully sampled cell in the map")
    (rms, u, v, _), ci, bi = best
    h = 1. / steps
    rms, u, v, corners = search(ci, bi, max(0., u - h), min(1., u + h),
                                max(0., v - h), min(1., v + h), steps)
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


def effective_mesh(weights, params, points=None, measured=None,
                   correct='none'):
    """Blend the weighted entries and apply the requested correction.

    Returns (matrix, residuals after blending, before correction).
    """
    matrix = average_matrices([e['matrix'] for _, e in weights],
                              [w for w, _ in weights])
    residuals = []
    if points and measured is not None:
        residuals = [m - z for m, z in zip(measured,
                                           signature(matrix, points))]
        surface = correction_surface(points, residuals, correct)
        for r in range(len(matrix)):
            for c in range(len(matrix[r])):
                matrix[r][c] += surface(*node_xy(params, r, c))
    return matrix, residuals
