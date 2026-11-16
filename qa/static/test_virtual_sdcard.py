"""The SD reader must hand the reactor a turn on a clock, not only per 8 KB.

Upstream's work_handler yields once per 8192-byte read and otherwise only
when the toolhead pauses it. Nothing pauses it while moves never reach the
motion queue -- the toolhead priming after a flush, or a cancelled object's
moves being discarded -- so on the printer's slow host a run of sub-mm moves
held the reactor for ~2 s and an MCU shut down with "Timer too close"
(docs/notes/54-reader-reactor-starvation.md).

These run the real work_handler against a fake reactor whose clock advances
by a fixed CPU cost per dispatched line: 5 ms, what the incident measured.
"""
import importlib.util
import io
import os

import pytest

from lib.paths import ROOT

pytestmark = pytest.mark.static

MODULE = os.path.join(
    ROOT, "pkgs", "klipper", "payload", "klipper", "klippy", "extras",
    "virtual_sdcard.py")

LINE_COST = 0.005


def load_module():
    spec = importlib.util.spec_from_file_location(
        "anvil_virtual_sdcard", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Reactor:
    NOW = 0.
    NEVER = 9999999999999999.

    def __init__(self):
        self.clock = 1000.
        self.yields = []          # clock at every pause()
        self.on_pause = None

    def monotonic(self):
        return self.clock

    def pause(self, waketime):
        self.yields.append(self.clock)
        if self.on_pause is not None:
            self.on_pause(len(self.yields))
        self.clock = max(self.clock, waketime)
        return self.clock

    def unregister_timer(self, timer):
        pass


class Mutex:
    def test(self):
        return False


class GCode:
    class error(Exception):
        pass

    def __init__(self, reactor):
        self.reactor = reactor
        self.lines = []

    def get_mutex(self):
        return Mutex()

    def run_script(self, script):
        self.lines.append(script)
        self.reactor.clock += LINE_COST

    def respond_raw(self, msg):
        pass


class PrintStats:
    def __init__(self):
        self.state = None

    def note_start(self):
        self.state = 'printing'

    def note_complete(self):
        self.state = 'complete'

    def note_pause(self):
        self.state = 'paused'

    def note_error(self, message):
        self.state = 'error'


def build_reader(text):
    module = load_module()
    reactor = Reactor()
    sd = module.VirtualSD.__new__(module.VirtualSD)
    sd.reactor = reactor
    sd.gcode = GCode(reactor)
    sd.print_stats = PrintStats()
    sd.current_file = io.StringIO(text)
    sd.file_position = 0
    sd.next_file_position = 0
    sd.must_pause_work = False
    sd.cmd_from_sd = False
    sd.work_timer = object()
    return module, sd


def dot_file(lines):
    # The incident's shape: ~25-byte support-interface lines, so one 8 KB
    # read holds ~330 of them -- 1.6 s of dispatch at LINE_COST.
    body = []
    for i in range(lines):
        body.append("G1 X163.218 Y%.3f F30000" % (100. + i * 0.001))
    return "\n".join(body) + "\n"


def max_gap(reactor, start, end):
    marks = [start] + reactor.yields + [end]
    return max(b - a for a, b in zip(marks, marks[1:]))


def test_reader_yields_on_a_clock_within_one_read():
    module, sd = build_reader(dot_file(1400))
    start = sd.reactor.clock
    sd.work_handler(start)

    assert sd.print_stats.state == 'complete'
    assert len(sd.gcode.lines) == 1400
    # One read holds ~330 lines; without the clock that is ~1.6 s with no
    # yield. With it no gap may exceed the interval plus one line's cost.
    gap = max_gap(sd.reactor, start, sd.reactor.clock)
    assert gap <= module.READER_YIELD_INTERVAL + LINE_COST + 1e-9, gap


def test_every_line_dispatched_once_in_order_with_positions():
    text = dot_file(500)
    module, sd = build_reader(text)
    sd.work_handler(sd.reactor.clock)
    assert sd.gcode.lines == text.split("\n")[:-1]
    assert sd.file_position == len(text.encode())


def test_a_pause_requested_during_a_yield_stops_before_the_next_line():
    module, sd = build_reader(dot_file(1400))

    def request_pause(count):
        # The first pause is the read's; stop at the third, a clock yield.
        if count == 3:
            sd.must_pause_work = True
    sd.reactor.on_pause = request_pause
    sd.work_handler(sd.reactor.clock)

    dispatched = len(sd.gcode.lines)
    assert 0 < dispatched < 1400
    assert sd.print_stats.state == 'paused'
    # The position is the next undispatched line, so a RESUME re-reads it.
    text = dot_file(1400)
    consumed = sum(len(l) + 1 for l in text.split("\n")[:dispatched])
    assert sd.file_position == consumed


def test_interval_is_well_inside_the_step_flush_deadline():
    # toolhead's background step flush runs BGFLUSH_LOW_TIME (0.2 s) ahead
    # of the steps it generates, holding back STEPCOMPRESS_FLUSH_TIME
    # (0.05 s); the incident's lateness was that 50 ms. The yield interval
    # must stay an order of magnitude below both.
    module = load_module()
    assert 0. < module.READER_YIELD_INTERVAL <= 0.025
