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
standing square on the plate. Bed mesh still corrects whatever the plane
does not explain.

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
prints without it. That happens when there is no mesh (`BED_MESH_MODE off`),
when the file has no first layer to fit to, or when bed mesh fade is
configured. `END_PRINT` and a cancel turn the correction off.

**The flow mesh and tilt correction are alternatives.** The flow mesh absorbs
the whole mesh in the first layers, tilt included, so the two cannot be
combined. With both toggles on, the flow mesh wins and the console says
tilt correction was skipped.

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
4. **Apply.** The correction is installed for the rest of the print.

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

### 3. Sharing the work with bed mesh

Bed mesh sits below the correction in Klipper's chain of move transforms. It
keeps adding *m(x, y)* to every move. The bed guard and the screens rely on
that, because they need to see the real plate. The mesh is split into the
plane and a residual:

  m(x, y) = plane(x, y) + residual(x, y)

The correction sends down

  **out** = ( qₓ, q_y, q_z − plane(qₓ, q_y) )

Bed mesh then adds *m(qₓ, q_y)*. The plane parts cancel and leave the nozzle
at **q** + residual. The tilt and the Z offset z₀ reach the nozzle through
the frame. The residual is corrected by bed mesh exactly as standard bed
mesh corrects the whole mesh. Nothing is added twice.

Working through the algebra, the vertical part simplifies completely. *R*
maps the plane z = 0 onto the fitted plane, so every x/y term cancels:

  out_z = s · z

A layer of thickness *h* measured along the normal therefore sits *s·h*
above the plane, measured vertically. That is the same distance seen from a
tilted direction. For any real tilt *s* differs from 1 by a few millionths.

The whole map **p** → **out** is a matrix multiplication plus a constant,
an *affine* map:

```
          ⎡ R₁₁   R₁₂   R₁₃ ⎤
  out  =  ⎢ R₂₁   R₂₂   R₂₃ ⎥ (p − P₀)  +  (x₀, y₀, 0)
          ⎣  0     0     s  ⎦
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
map into toolhead coordinates. For Z, the check adds what the chain below
adds: the tools' Z offsets, the job term, and the mesh's lowest and highest
value. Slab-sized boxes keep this tight: a corner low on the part is never
checked at the height of the part's top.

The tilt is then scaled by a factor σ ∈ [0, 1]. The slope becomes σ**g**,
with the same pivot and the same z₀. σ is searched from 1 down in steps of
0.01, and the first value that fits is used. "Fits" means that no axis, on
either side, goes further outside the envelope than the uncorrected file
already does at σ = 0. If the file is already outside the envelope as
sliced, the correction is never blamed for it. It is only stopped from
making things worse.

A reduced σ costs nothing on the first layer. The part of the tilt that is
dropped stays in the residual, and bed mesh follows it as before. Only the
lean is reduced. A slope steeper than `max_slope` (1 mm per 100 mm) is
almost certainly a bad mesh rather than a tilted plate, and it is capped
there.

---

## Things to know

* **It corrects the frame, not the mesh.** Bed mesh stays loaded and
  active. Your first layer behaves exactly as before.
* **The scan reads the whole file.** On a large file this takes tens of
  seconds on the printer's processor, which is why it starts while the bed
  heats. If it has not finished by the time the mesh is loaded,
  `START_PRINT` waits for it, up to `scan_timeout`.
* **Tool changes**: each tool's moves are placed using that tool's offset.
  Dock moves run in the raw machine frame and are untouched.
* **Bed mesh fade must be off**, as it is on a stock printer. Fade would
  remove the plane with height while the frame stays tilted.

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
`residual` and `reason` (why the last attempt did not apply).
