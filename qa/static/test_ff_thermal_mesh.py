"""The thermal mesh map: sweep order, convergence, point choice and matching."""

import importlib.util
import json
import sys
import types

import pytest

from lib.paths import ROOT


pytestmark = pytest.mark.static
MODULE = (ROOT / "pkgs" / "klipper" / "payload" / "klipper" /
          "klippy" / "extras" / "ff_thermal_mesh.py")
PKG = "ff_thermal_mesh_extras"

PARAMS = {"min_x": 0., "max_x": 200., "min_y": 0., "max_y": 200.,
          "x_count": 5, "y_count": 5, "mesh_x_pps": 2, "mesh_y_pps": 2,
          "algo": "lagrange", "tension": .2}


class ZrefMode:
    DISABLED = 0
    IN_MESH = 1
    PROBE = 2


class ZMesh:
    def __init__(self, params, name):
        self.params, self.name, self.matrix = params, name, None

    def build_mesh(self, matrix):
        self.matrix = matrix


class ProbePointsHelper:
    def __init__(self, config, finalize, default_points=None):
        self.finalize = finalize
        self.points = default_points
        self.z_at = None

    def use_xy_offsets(self, flag):
        pass

    def update_probe_points(self, points, min_points):
        self.points = points

    def start_probe(self, gcmd):
        # Probe z_offset 1.0: the trigger reads the bed height plus it.
        self.finalize((0., 0., 1.),
                      [[x, y, self.z_at(x, y) + 1.] for x, y in self.points])


@pytest.fixture(scope="module")
def tm():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = []
    sys.modules[PKG] = pkg
    sys.modules[PKG + ".bed_mesh"] = types.SimpleNamespace(
        ZrefMode=ZrefMode, ZMesh=ZMesh, BedMeshError=ValueError)
    sys.modules[PKG + ".probe"] = types.SimpleNamespace(
        ProbePointsHelper=ProbePointsHelper)
    spec = importlib.util.spec_from_file_location(PKG + ".ff_thermal_mesh",
                                                  MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A synthetic bed. The chamber bows the centre up, the bed tilts it along X,
# and both add a uniform rise, so each axis has its own region of the plate.
def bed_shape(chamber, bed, x, y):
    centre = 1. - ((x - 100.) ** 2 + (y - 100.) ** 2) / 20000.
    return (0.002 * chamber * centre + 0.001 * bed * (x / 200.)
            + 0.0001 * (chamber + bed))


def matrix_at(chamber, bed):
    return [[bed_shape(chamber, bed, c * 50., r * 50.) for c in range(5)]
            for r in range(5)]


def mesh_map(tm, chambers=(30., 40., 50.), beds=(60., 80., 100.)):
    return [{"chamber": c, "bed": b, "matrix": matrix_at(c, b)}
            for c, b in tm.sweep_plan(list(beds), list(chambers))]


def test_steps_always_end_on_the_maximum(tm):
    assert tm.temperature_steps(60, 100, 15) == [60., 75., 90., 100.]
    assert tm.temperature_steps(60, 60, 0) == [60.]
    with pytest.raises(ValueError):
        tm.temperature_steps(100, 60, 10)


def test_the_bed_snakes_up_and_down_as_the_chamber_climbs(tm):
    plan = tm.sweep_plan([60., 80., 100.], [30., 40., 50.])
    assert plan == [(30., 60.), (30., 80.), (30., 100.),
                    (40., 100.), (40., 80.), (40., 60.),
                    (50., 60.), (50., 80.), (50., 100.)]
    # Every step changes exactly one heater.
    for (c1, b1), (c2, b2) in zip(plan, plan[1:]):
        assert (c1 != c2) + (b1 != b2) == 1


def test_convergence_uses_the_mean_absolute_difference(tm):
    a = [[0., 0.], [0., 0.]]
    b = [[0.01, -0.01], [0.03, -0.03]]
    assert tm.mean_abs_delta(a, b) == pytest.approx(0.02)
    assert tm.average_matrices([a, b])[1][0] == pytest.approx(0.015)


def test_each_axis_finds_the_nodes_it_moves(tm):
    chamber_map, bed_map = tm.sensitivity_maps(mesh_map(tm))
    # The chamber bows the centre; the bed lifts the +X edge.
    flat = [(chamber_map[r][c], r, c) for r in range(5) for c in range(5)]
    assert max(flat)[1:] == (2, 2)
    flat = [(bed_map[r][c], r, c) for r in range(5) for c in range(5)]
    assert max(flat)[2] == 4
    assert chamber_map[2][2] == pytest.approx(0.0021)


def test_four_points_alternate_axes_and_keep_apart(tm):
    chamber_map, bed_map = tm.sensitivity_maps(mesh_map(tm))
    points = tm.select_points(PARAMS, chamber_map, bed_map, 2, 2, 60.)
    assert [p["axis"] for p in points] == ["chamber", "bed", "chamber", "bed"]
    assert (points[0]["x"], points[0]["y"]) == (100., 100.)
    assert points[1]["x"] == 200.
    for i, p in enumerate(points):
        for q in points[i + 1:]:
            assert ((p["x"] - q["x"]) ** 2 + (p["y"] - q["y"]) ** 2) ** .5 >= 60.


def test_a_single_chamber_target_gives_every_point_to_the_bed(tm):
    entries = mesh_map(tm, chambers=(0.,))
    chamber_map, bed_map = tm.sensitivity_maps(entries)
    assert chamber_map is None
    points = tm.select_points(PARAMS, chamber_map, bed_map, 2, 2, 60.)
    assert len(points) == 4 and {p["axis"] for p in points} == {"bed"}


def test_spacing_relaxes_rather_than_returning_too_few(tm):
    chamber_map, bed_map = tm.sensitivity_maps(mesh_map(tm))
    points = tm.select_points(PARAMS, chamber_map, bed_map, 2, 2, 1000.)
    assert len(points) == 4


def points_for(tm, entries):
    return tm.select_points(PARAMS, *tm.sensitivity_maps(entries))


def test_nearest_finds_the_sampled_pair(tm):
    entries = mesh_map(tm)
    points = points_for(tm, entries)
    measured = tm.signature(matrix_at(40., 80.), points)
    rms, best = tm.rank_entries(entries, points, measured)[0]
    assert (best["chamber"], best["bed"]) == (40., 80.)
    assert rms == pytest.approx(0.)
    weights = tm.idw_weights(tm.rank_entries(entries, points, measured))
    assert len(weights) == 1 and weights[0][1] is best


def test_bilinear_recovers_temperatures_between_the_samples(tm):
    entries = mesh_map(tm)
    points = points_for(tm, entries)
    measured = [bed_shape(45., 70., p["x"], p["y"]) for p in points]
    fit = tm.fit_bilinear(entries, points, measured)
    assert fit["chamber"] == pytest.approx(45., abs=0.6)
    assert fit["bed"] == pytest.approx(70., abs=0.6)
    matrix, residuals = tm.effective_mesh(fit["weights"], PARAMS, points,
                                          measured)
    # The synthetic bed is linear in both temperatures, so a bilinear blend
    # of the corners reproduces it everywhere, not only at the probe points.
    truth = matrix_at(45., 70.)
    for r in range(5):
        for c in range(5):
            assert matrix[r][c] == pytest.approx(truth[r][c], abs=2e-4)
    assert max(abs(v) for v in residuals) < 2e-4


def test_bilinear_works_along_one_axis(tm):
    entries = mesh_map(tm, chambers=(0.,))
    points = points_for(tm, entries)
    measured = [bed_shape(0., 90., p["x"], p["y"]) for p in points]
    fit = tm.fit_bilinear(entries, points, measured)
    assert fit["bed"] == pytest.approx(90., abs=0.6)
    assert fit["chamber"] == 0.


def test_temperature_lookup_clamps_to_the_map(tm):
    entries = mesh_map(tm)
    weights, chamber, bed = tm.bilinear_weights(entries, 45., 200.)
    assert (chamber, bed) == (45., 100.)
    assert sum(w for w, _ in weights) == pytest.approx(1.)
    assert {(e["chamber"], e["bed"]) for _, e in weights} == {
        (40., 100.), (50., 100.)}


def test_corrections_meet_the_probed_points(tm):
    points = [{"x": 0., "y": 0.}, {"x": 200., "y": 0.},
              {"x": 0., "y": 200.}, {"x": 200., "y": 200.}]
    residuals = [0.01, 0.03, 0.01, 0.03]
    plane = tm.correction_surface(points, residuals, "plane")
    assert plane(0., 100.) == pytest.approx(0.01)
    assert plane(200., 50.) == pytest.approx(0.03)
    offset = tm.correction_surface(points, residuals, "offset")
    assert offset(0., 0.) == pytest.approx(0.02)
    assert tm.correction_surface(points, residuals, "none")(0., 0.) == 0.


def test_relative_matching_ignores_a_uniform_shift(tm):
    entries = mesh_map(tm)
    points = points_for(tm, entries)
    measured = [z + 0.05 for z in tm.signature(matrix_at(50., 60.), points)]
    absolute = tm.rank_entries(entries, points, measured)[0]
    relative = tm.rank_entries(entries, points, measured, relative=True)[0]
    assert relative[0] == pytest.approx(0.)
    assert (relative[1]["chamber"], relative[1]["bed"]) == (50., 60.)
    assert absolute[0] > 0.


# -- the command, end to end against fakes ------------------------------

class Gcmd:
    error = RuntimeError

    def __init__(self, **params):
        self.params = params
        self.info = []

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, default=None, minval=None):
        return int(self.params.get(name, default))

    def get_float(self, name, default=None, minval=None, above=None):
        return float(self.params.get(name, default))

    def respond_info(self, msg):
        self.info.append(msg)


class Gcode:
    error = RuntimeError

    def __init__(self):
        self.commands, self.scripts = {}, []

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def run_script_from_command(self, script):
        self.scripts.append(script)

    run_script = run_script_from_command

    def respond_info(self, msg):
        pass


class Template:
    def __init__(self, script):
        self.script = script

    def render(self):
        return self.script


class Macro:
    def load_template(self, config, option, default=None):
        return Template(config.values.get(option, default))


class BedMeshObj:
    def __init__(self):
        self.mesh, self.saved = None, []
        self.bmc = types.SimpleNamespace(probe_mgr=types.SimpleNamespace(
            get_zero_ref_mode=lambda: ZrefMode.DISABLED,
            get_zero_ref_pos=lambda: None))

    def set_mesh(self, mesh):
        self.mesh = mesh

    def get_mesh(self):
        return self.mesh

    def save_profile(self, name):
        self.saved.append(name)


class Printer:
    def __init__(self):
        self.gcode = Gcode()
        self.bed_mesh = BedMeshObj()
        self.handlers = {}
        self.events = []
        self.toolhead = types.SimpleNamespace(
            wait_moves=lambda: self.events.append("wait_moves"))

    def get_reactor(self):
        return types.SimpleNamespace(
            monotonic=lambda: 0.,
            pause=lambda t: self.events.append("pause"))

    def lookup_object(self, name, default=None):
        return {"gcode": self.gcode, "bed_mesh": self.bed_mesh,
                "toolhead": self.toolhead}.get(name, default)

    def load_object(self, config, name):
        return Macro()

    def get_start_args(self):
        return {}

    def register_event_handler(self, name, func):
        self.handlers[name] = func


class Config:
    error = RuntimeError

    def __init__(self, printer, **values):
        self.printer, self.values = printer, values

    def get_printer(self):
        return self.printer

    def get(self, option, default=None, **limits):
        return self.values.get(option, default)

    getfloat = getint = get


def test_apply_probes_the_points_and_loads_the_blend(tm, tmp_path):
    entries = mesh_map(tm)
    points = points_for(tm, entries)
    path = tmp_path / "thermal_mesh.json"
    path.write_text(json.dumps({"version": 1, "mesh_params": PARAMS,
                                "entries": entries,
                                "analysis": {"points": points}}))
    printer = Printer()
    obj = tm.FFThermalMesh(Config(printer, data_file=str(path),
                                  before_sample_gcode="G28 Z"))
    printer.handlers["klippy:connect"]()
    obj.probe_helper.z_at = lambda x, y: bed_shape(50., 80., x, y)

    gcmd = Gcmd(METHOD="nearest", SAVE="hot")
    printer.gcode.commands["THERMAL_MESH_APPLY"](gcmd)

    assert printer.gcode.scripts[0] == "G28 Z"
    mesh = printer.bed_mesh.mesh
    assert mesh.name == "hot"
    assert mesh.matrix == matrix_at(50., 80.)
    assert printer.bed_mesh.saved == ["hot"]
    assert obj.last_apply["weights"] == [[1., 50., 80.]]


def test_apply_refuses_without_chosen_points(tm, tmp_path):
    path = tmp_path / "thermal_mesh.json"
    path.write_text(json.dumps({"version": 1, "mesh_params": PARAMS,
                                "entries": mesh_map(tm)}))
    printer = Printer()
    tm.FFThermalMesh(Config(printer, data_file=str(path)))
    printer.handlers["klippy:connect"]()
    with pytest.raises(RuntimeError, match="THERMAL_MESH_ANALYZE"):
        printer.gcode.commands["THERMAL_MESH_APPLY"](Gcmd())


class Clock:
    NOW = 0.

    def __init__(self):
        self.t = 0.

    def monotonic(self):
        return self.t

    def pause(self, waketime):
        self.t = max(self.t + .001, waketime)

    def register_callback(self, cb):
        cb(self.t)


class Heater:
    def __init__(self):
        self.target = 0.

    def get_status(self, eventtime):
        return {"temperature": self.target}


class SweepPrinter(Printer):
    """Heaters reach any target at once; each mesh probe settles over time."""

    def __init__(self):
        super().__init__()
        self.clock = Clock()
        self.heaters = {"heater_bed": Heater(), "chamber_heater": Heater()}
        self.probes = []
        self.gcode.run_script = self.run_script

    def get_reactor(self):
        return self.clock

    def is_shutdown(self):
        return False

    def lookup_object(self, name, default=None):
        objects = {
            "gcode": self.gcode, "bed_mesh": self.bed_mesh,
            "heater_bed": self.heaters["heater_bed"],
            "heater_generic chamber_heater": self.heaters["chamber_heater"],
            "toolhead": types.SimpleNamespace(
                get_status=lambda t: {"homed_axes": "xyz"}),
            "idle_timeout": types.SimpleNamespace(idle_timeout=600.)}
        return objects.get(name, default)

    def run_script(self, script):
        self.gcode.scripts.append(script)
        words = script.split()
        if words and words[0] == "SET_HEATER_TEMPERATURE":
            args = dict(w.split("=") for w in words[1:])
            self.heaters[args["HEATER"]].target = float(args["TARGET"])
        elif script == "BED_MESH_CALIBRATE":
            c = self.heaters["chamber_heater"].target
            b = self.heaters["heater_bed"].target
            key = (c, b)
            n = sum(1 for k in self.probes if k == key)
            self.probes.append(key)
            drift = 0.05 / (2 ** n)          # halves with every sample
            m = [[z + drift for z in row] for row in matrix_at(c, b)]
            self.bed_mesh.mesh = types.SimpleNamespace(
                get_probed_matrix=lambda: m,
                get_mesh_params=lambda: PARAMS)


def test_the_sweep_maps_every_pair_until_it_settles(tm, tmp_path):
    path = tmp_path / "thermal_mesh.json"
    printer = SweepPrinter()
    obj = tm.FFThermalMesh(Config(printer, data_file=str(path),
                                  max_samples=6, min_point_spacing=60.,
                                  bed_tolerance=1., chamber_tolerance=2.,
                                  heat_timeout=100.))
    printer.handlers["klippy:connect"]()
    printer.gcode.commands["THERMAL_MESH_SWEEP"](Gcmd(
        BED_MIN=60, BED_MAX=100, BED_STEP=20, CHAMBER_MIN=30, CHAMBER_MAX=50,
        CHAMBER_STEP=10, SOAK=300, CHAMBER_SOAK=900, THRESHOLD=0.01))

    data = json.loads(path.read_text())
    assert obj.status["state"] == "done", obj.status
    pairs = [(e["chamber"], e["bed"]) for e in data["entries"]]
    assert pairs == tm.sweep_plan([60., 80., 100.], [30., 40., 50.])
    # drift 0.05, 0.025, 0.0125, 0.00625: the 4th sample is the first within
    # 0.01 of the one before it.
    assert {e["samples"] for e in data["entries"]} == {4}
    assert all(e["converged"] for e in data["entries"])
    assert len(data["analysis"]["points"]) == 4
    # Heaters are off and the idle timeout is back afterwards.
    assert printer.heaters["heater_bed"].target == 0.
    assert printer.heaters["chamber_heater"].target == 0.
    assert printer.gcode.scripts[-1] == "SET_IDLE_TIMEOUT TIMEOUT=600"


def test_a_resumed_sweep_skips_the_stored_pairs(tm, tmp_path):
    path = tmp_path / "thermal_mesh.json"
    stored = mesh_map(tm, chambers=(30.,), beds=(60., 80.))
    path.write_text(json.dumps({"version": 1, "mesh_params": PARAMS,
                                "entries": stored}))
    printer = SweepPrinter()
    tm.FFThermalMesh(Config(printer, data_file=str(path), max_samples=6,
                            min_point_spacing=60., bed_tolerance=1.,
                            chamber_tolerance=2., heat_timeout=100.))
    printer.handlers["klippy:connect"]()
    printer.gcode.commands["THERMAL_MESH_SWEEP"](Gcmd(
        BED_MIN=60, BED_MAX=100, BED_STEP=20, CHAMBER_MIN=30, CHAMBER_MAX=30,
        SOAK=0, THRESHOLD=0.01, RESUME=1))

    assert set(printer.probes) == {(30., 100.)}
    assert len(json.loads(path.read_text())["entries"]) == 3


def test_the_point_probe_borrows_the_mesh_probing_travel(tm, tmp_path):
    attached = []

    class WithTravel(Printer):
        def lookup_object(self, name, default=None):
            if name == "ff_bed_mesh":
                return types.SimpleNamespace(attach=attached.append)
            return super().lookup_object(name, default)

    printer = WithTravel()
    obj = tm.FFThermalMesh(Config(printer,
                                  data_file=str(tmp_path / "m.json")))
    printer.handlers["klippy:connect"]()
    assert attached == [obj.probe_helper]


def test_bilinear_apply_empties_the_queue_then_yields_while_it_fits(
        tm, tmp_path):
    entries = mesh_map(tm)
    points = points_for(tm, entries)
    path = tmp_path / "thermal_mesh.json"
    path.write_text(json.dumps({"version": 1, "mesh_params": PARAMS,
                                "entries": entries,
                                "analysis": {"points": points}}))
    printer = Printer()
    obj = tm.FFThermalMesh(Config(printer, data_file=str(path)))
    printer.handlers["klippy:connect"]()
    obj.probe_helper.z_at = lambda x, y: bed_shape(45., 70., x, y)

    gcmd = Gcmd(DRY_RUN=1)
    printer.gcode.commands["THERMAL_MESH_APPLY"](gcmd)

    # Nothing queued while Python works, and the reactor runs once per
    # cell of the 3 x 3 map: four cells.
    assert printer.events[0] == "wait_moves"
    assert printer.events.count("pause") == 4
    assert "fit chamber 45.0 bed 70.0" in gcmd.info[-1]


def test_the_fit_is_exact_on_a_bilinear_bed(tm):
    entries = mesh_map(tm)
    points = points_for(tm, entries)
    for chamber, bed in ((31., 99.), (47.3, 64.1), (50., 60.)):
        measured = [bed_shape(chamber, bed, p["x"], p["y"]) for p in points]
        fit = tm.fit_bilinear(entries, points, measured)
        assert fit["chamber"] == pytest.approx(chamber, abs=0.05)
        assert fit["bed"] == pytest.approx(bed, abs=0.05)
        assert fit["rms"] < 1e-6
