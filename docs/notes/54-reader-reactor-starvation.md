# The SD reader starves the reactor into "Timer too close"

**Status: fixed.** `pkgs/klipper` overlays `virtual_sdcard.py` with a reader
that yields to the reactor at least every 20 ms
(`READER_YIELD_INTERVAL`); `qa/static/test_virtual_sdcard.py` holds it. Not
yet confirmed on the printer by a print that reproduces the failure.

A Creator 5 Pro shut down mid-print with `MCU 'eboard' shutdown: Timer too
close` and, 4 ms later, the same from the main MCU. Both reported their
late timer: 8.13M ticks at 144 MHz and 31.4M at 600 MHz, **56 and 52 ms in
the past**. Nothing was cancelled, the host load was 1.1, and memory was
fine. It was six seconds after an ordinary tool change, one of ~200 in the
print.

## The chain

1. The tool change's prime-tower block ends with Orca's `G4 S0`. A dwell
   flushes the lookahead, which leaves the toolhead in `NeedPrime` with the
   lookahead's flush threshold raised to `BUFFER_TIME_HIGH`, 2.0 s
   (`toolhead._flush_lookahead`).
2. The first move after that puts it in `Priming` and arms a 100 ms priming
   timer -- and every further move re-arms it (`toolhead._check_pause`), so
   while moves keep coming the timer never fires. The lookahead releases
   moves only once the sum of their `min_move_t` reaches 2.0 s.
3. Until then `print_time` does not move, so the toolhead never pauses the
   reader (`buffer_time` is measured against the stale `print_time`), and
   the reader's only yield is upstream's one `reactor.pause(NOW)` per 8192-byte
   read.
4. The next G-code was T2's support interface: ~0.2 mm arcs, each with a
   ~1 mm travel. Summing `min_move_t` from the `G4 S0` on, 2.0 s is reached
   **440 lines later** (file line 328,172 to 328,614). On the printer that
   took ~2.3 s: ~5 ms a line through `gcode_arcs`, the move transforms and
   the lookahead. The `get_clock` timer ran 0.18 s, then 0.51 s late.
5. The dwell's own flush generated the tower's steps only up to
   `print_time - STEPCOMPRESS_FLUSH_TIME`; the last **50 ms** is left to the
   background flush timer, which the starved reactor could not run. When
   the 2.0 s finally flushed, that tail went out ~50 ms after its time --
   the 52/56 ms the MCUs reported, and the first rejected `queue_step` is
   exactly the start of the tower line's deceleration.

The host only acted on the shutdown six seconds later (`Transition to
shutdown state` at 186505.8 against the MCUs' 186499.46), still parsing.
So the dump's `Virtual sdcard` position, line 329,584, is not where it
failed; line ~328,614 is.

Why this tool change and not the 200 before it: after most of them the next
moves are walls or infill, and 2.0 s of `min_move_t` is a few dozen lines.
This one was followed by 1,400 lines of sub-millimetre dots.

## Same root as note 52

[Note 52](52-exclude-object-stall.md) is the other way to get here: a
cancelled object's moves are discarded, so nothing reaches the motion queue
and the reader again runs between 8 KB yields. Both are the reader holding
the reactor because the toolhead has no reason to pause it. Yielding on a
clock in the reader covers both, and any third way to get there.

## The fix

`work_handler` checks `reactor.monotonic()` before each line and calls
`reactor.pause(reactor.NOW)` once 20 ms have passed since the last yield (the
8 KB read and the mutex wait reset the clock). A `NOW` pause runs every due
timer -- the step flush, heater and soft-PWM updates, serial I/O -- and
returns. 20 ms is an order of magnitude inside the deadlines that failed
(the background flush works `BGFLUSH_LOW_TIME`, 0.2 s, ahead, and the
held-back tail is 50 ms), and it costs one reactor round trip per 20 ms of
parsing. Against the fork's copy the test's 1,400-line run goes 1.465 s
without a yield; with the overlay, 0.020 s.

The overlay is the pinned fork's file with only this added, so a bump of
`KLIPPER_VERSION` has to re-take it from the new tree.

## Not fixed here

The per-line cost itself. ~5 ms a line is what made 440 lines take 2.3 s,
and it is not measured term by term. `tools/gcode-bench/` can replay the
lines after the `G4 S0` to split it between `gcode_arcs`, the
flow-mesh/tool/bed-mesh transforms and the lookahead. On the slicer side,
arc fitting off and a support-interface pattern that prints lines instead
of dots make the same layer far fewer, cheaper moves.
