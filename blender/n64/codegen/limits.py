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
- code facts (n64_traits.json, n64_autoloads.json, n64_signals.json,
  n64_limits.json) multiplied by the instances in the scene: tweens of trait
  and autoload members, signal subscribers (one per connect() site and
  instance), map keys (literal ones)
- UI canvas data: exact, for the largest canvas (one is loaded at a time)

A value that can't be counted (a `new Tween()` in a function, a map key built
at runtime) keeps the default, at least.
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


def scene_counts(scene: Dict, trait_key: Callable[[str], str], traits: Dict) -> Dict:
    """What one exported scene needs from the runtime's pools.

    scene: an exporter.scene_data entry; trait_key: a class name as scenes
    store it -> its key in traits (n64_traits.json's traits).
    """
    objects = scene.get("objects", [])
    bodies = mesh_colliders = contact_bodies = contact_subscribers = anim_clips = 0
    button_subscribers: Dict[tuple, int] = {}
    instances: Dict[str, int] = {}

    def meta(trait):
        return traits.get(trait_key(trait.get("class_name", "")), {}).get("meta", {})

    def count_traits(trait_list):
        for trait in trait_list:
            key = trait_key(trait.get("class_name", ""))
            instances[key] = instances.get(key, 0) + 1
            for event in meta(trait).get("button_events", []):
                button = (event.get("c_button"), event.get("event_type"))
                button_subscribers[button] = button_subscribers.get(button, 0) + 1

    for obj in objects:
        rigid_body = obj.get("rigid_body")
        if rigid_body:
            if rigid_body.get("shape") == "mesh":
                mesh_colliders += 1
            else:
                bodies += 1
            # Contact handlers subscribed when the scene loads (scene_generator)
            handlers = sum(1 for trait in obj.get("traits", [])
                           for event in meta(trait).get("contact_events", [])
                           if event.get("subscribe", True) and event.get("handler_name"))
            if handlers:
                contact_bodies += 1
                contact_subscribers = max(contact_subscribers, handlers)
        anim_clips = max(anim_clips, len(obj.get("anim_clips", [])))
        count_traits(obj.get("traits", []))
    for holder in scene.get("cameras", []) + scene.get("lights", []) + [scene]:
        count_traits(holder.get("traits", []))

    return {
        "objects": len(objects),
        "bodies": bodies,
        "mesh_colliders": mesh_colliders,
        "contact_bodies": contact_bodies,
        "contact_subscribers": contact_subscribers,
        "button_subscribers": max(button_subscribers.values(), default=0),
        "anim_clips": anim_clips,
        "instances": instances,
    }


def compute(scenes: Optional[List[Dict]] = None, mesh_triangles: int = 0,
            ui_canvases: Optional[Dict] = None, spawn_templates: Optional[List[Dict]] = None,
            traits: Optional[Dict] = None, autoloads: Optional[Dict] = None,
            signals: Optional[Dict] = None, facts: Optional[Dict] = None) -> Dict[str, int]:
    """Every limit's value.

    scenes: scene_counts() of each exported scene
    mesh_triangles: triangles of the largest mesh collider
    ui_canvases: the exported UI canvas data (exporter.ui_canvas_data)
    spawn_templates: scene_counts() of each object the game can spawn, with
        an optional max_alive (spawning isn't supported yet: none so far)
    traits, autoloads, signals: the IR (n64_traits/autoloads/signals.json)
    facts: n64_limits.json (map keys)
    """
    values = {name: default for name, (default, _) in DEFAULTS.items()}
    traits, autoloads, signals = traits or {}, autoloads or {}, signals or {}
    if scenes:
        effective = [_with_spawns(s, spawn_templates or [], values["SPAWN_MAX_INSTANCES"]) for s in scenes]
        values.update(_scene_limits(effective))
        values.update(_code_limits(effective, traits, autoloads, signals))
    if facts:
        values.update(_map_limits(facts))
    if mesh_triangles:
        triangles = max(mesh_triangles, 4)
        values["OIMO_BVH_MAX_TRIANGLES"] = triangles
        values["OIMO_BVH_MAX_DEPTH"] = max(int(math.ceil(math.log2(max(triangles, 2)))) + 2, 4)
    if ui_canvases:
        values.update(_ui_limits(ui_canvases))
    return values


def _with_spawns(scene: Dict, templates: List[Dict], max_alive: int) -> Dict:
    """A scene's counts plus every copy spawnable templates may add."""
    total = dict(scene, instances=dict(scene.get("instances", {})))
    for template in templates:
        copies = template.get("max_alive", max_alive)
        for key, value in template.items():
            if key == "instances":
                for trait, n in value.items():
                    total["instances"][trait] = total["instances"].get(trait, 0) + n * copies
            elif key in ("contact_subscribers", "anim_clips"):
                total[key] = max(total.get(key, 0), value)  # per object, not a total
            elif isinstance(value, int) and key != "max_alive":
                total[key] = total.get(key, 0) + value * copies
    return total


def _scene_limits(scenes: List[Dict]) -> Dict[str, int]:
    # The largest scene's count; at least 1 (C has no empty arrays)
    def need(key):
        return max(1, max(s.get(key, 0) for s in scenes))

    bodies = need("bodies")
    return {
        "MAX_PHYSICS_BODIES": bodies,
        "MAX_MESH_COLLIDERS": need("mesh_colliders"),
        "OIMO_MAX_PROXIES": max(1, bodies + need("mesh_colliders")),
        # Islands only hold movable bodies (mesh colliders are static)
        "OIMO_MAX_ISLAND_BODIES": bodies,
        "OIMO_MAX_RIGID_BODIES": bodies,
        "MAX_CONTACT_BODIES": need("contact_bodies"),
        "MAX_CONTACT_SUBSCRIBERS": need("contact_subscribers"),
        "MAX_BUTTON_SUBSCRIBERS": need("button_subscribers"),
        "MAX_PENDING_REMOVALS": need("objects"),
        "ANIM_MAX_CLIPS": need("anim_clips"),
    }


def _chain(traits: Dict, key: str) -> List[str]:
    """A trait and its exported ancestors."""
    chain = []
    while key in traits and key not in chain:
        chain.append(key)
        key = traits[key].get("parent")
    return chain


def _code_limits(scenes: List[Dict], traits: Dict, autoloads: Dict, signals: Dict) -> Dict[str, int]:
    values = {}

    # Tweens: members allocate one each (traits in on_ready, autoloads at
    # init, kept across scenes); the scene's pool is cleared on scene change
    def tween_members(ir):
        return sum(1 for m in ir.get("members", []) if m.get("ctype") == "ArmTween*")

    persistent = sum(tween_members(ir) for ir in autoloads.values())
    per_scene = [sum(n * sum(tween_members(traits[k]) for k in _chain(traits, key))
                     for key, n in s.get("instances", {}).items()) for s in scenes]
    tweens = max(1, persistent + max(per_scene, default=0))
    # new Tween() elsewhere can't be counted: the default at least
    if any(ir.get("meta", {}).get("allocates_tweens") for ir in list(traits.values()) + list(autoloads.values())):
        tweens = max(tweens, DEFAULTS["MAX_TWEENS"][0])
    values["MAX_TWEENS"] = tweens

    # Signal subscribers: a connect() site subscribes once per instance of its
    # class (duplicates are ignored): one for an autoload, the instances of
    # the class and its subclasses for a trait
    autoload_paths = {ir.get("module", name) for name, ir in autoloads.items()}
    if signals:
        def subscribers(signal, scene):
            total = 0
            for site in signal.get("connect_sites", []):
                if site in autoload_paths:
                    total += 1
                else:
                    total += sum(n for key, n in scene.get("instances", {}).items() if site in _chain(traits, key))
            return total
        values["ARM_SIGNAL_MAX_SUBS"] = max(1, max(subscribers(sig, scene) for sig in signals.values() for scene in scenes))
    return values


def _map_limits(facts: Dict) -> Dict[str, int]:
    """Map keys from the literal keys the code uses (null: not countable)."""
    values = {}
    key_length = facts.get("map_key_length")
    if key_length is not None:
        values["ARM_MAP_MAX_KEY_LEN"] = key_length + 1  # with the terminator
    keys = facts.get("map_keys") or {}

    def capacity(name, types):
        counts = [keys[t] for t in types if t in keys]
        if any(c is None for c in counts):
            return  # some keys aren't literals: the default
        values[name] = max([1] + counts)
    capacity("ARM_MAP_CAPACITY", ("ArmStringMap", "ArmIntMap", "ArmFloatMap", "ArmBoolMap"))
    capacity("ARM_SOUND_CHANNEL_MAP_CAPACITY", ("ArmSoundChannelMap",))
    return values


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
