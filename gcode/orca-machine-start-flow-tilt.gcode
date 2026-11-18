; ====================================================================
; OrcaSlicer -> Printer settings -> Machine G-code -> Machine start G-code
; for the Creator 5 / Creator 5 Pro on this firmware.
;
; Paste everything below the dashed line into that field. It turns on
; the flow mesh and tilt correction for this print, then runs
; START_PRINT with every tool the file uses, so the printer homes,
; cleans every used tool, meshes, fits the bed plane and leaves the
; first tool mounted and primed before the first layer starts.
;
; This firmware no longer runs START_PRINT by itself before a file
; (FF_BEFORE_PRINT_START prepare is 0 by default), so a profile without
; this block -- the stock FlashForge one -- would start on an unhomed
; machine and stop at its first move with "Must home axis first".
;
; Notes
;   * Leave "Change filament G-code" empty: Orca's bare Tn reaches the
;     toolchanger directly.
;   * Machine end G-code may stay empty or call END_PRINT; the firmware
;     runs END_PRINT once the job ends either way, and only once.
;   * Enable Label objects / Exclude objects in Orca so the adaptive mesh
;     and the tilt fit cover just your parts.
;   * Both toggles stay set until the next restart, so later prints from
;     other profiles use them too. Add FLOW_MESH_TOGGLE ENABLE=0 and
;     TILT_CORRECTION_TOGGLE ENABLE=0 to Machine end G-code if you do not
;     want that.
;   * The chamber line is a no-op on the Creator 5 (no chamber heater) and
;     with a filament whose chamber temperature is 0.
;   * Flow mesh and tilt correction are not yet tested on hardware. Watch
;     the first layer of your first print with them.
; --------------------------------------------------------------------
; Chamber heats (without waiting) while START_PRINT homes and cleans,
; so the mesh is measured nearer the temperature you print at.
M141 S{chamber_temperature[initial_extruder]}
; Bed-mesh strategy for this print: absorb the plate in the first
; layers' thickness, in a frame tilted onto the bed's fitted plane.
FLOW_MESH_TOGGLE ENABLE=1
TILT_CORRECTION_TOGGLE ENABLE=1
; Tilt correction needs a mesh: keep BED_MESH_MODE on auto (the default)
; or pick one here, e.g. MESH=adaptive. MESH=off disables both features.
START_PRINT BED=[bed_temperature_initial_layer_single] TOOL=[initial_extruder] NOZZLE={nozzle_temperature_initial_layer[initial_extruder]} LAYER=[initial_layer_print_height] TOOLS={if is_extruder_used[0]}0:{nozzle_temperature_initial_layer[0]},{endif}{if is_extruder_used[1]}1:{nozzle_temperature_initial_layer[1]},{endif}{if is_extruder_used[2]}2:{nozzle_temperature_initial_layer[2]},{endif}{if is_extruder_used[3]}3:{nozzle_temperature_initial_layer[3]},{endif}
; START_PRINT leaves the first tool mounted, purged and at temperature;
; wait for the bed and nozzle once more in case the slicer's values
; changed, then print. No prime line needed.
M190 S[bed_temperature_initial_layer_single]
M109 S{nozzle_temperature_initial_layer[initial_extruder]}
