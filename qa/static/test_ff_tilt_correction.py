"""Tilt correction: a print frame standing square on the bed's fitted plane."""

import importlib.util
import math
from types import SimpleNamespace

import pytest

from lib.paths import ROOT


pytestmark = pytest.mark.static
MODULE = (ROOT / "pkgs" / "klipper" / "payload" / "klipper" /
          "klippy" / "extras" / "ff_tilt_correction.py")


@pytest.fixture(scope="module")
def tc():
    spec = importlib.util.spec_from_file_location("test_ff_tilt", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


# -- the map --------------------------------------------------------------

SLOPES = [(0., 0.), (0.002, 0.), (0., -0.003), (0.004, 0.0025),
          (-0.05, 0.03)]


@pytest.mark.parametrize("gx, gy", SLOPES)
def test_the_rotation_is_rigid_and_takes_z_onto_the_normal(tc, gx, gy):
    t = tc.TiltMap(120., 90., 0.1, gx, gy)
    r = t.rotation
    for i in range(3):
        for j in range(3):
            col_i = [r[k][i] for k in range(3)]
            col_j = [r[k][j] for k in range(3)]
            assert dot(col_i, col_j) == pytest.approx(float(i == j), abs=1e-12)
    assert [r[k][2] for k in range(3)] == pytest.approx(list(t.normal))
    # The normal is perpendicular to the plane's own directions.
    assert dot(t.normal, (1., 0., gx)) == pytest.approx(0., abs=1e-12)
    assert dot(t.normal, (0., 1., gy)) == pytest.approx(0., abs=1e-12)


@pytest.mark.parametrize("gx, gy", SLOPES)
def test_the_print_bed_plane_lands_on_the_fitted_plane(tc, gx, gy):
    t = tc.TiltMap(120., 90., 0.1, gx, gy)
    for x, y in ((0., 0.), (250., 10.), (37., 211.)):
        q = t.forward(x, y, 0.)
        assert q[2] == pytest.approx(t.plane(q[0], q[1]), abs=1e-12)
    # The origin under the pivot sits at z0: the frame's Z offset.
    assert t.forward(120., 90., 0.) == pytest.approx((120., 90., 0.1))


@pytest.mark.parametrize("gx, gy", SLOPES)
def test_heights_are_measured_along_the_normal_and_invert_exactly(
        tc, gx, gy):
    t = tc.TiltMap(120., 90., 0.1, gx, gy)
    for p in ((10., 20., 0.2), (200., 150., 75.), (128., 128., 250.)):
        q = t.forward(*p)
        base = t.forward(p[0], p[1], 0.)
        assert dist(q, base) == pytest.approx(p[2])
        # Vertically above the plane: s times the height, nothing else.
        assert q[2] - t.plane(q[0], q[1]) == pytest.approx(t.s * p[2],
                                                           abs=1e-9)
        assert t.inverse(*q) == pytest.approx(p, abs=1e-9)


def test_the_part_is_printed_as_a_rigid_body(tc):
    t = tc.TiltMap(100., 100., -0.05, 0.004, -0.002)
    a, b = (20., 30., 0.2), (180., 160., 120.)
    assert dist(t.forward(*a), t.forward(*b)) == pytest.approx(dist(a, b))


def test_a_level_bed_is_a_pure_z_offset(tc):
    t = tc.TiltMap(100., 100., 0.3, 0., 0.)
    assert t.forward(12., 34., 56.) == pytest.approx((12., 34., 56.3))
    assert t.angle() == 0.


def test_the_top_of_a_part_leans_back_from_a_rising_bed(tc):
    # Bed rising toward +X: the normal, and the part, lean toward -X.
    t = tc.TiltMap(100., 100., 0., 0.002, 0.)
    out = t.forward(100., 100., 200.)
    assert out[0] == pytest.approx(100. - 0.4, abs=1e-3)
    assert out[1] == pytest.approx(100.)


# -- the fit ----------------------------------------------------------------

def grid(x0, x1, y0, y1, z, step=5.):
    pts = []
    x = x0
    while x <= x1:
        y = y0
        while y <= y1:
            pts.append((x, y, z(x, y)))
            y += step
        x += step
    return pts


def test_an_exact_plane_is_recovered(tc):
    fit = tc.fit_plane(grid(20, 120, 40, 200,
                            lambda x, y: 0.1 + 0.003 * x - 0.001 * y))
    assert fit['gx'] == pytest.approx(0.003)
    assert fit['gy'] == pytest.approx(-0.001)
    # z0 is the plane's height at the centroid: the frame's Z offset.
    assert fit['z0'] == pytest.approx(
        0.1 + 0.003 * fit['x0'] - 0.001 * fit['y0'])
    assert fit['residual'] == pytest.approx(0., abs=1e-12)
    assert fit['rms'] > 0.05


def test_the_fit_minimises_rms_over_the_area(tc):
    bowl = lambda x, y: 0.002 * x + 1e-5 * ((x - 70) ** 2 + (y - 120) ** 2)
    pts = grid(20, 120, 40, 200, bowl)
    fit = tc.fit_plane(pts)
    def rms(gx, gy, z0):
        return math.sqrt(sum((p[2] - z0 - gx * (p[0] - fit['x0'])
                              - gy * (p[1] - fit['y0'])) ** 2
                             for p in pts) / len(pts))
    best = rms(fit['gx'], fit['gy'], fit['z0'])
    assert best == pytest.approx(fit['residual'])
    for d in ((1e-4, 0, 0), (0, 1e-4, 0), (0, 0, 1e-3), (-1e-4, 0, 0)):
        assert rms(fit['gx'] + d[0], fit['gy'] + d[1],
                   fit['z0'] + d[2]) > best


def test_a_narrow_footprint_fits_no_slope_across_itself(tc):
    # A 3 mm wide strip along X: the slope along it is real, across is not.
    pts = [(x, y, 0.002 * x + 0.01 * y) for x in range(0, 200, 5)
           for y in (100., 101.5, 103.)]
    fit = tc.fit_plane(pts, min_span=30.)
    assert fit['dropped'] == 1
    assert fit['gx'] == pytest.approx(0.002, rel=1e-6)
    assert fit['gy'] == pytest.approx(0., abs=1e-9)


# -- the scan ---------------------------------------------------------------

def scan(tc, text, **kw):
    s = tc.GcodeScan(**kw)
    for line in text.strip().splitlines():
        s.feed(line)
    return s


FILE = """
; a header
M83
T0
G1 Z5 F600
G1 X10 Y10 Z0.6
G1 X60 Y10 E3 ; purge line, higher than the first layer
G1 X100 Y100 Z0.2
G1 X120 Y100 E1
G1 X120 Y120 E1
G1 X100 Y120 E1
G1 X100 Y100 E1
G1 Z0.4
G1 X120 Y120 E1
G1 Z30
T1
G1 X200 Y50 Z30
"""


def test_the_footprint_is_the_lowest_layer_only(tc):
    s = scan(tc, FILE)
    cells = s.footprint()
    assert cells
    assert all(tool == 0 for tool, _x, _y in cells)
    xs = [ix for _t, ix, _y in cells]
    ys = [iy for _t, _x, iy in cells]
    # The 20 mm square at Z0.2, not the purge line at Z0.6 (x 10..60).
    assert min(xs) * 5 >= 95 and max(xs) * 5 <= 120
    assert min(ys) * 5 >= 95 and max(ys) * 5 <= 120
    assert s.zmin_extrude == pytest.approx(0.2)


def test_extents_are_kept_per_tool_and_slab(tc):
    s = scan(tc, FILE)
    boxes = dict(((t, k), b) for (t, k), b in s.extents.items())
    assert (1, 6) in boxes                      # T1 at Z30, slab 30..35
    assert boxes[(1, 6)][1] == pytest.approx(200.)
    assert s.tools == [0, 1]
    top = max(b[5] for b in boxes.values())
    assert top == pytest.approx(30.)


def test_relative_moves_and_g92_are_followed(tc):
    s = scan(tc, """
G90
G1 X10 Y10 Z1
G92 X0 Y0
G1 X5 Y5
G91
G1 X5 Z2
G90
""")
    box = [b for b in s.extents.values()]
    xs = [v for b in box for v in b[0:2]]
    assert max(xs) == pytest.approx(20.)       # 10 + 5 (G92) + 5 (G91)
    assert max(v for b in box for v in b[4:6]) == pytest.approx(3.)


def test_an_unretract_in_place_is_not_the_first_layer(tc):
    s = scan(tc, """
M83
G1 X50 Y50 Z0.05
G1 E2
G1 Z0.2
G1 X80 Y50 E2
""")
    assert s.zmin_extrude == pytest.approx(0.2)
    assert s.footprint()


def test_arcs_are_bounded_by_where_they_go(tc):
    # A half circle from (100,100) around (110,100), counter-clockwise:
    # it dips to Y 90.
    s = scan(tc, """
M83
G1 X100 Y100 Z0.2
G3 X120 Y100 I10 J0 E2
""")
    box = list(s.extents.values())[0]
    assert box[2] == pytest.approx(90., abs=0.01)
    assert box[3] == pytest.approx(100.)


def test_a_file_scans_from_disk(tc, tmp_path):
    path = tmp_path / "part.gcode"
    path.write_text(FILE)
    s = tc.scan_file(str(path), tc.GcodeScan(), chunk=7)
    assert s.footprint() == scan(tc, FILE).footprint()


# -- the envelope -------------------------------------------------------------

def fit_of(gx, gy, x0=128., y0=128., z0=0.):
    return {'x0': x0, 'y0': y0, 'z0': z0, 'gx': gx, 'gy': gy}


ENV_MIN, ENV_MAX = (0., 0., -10.), (256., 256., 270.)


def test_the_full_tilt_is_kept_when_it_fits(tc):
    boxes = [(50., 200., 50., 200., 0., 200.)]
    scale, limit = tc.choose_scale(fit_of(0.002, 0.), boxes, ENV_MIN, ENV_MAX)
    assert (scale, limit) == (1., '')


def test_the_tilt_is_reduced_until_nothing_leaves_the_envelope(tc):
    # Bed falling toward +X leans the part toward +X: 0.5 % over 200 mm is
    # 1 mm at the top, but only 0.4 mm is left before X 256.
    boxes = [(50., 255.6, 50., 200., 0., 200.)]
    scale, limit = tc.choose_scale(fit_of(-0.005, 0.), boxes, ENV_MIN,
                                   ENV_MAX)
    assert limit == ('X', 1)
    assert 0.3 < scale < 0.45
    excess = tc.envelope_excess(tc.scaled_map(fit_of(-0.005, 0.), scale),
                                boxes, ENV_MIN, ENV_MAX)
    assert excess[('X', 1)][0] <= 1e-6


def test_a_file_already_outside_may_not_get_worse(tc):
    boxes = [(50., 258., 50., 200., 0., 200.)]   # X 258 > 256 as sliced
    lean_out, _ = tc.choose_scale(fit_of(-0.005, 0.), boxes, ENV_MIN,
                                  ENV_MAX)
    lean_in, _ = tc.choose_scale(fit_of(0.005, 0.), boxes, ENV_MIN, ENV_MAX)
    assert lean_out == 0.
    assert lean_in == 1.


def test_max_slope_caps_the_tilt(tc):
    boxes = [(50., 200., 50., 200., 0., 10.)]
    scale, limit = tc.choose_scale(fit_of(0.03, 0.04), boxes, ENV_MIN,
                                   ENV_MAX, max_slope=0.01)
    assert limit == 'max_slope'
    assert scale == pytest.approx(0.2)


def test_z_counts_the_frame_and_what_the_chain_adds(tc):
    boxes = [(50., 200., 50., 200., 0., 265.)]
    excess = tc.envelope_excess(tc.scaled_map(fit_of(0., 0., z0=0.2), 0.),
                                boxes, ENV_MIN, ENV_MAX, 3., 3.4)
    # z0 rides in the frame; the tool's Z offset is added after the map.
    assert excess[('Z', 1)][0] == pytest.approx(265. + 0.2 + 3.4 - 270.)
    # The mesh's own height goes in BEFORE the map: it leans with the
    # frame, so it moves X too.
    lean = tc.scaled_map(fit_of(-0.01, 0.), 1.)
    flat = tc.envelope_excess(lean, boxes, ENV_MIN, ENV_MAX)
    raised = tc.envelope_excess(lean, boxes, ENV_MIN, ENV_MAX, w_high=5.)
    assert raised[('X', 1)][0] > flat[('X', 1)][0] + 0.04
    seen = []
    tc.choose_scale(fit_of(0.001, 0.), boxes, ENV_MIN, ENV_MAX,
                    z_extra=lambda s: seen.append(s) or (0., 0.))
    assert 0. in seen and 1. in seen


# -- the mesh in the frame -----------------------------------------------------

class Mesh:
    """A ZMesh: a 6x6 probe interpolated to 26x26 nodes, bilinear calc_z."""

    def __init__(self, z, lo=0., hi=250.):
        self.z = z
        self.params = {'min_x': lo, 'max_x': hi, 'min_y': lo, 'max_y': hi,
                       'x_count': 6, 'y_count': 6}
        self.mesh_offsets = [0., 0.]
        self.step = (hi - lo) / 25.
        self.lo = lo
        self.mesh_matrix = [[z(lo + c * self.step, lo + r * self.step)
                             for c in range(26)] for r in range(26)]
        probe = (hi - lo) / 5.
        self.probed_matrix = [[z(lo + c * probe, lo + r * probe)
                               for c in range(6)] for r in range(6)]

    def calc_z(self, x, y):
        fx = min(max((x - self.lo) / self.step, 0.), 25.)
        fy = min(max((y - self.lo) / self.step, 0.), 25.)
        c, r = min(int(fx), 24), min(int(fy), 24)
        tx, ty = fx - c, fy - r
        m = self.mesh_matrix
        z0 = m[r][c] + (m[r][c + 1] - m[r][c]) * tx
        z1 = m[r + 1][c] + (m[r + 1][c + 1] - m[r + 1][c]) * tx
        return z0 + (z1 - z0) * ty

    def get_mesh_params(self):
        return self.params

    def get_mesh_matrix(self):
        return [list(row) for row in self.mesh_matrix]

    def get_probed_matrix(self):
        return [list(row) for row in self.probed_matrix]

    def get_z_range(self):
        values = [v for row in self.mesh_matrix for v in row]
        return min(values), max(values)

    def get_z_average(self):
        values = [v for row in self.mesh_matrix for v in row]
        return round(sum(values) / len(values), 2)

    def get_profile_name(self):
        return "MESH_DATA"


def bumpy(x, y):
    return 0.1 + 0.002 * x - 0.001 * y + 0.03 * math.sin(x / 40.) \
        * math.cos(y / 55.)




def test_the_frame_mesh_is_the_plate_along_the_normal(tc):
    plate = Mesh(bumpy)
    t = tc.TiltMap(120., 130., 0.2, 0.002, -0.001)
    fm = tc.FrameMesh(plate, t)
    nx, ny, nz = t.normal
    for u, v in ((5., 5.), (17.3, 222.2), (130., 140.), (245., 245.)):
        w = fm.calc_z(u, v)
        q0 = t.forward(u, v, 0.)
        # Up the frame's Z (the normal) by w from the plane: on the plate.
        x, y, z = q0[0] + w * nx, q0[1] + w * ny, q0[2] + w * nz
        assert z == pytest.approx(plate.calc_z(x, y), abs=1e-8)
    # The frame matrix is calc_z at bed_mesh's own nodes.
    assert fm.get_mesh_matrix()[3][7] == pytest.approx(
        fm.calc_z(7 * plate.step, 3 * plate.step), abs=1e-6)
    lo, hi = fm.get_z_range()
    assert hi - lo < plate.get_z_range()[1] - plate.get_z_range()[0]
    # What bed_mesh saves as a profile stays the measured probe.
    assert fm.get_probed_matrix() == plate.get_probed_matrix()
    assert fm.get_profile_name() == "MESH_DATA"
    assert tc.plate_of(fm) is plate and tc.plate_of(plate) is plate


def test_a_plate_that_is_its_plane_is_flat_in_the_frame(tc):
    t = tc.TiltMap(100., 100., 0.15, 0.003, 0.002)
    fm = tc.FrameMesh(Mesh(lambda x, y: t.plane(x, y)), t)
    for u, v in ((10., 10.), (100., 200.), (240., 30.)):
        assert fm.calc_z(u, v) == pytest.approx(0., abs=1e-12)


# -- the stage, against a fake klippy ---------------------------------------------

class Toolhead:
    def __init__(self):
        self.pos = [0., 0., 10., 0.]
        self.moves = []
        self.max_accel = 20000.

    def move(self, newpos, speed):
        self.moves.append(list(newpos))
        self.pos = list(newpos)

    def get_position(self):
        return list(self.pos)


class BedMesh:
    """Klipper's bed_mesh as far as this touches it: its own `toolhead`,
    fade by the Z it is given, set_mesh resetting the G-code position."""
    FADE_DISABLE = 0x7FFFFFFF

    def __init__(self, gcode_move, toolhead, mesh, fade=None):
        self.gcode_move = gcode_move
        self.toolhead = toolhead
        self.fade_start, self.fade_end = fade or (1., self.FADE_DISABLE)
        self.fade_target = 0.
        self.z_mesh = None
        self.set_mesh(mesh)

    def set_mesh(self, mesh):
        faded = self.fade_end != self.FADE_DISABLE
        self.fade_target = mesh.get_z_average() if mesh and faded else 0.
        self.z_mesh = mesh
        self.gcode_move.reset_last_position()

    def get_mesh(self):
        return self.z_mesh

    def factor(self, z):
        if z >= self.fade_end:
            return 0.
        if z >= self.fade_start:
            return (self.fade_end - z) / (self.fade_end - self.fade_start)
        return 1.

    def added(self, x, y, z):
        if self.z_mesh is None:
            return self.fade_target
        ft = self.fade_target
        return self.factor(z) * (self.z_mesh.calc_z(x, y) - ft) + ft

    def move(self, newpos, speed):
        x, y, z = newpos[:3]
        self.toolhead.move([x, y, z + self.added(x, y, z)] + newpos[3:],
                           speed)

    def get_position(self):
        x, y, z = self.toolhead.get_position()[:3]
        # Exact while unfaded, which is all the tests read back.
        return [x, y, z - self.added(x, y, 0.),
                self.toolhead.get_position()[3]]


class ToolFrame:
    """ff_toolchange's per-tool frame: adds the tool offsets, Z with the
    job term, and hands down to bed_mesh."""

    def __init__(self, printer, offsets, job_z):
        self.printer = printer
        self.offsets = offsets
        self.job_z = job_z
        self.tool = 0
        self.next_transform = None

    def _offsets(self):
        if self.tool is None:
            return None
        ox, oy, oz = self.offsets[self.tool]
        return ox, oy, oz + self.job_z

    def move(self, newpos, speed):
        o = self._offsets() or (0., 0., 0.)
        self.next_transform.move([newpos[0] + o[0], newpos[1] + o[1],
                                  newpos[2] + o[2]] + list(newpos[3:]),
                                 speed)

    def get_position(self):
        o = self._offsets() or (0., 0., 0.)
        p = self.next_transform.get_position()
        return [p[0] - o[0], p[1] - o[1], p[2] - o[2]] + p[3:]


class GcodeMove:
    def __init__(self):
        self.move_transform = None
        self.homing_position = [0., 0., 0., 0.]
        self.base_position = [0., 0., 0., 0.]
        self.resets = 0

    def set_move_transform(self, transform, force=False):
        old, self.move_transform = self.move_transform, transform
        return old

    def reset_last_position(self):
        self.resets += 1


class CommandError(Exception):
    pass


class Gcode:
    error = CommandError

    def __init__(self):
        self.commands, self.info = {}, []

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def respond_info(self, msg):
        self.info.append(msg)


OFFSETS = ((0., 0., 3.2), (7., -3., 3.1), (0., 0., 3.2), (0., 0., 3.2))
JOB_Z = 0.05


class Printer:
    command_error = CommandError

    def __init__(self, mesh_z, tool=0, fade=None, filename=None):
        self.gcode = Gcode()
        self.toolhead = Toolhead()
        self.gcode_move = GcodeMove()
        self.frame = ToolFrame(self, OFFSETS, JOB_Z)
        self.frame.tool = tool
        self.gcode_move.move_transform = self.frame
        self.bed_mesh = BedMesh(self.gcode_move, self.toolhead,
                                Mesh(mesh_z) if mesh_z else None, fade)
        self.frame.next_transform = self.bed_mesh
        toolchange = SimpleNamespace(
            offset_x=[o[0] for o in OFFSETS],
            offset_y=[o[1] for o in OFFSETS],
            offset_z=[o[2] for o in OFFSETS], job_z=JOB_Z,
            gcode_transform=self.frame,
            _current_tool_or_none=lambda: (self.frame.tool, ''))
        self.objects = {
            "gcode": self.gcode,
            "gcode_move": self.gcode_move,
            "bed_mesh": self.bed_mesh,
            "ff_toolchange": toolchange,
            "ff_print": SimpleNamespace(filename=filename),
            "toolhead": SimpleNamespace(get_extruder=lambda: SimpleNamespace(
                nozzle_diameter=0.4)),
        }

    def get_reactor(self):
        return SimpleNamespace(monotonic=lambda: 0., pause=lambda t: None)

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def gmove(self, x, y, z, e=0.):
        """A G-code move, down the whole chain."""
        self.gcode_move.move_transform.move([x, y, z, e], 100.)
        return self.toolhead.moves[-1]


class Section:
    def __init__(self, values):
        self.values = values

    def getfloat(self, option, default=None, **kw):
        return self.values.get(option, default)


class Config:
    error = ValueError

    def __init__(self, printer, **values):
        self.printer, self.values = printer, values

    def get_printer(self):
        return self.printer

    def getfloat(self, option, default=None, **limits):
        return self.values.get(option, default)

    def getfloatlist(self, option, default=None, count=None):
        return self.values.get(option, default)

    def getsection(self, name):
        return Section({
            'stepper_x': {'position_min': -5., 'position_max': 310.},
            'stepper_y': {'position_min': -5., 'position_max': 260.},
            'stepper_z': {'position_min': -10., 'position_max': 270.},
        }[name])


class Cmd:
    def __init__(self, **params):
        self.params = {k: str(v) for k, v in params.items()}
        self.info = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, **kw):
        return int(self.params.get(name, default))

    def get_float(self, name, default=None, **kw):
        return float(self.params.get(name, default))

    def respond_info(self, msg):
        self.info.append(msg)

    def error(self, msg):
        return CommandError(msg)


PART = "\n".join(
    ["M83", "T0", "G1 X40 Y60 Z0.2", "G1 X200 Y60 E1", "G1 X200 Y62 E1",
     "G1 X40 Y62 E1"]
    + ["G1 X40 Y%d E1\nG1 X200 Y%d E1" % (y, y) for y in range(64, 200, 4)]
    + ["G1 Z150", "G1 X120 Y130 Z150"])


def make(tc, tmp_path, mesh_z, config=None, **kw):
    path = tmp_path / "part.gcode"
    path.write_text(PART)
    printer = Printer(mesh_z, filename=str(path), **kw)
    obj = tc.FFTiltCorrection(Config(printer, **(config or {})))
    printer.objects["ff_tilt_correction"] = obj
    return obj, printer


def started(tc, tmp_path, mesh_z, **kw):
    obj, printer = make(tc, tmp_path, mesh_z, **kw)
    cmd = Cmd()
    obj.cmd_TILT_CORRECTION_START(cmd)
    return obj, printer, cmd


def tilted(x, y):
    return 0.1 + 0.002 * x - 0.001 * y


OZ = OFFSETS[0][2] + JOB_Z


def nozzle(machine, oz=OZ):
    """Toolhead point -> nozzle, height above the homed plane."""
    return machine[0], machine[1], machine[2] - oz


def test_the_envelope_defaults_to_the_steppers(tc):
    obj = tc.FFTiltCorrection(Config(Printer(None)))
    assert obj.env_min == (-5., -5., -10.)
    assert obj.env_max == (310., 260., 270.)


def test_start_fits_the_measured_mesh_under_the_footprint(tc, tmp_path):
    obj, printer, cmd = started(tc, tmp_path, tilted)
    assert obj.active
    t = obj.tilt
    assert t.gx == pytest.approx(0.002, abs=1e-9)
    assert t.gy == pytest.approx(-0.001, abs=1e-9)
    # The footprint is 40..200 x 60..200: the pivot is its centre.
    assert t.x0 == pytest.approx(120., abs=3.)
    assert t.y0 == pytest.approx(130., abs=3.)
    assert t.z0 == pytest.approx(tilted(t.x0, t.y0), abs=1e-9)
    assert "Z offset" in cmd.info[0]
    status = obj.get_status(0.)
    assert status['transform']['z0'] == t.z0
    assert len(status['transform']['rotation']) == 3


def test_it_sits_under_bed_mesh_and_leaves_the_gcode_chain_alone(
        tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, bumpy)
    assert printer.gcode_move.move_transform is printer.frame
    assert printer.bed_mesh.toolhead is obj.stage
    assert obj.stage.toolhead is printer.toolhead
    assert obj.stage.max_accel == 20000.         # the rest is the toolhead's
    assert isinstance(printer.bed_mesh.get_mesh(), tc.FrameMesh)


def test_the_bed_mesh_is_applied_in_the_tilted_frame(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, bumpy)
    t = obj.tilt
    plate = printer.bed_mesh.get_mesh().plate
    for u, v in ((50., 70.), (190., 180.), (120., 130.), (20., 240.)):
        # Z0 in the file: the nozzle lands ON the measured plate...
        on = nozzle(printer.gmove(u, v, 0.))
        assert on[2] == pytest.approx(plate.calc_z(on[0], on[1]), abs=1e-7)
        # ...and a layer up is 0.2 from it ALONG THE NORMAL, not along Z:
        # the mesh correction is rotated with the frame.
        up = nozzle(printer.gmove(u, v, 0.2))
        step = [b - a for a, b in zip(on, up)]
        assert step == pytest.approx([0.2 * n for n in t.normal], abs=1e-9)
        # Back through the stage: bed_mesh's own frame, mesh on frame Z.
        frame = obj.to_frame(printer.toolhead.moves[-1])
        w = printer.bed_mesh.get_mesh().calc_z(u, v)
        assert frame[:3] == pytest.approx([u, v, 0.2 + OZ + w], abs=1e-9)


def test_fade_runs_on_the_frames_height(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, bumpy, fade=(1., 10.))
    mesh = printer.bed_mesh.get_mesh()
    ft = printer.bed_mesh.fade_target
    assert ft == mesh.get_z_average()
    # Mid-fade, one print height: the SAME fraction of the frame mesh at
    # both ends of the bed, though the plane is at different heights there.
    h = 5.
    f = printer.bed_mesh.factor(h + OZ)
    assert 0. < f < 1.
    for u, v in ((30., 40.), (230., 220.)):
        printer.gmove(u, v, h)
        added = obj.to_frame(printer.toolhead.moves[-1])[2] - (h + OZ)
        assert added == pytest.approx(f * (mesh.calc_z(u, v) - ft) + ft,
                                      abs=1e-9)
    # Above fade_end: the mesh is gone, the frame is still tilted.
    m = printer.gmove(150., 100., 80.)
    q = obj.tilt.forward(150., 100., 80. + ft)
    assert nozzle(m) == pytest.approx(q, abs=1e-9)
    assert m[0] == pytest.approx(150. - obj.tilt.gx * 80., abs=1e-3)


def test_the_frame_is_frozen_whatever_happens_to_the_mesh(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, bumpy)
    frozen = obj.tilt.describe()
    # A different mesh loaded mid-print: put in the SAME frozen frame.
    other = Mesh(lambda x, y: 0.5 - 0.003 * x)
    printer.bed_mesh.set_mesh(other)
    loaded = printer.bed_mesh.get_mesh()
    assert isinstance(loaded, tc.FrameMesh) and loaded.tilt is obj.tilt
    assert loaded.plate is other
    # BED_MESH_CLEAR: no mesh, the frame stays.
    printer.bed_mesh.set_mesh(None)
    assert printer.bed_mesh.get_mesh() is None
    m = printer.gmove(150., 100., 80.)
    assert nozzle(m) == pytest.approx(obj.tilt.forward(150., 100., 80.))
    assert obj.tilt.describe() == frozen


def test_the_flow_mesh_absorbs_the_plate_in_the_frame(tc, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "test_ff_flow_for_tilt", MODULE.parent / "ff_flow_mesh.py")
    fm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fm)
    obj, printer, _ = started(tc, tmp_path, bumpy)
    flow = fm.FFFlowMesh(SimpleNamespace(
        get_printer=lambda: printer,
        getfloat=lambda o, d=None, **k: {'min_thickness': 0.1,
                                         'split_distance': 2.}.get(o, d)))
    printer.objects["ff_flow_mesh"] = flow
    flow.start(0.1, None, 0.2)
    assert printer.gcode_move.move_transform is flow
    flow.move([60., 100., 0.2, 0.], 100.)
    flow.move([180., 100., 0.2, 1.], 100.)
    # The first layer's nozzle is flat ON THE TILTED PLANE: frame height
    # exactly 0.2 at every split, the plate absorbed in the layer.
    for m in printer.toolhead.moves[-60:]:
        assert obj.to_frame(m)[2] - OZ == pytest.approx(0.2, abs=1e-9)


def test_the_tools_offsets_ride_outside_the_rotation(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, tilted, tool=1)
    oz = OFFSETS[1][2] + JOB_Z
    m = printer.gmove(150., 100., 80.)
    w = printer.bed_mesh.get_mesh().calc_z(157., 97.)
    assert nozzle(m, oz) == pytest.approx(
        obj.tilt.forward(157., 97., 80. + w), abs=1e-9)
    assert printer.frame.get_position()[:3] == pytest.approx(
        [150., 100., 80.], abs=1e-6)


def test_dock_moves_in_the_raw_frame_pass_through(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, tilted)
    printer.frame.tool = None
    assert obj.stage.move([280., 50., 30., 0.], 100.) is None
    assert printer.toolhead.moves[-1] == [280., 50., 30., 0.]
    assert obj.stage.get_position() == printer.toolhead.get_position()


def test_stop_puts_the_toolhead_and_the_measured_mesh_back(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, bumpy)
    plate = printer.bed_mesh.get_mesh().plate
    assert obj.stop()
    assert printer.bed_mesh.toolhead is printer.toolhead
    assert printer.bed_mesh.get_mesh() is plate
    assert 'set_mesh' not in vars(printer.bed_mesh)
    assert not obj.active and not obj.stop()
    m = printer.gmove(150., 100., 80.)
    assert m[:2] == [150., 100.]


def test_stop_refuses_if_the_stage_was_displaced(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, bumpy)
    printer.bed_mesh.toolhead = Toolhead()
    with pytest.raises(CommandError, match="replaced"):
        obj.stop()


def test_a_restart_refits_the_plate_not_the_frame_mesh(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, tilted)
    first = obj.tilt.describe()
    obj.cmd_TILT_CORRECTION_START(Cmd())
    again = obj.tilt.describe()
    for key in ('x0', 'y0', 'z0', 'gx', 'gy'):
        assert again[key] == pytest.approx(first[key])
    assert printer.bed_mesh.toolhead is obj.stage
    assert obj.stage.toolhead is printer.toolhead     # not stacked twice


def test_dry_run_reports_without_applying(tc, tmp_path):
    obj, printer = make(tc, tmp_path, tilted)
    cmd = Cmd(DRY_RUN=1)
    obj.cmd_TILT_CORRECTION_START(cmd)
    assert not obj.active
    assert printer.bed_mesh.toolhead is printer.toolhead
    assert not isinstance(printer.bed_mesh.get_mesh(), tc.FrameMesh)
    assert "DRY_RUN" in cmd.info[0]


def test_no_mesh_no_correction(tc, tmp_path):
    obj, _ = make(tc, tmp_path, None)
    with pytest.raises(CommandError, match="no bed mesh"):
        obj.cmd_TILT_CORRECTION_START(Cmd())
    cmd = Cmd(OPTIONAL=1)
    obj.cmd_TILT_CORRECTION_START(cmd)
    assert not obj.active and "without tilt correction" in cmd.info[0]


def test_a_tight_envelope_reduces_the_tilt(tc, tmp_path):
    # The part reaches X 200 at Z 150; a bed falling toward +X leans it out.
    obj, _ = make(tc, tmp_path, lambda x, y: -0.004 * x, config={
        'envelope_min': (0., 0., -10.), 'envelope_max': (200.2, 256., 270.)})
    cmd = Cmd()
    obj.cmd_TILT_CORRECTION_START(cmd)
    assert 0. < obj.last['scale'] < 1.
    assert "reduced to" in cmd.info[0]
