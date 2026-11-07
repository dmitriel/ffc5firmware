# Bed-collision guard for the FlashForge Creator 5 / 5 Pro.
#
# Nothing in Klipper knows where the bed is. position_min on Z is -10, and
# after G28 Z the coordinate is the carriage EDDY's trigger plane, not the
# bed: a mounted nozzle meets the plate at roughly machine Z +3.2 (see
# docs/notes/40-offsets.md). A jog, a macro or a hand-typed G1 can drive the
# nozzle -- or the bare carriage -- straight into the plate, and the stepper
# will happily do it.
#
# This refuses any move that would bring whatever is lowest on the carriage
# within min_clearance of the bed, anywhere over the bed's footprint.
#
# WHERE THE BED IS. FlashForge publishes no CAD for the Creator 5's bed; the
# spec sheet's 256 x 256 mm is the printable area, not the plate. The
# footprint defaults to a square of -7..263 on X and Y (bed_min/bed_max),
# which covers the plate with margin. Y's travel (-5..260) lies entirely
# inside it, so in practice only X > 263 -- the dock bay, the purge chute
# and the wipe spot at X 266.5 -- is off the bed.
#
# HOW HIGH THE BED IS, in machine (raw toolhead) coordinates:
#   * bare carriage: carriage_contact_z. The station measures the carriage
#     at station_z and each nozzle at nozzle_z; the per-tool frame puts the
#     nozzle on the plate at nozzle_z - station_z, which places the
#     carriage's own underside at ~0. Default 0.0.
#   * a mounted tool: the Z=0 of its frame -- offset_z[tool] + job_z, the
#     same numbers _ToolTransform adds, so a G-code Z of 0.1 (before the
#     operator's babystep) is exactly the limit. A tool without a usable
#     calibration falls back to carriage_contact_z + nozzle_gap + z_adjust.
#   * an active bed mesh is added at the move's X/Y: the mesh is the
#     plate's measured shape, and bed_mesh has already added it to the move.
#     Under tilt correction the frame carries the plate's fitted plane and
#     bed_mesh only the rest; the measured mesh (ResidualMesh.plate) is
#     still the plate, so that is what is compared against.
# The carriage and nozzle limits are both checked; the higher one wins.
#
# MESH TOLERANCE. bed_mesh does not follow its mesh exactly: it splits a
# move only where the correction changes by more than its split_delta_z
# (0.025 mm by default) and goes straight between the pieces, so the nozzle
# sags up to that far below the mesh curve on a long first-layer line. The
# guard compares against the curve itself, so with a mesh active it allows
# mesh_tolerance (default: bed_mesh's split_delta_z) on top of
# min_clearance. Without it a 0.12 mm first layer was refused mid-print at
# 0.085 mm -- bed_mesh doing what it was configured to do.
#
# WHAT IS EXEMPT
#   * homing and probing moves: they stop on a trigger, which is the whole
#     point of them, and their target is position_min. Every one of them is
#     a toolhead drip move, so they are told apart by the toolhead's "Drip"
#     queuing state -- which drip_move always leaves -- rather than by the
#     homing:* events, whose *_end is not sent when a homing fails and
#     would leave the guard switched off.
#   * anything inside suspended(): TOOL_CALIBRATE_TOOL_OFFSET and
#     TOOL_LOCATE_SENSOR, whose station sits BELOW the bed plane and which
#     run with the plate removed (ff_tool_offset.py).
#   * a pure Z raise: moving away from the bed is never refused, so a head
#     left low by an exempt sequence can always be lifted out. "Pure" allows
#     RAISE_XY_TOLERANCE of X/Y: a probe stops a few microns off its point
#     (stepper rounding), and a retract back to the point's exact X/Y would
#     otherwise be judged from its low end -- the touch itself, which on a
#     low spot of the plate is below the bed plane.
#   * an unhomed Z: there is no position to compare. An unhomed X or Y is
#     treated as over the bed.
#   * SET_BED_GUARD ENABLE=0, until ENABLE=1 or a restart.
#
# THE HOOK. toolhead.move() calls kin.check_move() for every kinematic move
# -- G-code, macros, manual_move, other extras -- so the guard wraps that
# one method on the kinematics instance at connect, after the kinematics'
# own range check. It sees raw machine coordinates, BELOW every move
# transform (tool frame, babystep, bed mesh), which is why the mesh is added
# to the limit rather than taken off the move.

import logging
import math

# Mesh sampling along the part of a move that is over the bed. bed_mesh
# already splits its moves at 5 mm by default; this only matters for the
# moves it does not split (pure Z, or a mesh with splitting off).
SAMPLE_STEP = 5.0
MAX_SAMPLES = 200
EPSILON = 1e-6
RAISE_XY_TOLERANCE = 0.05


class FFBedGuard:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name()

        self.min_clearance = config.getfloat('min_clearance', 0.1, minval=0.)
        bed_min = config.getfloatlist('bed_min', (-7., -7.), count=2)
        bed_max = config.getfloatlist('bed_max', (263., 263.), count=2)
        if bed_min[0] >= bed_max[0] or bed_min[1] >= bed_max[1]:
            raise config.error("%s: bed_min must be below bed_max on both"
                               " axes" % self.name)
        self.bed_min = tuple(bed_min)
        self.bed_max = tuple(bed_max)
        self.carriage_contact_z = config.getfloat('carriage_contact_z', 0.0)
        # Nozzle below the carriage's contact plane for a tool with no
        # usable calibration (the measured gap is ~3.1-3.2 on both units we
        # have numbers for).
        self.nozzle_gap = config.getfloat('nozzle_gap', 3.2, minval=0.)
        self.enabled = config.getboolean('enable', True)
        self.mesh_tolerance = config.getfloat('mesh_tolerance', None,
                                              minval=0.)

        self.kin = None
        self.mesh_cache = (None, 0.)
        self.suspend_reasons = []
        self.toolchange = None
        self.bed_mesh = None
        self.blocked = 0
        self.last_block = None

        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)
        self.gcode.register_command('SET_BED_GUARD', self.cmd_SET_BED_GUARD,
                                    desc=self.cmd_SET_BED_GUARD_help)

    # ---------------- plumbing ----------------

    def _handle_connect(self):
        self.toolchange = self.printer.lookup_object('ff_toolchange', None)
        self.bed_mesh = self.printer.lookup_object('bed_mesh', None)
        if self.mesh_tolerance is None:
            splitter = getattr(self.bed_mesh, 'splitter', None)
            self.mesh_tolerance = getattr(splitter, 'split_delta_z', .025)
        toolhead = self.printer.lookup_object('toolhead')
        kin = toolhead.get_kinematics()
        kin_check = kin.check_move

        def check_move(move):
            kin_check(move)
            self.check_move(move)
        kin.check_move = check_move
        self.kin = kin

    class _Suspension:
        def __init__(self, guard, reason):
            self.guard = guard
            self.reason = reason

        def __enter__(self):
            self.guard.suspend_reasons.append(self.reason)
            return self

        def __exit__(self, *exc):
            self.guard.suspend_reasons.remove(self.reason)
            return False

    def suspended(self, reason):
        """Context manager: no checks while it is open. For sequences that
        must reach below the bed plane on purpose."""
        return self._Suspension(self, reason)

    def active(self):
        return self.enabled and not self.suspend_reasons

    # ---------------- where the bed is ----------------

    def _homed(self):
        """(xy_homed, z_homed) from the kinematics' own limits."""
        limits = getattr(self.kin, 'limits', None)
        if limits is not None:
            homed = [lo <= hi for lo, hi in limits[:3]]
        else:
            status = self.kin.get_status(self.reactor.monotonic())
            homed = [a in status['homed_axes'] for a in 'xyz']
        return homed[0] and homed[1], homed[2]

    def _nozzle_contact(self, tool):
        """Machine Z at which `tool`'s nozzle meets a flat, unmeshed bed."""
        tc = self.toolchange
        if tc.print_offset_ready(tool):
            base = tc.offset_z[tool]
        else:
            base = (self.carriage_contact_z + self.nozzle_gap
                    + tc.tools[tool].z_adjust)
        return base + tc.job_z

    def _contacts(self, mounted):
        """Machine Z of the lowest point on the carriage, flat bed.

        mounted: a tool index, -1 for a bare carriage, None for "cannot
        tell" (mid-change, sensors disagree) -- then the worst tool."""
        contact = self.carriage_contact_z
        tc = self.toolchange
        if tc is None:
            # No toolchanger to ask: assume a nozzle on the carriage.
            return contact + self.nozzle_gap
        if mounted is None:
            tools = range(len(tc.tools))
        elif mounted < 0:
            tools = ()
        else:
            tools = (mounted,)
        for tool in tools:
            contact = max(contact, self._nozzle_contact(tool))
        return contact

    def _mounted(self):
        """Tool index, -1 for a bare carriage, None when unknown."""
        if self.toolchange is None:
            return None
        tool, _reason = self.toolchange._current_tool_or_none()
        return tool

    def _mesh(self):
        if self.bed_mesh is None:
            return None
        mesh = self.bed_mesh.get_mesh()
        # Under tilt correction bed_mesh holds the residual (the plate less
        # the plane the tilted frame already carries); the nozzle has to
        # clear the measured plate itself, which the residual wraps.
        mesh = getattr(mesh, 'plate', mesh)
        if mesh is None or getattr(mesh, 'mesh_matrix', None) is None:
            return None
        return mesh

    def _mesh_max(self, mesh):
        """Highest point of the active mesh, 0 without one. get_z_range()
        walks the whole interpolated matrix, so it is cached per matrix
        rather than paid on every move (held by reference: an id() can be
        reused once the old matrix is freed)."""
        if mesh is None:
            return 0.
        matrix, z_max = self.mesh_cache
        if matrix is not mesh.mesh_matrix:
            z_max = mesh.get_z_range()[1]
            self.mesh_cache = (mesh.mesh_matrix, z_max)
        return z_max

    def _clip(self, start, end, xy_homed):
        """The [t0, t1] stretch of start->end that lies over the bed, or
        None. Liang-Barsky against the footprint rectangle."""
        if not xy_homed:
            return 0., 1.
        t0, t1 = 0., 1.
        for axis in (0, 1):
            p0 = start[axis]
            d = end[axis] - p0
            lo, hi = self.bed_min[axis], self.bed_max[axis]
            if abs(d) < EPSILON:
                if p0 < lo or p0 > hi:
                    return None
                continue
            ta, tb = (lo - p0) / d, (hi - p0) / d
            if ta > tb:
                ta, tb = tb, ta
            t0, t1 = max(t0, ta), min(t1, tb)
            if t0 > t1:
                return None
        return t0, t1

    # ---------------- the check ----------------

    def check_move(self, move):
        if not self.active():
            return
        if getattr(move.toolhead, 'special_queuing_state', '') == 'Drip':
            return
        xy_homed, z_homed = self._homed()
        if not z_homed:
            return
        start, end = move.start_pos, move.end_pos
        dx, dy, dz = (end[i] - start[i] for i in range(3))
        if dz >= 0. and math.hypot(dx, dy) <= RAISE_XY_TOLERANCE:
            return
        span = self._clip(start, end, xy_homed)
        if span is None:
            return
        t0, t1 = span
        z_low = min(start[2] + dz * t0, start[2] + dz * t1)
        mesh = self._mesh()
        mesh_max = self._mesh_max(mesh)
        # Fast path: clear of the worst case, sensors not read. Every
        # printing move above the first few layers ends here.
        if (z_low - self.min_clearance + EPSILON
                >= self._contacts(None) + mesh_max):
            return
        mounted = self._mounted()
        contact = self._contacts(mounted)
        if mesh is None or not xy_homed:
            # z is linear along the move, so its lowest point over the bed
            # is one end of the clipped stretch.
            t = t0 if dz >= 0. else t1
            where = [start[i] + (end[i] - start[i]) * t for i in range(3)]
            self._judge(move, z_low - (contact + mesh_max), where, mounted,
                        contact)
            return
        length = math.hypot(dx, dy) * (t1 - t0)
        samples = min(MAX_SAMPLES, int(math.ceil(length / SAMPLE_STEP)) + 1)
        worst_clearance, worst_t = None, t0
        for i in range(samples + 1):
            t = t0 + (t1 - t0) * (float(i) / samples if samples else 0.)
            x, y = start[0] + dx * t, start[1] + dy * t
            clearance = (start[2] + dz * t) - (contact + mesh.calc_z(x, y))
            if worst_clearance is None or clearance < worst_clearance:
                worst_clearance, worst_t = clearance, t
        where = [start[i] + (end[i] - start[i]) * worst_t for i in range(3)]
        self._judge(move, worst_clearance, where, mounted, contact,
                    slack=self.mesh_tolerance or 0.)

    def _judge(self, move, clearance, where, mounted, contact, slack=0.):
        if clearance + slack + EPSILON >= self.min_clearance:
            return
        if mounted is None:
            what = "the carriage (tool unknown -- assuming the lowest)"
        elif mounted < 0:
            what = "the bare carriage"
        else:
            what = "T%d's nozzle" % mounted
        msg = ("Bed guard: move would put %s %.3f mm from the bed at"
               " X%.2f Y%.2f (minimum %.2f%s; bed plane at machine Z %.3f)."
               " Raise Z first; SET_BED_GUARD ENABLE=0 overrides"
               % (what, clearance, where[0], where[1], self.min_clearance,
                  " less %.3f mesh tolerance" % slack if slack else "",
                  contact))
        self.blocked += 1
        self.last_block = msg
        logging.warning("ff_bed_guard: %s; move %s -> %s", msg,
                        tuple(round(v, 3) for v in move.start_pos[:3]),
                        tuple(round(v, 3) for v in move.end_pos[:3]))
        raise move.move_error(msg)

    # ---------------- commands / status ----------------

    cmd_SET_BED_GUARD_help = ("Enable or disable the bed-collision guard"
                              " (ENABLE=0|1); without ENABLE, report it")

    def cmd_SET_BED_GUARD(self, gcmd):
        enable = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        if enable is not None:
            self.enabled = bool(enable)
            if not self.enabled:
                gcmd.respond_info(
                    "Bed guard DISABLED: nothing stops the nozzle or the"
                    " carriage reaching the bed until SET_BED_GUARD ENABLE=1"
                    " or a restart.")
        status = self.get_status(self.reactor.monotonic())
        lines = ["Bed guard: %s, min_clearance %.2f mm, bed X%.1f..%.1f"
                 " Y%.1f..%.1f"
                 % ('enabled' if status['enabled'] else 'DISABLED',
                    self.min_clearance, self.bed_min[0], self.bed_max[0],
                    self.bed_min[1], self.bed_max[1])]
        if self.suspend_reasons:
            lines.append("suspended by: %s" % ", ".join(self.suspend_reasons))
        mounted = status['tool']
        lines.append("lowest point (%s) meets a flat bed at machine Z %.3f"
                     % ('tool unknown' if mounted is None else
                        'bare carriage' if mounted < 0 else 'T%d' % mounted,
                        status['bed_plane_z']))
        if self.last_block:
            lines.append("last refused (%d so far): %s"
                         % (self.blocked, self.last_block))
        gcmd.respond_info("\n".join(lines))

    def get_status(self, eventtime):
        mounted = self._mounted()
        return {'enabled': self.enabled,
                'active': self.active(),
                'suspended_by': list(self.suspend_reasons),
                'min_clearance': self.min_clearance,
                'mesh_tolerance': self.mesh_tolerance,
                'bed_min': list(self.bed_min),
                'bed_max': list(self.bed_max),
                'tool': mounted,
                'bed_plane_z': self._contacts(mounted),
                'blocked': self.blocked}


def load_config(config):
    return FFBedGuard(config)
