import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
EXTRA = (ROOT / "pkgs/klipper/payload/klipper/klippy/extras"
         / "ff_tool_offset.py")
CONFIG = ROOT / "pkgs/klipper-config/payload/config/ff-tool-offset.cfg"


def load_module():
    spec = importlib.util.spec_from_file_location("anvil_ff_tool_offset", EXTRA)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CommandError(Exception):
    pass


class GCmd:
    def __init__(self, values=None):
        self.values = values or {}
        self.info = []

    def get_int(self, name, default, **_kwargs):
        return int(self.values.get(name, default))

    def get_float(self, name, default, **_kwargs):
        return float(self.values.get(name, default))

    def respond_info(self, message):
        self.info.append(message)

    def error(self, message):
        return CommandError(message)


def test_plate_delta_requires_the_configured_recess():
    module = load_module()
    assert module.plate_removed(0.05, -0.75, 0.8)
    assert not module.plate_removed(0.05, -0.749, 0.8)
    assert not module.plate_removed(0.02, 0.01, 0.8)


def make_plate_check(module, readings):
    check = module.FFToolOffset.__new__(module.FFToolOffset)
    check.plate_reference_x = 155.0
    check.plate_reference_y = 130.0
    check.plate_min_drop = 0.8
    check.z_start = 10.0
    check._cylinder = lambda: (28.5, 214.5)
    check._enter_raw_frame = lambda: None
    check._run = lambda command: check.commands.append(command)
    check._normal_probe = lambda: next(readings)
    check.commands = []
    check.printer = SimpleNamespace(command_error=CommandError)
    return check


def test_plate_check_uses_only_normal_probe_before_accepting():
    module = load_module()
    check = make_plate_check(module, iter((0.1, -1.1)))
    gcmd = GCmd()
    check._plate_check(gcmd)
    assert len([line for line in check.commands if line.startswith("G1 X")]) == 2
    assert not any("ESTOP" in line for line in check.commands)
    assert any("drop 1.200" in line for line in gcmd.info)


def test_plate_check_refuses_a_sheet_spanning_both_points():
    module = load_module()
    check = make_plate_check(module, iter((0.1, 0.08)))
    with pytest.raises(module.FFToolOffsetError, match="remove the build plate"):
        check._plate_check(GCmd())
    assert not any("ESTOP" in line for line in check.commands)


def make_calibration(module, calibrated, station=(28.8, 212.6, -1.68),
                     heater_target=120.0):
    offset = module.FFToolOffset.__new__(module.FFToolOffset)
    offset.name = "ff_tool_offset"
    offset.reactor = SimpleNamespace(monotonic=lambda: 0.0)
    heater = SimpleNamespace(
        get_status=lambda _eventtime: {"target": heater_target})
    heaters = SimpleNamespace(lookup_heater=lambda _name: heater)
    toolhead = SimpleNamespace(wait_moves=lambda: None)
    objects = {"heaters": heaters, "toolhead": toolhead}
    offset.printer = SimpleNamespace(command_error=CommandError,
                                     lookup_object=objects.__getitem__)
    offset.toolchange = SimpleNamespace(
        get_status=lambda _eventtime: {"current_tool": 0, "state_ok": True})
    offset.tools = [SimpleNamespace(
        extruder_name="extruder", nozzle=(16.5, 212.8, 1.47),
        nozzle_temp=120.0, calibrated=lambda: calibrated)]
    offset.station = station
    offset.probe_temp = 120.0
    offset.temperature_tolerance = 3.0
    offset.nozzle_x_shift = 12.5
    offset._check_homed = lambda _gcmd: None
    offset._run_plate_check = lambda _gcmd: None
    offset._cylinder = lambda: (28.5, 214.5)
    offset._leave_raw_frame = lambda *_args: None
    offset._pad_shift = lambda _gcmd: -2.0
    offset.commands = []

    def run(command):
        offset.commands.append(command)
        if command.startswith("TEMPERATURE_WAIT"):
            raise CommandError("heater timeout")

    offset._run = run
    return offset


def test_uncalibrated_tool_is_measured_unpurged_at_probe_temp():
    module = load_module()
    offset = make_calibration(module, calibrated=False)
    gcmd = GCmd()
    with pytest.raises(CommandError, match="heater timeout"):
        offset.cmd_TOOL_CALIBRATE_TOOL_OFFSET(gcmd)
    assert not any("_FF_NOZZLE_CLEAN" in c for c in offset.commands)
    assert offset.commands[0] == (
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=120.0")
    assert offset.commands[1] == (
        "TEMPERATURE_WAIT SENSOR=extruder MINIMUM=117.0 MAXIMUM=123.0")
    assert offset.commands[-1] == (
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=0")
    assert any("no clean" in line for line in gcmd.info)


def test_calibrated_tool_is_cleaned_then_measured_at_the_cooled_target():
    module = load_module()
    offset = make_calibration(module, calibrated=True, heater_target=140.0)
    with pytest.raises(CommandError, match="heater timeout"):
        offset.cmd_TOOL_CALIBRATE_TOOL_OFFSET(GCmd({"TEMP": "240"}))
    assert offset.commands[0] == (
        "_FF_NOZZLE_CLEAN MODE=standby TOOLS=0 TEMPS=240.0,0,0,0"
        " Z_SHIFT=-2.000")
    # The clean leaves the heater at temp - clean_cool_delta: no re-heat.
    assert not any(c.startswith("SET_HEATER_TEMPERATURE HEATER=extruder"
                                " TARGET=1") for c in offset.commands)
    assert offset.commands[1] == (
        "TEMPERATURE_WAIT SENSOR=extruder MINIMUM=137.0 MAXIMUM=143.0")
    assert offset.commands[-1] == (
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=0")


def test_clean_uses_the_material_temperature_by_default():
    module = load_module()
    offset = make_calibration(module, calibrated=True)
    with pytest.raises(CommandError, match="heater timeout"):
        offset.cmd_TOOL_CALIBRATE_TOOL_OFFSET(GCmd())
    assert offset.commands[0] == (
        "_FF_NOZZLE_CLEAN MODE=standby TOOLS=0 Z_SHIFT=-2.000")


def test_clean_needs_a_station_calibration_too():
    module = load_module()
    offset = make_calibration(module, calibrated=True, station=None)
    with pytest.raises(CommandError, match="heater timeout"):
        offset.cmd_TOOL_CALIBRATE_TOOL_OFFSET(GCmd())
    assert not any("_FF_NOZZLE_CLEAN" in c for c in offset.commands)


def test_clean_left_with_the_heater_off_measures_nothing():
    module = load_module()
    offset = make_calibration(module, calibrated=True, heater_target=0.0)
    with pytest.raises(CommandError, match="heater is off"):
        offset.cmd_TOOL_CALIBRATE_TOOL_OFFSET(GCmd())
    assert not any(c.startswith("TEMPERATURE_WAIT") for c in offset.commands)


def test_public_macro_measures_station_once_and_skips_duplicate_checks():
    text = CONFIG.read_text()
    macro = text.split("[gcode_macro CALIBRATE_TOOL_OFFSETS]", 1)[1]
    assert "params.TOOL" in macro
    assert macro.count("TOOL_LOCATE_SENSOR") == 1
    assert "TOOL_CALIBRATE_TOOL_OFFSET PLATE_CHECK=0" in macro
    assert "TEMP={params.TEMP}" in macro


def make_pad_probe(module, reading, mounted=0):
    offset = module.FFToolOffset.__new__(module.FFToolOffset)
    offset.name = "ff_tool_offset"
    offset.reactor = SimpleNamespace(monotonic=lambda: 0.0)
    state = {"current_tool": mounted, "state_ok": True}
    offset.toolchange = SimpleNamespace(get_status=lambda _e: dict(state))
    macro = SimpleNamespace(variables={"clean_probe_z": 0.12})
    toolhead = SimpleNamespace(wait_moves=lambda: None)
    objects = {"gcode_macro _FF_FILAMENT": macro, "toolhead": toolhead}
    offset.printer = SimpleNamespace(command_error=CommandError,
                                     lookup_object=objects.__getitem__)
    offset.z_start = 10.0
    offset.commands = []
    offset._enter_raw_frame = lambda: offset.commands.append("RAW")

    def run(command):
        offset.commands.append(command)
        if command == "TOOLCHANGE_PARK":
            state["current_tool"] = -1

    offset._run = run
    offset._normal_probe = lambda: reading
    return offset


def test_pad_probe_parks_probes_at_the_app_point_and_returns_the_shift():
    module = load_module()
    offset = make_pad_probe(module, reading=-1.88)
    gcmd = GCmd()
    assert offset._pad_shift(gcmd) == pytest.approx(-2.0)
    assert offset.commands[0] == "TOOLCHANGE_PARK"
    assert "RAW" in offset.commands
    moves = [c for c in offset.commands if c.startswith("G1 X") or
             c.startswith("G1 Y")]
    # Via X250 so Y travel stays clear of the docks, then the probe point.
    assert moves == ["G1 X250.000 F12000", "G1 Y4.800 F12000",
                     "G1 X265.000 F12000"]
    assert any("shifted -2.000" in line for line in gcmd.info)


def test_pad_probe_refuses_a_carriage_it_cannot_empty():
    module = load_module()
    offset = make_pad_probe(module, reading=0.12)
    offset._run = lambda command: offset.commands.append(command)
    with pytest.raises(CommandError, match="not verifiably empty"):
        offset._pad_shift(GCmd())


def test_wipe_height_follows_z_shift_and_refuses_a_wild_one():
    cfg = (ROOT / "pkgs/klipper-config/payload/config/ff-filament.cfg"
           ).read_text()
    wipe = cfg.split("[gcode_macro _FF_NOZZLE_WIPE]", 1)[1].split(
        "[gcode_macro", 1)[0]
    assert "ff.clean_wipe_z + z_shift" in wipe
    assert "clean_probe_max_shift" in wipe
    clean = (ROOT / "pkgs/klipper-config/payload/config/ff-print-macros.cfg"
             ).read_text().split("[gcode_macro _FF_NOZZLE_CLEAN]", 1)[1]
    clean = clean.split("[gcode_macro", 1)[0]
    assert clean.count("Z_SHIFT={z_shift}") == 2
