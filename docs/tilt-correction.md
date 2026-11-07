# Tilt correction

A way to print square to a bed that has tilted. It is off unless you turn it
on, and it is new: it has not been tried on hardware yet.

The bed's tramming is not fixed. A hot plate in a hot chamber sits at a
slightly different angle from the one the gantry was trammed to, and the
angle moves with temperature. **Standard bed mesh** follows that tilt with the
nozzle, so each layer lies parallel to the plate. But the layers are still
stacked along the machine's Z axis, which is no longer perpendicular to the
plate. Measured against the plate, the part leans: a 0.2 % tilt (0.5 mm
across 250 mm) leans a 200 mm part by 0.4 mm. With bed mesh fade the part
comes out wedge-shaped instead.

**Tilt correction** fits a plane to the mesh under the print. It then tilts
the print's whole coordinate frame onto that plane, so the part is built
standing square on the plate. The tilted frame is calculated once, at the
print start, and frozen for the whole print. Bed mesh then corrects only
what the plane does not explain, the *residual*. Mesh fade and the
[flow mesh](flow-mesh.md) both work with it, and both act on that residual
alone: neither of them, nor a mesh cleared or reloaded mid-print, can
change the tilted frame.

```gcode
TILT_CORRECTION_TOGGLE ENABLE=1     ; prints use it, until a restart
TILT_CORRECTION_TOGGLE ENABLE=0     ; back to standard
TILT_CORRECTION_STATUS              ; during a print: tilt, offset, scale
TILT_CORRECTION_START DRY_RUN=1 FILENAME=part.gcode   ; report only
```

When it is enabled, `START_PRINT` starts reading the file straight after its
preflight checks. The scan runs in the background while the bed heats. After
the mesh is loaded and the first tool is clean, `START_PRINT` fits the plane
and tilts the frame, and the console reports what it did:

```
tilt correction: plane over 412 cells (1030 cm^2), centred X131.2 Y118.0: Z offset +0.084 mm there, slope X +0.180 Y -0.065 mm/100mm
mesh over the footprint: 0.1430 mm rms about its mean, 0.0210 mm rms about the plane (left to bed_mesh)
frame tilted 0.1097 deg; at the top of the print (Z 85.0) the part shifts 0.163 mm in X/Y against an untilted one
```

If the correction cannot be applied, `START_PRINT` reports the reason and
prints without it. That happens when there is no mesh (`BED_MESH_MODE off`)
or when the file has no first layer to fit to. `END_PRINT` and a cancel
turn the correction off and put the measured mesh back.

**With the flow mesh**, the frame takes the plate's tilt and the first
layers absorb the residual: the nozzle stays on the tilted plane, and the
first layers are laid thicker over low spots and thinner over high ones
relative to that plane. **With mesh fade**, the residual fades out with
height and the tilted frame stays to the top of the print. Fade then works
from the height of the plane under the nozzle, not from machine Z = 0.

The toggle resets to off on every restart. To make tilt correction the
default, add this at the end of `printer.cfg`:

```
[gcode_macro TILT_CORRECTION_TOGGLE]
variable_enabled: 1
```

---

## What happens, step by step

1. **Scan the file.** The scanner reads every move in the G-code and keeps
   two things. The first is the **first-layer footprint**: the 5 mm cells
   that an extruding move crosses at the lowest height anything is extruded
   (within 0.05 mm). A purge line printed higher than the first layer does
   not count. The second is the **extent of every move**, kept per tool and
   per 5 mm slab of Z: the smallest box in X, Y and Z holding that slab's
   moves. Positions follow `G90`/`G91`, `G92`, `M82`/`M83`, arcs and tool
   changes.
2. **Fit a plane** to the active mesh over the footprint.
3. **Check the envelope.** Each extent box goes through the correction. If
   any of them would move further outside the machine's travel than the
   file already goes, the tilt is reduced.
4. **Freeze and apply.** The map is computed this once and stored, and is
   readable as `printer.ff_tilt_correction.transform`. From here on
   `bed_mesh` holds the measured mesh less the frozen plane.

The rest of this page is the math behind steps 2 to 4.

---

## The math

### Coordinates

All positions are in **toolhead X/Y**, the coordinates the bed mesh is
indexed by: the G-code X/Y plus the mounted tool's offset and any
`SET_GCODE_OFFSET`. **Z** is the G-code Z after the babystep, so it is the
nozzle's height above the homed, unmeshed bed. The tool's ~3.2 mm Z offset
and the print-start job term are added further down the chain, so the
correction never sees them.

The bed mesh is a function *m(x, y)*: the plate's height above the homed
plane at each point.

### 1. Fitting the plane

Each footprint cell *i* gives one sample: its centre (*xᵢ*, *yᵢ*) and the
mesh height there, *zᵢ = m(xᵢ, yᵢ)*. There are *N* samples. We want the
plane

  *z = z₀ + gₓ (x − x₀) + g_y (y − y₀)*

that minimises the RMS error over the used area:

  E = (1/N) Σᵢ ( *zᵢ − z₀ − gₓ(xᵢ − x₀) − g_y(yᵢ − y₀)* )²

Every cell counts once, so the fit weights **area**. It does not weight the
toolpath, which would favour wherever the infill is dense.

The pivot (*x₀*, *y₀*) is the footprint's centroid. Taking derivatives of E
gives three conditions:

* ∂E/∂z₀ = 0 gives **z₀ = mean(zᵢ)**. The fitted plane passes through the
  mean mesh height, at the centroid.
* ∂E/∂gₓ = ∂E/∂g_y = 0 give a 2×2 system in the slope **g** = (gₓ, g_y).
  With *uᵢ = (xᵢ − x₀, yᵢ − y₀)* the centred positions and *wᵢ = zᵢ − z₀*:

  **C g = r**, where **C** = (1/N) Σ uᵢuᵢᵀ is the covariance of the
  footprint and **r** = (1/N) Σ uᵢ wᵢ is the covariance of position with
  height.

**C** is symmetric, so it has orthonormal eigenvectors **v₁**, **v₂** with
eigenvalues λ₁ ≥ λ₂. These are the footprint's principal directions.
Written in that basis the solution separates into two independent terms:

  **g** = Σₖ ( (**v**ₖ · **r**) / λₖ ) **v**ₖ

Each term is the slope along one principal direction. A term is used only if
the footprint spans at least `min_span` (30 mm) along that direction. A
direction too narrow for that contributes no slope. That is the
pseudo-inverse of **C**, and it handles a narrow footprint correctly. For
example, a 3 mm wide strip says nothing about the tilt across it: fitting
across it would turn probe noise into a large, meaningless slope. When both
directions are wide enough, this is ordinary least squares.

The console reports two numbers for the fit. The first is the RMS of the
mesh about its mean, which is the error left by Z offset alone. The second
is the RMS about the plane, *√E*, which is what tilt correction leaves to
bed mesh.

### 2. The tilted frame

The plane's unit normal is

  **n** = (−gₓ, −g_y, 1) / s,  s = √(1 + gₓ² + g_y²)

The correction rotates the print's frame so that its Z axis lies along
**n**. Many rotations do that. We use the smallest one, a rotation about the
horizontal axis **ẑ × n**, so the print is not also turned about Z.
Rodrigues' formula gives it as follows, writing **n** = (nₓ, n_y, n_z) and
k = 1/(1 + n_z):

```
        ⎡ 1 − nₓ²k    −nₓn_y k    nₓ  ⎤
  R  =  ⎢ −nₓn_y k    1 − n_y²k   n_y ⎥
        ⎣   −nₓ         −n_y      n_z ⎦
```

*R* is orthonormal, which makes it a rigid rotation: it preserves every
length and angle in the part. Its third column is **n**, so *R* takes the
machine's Z axis onto the normal.

A print-frame point **p** = (x, y, z) is placed at

  **q** = **P** + R (**p** − **P₀**),  **P₀** = (x₀, y₀, 0),
  **P** = (x₀, y₀, z₀)

So the print's bed plane, z = 0, lands exactly on the fitted plane. The
print's origin (x₀, y₀, 0) lands on the plane at height **z₀**. That is the
Z offset of the new frame: the mean mesh height over the footprint. The
print's Z axis follows **n**.

### 3. Freezing the frame, and the residual

The frame is the map **p** → **q**, and the correction sends **q** down
Klipper's chain of move transforms as it is. The tilt and the Z offset z₀
belong to the frame alone. From the moment the map is computed it is
fixed: nothing reads the mesh to rebuild it.

Bed mesh sits below the correction and adds its mesh to every move. If it
still held the measured mesh *m(x, y)*, it would add the plane a second
time. So while the correction is on, bed mesh holds the **residual**:

  r(x, y) = m(x, y) − plane(x, y)

It is computed node by node on bed mesh's own interpolated grid, so between
the nodes it is still exactly *m − plane*. The nozzle then lands at

  **q** + r(qₓ, q_y)

On the first layer that is the measured plate plus one layer height, as
with standard bed mesh. Above it, the part stands square on the plane.

Everything that shapes the *effective* mesh then works on *r* alone:

* **Fade** scales what bed mesh adds by a factor that falls from 1 to 0
  between `fade_start` and `fade_end`. It adds
  *f·(r − t) + t*, with *t* the fade target (by default the average of *r*).
  The residual fades out; **q** does not.
* **The flow mesh** sits above the correction, in the print's own frame. Its
  layer tracking sees the file's Z, and it absorbs *r* instead of following
  it. It looks *r* up at (qₓ, q_y), where the leaning nozzle really is, not
  at the file's X/Y.
* **A mesh loaded mid-print** (`BED_MESH_PROFILE LOAD`, a new calibration)
  is turned into its residual against the same frozen plane on the way in.
  `BED_MESH_CLEAR` removes the residual and leaves the frame tilted.

The probed points bed mesh keeps, which are what `BED_MESH_PROFILE SAVE`
writes, stay the measured ones. Saving a profile mid-print saves the plate,
not the residual. The screens show the residual as the mesh while a tilted
print runs; `END_PRINT` puts the measured mesh back.

The bed guard has to know where the plate really is, so it reads the
measured mesh behind the residual.

How tall a layer is: *R* maps the plane z = 0 onto the fitted plane, so a
point *h* along the normal sits

  q_z − plane(qₓ, q_y) = s · h

above the plane vertically. That is the same distance seen from a tilted
direction. For any real tilt *s* differs from 1 by a few millionths.

*R* is orthonormal, so the whole map is a rotation plus a constant (an
*affine* map), and its inverse is the transpose:

```
  q  =  R (p − P₀)  +  P            p  =  Rᵀ (q − P)  +  P₀
```

An affine map sends straight lines to straight lines, so moves never need
splitting. It is also inverted exactly, so the position Klipper reports (and
pause/resume, and the toolchanger's return move) come back in the print's
own coordinates.

For small tilts the main visible effect is a horizontal lean that grows with
height:

  Δx ≈ −gₓ · z,  Δy ≈ −g_y · z

A plate rising toward +X makes the part lean toward −X. At 0.002 that is
0.4 mm at Z 200.

### 4. Keeping inside the machine

The lean can push the top of a tall part, or anything near the edge of the
bed, past the machine's travel. The extent boxes from the scan are checked
against an envelope: `envelope_min`/`envelope_max`, by default each axis'
`position_min`/`position_max`.

The map is affine. So the image of a box lies within the images of its eight
corners, and checking the corners is enough. Each corner goes through the
map into toolhead coordinates. Z already includes the plane, which now
rides in the frame. For Z the check adds what the chain below adds: the
tools' Z offsets, the job term, and the residual's lowest and highest value
against the plane being tried. 0 is included, because fade can take the
residual away. Slab-sized boxes keep this tight: a corner low on the part is never
checked at the height of the part's top.

The tilt is then scaled by a factor σ ∈ [0, 1]. The slope becomes σ**g**,
with the same pivot and the same z₀. σ is searched from 1 down in steps of
0.01, and the first value that fits is used. "Fits" means that no axis, on
either side, goes further outside the envelope than the uncorrected file
already does at σ = 0. If the file is already outside the envelope as
sliced, the correction is never blamed for it. It is only stopped from
making things worse.

A reduced σ costs nothing on the first layer. The frozen plane is the
reduced one, so the part of the tilt that is dropped stays in the residual,
and bed mesh follows it as before. Only the
lean is reduced. A slope steeper than `max_slope` (1 mm per 100 mm) is
almost certainly a bad mesh rather than a tilted plate, and it is capped
there.

---

## Things to know

* **It corrects the frame; bed mesh keeps the rest.** Your first layer
  behaves exactly as before: on the plate, plus the flow mesh's thickness
  changes if that is on.
* **The frame is frozen at the print start.** Nothing that changes the mesh
  later changes it. `TILT_CORRECTION_STATUS` shows it, and
  `TILT_CORRECTION_STOP` ends it.
* **The scan reads the whole file.** On a large file this takes tens of
  seconds on the printer's processor, which is why it starts while the bed
  heats. If it has not finished by the time the mesh is loaded,
  `START_PRINT` waits for it, up to `scan_timeout`.
* **Tool changes**: each tool's moves are placed using that tool's offset.
  Dock moves run in the raw machine frame and are untouched.
* **Mesh fade** heights are machine heights: the tool's ~3.2 mm frame
  offset, and now the plane's height under the nozzle, are included in
  them, as they always were for the tool offset.

---

## Settings

`[ff_tilt_correction]` is in `ff-print-macros.cfg`. To change an option,
restate the section with just that option at the end of `printer.cfg`.

| Option | Default | |
|---|---|---|
| `cell_size` | 5 | mm. Footprint resolution, in both the scan and the fit. |
| `slab_height` | 5 | mm. Height of the extent boxes used for the envelope check. |
| `footprint_tolerance` | 0.05 | mm. Extrusions this close to the lowest one count as the first layer. |
| `min_span` | 30 | mm. A footprint narrower than this in a direction gets no slope fitted in that direction. |
| `max_slope` | 0.01 | mm/mm. The steepest tilt applied. |
| `scan_timeout` | 300 | s. The longest `START_PRINT` waits for the scan. |
| `envelope_min` | stepper `position_min` | X, Y, Z in toolhead coordinates. |
| `envelope_max` | stepper `position_max` | X, Y, Z in toolhead coordinates. |

`TILT_CORRECTION_START` also takes `MAX_SCALE=` (0–1, to apply only part of
the fitted tilt) and `DRY_RUN=1`. `printer.ff_tilt_correction` reports
`active`, `slope_x`, `slope_y`, `angle`, `pivot`, `z_offset`, `scale`, `rms`,
`residual`, `reason` (why the last attempt did not apply) and `transform`
(the frozen map: pivot, z₀, slopes, normal and rotation matrix).
