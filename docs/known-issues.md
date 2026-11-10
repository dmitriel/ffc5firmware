# Known issues

## OrcaSlicer sends a 3MF file instead of G-code

Some OrcaSlicer profiles can upload a `.3mf` project rather than the sliced
`.gcode` file. Reforge expects the G-code file for printing, so the job may
not appear or start correctly in Mainsail.

In OrcaSlicer, open **Printer Settings**, change the upload protocol from
**FlashForge** to **Moonraker**, and uncheck:

> Use 3MF instead of G-code

Slice the model again and upload the resulting `.gcode` file through the
Moonraker upload target.

## HelixScreen shows the wrong tool as active after a tool remap

Reforge does not support remapping tools on the printer: a print's `T0` always
picks up the tool in dock 1, and so on. HelixScreen still offers to remap tools,
from a slot's edit screen or the pre-print screen, and it saves the remap on
its own side even when the printer refuses it. After that it works out which
slot is mounted from the remap the printer never accepted. So it can keep
highlighting one tool, often the one mounted when the print paused, whichever
tool is actually on the carriage.

The printer itself is not affected: it keeps reading which tool is mounted
from the dock sensors, and the print uses the right tool. To bring the screen
back in step, restart HelixScreen or reset its tool mapping. To print with
different tools, remap them in the slicer.
