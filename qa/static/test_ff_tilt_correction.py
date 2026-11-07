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
        q = t.rotate(x, y, 0.)
        assert q[2] == pytest.approx(t.plane(q[0], q[1]), abs=1e-12)
    assert t.rotate(120., 90., 0.) == pytest.approx((120., 90., 0.1))


@pytest.mark.parametrize("gx, gy", SLOPES)
def test_what_goes_down_is_the_rotated_point_less_the_plane(tc, gx, gy):
    """bed_mesh adds the plane back: the nozzle lands on the rotated point."""
    t = tc.TiltMap(120., 90., 0.1, gx, gy)
    for p in ((10., 20., 0.2), (200., 150., 75.), (128., 128., 250.)):
        out = t.forward(*p)
        q = t.rotate(*p)
        assert out[0] == pytest.approx(q[0], abs=1e-9)
        assert out[1] == pytest.approx(q[1], abs=1e-9)
        assert out[2] + t.plane(out[0], out[1]) == pytest.approx(q[2],
                                                                abs=1e-9)
        # ...and the vertical part is the height scaled by s, nothing else.
        assert out[2] == pytest.approx(t.s * p[2], abs=1e-9)
        assert t.inverse(*out) == pytest.approx(p, abs=1e-9)


def test_the_part_is_printed_as_a_rigid_body(tc):
    t = tc.TiltMap(100., 100., -0.05, 0.004, -0.002)
    a, b = (20., 30., 0.2), (180., 160., 120.)
    assert dist(t.rotate(*a), t.rotate(*b)) == pytest.approx(dist(a, b))


def test_a_level_bed_changes_nothing(tc):
    t = tc.TiltMap(100., 100., 0.3, 0., 0.)
    assert t.forward(12., 34., 56.) == pytest.approx((12., 34., 56.))
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


def test_z_counts_what_the_chain_below_adds(tc):
    boxes = [(50., 200., 50., 200., 0., 265.)]
    excess = tc.envelope_excess(tc.scaled_map(fit_of(0.001, 0.), 0.), boxes,
                                ENV_MIN, ENV_MAX, 3., 3.4)
    assert excess[('Z', 1)][0] == pytest.approx(265. + 3.4 - 270.)


# -- the transform, against a fake klippy -------------------------------------

class Mesh:
    def __init__(self, z):
        self.z = z

    def calc_z(self, x, y):
        return self.z(x, y)

    def get_z_range(self):
        values = [self.z(x, y) for x in range(0, 257, 32)
                  for y in range(0, 257, 32)]
        return min(values), max(values)


class Next:
    def __init__(self):
        self.pos = [0., 0., 10., 0.]
        self.moves = []
        self.next_transform = None

    def move(self, newpos, speed):
        self.moves.append(list(newpos))
        self.pos = list(newpos)

    def get_position(self):
        return list(self.pos)


class Above:
    """exclude_object, installed over us mid-print."""

    def __init__(self, below):
        self.next_transform = below


class GcodeMove:
    def __init__(self, below):
        self.move_transform = below
        self.homing_position = [0., 0., 0., 0.]
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


class Printer:
    command_error = CommandError

    def __init__(self, mesh_z, tool=0, offsets=((0., 0., 3.2),) * 4,
                 fade=False, flow=False, filename=None):
        self.gcode = Gcode()
        self.next = Next()
        self.gcode_move = GcodeMove(self.next)
        self.frame = SimpleNamespace(tool=tool)
        tc_offsets = SimpleNamespace(
            offset_x=[o[0] for o in offsets],
            offset_y=[o[1] for o in offsets],
            offset_z=[o[2] for o in offsets], job_z=0.05)
        tc_offsets.gcode_transform = SimpleNamespace(
            tool=None,
            _offsets=lambda: None if self.frame.tool is None else (
                offsets[self.frame.tool][0], offsets[self.frame.tool][1],
                offsets[self.frame.tool][2]))
        tc_offsets._current_tool_or_none = lambda: (self.frame.tool, '')
        self.objects = {
            "gcode": self.gcode,
            "gcode_move": self.gcode_move,
            "bed_mesh": SimpleNamespace(
                get_mesh=lambda: Mesh(mesh_z) if mesh_z else None,
                FADE_DISABLE=0x7FFFFFFF,
                fade_end=5. if fade else 0x7FFFFFFF),
            "ff_toolchange": tc_offsets,
            "ff_flow_mesh": SimpleNamespace(active=flow),
            "ff_print": SimpleNamespace(filename=filename),
        }

    def get_reactor(self):
        return SimpleNamespace(monotonic=lambda: 0., pause=lambda t: None)

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


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
    ["M83", "T0", "G1 X40 Y60 Z0.2"]
    + ["G1 X%d Y%d E1" % (x, y) for x, y in
       ((200, 60), (200, 62), (40, 62)) for _ in range(1)]
    + ["G1 X40 Y%d E1\nG1 X200 Y%d E1" % (y, y) for y in range(64, 200, 4)]
    + ["G1 Z150", "G1 X120 Y130 Z150"])


def started(tc, tmp_path, mesh_z, **kw):
    path = tmp_path / "part.gcode"
    path.write_text(PART)
    printer = Printer(mesh_z, filename=str(path), **kw)
    obj = tc.FFTiltCorrection(Config(printer))
    cmd = Cmd()
    obj.cmd_TILT_CORRECTION_START(cmd)
    obj.job.thread.join()
    return obj, printer, cmd


def tilted(x, y):
    return 0.1 + 0.002 * x - 0.001 * y


def test_the_envelope_defaults_to_the_steppers(tc):
    obj = tc.FFTiltCorrection(Config(Printer(None)))
    assert obj.env_min == (-5., -5., -10.)
    assert obj.env_max == (310., 260., 270.)


def test_start_fits_the_mesh_under_the_footprint(tc, tmp_path):
    obj, printer, cmd = started(tc, tmp_path, tilted)
    assert obj.active
    assert printer.gcode_move.move_transform is obj
    t = obj.tilt
    assert t.gx == pytest.approx(0.002, abs=1e-9)
    assert t.gy == pytest.approx(-0.001, abs=1e-9)
    # The footprint is 40..200 x 60..200: the pivot is its centre.
    assert t.x0 == pytest.approx(120., abs=3.)
    assert t.y0 == pytest.approx(130., abs=3.)
    assert t.z0 == pytest.approx(tilted(t.x0, t.y0), abs=1e-9)
    assert "Z offset" in cmd.info[0]


def test_moves_go_down_tilted_and_come_back_exact(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, tilted)
    obj.move([150., 100., 80., 5.], 100.)
    down = printer.next.moves[-1]
    assert down[3] == 5.
    assert down[:3] == pytest.approx(list(obj.tilt.forward(150., 100., 80.)))
    assert obj.get_position() == pytest.approx([150., 100., 80., 5.])


def test_the_tools_offset_places_the_plane(tc, tmp_path):
    offsets = ((0., 0., 3.2), (7., -3., 3.1), (0., 0., 3.2), (0., 0., 3.2))
    obj, printer, _ = started(tc, tmp_path, tilted, tool=1, offsets=offsets)
    obj.move([150., 100., 80., 0.], 100.)
    expected = obj.tilt.forward(157., 97., 80.)
    assert printer.next.moves[-1][:3] == pytest.approx(
        [expected[0] - 7., expected[1] + 3., expected[2]])
    assert obj.get_position()[:3] == pytest.approx([150., 100., 80.])


def test_dock_moves_in_the_raw_frame_pass_through(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, tilted)
    printer.frame.tool = None
    obj.move([280., 50., 30., 0.], 100.)
    assert printer.next.moves[-1] == [280., 50., 30., 0.]
    assert obj.get_position() == [280., 50., 30., 0.]


def test_stop_unlinks_it_even_from_under_exclude_object(tc, tmp_path):
    obj, printer, _ = started(tc, tmp_path, tilted)
    above = Above(obj)
    printer.gcode_move.move_transform = above
    assert obj.stop()
    assert above.next_transform is printer.next
    assert not obj.active and not obj.stop()


def test_dry_run_reports_without_applying(tc, tmp_path):
    path = tmp_path / "part.gcode"
    path.write_text(PART)
    printer = Printer(tilted, filename=str(path))
    obj = tc.FFTiltCorrection(Config(printer))
    cmd = Cmd(DRY_RUN=1)
    obj.cmd_TILT_CORRECTION_START(cmd)
    assert not obj.active
    assert "DRY_RUN" in cmd.info[0]


@pytest.mark.parametrize("kw, match", [
    ({"fade": True}, "fade"),
    ({"flow": True}, "flow mesh"),
])
def test_it_refuses_what_it_cannot_combine_with(tc, tmp_path, kw, match):
    path = tmp_path / "part.gcode"
    path.write_text(PART)
    printer = Printer(tilted, filename=str(path), **kw)
    obj = tc.FFTiltCorrection(Config(printer))
    with pytest.raises(CommandError, match=match):
        obj.cmd_TILT_CORRECTION_START(Cmd())
    # From START_PRINT it says so and the print goes on untilted.
    cmd = Cmd(OPTIONAL=1)
    obj.cmd_TILT_CORRECTION_START(cmd)
    assert not obj.active
    assert "without tilt correction" in cmd.info[0]


def test_no_mesh_no_correction(tc, tmp_path):
    path = tmp_path / "part.gcode"
    path.write_text(PART)
    obj = tc.FFTiltCorrection(Config(Printer(None, filename=str(path))))
    with pytest.raises(CommandError, match="no bed mesh"):
        obj.cmd_TILT_CORRECTION_START(Cmd())


def test_a_tight_envelope_reduces_the_tilt(tc, tmp_path):
    # The part reaches X 200 at Z 150; a bed falling toward +X leans it out.
    path = tmp_path / "part.gcode"
    path.write_text(PART)
    printer = Printer(lambda x, y: -0.004 * x, filename=str(path))
    obj = tc.FFTiltCorrection(Config(printer, envelope_min=(0., 0., -10.),
                                     envelope_max=(200.2, 256., 270.)))
    cmd = Cmd()
    obj.cmd_TILT_CORRECTION_START(cmd)
    assert 0. < obj.last['scale'] < 1.
    assert "reduced to" in cmd.info[0]
