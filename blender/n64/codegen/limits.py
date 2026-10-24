"""
Engine limits: every fixed capacity of the N64 runtime, decided here.

The runtime has no dynamic allocation during gameplay: pools and containers
are static arrays, so each needs a size at build time. Python computes each
limit and writes it into the C as a plain number:
- limits.h (from limits.h.j2): the engine's limits
- the UI headers (ui_exporter.py): the UI pools, next to the UI data

A pool that is full reports it once with debugf (ARM_LIMIT_REACHED in
limits.h) and the request is dropped.

DEFAULTS is the value of each limit when nothing more precise is known.
compute() replaces defaults with values worked out from the project (scene
counts, UI canvas data).
"""

import math
from typing import Dict, Optional

# name -> (default, what it limits). Documented in blender/n64/README.md (Engine limits).
DEFAULTS = {
    # Objects and traits
    "MAX_TWEENS": (16, "Tweens allocated at once (tween pool)"),
    "ARM_SIGNAL_MAX_SUBS": (16, "Subscribers of one signal"),
    "ANIM_MAX_CLIPS": (8, "Animation clips of one model"),
    "MAX_PENDING_REMOVALS": (32, "Objects removed during one frame"),
    "MAX_BUTTON_SUBSCRIBERS": (16, "Handlers of one gamepad button event"),
    "SPAWN_MAX_INSTANCES": (4, "Copies alive at once of one spawnable template (Python only; spawning isn't supported yet)"),

    # Physics
    "MAX_PHYSICS_BODIES": (128, "Rigid bodies (physics body pool)"),
    "MAX_MESH_COLLIDERS": (8, "Mesh colliders"),
    "MAX_CONTACT_BODIES": (16, "Bodies with contact handlers"),
    "MAX_CONTACT_SUBSCRIBERS": (4, "Contact handlers of one body"),
    "OIMO_MAX_PROXIES": (128, "Broadphase proxies (one per shape)"),
    "OIMO_MAX_PROXY_PAIRS": (256, "Broadphase overlapping pairs"),
    "OIMO_MAX_CONTACTS": (64, "Contacts between shapes"),
    "OIMO_MAX_ISLAND_BODIES": (32, "Bodies in one simulation island"),
    "OIMO_MAX_ISLAND_SOLVERS": (64, "Contact solvers in one simulation island"),
    "OIMO_MAX_RIGID_BODIES": (32, "Bodies visited while building one island"),
    "OIMO_MAX_SHAPES": (64, "Shapes (unused by the runtime)"),
    "OIMO_MESH_MAX_CONTACTS": (4, "Contacts kept per mesh collision (the shallowest is replaced)"),
    "OIMO_BVH_MAX_TRIANGLES": (4, "Triangles of the largest mesh collider (its BVH)"),
    "OIMO_BVH_MAX_DEPTH": (4, "Depth of the largest mesh collider's BVH"),

    # Containers (Haxe Array / Map)
    "ARM_INT_ARRAY_CAPACITY": (32, "Items of an Array<Int>"),
    "ARM_FLOAT_ARRAY_CAPACITY": (32, "Items of an Array<Float>"),
    "ARM_BOOL_ARRAY_CAPACITY": (32, "Items of an Array<Bool>"),
    "ARM_STRING_ARRAY_CAPACITY": (16, "Items of an Array<String>"),
    "ARM_MAP_CAPACITY": (16, "Keys of a Map<String, *>"),
    "ARM_MAP_MAX_KEY_LEN": (32, "Characters of a Map key, including the terminator"),
    "ARM_SOUND_HANDLE_ARRAY_CAPACITY": (8, "Items of an Array<BaseChannelHandle>"),
    "ARM_SOUND_CHANNEL_MAP_CAPACITY": (16, "Keys of a Map<String, Array<BaseChannelHandle>>"),

    # Strings
    "STR_FMT_BUFFER_SIZE": (64, "Characters of a string built by concatenation"),
    "STR_FMT_BUFFER_COUNT": (4, "Concatenated strings alive at once (rotating buffers)"),

    # UI (defined in the UI headers)
    "UI_MAX_LABELS": (1, "Labels"),
    "UI_LABEL_TEXT_SIZE": (32, "Characters of a label"),
    "UI_MAX_IMAGES": (1, "Images"),
    "UI_MAX_BUTTONS": (1, "Buttons"),
    "UI_BUTTON_TEXT_SIZE": (32, "Characters of a button"),
    "UI_MAX_PANELS": (1, "Panels"),
    "UI_MAX_GROUPS": (1, "Layout groups"),
    "UI_MAX_GROUP_CHILDREN": (8, "Children of one layout group"),
}

# Limits that only Python uses (not written to C)
PYTHON_ONLY = {"SPAWN_MAX_INSTANCES"}

# UI limits are written into the UI headers, not limits.h
UI_LIMITS = {name for name in DEFAULTS if name.startswith("UI_")}


def compute(physics: Optional[Dict] = None, ui_canvases: Optional[Dict] = None) -> Dict[str, int]:
    """Every limit's value.

    physics: counts from the scene export (largest scene): bodies,
        mesh_colliders, contact_bodies, mesh_triangles
    ui_canvases: the exported UI canvas data (exporter.ui_canvas_data)
    """
    values = {name: default for name, (default, _) in DEFAULTS.items()}
    if physics:
        values.update(_physics_limits(physics))
    if ui_canvases:
        values.update(_ui_limits(ui_canvases))
    return values


def _physics_limits(counts: Dict) -> Dict[str, int]:
    # Counts plus a margin, at least the default
    def limit(name, count, margin, minimum):
        return max(DEFAULTS[name][0], count + margin, minimum)

    triangles = max(counts.get("mesh_triangles", 0), 4)
    return {
        "MAX_PHYSICS_BODIES": limit("MAX_PHYSICS_BODIES", counts.get("bodies", 0), 4, 8),
        "MAX_MESH_COLLIDERS": max(counts.get("mesh_colliders", 0) + 2, 4),
        "MAX_CONTACT_BODIES": limit("MAX_CONTACT_BODIES", counts.get("contact_bodies", 0), 4, 8),
        "OIMO_BVH_MAX_TRIANGLES": triangles,
        "OIMO_BVH_MAX_DEPTH": max(int(math.ceil(math.log2(max(triangles, 2)))) + 2, 4),
    }


def _ui_limits(canvases: Dict) -> Dict[str, int]:
    # Largest canvas's counts, plus a margin
    def largest(key):
        return max((len(c.get(key, [])) for c in canvases.values()), default=0)

    label_text = 32
    group_children = 8
    for canvas in canvases.values():
        for label in canvas.get("labels", []):
            label_text = max(label_text, len(label.get("text", "")) + 1)
        for group in canvas.get("groups", []):
            # Children of each kind are listed separately
            group_children = max(group_children, *(len(group.get(f"child_{kind}_indices", []))
                                                   for kind in ("image", "label", "button", "panel", "group")))
    return {
        "UI_MAX_LABELS": max(1, largest("labels") + 4),
        "UI_LABEL_TEXT_SIZE": 1 << (label_text - 1).bit_length(),
        "UI_MAX_IMAGES": max(1, largest("images") + 4),
        "UI_MAX_BUTTONS": max(1, largest("buttons") + 2),
        "UI_BUTTON_TEXT_SIZE": DEFAULTS["UI_BUTTON_TEXT_SIZE"][0],
        "UI_MAX_PANELS": max(1, largest("panels") + 2),
        "UI_MAX_GROUPS": max(1, largest("groups") + 2),
        "UI_MAX_GROUP_CHILDREN": group_children,
    }


def header_data(values: Dict[str, int]) -> Dict[str, str]:
    """Values for limits.h.j2."""
    lines = []
    for name, (_, what) in DEFAULTS.items():
        if name in PYTHON_ONLY or name in UI_LIMITS:
            continue
        lines.append(f"#define {name} {values[name]}  // {what}")
    return {"limit_defines": "\n".join(lines)}
