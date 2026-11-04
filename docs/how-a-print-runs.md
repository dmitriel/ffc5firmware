# How a print runs

The mod is built so that **the same file prints on both firmwares**. A stock
FlashForge Orca profile, untouched, and files you sliced before you flashed:
they run here without a re-slice and without a line pasted into Machine start
G-code.

That is not a coincidence, and it is worth understanding if you intend to
change anything — because the reason it works is that something now does, in
Klipper, what FlashForge's application used to do outside it.

---

## Why the same file works

A FlashForge Orca profile is written for a machine whose touchscreen has
already prepared it. Its start block heats without waiting, runs fans, and
makes `G1 Z5 F2400` its first motion — on axes nothing has homed. There is no
`G28` in it and no `M190`. On stock that is fine: the app homed, soaked the
bed and grabbed the first tool before the file was ever handed to Klipper.

Delete the app and that file is dangerous. So `[ff_print]` takes over
`SDCARD_PRINT_FILE` and `M23`, reads the bed and nozzle temperatures, the
first tool and the first-layer height **out of the file itself**, and runs the
whole preparation before the file's first line executes.

The file did not change. What changed is that the preparation moved from an
application nobody else could talk to into Klipper macros anyone can read.

---

## The two flows

**Stock, driven by the touchscreen:**

```
touchscreen: pick a file, Print
  │
  ├─ app: prepareForEddy ──── home, probe checks, heat bed + chamber,
  │                           tool-presence gate, nozzle clean per tool,
  │                           5-minute soak, re-home Z, bed mesh, grab tool
  ├─ app: absolute Z offset ─ the ~3.2 mm eddy-to-nozzle gap, once per print
  └─ app: serialPrint ─────── M21 → M23 → M26 S0 → M24
        │
        ▼
     Klipper streams the file
        │
        ├─ a bare `T2` ─────► the fork raises a flag and WAITS for the app
        │                     to perform the change
        └─ pause / resume / cancel ─► the app sends each sequence itself
```

**Reforge, driven by whatever you like:**

```
Mainsail, HelixScreen, OrcaSlicer — anything that speaks Moonraker
  │
  └─ [ff_print] wraps SDCARD_PRINT_FILE and M23
        │   reads bed, nozzle, first tool and first-layer height from the file
        │
        ├─ FF_BEFORE_PRINT_START ─┬─► _FF_PREFLIGHT  calibration + tool gate
        │                         └─► START_PRINT    the preparation sequence
        ▼
     Klipper streams the file
        │
        ├─ a bare `T2` ─────► ff_toolchange performs the change
        └─ PAUSE / RESUME / CANCEL_PRINT ─► macros in ff-print-macros.cfg
        │
        ▼
     FF_AFTER_PRINT_END ──► END_PRINT   the exit block
```

The shape is the same. The difference is that every box in the second diagram
is a Klipper macro or a Klipper extra, and every box in the first was inside
one binary.

Step by step, with the app's own sequence, the fork code that makes a
Mainsail print hang, and what `START_PRINT` actually sends:
[The print pipeline](print-pipeline.md).

---

## Changing what it does

Everything below goes at the **end of your `printer.cfg`**, restating only
what you are changing. Klipper merges same-named sections and the last value
wins, so the shipped defaults stay for everything you leave out, and your
overrides survive every update. Never edit the `ff-*.cfg` files themselves —
an update replaces them without asking.

### Turn off the automatic preparation

If your own profile already calls `START_PRINT`, or homes for itself:

```ini
[gcode_macro FF_BEFORE_PRINT_START]
variable_prepare: 0
```

Preparing twice misplaces nothing — the Z offset and `T<n>` are both
idempotent — but it re-homes and re-purges every tool for no reason. A
profile that runs its own `G28` **needs** this off: our `G28` docks a mounted
tool before homing Z, so it would dock the tool preparation just grabbed and
print with an empty carriage.

### Drive the sequence from the slicer

With `prepare: 0`, call it yourself from Machine start G-code:

```gcode
START_PRINT TOOLS=0:220,2:240 BED=60 MESH=adaptive SOAK=300
```

| Parameter | Does |
|---|---|
| `TOOLS=0:220,2:240` | every tool the file uses, with its clean temperature. A bare `TOOLS=0,2` also works |
| `BED=`, `NOZZLE=`, `LAYER=` | the print's bed, nozzle and first-layer height |
| `CLEAN=0` | skip the pre-print purge and wipe; the first tool is just grabbed |
| `PURGE=<mm>` | purge length for every tool's clean (default 50). `PURGE=0` wipes without purging |
| `MESH=` | `auto`, `off`, `fixed`, `adaptive` or `thermal` for this print, instead of `BED_MESH_MODE`'s choice (below) |
| `SOAK=<seconds>` | dwell after the bed reaches target. The app waited 5 minutes; the default here is 0 |

The order: the bed and every tool start heating at once — the tools to 100 °C
below their print temperature. Then a full home; the other tools are purged,
wiped and parked at that standby temperature while the bed heats; the bed
wait and soak; the Z re-home and the mesh; the print offset; and **last**
the tool the print starts with is purged and wiped at print temperature and
left mounted, so the file's first move follows straight on. That last wipe is hot -- the nozzle
rests a second on the pad (`clean_hot_dwell` in `ff-filament.cfg`) and lifts
-- so it tears off a little less ooze than the cold wipe the others get.

### Which mesh: `BED_MESH_MODE`

```gcode
BED_MESH_MODE MODE=adaptive     ; auto | off | fixed | adaptive | thermal
BED_MESH_MODE                   ; show the current choice
```

| Mode | The mesh step of `START_PRINT` |
|---|---|
| `auto` | **the default.** The thermal map's match, as `thermal` — but if there is no map, or the four probed points are more than 0.05 mm rms (`variable_auto_max_rms`) off the best the map can produce, it probes `adaptive` instead. Until you have swept a map, that means an adaptive probe every print |
| `off` | no mesh: the print runs on the homed plane, and the flow mesh is not started |
| `fixed` | loads the saved `MESH_DATA` — the factory mesh, or yours saved over it. No probing. With no `MESH_DATA` at all it probes the whole bed into it once; run `SAVE_CONFIG` after that print to keep it |
| `adaptive` | probes only where the print is: the file's objects and its prime tower, plus 16 mm (`variable_adaptive_margin`). A file without labelled objects (Orca's *Label objects* and *Exclude objects* off) uses `fixed` instead |
| `thermal` | probes the [thermal map](thermal-mesh.md)'s four points and loads the matching mesh; `fixed` without a map |

The mesh step runs after the soak and the Z re-home, with every tool docked,
and before the first tool's clean, so the print still starts straight after
the wipe. For `adaptive`, `[ff_print]` reads the file's
`EXCLUDE_OBJECT_DEFINE` lines (Orca writes them at the top when *Label
objects* is on) and defines them before probing, and defines the prime tower
from the file's own moves rather than the numbers Orca expands into start
G-code, which in a multi-plate project can belong to another plate. Nothing
is needed in the slicer's start G-code.

The console says which it took: `Bed mesh: thermal match accepted (0.012 mm
rms)`, or `thermal not used (...); probing adaptive` with the reason.

`START_PRINT MESH=<mode>` overrides it for one print. The choice is not kept
across a restart; to make one the default, add this at the end of
`printer.cfg`:

```
[gcode_macro BED_MESH_MODE]
variable_mode: 'adaptive'
```

A profile that still calls `ADAPTIVE_MESH` keeps working: it now only says
the mesh was already taken. Remove the call when convenient.

---

## Where to read the real thing

The macros are on the printer at `/usr/data/anvil-data/config/ff-print-macros.cfg`, and
in this repo at
[`pkgs/klipper-config/payload/config/ff-print-macros.cfg`](../pkgs/klipper-config/payload/config/ff-print-macros.cfg).
They are ordinary Klipper macros with comments explaining why each step is
what it is.

The app's own sequences, recovered from the binary with addresses, are in
[Print lifecycle](notes/50-print-lifecycle.md), and the fuller division of
labour between application and Klipper is
[firmwareExe vs Klipper](notes/25-app-vs-klipper-ownership.md).
