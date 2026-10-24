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
compute() replaces defaults with values worked out from the project:
- scene counts (scene_counts()): exact, for the largest scene (the runtime
  resets its pools on scene change), plus the copies spawnable templates may
  add (SPAWN_MAX_INSTANCES each)
- UI canvas data: exact, for the largest canvas (one is loaded at a time)
"""

import math
from typing import Callable, Dict, List, Optional

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


def scene_counts(scene: Dict, trait_meta: Callable[[str], Dict]) -> Dict[str, int]:
    """What one exported scene needs from the runtime's pools.

    scene: an exporter.scene_data entry; trait_meta: a trait class name (as
    scenes store it) -> its IR meta.
    """
    objects = scene.get("objects", [])
    bodies = mesh_colliders = contact_bodies = contact_subscribers = anim_clips = 0
    button_subscribers: Dict[tuple, int] = {}

    def count_buttons(traits):
        for trait in traits:
            for event in trait_meta(trait.get("class_name", "")).get("button_events", []):
                key = (event.get("c_button"), event.get("event_type"))
                button_subscribers[key] = button_subscribers.get(key, 0) + 1

    for obj in objects:
        rigid_body = obj.get("rigid_body")
        if rigid_body:
            if rigid_body.get("shape") == "mesh":
                mesh_colliders += 1
            else:
                bodies += 1
            # Contact handlers subscribed when the scene loads (scene_generator)
            handlers = sum(1 for trait in obj.get("traits", [])
                           for event in trait_meta(trait.get("class_name", "")).get("contact_events", [])
                           if event.get("subscribe", True) and event.get("handler_name"))
            if handlers:
                contact_bodies += 1
                contact_subscribers = max(contact_subscribers, handlers)
        anim_clips = max(anim_clips, len(obj.get("anim_clips", [])))
        count_buttons(obj.get("traits", []))
    count_buttons(scene.get("traits", []))

    return {
        "objects": len(objects),
        "bodies": bodies,
        "mesh_colliders": mesh_colliders,
        "contact_bodies": contact_bodies,
        "contact_subscribers": contact_subscribers,
        "button_subscribers": max(button_subscribers.values(), default=0),
        "anim_clips": anim_clips,
    }


def compute(scenes: Optional[List[Dict]] = None, mesh_triangles: int = 0,
            ui_canvases: Optional[Dict] = None, spawn_templates: Optional[List[Dict]] = None) -> Dict[str, int]:
    """Every limit's value.

    scenes: scene_counts() of each exported scene
    mesh_triangles: triangles of the largest mesh collider
    ui_canvases: the exported UI canvas data (exporter.ui_canvas_data)
    spawn_templates: scene_counts() of each object the game can spawn
        (spawning isn't supported yet: none so far)
    """
    values = {name: default for name, (default, _) in DEFAULTS.items()}
    if scenes:
        values.update(_scene_limits(scenes, spawn_templates or [], values["SPAWN_MAX_INSTANCES"]))
    if mesh_triangles:
        triangles = max(mesh_triangles, 4)
        values["OIMO_BVH_MAX_TRIANGLES"] = triangles
        values["OIMO_BVH_MAX_DEPTH"] = max(int(math.ceil(math.log2(max(triangles, 2)))) + 2, 4)
    if ui_canvases:
        values.update(_ui_limits(ui_canvases))
    return values


def _scene_limits(scenes: List[Dict], spawn_templates: List[Dict], max_alive: int) -> Dict[str, int]:
    # The largest scene's count, plus what spawned copies may add; at least
    # 1 (C has no empty arrays)
    def need(key):
        spawned = sum(t.get(key, 0) * t.get("max_alive", max_alive) for t in spawn_templates)
        return max(1, max(s.get(key, 0) for s in scenes) + spawned)

    def largest(key):
        return max(1, max([s.get(key, 0) for s in scenes] + [t.get(key, 0) for t in spawn_templates]))

    bodies = need("bodies")
    return {
        "MAX_PHYSICS_BODIES": bodies,
        "MAX_MESH_COLLIDERS": need("mesh_colliders"),
        "OIMO_MAX_PROXIES": max(1, bodies + need("mesh_colliders")),
        # Islands only hold movable bodies (mesh colliders are static)
        "OIMO_MAX_ISLAND_BODIES": bodies,
        "OIMO_MAX_RIGID_BODIES": bodies,
        "MAX_CONTACT_BODIES": need("contact_bodies"),
        "MAX_CONTACT_SUBSCRIBERS": largest("contact_subscribers"),
        "MAX_BUTTON_SUBSCRIBERS": need("button_subscribers"),
        "MAX_PENDING_REMOVALS": need("objects"),
        "ANIM_MAX_CLIPS": largest("anim_clips"),
    }


def _ui_limits(canvases: Dict) -> Dict[str, int]:
    # The largest canvas's counts: canvas_clear() empties the pools and the
    # canvas fills them from its definitions
    def largest(key):
        return max(1, max((len(c.get(key, [])) for c in canvases.values()), default=0))

    # Text can change at runtime (ui_label_set_text, button_set_text)
    label_text = 32
    group_children = 1
    for canvas in canvases.values():
        for label in canvas.get("labels", []):
            label_text = max(label_text, len(label.get("text", "")) + 1)
        for group in canvas.get("groups", []):
            # Children of each kind are listed separately
            group_children = max(group_children, *(len(group.get(f"child_{kind}_indices", []))
                                                   for kind in ("image", "label", "button", "panel", "group")))
    return {
        "UI_MAX_LABELS": largest("labels"),
        "UI_LABEL_TEXT_SIZE": 1 << (label_text - 1).bit_length(),
        "UI_MAX_IMAGES": largest("images"),
        "UI_MAX_BUTTONS": largest("buttons"),
        "UI_BUTTON_TEXT_SIZE": DEFAULTS["UI_BUTTON_TEXT_SIZE"][0],
        "UI_MAX_PANELS": largest("panels"),
        "UI_MAX_GROUPS": largest("groups"),
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
