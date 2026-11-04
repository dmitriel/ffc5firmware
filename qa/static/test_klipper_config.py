"""The Klipper config we ship: one pin, one owner, and macros that compile.

THE BUG THIS EXISTS FOR. `chamber_heat_fan` had to change section type per
model -- `[heater_fan]` on the Pro so the element's fan follows the chamber
heater without any G-code running, `[fan_generic]` on the plain Creator 5,
which has no `chamber_heater` for `lookup_heater()` to resolve at
`klippy:ready`. Klipper can override an option but cannot un-declare a section,
so the section had to MOVE out of `printer.base.cfg` into the per-model
`chamber/<model>.cfg`.

That move is the dangerous kind. Leave the old section behind and PD12 is
claimed twice; `pins.py` raises "pin PD12 used multiple times in config" and
klippy refuses to start -- on a printer, after a flash, with no UI to say why.
Nothing else in the suite reads these files: `test_ipk.py` checks that the
chamber configs are PACKAGED, never that they PARSE.

WHY THIS LANE, AND WHAT IT THEREFORE CANNOT SEE. The static lane runs on a bare
checkout, so the only configs here are the ones this repo ships. FlashForge's
own includes -- printer.filament.cfg, printer.probe.cfg, printer.mesh.cfg,
printer.vibration.cfg -- exist only under work/ after `bin/unpack.sh`, and
conftest is explicit that a missing firmware image is a failure rather than a
skip, so depending on them here would make this lane fail on a clean clone.
They are skipped, and a pin they claim that one of ours also claims is NOT
caught here. That is a real hole; what closes it is that every pin-bearing
section on the main MCU lives in printer.base.cfg, which is ours.

WHY RawConfigParser(strict=False). Klipper's own parser. Same-named sections
MERGE and the last value wins, which is the mechanism ff-chamber.cfg uses to
override `initial_WHITE` on `[led chamber_led]`; a strict parser would call
that a duplicate-section error and this test would be wrong about the file the
printer actually reads.
"""
import ast
import configparser
import re
import shlex

import jinja2
import pytest

from lib.paths import ROOT

pytestmark = pytest.mark.static

CONFIG = ROOT / "pkgs" / "klipper-config" / "payload" / "config"
MODELS = ("Creator5", "Creator5Pro")
# Only the Pro has a chamber heating element, which is the single functional
# difference between the two stock firmwareExe builds.
HAS_HEATER = {"Creator5": False, "Creator5Pro": True}

# Outputs and thermistors. `endstop_pin` is deliberately absent: Klipper shares
# endstops by design (share_type "endstop"), and FlashForge's hd_home and
# e_stop extras re-claim those same input pins on purpose. An output pin or an
# ADC pin cannot be claimed twice under any share_type, so those are the ones
# worth asserting on.
PIN_OPTS = ("pin", "heater_pin", "sensor_pin", "white_pin", "red_pin",
            "green_pin", "blue_pin", "step_pin", "dir_pin", "enable_pin")
SHARED_SECTION_TYPES = ("hd_home", "e_stop", "gcode_button")

INCLUDE = re.compile(r"^\[include (.+)\]\s*$")


def _resolve(name, model):
    # printer.chamber.cfg is not a file in the payload: the package ships a
    # chamber/ directory and anvil-link-prog.sh links the model's file into
    # place on the printer. Resolve it the same way.
    if name == "printer.chamber.cfg":
        return CONFIG / "chamber" / (model + ".cfg")
    path = CONFIG / name
    return path if path.is_file() else None


def _assemble(model):
    """The config text klippy would see, minus FlashForge's own includes."""
    seen, chunks, skipped = set(), [], []

    def load(name):
        path = _resolve(name, model)
        if path is None:
            skipped.append(name)
            return
        if path in seen:
            return
        seen.add(path)
        body = []
        for line in path.read_text(encoding="utf-8").splitlines():
            m = INCLUDE.match(line)
            if m:
                load(m.group(1).strip())
                body.append("")
            else:
                body.append(line)
        chunks.append("\n".join(body))

    load("printer.base.cfg")
    return "\n".join(chunks), skipped


def _parse(model):
    text, _ = _assemble(model)
    # klippy's own parser settings (configfile.py). inline_comment_prefixes
    # matters as much as strict=False: configparser strips ';' and '#' only
    # when whitespace precedes them, so a ';' inside a string literal -- as in
    # ff-filament's "paused with T%s mounted; filament operations ..." --
    # survives. Stripping comments by hand instead reports those as broken.
    cp = configparser.RawConfigParser(
        strict=False, inline_comment_prefixes=(";", "#"))
    cp.read_string(text)
    return cp


# --------------------------------------------------------------------------
# Running the macros, because parsing them is not the same as believing them.
#
# THE BUG THIS HALF EXISTS FOR. SET_HEATER_TEMPERATURE is a MUX command keyed
# on HEATER: klippy dispatches it through _cmd_mux, which raises "The value
# 'chamber_heater' is not valid for HEATER" for a heater that was never
# registered (gcode.py). That is a command error, and a command error aborts a
# running print. On the plain Creator 5 there is no chamber_heater, and the
# gate in ff-chamber.cfg only blocked TARGET > 0 -- so `M141 S0`, which is what
# slicers put in their END G-CODE, went straight to the base command and killed
# the print at the very end. The file's own header said "chamber off behaves
# exactly as before"; it did not.
#
# Reading the macro did not show that. Running it did.
# --------------------------------------------------------------------------

ARGS_R = re.compile("([A-Z_]+|[A-Z*])")


def _is_traditional(cmd):
    return len(cmd) >= 2 and cmd[0].isupper() and cmd[1].isdigit()


def _parse_params(line, cmd):
    """klippy's TWO parameter parsers, which disagree with each other.

    Traditional (M141): args_r.split(line.upper()) -- the whole line is
    uppercased, and "M141 S60" yields {'M': '141', 'S': '60'}.

    Extended (SET_HEATER_TEMPERATURE): shlex over the RAW text, keys uppercased
    and values left alone, which is why HEATER=chamber_heater stays lowercase
    and the macro's `|lower` is load-bearing rather than decorative.
    """
    rest = line[len(line.split()[0]):]
    if _is_traditional(cmd):
        parts = ARGS_R.split(line.upper())
        return {parts[i]: parts[i + 1].strip()
                for i in range(1, len(parts) - 1, 2)}
    lex = shlex.shlex(rest, posix=True)
    lex.whitespace_split = True
    lex.commenters = "#;"
    return {k.upper(): v for k, v in (a.split("=", 1) for a in lex)}


def _run(model, command, has_heater, extra=None):
    """Expand `command` until only non-macro commands remain, as klippy would.

    `extra` adds printer objects a macro reads that are not macros themselves,
    such as {"ff_print": {...}}.

    Returns (emitted commands, action_respond_info messages).
    """
    cp = _parse(model)
    printer = {}
    for sec in cp.sections():
        if sec.startswith("gcode_macro "):
            variables = {}
            for opt in cp.options(sec):
                if opt.startswith("variable_"):
                    try:
                        variables[opt[9:]] = ast.literal_eval(cp.get(sec, opt))
                    except (ValueError, SyntaxError):
                        variables[opt[9:]] = cp.get(sec, opt)
            printer[sec] = variables
    if has_heater:
        printer["heater_generic chamber_heater"] = {
            "temperature": 25.0, "target": 0.0}
    printer.update(extra or {})

    env = jinja2.Environment("{%", "%}", "{", "}")
    out, info = [], []

    def step(line, depth=0):
        assert depth < 10, "macro recursion in %s" % command
        line = line.strip()
        if not line:
            return
        cmd = line.split()[0].upper()
        sec = next((s for s in cp.sections()
                    if s.lower() == ("gcode_macro " + cmd).lower()), None)
        if sec is None or not cp.has_option(sec, "gcode"):
            out.append(line)
            return
        rendered = env.from_string(cp.get(sec, "gcode")).render(
            params=_parse_params(line, cmd),
            rawparams=line[len(line.split()[0]):].strip(),
            printer=printer,
            action_respond_info=lambda m: info.append(m) or "",
            action_raise_error=_raise,
        )
        for sub in rendered.splitlines():
            step(sub, depth + 1)

    step(command)
    return out, info


def _raise(msg):
    raise AssertionError("macro raised: %s" % msg)


def test_setting_a_chamber_target_starts_the_loop_fan_first():
    out, _ = _run("Creator5Pro", "M141 S60", has_heater=True)
    assert out == [
        "SET_FAN_SPEED FAN=chamber_loop_fan SPEED=0.3",
        "SET_HEATER_TEMPERATURE_BASE HEATER=chamber_heater TARGET=60.0",
    ], out


def test_clearing_a_chamber_target_stops_the_loop_fan_last():
    out, _ = _run("Creator5Pro", "M141 S0", has_heater=True)
    assert out == [
        "SET_HEATER_TEMPERATURE_BASE HEATER=chamber_heater TARGET=0.0",
        "SET_FAN_SPEED FAN=chamber_loop_fan SPEED=0",
    ], out


def test_m191_waits_below_the_target_by_the_band():
    out, _ = _run("Creator5Pro", "M191 S60", has_heater=True)
    assert 'TEMPERATURE_WAIT SENSOR="heater_generic chamber_heater" ' \
           "MINIMUM=58.0" in out, out


@pytest.mark.parametrize("command", ["M141 S0", "M191 S0", "M141"])
def test_turning_the_chamber_off_is_silent_on_a_model_without_one(command):
    """The end-of-print case. Emitting anything here aborts the print."""
    out, info = _run("Creator5", command, has_heater=False)
    assert out == [], (
        "%s reaches klippy on a Creator 5 and SET_HEATER_TEMPERATURE is a mux "
        "command: it raises \"The value 'chamber_heater' is not valid for "
        "HEATER\" and aborts the print. Emitted: %s" % (command, out))
    assert info == [], "nothing to turn off is not worth a message: %s" % info


def test_asking_for_chamber_heat_on_a_model_without_one_warns_and_continues():
    out, info = _run("Creator5", "M141 S60", has_heater=False)
    assert out == [], out
    assert info and "ignoring TARGET=60.0" in info[0], info


@pytest.mark.parametrize("model", MODELS)
def test_other_heaters_are_untouched(model):
    """The gate must not cost the hotend or the bed anything."""
    out, info = _run(model, "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=220",
                     HAS_HEATER[model])
    assert out == ["SET_HEATER_TEMPERATURE_BASE HEATER=extruder TARGET=220"], out
    assert info == []


@pytest.mark.parametrize("model", MODELS)
def test_the_shipped_config_parses(model):
    cp = _parse(model)
    assert cp.sections(), "%s assembled to nothing -- includes did not resolve" % model


@pytest.mark.parametrize("model", MODELS)
def test_dc24v_rail_follows_every_hotend(model):
    cp = _parse(model)
    section = "heater_fan dc24v_ctl"
    assert cp.has_section(section), "%s has no [%s]" % (model, section)
    assert cp.get(section, "pin").strip() == "eheaterboard:PA3"
    assert cp.get(section, "heater").replace(" ", "") == \
        "extruder,extruder1,extruder2,extruder3"
    assert cp.getfloat(section, "heater_temp") == 50.0
    assert cp.getfloat(section, "fan_speed") == 1.0
    assert cp.getfloat(section, "shutdown_speed") == 0.0
    assert cp.getfloat(section, "kick_start_time") == 0.0
    assert not cp.has_section("output_pin DC24V_CTL")


@pytest.mark.parametrize("model", MODELS)
def test_no_output_pin_is_claimed_twice(model):
    cp = _parse(model)
    claims = {}
    for sec in cp.sections():
        if sec.split()[0] in SHARED_SECTION_TYPES:
            continue
        for opt in PIN_OPTS:
            if not cp.has_option(sec, opt):
                continue
            raw = cp.get(sec, opt).split(",")[0].strip()
            pin = raw.lstrip("!^~").strip()
            # A templated or empty value is not a literal claim.
            if not pin or "{" in pin:
                continue
            claims.setdefault(pin, []).append("[%s] %s" % (sec, opt))

    dupes = {p: v for p, v in sorted(claims.items()) if len(v) > 1}
    assert not dupes, "klippy would refuse to start on %s:\n%s" % (
        model,
        "\n".join("  pin %s claimed by %s" % (p, " AND ".join(v))
                  for p, v in dupes.items()),
    )


@pytest.mark.parametrize("model", MODELS)
def test_every_gcode_macro_body_compiles(model):
    # Klipper's own delimiters (gcode_macro.py: Environment('{%','%}','{','}')).
    # A default Environment would read `{rawparams}` as literal text and compile
    # anything, which is a test that cannot fail.
    env = jinja2.Environment("{%", "%}", "{", "}")
    cp = _parse(model)
    broken = []
    for sec in cp.sections():
        if not sec.startswith("gcode_macro ") or not cp.has_option(sec, "gcode"):
            continue
        # No hand-stripping: _parse() already read this with klippy's own
        # inline_comment_prefixes, so the body here is the body Jinja gets.
        src = cp.get(sec, "gcode")
        try:
            env.from_string(src)
        except jinja2.TemplateSyntaxError as exc:
            broken.append("  [%s] line %s: %s" % (sec, exc.lineno, exc.message))
    assert not broken, "macros klippy would reject on %s:\n%s" % (
        model, "\n".join(broken))


@pytest.mark.parametrize("model", MODELS)
def test_the_chamber_heater_fan_matches_the_model(model):
    """The point of the move: the Pro binds the fan to the heater, the plain
    Creator 5 cannot, and neither may leave the section in printer.base.cfg."""
    cp = _parse(model)
    base = (CONFIG / "printer.base.cfg").read_text(encoding="utf-8")
    assert not re.search(r"^\[\w+ chamber_heat_fan\]", base, re.M), (
        "chamber_heat_fan must live in chamber/<model>.cfg, not printer.base.cfg: "
        "a section left in both claims PD12 twice")

    if model == "Creator5Pro":
        sec = "heater_fan chamber_heat_fan"
        assert sec in cp.sections(), "the Pro's element fan must follow the heater"
        assert cp.get(sec, "heater").strip() == "chamber_heater"
        assert cp.get(sec, "pin").strip() == "PD12"
    else:
        sec = "fan_generic chamber_heat_fan"
        assert sec in cp.sections(), (
            "the plain Creator 5 has no chamber_heater, so a [heater_fan] here "
            "would fail lookup_heater() at klippy:ready")
        assert cp.get(sec, "pin").strip() == "PD12"
        assert "heater_fan chamber_heat_fan" not in cp.sections()


# --------------------------------------------------------------------------
# Print start: heating, deferred mesh, adaptive mesh.
# --------------------------------------------------------------------------

def _commands(out):
    """What klippy would see: comment text gone, blank lines gone."""
    lines = (line.split(";", 1)[0].strip() for line in out)
    return [line for line in lines if line]


def _idle_file():
    return {"ff_toolchange": {
        "calibrated_tools": [0, 1, 2, 3], "docked_tools": [0, 1, 2, 3],
        "current_tool": -1, "station_z": 1.0, "print_offset_ready": True,
        "state_ok": True, "state_reason": ""}}


@pytest.mark.parametrize("model", MODELS)
def test_the_bed_starts_heating_before_the_first_homing(model):
    out, _ = _run(model, "START_PRINT BED=60 TOOL=0 NOZZLE=220 CLEAN=0 MESH=default",
                  has_heater=model == "Creator5Pro", extra=_idle_file())
    out = _commands(out)

    # ff-legacy.cfg's G28 wrapper docks a mounted tool and calls G28.1.
    assert out.index("M140 S60.0") < out.index("G28.1")


@pytest.mark.parametrize("model", MODELS)
def test_start_print_without_defer_still_meshes_and_grabs_the_first_tool(model):
    out, _ = _run(model, "START_PRINT BED=60 TOOL=2 NOZZLE=220 CLEAN=0 MESH=default",
                  has_heater=model == "Creator5Pro", extra=_idle_file())
    out = _commands(out)

    assert "BED_MESH_PROFILE LOAD=MESH_DATA" in out
    assert out.index("G28.1 Z") < out.index("BED_MESH_PROFILE LOAD=MESH_DATA")
    assert out.index("BED_MESH_PROFILE LOAD=MESH_DATA") < out.index("T2")
    assert ("TOOLCHANGE_SET_PRINT_OFFSET NOZZLE=220.0 BED=60.0 LAYER=0.0 TOOL=2"
            in out)


@pytest.mark.parametrize("model", MODELS)
def test_a_print_says_which_mesh_strategy_it_uses(model):
    _out, info = _run(model, "_FF_FLOW_MESH_BEGIN FIRST_LAYER=0.2",
                      has_heater=False)
    assert "Bed mesh strategy: standard" in info[0]


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("first, started", [("0.24", True), ("0", False)])
def test_the_flow_mesh_takes_the_first_layer_from_the_print_start(
        model, first, started):
    on = {"gcode_macro FLOW_MESH_TOGGLE": {"enabled": 1}}
    out, info = _run(model, "_FF_FLOW_MESH_BEGIN FIRST_LAYER=%s" % first,
                     has_heater=False, extra=on)
    if started:
        assert out == ["FLOW_MESH_START FIRST_LAYER=0.24"]
    else:
        assert out == [] and "needs the first layer height" in info[0]


@pytest.mark.parametrize("model", MODELS)
def test_print_start_hands_its_layer_to_the_flow_mesh(model):
    cp = _parse(model)
    assert "_FF_FLOW_MESH_BEGIN FIRST_LAYER={layer}" in cp.get(
        "gcode_macro START_PRINT", "gcode")


@pytest.mark.parametrize("model", MODELS)
def test_the_prime_tower_is_defined_from_the_files_measured_outline(model):
    tower = {
        "prime_tower_x": 16.4744, "prime_tower_y": 221.74,
        "prime_tower_width": 28.0, "prime_tower_depth": 27.0,
        "prime_tower_brim": 2.2, "prime_tower_rotation": 0.0,
        "prime_tower_center_x": 23.645, "prime_tower_center_y": 209.136,
        "prime_tower_core_min_x": None,
        "prime_tower_outer_min_x": 7.413, "prime_tower_outer_max_x": 39.877,
        "prime_tower_outer_min_y": 193.394, "prime_tower_outer_max_y": 224.878,
    }
    out, info = _run(
        model, "DEFINE_PRIME_TOWER_OBJECT X=999 Y=999 WIDTH=10",
        has_heater=False, extra={"ff_print": tower})

    assert _commands(out) == [
        "EXCLUDE_OBJECT_DEFINE NAME=PRIME_TOWER CENTER=23.645,209.136 "
        "POLYGON=[[6.413,192.394],[40.877,192.394],[40.877,225.878],"
        "[6.413,225.878],[6.413,192.394]]"]
    assert any("replacing the start G-code's 999.000,999.000" in m
               for m in info)


@pytest.mark.parametrize("model", MODELS)
def test_a_tower_without_a_measured_outline_is_boxed_at_any_rotation(model):
    blank = {key: None for key in (
        "prime_tower_x", "prime_tower_y", "prime_tower_width",
        "prime_tower_depth", "prime_tower_brim", "prime_tower_rotation",
        "prime_tower_center_x", "prime_tower_center_y",
        "prime_tower_core_min_x", "prime_tower_outer_min_x")}
    out, _ = _run(
        model, "DEFINE_PRIME_TOWER_OBJECT X=100 Y=200 WIDTH=20 DEPTH=10 BRIM=2",
        has_heater=False, extra={"ff_print": blank})

    # Centre (110, 205); half extents 10+2+1 and 5+2+1; r = sqrt(2) * 13.
    (command,) = _commands(out)
    assert command.startswith(
        "EXCLUDE_OBJECT_DEFINE NAME=PRIME_TOWER CENTER=110.0,205.0 POLYGON=")
    assert "[[91.615" in command and "128.384" in command


def _render_one(model, macro, params, extra=None):
    """One macro's own lines, its sub-macros left unexpanded."""
    cp = _parse(model)
    printer = {}
    for sec in cp.sections():
        if sec.startswith("gcode_macro "):
            variables = {}
            for opt in cp.options(sec):
                if opt.startswith("variable_"):
                    try:
                        variables[opt[9:]] = ast.literal_eval(cp.get(sec, opt))
                    except (ValueError, SyntaxError):
                        variables[opt[9:]] = cp.get(sec, opt)
            printer[sec] = variables
    printer.update(extra or {})
    env = jinja2.Environment("{%", "%}", "{", "}")
    info = []
    out = env.from_string(cp.get("gcode_macro " + macro, "gcode")).render(
        params=params, printer=printer,
        action_respond_info=lambda m: info.append(m) or "",
        action_raise_error=_raise)
    return _commands(out.splitlines()), info


def _first(out, prefix):
    return next(i for i, c in enumerate(out) if c.startswith(prefix))


@pytest.mark.parametrize("model", MODELS)
def test_start_print_preheats_all_cleans_the_others_then_the_first_last(model):
    out, _ = _render_one(model, "START_PRINT", {
        "BED": "60", "TOOL": "2", "NOZZLE": "220", "LAYER": "0.2",
        "TOOLS": "0:230,2:220,3:240", "PURGE": "30"})
    preheat = _first(out, "_FF_NOZZLE_CLEAN MODE=preheat TOOLS=0,2,3")
    others = _first(out, "_FF_NOZZLE_CLEAN TOOLS=0,3 ")
    final = _first(out, "_FF_NOZZLE_CLEAN MODE=print TOOLS=2 ")
    # All tools heat at once, beside the bed, before anything moves.
    assert _first(out, "M140 S60") < preheat < _first(out, "G28")
    # The others are cleaned while the bed heats; the first tool goes last,
    # after the mesh, and the print offset/flow mesh follow without motion.
    assert others < _first(out, "M190") < _first(out, "_FF_MESH MODE=auto")
    # The print offset (no motion) comes just before the last clean, and the
    # flow mesh starts after it.
    assert _first(out, "_FF_MESH") < \
        _first(out, "TOOLCHANGE_SET_PRINT_OFFSET") < final < \
        _first(out, "_FF_FLOW_MESH_BEGIN FIRST_LAYER=0.2")
    assert "PURGE=30.0" in out[others] and "PURGE=30.0" in out[final]
    assert not any(c == "T2" for c in out)          # the clean grabs it


def _clean_printer():
    return {"tool T%d" % t: {"extruder": "extruder%s" % (t or "")}
            for t in range(4)} | {"configfile": {"settings": {
                "extruder%s" % (t or ""): {"nozzle_diameter": 0.4}
                for t in range(4)}}}


@pytest.mark.parametrize("model", MODELS)
def test_preheat_only_sets_each_tool_100_below_its_temperature(model):
    out, _ = _render_one(model, "_FF_NOZZLE_CLEAN", {
        "MODE": "preheat", "TOOLS": "0,2", "TEMPS": "230,0,250,0",
        "TEMP": "220"}, extra=_clean_printer())
    assert out == ["M104 S130.0 T0", "M104 S150.0 T2"]


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("mode, cool, release", [("standby", 1, 1),
                                                 ("print", 0, 0)])
def test_the_print_tool_wipes_hot_and_stays_mounted(model, mode, cool,
                                                    release):
    out, _ = _render_one(model, "_FF_NOZZLE_CLEAN", {
        "MODE": mode, "TOOLS": "1", "TEMP": "220"}, extra=_clean_printer())
    assert "_FF_NOZZLE_WIPE TOOL=1 TEMP=220 COOL=%d" % cool in out
    assert "_FF_FILAMENT_FINISH RELEASE=%d HEAT_OFF=0" % release in out
    assert "G1 E50.0 F240" in out


@pytest.mark.parametrize("model", MODELS)
def test_purge_0_wipes_without_purging(model):
    out, _ = _render_one(model, "_FF_NOZZLE_CLEAN", {
        "TOOLS": "1", "TEMP": "220", "PURGE": "0"}, extra=_clean_printer())
    assert not any(c.startswith("G1 E") for c in out)
    assert any(c.startswith("_FF_NOZZLE_WIPE") for c in out)


def _wipe_printer():
    return {"tool T1": {"extruder": "extruder1", "gcode_z_offset": 3.2},
            "gcode_move": {"homing_origin": {"z": 0.}},
            "toolchanger": {"print_z_offset": 0.05}}


@pytest.mark.parametrize("model", MODELS)
def test_the_hot_wipe_rests_a_second_on_the_pad_before_lifting(model):
    out, _ = _render_one(model, "_FF_NOZZLE_WIPE",
                         {"TOOL": "1", "TEMP": "220", "COOL": "0"},
                         extra=_wipe_printer())
    assert "G4 P1000" in out
    assert out.index("G4 P1000") < out.index("G1 Z10 F1200")
    assert not any(c.startswith("TEMPERATURE_WAIT") for c in out)


@pytest.mark.parametrize("model", MODELS)
def test_the_cold_wipe_waits_for_the_cooldown_instead(model):
    out, _ = _render_one(model, "_FF_NOZZLE_WIPE",
                         {"TOOL": "1", "TEMP": "220"}, extra=_wipe_printer())
    assert not any(c.startswith("G4") for c in out)
    assert any(c.startswith("TEMPERATURE_WAIT") for c in out)


# -- one bed mesh selector -------------------------------------------------

@pytest.mark.parametrize("model", MODELS)
def test_bed_mesh_mode_sets_one_of_five_and_refuses_the_rest(model):
    out, info = _run(model, "BED_MESH_MODE MODE=Adaptive", has_heater=False)
    assert out == ["SET_GCODE_VARIABLE MACRO=BED_MESH_MODE VARIABLE=mode "
                   "VALUE=\"'adaptive'\""]
    _out, info = _run(model, "BED_MESH_MODE", has_heater=False)
    assert "Bed mesh mode: auto" in info[0]
    with pytest.raises(AssertionError, match="must be one of"):
        _run(model, "BED_MESH_MODE MODE=level", has_heater=False)


def _mesh_printer(objects=(), tower=True):
    return {"exclude_object": {"objects": [{"name": n} for n in objects]},
            "ff_print": {"prime_tower_x": 100.0 if tower else None}}


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("mode, expected", [
    ("off", ["BED_MESH_CLEAR"]),
    ("default", ["BED_MESH_CLEAR", "BED_MESH_PROFILE LOAD=MESH_DATA"]),
    ("adaptive", ["BED_MESH_CLEAR", "_FF_MESH_ADAPTIVE"]),
    ("auto", ["BED_MESH_CLEAR",
              "THERMAL_MESH_APPLY PREPARE=0 METHOD=bilinear MATCH=absolute"
              " CORRECT=none MAX_RMS=0.05 FALLBACK=NONE",
              "_FF_MESH_AUTO_FALLBACK"]),
    ("thermal", ["BED_MESH_CLEAR",
                 "THERMAL_MESH_APPLY PREPARE=0 METHOD=bilinear MATCH=absolute"
                 " CORRECT=none FALLBACK=MESH_DATA"]),
])
def test_the_mesh_step_for_each_mode(model, mode, expected):
    out, _ = _render_one(model, "_FF_MESH", {"MODE": mode},
                         extra=_mesh_printer())
    assert out == expected


@pytest.mark.parametrize("model", MODELS)
def test_a_prime_tower_the_file_already_defined_is_not_defined_twice(model):
    out, _ = _render_one(model, "_FF_MESH_ADAPTIVE", {},
                         extra=_mesh_printer(objects=["PRIME_TOWER", "CUBE"]))
    assert "DEFINE_PRIME_TOWER_OBJECT" not in out
    out, _ = _render_one(model, "_FF_MESH_ADAPTIVE", {},
                         extra=_mesh_printer(tower=False))
    assert "DEFINE_PRIME_TOWER_OBJECT" not in out


@pytest.mark.parametrize("model", MODELS)
def test_the_adaptive_probe_defines_objects_and_tower_first(model):
    out, _ = _render_one(model, "_FF_MESH_ADAPTIVE", {},
                         extra=_mesh_printer())
    assert out == ["FF_PRINT_DEFINE_OBJECTS", "DEFINE_PRIME_TOWER_OBJECT",
                   "M106 P1 S0",
                   "BED_MESH_CALIBRATE ADAPTIVE=1 ADAPTIVE_MARGIN=16",
                   "SET_VELOCITY_LIMIT ACCEL=20000"]


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("result, probes", [
    ({"accepted": True, "reason": "", "rms": 0.012}, False),
    ({"accepted": False, "reason": "no map in thermal_mesh.json"}, True),
    ({"accepted": False, "reason": "0.0753 mm rms off, more than MAX_RMS"},
     True),
])
def test_auto_falls_back_to_adaptive_only_when_thermal_is_not_accepted(
        model, result, probes):
    out, info = _render_one(model, "_FF_MESH_AUTO_FALLBACK", {}, extra={
        "ff_thermal_mesh": {"last_apply": result}})
    assert out == (["_FF_MESH_ADAPTIVE"] if probes else [])
    assert ("probing adaptive" in info[0]) == probes


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("params, mode", [
    ({}, "auto"),                          # BED_MESH_MODE's own
    ({"MESH": "Thermal"}, "thermal"),      # this print only
    ({"LEVEL": "1"}, "adaptive"),          # the old full probe
    ({"MESH": "off"}, "off"),
])
def test_start_print_meshes_by_the_selector_or_its_own_mesh_param(
        model, params, mode):
    base = {"BED": "60", "TOOL": "0", "NOZZLE": "220", "LAYER": "0.2"}
    out, info = _render_one(model, "START_PRINT", dict(base, **params))
    assert out.count("_FF_MESH MODE=%s" % mode) == 1
    # Mesh before the last clean, while every tool is docked.
    assert _first(out, "_FF_MESH") < _first(out, "_FF_NOZZLE_CLEAN MODE=print")
    flow = any(c.startswith("_FF_FLOW_MESH_BEGIN") for c in out)
    assert flow == (mode != "off")
    if mode == "off":
        assert any("no flow mesh" in m for m in info)


@pytest.mark.parametrize("model", MODELS)
def test_adaptive_mesh_is_retired_to_a_harmless_message(model):
    out, info = _render_one(model, "ADAPTIVE_MESH", {"TOOL": "0"})
    assert out == []
    assert "START_PRINT took the mesh" in info[0]


@pytest.mark.parametrize("model", MODELS)
def test_no_mesh_toggles_or_defer_are_left(model):
    cp = _parse(model)
    for gone in ("gcode_macro ADAPTIVE_MESH_TOGGLE",
                 "gcode_macro ADAPTIVE_MESH_STATUS",
                 "gcode_macro THERMAL_MESH_TOGGLE", "gcode_macro _FF_SAVED_MESH"):
        assert not cp.has_section(gone)
    assert not cp.has_option("gcode_macro FF_BEFORE_PRINT_START",
                             "variable_defer_mesh")
    assert "DEFER_MESH" not in cp.get("gcode_macro START_PRINT", "gcode")
    assert cp.has_section("gcode_macro FLOW_MESH_TOGGLE")
