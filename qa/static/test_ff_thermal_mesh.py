"""The thermal mesh map: sweep order, convergence, point choice and matching."""

import importlib.util
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


@pytest.fixture(scope="module")
def tm():
    pkg = types.ModuleType(PKG)
    pkg.__path__ = []
    sys.modules[PKG] = pkg
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
