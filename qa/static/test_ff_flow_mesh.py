"""Flow mesh: a flat nozzle, layers that absorb the plate, and a flat finish."""

import importlib.util
import math
from types import SimpleNamespace

import pytest

from lib.paths import ROOT


pytestmark = pytest.mark.static
MODULE = (ROOT / "pkgs" / "klipper" / "payload" / "klipper" /
          "klippy" / "extras" / "ff_flow_mesh.py")


@pytest.fixture(scope="module")
def fm():
    spec = importlib.util.spec_from_file_location("test_ff_flow_mesh", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- the arithmetic -------------------------------------------------------

def test_a_flat_bed_prints_plain_layers(fm):
    r, t = fm.layer_surface(0., [0.2, 0.2], 0.5, 0.4)
    assert (r, t) == (0., 0.2)


def test_a_small_bump_is_absorbed_by_the_first_layer(fm):
    r, t = fm.layer_surface(0.05, [0.2], 0.5, 0.4)
    assert t == pytest.approx(0.15)
    assert r == pytest.approx(0.)


def test_a_high_spot_holds_the_minimum_and_carries_the_rest(fm):
    r1, t1 = fm.layer_surface(0.15, [0.2], 0.5, 0.4)
    assert t1 == pytest.approx(0.1)          # cannot squash below h / 2
    assert r1 == pytest.approx(0.05)         # nozzle followed by 0.05
    r2, t2 = fm.layer_surface(0.15, [0.2, 0.2], 0.5, 0.4)
    assert t2 == pytest.approx(0.15)
    assert r2 == pytest.approx(0.)           # flat after layer 2


def test_a_low_spot_holds_the_maximum_and_carries_the_rest(fm):
    r1, t1 = fm.layer_surface(-0.3, [0.2], 0.5, 0.4)
    assert t1 == pytest.approx(0.4)
    assert r1 == pytest.approx(-0.1)
    r2, _ = fm.layer_surface(-0.3, [0.2, 0.2], 0.5, 0.4)
    assert r2 == pytest.approx(0.)


def test_max_flow_spreads_a_deep_low_spot_over_more_layers(fm):
    heights = [0.2, 0.1, 0.1, 0.1, 0.1]
    # Unlimited, layer 2 would be 0.2 thick: twice its own height.
    _, t2 = fm.layer_surface(-0.3, heights[:2], 0.5, 0.4)
    assert t2 == pytest.approx(0.2)
    # At 1.5x: layer 1 0.3 (-0.2 left), then 0.15 a layer, each taking
    # 0.05 off what is left, flat after layer 5.
    expected = [0.3, 0.15, 0.15, 0.15, 0.15]
    for n, t in enumerate(expected, 1):
        r, got = fm.layer_surface(-0.3, heights[:n], 0.5, 0.4, 1.5)
        assert got == pytest.approx(t)
    assert r == pytest.approx(0.)


def test_max_flow_never_forces_a_layer_below_its_own_height(fm):
    # 1.5 x 0.2 = 0.3 is above h, the nozzle limit 0.4 is not the cap.
    assert fm.layer_surface(0., [0.2], 0.5, 0.4, 1.5) == (0., 0.2)
    assert fm.layer_surface(0., [0.5], 0.5, 0.4, 1.5) == (0., 0.5)


def test_the_squish_limit_is_half_of_each_layers_height(fm):
    assert fm.layer_surface(0., [0.08], 0.5, 0.4) == (0., 0.08)
    # 0.12 mm layers over a 0.1 mm high spot: 0.06 each, 0.06 a layer off.
    r1, t1 = fm.layer_surface(0.1, [0.12], 0.5, 0.4)
    assert (r1, t1) == (pytest.approx(0.04), pytest.approx(0.06))
    r2, t2 = fm.layer_surface(0.1, [0.12, 0.12], 0.5, 0.4)
    assert (r2, t2) == (pytest.approx(0.), pytest.approx(0.08))


def test_layers_come_from_extruding_z(fm):
    layers = fm.LayerTracker()
    assert layers.note_extrusion(0.3)        # a purge line, higher
    assert layers.note_extrusion(0.2)        # the real first layer
    assert layers.tops == [0.2]
    assert not layers.note_extrusion(0.2)
    assert layers.note_extrusion(0.4)
    assert layers.heights() == pytest.approx([0.2, 0.2])


# -- the transform ---------------------------------------------------------

class Mesh:
    def __init__(self, z):
        self.z = z

    def calc_z(self, x, y):
        return self.z(x, y)

    def get_mesh_matrix(self):
        return [[self.z(x * 50., y * 50.) for x in range(5)]
                for y in range(5)]


class Next:
    """ff_toolchange + bed_mesh below us: records what reaches them."""

    def __init__(self):
        self.pos = [0., 0., 10., 0.]
        self.moves = []
        self.speeds = []

    def move(self, newpos, speed):
        self.moves.append(list(newpos))
        self.speeds.append(speed)
        self.pos = list(newpos)

    def get_position(self):
        return list(self.pos)


class GcodeMove:
    def __init__(self, below):
        self.move_transform = below
        self.homing_position = [0., 0., 0., 0.]
        self.base_position = [0., 0., 0., 0.]
        self.resets = 0

    def set_move_transform(self, transform, force=False):
        old, self.move_transform = self.move_transform, transform
        return old

    def reset_last_position(self):
        self.resets += 1


class Gcode:
    error = RuntimeError

    def __init__(self):
        self.commands, self.info = {}, []

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def respond_info(self, msg):
        self.info.append(msg)


class Printer:
    def __init__(self, mesh_z, tool_xy=(0., 0.), fade=False):
        self.gcode = Gcode()
        self.next = Next()
        self.gcode_move = GcodeMove(self.next)
        self.tool = SimpleNamespace(offsets=tool_xy)
        self.objects = {
            "gcode": self.gcode,
            "gcode_move": self.gcode_move,
            "bed_mesh": SimpleNamespace(
                get_mesh=lambda: Mesh(mesh_z), FADE_DISABLE=0x7FFFFFFF,
                fade_end=5. if fade else 0x7FFFFFFF),
            "ff_toolchange": SimpleNamespace(gcode_transform=SimpleNamespace(
                _offsets=lambda: None if self.tool.offsets is None
                else tuple(self.tool.offsets) + (0.,))),
            "toolhead": SimpleNamespace(get_extruder=lambda: SimpleNamespace(
                nozzle_diameter=0.4)),
        }

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class Config:
    def __init__(self, printer, **values):
        self.printer, self.values = printer, values

    def get_printer(self):
        return self.printer

    def getfloat(self, option, default=None, **limits):
        return self.values.get(option, default)


def started(fm, mesh_z, mean=0., max_flow=None, **kw):
    """Started on mesh_z. The transform tests check the arithmetic against
    the plain mesh, so the reference plane is pinned to `mean` (0) unless
    mean=None lets it be the mesh's own; max_flow None means unlimited."""
    printer = Printer(mesh_z, **kw)
    obj = fm.FFFlowMesh(Config(printer, min_flow=0.5,
                               split_distance=2., max_flow=1.5))
    if mean is not None:
        obj._mean = lambda mesh: mean
    obj.start(0.5, None, 0.2, max_flow or 100.)
    return obj, printer


def line(obj, x0, x1, z, e0, e1):
    """Travel to the line's start, then extrude it at Z."""
    obj.move([x0, 10., z, e0], 100.)
    obj.move([x1, 10., z, e1], 100.)


def test_the_nozzle_stays_flat_and_flow_absorbs_a_bump(fm):
    obj, printer = started(fm, lambda x, y: 0.05)
    line(obj, 0., 10., 0.2, 0., 1.)
    # A uniform bump: one straight piece holds, so the line goes as one.
    extruding = printer.next.moves[-1]
    assert extruding[0] == pytest.approx(10.)
    # bed_mesh adds +0.05 below, so the nozzle lands at exactly Z 0.2.
    assert extruding[2] == pytest.approx(0.15)
    assert extruding[3] == pytest.approx(0.75)       # 0.15 / 0.2 of E
    assert obj.flat_layer == 1


def test_after_the_mesh_is_absorbed_printing_is_flat_and_normal(fm):
    obj, printer = started(fm, lambda x, y: 0.05)
    line(obj, 0., 10., 0.2, 0., 1.)
    line(obj, 10., 0., 0.4, 1., 2.)
    second = printer.next.moves[-1]
    assert second[2] == pytest.approx(0.35)          # 0.4 - mesh
    # One full E of the second line, unscaled, after 0.75 of the first.
    assert second[3] == pytest.approx(1.75)


def test_a_high_spot_is_flattened_over_two_layers(fm):
    obj, printer = started(fm, lambda x, y: 0.15)
    line(obj, 0., 10., 0.2, 0., 1.)
    first = printer.next.moves[-1]
    assert first[2] + 0.15 == pytest.approx(0.25)    # nozzle followed 0.05
    assert first[3] == pytest.approx(0.5)            # 0.1 / 0.2
    assert obj.flat_layer is None
    line(obj, 10., 0., 0.4, 1., 2.)
    second = printer.next.moves[-1]
    assert second[2] + 0.15 == pytest.approx(0.4)    # back on the plane
    assert second[3] - first[3] == pytest.approx(0.75)
    assert obj.flat_layer == 2


def test_the_approach_descends_to_the_first_layers_height(fm):
    obj, printer = started(fm, lambda x, y: 0.15)
    obj.move([0., 10., 0.2, 0.], 100.)               # travel, no E
    assert printer.next.moves[-1][2] + 0.15 == pytest.approx(0.25)


def test_a_z_hop_keeps_the_layers_offset(fm):
    obj, printer = started(fm, lambda x, y: 0.15)
    line(obj, 0., 10., 0.2, 0., 1.)
    obj.move([10., 10., 1.2, 1.], 100.)              # hop 1 mm
    assert printer.next.moves[-1][2] + 0.15 == pytest.approx(1.25)


def test_the_mesh_is_looked_up_under_the_mounted_tool(fm):
    seen = []

    def mesh_z(x, y):
        seen.append((x, y))
        return 0.
    obj, _ = started(fm, mesh_z, tool_xy=(3., -2.))
    seen.clear()
    obj.move([5., 10., 0.2, 0.], 100.)
    assert (8., 8.) in seen


def test_dock_moves_in_the_raw_frame_pass_through(fm):
    obj, printer = started(fm, lambda x, y: 0.15, tool_xy=None)
    obj.move([270., 10., 5., 0.], 100.)
    assert printer.next.moves[-1] == [270., 10., 5., 0.]


def test_retracts_are_not_scaled(fm):
    obj, printer = started(fm, lambda x, y: 0.05)
    line(obj, 0., 10., 0.2, 0., 1.)
    obj.move([10., 10., 0.2, 0.2], 100.)             # retract 0.8 in place
    assert printer.next.moves[-1][3] == pytest.approx(0.75 - 0.8)


def test_stopping_hands_back_a_consistent_e(fm):
    obj, printer = started(fm, lambda x, y: 0.05)
    line(obj, 0., 10., 0.2, 0., 1.)
    assert obj.get_position()[3] == pytest.approx(1.)
    assert obj.stop()
    assert printer.gcode_move.move_transform is printer.next
    # The extruder sits 0.25 behind the file; the G-code E must not jump.
    assert printer.gcode_move.base_position[3] == pytest.approx(-0.25)
    assert not obj.stop()


def test_it_stops_from_under_exclude_object(fm):
    # exclude_object installs over us at the first EXCLUDE_OBJECT and stays
    # until the next file. END_PRINT's FLOW_MESH_STOP used to refuse here,
    # and the error skipped the rest of the exit sequence.
    obj, printer = started(fm, lambda x, y: 0.05)
    line(obj, 0., 10., 0.2, 0., 1.)
    exclude = SimpleNamespace(next_transform=obj)
    printer.gcode_move.move_transform = exclude
    assert obj.stop()
    assert printer.gcode_move.move_transform is exclude
    assert exclude.next_transform is printer.next
    assert printer.gcode_move.base_position[3] == pytest.approx(-0.25)


def test_it_still_refuses_when_it_cannot_find_itself(fm):
    obj, printer = started(fm, lambda x, y: 0.05)
    printer.gcode_move.move_transform = SimpleNamespace()   # hides the rest
    with pytest.raises(RuntimeError, match="not found"):
        obj.stop()
    assert obj.active


def test_it_refuses_without_a_mesh_or_with_fade(fm):
    printer = Printer(lambda x, y: 0., fade=True)
    obj = fm.FFFlowMesh(Config(printer))
    with pytest.raises(RuntimeError, match="fade"):
        obj.start(0.5, None, 0.2)
    printer.objects["bed_mesh"].get_mesh = lambda: None
    with pytest.raises(RuntimeError, match="no bed mesh"):
        obj.start(0.5, None, 0.2)


def test_a_purge_line_above_the_first_layer_does_not_count(fm):
    obj, _ = started(fm, lambda x, y: 0.15)
    line(obj, 0., 10., 0.3, 0., 1.)                  # purge at Z 0.3
    line(obj, 10., 0., 0.2, 1., 2.)                  # first layer
    assert obj.layers.tops == [0.2]


# -- the first layer comes from the print start --------------------------

def test_the_first_layer_is_known_before_anything_is_printed(fm):
    obj, printer = started(fm, lambda x, y: 0.15)
    assert obj.layers.tops == [0.2]
    # The very first travel down already sits at layer 1's offset.
    obj.move([0., 10., 5., 0.], 100.)
    assert printer.next.moves[-1][2] + 0.15 == pytest.approx(5.05)


def test_an_extrusion_below_the_first_layer_is_still_layer_one(fm):
    layers = fm.LayerTracker(0.2)
    assert not layers.note_extrusion(0.18)
    assert layers.note_extrusion(0.4)
    assert layers.note_extrusion(0.15)          # back down: layer 1 again
    assert layers.tops == [0.2]


def test_a_first_extrusion_off_the_given_height_is_reported(fm):
    obj, printer = started(fm, lambda x, y: 0.)
    line(obj, 0., 10., 0.3, 0., 1.)
    assert any("first extrusion is at Z 0.300" in m
               for m in printer.gcode.info)
    printer.gcode.info.clear()
    line(obj, 10., 0., 0.2, 1., 2.)
    assert not any("first extrusion" in m for m in printer.gcode.info)


def test_starting_without_a_first_layer_is_refused(fm):
    printer = Printer(lambda x, y: 0.)
    obj = fm.FFFlowMesh(Config(printer))
    with pytest.raises(RuntimeError, match="first layer height"):
        obj.start(0.5, None, 0.)


# -- the reference plane and the volumetric rate ---------------------------

def test_the_nozzle_follows_the_mesh_mean_and_absorbs_only_the_rest(fm):
    # The test mesh samples x = 0..200: mean (0 + 50 + ... + 200)/5 * k.
    obj, printer = started(fm, lambda x, y: -0.001 * x, mean=None)
    assert obj.reference == pytest.approx(-0.1)
    line(obj, 0., 10., 0.2, 0., 1.)
    # At x = 10 the plate is 0.01 low, 0.09 above the mean: layer 1 is
    # 0.2 - 0.09 = 0.11 thick there, the nozzle at Z 0.2 - 0.1.
    last = printer.next.moves[-1]
    assert last[2] + (-0.01) == pytest.approx(0.1)
    # Layer 1 is 0.1 + 0.001 x thick, sampled every 2 mm: x = 2..10 gives
    # 0.102 .. 0.110 of each 0.2 mm of E.
    assert last[3] == pytest.approx(0.102 + 0.104 + 0.106 + 0.108 + 0.110)


def test_a_uniformly_offset_plate_prints_plain_layers(fm):
    obj, printer = started(fm, lambda x, y: -0.25, mean=None)
    assert obj.flat_layer == 1
    line(obj, 0., 10., 0.2, 0., 1.)
    last = printer.next.moves[-1]
    assert last[2] - 0.25 == pytest.approx(-0.05)    # standard mesh
    assert last[3] == pytest.approx(1.)              # unscaled E
    assert printer.next.speeds[-1] == pytest.approx(100.)


def test_speed_is_scaled_to_hold_the_volumetric_rate(fm):
    obj, printer = started(fm, lambda x, y: 0.05)    # layer 1 at 0.75x
    line(obj, 0., 10., 0.2, 0., 1.)
    assert printer.next.speeds[-1] == pytest.approx(100. / 0.75)
    obj2, printer2 = started(fm, lambda x, y: -0.1)  # layer 1 at 1.5x
    line(obj2, 0., 10., 0.2, 0., 1.)
    assert printer2.next.speeds[-1] == pytest.approx(100. / 1.5)


def test_travel_and_retracts_keep_their_speed(fm):
    obj, printer = started(fm, lambda x, y: 0.05)
    obj.move([0., 10., 0.2, 0.], 300.)               # travel
    assert printer.next.speeds[-1] == 300.
    line(obj, 0., 10., 0.2, 0., 1.)
    obj.move([10., 10., 0.2, 0.2], 40.)              # retract in place
    assert printer.next.speeds[-1] == 40.


def test_the_configured_max_flow_is_the_default(fm):
    printer = Printer(lambda x, y: -0.001 * x)
    obj = fm.FFFlowMesh(Config(printer, min_flow=0.5,
                               split_distance=2., max_flow=1.5))
    obj.start(0.5, None, 0.2)
    assert obj.run_flow == 1.5
    assert obj.get_status(0.)["max_flow"] == 1.5
    assert obj.get_status(0.)["min_flow"] == 0.5


# -- how many pieces a move becomes ------------------------------------------

def _wavy(x, y):
    """A plate with a bump every ~60 mm, 0.05 mm high."""
    return 0.05 * math.sin(x / 10.) + 0.0002 * y


def _nozzle_error(printer, start, z):
    """Worst distance, at every split_distance sample, between where the
    nozzle goes (straight lines between the pieces sent down, plus the
    mesh bed_mesh adds) and where the 2 mm pieces would have put it."""
    sent = [list(start)] + printer.next.moves
    worst = 0.
    for i in range(len(sent) - 1):
        a, b = sent[i], sent[i + 1]
        length = abs(b[0] - a[0]) + abs(b[1] - a[1])
        steps = max(1, int(round(length / 2.)))
        for k in range(steps + 1):
            f = k / float(steps)
            x = a[0] + (b[0] - a[0]) * f
            y = a[1] + (b[1] - a[1]) * f
            nozzle = a[2] + (b[2] - a[2]) * f + _wavy(x, y)
            # Flat (mean pinned to 0): the nozzle belongs at G-code Z.
            worst = max(worst, abs(nozzle - z))
    return worst


def test_a_long_travel_after_flat_is_a_few_pieces_not_one_per_2mm(fm):
    obj, printer = started(fm, lambda x, y: 0.05)
    line(obj, 0., 10., 0.2, 0., 1.)
    line(obj, 10., 0., 0.4, 1., 2.)
    assert obj.flat_layer is not None
    printer.next.moves.clear()
    obj.move([240., 10., 0.6, 2.], 500.)            # 240 mm cross-bed travel
    # Flat plate: one straight piece holds, capped at MAX_MERGE samples.
    assert len(printer.next.moves) == 4             # 120 samples / 32
    assert printer.next.moves[-1][:3] == pytest.approx([240., 10., 0.55])


def test_merged_pieces_keep_the_nozzle_within_tolerance_of_the_plane(fm):
    obj, printer = started(fm, _wavy)
    obj.layers = fm.LayerTracker(0.2)
    obj.layers.tops = [0.2, 0.4]
    obj.flat_layer = 1
    obj.last_offset = obj.adjust(0., 10., 0.6, False)[0]
    printer.next.pos = [0., 10., 0.6 + obj.last_offset, 2.]
    obj.last = [0., 10., 0.6, 2.]
    printer.next.moves.clear()
    obj.move([240., 10., 0.6, 2.], 500.)
    pieces = len(printer.next.moves)
    assert pieces < 120 / 2                         # far fewer than before
    start = [0., 10., 0.6 + obj.adjust(0., 10., 0.6, False)[0]]
    assert _nozzle_error(printer, start, 0.6) <= \
        obj.split_tolerance + 1e-9


def test_extrusion_is_conserved_when_pieces_merge(fm):
    obj, printer = started(fm, _wavy)
    obj.move([0., 10., 0.2, 0.], 100.)
    obj.move([100., 10., 0.2, 5.], 100.)            # layer 1, flow varies
    # Every 2 mm sample's E is counted exactly once, merged or not.
    total = sum(0.1 * obj._surface(_wavy(2. * i, 10.), [0.2])[1] / 0.2
                for i in range(1, 51))
    assert printer.next.moves[-1][3] == pytest.approx(total)


def test_merge_pieces_splits_where_the_offset_bends(fm):
    flat = [(i, 0., 0., 0.1, 1.) for i in range(11)]
    assert fm.merge_pieces(flat, 0.005) == [(0, 10)]
    bent = [(i, 0., 0., 0.02 if i == 5 else 0., 1.) for i in range(11)]
    pieces = fm.merge_pieces(bent, 0.005)
    # The bump's own sample must be a piece end, or it is cut off.
    assert any(b == 5 for _a, b in pieces)
    flows = [(i, 0., 0., 0., 1. + 0.02 * i) for i in range(11)]
    assert len(fm.merge_pieces(flows, 0.005, 0.01)) == 10
