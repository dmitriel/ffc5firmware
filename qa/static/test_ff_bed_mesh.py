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

    def get(self, name, default=None):
        return self.params.get(name, default)

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

    def getint(self, option, default, minval=None):
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
        pytest.approx(0.55)

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
        return {"lift_speed": 10., "samples_tolerance": 0.02}

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
    assert adapter.lift() == pytest.approx(0.55)
    assert saved == {("ff_bed_mesh", "release_distance"): "0.350"}
    assert "holds its trigger" in gcmd.info[0]
    assert sum("opened 0.34" in m for m in gcmd.info) == 3


def test_a_switch_that_never_opens_is_reported(ff_bed_mesh):
    _, _, printer = install(ff_bed_mesh)
    printer.toolhead = MovingToolhead()
    printer.objects["toolhead"] = printer.toolhead
    printer.objects["probe"] = SwitchProbe(printer.toolhead, gap=10.)

    with pytest.raises(RuntimeError, match="still triggered"):
        printer.gcode.commands["FF_PROBE_RELEASE_TEST"](Gcmd(MAX=0.5,
                                                             STEP=0.05))


def test_an_unmeasured_probe_still_lifts_two_millimetres(ff_bed_mesh):
    adapter, _, _ = install(ff_bed_mesh)
    assert adapter.release_margin == pytest.approx(0.2)
    assert adapter.lift() == pytest.approx(2.0)


class AccelToolhead(Toolhead):
    def __init__(self):
        super().__init__()
        self.max_accel = 20000.

    def get_status(self, eventtime):
        return {"max_accel": self.max_accel}


def accel_printer(ff_bed_mesh, fail=False):
    adapter, helper, printer = install(ff_bed_mesh, travel_accel=20000.)
    printer.toolhead = AccelToolhead()
    printer.toolhead.max_accel = 2000.
    printer.objects["toolhead"] = printer.toolhead
    printer.get_reactor = lambda: SimpleNamespace(monotonic=lambda: 0.)
    scripts, seen = [], []

    def run(script):
        scripts.append(script)
        printer.toolhead.max_accel = float(script.split("ACCEL=")[1])
    printer.gcode.run_script_from_command = run

    def probe(gcmd):
        seen.append(printer.toolhead.max_accel)
        if fail:
            raise RuntimeError("probe failed")
    helper.start_probe = probe
    adapter.attach(helper)
    return helper, scripts, seen


def test_the_probe_run_moves_at_travel_accel_and_restores_it(ff_bed_mesh):
    helper, scripts, seen = accel_printer(ff_bed_mesh)
    helper.start_probe(Gcmd())
    assert seen == [20000.]
    assert scripts == ["SET_VELOCITY_LIMIT ACCEL=20000",
                       "SET_VELOCITY_LIMIT ACCEL=2000"]


def test_a_failed_probe_run_still_restores_the_accel(ff_bed_mesh):
    helper, scripts, _ = accel_printer(ff_bed_mesh, fail=True)
    with pytest.raises(RuntimeError):
        helper.start_probe(Gcmd())
    assert scripts[-1] == "SET_VELOCITY_LIMIT ACCEL=2000"


def test_mesh_points_take_the_configured_sample_count(ff_bed_mesh):
    _, helper, _ = install(ff_bed_mesh, samples=2)

    helper.start_probe(Gcmd())
    assert helper.probed_with["SAMPLES"] == "2"

    helper.start_probe(Gcmd(SAMPLES=5))
    assert helper.probed_with["SAMPLES"] == "5"


def test_without_samples_the_probe_section_decides(ff_bed_mesh):
    _, helper, _ = install(ff_bed_mesh)
    helper.start_probe(Gcmd())
    assert "SAMPLES" not in helper.probed_with


class ScriptedSession:
    """A fork ProbeSessionHelper whose touches read from a list."""

    def __init__(self, zs, tolerance=0.02, retries=10, result="average"):
        self.zs = list(zs)
        self.touches = 0
        self.results = []
        self.ended = False
        self.params = {"probe_speed": 3., "lift_speed": 10., "samples": 2,
                       "sample_retract_dist": 0.5,
                       "samples_tolerance": tolerance,
                       "samples_tolerance_retries": retries,
                       "samples_result": result}

    def get_probe_params(self, gcmd=None):
        return dict(self.params)

    def _probe(self, speed):
        self.touches += 1
        z = self.zs.pop(0)
        return 9999 if z == 9999 else [10., 20., z]

    def pull_probed_results(self):
        return self.results

    def end_probe_session(self):
        self.ended = True


class RetractToolhead(Toolhead):
    def __init__(self):
        super().__init__()
        self.retracts = []

    def manual_move(self, coord, speed):
        self.retracts.append((coord, speed))


def run_point(ff_bed_mesh, zs, **params):
    adapter, _, printer = install(ff_bed_mesh, samples=2)
    printer.toolhead = RetractToolhead()
    printer.objects["toolhead"] = printer.toolhead
    session = ScriptedSession(zs, **params)
    gcmd = Gcmd()
    ff_bed_mesh.TiebreakSession(adapter, session).run_probe(gcmd)
    return session, printer.toolhead, gcmd


def test_two_touches_that_agree_are_the_point(ff_bed_mesh):
    session, toolhead, gcmd = run_point(ff_bed_mesh, [0.100, 0.110])
    assert session.touches == 2
    assert session.results[0][2] == pytest.approx(0.105)
    assert len(toolhead.retracts) == 1          # between the two only
    assert toolhead.retracts[0][0][2] == pytest.approx(0.6)
    assert gcmd.info == []


def test_a_third_touch_breaks_the_tie_and_keeps_the_good_pair(ff_bed_mesh):
    session, _, gcmd = run_point(ff_bed_mesh, [0.100, 0.180, 0.110])
    assert session.touches == 3
    assert session.results[0][2] == pytest.approx(0.105)
    assert "one more" in gcmd.info[0]


def test_the_third_touch_can_side_with_the_second(ff_bed_mesh):
    session, _, _ = run_point(ff_bed_mesh, [0.180, 0.100, 0.105])
    assert session.results[0][2] == pytest.approx(0.1025)


def test_three_that_never_agree_start_the_point_over(ff_bed_mesh):
    session, _, gcmd = run_point(ff_bed_mesh,
                                 [0.10, 0.20, 0.30, 0.100, 0.101])
    assert session.touches == 5
    assert session.results[0][2] == pytest.approx(0.1005)
    assert any("Retrying" in m for m in gcmd.info)


def test_retries_run_out_like_klippys(ff_bed_mesh):
    with pytest.raises(RuntimeError, match="exceed samples_tolerance"):
        run_point(ff_bed_mesh, [0.1, 0.2, 0.3] * 2, retries=1)


def test_a_pretriggered_touch_is_retaken_but_not_forever(ff_bed_mesh):
    session, _, _ = run_point(ff_bed_mesh, [9999, 0.100, 0.101])
    assert session.results[0][2] == pytest.approx(0.1005)
    with pytest.raises(RuntimeError, match="still triggered"):
        run_point(ff_bed_mesh, [9999] * 5)


def test_median_results_follow_the_probe_section(ff_bed_mesh):
    session, _, _ = run_point(ff_bed_mesh, [0.100, 0.180, 0.110],
                              result="median")
    assert session.results[0][2] == pytest.approx(0.105)


class RealProbe:
    """start_probe_session as a method, the way klippy's PrinterProbe has it."""

    def start_probe_session(self, gcmd):
        return "real session"


def test_the_mesh_run_samples_with_the_tiebreak_and_then_lets_go(ff_bed_mesh):
    adapter, _, printer = install(ff_bed_mesh, samples=2)
    real = RealProbe()
    printer.objects["probe"] = real
    seen = []
    inner = Helper()
    inner.start_probe = lambda gcmd: seen.append(
        real.start_probe_session(gcmd))
    adapter.attach(inner)

    inner.start_probe(Gcmd())

    assert isinstance(seen[0], ff_bed_mesh.TiebreakSession)
    assert seen[0].session == "real session"
    # Afterwards PROBE and the calibrations get the standard session again.
    assert "start_probe_session" not in vars(real)
    assert real.start_probe_session(None) == "real session"


def test_the_session_is_released_when_the_mesh_fails(ff_bed_mesh):
    adapter, _, printer = install(ff_bed_mesh, samples=2)
    real = RealProbe()
    printer.objects["probe"] = real
    inner = Helper()

    def fail(gcmd):
        raise RuntimeError("probe failed")
    inner.start_probe = fail
    adapter.attach(inner)

    with pytest.raises(RuntimeError):
        inner.start_probe(Gcmd())
    assert "start_probe_session" not in vars(real)


class TapProbe(SwitchProbe):
    """A momentary trigger: open again the moment it stops. Starting a touch
    closer than `ready` lands it late; closer than `stuck` fails it."""

    def __init__(self, toolhead, ready, stuck=0.):
        super().__init__(toolhead, gap=0.)
        self.ready, self.stuck = ready, stuck
        self.z = None

    def _query(self, print_time):
        return False

    def run_probe(self, gcmd):
        start = self.toolhead.z
        lift = None if self.z is None else start - self.z
        if lift is not None and lift < self.stuck:
            raise RuntimeError("Probe triggered prior to movement")
        late = lift is not None and lift < self.ready
        self.z = 0.1 + (0.05 if late else 0.)
        self.toolhead.z = self.z

    def pull_probed_results(self):
        return [[0., 0., self.z]]


def tap_printer(ff_bed_mesh, ready, stuck=0.):
    adapter, _, printer = install(ff_bed_mesh)
    printer.toolhead = MovingToolhead()
    printer.toolhead.z = 5.
    printer.objects["toolhead"] = printer.toolhead
    printer.objects["probe"] = TapProbe(printer.toolhead, ready, stuck)
    printer.command_error = RuntimeError
    return adapter, printer


def test_a_momentary_probe_is_measured_by_where_it_still_repeats(ff_bed_mesh):
    adapter, printer = tap_printer(ff_bed_mesh, ready=0.3, stuck=0.15)
    gcmd = Gcmd(SAMPLES=3)
    printer.gcode.commands["FF_PROBE_RELEASE_TEST"](gcmd)

    assert "momentary" in gcmd.info[0]
    assert adapter.release_distance == pytest.approx(0.3)
    assert adapter.lift() == pytest.approx(0.5)
    assert any("0.10 mm: failed" in m for m in gcmd.info)
    assert any("0.20 mm: off by 0.05" in m for m in gcmd.info)
    assert any("0.30 mm: within" in m for m in gcmd.info)


def test_a_momentary_probe_that_never_repeats_keeps_the_default(ff_bed_mesh):
    # Late from anything under 2.5 mm: every candidate below MAX misses.
    adapter, printer = tap_printer(ff_bed_mesh, ready=2.5)
    with pytest.raises(RuntimeError, match="keep the default"):
        printer.gcode.commands["FF_PROBE_RELEASE_TEST"](Gcmd(SAMPLES=2))
    assert adapter.release_distance == pytest.approx(1.8)


def test_travel_between_points_lifts_at_least_the_minimum(ff_bed_mesh):
    adapter, helper, printer = install(ff_bed_mesh, release_distance=0.01,
                                       low_height_threshold=-5.)
    printer.toolhead.z = 2.0
    helper._raise_tool(False)
    assert adapter.lift() == pytest.approx(0.21)          # between samples
    assert helper.moves[0][0][2] == pytest.approx(3.0)    # 1 mm, not 0.21


class StoppedOffToolhead(RetractToolhead):
    """Stopped a few microns off the point, as the real carriage does."""

    def get_position(self):
        return [12.998, 3.996, self.z, 0.]


def test_the_sample_retract_goes_straight_up_from_where_it_stopped(
        ff_bed_mesh):
    adapter, _, printer = install(ff_bed_mesh, samples=2)
    printer.toolhead = StoppedOffToolhead()
    printer.objects["toolhead"] = printer.toolhead
    session = ScriptedSession([-0.134, -0.130])
    ff_bed_mesh.TiebreakSession(adapter, session).run_probe(Gcmd())
    (coord, _speed), = printer.toolhead.retracts
    assert coord[:2] == [12.998, 3.996]      # pure Z: nothing for the guard


def shipped_section():
    import configparser
    cp = configparser.RawConfigParser(strict=False,
                                      inline_comment_prefixes=("#", ";"))
    cp.read(ROOT / "pkgs" / "klipper-config" / "payload" / "config" /
            "ff-print-macros.cfg")
    return {k: float(v) for k, v in cp.items("ff_bed_mesh")}


@pytest.mark.parametrize("measured, travel_z", [
    (None, 1.866),      # unmeasured: the 2 mm lift
    (0.3, 0.866),       # measured: the 1 mm minimum wins over 0.5
])
def test_shipped_settings_never_send_a_low_touch_to_z3(ff_bed_mesh, measured,
                                                       travel_z):
    # The sweep's first point: a touch at -0.134 on a plate around Z 0.
    settings = shipped_section()
    if measured is not None:
        settings["release_distance"] = measured
    _, helper, printer = install(ff_bed_mesh, **settings)
    printer.toolhead.z = -0.134
    helper._raise_tool(False)
    assert helper.moves[0][0][2] == pytest.approx(travel_z)
