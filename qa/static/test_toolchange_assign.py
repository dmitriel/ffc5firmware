"""ASSIGN_TOOL: a tool's own number is accepted, a remap is refused.

There is no logical-to-physical table on this machine, so the identity
assignment is a no-op -- and it is exactly what a UI sends to reset its
mapping to the machine's. Any other number is refused, loudly, because
HelixScreen keeps a remap in its own table even when the printer refuses it.
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


@pytest.fixture
def toolchange():
    spec = importlib.util.spec_from_file_location("anvil_ff_toolchange", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FFToolchange.__new__(module.FFToolchange)


_REQUIRED = object()


def _gcmd(**params):
    """Klipper's GCodeCommand as far as ASSIGN_TOOL uses it: a parameter with
    no default is required, and an explicit default of None is returned."""
    said = []

    def get_int(name, default=_REQUIRED):
        if name in params:
            return int(params[name])
        if default is _REQUIRED:
            raise CommandError("Error on '%s': missing" % name)
        return default

    return SimpleNamespace(
        get=lambda name, default=None: params.get(name, default),
        get_int=get_int, error=CommandError,
        respond_info=said.append), said


@pytest.mark.parametrize("params", [{"TOOL": "T2", "N": "2"},
                                    {"T": "2", "N": "2"}])
def test_a_tool_keeps_its_own_number(toolchange, params):
    gcmd, said = _gcmd(**params)
    toolchange.cmd_ASSIGN_TOOL(gcmd)
    assert said == ["ASSIGN_TOOL: T2 already answers to 2"]


def test_a_remap_is_refused_and_says_the_screen_is_out_of_step(toolchange):
    gcmd, _ = _gcmd(TOOL="T2", N="0")
    with pytest.raises(CommandError, match="T2 cannot answer to T0") as err:
        toolchange.cmd_ASSIGN_TOOL(gcmd)
    assert "HelixScreen" in str(err.value)


def test_a_number_is_required(toolchange):
    gcmd, _ = _gcmd(TOOL="T2")
    with pytest.raises(CommandError):
        toolchange.cmd_ASSIGN_TOOL(gcmd)
