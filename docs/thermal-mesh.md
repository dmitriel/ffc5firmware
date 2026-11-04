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
THERMAL_MESH_SWEEP BED_MIN=60 BED_MAX=110 BED_STEP=10 CHAMBER_MIN=30 CHAMBER_MAX=60 CHAMBER_STEP=10 SOAK=600 CHAMBER_SOAK=1800 THRESHOLD=0.01
```

| Parameter | Default | Meaning |
|---|---|---|
| `BED_MIN`, `BED_MAX`, `BED_STEP` | — , `BED_MIN`, 10 | Bed targets, °C. The last step is always `BED_MAX`. |
| `CHAMBER_MIN`, `CHAMBER_MAX`, `CHAMBER_STEP` | 0, `CHAMBER_MIN`, 10 | Chamber targets. `0` means unheated; on the plain Creator 5 leave both at 0. |
| `SOAK` | 600 | Seconds to soak after a bed step, once both heaters are at target. |
| `CHAMBER_SOAK` | `SOAK` | Seconds to soak after a chamber step (and at the first pair). The chamber is slow; make this the longer one. |
| `THRESHOLD` | 0.01 | mm. Meshes are probed again until the mean \|ΔZ\| between the last two is below this. |
| `RESUME` | 0 | `1` keeps the pairs already in the map and continues. Without it an existing map is moved to `thermal_mesh.json.bak`. |
| `KEEP_HEAT` | 0 | `1` leaves the heaters at the last pair's targets when the sweep finishes. |
| `CHAMBER_POINTS`, `BED_POINTS` | 2, 2 | How many probe points the analysis at the end picks for each axis. |

The order is a serpentine, so every move changes one heater by one step:

```
chamber 30: bed 60 → 70 → … → 110
chamber 40: bed 110 → 100 → … → 60
chamber 50: bed 60 → … → 110
…
```

At every pair it waits for the bed (±`bed_tolerance`) and the chamber (at
least target − `chamber_tolerance`; a hot bed may carry the chamber past its
target, which is recorded rather than waited out), soaks, then probes full
meshes. When two in a row agree to within `THRESHOLD` their average is
stored. After `max_samples` it stores the last two anyway and marks the pair
`"converged": false`. If a heater does not reach its target within
`heat_timeout` the sweep stops; fix the cause and run it again with `RESUME=1`.

Each pair goes to `thermal_mesh.json` beside `printer.cfg` as soon as it is
done, along with the measured bed and chamber temperatures at the start and
end of probing. The file shows up in Mainsail's config file list.

The sweep runs in the background. `THERMAL_MESH_STATUS` shows where it is;
`THERMAL_MESH_ABORT` stops it at the end of the current step (a full mesh
probe in progress finishes first). Starting a print, or a Klipper shutdown,
stops it too. It raises the idle timeout while it runs and puts it back
afterwards, and turns both heaters off at the end unless `KEEP_HEAT=1`.

A 6 × 4 map with a 10-minute soak and three samples per pair takes most of a
day. Halve the steps for a first look.

---

## 2. Find the points that move: `THERMAL_MESH_ANALYZE`

Runs by itself at the end of a sweep; run it again to re-pick points.

```gcode
THERMAL_MESH_ANALYZE [CHAMBER_POINTS=2] [BED_POINTS=2] [MIN_SPACING=60]
```

For every mesh node it computes two numbers, in µm per °C:

* **chamber sensitivity** — the RMS of ΔZ/ΔT between neighbouring chamber
  targets, at the same bed target, over every bed target;
* **bed sensitivity** — the same between neighbouring bed targets at the same
  chamber target.

It then takes the most sensitive node for the chamber, the most sensitive for
the bed, the next for the chamber, the next for the bed, skipping any node
closer than `MIN_SPACING` mm to one already chosen (halving the spacing if
that leaves too few). With a single chamber target all four go to the bed.
The report prints the peak and median of each map — if the peak is not well
above the median, that axis barely changes the bed and the points for it are
mostly noise — and the four points are saved in the map file.

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
THERMAL_MESH_APPLY [METHOD=bilinear] [MATCH=absolute] [CORRECT=none] [SAVE=<profile>] [DRY_RUN=1]
```

It runs `before_sample_gcode` (park, `G28 Z`, as the sweep did before every
mesh), probes the four points, compares them with every mesh in the map,
builds an effective mesh and makes it the active one. `MAX_RMS=<mm>` refuses
a match whose residual rms (in the `MATCH` sense) is larger, and loads
nothing; `FALLBACK=NONE` makes that, and a missing map, a recorded result
rather than an error -- `printer.ff_thermal_mesh.last_apply.accepted` and
`.reason`, which `auto` reads. `PREPARE=0` skips the
park and `G28 Z` when the caller has just done both; `FALLBACK=<profile>`
loads that profile, with a message, when there is no map. `SAVE=` also stores it
as a bed-mesh profile; `DRY_RUN=1` only reports. The report names the
matched temperatures, the blend, and the residual at each point — the
distance between what was probed and the mesh that was loaded.

### Choosing the mesh: `METHOD`

| Method | What it does | Use it when |
|---|---|---|
| `nearest` | The single map entry whose four points are closest (RMS). | Checking the map; or a map so dense the neighbours barely differ. |
| `idw` | Inverse-distance blend of the `K` closest entries (default 4), weight 1/rms^`POWER` (default 2). An exact hit returns that mesh unchanged. | The bed's state is between samples but not on a clean grid, e.g. after a `RESUME` left gaps. |
| `bilinear` (default) | Treats the map as a grid over (chamber, bed) and searches every fully sampled cell for the point whose bilinearly interpolated four values best fit the probe. Loads the mesh interpolated at that point and reports the chamber and bed temperatures it corresponds to. | Normally. Expansion is close to linear over one step, so this reproduces states between the sampled ones rather than snapping to one. |
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

* **Hybrid prior.** Start `bilinear`'s search at the `temperature` estimate and
  penalise distance from it, so a noisy probe cannot pick a wildly different
  state than the thermistors suggest.
* **Modal fit.** Take the principal components of the map's meshes (most
  plates are one bow and one tilt), and fit their coefficients to the four
  points by least squares. This can produce shapes between, not only on, the
  map's grid lines, and four points determine up to four modes.
* **Radial basis / thin-plate correction.** Instead of a plane, spread the
  four residuals with a thin-plate spline, so a local disagreement stays
  local.
* **More points.** `CHAMBER_POINTS=3 BED_POINTS=3` costs two more probes and
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
| `max_samples` | 6 | Full meshes per pair at most. |
| `min_point_spacing` | 60 | mm between chosen points. |
| `before_sample_gcode` | park, `G28 Z`, `BED_MESH_CLEAR`, fan off | Before every mesh in the sweep and before the probe in `APPLY`. |
| `after_sample_gcode` | 20000 accel, `G1 Z10` | After each. |
| `data_file` | `thermal_mesh.json` beside `printer.cfg` | |

The four-point probe travels and lifts exactly as `BED_MESH_CALIBRATE` does,
with `[ff_bed_mesh]`'s `travel_speed` and lift.

`printer.ff_thermal_mesh` reports the sweep's state, pair, phase, sample count
and last ΔZ, and the last `APPLY`'s blend and residuals.
