"""A pickup does not return while its unsynced lock move is still running.

grab2 locks the tool with MANUAL_STEPPER ... SYNC=0 so the ~1.5 s move
overlaps the exit from the dock. Its tail is sent only by the toolhead's
background flush, so when AFC's restore travel right after the change held
the reactor for 329 ms, the tail went out 61 ms late and the eboard shut
down with "Timer too close". _grab now waits for the lock before it returns.
The hardware actions are substituted; these tests check the order.
"""
import contextlib
import importlib.util
from types import SimpleNamespace

import pytest

from lib.paths import ROOT

pytestmark = pytest.mark.static

MODULE = (ROOT / "pkgs" / "klipper" / "payload" / "klipper" / "klippy" /
          "extras" / "ff_toolchange.py")


@pytest.fixture
def grabbing(monkeypatch):
    spec = importlib.util.spec_from_file_location("grab_ff_toolchange", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tc = module.FFToolchange.__new__(module.FFToolchange)
    calls = []
    steppers = {"manual_stepper gear_stepper": object()}
    tc.printer = SimpleNamespace(
        lookup_object=lambda name, default=None: steppers.get(name, default))
    tc.lock_stepper = "gear_stepper"
    tc.grab_macro = "FF_MOTOR_GRAB"
    tc.grab2_macro = "FF_MOTOR_GRAB2"
    tc.accel_move = 9600
    tc.x_safe = 250.
    docked = {"in_dock": True}

    def record(name, result=None):
        def call(*args, **kwargs):
            calls.append((name,) + args)
            return result
        return call

    def run(script):
        calls.append(("run", script))

    def carried_withdraw(dock_x, target_x):
        calls.append(("withdraw",))
        docked["in_dock"] = False

    monkeypatch.setattr(tc, "_run", run)
    monkeypatch.setattr(tc, "_wait_moves", record("wait_moves"))
    monkeypatch.setattr(tc, "_dock", lambda tool: (297., 105.))
    monkeypatch.setattr(tc, "_contact_exit", lambda dock_x: dock_x - 5.)
    monkeypatch.setattr(tc, "_in_location",
                        lambda tool, eventtime=None: docked["in_dock"])
    monkeypatch.setattr(tc, "_grab_sensor", lambda eventtime=None: True)
    monkeypatch.setattr(tc, "_poll_until",
                        lambda check, timeout: (calls.append(("poll",))
                                                or check()))
    monkeypatch.setattr(tc, "_snapshot_motion_state", contextlib.nullcontext)
    monkeypatch.setattr(tc, "_set_tool_frame", record("tool_frame"))
    monkeypatch.setattr(tc, "_move_to_dock_y", record("dock_y"))
    monkeypatch.setattr(tc, "_soft_seat", record("seat"))
    monkeypatch.setattr(tc, "_retract_in_dock", record("retract"))
    monkeypatch.setattr(tc, "_carried_withdraw", carried_withdraw)
    monkeypatch.setattr(tc, "_extruder_name", lambda tool: "extruder%d" % tool)
    monkeypatch.setattr(tc, "_arm_runout", record("arm"))
    return tc, calls, steppers


def test_the_lock_is_waited_for_after_the_exit_and_before_the_verify(grabbing):
    tc, calls, _ = grabbing
    tc._grab(1)
    sync = calls.index(("run", "MANUAL_STEPPER STEPPER=gear_stepper SYNC=1"))
    assert calls.index(("run", "FF_MOTOR_GRAB2")) < sync
    assert calls.index(("withdraw",)) < sync
    # SYNC=1 only moves the toolhead's print time to the lock's end; the
    # wait right after is what holds the sequence, reactor free, until the
    # whole lock move has been sent.
    assert calls[sync + 1] == ("wait_moves",)
    verify = max(i for i, c in enumerate(calls) if c == ("poll",))
    assert sync < verify
    assert calls.index(("run", "ACTIVATE_EXTRUDER EXTRUDER=extruder1")) > sync


def test_without_a_lock_stepper_nothing_is_waited_for(grabbing):
    tc, calls, steppers = grabbing
    steppers.clear()
    tc._grab(1)
    assert not any(c[0] == "run" and c[1].startswith("MANUAL_STEPPER")
                   for c in calls)
