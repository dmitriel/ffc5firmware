# Flow mesh

A second way to use the bed mesh. It is off unless you turn it on, and it is
new: it has not been tried on hardware yet.

**Standard bed mesh** moves the nozzle up and down with the plate. Every
layer is the same thickness, so the plate's shape is copied into the bottom
of the part and carried up through it.

**Flow mesh** keeps the nozzle on the flat plane the file was sliced for and
lets the first layers take up the difference: a layer is laid thicker over a
low spot and thinner over a high one, with the extrusion scaled to match, so
its top comes out flat.

```gcode
FLOW_MESH_TOGGLE ENABLE=1      ; prints use it, until a restart
FLOW_MESH_TOGGLE ENABLE=0      ; back to standard
FLOW_MESH_STATUS               ; during a print: layer, mesh left, flat since layer N
```

`START_PRINT` starts it after the first tool's clean, whichever mesh
`BED_MESH_MODE` chose, and says `flow mesh: on`; otherwise it says
`Bed mesh strategy: standard`. With `BED_MESH_MODE` `off` there is no mesh
to absorb and it stays off. `END_PRINT` and a cancel stop it.

The toggle resets to off on every restart, including the
`FIRMWARE_RESTART` after a shutdown. To make the flow mesh the default, add
this at the end of `printer.cfg`:

```
[gcode_macro FLOW_MESH_TOGGLE]
variable_enabled: 1
``` You can also start
it yourself after the mesh is loaded:
`FLOW_MESH_START FIRST_LAYER=0.2 [MIN_THICKNESS=0.1] [MAX_THICKNESS=0.4]`.

---

## How each layer is printed

At every point on the plate the firmware keeps track of how far the surface
printed so far sits above or below the flat plane. Before the first layer
that is the bed mesh itself.

For a layer of height *h* the gap at a point is *h* minus that surface. The
layer fills the gap, within limits:

| | Thickness laid | Nozzle | Extrusion |
|---|---|---|---|
| gap between the limits | the gap | on the flat plane | × gap ÷ *h* |
| gap under `min_thickness` (a high spot) | `min_thickness` | raised just enough | × `min_thickness` ÷ *h* |
| gap over `max_thickness` (a low spot) | `max_thickness` | lowered just enough | × `max_thickness` ÷ *h* |

Where a limit was hit, what was left over is carried into the next layer,
which flattens what it can of it in turn. Once nothing is left anywhere on
the mesh, the console says `flow mesh: flat after layer N`, and the rest of
the print runs on the flat plane at normal flow.

Example, 0.2 mm layers, 0.1–0.4 mm limits:

* a spot 0.05 mm high: layer 1 is 0.15 mm thick there, extruded at 75 %, and
  flat;
* a spot 0.15 mm high: layer 1 is 0.1 mm (the minimum; the nozzle rises
  0.05 mm), layer 2 is 0.15 mm, flat after layer 2;
* a spot 0.3 mm low: layer 1 is 0.4 mm (the maximum), layer 2 0.3 mm, flat
  after layer 2.

A layer thinner than `min_thickness` (or thicker than `max_thickness`) is
never forced to the limit on a flat spot; it keeps its own height there.

---

## Things to know

* **The first layer's line width does not change; its height does.** Over a
  high spot the layer is thinner and carries less plastic, over a low spot
  more. If your first layer is already close to the minimum, raise the
  slicer's first-layer height rather than lowering `min_thickness`.
* **The first layer's height comes from the print start**, the `LAYER=`
  that `START_PRINT` already takes (filled in from the
  file's first `;HEIGHT:` when the printer starts the print itself). If it is
  missing, the print says so and uses the standard mesh. In a slicer's start
  G-code pass the *first* layer's height, e.g. Orca's
  `LAYER=[initial_layer_print_height]`. If the file's first extrusion is at
  another height the console warns, which is harmless for a purge line.
* **Layers above the first are detected from the file**: a new layer is the
  first extruding move at a higher Z. A purge or prime line printed higher
  than the first layer is discarded when the first layer starts. Vase mode
  works once its solid bottom layers are done; it is not meant for a spiral
  that starts on the plate.
* **Babysteps still work.** Layers are measured without the babystep, and
  the babystep moves the nozzle as usual.
* **The mesh stays loaded.** The bed guard and the screens still see the
  real plate. Bed mesh fade must be off, as it is on a stock printer.
* **With [tilt correction](tilt-correction.md)** the plate's tilt is taken
  by the print's frame, and the flow mesh absorbs only what is left: the
  nozzle stays on the tilted plane rather than the machine's flat one.
* **Tool changes**: the mesh is looked up under the mounted tool; dock moves
  are untouched.

---

## Settings

`[ff_flow_mesh]` in `ff-print-macros.cfg`. To change one, restate the
section with just that option at the end of `printer.cfg`.

| Option | Default | |
|---|---|---|
| `min_thickness` | 0.1 | Thinnest a layer may be laid over a high spot. |
| `max_thickness` | the mounted nozzle's diameter | Thickest a layer may be laid over a low spot. |
| `split_distance` | 2.0 | mm. Moves are split this finely so the nozzle height and flow follow the mesh along them. |

`printer.ff_flow_mesh` reports `active`, `layer`, `layer_z`, `mesh_left`
(the largest remaining deviation, mm) and `flat_layer`.
