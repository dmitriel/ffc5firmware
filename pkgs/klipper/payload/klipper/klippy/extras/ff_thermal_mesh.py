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

import json
import logging
import math
import os

from . import bed_mesh as bed_mesh_module
from . import probe as probe_module

DATA_VERSION = 1
METHODS = ('bilinear', 'idw', 'nearest', 'temperature')
CORRECTIONS = ('none', 'offset', 'plane')
# Far beyond any sweep; restored to the configured value when the sweep ends.
SWEEP_IDLE_TIMEOUT = 864000.


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
        self.max_samples = config.getint('max_samples', 6, minval=2)
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
        # Our own helper, not bed_mesh's: ff_bed_mesh patches that one, and
        # a 4-point probe has no use for its dynamic lift.
        self.probe_helper = probe_module.ProbePointsHelper(
            config, self._probe_finalize, default_points=[(0., 0.)])
        self.probe_helper.use_xy_offsets(True)
        self._probed = None
        self.bed_mesh = None
        self.job = None
        self.status = {'state': 'idle', 'step': 0, 'total': 0, 'phase': '',
                       'bed_target': 0., 'chamber_target': 0.,
                       'last_delta': None, 'samples': 0, 'remaining': 0.,
                       'message': ''}
        self.last_apply = {}
        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.printer.register_event_handler('klippy:shutdown',
                                            self._handle_shutdown)
        for name in ('SWEEP', 'ABORT', 'STATUS', 'ANALYZE', 'APPLY'):
            cmd = 'THERMAL_MESH_' + name
            func = getattr(self, 'cmd_' + cmd)
            self.gcode.register_command(cmd, func,
                                        desc=getattr(self, func.__name__
                                                     + '_help'))

    def _handle_connect(self):
        self.bed_mesh = self.printer.lookup_object('bed_mesh', None)

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

    def _save(self, data):
        tmp = self.data_file + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.data_file)

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
            ok = bed_now is not None and abs(bed_now - bed) <= self.bed_tolerance
            if chamber > 0.:
                # The chamber can only be heated: a hot bed may carry it past
                # the target, which is recorded rather than waited out.
                ch = self._chamber_temp(now)
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

    def _sample_until_stable(self, data, threshold):
        job = self.job
        previous, delta = None, None
        for n in range(1, self.max_samples + 1):
            self._check_abort()
            self.status['samples'] = n
            matrix, params = self._probe_full_mesh()
            self._check_params(data, params)
            if previous is not None:
                delta = mean_abs_delta(previous, matrix)
                self.status['last_delta'] = delta
                self.gcode.respond_info(
                    "thermal mesh: chamber %.1f bed %.1f sample %d: "
                    "mean |dZ| %.4f mm (want < %.4f)"
                    % (job['chamber'], job['bed'], n, delta, threshold))
                if delta < threshold:
                    return average_matrices([previous, matrix]), n, delta, True
            previous = matrix
        self.gcode.respond_info(
            "thermal mesh: chamber %.1f bed %.1f did not settle in %d samples; "
            "keeping the last two" % (job['chamber'], job['bed'], n))
        return average_matrices([previous, matrix]), n, delta, False

    def _set_idle_timeout(self, value):
        self.gcode.run_script('SET_IDLE_TIMEOUT TIMEOUT=%.0f' % (value,))

    def _run_sweep(self, eventtime):
        job = self.job
        data = job['data']
        idle = self.printer.lookup_object('idle_timeout', None)
        idle_restore = getattr(idle, 'idle_timeout', None)
        done = set(_key(e['chamber'], e['bed']) for e in data['entries'])
        state, message = 'done', ''
        try:
            self._set_idle_timeout(SWEEP_IDLE_TIMEOUT)
            prev_chamber = None
            for idx, (chamber, bed) in enumerate(job['plan']):
                self.status['step'] = idx + 1
                if _key(chamber, bed) in done:
                    continue
                self._check_abort()
                job['chamber'], job['bed'] = chamber, bed
                self.status.update(phase='heating', bed_target=bed,
                                   chamber_target=chamber, last_delta=None,
                                   samples=0)
                chamber_changed = chamber != prev_chamber
                if chamber_changed:
                    self._set_chamber(chamber)
                self._set_bed(bed)
                self._wait_for_temps(bed, chamber)
                self.status['phase'] = 'soaking'
                self._sleep(job['chamber_soak'] if chamber_changed
                            else job['soak'])
                self.status['phase'] = 'sampling'
                now = self.reactor.monotonic()
                bed_start, ch_start = self._bed_temp(now), \
                    self._chamber_temp(now)
                matrix, samples, delta, converged = self._sample_until_stable(
                    data, job['threshold'])
                now = self.reactor.monotonic()
                data['entries'].append({
                    'chamber': chamber, 'bed': bed,
                    'bed_measured': [bed_start, self._bed_temp(now)],
                    'chamber_measured': [ch_start, self._chamber_temp(now)],
                    'samples': samples, 'delta': delta,
                    'converged': converged, 'matrix': matrix})
                self._save(data)
                prev_chamber = chamber
            if self._analyze(data, job['chamber_points'], job['bed_points'],
                             self.min_spacing, report=True) is None:
                message = "map has a single temperature; nothing to analyze"
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
        chamber_soak = gcmd.get_float('CHAMBER_SOAK', soak, minval=0.)
        threshold = gcmd.get_float('THRESHOLD', 0.01, above=0.)
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
                 'chamber_soak': chamber_soak, 'threshold': threshold}
        data = self._load() if resume else None
        if data is None:
            if not resume and os.path.exists(self.data_file):
                os.replace(self.data_file, self.data_file + '.bak')
                gcmd.respond_info("thermal mesh: previous map kept as %s.bak"
                                  % (self.data_file,))
            data = {'version': DATA_VERSION, 'mesh_params': None,
                    'entries': []}
        data['sweep'] = sweep
        data.pop('analysis', None)
        plan = sweep_plan(beds, chambers)
        self.job = {'plan': plan, 'data': data, 'abort': False,
                    'soak': soak, 'chamber_soak': chamber_soak,
                    'threshold': threshold,
                    'keep_heat': gcmd.get_int('KEEP_HEAT', 0),
                    'chamber_points': gcmd.get_int('CHAMBER_POINTS', 2,
                                                   minval=0),
                    'bed_points': gcmd.get_int('BED_POINTS', 2, minval=0),
                    'chamber': None, 'bed': None}
        self.status.update(state='running', step=0, total=len(plan),
                           message='')
        stored = len(data['entries'])
        gcmd.respond_info(
            "thermal mesh: %d pairs (chamber %s x bed %s), %d already stored; "
            "soak %ds, chamber soak %ds, settle < %.4f mm. "
            "THERMAL_MESH_STATUS to follow, THERMAL_MESH_ABORT to stop."
            % (len(plan), _fmt_list(chambers), _fmt_list(beds), stored,
               soak, chamber_soak, threshold))
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
            if s['phase'] == 'sampling':
                lines.append("sample %d, last mean |dZ| %s"
                             % (s['samples'], 'n/a' if s['last_delta'] is None
                                else '%.4f mm' % s['last_delta']))
        elif s['message']:
            lines.append(s['message'])
        data = self._load()
        if data and data.get('entries'):
            lines.append("map: %d entries in %s"
                         % (len(data['entries']), self.data_file))
            for p in data.get('analysis', {}).get('points', []):
                lines.append("  point (%.1f, %.1f) %s" % (p['x'], p['y'],
                                                          p['axis']))
        gcmd.respond_info("\n".join(lines))

    # -- analysis --------------------------------------------------------
    def _analyze(self, data, chamber_points, bed_points, min_spacing,
                 report=False):
        entries = data['entries']
        chamber_map, bed_map = sensitivity_maps(entries)
        if chamber_map is None and bed_map is None:
            return None
        params = data['mesh_params']
        points = select_points(params, chamber_map, bed_map, chamber_points,
                               bed_points, min_spacing)
        data['analysis'] = {'chamber_rms': chamber_map, 'bed_rms': bed_map,
                            'points': points, 'min_spacing': min_spacing}
        self._save(data)
        if report:
            lines = ["thermal mesh: sensitivity (um/degC, peak / median):"]
            for name, smap in (('chamber', chamber_map), ('bed', bed_map)):
                if smap is None:
                    lines.append("  %s: single target, not mapped" % (name,))
                    continue
                flat = sorted(v for row in smap for v in row)
                lines.append("  %s: %.2f / %.2f" % (
                    name, flat[-1] * 1000., flat[len(flat) // 2] * 1000.))
            lines.append("probe points:")
            for p in points:
                lines.append(
                    "  (%.1f, %.1f) for %s: chamber %.2f, bed %.2f um/degC"
                    % (p['x'], p['y'], p['axis'], p['chamber_rms'] * 1000.,
                       p['bed_rms'] * 1000.))
            self.gcode.respond_info("\n".join(lines))
        return points

    cmd_THERMAL_MESH_ANALYZE_help = (
        "Find the mesh points most sensitive to bed and chamber temperature")

    def cmd_THERMAL_MESH_ANALYZE(self, gcmd):
        if self.job is not None:
            raise gcmd.error("thermal mesh: a sweep is running")
        data = self._require_map(need_points=False)
        try:
            points = self._analyze(
                data, gcmd.get_int('CHAMBER_POINTS', 2, minval=0),
                gcmd.get_int('BED_POINTS', 2, minval=0),
                gcmd.get_float('MIN_SPACING', self.min_spacing, minval=0.),
                report=True)
        except ValueError as e:
            raise gcmd.error("thermal mesh: %s" % (e,))
        if points is None:
            raise gcmd.error("thermal mesh: the map has a single temperature "
                             "pair; there is nothing to compare")

    # -- apply -----------------------------------------------------------
    def _probe_finalize(self, offsets, positions):
        self._probed = (offsets, positions)

    def _probe_points(self, gcmd, points):
        """Z at each point on the same basis as bed_mesh's probed matrix."""
        coords = [(p['x'], p['y']) for p in points]
        zref_mode, zref_pos = 0, None
        mgr = getattr(getattr(self.bed_mesh, 'bmc', None), 'probe_mgr', None)
        if mgr is not None:
            zref_mode = mgr.get_zero_ref_mode()
            zref_pos = mgr.get_zero_ref_pos()
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
        method = gcmd.get('METHOD', 'bilinear').lower()
        if method not in METHODS:
            raise gcmd.error("thermal mesh: METHOD must be one of %s"
                             % (', '.join(METHODS),))
        correct = gcmd.get('CORRECT', 'none').lower()
        if correct not in CORRECTIONS:
            raise gcmd.error("thermal mesh: CORRECT must be one of %s"
                             % (', '.join(CORRECTIONS),))
        relative = gcmd.get('MATCH', 'absolute').lower() == 'relative'
        data = self._require_map(need_points=method != 'temperature')
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
                self.gcode.run_script_from_command(self.before_sample.render())
                measured = self._probe_points(gcmd, points)
                self.gcode.run_script_from_command(self.after_sample.render())
                if method == 'bilinear':
                    fit = fit_bilinear(entries, points, measured, relative)
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
        matrix, residuals = effective_mesh(weights, params, points, measured,
                                           correct)
        lines.append("blend: " + ", ".join(
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
            'method': method, 'correct': correct,
            'measured': measured, 'residuals': residuals,
            'weights': [[w, e['chamber'], e['bed']] for w, e in weights]}
        gcmd.respond_info("thermal mesh (%s):\n%s" % (method,
                                                      "\n".join(lines)))


def _fmt_list(values):
    return "[%s]" % (", ".join("%g" % v for v in values),)


def load_config(config):
    return FFThermalMesh(config)
