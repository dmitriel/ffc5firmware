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


def test_the_sweep_runs_hottest_first_and_never_puts_the_bed_below_the_chamber(
        tm):
    plan = tm.sweep_plan([40., 60., 80.], [30., 50., 70.])
    assert plan == [(70., 80.),
                    (50., 80.), (50., 60.),
                    (30., 80.), (30., 60.), (30., 40.)]
    # Within a set the bed only ever goes down.
    for (c1, b1), (c2, b2) in zip(plan, plan[1:]):
        assert c2 < c1 or (c2 == c1 and b2 < b1)


def test_an_unheated_chamber_keeps_every_bed_target(tm):
    assert tm.sweep_plan([60., 80.], [0.]) == [(0., 80.), (0., 60.)]
    assert tm.sweep_plan([40.], [50.]) == []


def test_averaging_two_meshes(tm):
    a = [[0., 0.], [0., 0.]]
    b = [[0.01, -0.01], [0.03, -0.03]]
    assert tm.average_matrices([a, b])[1][0] == pytest.approx(0.015)


def test_f_quantiles_match_the_tables(tm):
    assert tm.f_quantile(.95, 1, 9) == pytest.approx(5.117, abs=1e-3)
    assert tm.f_quantile(.95, 5, 10) == pytest.approx(3.326, abs=1e-3)
    assert tm.f_quantile(.99, 10, 20) == pytest.approx(3.368, abs=1e-3)
    # t(9) at 97.5 % squared.
    assert tm.f_quantile(.95, 1, 9) == pytest.approx(2.2622 ** 2, abs=1e-3)


def noisy(sigma, rows=5, cols=5, seed=1):
    import random
    rnd = random.Random(seed)
    return [[rnd.gauss(0., sigma) for _ in range(cols)] for _ in range(rows)]


def test_two_probes_of_one_plate_verify_as_identical(tm):
    plate = matrix_at(40., 80.)
    a = [[z + e for z, e in zip(r, n)] for r, n in zip(plate, noisy(.003, seed=1))]
    b = [[z + e for z, e in zip(r, n)] for r, n in zip(plate, noisy(.003, seed=2))]
    check = tm.verify_meshes(a, b, 0.003, 9)
    assert check["identical"], check
    assert abs(check["mean"]) < check["mean_ci"]


def test_a_plate_that_moved_as_a_whole_fails_on_the_average(tm):
    # Every node 3 um higher: well inside the noise node by node, but 25
    # nodes average it out of the noise.
    a = noisy(.003, seed=1)
    b = [[z + .003 + e for z, e in zip(r, n)]
         for r, n in zip(a, noisy(.0001, seed=3))]
    check = tm.verify_meshes(a, b, 0.003, 9)
    assert check["bias"] > check["bias_limit"]
    assert check["shape"] <= check["shape_limit"]
    assert not check["identical"]


def test_one_node_that_moved_fails_on_the_shape(tm):
    a = noisy(.002, seed=1)
    b = [list(r) for r in a]
    b[2][2] += 0.03
    b[2][3] -= 0.03          # the average stays at 0
    check = tm.verify_meshes(a, b, 0.002, 9)
    assert check["mean"] == pytest.approx(0., abs=1e-12)
    assert check["bias"] <= check["bias_limit"]
    assert check["shape"] > check["shape_limit"]
    assert not check["identical"]


def test_a_perfect_probe_needs_an_exact_match(tm):
    a = matrix_at(40., 80.)
    assert tm.verify_meshes(a, a, 0., 9)["identical"]
    b = [[z + 1e-6 for z in r] for r in a]
    assert not tm.verify_meshes(a, b, 0., 9)["identical"]


def test_precision_is_the_sample_standard_deviation(tm):
    sigma, dof = tm.sample_std([1., 2., 3., 4.])
    assert dof == 3 and sigma == pytest.approx((5. / 3.) ** .5)


def test_each_axis_finds_the_nodes_it_moves(tm):
    chamber_map, bed_map = tm.sensitivity_maps(mesh_map(tm))
    # The chamber bows the centre; the bed lifts the +X edge.
    flat = [(chamber_map[r][c], r, c) for r in range(5) for c in range(5)]
    assert max(flat)[1:] == (2, 2)
    flat = [(bed_map[r][c], r, c) for r in range(5) for c in range(5)]
    assert max(flat)[2] == 4
    assert chamber_map[2][2] == pytest.approx(0.0021)


def test_four_points_keep_apart_and_pin_down_both_temperatures(tm):
    model = tm.thermal_model(mesh_map(tm))
    points = tm.select_points(PARAMS, model, 4, 60.)
    assert len(points) == 4
    assert {p["axis"] for p in points} == {"chamber", "bed"}
    for i, p in enumerate(points):
        for q in points[i + 1:]:
            assert ((p["x"] - q["x"]) ** 2 + (p["y"] - q["y"]) ** 2) ** .5 >= 60.
    # The bed tilts toward +X while the chamber bows the middle: the points
    # see the two as clearly different shapes.
    assert tm.point_separation(model, points) < 0.9


def test_a_single_chamber_target_gives_every_point_to_the_bed(tm):
    entries = mesh_map(tm, chambers=(0.,))
    chamber_map, bed_map = tm.sensitivity_maps(entries)
    assert chamber_map is None
    model = tm.thermal_model(entries)
    points = tm.select_points(PARAMS, model, 4, 60.)
    assert len(points) == 4 and {p["axis"] for p in points} == {"bed"}
    assert tm.point_separation(model, points) is None


def test_spacing_relaxes_rather_than_returning_too_few(tm):
    model = tm.thermal_model(mesh_map(tm))
    assert len(tm.select_points(PARAMS, model, 4, 1000.)) == 4


# A plate where the bed and the chamber both bow the middle, and differ only
# in that the chamber also lifts the -Y edge a little.
def twin_shape(chamber, bed, x, y):
    bow = 1. - ((x - 100.) ** 2 + (y - 100.) ** 2) / 20000.
    return 0.002 * bed * bow + 0.002 * chamber * bow + \
        0.0005 * chamber * (1. - y / 200.)


def twin_map(chambers=(30., 40., 50.), beds=(60., 80., 100.)):
    return [{"chamber": c, "bed": b,
             "matrix": [[twin_shape(c, b, col * 50., r * 50.)
                         for col in range(5)] for r in range(5)]}
            for c in chambers for b in beds]


def test_points_are_chosen_to_separate_heaters_that_bow_alike(tm):
    model = tm.thermal_model(twin_map())
    chosen = tm.select_points(PARAMS, model, 4, 60.)
    # The most sensitive nodes for each heater are all near the middle,
    # where the two look the same; picking by sensitivity alone cannot
    # tell a hot bed from a warm chamber.
    by_sens = []
    for axis in ("chamber", "bed", "chamber", "bed"):
        ranked = sorted(((abs(model[axis][r][c]), r, c) for r in range(5)
                         for c in range(5)), reverse=True)
        for _, r, c in ranked:
            x, y = tm.node_xy(PARAMS, r, c)
            if all(((x - p["x"]) ** 2 + (y - p["y"]) ** 2) ** .5 >= 60.
                   for p in by_sens):
                by_sens.append({"x": x, "y": y, "row": r, "col": c})
                break
    assert tm.point_separation(model, chosen) < \
        tm.point_separation(model, by_sens)
    # And with them the fit recovers both temperatures.
    measured = [twin_shape(42., 75., p["x"], p["y"]) for p in chosen]
    bed, chamber = tm.fit_model(model, chosen, measured)
    assert (bed, chamber) == (pytest.approx(75., abs=0.01),
                              pytest.approx(42., abs=0.01))


def test_the_model_uses_measured_temperatures_to_decouple_the_heaters(tm):
    # A hot bed warms the chamber: the chamber read 0.2 degC above its
    # target for every degree of bed above 60. The warp follows the real
    # temperatures. By targets alone, the chamber's share would be blamed
    # on the bed.
    entries = []
    for c in (30., 40., 50.):
        for b in (60., 80., 100.):
            real_c = c + 0.2 * (b - 60.)
            entries.append({
                "chamber": c, "bed": b, "bed_measured": [b, b],
                "chamber_measured": [real_c - .1, real_c + .1],
                "matrix": matrix_at(real_c, b)})
    model = tm.thermal_model(entries)
    truth_c = [[(matrix_at(1., 0.)[r][c] - matrix_at(0., 0.)[r][c])
                for c in range(5)] for r in range(5)]
    truth_b = [[(matrix_at(0., 1.)[r][c] - matrix_at(0., 0.)[r][c])
                for c in range(5)] for r in range(5)]
    for r in range(5):
        for c in range(5):
            assert model["chamber"][r][c] == pytest.approx(truth_c[r][c])
            assert model["bed"][r][c] == pytest.approx(truth_b[r][c])
    assert model["fit_rms"] == pytest.approx(0., abs=1e-9)


def test_heaters_that_moved_in_lockstep_cannot_be_separated(tm):
    entries = [{"chamber": c, "bed": c + 30., "matrix": matrix_at(c, c + 30.)}
               for c in (30., 40., 50.)]
    with pytest.raises(ValueError, match="cannot separate"):
        tm.thermal_model(entries)


def test_the_model_fits_a_triangular_map_up_to_its_diagonal(tm):
    # Bed never below chamber: no full cell touches (60, 60), but the model
    # does not need one.
    beds, chambers = [40., 50., 60.], [30., 45., 60.]
    entries = [{"chamber": c, "bed": b, "matrix": matrix_at(c, b)}
               for c, b in tm.sweep_plan(beds, chambers)]
    assert (60., 40.) not in {(e["chamber"], e["bed"]) for e in entries}
    model = tm.thermal_model(entries)
    points = tm.select_points(PARAMS, model)
    measured = [bed_shape(57., 58., p["x"], p["y"]) for p in points]
    bed, chamber = tm.fit_model(model, points, measured)
    assert (bed, chamber) == (pytest.approx(58., abs=0.01),
                              pytest.approx(57., abs=0.01))
    matrix, weights = tm.model_mesh(model, entries, bed, chamber)
    truth = matrix_at(57., 58.)
    for r in range(5):
        for c in range(5):
            assert matrix[r][c] == pytest.approx(truth[r][c], abs=1e-6)


def test_the_model_mesh_keeps_what_the_straight_line_misses(tm):
    # A bed whose bow grows with the square of the bed temperature.
    def curved(chamber, bed):
        return [[v + 1e-5 * (bed - 60.) ** 2 * (r == 2 and c == 2)
                 for c, v in enumerate(row)]
                for r, row in enumerate(matrix_at(chamber, bed))]
    entries = [{"chamber": c, "bed": b, "matrix": curved(c, b)}
               for c in (30., 40., 50.) for b in (60., 80., 100.)]
    model = tm.thermal_model(entries)
    assert model["fit_rms"] > 0.
    # At a sampled state the departures bring the exact entry back.
    matrix, weights = tm.model_mesh(model, entries, 80., 40.)
    assert len(weights) == 1
    assert matrix[2][2] == pytest.approx(curved(40., 80.)[2][2])


def points_for(tm, entries):
    return tm.select_points(PARAMS, tm.thermal_model(entries))


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

    def get_float(self, name, default=None, minval=None, maxval=None,
                  above=None):
        value = self.params.get(name, default)
        return None if value is None else float(value)

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


def repeatable_probe(sigma=0.002):
    """Touches alternate +sigma, -sigma about the plate."""
    count = []

    def z_at(x, y):
        count.append(1)
        return sigma if len(count) % 2 else -sigma
    return z_at


class SweepPrinter(Printer):
    """Heaters reach any target at once; each mesh probe settles over time."""

    def __init__(self):
        super().__init__()
        self.clock = Clock()
        self.heaters = {"heater_bed": Heater(), "chamber_heater": Heater()}
        self.probes = []
        self.ratio, self.mesh_seconds = 4., 100.
        self.drift = 0.05                     # mm, first mesh vs settled
        # Each mesh is homed (G28 Z on the plate) before it is probed, so
        # the homing touch's error shifts all of its nodes alike. Cycled.
        self.home_noise = [0.]
        self.extra_objects = {}
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
        objects.update(self.extra_objects)
        return objects.get(name, default)

    def run_script(self, script):
        self.gcode.scripts.append(script)
        words = script.split()
        if words and words[0] == "SET_HEATER_TEMPERATURE":
            args = dict(w.split("=") for w in words[1:])
            self.heaters[args["HEATER"]].target = float(args["TARGET"])
        elif words and words[0] == "_THERMAL_MESH_PRECISION":
            args = dict(w.split("=") for w in words[1:])
            self.gcode.commands[words[0]](Gcmd(**args))
        elif script == "BED_MESH_CALIBRATE":
            c = self.heaters["chamber_heater"].target
            b = self.heaters["heater_bed"].target
            key = (c, b)
            n = sum(1 for k in self.probes if k == key)
            self.probes.append(key)
            drift = self.drift / (self.ratio ** n)  # quarters each sample
            home = self.home_noise[(len(self.probes) - 1)
                                   % len(self.home_noise)]
            self.clock.t += self.mesh_seconds
            # The plate settles by changing SHAPE: a uniform rise is homed
            # away by the G28 Z before every mesh, on the printer as here.
            m = [[z + drift * (1. if (r + k) % 2 else -1.) + home
                  for k, z in enumerate(row)]
                 for r, row in enumerate(matrix_at(c, b))]
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
    obj.probe_helper.z_at = repeatable_probe()
    printer.gcode.commands["THERMAL_MESH_SWEEP"](Gcmd(
        BED_MIN=60, BED_MAX=100, BED_STEP=20, CHAMBER_MIN=30, CHAMBER_MAX=50,
        CHAMBER_STEP=10, SOAK=300, CHAMBER_SOAK=900))

    data = json.loads(path.read_text())
    assert obj.status["state"] == "done", obj.status
    pairs = [(e["chamber"], e["bed"]) for e in data["entries"]]
    assert pairs == tm.sweep_plan([60., 80., 100.], [30., 40., 50.])
    # Each sample's nodes move 0.0375, 0.0094, 0.0023 mm (half of them up,
    # half down) from the one before. Against a probe sigma of 0.0021 a
    # 0.0094 mm change in shape is real; 0.0023 mm at every node is not.
    assert {e["samples"] for e in data["entries"]} == {4}
    assert all(e["converged"] for e in data["entries"])
    check = data["entries"][0]["verification"]
    # Four readings at each of nine nodes: 9 x 3 degrees of freedom. The
    # stub's readings alternate +/-0.002, so each node's four are +-+- .
    assert check["identical"] and check["dof"] == 27
    assert check["sigma"] == pytest.approx(0.002 * (4 / 3) ** .5)
    # One precision measurement after each verification mesh.
    assert printer.gcode.scripts.count("_THERMAL_MESH_PRECISION") == 9 * 3
    assert len(data["analysis"]["points"]) == 4
    # Heaters are off and the idle timeout is back afterwards.
    assert printer.heaters["heater_bed"].target == 0.
    assert printer.heaters["chamber_heater"].target == 0.
    assert printer.gcode.scripts[-1] == "SET_IDLE_TIMEOUT TIMEOUT=600"


def _sweep(tm, tmp_path, printer=None, max_samples=6, _hook=None,
           **params):
    printer = printer or SweepPrinter()
    tmp_path.mkdir(parents=True, exist_ok=True)
    obj = tm.FFThermalMesh(Config(printer,
                                  data_file=str(tmp_path / "thermal_mesh.json"),
                                  max_samples=max_samples,
                                  min_point_spacing=60.,
                                  bed_tolerance=1., chamber_tolerance=2.,
                                  heat_timeout=100., cool_timeout=3600.))
    printer.handlers["klippy:connect"]()
    obj.probe_helper.z_at = repeatable_probe()
    soaks = []
    sleep = obj._sleep
    obj._sleep = lambda seconds: soaks.append(seconds) or sleep(seconds)
    if _hook is not None:
        _hook(obj)
    printer.gcode.commands["THERMAL_MESH_SWEEP"](Gcmd(**params))
    data = json.loads((tmp_path / "thermal_mesh.json").read_text())
    return obj, printer, data, soaks


def test_the_chamber_soaks_once_and_each_set_starts_with_a_long_bed_soak(
        tm, tmp_path):
    obj, printer, data, soaks = _sweep(
        tm, tmp_path, BED_MIN=60, BED_MAX=100, BED_STEP=20, CHAMBER_MIN=30,
        CHAMBER_MAX=50, CHAMBER_STEP=10, SOAK=300, MAX_SOAK=1200,
        CHAMBER_SOAK=3000)
    assert obj.status["state"] == "done", obj.status
    assert [(e["chamber"], e["bed"]) for e in data["entries"]] == [
        (50., 100.), (50., 80.), (50., 60.),
        (40., 100.), (40., 80.), (40., 60.),
        (30., 100.), (30., 80.), (30., 60.)]
    assert soaks == [3000., 300., 300.,
                     1200., 300., 300.,
                     1200., 300., 300.]
    # Verified on meshes 3 and 4; two 100 s meshes went before the 3rd.
    assert [e["soak_time"] for e in data["entries"]] == [
        s + 200. for s in soaks]


def test_without_a_limit_it_verifies_for_as_long_as_it_takes(tm, tmp_path):
    printer = SweepPrinter()
    printer.ratio, printer.drift = 1.5, 0.5   # a plate that settles slowly
    obj, printer, data, soaks = _sweep(
        tm, tmp_path, printer, max_samples=0, BED_MIN=60, BED_MAX=60,
        SOAK=0, MAX_SOAK=0, CHAMBER_SOAK=600)
    (entry,) = data["entries"]
    assert entry["converged"] and entry["samples"] > 6
    assert entry["soak_time"] == 600. + 100. * (entry["samples"] - 2)


def test_max_samples_of_one_is_refused(tm, tmp_path):
    with pytest.raises(RuntimeError, match="max_samples"):
        tm.FFThermalMesh(Config(SweepPrinter(), max_samples=1,
                                data_file=str(tmp_path / "m.json")))


class SlowChamber(Heater):
    """Heats at once but cannot fall below `floor` (a warm enclosure)."""

    def __init__(self, floor):
        super().__init__()
        self.floor = floor

    def get_status(self, eventtime):
        return {"temperature": max(self.target, self.floor)}


def test_a_chamber_that_will_not_cool_skips_its_set(tm, tmp_path):
    printer = SweepPrinter()
    printer.heaters["chamber_heater"] = SlowChamber(floor=45.)
    obj, printer, data, soaks = _sweep(
        tm, tmp_path, printer, BED_MIN=60, BED_MAX=80, BED_STEP=20,
        CHAMBER_MIN=30, CHAMBER_MAX=50, CHAMBER_STEP=10, SOAK=0,
        MAX_SOAK=0, CHAMBER_SOAK=0, COOL_TIMEOUT=600)
    assert obj.status["state"] == "done", obj.status
    assert {e["chamber"] for e in data["entries"]} == {50.}
    assert [(k["chamber"], k["bed"]) for k in data["skipped"]] == [
        (40., None), (30., None)]
    assert "did not cool" in data["skipped"][0]["reason"]
    # The bed was off while it tried.
    assert "SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=0.0" in \
        printer.gcode.scripts


def test_a_bed_target_below_the_measured_chamber_is_skipped(tm, tmp_path):
    # Unheated chamber (target 0) that the enclosure holds at 65.
    printer = SweepPrinter()
    printer.heaters["chamber_heater"] = SlowChamber(floor=65.)
    obj, printer, data, soaks = _sweep(
        tm, tmp_path, printer, BED_MIN=60, BED_MAX=100, BED_STEP=20,
        SOAK=0, MAX_SOAK=0, CHAMBER_SOAK=0)
    assert [e["bed"] for e in data["entries"]] == [100., 80.]
    assert [(k["chamber"], k["bed"]) for k in data["skipped"]] == [(0., 60.)]


def test_a_resumed_sweep_skips_the_stored_pairs(tm, tmp_path):
    path = tmp_path / "thermal_mesh.json"
    stored = mesh_map(tm, chambers=(30.,), beds=(60., 80.))
    path.write_text(json.dumps({"version": 1, "mesh_params": PARAMS,
                                "entries": stored}))
    printer = SweepPrinter()
    obj = tm.FFThermalMesh(Config(printer, data_file=str(path),
                                  max_samples=6, min_point_spacing=60.,
                                  bed_tolerance=1., chamber_tolerance=2.,
                                  heat_timeout=100.))
    printer.handlers["klippy:connect"]()
    obj.probe_helper.z_at = repeatable_probe()
    printer.gcode.commands["THERMAL_MESH_SWEEP"](Gcmd(
        BED_MIN=60, BED_MAX=100, BED_STEP=20, CHAMBER_MIN=30, CHAMBER_MAX=30,
        SOAK=0, RESUME=1))

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

    gcmd = Gcmd(METHOD="bilinear", DRY_RUN=1)
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


def test_a_print_without_a_map_falls_back_to_the_saved_mesh(tm, tmp_path):
    printer = Printer()
    tm.FFThermalMesh(Config(printer, data_file=str(tmp_path / "none.json")))
    printer.handlers["klippy:connect"]()
    gcmd = Gcmd(FALLBACK="MESH_DATA", PREPARE=0)
    printer.gcode.commands["THERMAL_MESH_APPLY"](gcmd)
    assert printer.gcode.scripts == ["BED_MESH_PROFILE LOAD=MESH_DATA"]
    assert "loading MESH_DATA instead" in gcmd.info[0]


def test_prepare_0_skips_the_park_and_homing(tm, tmp_path):
    entries = mesh_map(tm)
    points = points_for(tm, entries)
    path = tmp_path / "thermal_mesh.json"
    path.write_text(json.dumps({"version": 1, "mesh_params": PARAMS,
                                "entries": entries,
                                "analysis": {"points": points}}))
    printer = Printer()
    obj = tm.FFThermalMesh(Config(printer, data_file=str(path),
                                  before_sample_gcode="G28 Z",
                                  after_sample_gcode="G1 Z10"))
    printer.handlers["klippy:connect"]()
    obj.probe_helper.z_at = lambda x, y: bed_shape(50., 80., x, y)
    printer.gcode.commands["THERMAL_MESH_APPLY"](
        Gcmd(METHOD="nearest", PREPARE=0))
    assert "G28 Z" not in printer.gcode.scripts
    assert printer.gcode.scripts == ["G1 Z10"]



def _map_printer(tm, tmp_path, z_at, with_points=True):
    entries = mesh_map(tm)
    data = {"version": 1, "mesh_params": PARAMS, "entries": entries}
    if with_points:
        data["analysis"] = {"points": points_for(tm, entries)}
    path = tmp_path / "thermal_mesh.json"
    path.write_text(json.dumps(data))
    printer = Printer()
    obj = tm.FFThermalMesh(Config(printer, data_file=str(path)))
    printer.handlers["klippy:connect"]()
    obj.probe_helper.z_at = z_at
    return obj, printer


def test_a_match_within_max_rms_is_accepted_and_loaded(tm, tmp_path):
    obj, printer = _map_printer(tm, tmp_path,
                                lambda x, y: bed_shape(45., 70., x, y))
    printer.gcode.commands["THERMAL_MESH_APPLY"](
        Gcmd(MAX_RMS=0.05, FALLBACK="NONE", PREPARE=0))
    assert obj.last_apply["accepted"] is True
    assert obj.last_apply["rms"] < 0.001
    assert printer.bed_mesh.mesh is not None


def test_a_match_beyond_max_rms_loads_nothing_and_says_why(tm, tmp_path):
    # A plate 0.2 mm off every map entry: the fit cannot reach it.
    obj, printer = _map_printer(
        tm, tmp_path, lambda x, y: bed_shape(45., 70., x, y) + 0.2 * (x > 100))
    gcmd = Gcmd(MAX_RMS=0.05, FALLBACK="NONE", PREPARE=0)
    printer.gcode.commands["THERMAL_MESH_APPLY"](gcmd)
    assert obj.last_apply["accepted"] is False
    assert "MAX_RMS" in obj.last_apply["reason"]
    assert printer.bed_mesh.mesh is None
    assert any("thermal mesh not used" in m for m in gcmd.info)


def test_without_a_fallback_a_bad_match_is_an_error(tm, tmp_path):
    obj, printer = _map_printer(
        tm, tmp_path, lambda x, y: bed_shape(45., 70., x, y) + 0.2 * (x > 100))
    with pytest.raises(RuntimeError, match="MAX_RMS"):
        printer.gcode.commands["THERMAL_MESH_APPLY"](
            Gcmd(MAX_RMS=0.05, PREPARE=0))


def test_no_map_with_fallback_none_is_recorded_not_raised(tm, tmp_path):
    printer = Printer()
    obj = tm.FFThermalMesh(Config(printer, data_file=str(tmp_path / "x.json")))
    printer.handlers["klippy:connect"]()
    printer.gcode.commands["THERMAL_MESH_APPLY"](
        Gcmd(FALLBACK="NONE", PREPARE=0))
    assert obj.last_apply["accepted"] is False
    assert "no map" in obj.last_apply["reason"]
    assert printer.gcode.scripts == []



def test_reuse_probe_takes_the_points_just_probed(tm, tmp_path):
    probes = []
    # A plate the map never saw: auto refuses it on rms ...
    obj, printer = _map_printer(
        tm, tmp_path, lambda x, y: probes.append((x, y)) or
        bed_shape(45., 70., x, y) + 0.2 * (x > 100))
    apply = printer.gcode.commands["THERMAL_MESH_APPLY"]
    apply(Gcmd(MAX_RMS=0.05, FALLBACK="NONE", PREPARE=0))
    assert obj.last_apply["accepted"] is False
    # ... and adaptive's fallback, with no limit, takes it without probing.
    touched = len(probes)
    gcmd = Gcmd(FALLBACK="NONE", PREPARE=0, REUSE_PROBE=1)
    apply(gcmd)
    assert len(probes) == touched                    # no new touches
    assert obj.last_apply["accepted"] is True
    assert "reusing the points probed" in gcmd.info[-1]


def test_without_reuse_probe_the_points_are_probed_again(tm, tmp_path):
    probes = []
    obj, printer = _map_printer(
        tm, tmp_path,
        lambda x, y: probes.append((x, y)) or bed_shape(45., 70., x, y))
    apply = printer.gcode.commands["THERMAL_MESH_APPLY"]
    apply(Gcmd(FALLBACK="NONE", PREPARE=0))
    touched = len(probes)
    apply(Gcmd(FALLBACK="NONE", PREPARE=0))
    assert len(probes) == 2 * touched


def test_a_probe_noisier_than_max_sigma_never_verifies(tm, tmp_path):
    # With a sloppy enough probe any two meshes look alike; MAX_SIGMA stops
    # that passing for a verification.
    obj, printer, data, soaks = _sweep(
        tm, tmp_path, BED_MIN=60, BED_MAX=60, SOAK=0, MAX_SOAK=0,
        CHAMBER_SOAK=0, MAX_SIGMA=0.001)
    (entry,) = data["entries"]
    assert entry["converged"] is False and entry["samples"] == 6
    assert entry["verification"]["sigma_ok"] is False


def _twice(rnd, sigma, n=25, shift=0.):
    """One plate probed twice, and sigma measured from ten touches."""
    plate = [rnd.uniform(-.2, .2) for _ in range(n)]
    ref = [rnd.gauss(0., sigma), rnd.gauss(0., sigma) + shift]
    a = [[z + rnd.gauss(0., sigma) - ref[0] for z in plate]]
    b = [[z + rnd.gauss(0., sigma) - ref[1] for z in plate]]
    touches = [rnd.gauss(0., sigma) for _ in range(10)]
    return a, b, touches


def test_identical_plates_are_rejected_about_one_time_in_twenty(tm):
    import random
    rnd = random.Random(7)
    trials, rejected = 2000, 0
    for _ in range(trials):
        a, b, touches = _twice(rnd, 0.003)
        sigma, dof = tm.sample_std(touches)
        rejected += not tm.verify_meshes(a, b, sigma, dof, 0.95,
                                         shared_reference=True)["identical"]
    assert 0.035 < rejected / float(trials) < 0.065


def test_a_shared_reference_touch_widens_the_average_test(tm):
    a = [[0.] * 5 for _ in range(5)]
    b = [[0.004] * 5 for _ in range(5)]     # one reference touch 4 um off
    assert not tm.verify_meshes(a, b, 0.003, 9)["identical"]
    assert tm.verify_meshes(a, b, 0.003, 9,
                            shared_reference=True)["identical"]


def test_a_chamber_soak_at_every_chamber_step_is_optional(tm, tmp_path):
    params = dict(BED_MIN=60, BED_MAX=100, BED_STEP=20, CHAMBER_MIN=30,
                  CHAMBER_MAX=40, CHAMBER_STEP=10, SOAK=300, MAX_SOAK=1200,
                  CHAMBER_SOAK=3000)
    _, _, _, soaks = _sweep(tm, tmp_path / "off", **params)
    assert soaks == [3000., 300., 300., 1200., 300., 300.]
    _, _, _, soaks = _sweep(tm, tmp_path / "on", CHAMBER_STEP_SOAK=2400,
                            **params)
    assert soaks == [3000., 300., 300., 2400., 300., 300.]


def test_homing_noise_does_not_stop_a_settled_plate_verifying(tm, tmp_path):
    # Every mesh is homed on the plate first, and each homing touch lands a
    # little differently: here +/-3 um, against a probe sigma of ~2 um. That
    # moves every node of a mesh alike. Judged as if each node were
    # independent, the average error never passed and the sweep probed on
    # forever; it has to be judged as the one shared touch it is.
    printer = SweepPrinter()
    printer.home_noise = [0.003, -0.003]
    obj, printer, data, _ = _sweep(
        tm, tmp_path, printer=printer, max_samples=8, BED_MIN=60, BED_MAX=60,
        SOAK=0, MAX_SOAK=0, CHAMBER_SOAK=0)
    (entry,) = data["entries"]
    assert entry["converged"] is True, entry["verification"]
    assert entry["verification"]["shared_reference"] is True
    assert entry["samples"] < 8


def test_pooled_precision_ignores_how_high_each_point_is(tm):
    sigma, dof = tm.pooled_std([[1.0, 1.002, 0.998], [5.0, 5.002, 4.998]])
    assert dof == 4
    assert sigma == pytest.approx(0.002)
    with pytest.raises(ValueError):
        tm.pooled_std([[1.0], [2.0]])


def test_precision_nodes_spread_over_the_mesh(tm):
    params = dict(PARAMS, x_count=10, y_count=10, min_x=13., max_x=263.,
                  min_y=4., max_y=254.)
    nodes = tm.precision_nodes(params)
    assert len(nodes) == 9
    assert {x for x, _ in nodes} == {13., 263., 13. + 4 * 250. / 9}
    assert {y for _, y in nodes} == {4., 254., 4. + 4 * 250. / 9}


def test_precision_reads_like_a_mesh_node_round_the_mesh(tm, tmp_path):
    # The probe takes SAMPLES (and its other settings) off the command it
    # is handed. The precision command used to pass SAMPLES=10, so every
    # reading was the median of ten touches -- and all of them at the
    # middle, the bed's quietest spot. It must pass nothing, and read nine
    # nodes in turn.
    printer = SweepPrinter()
    seen = []
    obj, printer, data, _ = _sweep(
        tm, tmp_path, printer=printer, BED_MIN=60, BED_MAX=60, SOAK=0,
        MAX_SOAK=0, CHAMBER_SOAK=0,
        _hook=lambda obj: _record_probes(obj, seen))
    precision = [(params, points) for params, points in seen
                 if len(points) == 9 * 4]
    assert precision, seen
    for params, points in precision:
        assert params == {}
        assert points[:9] == points[9:18] == points[18:27]
        assert len(set(points[:9])) == 9


def _record_probes(obj, seen):
    start = obj.probe_helper.start_probe

    def recording(gcmd):
        seen.append((dict(gcmd.params), list(obj.probe_helper.points)))
        start(gcmd)
    obj.probe_helper.start_probe = recording


def test_saving_the_map_never_holds_up_the_reactor(tm, tmp_path,
                                                    monkeypatch):
    # The map is rewritten whole after every pair, and late in a sweep that
    # took long enough on the printer to stop klippy servicing the MCU
    # ("Timer too close"). The write runs in a thread: _save returns at once
    # even while the encode is stuck, and a read waits for it to land.
    import threading
    printer = SweepPrinter()
    obj = tm.FFThermalMesh(Config(printer,
                                  data_file=str(tmp_path / "map.json")))
    gate, started = threading.Event(), threading.Event()
    real_dump = tm.json.dump

    def slow_dump(data, f, **kw):
        started.set()
        assert gate.wait(5)
        real_dump(data, f, **kw)
    monkeypatch.setattr(tm.json, "dump", slow_dump)
    data = {"version": tm.DATA_VERSION, "mesh_params": None, "entries": []}
    obj._save(data)
    assert started.wait(5)
    data["entries"].append({"bed": 60.})     # the sweep carries on
    obj._save(data)                          # still returns at once
    assert not (tmp_path / "map.json").exists()
    gate.set()
    loaded = obj._load()                     # waits for the writer
    assert loaded["entries"] == [{"bed": 60.}]
    assert obj._save_thread is None


class BedHeldByChamber(Heater):
    """A bed with no cooling: it cannot read below the chamber's air."""

    def __init__(self, chamber):
        super().__init__()
        self.chamber = chamber

    def get_status(self, eventtime):
        air = self.chamber.get_status(eventtime)["temperature"]
        return {"temperature": max(self.target, air)}


@pytest.mark.parametrize("air", [60.0, 60.1, 60.8])
def test_a_chamber_a_little_over_the_bed_target_does_not_skip_it(
        tm, tmp_path, air):
    # Bed 60 with the chamber at 60.0-60.8: a hot bed carried the chamber
    # a hair past its target. These used to be skipped as "below the
    # chamber"; they run, and the bed the chamber holds a little warm is
    # accepted.
    printer = SweepPrinter()
    chamber = SlowChamber(floor=air)
    printer.heaters["chamber_heater"] = chamber
    printer.heaters["heater_bed"] = BedHeldByChamber(chamber)
    obj, printer, data, _ = _sweep(
        tm, tmp_path, printer, BED_MIN=60, BED_MAX=60, SOAK=0, MAX_SOAK=0,
        CHAMBER_SOAK=0)
    assert obj.status["state"] == "done", obj.status
    assert [e["bed"] for e in data["entries"]] == [60.]
    assert not data.get("skipped")
    assert data["entries"][0]["bed_measured"][0] == pytest.approx(air)


def test_a_chamber_well_over_the_bed_target_still_skips_it(tm, tmp_path):
    printer = SweepPrinter()
    printer.heaters["chamber_heater"] = SlowChamber(floor=61.5)
    obj, printer, data, _ = _sweep(
        tm, tmp_path, printer, BED_MIN=60, BED_MAX=60, SOAK=0, MAX_SOAK=0,
        CHAMBER_SOAK=0)
    assert [(k["chamber"], k["bed"]) for k in data["skipped"]] == [(0., 60.)]


class CoolingChamber:
    """Reaches a higher target at once, but after a lower one falls only
    `rate` degC/s from where it was -- down to `floor` at the least."""

    def __init__(self, clock, rate, floor=0.):
        self.clock, self.rate, self.floor = clock, rate, floor
        self._target, self._from, self._since = 0., 0., 0.

    def _temp(self, t):
        cooled = self._from - self.rate * (t - self._since)
        return max(self._target, self.floor, cooled)

    @property
    def target(self):
        return self._target

    @target.setter
    def target(self, value):
        now = self.clock.t
        self._from, self._since = self._temp(now), now
        self._target = value

    def get_status(self, eventtime):
        return {"temperature": self._temp(self.clock.t)}


def _bed_equals_chamber_sweep(tm, tmp_path, floor=0.):
    # 65/65, then the 60 set: the chamber comes down from 65 to the set
    # (60 + chamber_tolerance = 62).
    printer = SweepPrinter()
    chamber = CoolingChamber(printer.clock, rate=0.002, floor=floor)
    printer.heaters["chamber_heater"] = chamber
    printer.heaters["heater_bed"] = BedHeldByChamber(chamber)
    return _sweep(tm, tmp_path, printer, BED_MIN=60, BED_MAX=65, BED_STEP=5,
                  CHAMBER_MIN=60, CHAMBER_MAX=65, CHAMBER_STEP=5, SOAK=0,
                  MAX_SOAK=0, CHAMBER_SOAK=0)


def test_a_chamber_still_too_warm_for_the_bed_skips_the_pair(tm, tmp_path):
    # No second wait for the bed == chamber pair: one cool-down per set.
    obj, printer, data, _ = _bed_equals_chamber_sweep(tm, tmp_path,
                                                      floor=61.8)
    assert obj.status["state"] == "done", obj.status
    assert [(k["chamber"], k["bed"]) for k in data["skipped"]] == [
        (60., 60.)]


def test_the_chamber_fan_runs_while_the_chamber_cools_then_goes_back(
        tm, tmp_path):
    printer = SweepPrinter()
    chamber = CoolingChamber(printer.clock, rate=0.002)
    printer.heaters["chamber_heater"] = chamber
    printer.heaters["heater_bed"] = BedHeldByChamber(chamber)
    fan = types.SimpleNamespace(get_status=lambda t: {"speed": 0.3})
    printer.extra_objects["fan_generic chamber_fan"] = fan
    obj, printer, data, _ = _sweep(
        tm, tmp_path, printer, BED_MIN=60, BED_MAX=65, BED_STEP=5,
        CHAMBER_MIN=60, CHAMBER_MAX=65, CHAMBER_STEP=5, SOAK=0, MAX_SOAK=0,
        CHAMBER_SOAK=0)
    assert obj.status["state"] == "done", obj.status
    fans = [s for s in printer.gcode.scripts if s.startswith("SET_FAN_SPEED")]
    # One cool-down, to the 60 set: full speed, then the 0.3 it had.
    assert fans == ["SET_FAN_SPEED FAN=chamber_fan SPEED=1.000",
                    "SET_FAN_SPEED FAN=chamber_fan SPEED=0.300"]
    scripts = printer.gcode.scripts
    on = scripts.index(fans[0])
    assert "BED_MESH_CALIBRATE" not in scripts[on:scripts.index(fans[1])]


def test_no_cool_fan_configured_runs_no_fan(tm, tmp_path):
    printer = SweepPrinter()
    chamber = CoolingChamber(printer.clock, rate=0.002)
    printer.heaters["chamber_heater"] = chamber
    printer.heaters["heater_bed"] = BedHeldByChamber(chamber)
    printer.extra_objects["fan_generic chamber_fan"] = types.SimpleNamespace(
        get_status=lambda t: {"speed": 0.})
    obj = tm.FFThermalMesh(Config(printer, cool_fan="",
                                  data_file=str(tmp_path / "m.json"),
                                  cool_timeout=3600., heat_timeout=100.))
    printer.handlers["klippy:connect"]()
    obj.probe_helper.z_at = repeatable_probe()
    printer.gcode.commands["THERMAL_MESH_SWEEP"](Gcmd(
        BED_MIN=60, BED_MAX=65, BED_STEP=5, CHAMBER_MIN=60, CHAMBER_MAX=65,
        CHAMBER_STEP=5, SOAK=0, MAX_SOAK=0, CHAMBER_SOAK=0))
    assert not any(s.startswith("SET_FAN_SPEED")
                   for s in printer.gcode.scripts)

