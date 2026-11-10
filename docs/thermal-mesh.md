# Thermal mesh map

A bed mesh describes the plate at one temperature. On the Creator 5 Pro the
plate's shape depends on two: the bed's own temperature, and the chamber's,
because the frame and gantry the plate is measured against warm up with the
chamber. A mesh probed at bed 60 °C in a cold chamber is the wrong mesh for
bed 100 °C in a 50 °C chamber.

`[ff_thermal_mesh]` builds a map of meshes over both temperatures once, then
picks the right mesh for each print from a four-point probe instead of a full
grid. It is not used by anything unless you call it, or choose it with
`BED_MESH_MODE MODE=thermal`.

It is new and has not been validated on hardware yet. Look at what it
reports before you print on it.

---

## 1. Map the bed: `THERMAL_MESH_SWEEP`

```gcode
G28
THERMAL_MESH_SWEEP BED_MIN=60 BED_MAX=110 BED_STEP=10 CHAMBER_MIN=30 CHAMBER_MAX=60 CHAMBER_STEP=10 SOAK=600 MAX_SOAK=1800 CHAMBER_SOAK=3600
```

| Parameter | Default | Meaning |
|---|---|---|
| `BED_MIN`, `BED_MAX`, `BED_STEP` | — , `BED_MIN`, 10 | Bed targets, °C. The last step is always `BED_MAX`. |
| `CHAMBER_MIN`, `CHAMBER_MAX`, `CHAMBER_STEP` | 0, `CHAMBER_MIN`, 10 | Chamber targets. `0` means unheated; on the plain Creator 5 leave both at 0. |
| `CHAMBER_SOAK` | 3600 | Seconds to soak at the very first pair, once both heaters are at target. The only chamber soak in the sweep. |
| `MAX_SOAK` | `SOAK` or 1800, whichever is longer | Seconds to soak at the first (hottest) bed step of every later chamber set. |
| `SOAK` | 600 | Seconds to soak after every other bed step. |
| `COOL_TIMEOUT` | `cool_timeout` (3600) | Seconds to wait for the chamber to cool down to the next, lower, target. If it has not, that chamber set is skipped. |
| `CONFIDENCE` | `confidence` (0.95) | How sure the verification must be that two meshes are the same (below). |
| `MAX_SIGMA` | `max_probe_sigma` (0.01) | mm. The most the probe's measured standard deviation may be for a verification to count. |
| `RESUME` | 0 | `1` keeps the pairs already in the map and continues. Without it an existing map is moved to `thermal_mesh.json.bak`. |
| `KEEP_HEAT` | 0 | `1` leaves the heaters at the last pair's targets when the sweep finishes. |
| `POINTS` | 4 | How many probe points the analysis at the end picks. |

The sweep starts hot and works down, because the chamber has a heater but no
cooling, and the bed cools far faster than the chamber:

```
chamber 60: bed 110 → 100 → 90 → 80 → 70 → 60     (chamber soak, then bed soaks)
chamber 50: bed 110 → … → 60                      (wait to cool; long soak at 110)
chamber 40: bed 110 → … → 60
…
```

* **The chamber is soaked once**, at the first pair (`CHAMBER_SOAK`). It is
  held at each lower target by its heater after that.
* **Down to the next chamber target**, the bed is switched off and the sweep
  waits for the chamber to fall to within `chamber_tolerance` of the new
  target. If it has not within `COOL_TIMEOUT` — the enclosure holds heat
  better than expected — that whole set is skipped and the sweep moves on to
  the next lower one.
* **Within a set the bed goes from hottest to coolest.** The first step soaks
  for `MAX_SOAK` (the bed has just come back from off), every later one for
  `SOAK`.
* **The bed is never below the chamber.** Bed targets below the chamber
  target are left out of the plan; at each pair, a bed target below the
  chamber's measured temperature is skipped too. A bed cannot be held below
  the air around it.

Skipped sets and pairs are listed under `"skipped"` in the map file and by
`THERMAL_MESH_STATUS`. The map that results is triangular rather than a full
grid; the analysis below does not need a full grid.

At every pair it waits for the bed (±`bed_tolerance`) and the chamber (at
least target − `chamber_tolerance`; a hot bed may carry the chamber past its
target, which is recorded rather than waited out), soaks, then probes full
meshes until one verifies the one before it (next section), and stores their
average. After `max_samples` it stores the last two anyway and marks the pair
`"converged": false`. If a heater does not reach its target within
`heat_timeout` the sweep stops; fix the cause and run it again with `RESUME=1`.

### Verifying a mesh

Every mesh after the first at a pair is a **verification** of the one before.
Straight after it, the probe touches the middle of the mesh
`precision_samples` times (10) and the standard deviation of those touches is
the probe's precision σ, measured at that moment, on that plate, at that
heat. If the plate has stopped moving, the difference between the two meshes
at each node is just two probe readings apart: mean 0, variance 2σ². Two
tests check that, on the n node differences d:

| Test | Statistic | Passes when |
|---|---|---|
| **average error is 0** | n · mean(d)² / 2σ² | below the F(1, dof) quantile |
| **every node agrees** | Σ(d − mean(d))² / ((n − 1) · 2σ²) | below the F(n − 1, dof) quantile |

dof is the precision touches less one; F rather than normal limits account
for σ itself being measured. The two tests use independent parts of the
differences: the average, and what is left once it is taken out. With a
`zero_reference_position` in `[bed_mesh]`, one reference touch shifts a whole
mesh, and the first test widens its limit to match. Each test runs at half the remaining risk
(97.5 % for `CONFIDENCE=0.95`), so the two together hold at 95 %: the meshes
are accepted only when nothing in them tells them apart from the same plate
probed twice. The first test catches the whole plate still rising or falling
by a fraction of σ — 25 nodes average it out of the noise — and the second a
single region that is still moving while the average stays put.

A noisy enough probe makes any two meshes look alike, so a verification also
needs σ at or below `MAX_SIGMA`. The console line for each verification gives
the mean error with its 95 % interval, the rms difference, σ, and both
statistics against their limits; the map entry keeps them under
`"verification"`.

Each pair goes to `thermal_mesh.json` beside `printer.cfg` as soon as it is
done, along with the measured bed and chamber temperatures at the start and
end of probing — the analysis uses those, not the targets. The file shows up
in Mainsail's config file list.

The sweep runs in the background. `THERMAL_MESH_STATUS` shows where it is;
`THERMAL_MESH_ABORT` stops it at the end of the current step (a full mesh
probe in progress finishes first). Starting a print, or a Klipper shutdown,
stops it too. It raises the idle timeout while it runs and puts it back
afterwards, and turns both heaters off at the end unless `KEEP_HEAT=1`.

A 6 × 4 map with a 10-minute soak and three samples per pair takes most of a
day. Halve the steps for a first look.

---

## 2. Separate bed and chamber: `THERMAL_MESH_ANALYZE`

Runs by itself at the end of a sweep; run it again to re-pick points.

```gcode
THERMAL_MESH_ANALYZE [POINTS=4] [MIN_SPACING=60]
```

Both heaters change the plate's shape, and they are not independent: a hot
bed warms the chamber, so a pair swept at chamber 40 °C and bed 110 °C may
really have been at 44 °C. Comparing map entries by their targets would put
the chamber's share of that warp on the bed.

So for every mesh node the analysis fits a straight line in both
temperatures at once, by least squares over every entry, using the
temperatures **measured** while the entry was probed:

```
Z = base + bed slope × (bed − bed ref) + chamber slope × (chamber − chamber ref)
```

The bed slope is how much that node moves per °C of bed with the chamber held
still, and the chamber slope the reverse. This works on the triangular map the
sweep produces and on one with skipped pairs. It cannot work if the bed and
chamber always moved together (the report says so); with a single chamber
target only the bed is fitted.

It then picks the probe points. Not simply the nodes that move most: if the
bed and the chamber both bow the middle of the plate, the most sensitive nodes
for each are all in the middle, and four points there cannot tell a hot bed
from a warm chamber. Instead each point is chosen to add the most
information about both temperatures at once — the first is the node that
moves most overall, later ones where the bed and the chamber warp the plate
differently — skipping any node closer than `MIN_SPACING` mm to one already
chosen (halving the spacing if that leaves too few).

The report gives, for each heater, the peak and median slope in µm/°C — if
the peak is not well above the median, that heater barely changes the bed —
the correlation between the measured bed and chamber temperatures in the map,
how far the straight-line model is from the map (curvature and noise), and
the **overlap** of the two warps at the chosen points: 0 means the points
tell them apart perfectly, near 1 that they cannot. Each point is labelled
with the heater that moves it more.

---

## 3. Use it: `THERMAL_MESH_APPLY`

### In prints

`BED_MESH_MODE`'s default, `auto`, already uses the map: it probes the four
points at every print start and loads the match, unless the match is more
than 0.05 mm rms off what it probed (`variable_auto_max_rms`) or there is no
map, in which case it probes an adaptive mesh instead. To use the map
without that safety net:

```gcode
BED_MESH_MODE MODE=thermal
```

From then on `START_PRINT`'s mesh step probes the map's four points, after
the print's soak and Z homing, and loads the effective mesh. The console says
`Bed mesh mode: THERMAL`. Without a map or chosen points it says so and loads
`MESH_DATA`, so a print never fails for lack of one. `START_PRINT
MESH=thermal` does the same for one print.

The choice is not kept across a restart. To make it the default, add this at
the end of `printer.cfg`; `variable_thermal_method`, `variable_thermal_match`
and `variable_thermal_correct` set the options below the same way:

```
[gcode_macro BED_MESH_MODE]
variable_mode: 'thermal'
```

### By hand

In place of `BED_MESH_PROFILE LOAD=…` or `BED_MESH_CALIBRATE`, after the
heat soak:

```gcode
THERMAL_MESH_APPLY [METHOD=model] [MATCH=absolute] [CORRECT=none] [SAVE=<profile>] [DRY_RUN=1]
```

It runs `before_sample_gcode` (park, `G28 Z`, as the sweep did before every
mesh), probes the four points, compares them with every mesh in the map,
builds an effective mesh and makes it the active one. `MAX_RMS=<mm>` refuses
a match whose residual rms (in the `MATCH` sense) is larger, and loads
nothing; `FALLBACK=NONE` makes that, and a missing map, a recorded result
rather than an error -- `printer.ff_thermal_mesh.last_apply.accepted` and
`.reason`, which `auto` reads. `PREPARE=0` skips the
park and `G28 Z` when the caller has just done both; `FALLBACK=<profile>`
loads that profile, with a message, when there is no map. `REUSE_PROBE=1`
takes the points an APPLY probed in the last 10 minutes instead of touching
them again -- how `auto` falls back through `adaptive` to an unlimited
thermal match without probing twice. `SAVE=` also stores it
as a bed-mesh profile; `DRY_RUN=1` only reports. The report names the
matched temperatures, the blend, and the residual at each point — the
distance between what was probed and the mesh that was loaded.

### Choosing the mesh: `METHOD`

| Method | What it does | Use it when |
|---|---|---|
| `nearest` | The single map entry whose four points are closest (RMS). | Checking the map; or a map so dense the neighbours barely differ. |
| `idw` | Inverse-distance blend of the `K` closest entries (default 4), weight 1/rms^`POWER` (default 2). An exact hit returns that mesh unchanged. | The bed's state is between samples but not on a clean grid, e.g. after a `RESUME` left gaps. |
| `model` (default) | Solves for the bed and chamber temperatures whose model mesh (above) best fits the probed points, by least squares, clamped to the measured range. Loads the model's mesh at those temperatures, plus what the straight line misses there, taken from the `K` closest map entries (default 4). | Normally. It works anywhere in the swept range, including along the triangular map's diagonal, and keeps bed and chamber apart. |
| `bilinear` | Treats the map as a grid over (chamber, bed) and searches every fully sampled cell for the point whose bilinearly interpolated four values best fit the probe. Loads the mesh interpolated at that point and reports the chamber and bed temperatures it corresponds to. | A dense, full grid. Cells missing a corner — along the diagonal where the sweep skips a bed below the chamber — are not searched. |
| `temperature` | No probing: interpolates the map at the current bed and chamber readings (or `BED=` / `CHAMBER=`), clamped to the swept range. | A quick start, or to compare with what the probe says. A large difference between the two means the plate is not at the state its thermistors suggest — usually an unfinished soak. |

### Comparing: `MATCH`

`absolute` (default) compares heights as probed. Both the map and this probe
home Z the same way first, so the absolute height carries information: the
whole plate rising is part of the thermal state. `relative` subtracts each
side's mean first and matches shape only — use it if the Z homing itself
drifts with temperature.

### Meeting the probe: `CORRECT`

The effective mesh will not pass exactly through the four probed points. The
residual can be folded in:

* `none` (default) — load the interpolated mesh as it is;
* `offset` — shift it by the mean residual;
* `plane` — add a least-squares plane through the four residuals, which also
  absorbs a tilt (a plate seated differently from the one mapped).

Use `offset` together with `MATCH=relative`, so the shape comes from the map
and the height from today's probe.

### Other strategies worth trying

These are not built in, but the map file has everything they need:

* **Hybrid prior.** Start `model`'s fit at the `temperature` estimate and
  penalise distance from it, so a noisy probe cannot pick a wildly different
  state than the thermistors suggest.
* **Curved model.** Add a squared bed term (or a bed × chamber term) to the
  per-node fit if the report's straight-line error is large compared with
  the probe's σ; that costs one more unknown per term and wants more points.
* **Radial basis / thin-plate correction.** Instead of a plane, spread the
  four residuals with a thin-plate spline, so a local disagreement stays
  local.
* **More points.** `THERMAL_MESH_ANALYZE POINTS=6` costs two more probes and
  makes every method more robust to one bad touch.

---

## Settings

`[ff_thermal_mesh]` in `ff-print-macros.cfg`. To change one, restate the
section with just that option at the end of `printer.cfg`.

| Option | Default | |
|---|---|---|
| `bed_heater` | `heater_bed` | |
| `chamber_heater` | `chamber_heater` | Set through `SET_HEATER_TEMPERATURE`, so the chamber fan coupling applies. |
| `chamber_sensor` | (heater, then `temperature_sensor chamber`) | Object to read the chamber temperature from. |
| `bed_tolerance` | 1.0 | °C either side of the bed target. |
| `chamber_tolerance` | 2.0 | °C below the chamber target. |
| `heat_timeout` | 5400 | Seconds to reach a pair's targets before the sweep stops. |
| `cool_timeout` | 3600 | Seconds the chamber has to cool to a lower target before that set is skipped (`COOL_TIMEOUT`). |
| `max_samples` | 6 | Full meshes per pair at most. |
| `precision_samples` | 10 | Touches at the middle of the mesh after each verification mesh, to measure σ. |
| `confidence` | 0.95 | `CONFIDENCE`'s default. |
| `max_probe_sigma` | 0.01 | `MAX_SIGMA`'s default, mm. |
| `min_point_spacing` | 60 | mm between chosen points. |
| `before_sample_gcode` | park, `G28 Z`, `BED_MESH_CLEAR`, fan off | Before every mesh in the sweep and before the probe in `APPLY`. |
| `after_sample_gcode` | 20000 accel, `G1 Z10` | After each. |
| `data_file` | `thermal_mesh.json` beside `printer.cfg` | |

The four-point probe travels and lifts exactly as `BED_MESH_CALIBRATE` does,
with `[ff_bed_mesh]`'s `travel_speed` and lift.

`printer.ff_thermal_mesh` reports the sweep's state, pair, phase, sample count
last mean ΔZ and probe σ, and the last `APPLY`'s blend and residuals.
