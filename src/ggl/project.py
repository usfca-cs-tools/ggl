"""Assemble a Golden Gates circuit into one self-contained model for headless use (grading).

Two on-disk shapes exist and both are accepted:

* Self-contained (a standalone export): the circuit already carries its subcircuits under
  ``schematicComponents``, referenced by ``circuitId``. Nothing to resolve — it's used as-is.
* Multi-file project (a folder): each subcircuit lives in its own ``.ggc`` and a placement
  references it by ``props.filename``. The app treats such a project as a DIRECTORY — opening it
  loads every file and links the references. ``load_project`` mirrors that: it inlines each
  referenced subcircuit, transitively, from the same directory under ``schematicComponents`` and
  stamps each placement's ``props.circuitId`` so ``ggl.view`` can look it up.

This is pure file-linking: NO port geometry lives here. It relies on the save serializing each
subcircuit-instance's ports correctly, so the on-disk ``.ggc`` files are self-describing.
"""

import glob
import json
import os
import re


class ProjectError(Exception):
    """A structural problem assembling the project — e.g. a referenced subcircuit file is not
    present in the directory. Carries a student-readable message."""


def _circuit_id(filename):
    """A stable circuitId derived from a subcircuit's filename. It only needs to be consistent
    between a placement's ``props.circuitId`` and its ``schematicComponents`` key within one
    assembled model, so a sanitized basename is enough."""
    base = os.path.basename(filename)
    if base.endswith(".ggc"):
        base = base[:-4]
    return "sub_" + re.sub(r"[^A-Za-z0-9_]", "_", base)


def load_project(circuit_path):
    """Load ``circuit_path`` and inline the subcircuits it references (transitively) from the
    same directory. Return a self-contained ``.ggc``-shaped dict with ``schematicComponents``
    populated, ready for ``ggl.view.generate``.

    Raises ``FileNotFoundError``/``json.JSONDecodeError`` for the top circuit file, and
    ``ProjectError`` when a referenced subcircuit filename isn't found in the directory.
    """
    circuit_path = os.path.abspath(circuit_path)
    directory = os.path.dirname(circuit_path)

    with open(circuit_path) as f:
        top = json.load(f)

    # Index every sibling .ggc by basename so any filename reference resolves. Unrelated or
    # unparseable files are skipped — only referenced subcircuits are pulled in below.
    by_name = {}
    for path in glob.glob(os.path.join(directory, "*.ggc")):
        try:
            with open(path) as f:
                by_name[os.path.basename(path)] = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue

    # Start from whatever the circuit already embeds — a self-contained export carries its
    # subcircuits here (keyed by circuitId) — then add any referenced-by-filename subcircuits
    # from sibling files (a multi-file project). Both shapes end up in the same map.
    schematic = dict(top.get("schematicComponents") or {})

    def link(ggc):
        for comp in ggc.get("components", []) or []:
            if comp.get("type") != "schematic-component":
                continue
            props = comp.get("props") or {}
            cid = props.get("circuitId")
            if cid and cid in schematic:
                continue  # already self-contained (embedded definition present)
            filename = props.get("filename")
            if not filename:
                raise ProjectError(
                    f'schematic-component "{comp.get("id")}" references circuit "{cid}" that is '
                    "not embedded and has no filename to resolve"
                    if cid
                    else f'schematic-component "{comp.get("id")}" has no subcircuit reference')
            fcid = _circuit_id(filename)
            props["circuitId"] = fcid
            comp["props"] = props
            if fcid in schematic:
                continue  # already inlined (shared subcircuit)
            sub = by_name.get(filename)
            if sub is None:
                raise ProjectError(
                    f'referenced subcircuit "{filename}" not found in {directory}')
            inner = {
                "components": sub.get("components", []) or [],
                "wires": sub.get("wires", []) or [],
                "wireJunctions": sub.get("wireJunctions", []) or [],
            }
            schematic[fcid] = {"circuit": inner}
            link(inner)  # a subcircuit may reference deeper subcircuits by filename

    link(top)
    top["schematicComponents"] = schematic
    return top
