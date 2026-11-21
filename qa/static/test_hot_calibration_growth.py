"""A hot nozzle_z is not counted twice in the per-tool Z offset.

TOOL_CALIBRATE_TOOL_OFFSET measures each nozzle hot, so its nozzle_z already
carries the nozzle's thermal growth. The print-start thermal term assumes a
cold (factory) measurement, so _derive_offsets takes the recorded growth back
out. A cold or imported measurement (nozzle_temp 0) is left alone.
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
    tc.calibration_expansion = 0.00057
    tc.calibration_ambient = 25.0
    tc._station_z = lambda: station_z
    return tc


def test_growth_is_zero_for_a_cold_measurement():
    tc = make([])
    assert tc.calibration_growth(tool(1.5, 0.0)) == 0.0
    assert tc.calibration_growth(SimpleNamespace()) == 0.0


def test_growth_at_200c_is_about_a_tenth():
    tc = make([])
    assert tc.calibration_growth(tool(1.5, 200.0)) == pytest.approx(0.09975)


def test_hot_tool_loses_its_growth_cold_tool_keeps_the_raw_gap():
    tc = make([tool(1.6, 200.0, z_adjust=0.02), tool(1.5, 0.0),
               tool(1.5, 0.0), tool(1.5, 0.0)])
    _x, _y, z = tc._derive_offsets(0)
    assert z[0] == pytest.approx(0.02 + (1.6 + 1.7) - 0.09975)
    assert z[1] == pytest.approx(1.5 + 1.7)


def test_relative_fallback_corrects_both_sides():
    tc = make([tool(1.6, 200.0), tool(1.5, 0.0), tool(1.7, 200.0),
               tool(1.5, 0.0)], station_z=None)
    _x, _y, z = tc._derive_offsets(0)
    assert z[0] == pytest.approx(0.0)
    # cold T1 against hot T0: T0's growth comes back off the base.
    assert z[1] == pytest.approx(1.5 - (1.6 - 0.09975))
    assert z[2] == pytest.approx(0.1)


def test_set_nozzle_records_the_temperature_with_the_measurement():
    module = load("ff_tool")
    staged = {}
    configfile = SimpleNamespace(
        set=lambda section, option, value: staged.__setitem__(option, value))
    t = module.FFTool.__new__(module.FFTool)
    t.name = "ff_tool 0"
    t.printer = SimpleNamespace(lookup_object=lambda _name: configfile)
    t.set_nozzle(1.0, 2.0, 3.0, 200.0)
    assert t.nozzle_temp == 200.0
    assert staged["nozzle_temp"] == "200.0"
    t.set_nozzle(1.0, 2.0, 3.0)
    assert t.nozzle_temp == 0.0
    assert staged["nozzle_temp"] == "0.0"
