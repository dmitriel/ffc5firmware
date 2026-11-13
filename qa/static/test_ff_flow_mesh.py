"""Flow mesh: a flat nozzle, layers that absorb the plate, and a flat finish."""

import importlib.util
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
    r, t = fm.layer_surface(0., [0.2, 0.2], 0.1, 0.4)
    assert (r, t) == (0., 0.2)


def test_a_small_bump_is_absorbed_by_the_first_layer(fm):
    r, t = fm.layer_surface(0.05, [0.2], 0.1, 0.4)
    assert t == pytest.approx(0.15)
    assert r == pytest.approx(0.)


def test_a_high_spot_holds_the_minimum_and_carries_the_rest(fm):
    r1, t1 = fm.layer_surface(0.15, [0.2], 0.1, 0.4)
    assert t1 == pytest.approx(0.1)          # cannot squash below 0.1
    assert r1 == pytest.approx(0.05)         # nozzle followed by 0.05
    r2, t2 = fm.layer_surface(0.15, [0.2, 0.2], 0.1, 0.4)
    assert t2 == pytest.approx(0.15)
    assert r2 == pytest.approx(0.)           # flat after layer 2


def test_a_low_spot_holds_the_maximum_and_carries_the_rest(fm):
    r1, t1 = fm.layer_surface(-0.3, [0.2], 0.1, 0.4)
    assert t1 == pytest.approx(0.4)
    assert r1 == pytest.approx(-0.1)
    r2, _ = fm.layer_surface(-0.3, [0.2, 0.2], 0.1, 0.4)
    assert r2 == pytest.approx(0.)


def test_max_flow_spreads_a_deep_low_spot_over_more_layers(fm):
    heights = [0.2, 0.1, 0.1, 0.1, 0.1]
    # Unlimited, layer 2 would be 0.2 thick: twice its own height.
    _, t2 = fm.layer_surface(-0.3, heights[:2], 0.1, 0.4)
    assert t2 == pytest.approx(0.2)
    # At 1.5x: layer 1 0.3 (-0.2 left), then 0.15 a layer, each taking
    # 0.05 off what is left, flat after layer 5.
    expected = [0.3, 0.15, 0.15, 0.15, 0.15]
    for n, t in enumerate(expected, 1):
        r, got = fm.layer_surface(-0.3, heights[:n], 0.1, 0.4, 1.5)
        assert got == pytest.approx(t)
    assert r == pytest.approx(0.)


def test_max_flow_never_forces_a_layer_below_its_own_height(fm):
    # 1.5 x 0.2 = 0.3 is above h, the nozzle limit 0.4 is not the cap.
    assert fm.layer_surface(0., [0.2], 0.1, 0.4, 1.5) == (0., 0.2)
    assert fm.layer_surface(0., [0.5], 0.1, 0.4, 1.5) == (0., 0.5)


def test_a_layer_thinner_than_the_minimum_keeps_its_own_height(fm):
    assert fm.layer_surface(0., [0.08], 0.1, 0.4) == (0., 0.08)


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
    obj = fm.FFFlowMesh(Config(printer, min_thickness=0.1,
                               split_distance=2., max_flow=1.5))
    if mean is not None:
        obj._mean = lambda mesh: mean
    obj.start(0.1, None, 0.2, max_flow or 100.)
    return obj, printer


def line(obj, x0, x1, z, e0, e1):
    """Travel to the line's start, then extrude it at Z."""
    obj.move([x0, 10., z, e0], 100.)
    obj.move([x1, 10., z, e1], 100.)


def test_the_nozzle_stays_flat_and_flow_absorbs_a_bump(fm):
    obj, printer = started(fm, lambda x, y: 0.05)
    line(obj, 0., 10., 0.2, 0., 1.)
    extruding = printer.next.moves[-5:]      # 10 mm split every 2 mm
    assert [m[0] for m in extruding] == pytest.approx([2., 4., 6., 8., 10.])
    # bed_mesh adds +0.05 below, so the nozzle lands at exactly Z 0.2.
    assert all(m[2] == pytest.approx(0.15) for m in extruding)
    assert extruding[-1][3] == pytest.approx(0.75)   # 0.15 / 0.2 of E
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


def test_it_refuses_without_a_mesh_or_with_fade(fm):
    printer = Printer(lambda x, y: 0., fade=True)
    obj = fm.FFFlowMesh(Config(printer))
    with pytest.raises(RuntimeError, match="fade"):
        obj.start(0.1, None, 0.2)
    printer.objects["bed_mesh"].get_mesh = lambda: None
    with pytest.raises(RuntimeError, match="no bed mesh"):
        obj.start(0.1, None, 0.2)


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
        obj.start(0.1, None, 0.)


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
    seg_e = last[3] - printer.next.moves[-2][3]
    assert seg_e == pytest.approx(0.2 * 0.11 / 0.2)


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
    assert printer.next.speeds[-5:] == pytest.approx([100. / 0.75] * 5)
    obj2, printer2 = started(fm, lambda x, y: -0.1)  # layer 1 at 1.5x
    line(obj2, 0., 10., 0.2, 0., 1.)
    assert printer2.next.speeds[-5:] == pytest.approx([100. / 1.5] * 5)


def test_travel_and_retracts_keep_their_speed(fm):
    obj, printer = started(fm, lambda x, y: 0.05)
    obj.move([0., 10., 0.2, 0.], 300.)               # travel
    assert printer.next.speeds[-1] == 300.
    line(obj, 0., 10., 0.2, 0., 1.)
    obj.move([10., 10., 0.2, 0.2], 40.)              # retract in place
    assert printer.next.speeds[-1] == 40.


def test_the_configured_max_flow_is_the_default(fm):
    printer = Printer(lambda x, y: -0.001 * x)
    obj = fm.FFFlowMesh(Config(printer, min_thickness=0.1,
                               split_distance=2., max_flow=1.5))
    obj.start(0.1, None, 0.2)
    assert obj.run_flow == 1.5
    assert obj.get_status(0.)["max_flow"] == 1.5
