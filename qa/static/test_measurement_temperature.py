"""A nozzle_z measured away from 120 C is corrected to the app's reference.

The print-start thermal term, (nozzle_temp - 120) * temp_offset, assumes the
nozzle was measured at 120 C -- what the app's owner calibration measures PLA
at. Each tool records its measuring temperature as nozzle_temp, and
_derive_offsets takes the growth beyond 120 C back out.
"""
import importlib.util
from types import SimpleNamespace

import pytest

from lib.paths import ROOT

pytestmark = pytest.mark.static

EXTRAS = ROOT / "pkgs" / "klipper" / "payload" / "klipper" / "klippy" / "extras"


def load(name):
    spec = importlib.util.spec_from_file_location(
        "anvil_" + name, EXTRAS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tool(z, temp, z_adjust=0.0):
    return SimpleNamespace(nozzle=(10.0, 20.0, z), nozzle_temp=temp,
                           z_adjust=z_adjust, calibrated=lambda: True)


def make(tools, station_z=-1.7):
    module = load("ff_toolchange")
    tc = module.FFToolchange.__new__(module.FFToolchange)
    tc.tools = tools
    tc.temp_offset = 0.00045
    tc._station_z = lambda: station_z
    return tc


def test_no_correction_at_the_reference():
    tc = make([])
    assert tc.measurement_correction(tool(1.5, 120.0)) == 0.0
    # An [ff_tool] without nozzle_temp is the app's (or an old) measurement.
    assert tc.measurement_correction(SimpleNamespace()) == 0.0


@pytest.mark.parametrize("temp, correction", [
    (140.0, -0.009),    # PETG: purged at 240, measured at 140
    (200.0, -0.036),    # the old Reforge hot measurement
    (100.0, +0.009),
])
def test_growth_beyond_120_comes_back_out(temp, correction):
    assert make([]).measurement_correction(tool(1.5, temp)) == \
        pytest.approx(correction)


def test_absolute_offsets_carry_the_correction():
    tc = make([tool(1.6, 140.0, z_adjust=0.02), tool(1.5, 120.0),
               tool(1.5, 120.0), tool(1.5, 120.0)])
    _x, _y, z = tc._derive_offsets(0)
    assert z[0] == pytest.approx(0.02 + (1.6 + 1.7) - 0.009)
    assert z[1] == pytest.approx(1.5 + 1.7)


def test_relative_fallback_corrects_both_sides():
    tc = make([tool(1.6, 140.0), tool(1.5, 120.0), tool(1.7, 140.0),
               tool(1.5, 120.0)], station_z=None)
    _x, _y, z = tc._derive_offsets(0)
    assert z[0] == pytest.approx(0.0)
    assert z[1] == pytest.approx((1.5 - 1.6) + 0.009)
    assert z[2] == pytest.approx(0.1)


def test_set_nozzle_records_the_temperature_with_the_measurement():
    module = load("ff_tool")
    staged = {}
    configfile = SimpleNamespace(
        set=lambda section, option, value: staged.__setitem__(option, value))
    t = module.FFTool.__new__(module.FFTool)
    t.name = "ff_tool 0"
    t.printer = SimpleNamespace(lookup_object=lambda _name: configfile)
    t.set_nozzle(1.0, 2.0, 3.0, 140.0)
    assert (t.nozzle_temp, staged["nozzle_temp"]) == (140.0, "140.0")
    t.set_nozzle(1.0, 2.0, 3.0)
    assert (t.nozzle_temp, staged["nozzle_temp"]) == (120.0, "120.0")
