# Design canvas — source artboards

Working files for the Turonomics fleet-ops design exploration.

**Live canvas:** https://claude.ai/artifact/TTjS8Ye6gvKXjW74bJ8G3K

Each `.dc.html` is one artboard (a Design Component — self-contained HTML with a
small logic class). `canvas.json` is the layout manifest: positions, pages, and
which view the canvas opens on.

| File | Artboard |
|---|---|
| `Main.dc.html` | Today — run sheet (the hero screen) |
| `AspAlert.dc.html` | Alternate-side move alert + confirm-the-spot flow |
| `FleetMap.dc.html` | Fleet map |
| `Vehicle.dc.html` | Vehicle detail (status / trips / costs) |
| `Turnaround.dc.html` | Between-trips prep checklist |
| `Checkout.dc.html` | Check-out inspection record |
| `Messages.dc.html` | Guest messaging queue + autonomy dial |
| `Modules.dc.html` | Module architecture board |

The screens are clickable — filters, checklists, photo tiles, the side-of-street
toggle and the messaging autonomy dial all work.

The published bundle (`turo-fleet-ops.html`) is generated and git-ignored; edit
the artboards here and re-seed to update it.

Design rationale, integration constraints and the MVP cut live in
[`../00-overview.md`](../00-overview.md).
