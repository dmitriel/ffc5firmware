"""Mesh probing lifts just past the probe switch's release, at full XY speed."""

import importlib.util
from types import SimpleNamespace

import pytest

from lib.paths import ROOT


pytestmark = pytest.mark.static
MODULE = (ROOT / "pkgs" / "klipper" / "payload" / "klipper" /
          "klippy" / "extras" / "ff_bed_mesh.py")


@pytest.fixture(scope="module")
def ff_bed_mesh():
    spec = importlib.util.spec_from_file_location("test_ff_bed_mesh", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Helper:
    speed = 300.
    lift_speed = 20.

    def __init__(self):
        self.moves = []
        self.probed_with = None

    def _move(self, coord, speed):
        self.moves.append((coord, speed))

    def start_probe(self, gcmd):
        self.probed_with = dict(gcmd.get_command_parameters())


class Toolhead:
    def __init__(self):
        self.z = 0.

    def get_position(self):
        return [0., 0., self.z, 0.]


class Gcmd:
    error = RuntimeError

    def __init__(self, **params):
        self.params = {k: str(v) for k, v in params.items()}
        self.info = []

    def get_command_parameters(self):
        return self.params

    def get_int(self, name, default=None, minval=None):
        return int(self.params.get(name, default))

    def get_float(self, name, default=None, minval=None, above=None):
        return float(self.params.get(name, default))

    def respond_info(self, msg):
        self.info.append(msg)


class Gcode:
    def __init__(self):
        self.commands = {}

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def create_gcode_command(self, command, commandline, params):
        return Gcmd(**params)


class Printer:
    def __init__(self, helper, with_mesh=True):
        self.toolhead = Toolhead()
        self.gcode = Gcode()
        mesh = SimpleNamespace(bmc=SimpleNamespace(
            probe_mgr=SimpleNamespace(probe_helper=helper)))
        self.objects = {"toolhead": self.toolhead, "gcode": self.gcode}
        if with_mesh:
            self.objects["bed_mesh"] = mesh

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class Config:
    error = RuntimeError

    def __init__(self, printer, **values):
        self.printer = printer
        self.values = values

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "ff_bed_mesh"

    def getfloat(self, option, default, minval=None, above=None):
        return self.values.get(option, default)


def install(module, **values):
    helper = Helper()
    printer = Printer(helper)
    adapter = module.FFBedMesh(Config(printer, **values))
    return adapter, helper, printer


def test_the_first_move_goes_to_the_safe_height_at_full_speed(ff_bed_mesh):
    _, helper, printer = install(ff_bed_mesh)
    printer.toolhead.z = 0.5

    helper._raise_tool(True)

    assert helper.moves == [([None, None, 5.0], helper.speed)]


def test_the_first_move_never_lowers_a_head_above_the_safe_height(ff_bed_mesh):
    _, helper, printer = install(ff_bed_mesh)
    printer.toolhead.z = 8.

    helper._raise_tool(True)

    assert helper.moves[0][0] == [None, None, 8.0]


def test_later_moves_lift_the_clearance_above_the_last_trigger(ff_bed_mesh):
    _, helper, printer = install(ff_bed_mesh)
    printer.toolhead.z = 0.292

    helper._raise_tool(False)

    coord, speed = helper.moves[0]
    assert coord[2] == pytest.approx(2.292)
    assert speed == helper.lift_speed


def test_a_low_target_uses_the_recovery_height_without_latching(ff_bed_mesh):
    _, helper, printer = install(ff_bed_mesh)
    printer.toolhead.z = -1.5

    helper._raise_tool(False)
    printer.toolhead.z = 0.3
    helper._raise_tool(False)

    assert helper.moves[0][0] == [None, None, 3.0]
    assert helper.moves[1][0][2] == pytest.approx(2.3)


def test_only_the_mesh_helper_instance_is_patched(ff_bed_mesh):
    adapter, helper, printer = install(ff_bed_mesh)
    other = Helper()

    assert "_raise_tool" in vars(helper)
    assert "_raise_tool" not in vars(other)
    assert "FF_BED_MESH_STATUS" in printer.gcode.commands
    assert adapter.get_status(0)["dynamic_z"] is True


def test_heights_that_could_not_clear_the_threshold_are_refused(ff_bed_mesh):
    printer = Printer(Helper())

    with pytest.raises(RuntimeError, match="first_move_z"):
        ff_bed_mesh.FFBedMesh(Config(printer, first_move_z=1.0))
    with pytest.raises(RuntimeError, match="recovery_move_z"):
        ff_bed_mesh.FFBedMesh(Config(printer, recovery_move_z=1.0))


def test_it_needs_bed_mesh(ff_bed_mesh):
    printer = Printer(Helper(), with_mesh=False)

    with pytest.raises(RuntimeError, match="after \\[bed_mesh\\]"):
        ff_bed_mesh.FFBedMesh(Config(printer))


def test_the_lift_is_the_measured_release_plus_the_margin(ff_bed_mesh):
    adapter, helper, printer = install(ff_bed_mesh, release_distance=0.35,
                                       release_margin=1.0)
    printer.toolhead.z = 0.2

    helper._raise_tool(False)

    assert adapter.lift() == pytest.approx(1.35)
    assert helper.moves[0][0][2] == pytest.approx(1.55)
    assert helper.moves[0][1] == helper.lift_speed


def test_samples_retract_by_the_same_lift_unless_told_otherwise(ff_bed_mesh):
    _, helper, _ = install(ff_bed_mesh, release_distance=0.35)

    helper.start_probe(Gcmd())
    assert float(helper.probed_with["SAMPLE_RETRACT_DIST"]) == \
        pytest.approx(1.35)

    helper.start_probe(Gcmd(SAMPLE_RETRACT_DIST=4))
    assert helper.probed_with["SAMPLE_RETRACT_DIST"] == "4"


def test_xy_travel_runs_at_travel_speed_and_z_keeps_its_own(ff_bed_mesh):
    _, helper, printer = install(ff_bed_mesh, travel_speed=500.)
    assert helper.speed == 500.
    helper._raise_tool(False)
    assert helper.moves[0][1] == 20.        # lift_speed, untouched


def test_without_travel_speed_bed_mesh_keeps_its_own(ff_bed_mesh):
    _, helper, _ = install(ff_bed_mesh)
    assert helper.speed == 300.


def test_another_helper_can_borrow_the_travel(ff_bed_mesh):
    adapter, _, printer = install(ff_bed_mesh, travel_speed=500.)
    other = Helper()
    adapter.attach(other)
    printer.toolhead.z = 0.3
    other._raise_tool(False)
    assert other.speed == 500.
    assert other.moves[0][0][2] == pytest.approx(2.3)


class SwitchProbe:
    """A switch that closes at z <= 0.1 and opens again at z >= 0.1 + gap."""

    def __init__(self, toolhead, gap):
        self.toolhead, self.gap = toolhead, gap
        self.triggered = False
        self.mcu_probe = SimpleNamespace(query_endstop=self._query)

    def _query(self, print_time):
        if self.toolhead.z >= 0.1 + self.gap - 1e-9:
            self.triggered = False
        return self.triggered

    def get_probe_params(self, gcmd=None):
        return {"lift_speed": 10.}

    def start_probe_session(self, gcmd):
        return self

    def run_probe(self, gcmd):
        self.toolhead.z = 0.1
        self.triggered = True

    def pull_probed_results(self):
        return [[0., 0., 0.1]]

    def end_probe_session(self):
        pass


class MovingToolhead(Toolhead):
    def wait_moves(self):
        pass

    def get_last_move_time(self):
        return 0.

    def manual_move(self, coord, speed):
        self.z = coord[2]


def test_the_release_test_measures_saves_and_applies_the_gap(ff_bed_mesh):
    adapter, helper, printer = install(ff_bed_mesh)
    printer.toolhead = MovingToolhead()
    printer.objects["toolhead"] = printer.toolhead
    printer.objects["probe"] = SwitchProbe(printer.toolhead, gap=0.34)
    saved = {}
    printer.objects["configfile"] = SimpleNamespace(
        set=lambda sec, opt, val: saved.__setitem__((sec, opt), val))

    gcmd = Gcmd(SAMPLES=3, STEP=0.01, SAVE=1)
    printer.gcode.commands["FF_PROBE_RELEASE_TEST"](gcmd)

    # Opened at 0.34 (to the step), plus one step of headroom.
    assert adapter.release_distance == pytest.approx(0.35)
    assert adapter.lift() == pytest.approx(1.35)
    assert saved == {("ff_bed_mesh", "release_distance"): "0.350"}
    assert len(gcmd.info) == 4


def test_a_switch_that_never_opens_is_reported(ff_bed_mesh):
    _, _, printer = install(ff_bed_mesh)
    printer.toolhead = MovingToolhead()
    printer.objects["toolhead"] = printer.toolhead
    printer.objects["probe"] = SwitchProbe(printer.toolhead, gap=10.)

    with pytest.raises(RuntimeError, match="still triggered"):
        printer.gcode.commands["FF_PROBE_RELEASE_TEST"](Gcmd(MAX=0.5,
                                                             STEP=0.05))
