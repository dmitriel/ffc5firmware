# Changelog

All notable Reforge changes are recorded here. The project is in beta: a
feature can be implemented and replica-tested without yet being validated on
both printer models or through every real-world workflow.

## Unreleased

### Added

- Tilt correction: the print's coordinate frame is tilted to stand square on
  the bed's fitted plane, so a plate whose tramming has moved with
  temperature no longer makes the part lean. `START_PRINT` scans the file in
  the background, fits a plane to the active mesh over the first-layer
  footprint (least squares, equal weight per area), tilts the frame about the
  footprint's centre -- the Z offset being the plane's height there -- and
  freezes that frame for the whole print. The frame is applied last, under
  bed mesh, so the bed mesh correction is rotated with it (applied along
  the plate's normal), fade follows the print's own height, and the flow
  mesh absorbs the plate relative to the tilted plane; a mesh cleared or
  reloaded mid-print never changes the frame. If a tilted move
  would go further outside the machine's travel than the file already does,
  the tilt is reduced until none does. `TILT_CORRECTION_TOGGLE ENABLE=1`
  uses it for the next print. Off by default and not yet tested on hardware. See [the tilt correction page](docs/tilt-correction.md).
- Flow mesh, a second bed-mesh strategy: the nozzle stays on the flat sliced
  plane and the first layers absorb the plate's unevenness in their
  thickness, with extrusion scaled to match. A layer is never squished
  thinner than half its own height (`min_flow`) or laid thicker than the
  nozzle diameter; past those the nozzle
  follows the mesh just far enough, and the remainder goes into the next
  layer, until the effective mesh is flat. The nozzle follows the mesh's
  average height, and only the unevenness around it is absorbed. No layer is
  extruded at more than 1.5× its flow (`max_flow`), and the speed is scaled
  against the flow so the volumetric rate stays as sliced. `FLOW_MESH_TOGGLE
  ENABLE=1` uses it for the next print. Off by default. The first hardware
  print showed the second layer too far above the first over a deep low spot,
  which the average reference and the flow cap address. See
  [the flow mesh page](docs/flow-mesh.md).
- One bed-mesh selector, `BED_MESH_MODE MODE=auto|off|fixed|adaptive|thermal`,
  for the mesh `START_PRINT` uses: none, the saved `MESH_DATA`, a fresh mesh
  over just the file's objects and prime tower, or the thermal map's match
  (`MESH_DATA` without a map). The default, `auto`, uses the thermal match
  and probes adaptive instead when there is no map or the match is more than
  0.05 mm rms off -- so until a map is swept, every print probes adaptive.
  A file without labelled objects gets the thermal match instead of a
  whole-bed probe -- with no rms limit, reusing points `auto` just probed --
  and the fixed mesh only when there is no thermal map; with no `MESH_DATA`
  saved, the fixed mesh probes the whole bed into it once (`SAVE_CONFIG`
  afterwards keeps it).
  `THERMAL_MESH_APPLY` takes `MAX_RMS=` and `FALLBACK=NONE` for it.
  `START_PRINT MESH=` overrides it for one print; `LEVEL=` is gone. The
  adaptive mesh is now part of `START_PRINT`
  -- `[ff_print]` defines the file's objects ahead of it -- so the slicer
  needs no `ADAPTIVE_MESH` call, and it runs before the first tool's clean.
  Every print says which mesh and strategy it uses. The flow mesh keeps its
  own `FLOW_MESH_TOGGLE`.
- Thermal mesh map: `THERMAL_MESH_SWEEP` probes full meshes over a range of
  bed and chamber temperatures, hottest chamber first and the bed from
  hottest down within each, never with the bed below the chamber. The
  chamber is soaked once (and at every chamber step if `CHAMBER_STEP_SOAK`
  is set); each lower chamber set waits for it to cool and is
  skipped if it does not within `COOL_TIMEOUT`; the first bed step of each set
  soaks longer (`MAX_SOAK`). Each pair is probed until a mesh verifies the one
  before it: the probe's precision is measured right after, and the two must
  be identical at 95 % confidence -- average error statistically 0 and every
  node within the probe's noise -- as many meshes as that takes. The pair's
  node-by-node mean is then stored, with how long after reaching temperature
  it settled (`soak_time`), in `thermal_mesh.json` with the temperatures
  measured around it. `THERMAL_MESH_ANALYZE` fits, per mesh node, the bed's
  and the chamber's warp separately against those measured temperatures, and
  picks the four points that best tell the two apart; `THERMAL_MESH_APPLY`
  probes only those, solves for the bed and chamber state that fits them and
  loads that mesh (`model`; also `bilinear`, `idw`, `nearest`, or
  `temperature` without probing). See
  [the thermal mesh page](docs/thermal-mesh.md).
- A bed-collision guard. Klipper did not know where the bed was, so a jog, a
  macro or a typed `G1` could drive the nozzle, or the carriage with no tool
  on it, into the plate. Now any move that would bring them within 0.1 mm of
  the bed is refused with a `Bed guard:` error, and nothing moves. With a tool
  mounted the limit is G-code Z 0.1 before any babystep, so a negative
  babystep on a 0.1 mm first layer will be refused. The bed counts as
  X/Y -7 to 263; the dock bay, purge chute and wipe spot (X above 263) are
  outside it. Homing, probing, the tool-offset and station calibrations, and
  a straight Z raise are never blocked. `SET_BED_GUARD` shows the state, and
  `SET_BED_GUARD ENABLE=0` turns it off until a restart, for a paper test.
  The settings are the `[ff_bed_guard]` section of `ff-toolchange.cfg`. With a
  mesh active it allows bed_mesh's own `split_delta_z` (0.025 mm) on top,
  since bed_mesh itself lets a long line sag that far below the mesh. Not
  yet tested on hardware.
- Bed-mesh probing lifts only as far as the probe needs to be ready again,
  plus 0.2 mm, between the touches at one point, and at least 1 mm
  (`min_travel_lift`) before moving to the next, instead of back to one fixed
  height; and travels X/Y
  at 500 mm/s and 20000 mm/s², the fastest the carriage moves anywhere else,
  instead of the stock 200 mm/s and 2000 mm/s². Each point is touched twice
  rather than three times, and a third time only if the two disagree, keeping
  whichever pair agrees; the point starts over only when none does. Other
  probing keeps three. Z speeds and
  acceleration are unchanged. Run
  `FF_PROBE_RELEASE_TEST SAVE=1` then `SAVE_CONFIG` once to measure it -- for
  the Creator 5's momentary eddy trigger, the lowest start height whose
  touches repeat those from 3 mm; until then the lift is 2 mm. The first move still goes to Z 5,
  and a calculated height under Z -2 falls back to Z 3. `FF_BED_MESH_STATUS`
  shows the settings; they are the `[ff_bed_mesh]` section of
  `ff-print-macros.cfg`. Contributed by
  [@nielssedat](https://github.com/nielssedat) in
  [#29](https://github.com/Klipper4FlashForge/firmware/pull/29).
- `DEFINE_PRIME_TOWER_OBJECT` registers the prime tower the file actually
  prints as an object, so an adaptive mesh covers it. (`ADAPTIVE_MESH`, which
  a slicer profile called for this, now only reports that `START_PRINT` took
  the mesh.)
- `printer.ff_print` reports the active plate's prime tower: position, size,
  brim, rotation and measured outline.
- AFC (the AFC-Klipper-Add-On) runs over the toolchanger, one lane per head.
  `SET_MAP` prints any slicer tool number on any head and is remembered across
  restarts; `SET_RUNOUT` names a backup head that takes over when a spool runs
  out mid-print; `SET_SPOOL_ID` gives each head its own Spoolman spool, made
  active whenever that head is picked up. Mainsail's AFC panel and
  HelixScreen's AFC backend drive all three. Installs and loads in the
  replica; not yet run on a printer. See
  [Filament and spools](docs/filament-and-spools.md).
- Gentler return to the print after a tool change: `[AFC] z_hop` lifts the
  old head before it leaves the print and brings the new one back at that
  height, `[ff_toolchange] dock_retract` pulls filament back while the new head
  is still seated in its dock so it does not string, and `dock_unretract`
  gives part of it back once AFC has put the head over the print. All zero as
  shipped, which changes nothing. The Z-hop and in-dock retract came from
  [#29](https://github.com/Klipper4FlashForge/firmware/pull/29) as
  `restore_z_hop`/`restore_retract`; under AFC they keep their effect under
  these names.

### Changed

- **`START_PRINT` no longer runs unless something calls it.**
  `FF_BEFORE_PRINT_START`'s `prepare` now defaults to 0: `[ff_print]` still
  runs the calibration and tool gate before a file and the exit sequence
  after it, but homing, cleaning and meshing happen only when the slicer's
  Machine start G-code (or you) calls `START_PRINT`. A file from
  FlashForge's untouched Orca profile stops at its first move with "Must
  home axis first", before anything moves; `variable_prepare: 1` at the end
  of `printer.cfg` brings the automatic preparation back. A ready Orca
  Machine start G-code that turns on the flow mesh and tilt correction is
  [`gcode/orca-machine-start-flow-tilt.gcode`](gcode/orca-machine-start-flow-tilt.gcode).

- `START_PRINT` heats every tool to 100 °C below its print temperature at
  the start, beside the bed, then cleans the other tools while the bed heats,
  and cleans the tool the print starts with **last**, after the mesh: purged,
  wiped hot -- the purge's retract pushed back out on the pad, then a 1 s
  rest (`clean_hot_dwell`) -- and left mounted at the print's own
  temperature, primed, so the print starts without reheating. The
  separate grab step is gone; the print Z offset is set just before that last
  clean. New `PURGE=<mm>` sets the purge length from the slicer
  (`PURGE=0`: wipe only). With the first head mounted, `START_PRINT` then
  sends the file's own `T<n>` so AFC holds that lane as the one in use.
- `START_PRINT` starts heating the bed before the first homing instead of after
  it, so the bed is closer to temperature when the nozzle clean ends.
- XYZ tool calibration now verifies plate removal with two non-contact
  carriage-probe readings before approaching the under-bed station, performs
  that check only once per run, and measures each nozzle at 200 C. Use
  `CALIBRATE_TOOL_OFFSETS TOOL=<n>` for a station plus one-tool run; `TEMP=`
  overrides the calibration temperature.
- AFC is updated to its 2026-09-16 DEV revision and multiple mapping is
  enabled: several slicer T-numbers can now select the same physical head.
  AFC now performs its own Moonraker writes off Klipper's reactor, replacing
  this firmware's `ff_afc` compatibility hook.
- `T0`..`T3` and `M104`/`M109 T<n>` follow AFC's map. The print-start tool
  check, calibration check and nozzle clean act on the heads the map picks.
  `SELECT_TOOL T=<n>` still selects a head by its own number.
- Filament load, unload and the pre-print nozzle clean take a head's
  temperature from AFC's record of what is in it (its own temperature, else
  the material table by its material). The fixed `tool_material` list is
  gone; with no material recorded the clean falls back to the print's
  `NOZZLE=` temperature instead of assuming PLA. AFC heats from the same
  material table.
- The presence switches' runout now belongs to AFC: a backup head, or a
  pause. Clogs still pause through the motion sensors as before.

### Removed

- `INITIALIZE_TOOLCHANGER`, `SET_TOOL_TEMPERATURE`, `VERIFY_TOOL_DETECTED`,
  `SELECT_TOOL_ERROR`, `ASSIGN_TOOL`, `TOOLCHANGE INDEX=`, the
  `RESTORE_AXIS=` parameter and `[ff_toolchange] restore_axis`/`restore_*`
  (see `z_hop` and `dock_retract` above). Nothing sends them with AFC present;
  `SELECT_TOOL T=<n>` on the mounted head replaces `INITIALIZE_TOOLCHANGER`
  as the recovery after an aborted change.

### Fixed

- Every tool no longer prints about 0.1 mm too high after
  `CALIBRATE_TOOL_OFFSETS`. The calibration measures each nozzle hot
  (200 C), so its `nozzle_z` already includes the nozzle's thermal growth,
  and the print-start thermal term, copied from the stock firmware that
  measures cold, added that growth a second time. Each calibration now records
  its temperature as `[ff_tool n] nozzle_temp`, and the toolchanger subtracts
  `calibration_expansion` (0.00057 mm per degree above 25 C, ~0.1 mm at
  200 C) from that tool's Z offset. Factory imports are cold and are not
  corrected. Calibrations made before this update have no `nozzle_temp`, so
  they are treated as cold too: run `CALIBRATE_TOOL_OFFSETS` and
  `SAVE_CONFIG` again to pick up the fix. If you set a `z_adjust` to make up
  for this, remove it. The coefficient is tunable in `[ff_toolchange]` and
  has been checked against only one printer's report so far.
- A toolchange no longer returns to the print with the new head still
  heating. AFC moved the head back over the part as soon as it had grabbed the
  new head, so the nozzle reached temperature above the model and left blobs
  on it. The head now waits at the dock exit for the temperature the file
  already set, then returns. See the upstream report,
  [AFC-Klipper-Add-On #872](https://github.com/AFCProject/AFC-Klipper-Add-On/issues/872).
- The factory XYZ calibration import marker now lives in persistent
  `/usr/data/anvil-data` state, so reinstalling firmware does not reconsider
  or repeat the stock-JSON import. Upgrading migrates the marker from its old
  location before the payload tree is wiped.
- An AFC toolchange (`T0`..`T3`) no longer shuts the printer down with
  "Timer too close" right after the grab. AFC wrote its statistics to
  Moonraker with blocking requests while the moves back to the print were
  queued; the updated AFC runs those writes on its background thread.
- HelixScreen's tool remap sent `ASSIGN_TOOL`, which this firmware refuses.
  With AFC present HelixScreen runs its AFC backend, which remaps through
  `SET_MAP` instead. Not yet confirmed on a printer.
- Excluding an object no longer makes the next tool change fail. Orca puts
  the prime tower, and the `T<n>` before it, inside the label of the object
  it was printing; with that object excluded, the toolchanger's own moves to
  the dock were thrown away as "inside the object", the carriage never
  reached the dock, and the print stopped with `E0138` ("tool never read as
  seated in its dock"). Moves made during a tool change, and while a print
  is paused (the park lift, filament loading, the travel back), are now
  never excluded. The excluded object's own moves, and the prime tower
  inside its label, are still skipped.
- `END_PRINT` no longer stops halfway after an object was excluded. The
  exclusion sits on top of the flow mesh, and `FLOW_MESH_STOP` refused to
  take the flow mesh off from underneath it. Because a macro stops at its
  first error, the heaters stayed on, the tool stayed on the carriage and
  the motors stayed powered. The flow mesh now comes off from wherever it
  sits. `END_PRINT` also turns the heaters and fans off before any step
  that can fail.
- A print could shut down mid-file with "MCU shutdown: Timer too close",
  most likely just after a tool change followed by many tiny moves (support
  interface dots, arc-fitted detail), or after cancelling an object. Klipper
  read the file for seconds without letting its other work run, and motor
  steps reached the boards late. The file reader now hands Klipper a turn at
  least every 20 ms. Not yet tested on hardware. Details in
  [note 54](docs/notes/54-reader-reactor-starvation.md).
- The thermal sweep no longer probes the same pair over and over without
  ever verifying. Each mesh is homed on the plate first, and that homing
  touch shifts the whole mesh by its own small error; the check that two
  meshes match treated the shift as real movement unless a
  `zero_reference_position` was set, so on a settled plate it kept
  failing. It now always allows for that one touch.
- The thermal sweep measured the probe's noise about seven times too low,
  so even a settled bed could not verify. Each of its readings was secretly
  the median of ten touches (the probe took its `SAMPLES=10` as touches per
  reading), and all of them were at the middle of the bed, where the probe
  scatters least. It now reads nine nodes spread over the mesh, the way the
  mesh itself reads them, and pools their spread. `precision_samples` is
  now readings per node, 4 by default.
- A long thermal sweep could shut the printer down ("MCU shutdown: Timer
  too close"). The map file, every mesh in it, is rewritten after each
  pair, and late in a sweep that took the printer's CPU long enough to stop
  Klipper keeping up with the board. It is now written in the background.
- The thermal sweep no longer skips a pair whose bed target equals the
  chamber's because the chamber read a tenth of a degree over it. A bed
  target is skipped only when the chamber is more than `bed_tolerance`
  (1 °C) above it, and a bed the warm chamber holds a little over its
  target is accepted rather than waited on. The bed and the chamber now
  share one tolerance, `temp_tolerance` (1 °C, replacing `bed_tolerance` and
  `chamber_tolerance`), so the cool-down to a lower chamber set ends where
  its bed = chamber pair can start, instead of 1 °C short of it.
- The thermal sweep runs the chamber fan at full speed while it waits for
  the chamber to cool to a lower set, and puts it back to its previous
  speed afterwards (`cool_fan` / `cool_fan_speed` in `[ff_thermal_mesh]`).
- `LOAD_FILAMENT`, `UNLOAD_FILAMENT` and `PURGE` with a tool mounted and the
  motors off no longer run on an empty carriage. Homing docks the mounted
  tool before it homes Z, but the macro had already decided the tool was on
  the carriage and skipped picking it up again, so it travelled to the purge
  chute and fed filament with no tool on. It now picks the tool back up after
  homing.
- `RESUME` after a `PURGE`, `LOAD_FILAMENT` or `UNLOAD_FILAMENT` during a
  pause no longer drives the mounted tool into a docked one. Those macros
  leave the head at the purge chute or the wipe spot, beside the docks, and
  the resume used to go back to the print in one diagonal move. It now
  backs out of the dock bay along X first, rises to the pause's park height,
  crosses to the print, and only then descends.

## v20260827f-melitopol — 2026-09-24

### Added

- Fluidd is available beside Mainsail at `http://<printer-ip>:81/`, using the
  same Moonraker API and webcam proxy.
- Custom `*.sh` files in `/usr/data/anvil-data/scripts` run in filename order
  during boot, survive updates, and write `/usr/data/logs/custom-scripts.log`.

### Changed

- The normal boot screen says that Reforge is starting, names Moonraker and
  Klipper while it waits, and advances its progress bar through a complete
  normal boot instead of describing every boot as printer setup.
- Mainsail is updated to 2.19.0. Both web interfaces now return their SPA
  shell for client-side routes while keeping API routes behind Moonraker.
- Filament load and unload use the selected tool's saved material and shorter,
  overridable feed lengths; loading finishes with a retract.
- The X and Y rotation distances are both 40.4.

### Fixed

- Upgrading removes FlashForge's obsolete `[output_pin DC24V_CTL]` section
  when present, before the mod configures PA3 as a heater-following 24 V
  control.
- Object cancellation keeps extrusion and retraction history separate for
  every tool instead of carrying one tool's adjustment into another.

## v20260827e-melitopol — 2026-09-11

### Added

- The installer refuses to install on a printer running FlashForge firmware
  older than 1.9.6, and says to install FlashForge's current firmware first.
  On those releases the board firmware and Klipper do not agree, the MCU never
  connects, and the printer comes up with no Klipper and no screen — which
  looks like a bad flash rather than a mismatch. Nothing is written to the
  printer when the gate refuses: it puts the reason on the panel, leaves
  `anvil-NOT-INSTALLED.txt` on the USB stick, and the printer boots as it did
  before. A version it cannot read is installed on, as before.

### Changed

- Klipper's and Moonraker's config now live in Reforge's own directory,
  `/usr/data/anvil-data/config`, seeded once on the first install with a copy
  of `printer.cfg` and the config files beside it. FlashForge's
  `/usr/data/config` is not written to at all from here on.
- Moonraker's data path moved to `/usr/data/anvil-data`, which is what puts its
  config in that directory — `moonraker.conf` and `moonraker-custom.conf` moved
  with it, and Mainsail's config editor now shows the live files. Prints and
  logs stay in `/usr/data/gcodes` and `/usr/data/logs`; the job-history
  database is moved across once, by the service, while Moonraker is stopped.

  On a printer coming from an older Reforge, whatever that release put in
  `/usr/data/config` stays there — the stock files it replaced were not kept,
  so there is nothing to restore them from. Stock's own installer force-copies
  its `printer.base.cfg` back on every flash, which is what repairs that
  directory.

### Fixed

- Flashing the stock FlashForge package back over Reforge no longer leaves the
  printer with a Klipper config it cannot resolve. `/usr/data` is the data
  partition, so a stock flash does not clean it: the symlinked
  `printer.base.cfg` and the `ff-*.cfg` beside it used to outlive the mod, and
  Klipper treats an `[include]` that points at a removed `/usr/data/anvil` as
  fatal. Calibration and config edits made under Reforge stay in Reforge's
  directory and come back with it; a stock flash returns the machine to the
  `printer.cfg` it had before.
- FlashForge's `chelper` is put back on a printer that took one of the two
  releases whose software component carried our Klipper (`v20260827` and
  `v20260827b`). Those replaced `/usr/prog/klipper/klippy`, and a stock flash
  restores `c_helper.so` but not the `chelper/__init__.py` that declares what
  is in it — ours takes a fourth argument for `extruder_set_pressure_advance`,
  so a printer flashed back to stock failed at the first extruder. The whole
  directory is restored, with `c_helper.so` left newest so their own
  `check_build_code` does not send klippy to a compiler no printer has. Only
  on machines running FlashForge 1.9.7 or older, which is the firmware this
  copy came from; a newer one is left alone.
- A printer that took an earlier release has FlashForge's `klipperDaemon` put
  back for it. That file is the one thing a stock flash cannot restore — their
  package carries no copy — so a machine whose copy was replaced could not be
  returned to stock at all. `anvil-core` now ships FlashForge's own and
  restores it on every install and every `apk upgrade`, so one update is
  enough; owners who have already rolled back are served by the package at
  [Klipper4FlashForge/stock-recovery](https://github.com/Klipper4FlashForge/stock-recovery).
- Going back to stock works again. The mod no longer replaces
  `/usr/prog/klipper/klipperDaemon` with a shim of its own. That link was the
  one thing a stock FlashForge flash could not undo — their package carries no
  `klipperDaemon` to copy over it — so stock's `start.sh`, whose last line is
  `klipperDaemon start`, ran the mod's shim, whose `start` does nothing by
  design. The printer came back from a stock flash with a working screen and
  no Klipper behind it, however many times stock was flashed. Nothing on a
  modded machine called that file: our own `start.sh` replaces stock's and
  asks s6-rc, FlashForge's `firmwareExe` execs only `start.sh`, and Moonraker
  runs with `provider: none`. Printers that already took an earlier release
  still carry the link and are repaired by the package at
  [Klipper4FlashForge/stock-recovery](https://github.com/Klipper4FlashForge/stock-recovery).

## v20260827c-melitopol — 2026-08-27

### Added

- HelixScreen’s Filament page now follows the selected tool for material,
  spool, temperatures, loading, and retracting.
- Installed packages record the Reforge APK feed location for future updates.

### Fixed

- A selected tool with no resolvable heater is logged and ignored instead of
  heating the wrong hotend.

## v20260827d-melitopol — 2026-08-27

### Added

- HelixScreen pressure-advance calibration integration and per-tool Z
  correction selection in the tune overlay.

### Fixed

- Pressure-advance calibration restores the previous heater target, including
  when a sweep fails partway through.
- Calibration documentation now explains that results belong in the slicer’s
  filament profile rather than being saved to `printer.cfg`.

## v20260827-melitopol — 2026-08-27

### Added

- s6-rc supervision for the web stack, camera, Moonraker, Wi-Fi, and related
  services, with readiness checks based on actual service readiness.
- A printer-local CPython 3.13 runtime for Moonraker with working SQLite.
- CPU priority controls that let Moonraker and the camera yield to Klipper.
- HelixScreen settings persistence across firmware updates.

### Changed

- Reforge owns and starts the camera and web stack rather than stock
  `firmwareExe`.
- Startup retries until MCU boards answer and shows boot progress.

## v20260825b-nova-kakhovka — 2026-08-25

### Fixed

- `TOOL_OFFSET_CALIBRATE` no longer shuts Klipper down on completion.

## v20260825-nova-kakhovka — 2026-08-25

### Added

- First-boot import of factory per-unit calibration into Klipper config.
- Toolchanger-aware calibration, status commands, and safe plate checks.
- An update path that preserves the root password and user configuration.

### Fixed

- Release builds can no longer silently fall back to the stock Klipper tree.

## v20260824-nova-kakhovka — 2026-08-24

### Added

- USB packages for Creator 5 and Creator 5 Pro with model guards.
- Current Klipper with Creator 5 toolchanger support, Mainsail, Moonraker,
  HelixScreen, camera streaming, Wi-Fi, and root SSH.
- Stock slicer compatibility through `[ff_print]`, `START_PRINT`, and
  `END_PRINT`, including preparation, mesh loading, nozzle cleaning, tool
  checks, and safe cancellation.
- Tool-aware filament load, unload, purge, runout, and clog handling.
- Automatic pressure-advance calibration and VFA calibration support.
- Timelapse support, signed APK packages, pinned inputs, and Docker plus
  printer-replica QA.

### Known release issue

The original packages were withdrawn because they could install a mixed
Klipper tree. Use the rebuilt release or a later release; see its release note.

## Project start — 2026-08-20

- Initial reverse-engineering notes and USB-installable firmware builder.
