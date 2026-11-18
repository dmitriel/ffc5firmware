# Reforge

Custom firmware for the FlashForge **Creator 5** and **Creator 5 Pro**.

It installs the way a normal FlashForge update does: copy one file to a USB
stick, plug it in, power the printer on. No jailbreak, no soldering, no
opening the case, no ssh needed to get started — the printer's own updater
does the work.

Packages are published on the
[Releases page](https://github.com/Klipper4FlashForge/firmware/releases) —
two files per release, one per model, marked *pre-release* while the mod is
young. Help and support: [the Discord](https://discord.gg/ggJyfgVA4v).

> **Unofficial and unaffiliated with FlashForge.** It voids your warranty and
> you are responsible for your machine. See [Status](#status) for what has
> actually been tried on hardware before you flash anything.

---

## What this fork adds

This is a fork of [Reforge](https://github.com/Klipper4FlashForge/firmware).
Everything below is on top of what Reforge already changes from FlashForge's
stock firmware; the packages on Reforge's Releases page do not carry it, so
[build from this repository](docs/building.md) to get it. Items marked 🧪
are replica- and unit-tested but **not yet tested on hardware**.

| | Stock FlashForge | Reforge | This fork |
|---|---|---|---|
| Print start | The touchscreen app prepares every print | `START_PRINT` runs automatically before every file | **Runs only when called** — from Machine start G-code ([ready block](gcode/orca-machine-start-flow-tilt.gcode)) or by you |
| Bed mesh choice | Fixed saved mesh | Fixed or adaptive | **`BED_MESH_MODE auto\|off\|fixed\|adaptive\|thermal`**, adaptive mesh built into `START_PRINT` |
| Thermal mesh map | — | — | 🧪 **Sweep** bed × chamber temperatures, then probe 4 points per print to load the matching mesh ([page](docs/thermal-mesh.md)) |
| Flow mesh | — | — | 🧪 Nozzle stays on the flat sliced plane; the first layers **absorb the plate in their thickness**, extrusion scaled to match ([page](docs/flow-mesh.md)) |
| Tilt correction | — | — | 🧪 The print's frame is **tilted onto the bed's fitted plane**, so a plate whose tramming moved with heat no longer makes parts lean ([page](docs/tilt-correction.md)) |
| Bed-collision guard | — | — | 🧪 Any move that would bring the nozzle or bare carriage within 0.1 mm of the plate is refused (`SET_BED_GUARD`) |
| Tool remapping | Not comparable (FlashForge's app UI) | HelixScreen's remap is refused by the printer | 🧪 `ASSIGN_TOOL TOOL=T2 N=0`, before or during a print; HelixScreen's tool mapping works |
| Pre-print clean | Every used tool cleaned and docked, then the first grabbed | Same as stock | Tools preheat to −100 °C beside the bed; the **first tool is cleaned last**, hot, and left mounted and primed; `PURGE=<mm>` |
| Mesh probing speed | 3 touches per point, 200 mm/s travel | 3 touches, 2 mm lift between points | 2 touches (3rd only on disagreement), 500 mm/s travel, minimal lift between points |
| Exclude object | n/a | Toolchange could fail (`E0138`) after excluding the object holding the prime tower | Toolchange and pause moves are never excluded |
| "Timer too close" mid-print | n/a | Possible after a toolchange followed by many tiny moves | 🧪 File reader yields to Klipper every 20 ms |
| Resume after a purge/load in a pause | n/a | Diagonal move back could hit a docked tool | Backs out of the dock bay, lifts, crosses, then descends |

### Starting a print on this fork

Because the print start is no longer implicit, **a file must call
`START_PRINT` itself**. Paste
[`gcode/orca-machine-start-flow-tilt.gcode`](gcode/orca-machine-start-flow-tilt.gcode)
into OrcaSlicer's *Printer settings → Machine G-code → Machine start G-code*:
it turns on the flow mesh and tilt correction, heats the chamber, and runs
`START_PRINT` with every tool the file uses. The core of it:

```gcode
M141 S{chamber_temperature[initial_extruder]}
FLOW_MESH_TOGGLE ENABLE=1
TILT_CORRECTION_TOGGLE ENABLE=1
START_PRINT BED=[bed_temperature_initial_layer_single] TOOL=[initial_extruder] NOZZLE={nozzle_temperature_initial_layer[initial_extruder]} LAYER=[initial_layer_print_height] TOOLS={if is_extruder_used[0]}0:{nozzle_temperature_initial_layer[0]},{endif}{if is_extruder_used[1]}1:{nozzle_temperature_initial_layer[1]},{endif}{if is_extruder_used[2]}2:{nozzle_temperature_initial_layer[2]},{endif}{if is_extruder_used[3]}3:{nozzle_temperature_initial_layer[3]},{endif}
```

A file from FlashForge's untouched profile now stops at its first move with
"Must home axis first" — nothing moves before that. To print such files
unchanged, as upstream Reforge does, add this at the end of `printer.cfg`:

```ini
[gcode_macro FF_BEFORE_PRINT_START]
variable_prepare: 1
```

The calibration and tool-presence gate before a print, and the exit sequence
after it, run either way. Details: [Printing](docs/printing.md#slicer-setup)
and [How a print runs](docs/how-a-print-runs.md).

---

## Status

**It runs on real printers** — both a Creator 5 Pro and a Creator 5 have been
flashed and exercised; the pictured hardware is the Pro:

<img src="docs/helixscreen-print-status.webp" width="49%" alt="HelixScreen mid-print on the Creator 5 Pro: T2 mounted, layer 2 of 173"> <img src="docs/helixscreen-bed-mesh.webp" width="49%" alt="HelixScreen's bed-mesh screen showing a probed 10x10 mesh">

A checked box happened on that machine; an empty one has not, yet.

**For everyone:**

- [x] **Install and migration from stock firmware** — the printer's own
  updater does it; your unit's factory calibration imports itself on the
  first boot
- [x] **Printing** — gcode straight from the slicer (on this fork, with a
  Machine start G-code that calls `START_PRINT`; see
  [above](#starting-a-print-on-this-fork))
- [x] **Toolchanges** — a current Klipper with real toolchanger support, in
  place of the 0.12-era tree FlashForge ships
- [x] **Nozzle-offset calibration**
- [x] **Mainsail** at `http://<printer-ip>/`, with a current Moonraker behind
  it — anything that speaks the Klipper API works
- [ ] **Fluidd** at `http://<printer-ip>:81/`, beside Mainsail and using the
  same Moonraker and camera
- [x] **Camera** — mjpg-streamer, already on the printer; the mod just
  starts it
- [x] **Wifi**
- [x] **HelixScreen** on the printer's own screen
- [x] **Chamber heater** — tested on the Creator 5 Pro; the regular Creator 5
  correctly uses its heater-free chamber configuration
- [x] **Creator 5 (non-Pro)** — package installed and exercised on hardware;
  see the feature-specific validation notes in the [feature list](docs/features.md)
- [x] **Going back to stock** — tested with the matching stock packages; the
  stock package restores everything it carries. Moonraker is not in it, so
  the mod's build stays and keeps working — see [Support](docs/support.md)

**For the tinkerer:**

- [x] **ssh as root** — a random password is chosen on the **first** install
  and written to `anvil-password.txt` on your stick; updates keep it
  (`make passwd` bakes in your own)
- [ ] **Custom boot scripts** — ordered `*.sh` hooks under
  `/usr/data/anvil-data/scripts`, preserved across updates and logged

---

## What you give up

FlashForge's application is one binary, and replacing it takes everything
that lived inside it: **the network API on `:8898`**, and with it FlashPrint,
Orca's FlashForge profile and the mobile app. Moonraker's API takes over.
Flashing the stock package brings them back.

If your prints already come out the way you want and you like the printer's
own screen, this mod is not for you. It trades FlashForge's polish for
Klipper's reach, and it expects you to be comfortable with a Klipper config
and an ssh session when something breaks —
[Is this for you](docs/is-this-for-you.md) is the honest version of that
question.

---

## Get it

**Creator 5 and Creator 5 Pro are not interchangeable.** The printer refuses
a package built for the other model — the most common reason a flash "does
nothing". Yours is in Settings → About.

| Your printer | The file on the [Releases page](https://github.com/Klipper4FlashForge/firmware/releases) |
|---|---|
| Creator 5 | `Creator5-anvil-<date>.tgz` |
| Creator 5 Pro | `Creator5Pro-anvil-<date>.tgz` |

Download the stock FlashForge package for your model before you begin. It is
the undo button: flashing it back puts the printer as it was.

---

## Start here

| | |
|---|---|
| [Is this for you](docs/is-this-for-you.md) | Who should flash this, who should stay on stock, and what it asks of you |
| [Installing](docs/installing.md) | The flash, the first boot, your root password |
| [Your first print](docs/first-print.md) | The go/no-go checks, calibration, and the two verification files |
| [Support](docs/support.md) | The logs, and the way back to stock |

The full manual is at
[reforge.readthedocs.io](https://reforge.readthedocs.io/). If you want the
insides — one file, `firmwareExe`, with the stock boot scripts, updater and
recovery path left untouched — [How it works](docs/how-it-works.md) is under
*For contributors*.

For the complete beta inventory and validation boundaries, see the
[feature list](docs/features.md).

---

## Credits

Built on the work of others: [ghzserg/FF](https://github.com/ghzserg/FF)
publishes the stock packages and factory image everything here starts from;
[Mainsail](https://github.com/mainsail-crew/mainsail),
[Moonraker](https://github.com/Arksine/moonraker) and
[HelixScreen](https://github.com/Klipper4FlashForge/helixscreen) are shipped as
released; the toolchanger status API follows
[viesturz/klipper-toolchanger](https://github.com/viesturz/klipper-toolchanger)
so tool-aware UIs work unchanged.
