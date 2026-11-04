"""Creator 5 print-file metadata tests."""

import importlib.util

import pytest

from lib.paths import ROOT


MODULE = (ROOT / "pkgs" / "klipper" / "payload" / "klipper" /
          "klippy" / "extras" / "ff_print.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("ff_print_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_automatic_prime_tower_brim_is_resolved_from_moves(tmp_path):
    gcode = tmp_path / "automatic-brim.gcode"
    gcode.write_text(
        """T0
;TYPE:Prime tower
G1 X9.645 Y195.626
G1 X37.645
G1 Y222.646
G1 X9.645
G1 Y195.626
; WIPE_TOWER_BRIM_START
G1 X9.199 Y195.180
G1 X38.091
G1 Y223.092
G1 X7.413 Y193.394
G1 X39.877
G1 Y224.878
G1 X7.413
G1 Y193.394
; WIPE_TOWER_BRIM_END
; wipe_tower_x = 16.4744
; wipe_tower_y = 221.74
; prime_tower_width = 28
; prime_tower_brim_width = -1
; wipe_tower_rotation_angle = 0
""",
        encoding="utf-8")

    metadata = _load_module()._parse_metadata(str(gcode))

    assert metadata["prime_tower_brim"] == pytest.approx(2.232)
    assert metadata["prime_tower_outer_min_x"] == pytest.approx(7.413)
    assert metadata["prime_tower_outer_max_x"] == pytest.approx(39.877)
    assert metadata["prime_tower_outer_min_y"] == pytest.approx(193.394)
    assert metadata["prime_tower_outer_max_y"] == pytest.approx(224.878)


OBJECTS = """; HEADER_BLOCK_START
; EXECUTABLE_BLOCK_START
EXCLUDE_OBJECT_DEFINE NAME=cube_id_0_copy_0 CENTER=100,100 POLYGON=[[90,90],[110,90],[110,110],[90,110]]
EXCLUDE_OBJECT_DEFINE NAME=cone_id_1_copy_0 CENTER=150,60 POLYGON=[[140,50],[160,50],[160,70],[140,70]]
M140 S60
T0
;HEIGHT:0.2
EXCLUDE_OBJECT_START NAME=cube_id_0_copy_0
"""


def test_the_files_object_definitions_are_collected(tmp_path):
    gcode = tmp_path / "objects.gcode"
    gcode.write_text(OBJECTS, encoding="utf-8")
    metadata = _load_module()._parse_metadata(str(gcode))
    assert [d.split()[1] for d in metadata["object_defines"]] == [
        "NAME=cube_id_0_copy_0", "NAME=cone_id_1_copy_0"]


class _Gcode:
    def __init__(self):
        self.commands, self.scripts = {}, []

    def register_command(self, name, func, desc=None):
        self.commands[name] = func

    def run_script_from_command(self, script):
        self.scripts.append(script)


class _Gcmd:
    def __init__(self):
        self.info = []

    def respond_info(self, msg):
        self.info.append(msg)


def _ff_print(objects_known):
    from types import SimpleNamespace
    gcode = _Gcode()
    exclude = SimpleNamespace(objects=[{"name": n} for n in objects_known])
    printer = SimpleNamespace(
        get_reactor=lambda: None,
        lookup_object=lambda name, default=None: {
            "gcode": gcode, "exclude_object": exclude}.get(name, default),
        register_event_handler=lambda *a: None)
    config = SimpleNamespace(get_printer=lambda: printer,
                             get=lambda o, d=None: d,
                             getlist=lambda o, d=None: d)
    obj = _load_module().FFPrint(config)
    return obj, gcode


def test_objects_are_defined_ahead_of_the_file_for_the_mesh(tmp_path):
    gcode_file = tmp_path / "objects.gcode"
    gcode_file.write_text(OBJECTS, encoding="utf-8")
    obj, gcode = _ff_print([])
    obj.metadata = _load_module()._parse_metadata(str(gcode_file))
    gcmd = _Gcmd()
    gcode.commands["FF_PRINT_DEFINE_OBJECTS"](gcmd)
    assert len(gcode.scripts) == 2
    assert gcode.scripts[0].startswith(
        "EXCLUDE_OBJECT_DEFINE NAME=cube_id_0_copy_0")
    assert "2 object(s)" in gcmd.info[0]


def test_objects_the_file_already_defined_are_left_alone(tmp_path):
    gcode_file = tmp_path / "objects.gcode"
    gcode_file.write_text(OBJECTS, encoding="utf-8")
    obj, gcode = _ff_print(["CUBE_ID_0_COPY_0"])
    obj.metadata = _load_module()._parse_metadata(str(gcode_file))
    gcode.commands["FF_PRINT_DEFINE_OBJECTS"](_Gcmd())
    assert gcode.scripts == []


def test_a_file_without_objects_meshes_the_whole_bed():
    obj, gcode = _ff_print([])
    gcmd = _Gcmd()
    gcode.commands["FF_PRINT_DEFINE_OBJECTS"](gcmd)
    assert gcode.scripts == [] and "whole bed" in gcmd.info[0]
