"""The bed guard refuses moves that bring the nozzle or carriage near the bed."""

import importlib.util
from types import SimpleNamespace

import pytest

from lib.paths import ROOT


pytestmark = pytest.mark.static
MODULE = (ROOT / "pkgs" / "klipper" / "payload" / "klipper" /
          "klippy" / "extras" / "ff_bed_guard.py")


@pytest.fixture(scope="module")
def ff_bed_guard():
    spec = importlib.util.spec_from_file_location("test_ff_bed_guard", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CommandError(Exception):
    pass


class Kinematics:
    def __init__(self, homed="xyz"):
        self.limits = [(-5., 310.) if a in homed else (1., -1.)
                       for a in "xyz"]
        self.checked = 0

    def check_move(self, move):
        self.checked += 1


class Toolhead:
    def __init__(self, printer, kin):
        self.printer = printer
        self.kin = kin
        self.special_queuing_state = ""
        self.pos = [100., 100., 20., 0.]

    def get_kinematics(self):
        return self.kin

    def move(self, newpos, speed=100.):
        move = Move(self, self.pos, newpos)
        self.kin.check_move(move)
        self.pos = list(newpos)


class Move:
    def __init__(self, toolhead, start, end):
        self.toolhead = toolhead
        self.start_pos = tuple(start)
        self.end_pos = tuple(end)

    def move_error(self, msg):
        return CommandError(msg)


class Gcode:
    def __init__(self):
        self.commands = {}

    def register_command(self, name, func, desc=None):
        self.commands[name] = func


class Toolchange:
    """Two calibrated tools: T0 nozzle meets the bed at machine Z 3.2."""

    def __init__(self, mounted=0):
        self.mounted = mounted
        self.offset_z = [3.2, 3.1, 3.3, 3.25]
        self.job_z = 0.
        self.tools = [SimpleNamespace(z_adjust=0.) for _ in range(4)]
        self.calibrated = {0, 1, 2, 3}

    def print_offset_ready(self, tool):
        return tool in self.calibrated

    def _current_tool_or_none(self, eventtime=None):
        return self.mounted, "ok"


class Mesh:
    def __init__(self, z):
        self.z = z
        self.mesh_matrix = [[z]]

    def calc_z(self, x, y):
        return self.z

    def get_z_range(self):
        return self.z, self.z


class Printer:
    command_error = CommandError

    def __init__(self, homed="xyz", toolchange=None, mesh=None):
        self.gcode = Gcode()
        self.kin = Kinematics(homed)
        self.toolhead = Toolhead(self, self.kin)
        self.handlers = {}
        self.objects = {"gcode": self.gcode, "toolhead": self.toolhead}
        if toolchange is not None:
            self.objects["ff_toolchange"] = toolchange
        if mesh is not None:
            self.objects["bed_mesh"] = SimpleNamespace(get_mesh=lambda: mesh)

    def get_reactor(self):
        return SimpleNamespace(monotonic=lambda: 0.)

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, event, handler):
        self.handlers[event] = handler


class Config:
    error = RuntimeError

    def __init__(self, printer, **values):
        self.printer = printer
        self.values = values

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "ff_bed_guard"

    def getfloat(self, option, default, minval=None):
        return self.values.get(option, default)

    def getfloatlist(self, option, default, count=None):
        return self.values.get(option, default)

    def getboolean(self, option, default):
        return self.values.get(option, default)


def install(module, mounted=0, homed="xyz", mesh=None, toolchange=True,
            **values):
    tc = Toolchange(mounted) if toolchange else None
    printer = Printer(homed, tc, mesh)
    guard = module.load_config(Config(printer, **values))
    printer.handlers["klippy:connect"]()
    return printer, guard, tc


def test_nozzle_may_reach_the_limit_but_not_below(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard)
    toolhead = printer.toolhead
    toolhead.move([100., 100., 3.3, 0.])        # exactly 0.1 above the bed
    with pytest.raises(CommandError, match="Bed guard"):
        toolhead.move([100., 100., 3.29, 0.])
    assert toolhead.pos[2] == pytest.approx(3.3)


def test_kinematics_check_still_runs(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard)
    printer.toolhead.move([100., 100., 10., 0.])
    assert printer.kin.checked == 1


def test_off_bed_moves_are_free(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard)
    toolhead = printer.toolhead
    toolhead.pos = [266.5, 13.8, 10., 0.]
    toolhead.move([266.5, 13.8, 1.0, 0.])       # the wipe spot, X > 263


def test_crossing_onto_the_bed_low_is_refused(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard)
    toolhead = printer.toolhead
    toolhead.pos = [266.5, 13.8, 1.0, 0.]
    with pytest.raises(CommandError):
        toolhead.move([200., 13.8, 1.0, 0.])
    # descending while still off the bed, then crossing high, is fine
    toolhead.move([266.5, 13.8, 10., 0.])
    toolhead.move([200., 13.8, 10., 0.])


def test_diagonal_reaching_low_only_off_bed_is_allowed(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard)
    toolhead = printer.toolhead
    toolhead.pos = [250., 100., 3.5, 0.]
    # still 3.305 at the bed edge (X 263), well below it past the edge
    toolhead.move([290., 100., 2.9, 0.])
    toolhead.pos = [250., 100., 3.5, 0.]
    with pytest.raises(CommandError, match="X263.00"):
        toolhead.move([290., 100., 0.5, 0.])


def test_bare_carriage_uses_carriage_contact(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, mounted=-1)
    toolhead = printer.toolhead
    toolhead.move([100., 100., 0.1, 0.])
    with pytest.raises(CommandError, match="bare carriage"):
        toolhead.move([100., 100., 0.05, 0.])


def test_unknown_tool_assumes_the_lowest(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, mounted=None)
    with pytest.raises(CommandError, match="tool unknown"):
        printer.toolhead.move([100., 100., 3.35, 0.])   # T2 is at 3.3
    printer.toolhead.move([100., 100., 3.4, 0.])


def test_job_z_and_z_adjust_move_the_plane(ff_bed_guard):
    printer, _guard, tc = install(ff_bed_guard)
    tc.job_z = -0.06                             # thin first layer
    printer.toolhead.move([100., 100., 3.24, 0.])
    tc.calibrated = set()                       # fallback: 0 + 3.2 + z_adjust
    tc.tools[0].z_adjust = 0.2
    with pytest.raises(CommandError):
        printer.toolhead.move([110., 100., 3.40, 0.])


def test_mesh_raises_the_limit(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, mesh=Mesh(0.3))
    with pytest.raises(CommandError):
        printer.toolhead.move([100., 100., 3.5, 0.])
    printer.toolhead.move([100., 100., 3.6, 0.])


def test_under_tilt_correction_the_measured_plate_is_the_limit(
        ff_bed_guard):
    # bed_mesh holds the residual (here 0: a pure plane) while the tilted
    # frame carries the plane; the plate is still 0.3 up, and the nozzle has
    # to clear that, not the residual.
    residual = SimpleNamespace(plate=Mesh(0.3), mesh_matrix=[[0.]],
                               calc_z=lambda x, y: 0.,
                               get_z_range=lambda: (0., 0.))
    printer, _guard, _tc = install(ff_bed_guard, mesh=residual)
    with pytest.raises(CommandError):
        printer.toolhead.move([100., 100., 3.5, 0.])
    printer.toolhead.move([100., 100., 3.6, 0.])


class DomeMesh(Mesh):
    """Flat except a bump rising to `height` midway along X 158..182."""

    def __init__(self, height):
        super().__init__(0.)
        self.height = height

    def calc_z(self, x, y):
        return max(0., self.height * (1. - ((x - 170.) / 12.) ** 2))

    def get_z_range(self):
        return 0., self.height


def test_bed_mesh_split_sag_on_a_first_layer_line_is_allowed(ff_bed_guard):
    # The refused print move: 0.113 above the mesh at both ends of a 24 mm
    # line that bed_mesh did not split, the mesh 0.028 higher in between.
    printer, guard, _tc = install(ff_bed_guard, mesh=DomeMesh(0.028))
    assert guard.mesh_tolerance == pytest.approx(0.025)
    printer.toolhead.pos = [158., 60., 3.313, 0.]
    printer.toolhead.move([182., 60., 3.313, 0.])


def test_a_real_dip_below_the_limit_is_still_refused(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, mesh=DomeMesh(0.05))
    printer.toolhead.pos = [158., 60., 3.313, 0.]
    with pytest.raises(CommandError, match="mesh tolerance"):
        printer.toolhead.move([182., 60., 3.313, 0.])


def test_mesh_tolerance_follows_bed_mesh_split_delta_z(ff_bed_guard):
    printer, guard, _tc = install(ff_bed_guard, mesh=DomeMesh(0.))
    assert guard.mesh_tolerance == pytest.approx(0.025)
    _p, guard, _tc = install(ff_bed_guard, mesh=DomeMesh(0.),
                             mesh_tolerance=0.)
    assert guard.mesh_tolerance == 0.


def test_pure_z_raise_is_always_allowed(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard)
    printer.toolhead.pos = [100., 100., 1.0, 0.]
    printer.toolhead.move([100., 100., 2.0, 0.])


def test_a_raise_off_a_low_touch_with_step_rounding_is_allowed(ff_bed_guard):
    # The probe stopped at (12.998, 3.996), 0.134 below the plane; the
    # retract goes back up to the point it was sent to.
    printer, _guard, _tc = install(ff_bed_guard, mounted=-1)
    printer.toolhead.pos = [12.998, 3.996, -0.134, 0.]
    printer.toolhead.move([13.000, 4.000, 0.076, 0.])


def test_a_raise_that_also_travels_is_still_checked(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, mounted=-1)
    printer.toolhead.pos = [100., 100., -0.134, 0.]
    with pytest.raises(CommandError):
        printer.toolhead.move([101., 100., 0.076, 0.])


def test_homing_moves_are_exempt(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, mounted=-1)
    printer.toolhead.special_queuing_state = "Drip"
    printer.toolhead.move([100., 100., -10., 0.])


def test_unhomed_z_is_not_checked(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, homed="xy")
    printer.toolhead.move([100., 100., -5., 0.])


def test_unhomed_xy_counts_as_over_the_bed(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, homed="z")
    printer.toolhead.pos = [290., 100., 10., 0.]
    with pytest.raises(CommandError):
        printer.toolhead.move([290., 100., 1.0, 0.])


def test_suspended_and_disabled(ff_bed_guard):
    printer, guard, _tc = install(ff_bed_guard)
    with guard.suspended("TOOL_LOCATE_SENSOR"):
        printer.toolhead.move([100., 100., -1.0, 0.])
        assert guard.get_status(0.)["suspended_by"] == ["TOOL_LOCATE_SENSOR"]
    assert guard.active()
    printer.toolhead.move([100., 100., 10., 0.])
    gcmd = SimpleNamespace(get_int=lambda *a, **k: 0,
                           respond_info=lambda msg: None)
    printer.gcode.commands["SET_BED_GUARD"](gcmd)
    printer.toolhead.move([100., 100., 0.0, 0.])


def test_custom_footprint_and_clearance(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, bed_max=(200., 200.),
                                   min_clearance=0.5)
    toolhead = printer.toolhead
    toolhead.pos = [220., 100., 10., 0.]
    toolhead.move([220., 100., 1.0, 0.])        # off this smaller bed
    toolhead.pos = [100., 100., 10., 0.]
    with pytest.raises(CommandError):
        toolhead.move([100., 100., 3.6, 0.])


def test_no_toolchanger_assumes_a_nozzle(ff_bed_guard):
    printer, _guard, _tc = install(ff_bed_guard, toolchange=False)
    with pytest.raises(CommandError):
        printer.toolhead.move([100., 100., 3.2, 0.])
    printer.toolhead.move([100., 100., 3.3, 0.])


def test_shipped_config_declares_the_guard():
    cfg = (ROOT / "pkgs" / "klipper-config" / "payload" / "config" /
           "ff-toolchange.cfg").read_text()
    assert "\n[ff_bed_guard]\n" in cfg
