# Creator 5 bed-mesh probing adapter
#
# Copyright (C) 2026
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Shorten contact-probe travel for the Creator 5 bed mesh.

Klipper normally raises Z to one fixed absolute height before every bed-mesh
XY move, retracts a fixed sample_retract_dist between samples, and travels at
bed_mesh's own speed. On the Creator 5 the Z part of that is most of the
mesh's time: the probe descends at 3 mm/s, so every millimetre lifted costs a
third of a second on the way back down, per sample, per point.

The only lift a touch needs is enough for the probe to be ready to trigger
again. This adapter lifts release_distance + release_margin above the last
trigger between samples at one point, and at least min_travel_lift before
moving on to the next point, where a warped plate can be higher.
release_distance is that ready distance: for a held switch, how far above
its trigger it opens again; for a momentary trigger like the Creator 5's
(the eboard pulses the line on an eddy-current tap), the smallest start
height from which touches still land where touches from a few mm do.
FF_PROBE_RELEASE_TEST finds out which kind it is, measures it, and
SAVE_CONFIG keeps it. XY travel runs at travel_speed and
travel_accel, the fastest any built-in command moves the carriage (the tool
change's fast_feed, the print acceleration START_PRINT restores), for the
length of the probe run only. Z speeds and accelerations are left as
configured.

Sampling, when `samples` is set: each point is touched `samples` times (2).
If those disagree by more than [probe] samples_tolerance, it touches again,
up to `tiebreak_samples` more (1), and accepts the closest `samples` of all
the touches as soon as they agree. Only when none do are they all thrown
away and the point started over, counting against
samples_tolerance_retries. A clean point therefore costs two touches and one
bad touch costs one more, where stock Klipper would discard both good
touches with it. PROBE, homing and the tool calibrations keep the [probe]
section's own count and its standard sampling.

The first transfer still goes to a safe absolute height, and a calculated
target below low_height_threshold falls back to recovery_move_z.

The adapter is attached to bed_mesh's ProbePointsHelper, and to any other
helper that asks for it (ff_thermal_mesh's four-point probe). It keeps
Klipper's point order, and homing and other probe users are unchanged.
"""

import logging


class FFBedMesh:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name()
        self.first_move_z = config.getfloat('first_move_z', 5., minval=0.)
        # Not in the shipped cfg on purpose: FF_PROBE_RELEASE_TEST SAVE=1
        # writes it to printer.cfg's SAVE_CONFIG block, which Klipper refuses
        # for an option an included file also sets. 1.8 + 0.2 is the 2 mm the
        # adapter lifted before the release point was measured.
        self.release_distance = config.getfloat(
            'release_distance', 1.8, minval=0.)
        self.release_margin = config.getfloat('release_margin', .2, above=0.)
        self.min_travel_lift = config.getfloat('min_travel_lift', 1.,
                                               minval=0.)
        self.travel_speed = config.getfloat('travel_speed', None, above=0.)
        self.travel_accel = config.getfloat('travel_accel', None, above=0.)
        self.samples = config.getint('samples', None, minval=1)
        self.tiebreak_samples = config.getint('tiebreak_samples', 1,
                                              minval=0)
        self.low_height_threshold = config.getfloat(
            'low_height_threshold', 1.)
        self.recovery_move_z = config.getfloat(
            'recovery_move_z', 3., minval=0.)
        if self.first_move_z <= self.low_height_threshold:
            raise config.error(
                "ff_bed_mesh: first_move_z must be above "
                "low_height_threshold")
        if self.recovery_move_z <= self.low_height_threshold:
            raise config.error(
                "ff_bed_mesh: recovery_move_z must be above "
                "low_height_threshold")

        bed_mesh = self.printer.lookup_object('bed_mesh', None)
        if bed_mesh is None:
            raise config.error(
                "[ff_bed_mesh] must be configured after [bed_mesh]")
        try:
            self.probe_helper = bed_mesh.bmc.probe_mgr.probe_helper
        except AttributeError:
            raise config.error(
                "[ff_bed_mesh] is incompatible with this Klipper bed_mesh")

        # Patch only this bed-mesh helper instance. Other users of the probe
        # retain their standard Klipper behavior and point ordering.
        self.attach(self.probe_helper)
        self.gcode.register_command(
            'FF_BED_MESH_STATUS', self.cmd_FF_BED_MESH_STATUS,
            desc='Show Creator 5 bed-mesh probing travel')
        self.gcode.register_command(
            'FF_PROBE_RELEASE_TEST', self.cmd_FF_PROBE_RELEASE_TEST,
            desc='Measure how far the probe lifts before its switch opens')
        logging.info(
            "ff_bed_mesh: installed; first_z=%.3f lift=%.3f (release %.3f + "
            "margin %.3f) travel_speed=%s travel_accel=%s threshold=%.3f "
            "recovery_z=%.3f",
            self.first_move_z, self.lift(), self.release_distance,
            self.release_margin, self.travel_speed, self.travel_accel,
            self.low_height_threshold, self.recovery_move_z)

    def lift(self):
        return self.release_distance + self.release_margin

    def attach(self, helper):
        """Give a ProbePointsHelper this adapter's travel and lift."""
        if self.travel_speed is not None:
            # helper.speed is the XY travel between points; it is also the
            # first Z move's speed, which the toolhead clamps to
            # max_z_velocity exactly as it did before.
            helper.speed = self.travel_speed
        helper._raise_tool = lambda is_first=False: self._raise_tool(
            helper, is_first)
        start_probe = helper.start_probe

        def start_probe_with_lift(gcmd):
            # The retract between samples at one point, and how many there
            # are. Either given explicitly on the command still wins.
            params = gcmd.get_command_parameters()
            if 'SAMPLE_RETRACT_DIST' not in params:
                params['SAMPLE_RETRACT_DIST'] = '%.3f' % (self.lift(),)
            if self.samples is not None and 'SAMPLES' not in params:
                params['SAMPLES'] = '%d' % (self.samples,)
            probe = self.printer.lookup_object('probe', None)
            restore_accel = restore_session = None
            if self.travel_accel is not None:
                # For the probe run only, put back afterwards even when it
                # fails. Probing moves are Z, which max_z_accel limits on
                # its own.
                toolhead = self.printer.lookup_object('toolhead')
                eventtime = self.printer.get_reactor().monotonic()
                restore_accel = toolhead.get_status(eventtime)['max_accel']
                self._set_accel(self.travel_accel)
            if self.samples is not None and probe is not None:
                # Only this run's session samples the tiebreak way: the
                # instance attribute shadows the method for its duration.
                restore_session = vars(probe).get('start_probe_session')
                open_session = probe.start_probe_session
                probe.start_probe_session = lambda g: TiebreakSession(
                    self, open_session(g))
            try:
                return start_probe(gcmd)
            finally:
                if self.samples is not None and probe is not None:
                    if restore_session is None:
                        del probe.start_probe_session
                    else:
                        probe.start_probe_session = restore_session
                if restore_accel is not None:
                    self._set_accel(restore_accel)
        helper.start_probe = start_probe_with_lift

    def _set_accel(self, accel):
        self.gcode.run_script_from_command(
            'SET_VELOCITY_LIMIT ACCEL=%.0f' % (accel,))

    def _raise_tool(self, helper, is_first=False):
        toolhead = self.printer.lookup_object('toolhead')
        current_z = toolhead.get_position()[2]
        speed = helper.speed if is_first else helper.lift_speed

        if is_first:
            target_z = max(self.first_move_z, current_z)
        else:
            # Recalculate after every trigger. Recovery is therefore not
            # latched: as soon as trigger_z + lift is safe again, the
            # following transfer automatically returns to the lower height.
            target_z = current_z + max(self.lift(), self.min_travel_lift)
            if target_z < self.low_height_threshold:
                target_z = self.recovery_move_z
            target_z = max(target_z, current_z + .001)
        helper._move([None, None, target_z], speed)

    def get_status(self, eventtime):
        return {
            'path': 'standard',
            'dynamic_z': True,
            'first_move_z': self.first_move_z,
            'release_distance': self.release_distance,
            'release_margin': self.release_margin,
            'lift': self.lift(),
            'min_travel_lift': self.min_travel_lift,
            'travel_speed': self.travel_speed,
            'travel_accel': self.travel_accel,
            'samples': self.samples,
            'tiebreak_samples': self.tiebreak_samples,
            'low_height_threshold': self.low_height_threshold,
            'recovery_move_z': self.recovery_move_z,
        }

    def cmd_FF_BED_MESH_STATUS(self, gcmd):
        gcmd.respond_info(
            "FF bed mesh: path=standard, dynamic_z=yes\n"
            "lift=%.3f (release_distance %.3f + release_margin %.3f) between "
            "samples, at least %.3f between points; samples per point: %s, "
            "tiebreak: up to %d more\n"
            "travel_speed=%s, travel_accel=%s, first_move_z=%.3f, "
            "low_height_threshold=%.3f, recovery_move_z=%.3f\n"
            "Recovery is evaluated per point and is not latched."
            % (self.lift(), self.release_distance, self.release_margin,
               self.min_travel_lift,
               '[probe] default' if self.samples is None else self.samples,
               self.tiebreak_samples if self.samples is not None else 0,
               'bed_mesh speed' if self.travel_speed is None
               else '%.0f mm/s' % self.travel_speed,
               'unchanged' if self.travel_accel is None
               else '%.0f mm/s^2' % self.travel_accel,
               self.first_move_z, self.low_height_threshold,
               self.recovery_move_z))

    # FF_PROBE_RELEASE_TEST [SAMPLES=5] [STEP=0.01] [MAX=3]
    #     [LIFTS=0.1,0.2,0.3,0.5,0.75,1,1.5,2] [SAVE=0]
    def cmd_FF_PROBE_RELEASE_TEST(self, gcmd):
        samples = gcmd.get_int('SAMPLES', 5, minval=1)
        step = gcmd.get_float('STEP', 0.01, above=0.)
        limit = gcmd.get_float('MAX', 3., above=0.)
        try:
            lifts = sorted(float(v) for v in gcmd.get(
                'LIFTS', '0.1,0.2,0.3,0.5,0.75,1,1.5,2').split(',')
                if v.strip())
        except ValueError:
            raise gcmd.error("ff_bed_mesh: LIFTS is a list of mm, "
                             "e.g. 0.2,0.5,1")
        probe = self.printer.lookup_object('probe', None)
        if probe is None:
            raise gcmd.error("ff_bed_mesh: no [probe]")
        test = _ReleaseTest(self.printer, self.gcode, probe, limit)
        test.touch()
        if test.triggered():
            gcmd.respond_info("probe holds its trigger after stopping: "
                              "measuring where it opens")
            distance = test.measure_release(gcmd, samples, step)
        else:
            gcmd.respond_info(
                "probe released as soon as it stopped: a momentary trigger, "
                "not a held switch. Finding the lowest start height that "
                "still touches where a %.1f mm start does" % (limit,))
            distance = test.measure_ready_lift(gcmd, samples, lifts)
        self.release_distance = round(distance, 3)
        msg = ("release_distance=%.3f for this session: %.3f mm between "
               "samples, at least %.3f mm between points."
               % (self.release_distance, self.lift(),
                  max(self.lift(), self.min_travel_lift)))
        if gcmd.get_int('SAVE', 0):
            configfile = self.printer.lookup_object('configfile')
            configfile.set(self.name, 'release_distance',
                           '%.3f' % (self.release_distance,))
            msg += " SAVE_CONFIG keeps it."
        else:
            msg += " Run with SAVE=1, then SAVE_CONFIG, to keep it."
        gcmd.respond_info(msg)


class _ReleaseTest:
    """Touches at one X/Y, every lift a pure Z move from where it stopped."""

    def __init__(self, printer, gcode, probe, limit):
        self.printer = printer
        self.probe = probe
        self.limit = limit
        self.toolhead = printer.lookup_object('toolhead')
        self.lift_speed = probe.get_probe_params()['lift_speed']
        self.tolerance = probe.get_probe_params()['samples_tolerance']
        self.one = gcode.create_gcode_command(
            '', '', {'SAMPLES': '1', 'SAMPLE_RETRACT_DIST': '%.3f' % limit})
        self.last = None

    def touch(self):
        session = self.probe.start_probe_session(self.one)
        try:
            session.run_probe(self.one)
            self.last = session.pull_probed_results()[0][2]
        finally:
            session.end_probe_session()
        return self.last

    def lift_to(self, z):
        x, y = self.toolhead.get_position()[:2]
        self.toolhead.manual_move([x, y, z], self.lift_speed)

    def triggered(self):
        self.toolhead.wait_moves()
        return self.probe.mcu_probe.query_endstop(
            self.toolhead.get_last_move_time())

    def measure_release(self, gcmd, samples, step):
        """A held switch: step up from each trigger until it opens."""
        releases = []
        for n in range(samples):
            if n:
                self.lift_to(self.last + self.limit)
                self.touch()
            trigger_z = z = self.last
            while self.triggered():
                if z - trigger_z >= self.limit:
                    raise gcmd.error(
                        "ff_bed_mesh: probe still triggered %.2f mm above "
                        "the trigger; check it, or raise MAX" % (self.limit,))
                z += step
                self.lift_to(z)
            releases.append(z - trigger_z)
            gcmd.respond_info("sample %d: triggered at z=%.4f, opened %.3f mm "
                              "above" % (n + 1, trigger_z, releases[-1]))
        self.lift_to(self.last + self.limit)
        worst = max(releases)
        gcmd.respond_info("probe opens up to %.3f mm above its trigger "
                          "(mean %.3f over %d)"
                          % (worst, sum(releases) / len(releases), samples))
        return worst + step

    def measure_ready_lift(self, gcmd, samples, lifts):
        """A momentary trigger: the lowest start that still repeats."""
        reference = []
        for _ in range(samples):
            self.lift_to(self.last + self.limit)
            reference.append(self.touch())
        ref = sum(reference) / len(reference)
        spread = max(reference) - min(reference)
        gcmd.respond_info("from %.1f mm: z=%.4f, spread %.4f over %d"
                          % (self.limit, ref, spread, samples))
        if spread > self.tolerance:
            raise gcmd.error(
                "ff_bed_mesh: touches from %.1f mm already spread %.4f, more "
                "than samples_tolerance %.4f; nothing to compare against"
                % (self.limit, spread, self.tolerance))
        for lift in lifts:
            if lift >= self.limit:
                break
            worst, failure = 0., None
            for _ in range(samples):
                self.lift_to(self.last + lift)
                try:
                    z = self.touch()
                except self.printer.command_error as e:
                    failure = str(e)
                    break
                worst = max(worst, abs(z - ref))
                if worst > self.tolerance:
                    break
            self.lift_to(self.last + self.limit)
            if failure is not None:
                gcmd.respond_info("from %.2f mm: failed (%s)" % (lift, failure))
            elif worst > self.tolerance:
                gcmd.respond_info("from %.2f mm: off by %.4f" % (lift, worst))
            else:
                gcmd.respond_info("from %.2f mm: within %.4f over %d -- ready"
                                  % (lift, worst, samples))
                return lift
        raise gcmd.error(
            "ff_bed_mesh: no start height below %.1f mm repeated within "
            "%.4f; keep the default" % (self.limit, self.tolerance))


def closest_agreeing(positions, count, tolerance):
    """The `count` positions whose Z spread least, if within tolerance."""
    if len(positions) < count:
        return None
    ordered = sorted(positions, key=lambda p: p[2])
    best = None
    for i in range(len(ordered) - count + 1):
        window = ordered[i:i + count]
        spread = window[-1][2] - window[0][2]
        if spread <= tolerance and (best is None or spread < best[0]):
            best = (spread, window)
    return None if best is None else best[1]


def combine(positions, method):
    """klippy probe.calc_probe_z_average, for one point's samples."""
    if method != 'median':
        return [sum(p[i] for p in positions) / len(positions)
                for i in range(3)]
    ordered = sorted(positions, key=lambda p: p[2])
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return list(ordered[middle])
    return combine(ordered[middle - 1:middle + 1], 'average')


class TiebreakSession:
    """A probe session whose run_probe samples with a tiebreak.

    Everything except run_probe is the real session's: the results list,
    pull_probed_results and end_probe_session. Each touch is the fork's own
    ProbeSessionHelper._probe, so its pre-touch "already triggered" checks
    and its reporting are unchanged.
    """
    # The fork returns 9999 for a touch that found the switch closed before
    # it moved, and retries it forever; this gives up after a few.
    MAX_PRETRIGGERED = 5

    def __init__(self, adapter, session):
        self.adapter = adapter
        self.session = session

    def __getattr__(self, name):
        return getattr(self.session, name)

    def run_probe(self, gcmd):
        session = self.session
        params = session.get_probe_params(gcmd)
        count = params['samples']
        tolerance = params['samples_tolerance']
        limit = count + self.adapter.tiebreak_samples
        toolhead = self.adapter.printer.lookup_object('toolhead')
        retries = pretriggered = 0
        positions = []
        while True:
            pos = session._probe(params['probe_speed'])
            if pos == 9999:
                pretriggered += 1
                if pretriggered >= self.MAX_PRETRIGGERED:
                    raise gcmd.error("Probe still triggered before %d touches"
                                     " in a row" % (pretriggered,))
                continue
            positions.append(pos)
            chosen = closest_agreeing(positions, count, tolerance)
            if chosen is not None:
                break
            if len(positions) >= limit:
                if retries >= params['samples_tolerance_retries']:
                    raise gcmd.error("Probe samples exceed samples_tolerance")
                gcmd.respond_info("Probe samples exceed tolerance. "
                                  "Retrying...")
                retries += 1
                positions = []
            elif len(positions) >= count:
                gcmd.respond_info("Probe samples %.4f apart; one more"
                                  % (max(p[2] for p in positions)
                                     - min(p[2] for p in positions),))
            # Straight up from where it stopped, not back to the point's own
            # X/Y: the stop is a few microns off it, and a move that is not purely
            # vertical is judged by ff_bed_guard from its low end -- the
            # touch itself, which on a low spot is below the bed plane.
            here = toolhead.get_position()[:2]
            toolhead.manual_move(
                here + [pos[2] + params['sample_retract_dist']],
                params['lift_speed'])
        session.results.append(combine(chosen, params['samples_result']))


def load_config(config):
    return FFBedMesh(config)
