"""ASSIGN_TOOL: tool numbers, as klipper-toolchanger and HelixScreen have them.

A print file speaks in tool NUMBERS (T<n>, M104/M109 T<n>); ASSIGN_TOOL
TOOL=T<p> N=<n> points number n at physical tool p. p gives up its old
number and the tool that had n is left with none -- the eviction HelixScreen
applies to its own table, so the two agree on what the carriage holds.
These run the real command, number lookups, status views and the M104
wrapper; tool changes themselves are substituted.
"""
import importlib.util
from types import SimpleNamespace

import pytest

from lib.paths import ROOT

pytestmark = pytest.mark.static

MODULE = (ROOT / "pkgs" / "klipper" / "payload" / "klipper" / "klippy" /
          "extras" / "ff_toolchange.py")


class CommandError(Exception):
    pass


_REQUIRED = object()


class Gcmd:
    """Klipper's GCodeCommand as far as these commands use it: a parameter
    with no default is required, an explicit default of None is returned,
    and get_command_parameters() is the live dict."""

    def __init__(self, **params):
        self.params = {k: str(v) for k, v in params.items()}
        self.said = []
        self.error = CommandError

    def get_command_parameters(self):
        return self.params

    def get(self, name, default=_REQUIRED):
        if name in self.params:
            return self.params[name]
        if default is _REQUIRED:
            raise CommandError("Error on '%s': missing" % name)
        return default

    def get_int(self, name, default=_REQUIRED, minval=None, maxval=None):
        value = self.get(name, default)
        if value is None:
            return None
        value = int(value)
        if (minval is not None and value < minval) or (
                maxval is not None and value > maxval):
            raise CommandError("Error on '%s': out of range" % name)
        return value

    def respond_info(self, msg):
        self.said.append(msg)


@pytest.fixture
def tc(monkeypatch):
    spec = importlib.util.spec_from_file_location("anvil_ff_toolchange", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tc = module.FFToolchange.__new__(module.FFToolchange)
    tc.changing = False
    tc.tool_numbers = list(range(module.EXTRUDER_COUNT))
    tc.tools = [SimpleNamespace(index=i,
                                extruder_name="extruder%s" % (i or ""),
                                calibrated=lambda: True)
                for i in range(module.EXTRUDER_COUNT)]
    tc.part_fan = "fan_generic fanM106"
    tc.offset_x = tc.offset_y = tc.offset_z = [0.0] * module.EXTRUDER_COUNT
    tc.grab_sensors = []
    tc.job_z = 0.0
    tc.armed_tool = -1
    tc.gcode_transform = SimpleNamespace(tool=None)
    tc.mounted = 0
    monkeypatch.setattr(tc, "_current_tool_or_none",
                        lambda eventtime=None: (tc.mounted, "test"))
    monkeypatch.setattr(tc, "_in_location", lambda i, eventtime=None: False)
    monkeypatch.setattr(tc, "_station_z", lambda: 1.0)
    monkeypatch.setattr(tc, "_armed_sensors", lambda: [])
    tc.changes = []
    monkeypatch.setattr(tc, "_toolchange",
                        lambda gcmd, tool: tc.changes.append(tool))
    tc.module = module
    return tc


def assign(tc, tool, number):
    gcmd = Gcmd(TOOL=tool, N=number)
    tc.cmd_ASSIGN_TOOL(gcmd)
    return gcmd


def test_identity_at_start(tc):
    status = tc.get_status(0)
    assert status["tool_map"] == [0, 1, 2, 3]
    assert status["tool_numbers_by_tool"] == [0, 1, 2, 3]


def test_assigning_evicts_both_losing_sides(tc):
    # T0 in the file now picks up the tool in dock 3: tool T2 gives up 2,
    # tool T0 gives up 0. HelixScreen's own table does the same.
    assign(tc, "T2", 0)
    status = tc.get_status(0)
    assert status["tool_map"] == [2, 1, -1, 3]
    assert status["tool_numbers_by_tool"] == [-1, 1, 0, 3]


@pytest.mark.parametrize("name", ["T2", "tool T2", "t2"])
def test_the_tool_can_be_named_as_helixscreen_names_it(tc, name):
    assign(tc, name, 0)
    assert tc.get_status(0)["tool_map"][0] == 2


def test_bare_tn_follows_the_number(tc):
    assign(tc, "T2", 0)
    tc._make_tn(0)(Gcmd())
    assert tc.changes == [2]
    with pytest.raises(CommandError, match="T2 is not assigned"):
        tc._make_tn(2)(Gcmd())


def test_toolchange_index_follows_the_number(tc):
    assign(tc, "T3", 1)
    tc.cmd_TOOLCHANGE(Gcmd(INDEX=1))
    assert tc.changes == [3]


def test_select_tool_stays_physical(tc, monkeypatch):
    # What HelixScreen 0.99.118 sends to mount the head in slot 2.
    assign(tc, "T2", 0)
    tc.cmd_SELECT_TOOL(Gcmd(T=2))
    tc.cmd_SELECT_TOOL(Gcmd(TOOL="tool T2"))
    assert tc.changes == [2, 2]


def test_helixscreen_reset_restores_identity(tc):
    # After T2 -> 0, HelixScreen's reset sends one ASSIGN_TOOL per number
    # that is not its own.
    assign(tc, "T2", 0)
    assign(tc, "T0", 0)
    assign(tc, "T2", 2)
    assert tc.get_status(0)["tool_map"] == [0, 1, 2, 3]


def test_reset_1_restores_identity(tc):
    assign(tc, "T2", 0)
    tc.cmd_ASSIGN_TOOL(Gcmd(RESET=1))
    assert tc.get_status(0)["tool_numbers_by_tool"] == [0, 1, 2, 3]


def test_refused_mid_change(tc):
    tc.changing = True
    with pytest.raises(CommandError, match="during a tool change"):
        assign(tc, "T2", 0)


def test_a_number_is_required(tc):
    with pytest.raises(CommandError):
        tc.cmd_ASSIGN_TOOL(Gcmd(TOOL="T2"))


def test_status_reports_the_mounted_tools_number(tc):
    assign(tc, "T2", 0)
    tc.mounted = 2
    view = tc.module._ToolchangerView(tc).get_status(0)
    assert view["tool_number"] == 0
    assert view["tool"] == "T2"
    # Ordered by number, as klipper-toolchanger lists them.
    assert view["tool_numbers"] == [0, 1, 3]
    assert view["tool_names"] == ["T2", "T1", "T3"]
    assert tc.module._ToolView(tc, 2).get_status(0)["tool_number"] == 0
    assert tc.module._ToolView(tc, 0).get_status(0)["tool_number"] == -1
    # A tool left with no number is still on the carriage, but has no number.
    tc.mounted = 0
    assert tc.module._ToolchangerView(tc).get_status(0)["tool_number"] == -1


def test_m104_t_heats_the_tool_the_number_selects(tc):
    assign(tc, "T2", 0)
    seen = []
    handler = tc._make_heater_handler(
        "M104", lambda gcmd: seen.append(dict(gcmd.params)))
    handler(Gcmd(S=220, T=0))
    handler(Gcmd(S=200))
    assert seen == [{"S": "220", "T": "2"}, {"S": "200"}]


def test_m104_on_an_unassigned_number_is_skipped_not_fatal(tc):
    assign(tc, "T2", 0)
    seen = []
    gcmd = Gcmd(S=220, T=2)
    tc._make_heater_handler("M109", seen.append)(gcmd)
    assert seen == []
    assert "T2 is not assigned" in gcmd.said[0]
