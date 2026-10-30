"""
N64 exporter utilities: Blender to N64 coordinates, Blender data reads, trait
data initializers, C literals, the build folder's files (written only when they
change), and armor_64's settings and paths. Used by every export module.
"""

import filecmp
import os
import shutil
from typing import List, Optional

import bpy
import bmesh

import arm.utils
import arm.log as log


# =============================================================================
# Coordinate Conversion: Blender (X,Y,Z) → N64 (X,Z,-Y)
# =============================================================================

SCALE_FACTOR: float = 0.015625  # 1/64 - Blender scale 1.0 → N64 scale 0.015625

# Python value conversion
def convert_vec3_list(vec: List[float]) -> List[float]:
    return [vec[0], vec[2], -vec[1]]

def convert_quat_list(quat: List[float]) -> List[float]:
    return [quat[0], quat[2], -quat[1], quat[3]]

def convert_scale_list(vec: List[float], factor: float = SCALE_FACTOR) -> List[float]:
    return [vec[0] * factor, vec[2] * factor, vec[1] * factor]

def c_float(value) -> str:
    f = float(value)
    s = str(f)
    return f"{s}f" if '.' in s else f"{int(f)}.0f"


# =============================================================================
# Build Helpers
# =============================================================================


def get_physics_debug_mode() -> int:
    wrd = bpy.data.worlds.get('Arm')
    if wrd is None:
        return 0
    flags = [
        ('arm_physics_dbg_draw_wireframe', 1),
        ('arm_physics_dbg_draw_aabb', 2),
        ('arm_physics_dbg_draw_contact_points', 8),
        ('arm_physics_dbg_draw_constraints', 2048),
        ('arm_physics_dbg_draw_constraint_limits', 4096),
        ('arm_physics_dbg_draw_normals', 16384),
        ('arm_physics_dbg_draw_axis_gizmo', 32768),
        ('arm_physics_dbg_draw_raycast', 65536),
    ]
    return sum(bit for prop, bit in flags if getattr(wrd, prop, False))

def copy_runtime(physics: bool, ui: bool, audio: bool):
    """The runtime's fixed files into build/n64/src, only those that changed: the
    core always, a feature's folders when the game uses it. Templates (.j2) are
    rendered, and the generated headers written, by the exporters."""
    features = {'oimo': physics, 'events': physics, 'ui': ui, 'audio': audio}
    src = os.path.join(get_n64_runtime_path(), 'src')
    out = build_path('src')
    for folder, _, files in os.walk(src):
        rel = os.path.relpath(folder, src)
        if not features.get(rel.split(os.sep)[0], True):
            continue
        for file in files:
            if not file.endswith('.j2'):
                copy_if_changed(os.path.join(folder, file), os.path.join(out, rel, file))


def render_template(template: str, out: str, **data) -> bool:
    """A runtime template (path in runtime/) filled with str.format, written to
    build/n64/<out> if it changed."""
    with open(os.path.join(get_n64_runtime_path(), template), encoding='utf-8') as f:
        text = f.read().format(**data)
    return write_if_changed(build_path(out), text)


# =============================================================================
# Blender Data Extraction
# =============================================================================

def get_clear_color(scene):
    if scene.world is None:
        return [0.051, 0.051, 0.051, 1.0]
    if scene.world.node_tree is None:
        c = scene.world.color
        return [c[0], c[1], c[2], 1.0]
    if 'Background' in scene.world.node_tree.nodes:
        background_node = scene.world.node_tree.nodes['Background']
        col = background_node.inputs[0].default_value
        strength = background_node.inputs[1].default_value
        ar = [col[0] * strength, col[1] * strength, col[2] * strength, col[3]]
        return [max(min(v, 1.0), 0.0) for v in ar]
    return [0.051, 0.051, 0.051, 1.0]

def deselect_from_all_viewlayers():
    bpy.context.view_layer.objects.active = None
    bpy.ops.object.select_all(action='DESELECT')
    for scene in bpy.data.scenes:
        for view_layer in scene.view_layers:
            view_layer.objects.active = None
            for obj in scene.objects:
                obj.select_set(False, view_layer=view_layer)
            view_layer.update()
    bpy.context.view_layer.update()

def to_uint8(value):
    return int(max(0, min(1, value)) * 255)

# =============================================================================
# Trait IR Helpers
# =============================================================================

def get_trait(trait_info: dict, trait_class: str) -> dict:
    """Get trait info by class name (see trait_key)."""
    return trait_info.get("traits", {}).get(trait_key(trait_info, trait_class), {})

def trait_key(trait_info: dict, trait_class: str) -> str:
    """Key of a trait in the IR, its full class path (e.g. "arm.player.Player"),
    for a class name from a scene's trait list, which Armory stores relative to
    the project package ("player.Player")."""
    traits = trait_info.get("traits", {})
    if trait_class in traits:
        return trait_class
    try:
        package = arm.utils.safestr(bpy.data.worlds['Arm'].arm_project_package)
    except (KeyError, AttributeError):
        package = ''
    if package and f'{package}.{trait_class}' in traits:
        return f'{package}.{trait_class}'
    # Unique match on the simple class name (e.g. IR from an older build)
    simple_name = trait_class.rsplit('.', 1)[-1]
    matches = [k for k in traits if k == simple_name or k.endswith('.' + simple_name)]
    return matches[0] if len(matches) == 1 else trait_class


def trait_needs_data(trait_info: dict, trait_class: str) -> bool:
    """Check if a trait needs a data struct, with the same rule the struct
    layout uses (members, signals, signal handlers, a parent, storing `this`,
    or vtable pointers)."""
    from armor64.codegen.trait_generator import needs_data_struct

    return needs_data_struct(trait_info.get("traits", {}), trait_key(trait_info, trait_class))


def trait_chain_events(trait_info: dict, trait_class: str) -> set:
    """Events of a trait and its ancestors (see chain_events)."""
    from armor64.codegen.trait_generator import chain_events

    return chain_events(trait_info.get("traits", {}), trait_key(trait_info, trait_class))


def _trait_data_fields(trait_info: dict, trait_class: str, instance_props: dict,
                       type_overrides: dict, entity_ptr, instance_class: str) -> list:
    """The C initializer fields of a trait's data: its parent's data first
    (._parent = (ParentData){...}, embedded at offset 0, down to the base trait,
    whose .object is the entity), the virtual method pointers its struct holds
    (the instance's class's implementations), then its members (an instance's
    Blender property, else the Haxe initial value; tweens allocated, as a
    `new Tween()` member) and signals (no subscribers). The data is initialized
    when the scene starts (init_trait_data), before any handler runs."""
    from armor64.codegen.trait_generator import vtable_initializers

    trait = get_trait(trait_info, trait_class)
    fields = []
    parent_class = trait.get("parent")
    parent = get_trait(trait_info, parent_class) if parent_class else None
    if parent:
        parent_fields = _trait_data_fields(trait_info, parent_class, instance_props, type_overrides,
                                           entity_ptr, instance_class)
        fields.append(f"._parent = ({parent['c_name']}Data){{{', '.join(parent_fields) or '0'}}}")
    elif not parent_class and entity_ptr:
        fields.append(f'.object = {entity_ptr}')

    traits = trait_info.get("traits", {})
    for method, function in vtable_initializers(traits, trait_key(trait_info, instance_class),
                                                trait_key(trait_info, trait_class)):
        fields.append(f'._vfn_{method} = {function}')

    for member in trait.get("members", []):
        name = member.get("name", "")
        if not name or member.get("is_static"):
            continue
        ctype = type_overrides.get(name, member["ctype"])
        if ctype == "ArmTween*":
            fields.append(f'.{name} = tween_alloc()')
        elif name in instance_props:
            fields.append(f'.{name} = {to_c_literal(instance_props[name], ctype)}')
        else:
            fields.append(f'.{name} = {extract_default_value(member.get("default_value"), ctype)}')

    for signal in trait.get("meta", {}).get("signals", []):
        if signal.get("name"):
            fields.append(f'.{signal["name"]} = {{0}}')
    return fields


def build_trait_initializer(trait_info: dict, trait_class: str, instance_props: dict = None,
                            type_overrides: dict = None, entity_ptr: str = None) -> str:
    """The C initializer of a trait instance's data struct."""
    return ', '.join(_trait_data_fields(trait_info, trait_class, instance_props or {},
                                        type_overrides or {}, entity_ptr, trait_class))


# =============================================================================
# C Literal Helpers
# =============================================================================

def extract_default_value(node, member_ctype: str) -> str:
    """Convert an IR node to a C literal string."""
    from armor64.codegen.c_text import c_string  # Here: the codegen package imports this module
    if node is None:
        return get_type_default(member_ctype)

    if isinstance(node, dict):
        node_type = node.get("type", "")
        value = node.get("value")

        if node_type == "int":
            return c_float(value) if member_ctype == "float" else str(value)
        elif node_type == "float":
            return c_float(value)
        elif node_type == "string":
            if member_ctype == "SceneId":
                return f"SCENE_{arm.utils.safesrc(str(value)).upper()}"
            return c_string(value)
        elif node_type == "bool":
            return "true" if value else "false"
        elif node_type == "null":
            return "SCENE_UNKNOWN" if member_ctype == "SceneId" else "NULL"
        elif node_type == "new_vec":
            # c_code template with {0}, {1}... for the given arguments
            c_code = node.get("c_code", "")
            for i, a in enumerate(node.get("args", [])):
                c_code = c_code.replace(f"{{{i}}}", extract_default_value(a, "float"))
            return c_code
        elif node_type == "c_literal":
            # Direct C code literal - use c_code field directly
            return node.get("c_code", get_type_default(member_ctype))

    if isinstance(node, (int, float, str, bool)):
        return to_c_literal(node, member_ctype)
    return get_type_default(member_ctype)

TYPE_DEFAULTS = {
    "float": "0.0f", "int32_t": "0", "int": "0", "bool": "false",
    "const char*": "NULL", "SceneId": "SCENE_UNKNOWN",
    "ArmVec2": "(ArmVec2){0, 0}", "ArmVec3": "(ArmVec3){0, 0, 0}",
    "ArmVec4": "(ArmVec4){0, 0, 0, 0}", "void*": "NULL",
    "KouiLabel*": "NULL", "ArmTween*": "NULL",
}

def get_type_default(ctype: str) -> str:
    """Get the default C literal for a given C type."""
    return TYPE_DEFAULTS.get(ctype, "0")

def to_c_literal(value, ctype: str) -> str:
    """Convert a Python value to a C literal string."""
    from armor64.codegen.c_text import c_string  # Here: the codegen package imports this module
    if ctype == "float":
        return c_float(value)
    elif ctype in ("int32_t", "int"):
        return str(int(value))
    elif ctype == "bool":
        return "true" if value else "false"
    elif ctype == "const char*":
        # An empty property is "", as Iron sets it (not null)
        return c_string(value) if value is not None else "NULL"
    elif ctype == "SceneId":
        return f"SCENE_{arm.utils.safesrc(str(value)).upper()}"
    elif "Vec3" in ctype and isinstance(value, (list, tuple)) and len(value) >= 3:
        return f"(ArmVec3){{{c_float(value[0])}, {c_float(value[1])}, {c_float(value[2])}}}"
    elif "Vec2" in ctype and isinstance(value, (list, tuple)) and len(value) >= 2:
        return f"(ArmVec2){{{c_float(value[0])}, {c_float(value[1])}}}"
    return str(value)


# =============================================================================
# Collision Mesh
# =============================================================================

def extract_collision_mesh(obj: bpy.types.Object, scale) -> Optional[dict]:
    """The triangles of a static mesh collider (the engine's BVH is sized for the
    largest, limits.py: a mesh collider costs CPU, simple shapes or a simplified
    collision mesh are cheaper).

    Args:
        obj: Blender mesh object to extract collision data from.
        scale: The object's world scale (vertices are scaled by it; rotation
               and position come from the body's transform).
    """
    if obj.type != 'MESH':
        return None

    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = obj.evaluated_get(depsgraph)
    mesh = obj_eval.to_mesh()
    if mesh is None:
        return None

    try:
        bm = bmesh.new()
        bm.from_mesh(mesh)
        bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method='FIXED')

        vertices = []
        for v in bm.verts:
            scaled = [v.co.x * scale[0], v.co.y * scale[1], v.co.z * scale[2]]
            vertices.append(convert_vec3_list(scaled))

        indices = [v.index for face in bm.faces for v in face.verts]  # Triangulated above

        bm.free()
        return {
            "vertices": vertices,
            "indices": indices,
            "num_vertices": len(vertices),
            "num_triangles": len(indices) // 3
        }
    finally:
        obj_eval.to_mesh_clear()


# =============================================================================
# Files, Paths and N64 Settings
# =============================================================================

# armor_64's folder (this file is exporter/utils.py)
_LIBRARY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write_if_changed(path: str, text: str) -> bool:
    """Write a text file only if its content changed (an unchanged file keeps
    its modification time, so make doesn't rebuild what depends on it).
    Returns whether it was written."""
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return True


def _id_collections():
    """bpy.data's collections of data blocks (objects, meshes, materials...)."""
    for name in dir(bpy.data):
        collection = getattr(bpy.data, name, None)
        if isinstance(collection, bpy.types.bpy_prop_collection):
            yield collection


def data_snapshot() -> set:
    """The data blocks the .blend has now (see remove_new_unused_data)."""
    return {item.as_pointer() for collection in _id_collections() for item in collection
            if isinstance(item, bpy.types.ID)}


def remove_new_unused_data(before: set):
    """Remove the data blocks added since the snapshot `before` that no data from
    before (nor a scene, nor a fake user) reaches, directly or through other data:
    what the export created and left behind, also when those blocks reference each
    other. The .blend's own data stays, used or not (unlike Blender's Purge)."""
    users = bpy.data.user_map()
    uses = {}
    for item, item_users in users.items():
        for user in item_users:
            uses.setdefault(user, set()).add(item)
    reached = {item for item in users if item.as_pointer() in before or item.use_fake_user
               or isinstance(item, bpy.types.Scene)}
    stack = list(reached)
    while stack:
        for used in uses.get(stack.pop(), ()):
            if used not in reached:
                reached.add(used)
                stack.append(used)
    left_behind = [item for item in users if item not in reached]
    if left_behind:
        bpy.data.batch_remove(left_behind)


def build_path(rel_path: str) -> str:
    """A path in the N64 build folder (build_<project>/n64)."""
    return os.path.join(arm.utils.build_dir(), 'n64', rel_path)


def write_generated(rel_path: str, body: str, writer: str, included_by: str) -> bool:
    """A generated header under build/n64/src: the part of a runtime file that
    comes from the project (ids, counts, tables). Its first line names what
    writes it and the runtime file that includes it."""
    text = (f'// Generated by armor_64 ({writer}) for {included_by}. Do not edit.\n'
            '#pragma once\n\n' + body.rstrip('\n') + '\n')
    return write_if_changed(build_path(os.path.join('src', rel_path)), text)


def copy_if_changed(src: str, dst: str) -> bool:
    """Copy a file only if the destination's content differs (see
    write_if_changed). Returns whether it was copied."""
    if os.path.isfile(dst) and filecmp.cmp(src, dst, shallow=False):
        return False
    os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
    shutil.copyfile(src, dst)
    return True


def win_path_to_posix(path: str) -> str:
    """A Windows path for MSYS2 (e.g. D:\\foo -> /d/foo)."""
    path = os.path.abspath(path).replace('\\', '/')
    if len(path) >= 2 and path[1] == ':':
        path = '/' + path[0].lower() + path[2:]
    return path


def _preference(name: str, default):
    prefs = arm.utils.get_arm_preferences()
    return getattr(prefs, name, default)


def get_n64_toolchain_path() -> str:
    if os.getenv('N64_INST') is not None:
        return os.getenv('N64_INST')
    path = _preference('n64_toolchain_path', '')
    return win_path_to_posix(path) if path else ''


def get_msys2_bash_executable() -> str:
    return _preference('msys2_bash_executable', '')


def get_mingw64_path() -> str:
    path = _preference('mingw64_path', '')
    return win_path_to_posix(path) if path else ''


def get_ares_emulator_executable() -> str:
    return _preference('ares_emulator_executable', '')


def get_open_n64_rom_directory() -> bool:
    return _preference('open_n64_rom_directory', False)


def get_n64_compilation_server_port() -> int:
    return _preference('n64_compilation_server_port', 7002)


def get_n64_runtime_path() -> str:
    """armor_64's C runtime and templates."""
    return os.path.join(_LIBRARY_DIR, 'runtime')


def get_n64_libraries_dir() -> str:
    """The project's Libraries/ folder."""
    return os.path.join(arm.utils.get_fp(), 'Libraries')


def get_libdragon_path() -> str:
    """libdragon, expected in the project's Libraries/."""
    return os.path.join(get_n64_libraries_dir(), 'libdragon')


def get_tiny3d_path() -> str:
    """tiny3d, expected in the project's Libraries/."""
    return os.path.join(get_n64_libraries_dir(), 'tiny3d')
