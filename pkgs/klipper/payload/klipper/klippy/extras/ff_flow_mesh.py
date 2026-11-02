# Flow-compensated bed mesh for the Creator 5
#
# Copyright (C) 2026
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Absorb the bed's unevenness into the first layers' thickness instead of
following it with the nozzle.

Standard bed mesh moves the nozzle up and down with the plate, so every
layer is the same thickness and the part's top inherits nothing -- but its
bottom is a copy of the plate, and every layer above carries the waviness up.
This strategy keeps the nozzle on the flat G-code plane and lets the first
layer fill the gap: thicker over a low spot, thinner over a high one, with
the extrusion scaled to match, so the layer's top comes out flat.

THICKNESS LIMITS. A layer cannot be squashed thinner than min_thickness
(0.1 mm) or laid thicker than max_thickness (the active nozzle's diameter).
Where the gap would fall outside them, the nozzle follows the mesh just far
enough to hold the limit -- standard behaviour, reduced by what the limit
lets the layer absorb -- and what is left over carries into the next layer.
Each layer flattens what it can, so the remaining mesh shrinks layer by
layer until it is flat, and from then on the nozzle prints on the flat plane
with normal flow.

THE ARITHMETIC, per point (x, y) on the bed. r is how far the printed
surface sits above the flat G-code plane; before the first layer it is the
bed mesh itself. A layer of nominal height h then has a gap of h - r, which
is clamped to [min_thickness, max_thickness] to give the thickness t laid
there. The nozzle sits at the layer's G-code Z + (t - (h - r)), the new
surface is r + t - h above the plane, and the extrusion is scaled by t / h.
A flat bed (r = 0) gives t = h everywhere, which is plain printing. Limits
are widened to h itself for a layer thinner than min_thickness or thicker
than max_thickness, so such a layer is never forced off its own height.

LAYERS are the G-code Z of extruding moves (X/Y travel with E going
forward), measured without the babystep. An extruding move higher than the
current layer starts a new one; one lower drops every layer at or above it,
which is what a purge line printed higher than the first layer, or
sequential printing, looks like. Travel slightly above the current layer
(less than a max thickness) is treated as the approach to the next one, so
the nozzle descends to the right height before it starts extruding; travel
higher than that, a Z-hop, keeps the current layer's offset.

HOW IT SITS IN KLIPPER. bed_mesh keeps its mesh loaded and goes on adding
it to every move, so ff_bed_guard, the screens and anything else that reads
it see the real plate. This transform sits at the top of the chain, above
ff_toolchange's per-tool frame, and sends each move down with
(r - mesh) added to Z: bed_mesh's own +mesh cancels the mesh part, leaving
the nozzle at G-code Z + r. Moves are split every split_distance so that
cancellation holds along the move and not only at its ends. Dock moves,
which ff_toolchange runs in raw machine coordinates, pass through untouched.

FLOW_MESH_START turns it on for one print and FLOW_MESH_STOP off; END_PRINT
stops it. Nothing here runs unless started.
"""

import logging
import math

EPSILON = 1e-4
FLAT_TOLERANCE = 1e-4


def layer_surface(mesh_z, heights, min_thickness, max_thickness):
    """(r, t) after printing layers of `heights` over a bed at `mesh_z`.

    r is the printed surface's height above the flat G-code plane, t the
    thickness of the last layer (None before any).
    """
    r, t = mesh_z, None
    for h in heights:
        lo = min(min_thickness, h)
        hi = max(max_thickness, h)
        t = min(max(h - r, lo), hi)
        r += t - h
    return r, t


class LayerTracker:
    """The G-code Z of each layer printed so far, bottom first."""

    def __init__(self):
        self.tops = []

    def top(self):
        return self.tops[-1] if self.tops else 0.

    def heights(self):
        out, below = [], 0.
        for top in self.tops:
            out.append(top - below)
            below = top
        return out

    def note_extrusion(self, z):
        """Record an extruding move at G-code Z; True if a layer started."""
        if self.tops and abs(z - self.tops[-1]) <= EPSILON:
            return False
        if z <= EPSILON:
            return False
        if self.tops and z < self.tops[-1]:
            self.tops = [t for t in self.tops if t < z - EPSILON]
        self.tops.append(z)
        return True


class FFFlowMesh:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.min_thickness = config.getfloat('min_thickness', 0.1, above=0.)
        self.max_thickness = config.getfloat('max_thickness', None,
                                             above=0.)
        self.split_distance = config.getfloat('split_distance', 2., above=0.)
        self.next_transform = None
        self.active = False
        self.layers = LayerTracker()
        self.flat_layer = None
        self.max_residual = 0.
        self.run_min = self.run_max = None
        self.last = None
        self.last_offset = 0.
        self.e_shift = 0.
        self.gcode.register_command(
            'FLOW_MESH_START', self.cmd_FLOW_MESH_START,
            desc='Absorb the bed mesh into first-layer flow for this print')
        self.gcode.register_command(
            'FLOW_MESH_STOP', self.cmd_FLOW_MESH_STOP,
            desc='Return to standard bed mesh compensation')
        self.gcode.register_command(
            'FLOW_MESH_STATUS', self.cmd_FLOW_MESH_STATUS,
            desc='Show the flow mesh state')

    # -- the model ---------------------------------------------------------
    def _limits(self):
        hi = self.run_max
        if hi is None:
            toolhead = self.printer.lookup_object('toolhead')
            hi = getattr(toolhead.get_extruder(), 'nozzle_diameter', 0.4)
        return self.run_min, hi

    def _mesh(self):
        bed_mesh = self.printer.lookup_object('bed_mesh', None)
        return None if bed_mesh is None else bed_mesh.get_mesh()

    def _tool_xy(self):
        """The mounted tool's X/Y offset, or None in raw machine frame."""
        toolchange = self.printer.lookup_object('ff_toolchange', None)
        if toolchange is None:
            return (0., 0.)
        offsets = toolchange.gcode_transform._offsets()
        return None if offsets is None else offsets[:2]

    def _surface_heights(self, z, extruding):
        """The layer heights that place the nozzle for a move at Z."""
        heights = self.layers.heights()
        if self.flat_layer is not None and len(heights) > self.flat_layer:
            # Flat from there on: r is 0 at every point and stays 0.
            return None
        if extruding:
            return heights
        lo, hi = self._limits()
        top = self.layers.top()
        if top + EPSILON < z < top + hi:
            return heights + [z - top]      # approaching the next layer
        return heights

    def adjust(self, bed_x, bed_y, z, extruding):
        """(Z to add before bed_mesh, extrusion factor) at one point."""
        mesh = self._mesh()
        if mesh is None:
            return 0., 1.
        mesh_z = mesh.calc_z(bed_x, bed_y)
        heights = self._surface_heights(z, extruding)
        if heights is None:
            return -mesh_z, 1.
        lo, hi = self._limits()
        r, t = layer_surface(mesh_z, heights, lo, hi)
        flow = t / heights[-1] if extruding and heights else 1.
        return r - mesh_z, flow

    def _check_flat(self):
        """After a new layer: is what is left of the mesh flat yet?"""
        mesh = self._mesh()
        if mesh is None:
            return
        lo, hi = self._limits()
        heights = self.layers.heights()
        worst = 0.
        for row in mesh.get_mesh_matrix():
            for mesh_z in row:
                r, _ = layer_surface(mesh_z, heights, lo, hi)
                worst = max(worst, abs(r))
        self.max_residual = worst
        if worst <= FLAT_TOLERANCE and self.flat_layer is None:
            self.flat_layer = len(heights)
            self.gcode.respond_info(
                "flow mesh: flat after layer %d (Z %.3f); printing on the "
                "flat plane with normal flow" % (len(heights),
                                                 self.layers.top()))

    # -- the transform -----------------------------------------------------
    def move(self, newpos, speed):
        x, y, z, e = newpos[:4]
        rest = list(newpos[4:])
        lx, ly, lz, le = self.last[:4]
        tool = self._tool_xy()
        if tool is None:
            self.last_offset = 0.
            self.next_transform.move([x, y, z, e + self.e_shift] + rest,
                                     speed)
            self.last = list(newpos)
            return
        gcode_move = self.printer.lookup_object('gcode_move')
        babystep = gcode_move.homing_position[2]
        dxy = math.hypot(x - lx, y - ly)
        de = e - le
        extruding = dxy > 0. and de > 0.
        if extruding and self.layers.note_extrusion(z - babystep):
            if self.flat_layer is not None and \
                    len(self.layers.tops) <= self.flat_layer:
                self.flat_layer = None      # dropped back below it
            if self.flat_layer is None:
                self._check_flat()
        count = max(1, int(math.ceil(dxy / self.split_distance)))
        out_e = le + self.e_shift
        for i in range(1, count + 1):
            f = float(i) / count
            px, py = lx + (x - lx) * f, ly + (y - ly) * f
            pz = lz + (z - lz) * f
            offset, flow = self.adjust(px + tool[0], py + tool[1],
                                       pz - babystep, extruding)
            out_e += de / count * (flow if extruding else 1.)
            self.next_transform.move([px, py, pz + offset, out_e] + rest,
                                     speed)
            self.last_offset = offset
        self.e_shift = out_e - e
        self.last = list(newpos)

    def get_position(self):
        pos = self.next_transform.get_position()
        return [pos[0], pos[1], pos[2] - self.last_offset,
                pos[3] - self.e_shift] + list(pos[4:])

    # -- commands ----------------------------------------------------------
    def start(self, min_thickness, max_thickness):
        bed_mesh = self.printer.lookup_object('bed_mesh', None)
        if bed_mesh is None or bed_mesh.get_mesh() is None:
            raise self.gcode.error("flow mesh: no bed mesh is loaded")
        if bed_mesh.fade_end != bed_mesh.FADE_DISABLE:
            raise self.gcode.error(
                "flow mesh: bed_mesh fade is configured; it already thins "
                "the mesh with height and the two cannot be combined")
        self.run_min = min_thickness
        self.run_max = max_thickness
        self.layers = LayerTracker()
        self.flat_layer = None
        self.max_residual = 0.
        if self.active:
            return
        gcode_move = self.printer.lookup_object('gcode_move')
        self.next_transform = gcode_move.set_move_transform(self, force=True)
        self.last = list(self.next_transform.get_position())
        self.last_offset = 0.
        self.e_shift = 0.
        self.active = True
        gcode_move.reset_last_position()
        logging.info("ff_flow_mesh: started, thickness %.3f..%s",
                     min_thickness, max_thickness)

    def stop(self):
        if not self.active:
            return False
        gcode_move = self.printer.lookup_object('gcode_move')
        if gcode_move.move_transform is not self:
            raise self.gcode.error(
                "flow mesh: another move transform was installed above it; "
                "cannot remove it safely")
        gcode_move.set_move_transform(self.next_transform, force=True)
        self.active = False
        # The extruder is e_shift ahead of the G-code E; keep the G-code E
        # where the file thinks it is, or an absolute-E move would jump.
        gcode_move.reset_last_position()
        gcode_move.base_position[3] += self.e_shift
        self.e_shift = 0.
        self.last_offset = 0.
        return True

    def cmd_FLOW_MESH_START(self, gcmd):
        lo = gcmd.get_float('MIN_THICKNESS', self.min_thickness, above=0.)
        hi = gcmd.get_float('MAX_THICKNESS', self.max_thickness or 0.,
                            minval=0.)
        hi = hi if hi > 0. else None
        if hi is not None and hi < lo:
            raise gcmd.error("flow mesh: MAX_THICKNESS is below MIN_THICKNESS")
        self.start(lo, hi)
        gcmd.respond_info(
            "flow mesh: on -- first layers keep a flat nozzle and absorb the "
            "mesh, %.2f..%s mm thick" % (lo, '%.2f' % hi if hi else
                                         'nozzle diameter'))

    def cmd_FLOW_MESH_STOP(self, gcmd):
        if self.stop():
            gcmd.respond_info("flow mesh: off, standard bed mesh")

    def cmd_FLOW_MESH_STATUS(self, gcmd):
        if not self.active:
            gcmd.respond_info("flow mesh: off")
            return
        lo, hi = self._limits()
        gcmd.respond_info(
            "flow mesh: on, thickness %.2f..%.2f mm, layer %d (Z %.3f), "
            "mesh left %.4f mm%s"
            % (lo, hi, len(self.layers.tops), self.layers.top(),
               self.max_residual,
               ", flat since layer %d" % self.flat_layer
               if self.flat_layer is not None else ""))

    def get_status(self, eventtime):
        return {'active': self.active,
                'layer': len(self.layers.tops),
                'layer_z': self.layers.top(),
                'mesh_left': self.max_residual,
                'flat_layer': self.flat_layer,
                'min_thickness': self.run_min if self.active
                else self.min_thickness}


def load_config(config):
    return FFFlowMesh(config)
