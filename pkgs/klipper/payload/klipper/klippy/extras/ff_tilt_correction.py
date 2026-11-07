# Tilt-based print coordinate correction for the Creator 5
#
# Copyright (C) 2026
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Print in a frame whose Z axis stands perpendicular to the bed.

WHY. The bed's tramming changes with temperature: a hot bed in a hot chamber
sits measurably tilted against the one the gantry was trammed to. Bed mesh
compensation follows the tilt with the nozzle, so every layer lies parallel
to the plate -- but the layers are still stacked along machine Z. Relative
to the plate the part comes out sheared: a cube leans by the plate's slope,
0.4 mm over 200 mm of height for a 0.2 % tilt. (With bed_mesh fade it is a
wedge instead.) This module tilts the print's coordinate frame to match the
plate, so the part is printed as a rigid body standing square on it.

WHAT IT DOES, per print:

1. SCAN. Read the G-code file: the first-layer extrusion footprint (cells of
   cell_size mm that an extruding move at the lowest extrusion height
   crosses) and, per tool and per slab_height of Z, the X/Y/Z extent of every
   move. Runs in a thread so the bed keeps heating and the reactor keeps
   running; START_PRINT kicks it off before homing.

2. FIT. Sample the active mesh at the centre of every footprint cell, in
   toolhead coordinates, and fit z = z0 + gx (x - x0) + gy (y - y0) by least
   squares, (x0, y0) being the footprint's centroid. Each cell counts once,
   so the fit minimises the RMS mesh error over the used AREA, not over
   wherever the toolpath happens to be dense. A direction in which the
   footprint spans less than min_span gets no slope: a 3 mm wide ruler says
   nothing about the tilt across it.

3. CHECK. Map every slab's extent box through the correction into toolhead
   coordinates and compare it with the envelope. If anything ends up further
   outside than it already was without the correction, the slope is scaled
   down (scale 1.00, 0.99, ... 0) until nothing does.

4. FREEZE AND APPLY. The map is computed once, here, and stored; nothing
   after this point recomputes it or reads the mesh to build it.

THE MAP. With n = (-gx, -gy, 1) / s, s = sqrt(1 + gx^2 + gy^2), the fitted
plane's unit normal, and R the smallest rotation taking the machine Z axis
onto n (Rodrigues, axis z x n), a print-frame point p goes to

    q = P + R (p - P0)          P0 = (x0, y0, 0), P = (x0, y0, z0)

which puts the print's z = 0 exactly onto the fitted plane, its origin at
height z0 (the frame's Z offset: the mean mesh height over the footprint),
its Z axis along n, and keeps every length (rigid).

WHERE IT SITS: LAST. Klipper's chain runs gcode_move -> [flow mesh,
exclude_object] -> ff_toolchange's tool frame -> bed_mesh -> toolhead, and
bed_mesh calls toolhead.move itself. The tilt is a stage in place of
bed_mesh's toolhead, so EVERYTHING above it -- the tool offsets, the bed
mesh, its fade, the flow mesh -- works in the tilted frame, and the stage
rotates the result, mesh correction included:

  * the bed mesh is looked up at frame (u, v) and added along the frame's
    Z, i.e. along the plate's normal;
  * fade is a function of frame height, so a layer fades by the same
    amount across the whole bed, not by a band that follows the plane;
  * the flow mesh tracks layers in the print's own Z and absorbs the mesh
    in the frame.

THE MESH IN THE FRAME. bed_mesh therefore holds FrameMesh(measured, map):
at frame (u, v), the measured plate's height above the fitted plane
MEASURED ALONG n. With Q0 the frame point on the plane and r = measured -
plane, the plate lies where Q0 + w n meets it, w = r(Q0 + w n) / s -- a
fixed point, two iterations closing it far below a micron. A first-layer
point h up then sits exactly h from the plate along the normal.

FROZEN. The map is computed once, at TILT_CORRECTION_START, and nothing
recomputes it. bed_mesh.set_mesh is wrapped for the duration, so a mesh
loaded mid-print is re-expressed in the same frozen frame and
BED_MESH_CLEAR leaves the frame tilted. The probed matrix stays the
measured one (bed_mesh's save_profile writes it), and ff_bed_guard reads
the measured plate (FrameMesh.plate) against final machine coordinates.

The stage maps the toolhead point the tool frame produced: X/Y are
toolhead coordinates, Z less the mounted tool's Z offset (gap + job term)
is the print height, which is what rotates; the offset is added back
after. In the raw machine frame (dock moves: no tool) it passes through.
R is orthonormal, so the map is affine with an exact inverse (R
transposed): lines stay lines, and get_position() is exact.

The module level functions hold the arithmetic and touch nothing in klippy;
qa/static/test_ff_tilt_correction.py tests them directly.
"""

import logging
import math
import os
import re
import threading

EPSILON = 1e-9
# Scale is searched downward in these steps; 1 % of the fitted slope.
SCALE_STEPS = 100
# Arc sampling: at most this angle, and at most ARC_STEP mm, per sample.
ARC_MAX_ANGLE = math.radians(10.)
ARC_STEP = 1.0

CMD = re.compile(r'([GMT])(\d+)(?:\.\d+)?(.*)$')
WORD = re.compile(r'([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))')


######################################################################
# The G-code scan
######################################################################

class GcodeScan:
    """Streaming scan of a G-code file: footprint and extents.

    Positions are in the file's own frame after G92 shifts -- what
    gcode_move hands to the transform chain, before the babystep. Keys use
    the tool active at the time, None before the file's first T<n>.
    """

    def __init__(self, cell_size=5., slab_height=5., footprint_tolerance=.05):
        self.cell = float(cell_size)
        self.slab = float(slab_height)
        self.tolerance = float(footprint_tolerance)
        self.pos = [None, None, None]
        self.offset = [0., 0., 0.]      # G92: physical = logical + offset
        self.e = 0.
        self.absolute = True
        self.absolute_e = True
        self.tool = None
        self.tools = []
        self.cells = {}                 # (tool, ix, iy) -> lowest extrusion z
        self.extents = {}               # (tool, slab) -> [x0,x1,y0,y1,z0,z1]
        self.zmin_extrude = None
        self.lines = 0
        self.moves = 0

    # -- feeding ------------------------------------------------------------
    def feed(self, raw):
        self.lines += 1
        line = raw.split(';', 1)[0].strip()
        if not line:
            return
        if line[0] in 'gmt':
            line = line.upper()
        match = CMD.match(line)
        if match is None:
            return
        letter, number, rest = match.group(1), int(match.group(2)), \
            match.group(3)
        if letter == 'T':
            if not rest.strip():
                self.tool = number
                if number not in self.tools:
                    self.tools.append(number)
            return
        if letter == 'M':
            if number == 82:
                self.absolute_e = True
            elif number == 83:
                self.absolute_e = False
            return
        if number in (0, 1, 2, 3):
            self._move(number, dict(WORD.findall(rest)))
        elif number == 90:
            self.absolute = True
            self.absolute_e = True
        elif number == 91:
            self.absolute = False
            self.absolute_e = False
        elif number == 92:
            self._set_position(dict(WORD.findall(rest)))
        elif number == 28:
            # Homing: the file's idea of where it is no longer holds.
            words = dict(WORD.findall(rest))
            axes = [i for i, a in enumerate('XYZ') if a in words] or [0, 1, 2]
            for i in axes:
                self.pos[i] = None
                self.offset[i] = 0.

    def _set_position(self, words):
        if not words:
            words = {'X': '0', 'Y': '0', 'Z': '0', 'E': '0'}
        for i, axis in enumerate('XYZ'):
            if axis in words and self.pos[i] is not None:
                self.offset[i] = self.pos[i] - float(words[axis])
        if 'E' in words:
            self.e = float(words['E'])

    def _move(self, number, words):
        start = list(self.pos)
        end = list(self.pos)
        for i, axis in enumerate('XYZ'):
            if axis in words:
                v = float(words[axis])
                if self.absolute:
                    end[i] = v + self.offset[i]
                elif end[i] is not None:
                    end[i] += v
        de = 0.
        if 'E' in words:
            v = float(words['E'])
            if self.absolute_e:
                de, self.e = v - self.e, v
            else:
                de = v
        self.pos = end
        if None in end:
            return
        self.moves += 1
        if number in (2, 3) and None not in start:
            points = arc_points(start, end, words, number == 2)
        else:
            points = [end] if None in start else [start, end]
        for p in points:
            self._extent(p)
        if de > 0. and None not in start and len(points) >= 2:
            self._footprint(points)

    def _extent(self, p):
        key = (self.tool, int(math.floor(p[2] / self.slab)))
        box = self.extents.get(key)
        if box is None:
            self.extents[key] = [p[0], p[0], p[1], p[1], p[2], p[2]]
            return
        if p[0] < box[0]:
            box[0] = p[0]
        elif p[0] > box[1]:
            box[1] = p[0]
        if p[1] < box[2]:
            box[2] = p[1]
        elif p[1] > box[3]:
            box[3] = p[1]
        if p[2] < box[4]:
            box[4] = p[2]
        elif p[2] > box[5]:
            box[5] = p[2]

    def _footprint(self, points):
        if len(points) == 2 and math.hypot(
                points[1][0] - points[0][0],
                points[1][1] - points[0][1]) < EPSILON:
            return          # an unretract in place lays no footprint
        z = min(p[2] for p in points)
        if self.zmin_extrude is None or z < self.zmin_extrude:
            self.zmin_extrude = z
        elif z > self.zmin_extrude + self.tolerance:
            return          # above the lowest layer seen: never kept
        step = self.cell / 2.
        cells = self.cells
        for a, b in zip(points, points[1:]):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            count = max(1, int(math.ceil(length / step)))
            for i in range(count + 1):
                f = float(i) / count
                key = (self.tool,
                       int(math.floor((a[0] + (b[0] - a[0]) * f) / self.cell)),
                       int(math.floor((a[1] + (b[1] - a[1]) * f) / self.cell)))
                old = cells.get(key)
                if old is None or z < old:
                    cells[key] = z

    # -- results ------------------------------------------------------------
    def footprint(self):
        """{(tool, ix, iy)} of the first layer: extrusions within
        footprint_tolerance of the lowest extrusion height."""
        if self.zmin_extrude is None:
            return set()
        limit = self.zmin_extrude + self.tolerance
        return set(k for k, z in self.cells.items() if z <= limit)

    def boxes(self):
        """[(tool, [x0, x1, y0, y1, z0, z1])] -- one per tool and slab."""
        return [(key[0], box) for key, box in self.extents.items()]


def arc_points(start, end, words, clockwise):
    """Points along a G2/G3 arc, start and end included (I/J or R form)."""
    x0, y0, z0 = start
    x1, y1, z1 = end
    if 'I' in words or 'J' in words:
        cx = x0 + float(words.get('I', 0.))
        cy = y0 + float(words.get('J', 0.))
    elif 'R' in words:
        r = float(words['R'])
        dx, dy = x1 - x0, y1 - y0
        chord = math.hypot(dx, dy)
        if chord < EPSILON or abs(r) < chord / 2. - 1e-6:
            return [start, end]
        h = math.sqrt(max(0., r * r - chord * chord / 4.))
        # Klipper/Marlin: positive R is the shorter arc.
        sign = -1. if (r > 0.) == clockwise else 1.
        mx, my = (x0 + x1) / 2., (y0 + y1) / 2.
        cx = mx + sign * h * (-dy) / chord
        cy = my + sign * h * dx / chord
    else:
        return [start, end]
    radius = math.hypot(x0 - cx, y0 - cy)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = a1 - a0
    if clockwise:
        while sweep >= 0.:
            sweep -= 2. * math.pi
        if abs(x1 - x0) < EPSILON and abs(y1 - y0) < EPSILON:
            sweep = -2. * math.pi
        sweep = max(sweep, -2. * math.pi)
    else:
        while sweep <= 0.:
            sweep += 2. * math.pi
        sweep = min(sweep, 2. * math.pi)
    step = ARC_MAX_ANGLE
    if radius > EPSILON:
        step = min(step, ARC_STEP / radius)
    count = max(1, int(math.ceil(abs(sweep) / step)))
    points = [list(start)]
    for i in range(1, count):
        f = float(i) / count
        a = a0 + sweep * f
        points.append([cx + radius * math.cos(a), cy + radius * math.sin(a),
                       z0 + (z1 - z0) * f])
    points.append(list(end))
    return points


def scan_file(path, scan, stop=None, chunk=1 << 16):
    """Feed every line of `path` to `scan`. `stop` is a threading.Event."""
    with open(path, 'rb') as fh:
        tail = b''
        while True:
            if stop is not None and stop.is_set():
                raise RuntimeError("scan cancelled")
            data = fh.read(chunk)
            if not data:
                break
            data = tail + data
            lines = data.split(b'\n')
            tail = lines.pop()
            for line in lines:
                scan.feed(line.decode('utf-8', 'replace'))
        if tail:
            scan.feed(tail.decode('utf-8', 'replace'))
    return scan


######################################################################
# The fit
######################################################################

def _eigen2(a, b, d):
    """Eigenpairs of the symmetric [[a, b], [b, d]], largest first."""
    mean = (a + d) / 2.
    root = math.sqrt(((a - d) / 2.) ** 2 + b * b)
    l1, l2 = mean + root, mean - root
    if abs(b) > EPSILON:
        v1 = (l1 - d, b)
    elif a >= d:
        v1 = (1., 0.)
    else:
        v1 = (0., 1.)
    norm = math.hypot(v1[0], v1[1])
    v1 = (v1[0] / norm, v1[1] / norm)
    v2 = (-v1[1], v1[0])
    return [(l1, v1), (l2, v2)]


def fit_plane(points, min_span=30.):
    """Least-squares plane through [(x, y, z)], equal weight per point.

    Returns a dict: x0, y0 (centroid), z0 (mean z = plane height there),
    gx, gy (slope, mm/mm), rms (of z about its mean), residual (rms about
    the plane), spans (footprint extent along each principal direction) and
    dropped (how many of the two directions were too narrow to fit).

    Centred normal equations C g = r, C the 2x2 covariance of x/y and r the
    covariance of x/y with z, solved on C's eigenbasis so that a direction
    spanning less than min_span contributes no slope (a pseudo-inverse);
    with both directions wide enough it is the ordinary least squares.
    """
    n = len(points)
    if n == 0:
        raise ValueError("no points to fit")
    x0 = sum(p[0] for p in points) / n
    y0 = sum(p[1] for p in points) / n
    z0 = sum(p[2] for p in points) / n
    sxx = sxy = syy = sxz = syz = 0.
    for x, y, z in points:
        u, v, w = x - x0, y - y0, z - z0
        sxx += u * u
        sxy += u * v
        syy += v * v
        sxz += u * w
        syz += v * w
    sxx, sxy, syy, sxz, syz = (s / n for s in (sxx, sxy, syy, sxz, syz))
    gx = gy = 0.
    spans = []
    dropped = 0
    for lam, (vx, vy) in _eigen2(sxx, sxy, syy):
        proj = [(p[0] - x0) * vx + (p[1] - y0) * vy for p in points]
        span = max(proj) - min(proj)
        spans.append(span)
        if span < min_span or lam <= EPSILON:
            dropped += 1
            continue
        k = (vx * sxz + vy * syz) / lam
        gx += k * vx
        gy += k * vy
    rms = math.sqrt(sum((p[2] - z0) ** 2 for p in points) / n)
    residual = math.sqrt(sum(
        (p[2] - z0 - gx * (p[0] - x0) - gy * (p[1] - y0)) ** 2
        for p in points) / n)
    return {'x0': x0, 'y0': y0, 'z0': z0, 'gx': gx, 'gy': gy, 'rms': rms,
            'residual': residual, 'spans': spans, 'dropped': dropped,
            'points': n}


######################################################################
# The map
######################################################################

class TiltMap:
    """The frozen rigid map of the module docstring.

    Toolhead X/Y, G-code Z in; the point to send down the chain out.
    plane() is the fitted plane the frame stands on.
    """

    def __init__(self, x0, y0, z0, gx, gy):
        self.x0, self.y0, self.z0 = x0, y0, z0
        self.gx, self.gy = gx, gy
        self.s = s = math.sqrt(1. + gx * gx + gy * gy)
        nx, ny, nz = -gx / s, -gy / s, 1. / s
        self.normal = (nx, ny, nz)
        k = 1. / (1. + nz)
        # Rodrigues: I + [v]x + [v]x^2 / (1 + cos), v = z x n.
        self.rotation = [
            [1. - nx * nx * k, -nx * ny * k, nx],
            [-nx * ny * k, 1. - ny * ny * k, ny],
            [-nx, -ny, nz]]

    def plane(self, x, y):
        return self.z0 + self.gx * (x - self.x0) + self.gy * (y - self.y0)

    def forward(self, x, y, z):
        """q = P + R (p - P0)."""
        d = (x - self.x0, y - self.y0, z)
        r = self.rotation
        return (self.x0 + r[0][0] * d[0] + r[0][1] * d[1] + r[0][2] * d[2],
                self.y0 + r[1][0] * d[0] + r[1][1] * d[1] + r[1][2] * d[2],
                self.z0 + r[2][0] * d[0] + r[2][1] * d[1] + r[2][2] * d[2])

    def inverse(self, x, y, z):
        """p = P0 + R^T (q - P): R is orthonormal."""
        d = (x - self.x0, y - self.y0, z - self.z0)
        r = self.rotation
        return (self.x0 + r[0][0] * d[0] + r[1][0] * d[1] + r[2][0] * d[2],
                self.y0 + r[0][1] * d[0] + r[1][1] * d[1] + r[2][1] * d[2],
                r[0][2] * d[0] + r[1][2] * d[1] + r[2][2] * d[2])

    def angle(self):
        """Tilt of the frame, degrees."""
        return math.degrees(math.acos(min(1., self.normal[2])))

    def describe(self):
        return {'x0': self.x0, 'y0': self.y0, 'z0': self.z0,
                'gx': self.gx, 'gy': self.gy,
                'normal': list(self.normal),
                'rotation': [list(row) for row in self.rotation]}


def scaled_map(fit, scale):
    return TiltMap(fit['x0'], fit['y0'], fit['z0'],
                   fit['gx'] * scale, fit['gy'] * scale)


def mesh_nodes(matrix, params, offsets):
    """[[(x, y, z)]]: each node of a mesh matrix at its toolhead X/Y.

    Nodes span the mesh bounds evenly: node (r, c) sits at mesh X
    min_x + c * dx, and calc_z looks a toolhead X up at X + mesh offset, so
    the node's toolhead X is that less the offset."""
    if not matrix or not matrix[0]:
        return []
    rows, cols = len(matrix), len(matrix[0])
    dx = (params['max_x'] - params['min_x']) / (cols - 1) if cols > 1 else 0.
    dy = (params['max_y'] - params['min_y']) / (rows - 1) if rows > 1 else 0.
    return [[(params['min_x'] + c * dx - offsets[0],
              params['min_y'] + r * dy - offsets[1], z)
             for c, z in enumerate(line)] for r, line in enumerate(matrix)]


class FrameMesh:
    """A bed_mesh ZMesh re-expressed in the tilted frame: what bed_mesh
    applies while the stage rotates its output.

    calc_z(u, v) is the measured plate's height above the fitted plane,
    along the plane's normal, under frame point (u, v). `plate` is the
    measured mesh itself, for whoever needs the real plate (ff_bed_guard).

    The probed matrix stays the measured one, because bed_mesh's
    save_profile writes it out: a BED_MESH_PROFILE SAVE mid-print must save
    the plate, never this. Profile name, params and BED_MESH_OUTPUT are the
    measured mesh's too; the interpolated matrix and its range/average
    (which fade reads) are this one's, on the same nodes."""

    ITERATIONS = 2

    def __init__(self, plate, tilt):
        self.plate = plate
        self.tilt = tilt
        params = plate.get_mesh_params()
        offsets = list(getattr(plate, 'mesh_offsets', (0., 0.)))
        self.frame_matrix = [[self.calc_z(u, v) for u, v, _z in line]
                             for line in mesh_nodes(plate.get_mesh_matrix(),
                                                    params, offsets)] or [[]]

    def __getattr__(self, name):
        return getattr(self.plate, name)

    def calc_z(self, u, v):
        t = self.tilt
        x0, y0, _z = t.forward(u, v, 0.)
        nx, ny, _nz = t.normal
        w = 0.
        for _ in range(self.ITERATIONS):
            x, y = x0 + w * nx, y0 + w * ny
            w = (self.plate.calc_z(x, y) - t.plane(x, y)) / t.s
        return w

    def get_mesh_matrix(self):
        return [[round(z, 6) for z in line] for line in self.frame_matrix]

    def get_z_range(self):
        values = [z for line in self.frame_matrix for z in line]
        return (min(values), max(values)) if values else (0., 0.)

    def get_z_average(self):
        values = [z for line in self.frame_matrix for z in line]
        return round(sum(values) / len(values), 2) if values else 0.


def plate_of(mesh):
    """The measured mesh behind a FrameMesh (or the mesh itself)."""
    return getattr(mesh, 'plate', mesh)


######################################################################
# The envelope
######################################################################

AXES = 'XYZ'


def envelope_excess(tilt, boxes, env_min, env_max, z_low=0., z_high=0.,
                    w_low=0., w_high=0.):
    """Worst excursion beyond the envelope, per axis and side.

    boxes: [(x0, x1, y0, y1, z0, z1)] in toolhead X/Y and G-code Z. Each
    box's eight corners go through the map -- affine, so a box's image is
    bounded by its corners' images. w_low/w_high is what the bed mesh adds
    IN THE FRAME (along the normal, faded or not): it widens each box's Z
    before the map, so its X/Y lean is counted too. z_low/z_high is what
    is added after the map in machine Z: the tools' Z offsets.

    Returns {(axis, side): (excess_mm, corner)}, side 0 below env_min and 1
    above env_max; excess is negative when inside with that much margin.
    """
    worst = {}
    for box in boxes:
        for x in box[0:2]:
            for y in box[2:4]:
                for z in (box[4] + w_low, box[5] + w_high):
                    out = tilt.forward(x, y, z)
                    values = (
                        (out[0], out[0]), (out[1], out[1]),
                        (out[2] + z_low, out[2] + z_high))
                    for a in range(3):
                        lo, hi = values[a]
                        for side, excess in (
                                (0, env_min[a] - lo), (1, hi - env_max[a])):
                            key = (AXES[a], side)
                            if key not in worst or excess > worst[key][0]:
                                worst[key] = (excess, (x, y, z))
    return worst


def fits(excess, baseline):
    """Nothing further outside than without the correction."""
    for key, (value, _corner) in excess.items():
        allowed = max(0., baseline.get(key, (0., None))[0])
        if value > allowed + 1e-6:
            return False
    return True


def choose_scale(fit, boxes, env_min, env_max, z_extra=None,
                 max_slope=None, max_scale=1., steps=SCALE_STEPS,
                 z_low=0., z_high=0.):
    """The largest scale <= max_scale (and slope <= max_slope) that fits.

    z_extra(scale) -> (low, high): what the bed mesh adds in the frame at
    that scale (the plate above the plane being tried). z_low/z_high: the
    tools' Z offsets, added in machine Z.
    Returns (scale, limit) -- limit is '' at the full scale, else what held
    it back: 'max_slope', or the (axis, side) that would have gone out.
    """
    if z_extra is None:
        def z_extra(scale):
            return 0., 0.

    def excess_at(scale):
        lo, hi = z_extra(scale)
        return envelope_excess(scaled_map(fit, scale), boxes, env_min,
                               env_max, z_low, z_high, lo, hi)
    slope = math.hypot(fit['gx'], fit['gy'])
    top, limit = float(max_scale), ''
    if max_slope is not None and slope * top > max_slope:
        top, limit = max_slope / slope, 'max_slope'
    baseline = excess_at(0.)
    if not boxes or slope < EPSILON:
        return top, limit
    blocked = None
    for i in range(steps, -1, -1):
        scale = top * i / steps
        excess = excess_at(scale)
        if fits(excess, baseline):
            return scale, (limit if blocked is None else blocked)
        if blocked is None:
            blocked = next(key for key, (value, _c) in excess.items()
                           if value > max(0., baseline[key][0]) + 1e-6)
    return 0., blocked


######################################################################
# Klipper glue
######################################################################

class TiltStage:
    """Stands in for the toolhead under bed_mesh: every move bed_mesh makes,
    mesh correction included, goes through the frozen map on its way to
    the real toolhead; get_position comes back through the inverse.
    Everything else is the toolhead's."""

    def __init__(self, owner, toolhead):
        self.owner = owner
        self.toolhead = toolhead

    def __getattr__(self, name):
        return getattr(self.toolhead, name)

    def move(self, newpos, speed):
        self.toolhead.move(self.owner.to_machine(newpos), speed)

    def get_position(self):
        return self.owner.to_frame(self.toolhead.get_position())


class _ScanJob:
    def __init__(self, key, path, scan):
        self.key = key
        self.path = path
        self.scan = scan
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run,
                                       name='ff_tilt_scan', daemon=True)

    def _run(self):
        try:
            # A lower priority for this thread only (Linux threads are
            # tasks): the reactor and the move queue come first.
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            except Exception:
                pass
            scan_file(self.path, self.scan, self.stop)
        except Exception as e:
            logging.exception("ff_tilt_correction: scan of %s failed",
                              self.path)
            self.error = str(e)

    def done(self):
        return not self.thread.is_alive()


class FFTiltCorrection:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.cell_size = config.getfloat('cell_size', 5., above=0.)
        self.slab_height = config.getfloat('slab_height', 5., above=0.)
        self.footprint_tolerance = config.getfloat(
            'footprint_tolerance', .05, minval=0.)
        self.min_span = config.getfloat('min_span', 30., minval=0.)
        self.max_slope = config.getfloat('max_slope', .01, above=0.)
        self.scan_timeout = config.getfloat('scan_timeout', 300., above=0.)
        self.env_min, self.env_max = self._envelope(config)
        self.stage = None
        self.tilt = None
        self.active = False
        self.job = None
        self.last = {}
        for name, func, desc in (
                ('TILT_CORRECTION_SCAN', self.cmd_TILT_CORRECTION_SCAN,
                 'Read a G-code file for tilt correction (in the background)'),
                ('TILT_CORRECTION_START', self.cmd_TILT_CORRECTION_START,
                 'Fit the mesh under the print and tilt its frame to match'),
                ('TILT_CORRECTION_STOP', self.cmd_TILT_CORRECTION_STOP,
                 'Return to the untilted print frame'),
                ('TILT_CORRECTION_STATUS', self.cmd_TILT_CORRECTION_STATUS,
                 'Show the tilt correction state')):
            self.gcode.register_command(name, func, desc=desc)

    @staticmethod
    def _envelope(config):
        """envelope_min/max, defaulting to the steppers' own ranges."""
        defaults_min, defaults_max = [], []
        for axis in 'xyz':
            try:
                section = config.getsection('stepper_%s' % axis)
                defaults_min.append(section.getfloat('position_min', 0.,
                                                     note_valid=False))
                defaults_max.append(section.getfloat('position_max',
                                                     note_valid=False))
            except Exception:
                defaults_min, defaults_max = None, None
                break
        env_min = config.getfloatlist('envelope_min', defaults_min, count=3)
        env_max = config.getfloatlist('envelope_max', defaults_max, count=3)
        if env_min is None or env_max is None:
            raise config.error("ff_tilt_correction: envelope_min and "
                               "envelope_max are needed (X, Y, Z)")
        if any(lo >= hi for lo, hi in zip(env_min, env_max)):
            raise config.error("ff_tilt_correction: envelope_min must be "
                               "below envelope_max on every axis")
        return tuple(env_min), tuple(env_max)

    # -- printer state --------------------------------------------------------
    def _toolchange(self):
        return self.printer.lookup_object('ff_toolchange', None)

    def _tool_offsets(self, tool):
        """(x, y, z) the chain below adds for `tool`, job Z included."""
        toolchange = self._toolchange()
        if toolchange is None or tool is None or tool < 0:
            return (0., 0., 0.)
        return (toolchange.offset_x[tool], toolchange.offset_y[tool],
                toolchange.offset_z[tool] + toolchange.job_z)

    def _initial_tool(self):
        """The tool the file's moves before its first T<n> run on."""
        toolchange = self._toolchange()
        if toolchange is None:
            return None
        tool = toolchange.gcode_transform.tool
        if tool is None:
            tool, _reason = toolchange._current_tool_or_none()
        return tool if tool is not None and tool >= 0 else None

    def _bed_mesh(self):
        return self.printer.lookup_object('bed_mesh', None)

    def _plate(self):
        """The MEASURED mesh -- under a FrameMesh if one is loaded."""
        bed_mesh = self._bed_mesh()
        mesh = None if bed_mesh is None else bed_mesh.get_mesh()
        return None if mesh is None else plate_of(mesh)

    # -- the stage ------------------------------------------------------------
    def _frame_z(self):
        """The mounted tool's Z offset (gap + job term), or None in the raw
        machine frame (no tool: dock moves, calibrations)."""
        toolchange = self._toolchange()
        if toolchange is None:
            return 0.
        offsets = toolchange.gcode_transform._offsets()
        return None if offsets is None else offsets[2]

    def to_machine(self, pos):
        oz = self._frame_z()
        if oz is None or self.tilt is None:
            return list(pos)
        x, y, z = self.tilt.forward(pos[0], pos[1], pos[2] - oz)
        return [x, y, z + oz] + list(pos[3:])

    def to_frame(self, pos):
        oz = self._frame_z()
        if oz is None or self.tilt is None:
            return list(pos)
        x, y, z = self.tilt.inverse(pos[0], pos[1], pos[2] - oz)
        return [x, y, z + oz] + list(pos[3:])

    # -- installing ---------------------------------------------------------------
    def _hook_set_mesh(self, bed_mesh):
        """Every mesh bed_mesh is given while the frame is tilted is
        re-expressed in the frozen frame on its way in; None (BED_MESH_CLEAR)
        stays None. The frame itself never changes."""
        if 'set_mesh' in vars(bed_mesh):
            return
        original = bed_mesh.set_mesh

        def set_mesh(mesh, *args, **kwargs):
            if mesh is not None and self.tilt is not None:
                mesh = FrameMesh(plate_of(mesh), self.tilt)
            return original(mesh, *args, **kwargs)
        bed_mesh.set_mesh = set_mesh

    def _unhook_set_mesh(self, bed_mesh):
        if 'set_mesh' in vars(bed_mesh):
            del bed_mesh.set_mesh
        mesh = bed_mesh.get_mesh()
        if mesh is not None and mesh is not plate_of(mesh):
            bed_mesh.set_mesh(plate_of(mesh))       # the measured mesh back

    def install(self, tilt):
        """Freeze `tilt` for the rest of the print."""
        bed_mesh = self._bed_mesh()
        plate = self._plate()
        previous = self.tilt
        self.tilt = tilt
        staged = False
        if not self.active:
            self.stage = TiltStage(self, bed_mesh.toolhead)
            bed_mesh.toolhead = self.stage
            staged = True
        self.active = True
        try:
            self._hook_set_mesh(bed_mesh)
            # Through the hook: the plate in THIS frame. set_mesh resets
            # gcode_move's position, through the stage.
            bed_mesh.set_mesh(plate)
        except Exception:
            # e.g. bed_mesh refusing the mesh against its fade range: leave
            # everything as it was.
            self.tilt = previous
            if staged:
                bed_mesh.toolhead = self.stage.toolhead
                self.stage = None
                self.active = False
                self._unhook_set_mesh(bed_mesh)
            elif previous is not None:
                bed_mesh.set_mesh(plate)
            raise
        self.printer.lookup_object('gcode_move').reset_last_position()

    def stop(self):
        if not self.active:
            return False
        bed_mesh = self._bed_mesh()
        if bed_mesh.toolhead is not self.stage:
            raise self.gcode.error(
                "tilt correction: bed_mesh's toolhead was replaced by "
                "something else; cannot remove the stage safely")
        bed_mesh.toolhead = self.stage.toolhead
        self.stage = None
        self.active = False
        self.tilt = None
        self._unhook_set_mesh(bed_mesh)
        self.printer.lookup_object('gcode_move').reset_last_position()
        return True

    # -- the scan ---------------------------------------------------------------
    def _resolve(self, gcmd):
        name = gcmd.get('FILENAME', None)
        sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if name:
            name = name.strip().lstrip('/')
            if sdcard is not None and not os.path.isabs(name):
                return os.path.join(sdcard.sdcard_dirname, name)
            return name
        ff_print = self.printer.lookup_object('ff_print', None)
        if ff_print is not None and ff_print.filename:
            return ff_print.filename
        if sdcard is not None:
            path = sdcard.file_path()
            if path:
                return path
        return None

    def start_scan(self, path):
        try:
            st = os.stat(path)
        except OSError as e:
            raise self.gcode.error("tilt correction: cannot read %s (%s)"
                                   % (path, e))
        key = (path, st.st_size, st.st_mtime)
        if self.job is not None and self.job.key == key and \
                self.job.error is None:
            return self.job
        if self.job is not None:
            self.job.stop.set()
        self.job = _ScanJob(key, path, GcodeScan(
            self.cell_size, self.slab_height, self.footprint_tolerance))
        self.job.thread.start()
        return self.job

    def wait_scan(self, job):
        deadline = self.reactor.monotonic() + self.scan_timeout
        while not job.done():
            now = self.reactor.monotonic()
            if now > deadline:
                job.stop.set()
                raise self.gcode.error(
                    "tilt correction: the scan took longer than %.0f s"
                    % (self.scan_timeout,))
            self.reactor.pause(now + .1)
        if job.error is not None:
            raise self.gcode.error("tilt correction: scan failed: %s"
                                   % (job.error,))
        return job.scan

    # -- the correction -----------------------------------------------------------
    def plan(self, scan, max_scale=1.):
        """Fit, check and scale. Returns a report dict with 'tilt'."""
        # Always the MEASURED mesh: a re-start mid-print fits the plate, not
        # the residual of the previous fit.
        mesh = self._plate()
        if mesh is None:
            raise self.gcode.error("tilt correction: no bed mesh is loaded")
        initial = self._initial_tool()
        gcode_move = self.printer.lookup_object('gcode_move')
        babystep = list(gcode_move.homing_position[:3])

        def resolve(tool):
            return initial if tool is None else tool

        # The footprint, in toolhead coordinates, one point per cell.
        cells = set()
        cell = self.cell_size
        for tool, ix, iy in scan.footprint():
            ox, oy, _oz = self._tool_offsets(resolve(tool))
            x = (ix + .5) * cell + babystep[0] + ox
            y = (iy + .5) * cell + babystep[1] + oy
            cells.add((int(math.floor(x / cell)), int(math.floor(y / cell))))
        if not cells:
            raise self.gcode.error("tilt correction: the file has no "
                                   "first-layer extrusion to fit to")
        points = []
        for ix, iy in sorted(cells):
            x, y = (ix + .5) * cell, (iy + .5) * cell
            points.append((x, y, mesh.calc_z(x, y)))
        fit = fit_plane(points, self.min_span)

        # Extents, per tool, in toolhead X/Y and G-code Z (the frame's own Z:
        # the tool's Z offset is added after the rotation, not rotated).
        boxes = []
        z_lo = z_hi = None
        for tool, box in scan.boxes():
            ox, oy, oz = self._tool_offsets(resolve(tool))
            boxes.append((box[0] + babystep[0] + ox, box[1] + babystep[0] + ox,
                          box[2] + babystep[1] + oy, box[3] + babystep[1] + oy,
                          box[4] + babystep[2], box[5] + babystep[2]))
            z_lo = oz if z_lo is None else min(z_lo, oz)
            z_hi = oz if z_hi is None else max(z_hi, oz)
        z_lo, z_hi = z_lo or 0., z_hi or 0.
        # What bed_mesh adds, in the frame, is the plate above the plane of
        # the scale being tried, along its normal: (mesh - plane) / s, faded
        # by any factor 0..1 towards fade_target, which lies within it (or
        # is 0), so 0 is included. mesh - plane = (mesh - z0) - scale *
        # (g . d): linear in the scale. (Taken at the nodes' own X/Y rather
        # than where the normal meets them: microns of difference.)
        params = mesh.get_mesh_params()
        offsets = list(getattr(mesh, 'mesh_offsets', (0., 0.)))
        nodes = [(z - fit['z0'],
                  fit['gx'] * (x - fit['x0']) + fit['gy'] * (y - fit['y0']))
                 for line in mesh_nodes(mesh.get_mesh_matrix(), params,
                                        offsets)
                 for x, y, z in line]

        def z_extra(scale):
            s = math.sqrt(1. + (scale * fit['gx']) ** 2
                          + (scale * fit['gy']) ** 2)
            values = [(a - scale * b) / s for a, b in nodes] or [0.]
            return min(0., min(values)), max(0., max(values))
        scale, limit = choose_scale(fit, boxes, self.env_min, self.env_max,
                                    z_extra, self.max_slope, max_scale,
                                    z_low=z_lo, z_high=z_hi)
        tilt = scaled_map(fit, scale)
        top = max([b[5] for b in boxes] or [0.])
        report = dict(fit)
        report.update({
            'scale': scale, 'limit': limit, 'tilt': tilt,
            'angle': tilt.angle(),
            'shift_at_top': top * math.hypot(tilt.normal[0], tilt.normal[1]),
            'top': top, 'cells': len(cells),
            'area': len(cells) * cell * cell,
        })
        return report

    def _describe(self, r):
        lines = [
            "tilt correction: plane over %d cells (%.0f cm^2), centred X%.1f "
            "Y%.1f: Z offset %+.3f mm there, slope X %+.3f Y %+.3f mm/100mm"
            % (r['cells'], r['area'] / 100., r['x0'], r['y0'], r['z0'],
               r['gx'] * 100., r['gy'] * 100.),
            "mesh over the footprint: %.4f mm rms about its mean, %.4f mm "
            "rms about the plane (what bed_mesh now applies, in the frame)"
            % (r['rms'],
                                                         r['residual'])]
        if r['dropped']:
            lines.append("footprint narrower than min_span %.0f mm in %d "
                         "direction(s) (spans %s mm): no slope fitted there"
                         % (self.min_span, r['dropped'],
                            ', '.join('%.0f' % s for s in r['spans'])))
        if r['limit'] == 'max_slope':
            lines.append("slope held to max_slope %.4f: scale %.2f"
                         % (self.max_slope, r['scale']))
        elif r['limit']:
            axis, side = r['limit']
            lines.append("reduced to %.0f%% of the fitted tilt: more would "
                         "take %s %s the envelope (%.2f..%.2f)"
                         % (r['scale'] * 100., axis,
                            'below' if side == 0 else 'beyond',
                            self.env_min[AXES.index(axis)],
                            self.env_max[AXES.index(axis)]))
        lines.append("frame tilted %.4f deg; at the top of the print (Z %.1f) "
                     "the part shifts %.3f mm in X/Y against an untilted one"
                     % (r['angle'], r['top'], r['shift_at_top']))
        return "\n".join(lines)

    # -- commands -------------------------------------------------------------------
    def cmd_TILT_CORRECTION_SCAN(self, gcmd):
        path = self._resolve(gcmd)
        if path is None:
            gcmd.respond_info("tilt correction: no file to scan")
            return
        job = self.start_scan(path)
        if gcmd.get_int('WAIT', 0, minval=0, maxval=1):
            scan = self.wait_scan(job)
            gcmd.respond_info(
                "tilt correction: %s: %d lines, %d moves, %d first-layer "
                "cells, tools %s"
                % (os.path.basename(path), scan.lines, scan.moves,
                   len(scan.footprint()), scan.tools or '(none)'))

    def cmd_TILT_CORRECTION_START(self, gcmd):
        optional = gcmd.get_int('OPTIONAL', 0, minval=0, maxval=1)
        try:
            self._start(gcmd)
        except self.printer.command_error as e:
            if not optional:
                raise
            self.last = {'applied': False, 'reason': str(e)}
            gcmd.respond_info("%s -- printing without tilt correction"
                              % (e,))

    def _start(self, gcmd):
        max_scale = gcmd.get_float('MAX_SCALE', 1., minval=0., maxval=1.)
        dry_run = gcmd.get_int('DRY_RUN', 0, minval=0, maxval=1)
        path = self._resolve(gcmd)
        if path is None:
            raise gcmd.error("tilt correction: no file (FILENAME=)")
        scan = self.wait_scan(self.start_scan(path))
        report = self.plan(scan, max_scale)
        msg = self._describe(report)
        self.last = {k: v for k, v in report.items() if k != 'tilt'}
        self.last.update({'applied': not dry_run, 'reason': '',
                          'file': os.path.basename(path)})
        logging.info("ff_tilt_correction: %s", msg.replace("\n", "; "))
        if dry_run:
            gcmd.respond_info(msg + "\n(DRY_RUN: not applied)")
            return
        self.install(report['tilt'])
        gcmd.respond_info(msg)

    def cmd_TILT_CORRECTION_STOP(self, gcmd):
        if self.stop():
            gcmd.respond_info("tilt correction: off")

    def cmd_TILT_CORRECTION_STATUS(self, gcmd):
        if not self.active:
            gcmd.respond_info("tilt correction: off%s" % (
                " (last: %s)" % self.last['reason']
                if self.last.get('reason') else ""))
            return
        t = self.tilt
        gcmd.respond_info(
            "tilt correction: on, %.4f deg, slope X %+.3f Y %+.3f mm/100mm "
            "about X%.1f Y%.1f, Z offset %+.3f, scale %.2f"
            % (t.angle(), t.gx * 100., t.gy * 100., t.x0, t.y0, t.z0,
               self.last.get('scale', 1.)))

    def get_status(self, eventtime):
        t = self.tilt
        return {
            'active': self.active,
            'slope_x': t.gx if t else 0.,
            'slope_y': t.gy if t else 0.,
            'angle': t.angle() if t else 0.,
            'pivot': [t.x0, t.y0] if t else None,
            'z_offset': t.z0 if t else 0.,
            'scale': self.last.get('scale'),
            'rms': self.last.get('rms'),
            'residual': self.last.get('residual'),
            'reason': self.last.get('reason', ''),
            # The frozen map: rotation, pivot, plane -- fixed until STOP.
            'transform': t.describe() if t else None,
            'envelope_min': list(self.env_min),
            'envelope_max': list(self.env_max),
        }


def load_config(config):
    return FFTiltCorrection(config)
