"""
Scene Exporter - Handles scene data extraction and C code generation for N64.

This module provides functions for building scene data from Blender scenes
and generating scene C files.
"""

import os
import math

import bpy
from mathutils import Matrix

import arm.utils
import arm.log as log
import arm.linked_utils as linked_utils
import armor64.codegen as codegen
import armor64.utils as n64_utils
from armor64.codegen.c_text import c_string
from armor64.export import linked_export

# tiny3d's fixed-point matrices hold ±511 Blender units (runtime utils.h, FIXED_POINT_MAX / 64)
DRAWABLE_RANGE = 511.0
# Directional lights tiny3d holds (t3d_light_set_count); more overrun the RSP's light memory
MAX_LIGHTS = 7
# A mesh collider's size: vertex indices are uint16_t (physics.h), and the BVH's
# node and triangle indices int16_t with 2 nodes per triangle (bvh.h)
MESH_COLLIDER_MAX_VERTICES = 65535
MESH_COLLIDER_MAX_TRIANGLES = 16383


def _warn_if_out_of_range(obj, pos):
    """The runtime skips drawing what's farther (converting it would crash the N64)."""
    if any(abs(v) > DRAWABLE_RANGE for v in pos):
        log.warn(f'N64: "{obj.name}" is at ({pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}), beyond '
                 f"±{DRAWABLE_RANGE:.0f} Blender units from the origin: it can't be drawn there on N64")


def exported_scenes():
    """The scenes the game has, in SceneId order: the project's (not linked ones,
    nor the linked export's temporary one) with Armory's Export checked."""
    return [scene for scene in bpy.data.scenes
            if not scene.library and not linked_export.is_temp_scene(scene)
            and getattr(scene, 'arm_export', True)]


def find_armature(obj):
    """The armature that skins a mesh (its Armature modifier's, else an
    armature parent), or None: the mesh is exported skinned with it."""
    if obj.type != 'MESH':
        return None
    for mod in obj.modifiers:
        if mod.type == 'ARMATURE' and mod.object:
            return mod.object
    if obj.parent and obj.parent.type == 'ARMATURE':
        return obj.parent
    return None


def is_exported(obj):
    """Armory's object Export checkbox: an object out of the export takes its
    children with it (Armory exports children from their parent)."""
    while obj is not None:
        if not getattr(obj, 'arm_export', True):
            return False
        obj = obj.parent
    return True


def is_visible(obj, drawn=True):
    """Armory's visibility: Disable in Renders or Armory's Visible off hides the
    object; Camera ray visibility off only stops drawing its mesh, but the N64 has
    one flag for both (drawn: the object has a mesh)."""
    visible = not obj.hide_render and getattr(obj, 'arm_visible', True)
    return visible and (obj.visible_camera if drawn else True)


def _inlines(obj):
    """An instance Armory replaces by the collection's objects: a linked
    collection instanced by an empty without traits."""
    return (obj.instance_type == 'COLLECTION' and obj.instance_collection is not None
            and obj.instance_collection.library is not None
            and not len(getattr(obj, 'arm_traitlist', [])))


def _group_size(collection):
    """Objects of a group as Armory references them (export_collection): the
    collection's own exported objects whose parent isn't in it, an inlined
    instance counting the instanced collection's own such objects."""
    def is_root(obj, coll):
        return obj.parent is None or obj.parent.name not in coll.objects

    size = 0
    for obj in collection.objects:
        if not getattr(obj, 'arm_export', True):
            continue
        if _inlines(obj):
            # Its collection's exported roots (a nested inlined instance isn't one)
            inner = obj.instance_collection
            size += sum(1 for o in inner.objects
                        if getattr(o, 'arm_export', True) and is_root(o, inner) and not _inlines(o))
        elif is_root(obj, collection):
            size += 1
    return size


def _scene_groups(exporter, scene):
    """Armory's groups of a scene: the collections the scene uses or instances
    (not inlined ones, nor the rigid body world's and traits' ones), by name
    (before a Blender rename of an appended one), with their size."""
    instanced = [o for o, _, _ in collect_scene_objects(scene)] + list(scene.collection.all_objects)
    referenced = {o.instance_collection for o in instanced
                  if o.instance_type == 'COLLECTION' and o.instance_collection is not None and not _inlines(o)}
    inlined = {o.instance_collection for o in instanced if _inlines(o)}
    groups = {}
    for collection in bpy.data.collections:
        if collection.name.startswith(('RigidBodyWorld', 'Trait|')) or collection in inlined:
            continue
        if scene.user_of_id(collection) or collection in referenced:
            name = exporter.collection_original_names.get(collection.name, collection.name)
            groups[name] = {'original_name': name, 'count': _group_size(collection)}
    return groups


def collect_scene_objects(scene):
    """Every object of a scene, including those of instanced collections (also
    collections instanced inside them), each instance at its own transform.

    Returns:
        List of (blender_obj, instance_matrix, instancer_parent) tuples.
        - instance_matrix: the member's world matrix as Blender places the
          instance (the instancer's transform, minus the collection's instance
          offset), None for direct scene objects.
        - instancer_parent: The scene instancer Empty's parent object (e.g. Cube)
          for instance collection members, None for direct scene objects.
          Used to propagate parent/child hierarchy to the N64 scene.
    """
    objects = []

    def add_instance(instancer, instancer_matrix, instancer_parent):
        collection = instancer.instance_collection
        base = instancer_matrix @ Matrix.Translation(-collection.instance_offset)
        for cobj in collection.all_objects:
            if not is_exported(cobj):
                continue
            matrix = base @ cobj.matrix_world
            if cobj.instance_type == 'COLLECTION' and cobj.instance_collection is not None:
                add_instance(cobj, matrix, instancer_parent)
            else:
                objects.append((cobj, matrix, instancer_parent))

    for obj in scene.collection.all_objects:
        if not is_exported(obj):
            continue
        if obj.instance_type == 'COLLECTION' and obj.instance_collection is not None:
            add_instance(obj, obj.matrix_world, obj.parent)  # Parent: e.g. Cube
        else:
            objects.append((obj, None, None))

    return objects


def _topological_sort_objects(objects):
    """Sort objects so every parent precedes all its children (DFS order).

    This ensures that when iterating in index order at runtime, parent
    world transforms are always computed before their children's.

    Args:
        objects: list of (blender_obj, instance_matrix, instancer_parent) tuples

    Returns:
        Sorted list in the same format, with parent/child ordering preserved.
    """
    # Stable base order: sort by name so indices are deterministic across builds.
    # Blender's scene.collection.all_objects has no guaranteed order.
    objects = sorted(objects, key=lambda t: t[0].name)

    # Build lookup: blender object name -> index in input list
    name_to_idx = {}
    for i, (obj, _, _) in enumerate(objects):
        # Use object name as key (unique within a scene)
        name_to_idx[obj.name] = i

    visited = set()
    sorted_result = []

    def visit(idx):
        if idx in visited:
            return
        visited.add(idx)
        obj, inst_mat, iparent = objects[idx]
        if inst_mat:
            # Instance collection object: visit instancer's parent first
            # (skip intra-collection obj.parent — names collide across instances)
            if iparent and iparent.name in name_to_idx:
                visit(name_to_idx[iparent.name])
        else:
            # Direct scene object: visit obj.parent first
            if obj.parent and obj.parent.name in name_to_idx:
                visit(name_to_idx[obj.parent.name])
        sorted_result.append((obj, inst_mat, iparent))

    for i in range(len(objects)):
        visit(i)

    return sorted_result


def build_scene_data(exporter, scene):
    """Build scene data dictionary from a Blender scene.

    Args:
        exporter: N64Exporter instance to update with scene data
        scene: Blender scene object to extract data from
    """
    scene_name = arm.utils.safesrc(scene.name).lower()
    scene_traits = _extract_traits(scene)

    # Extract canvas name directly from Blender trait list (UI Canvas is not a runtime trait)
    canvas_name = None
    if hasattr(scene, 'arm_traitlist'):
        for trait in scene.arm_traitlist:
            if trait.enabled_prop and trait.type_prop == 'UI Canvas':
                canvas_name = getattr(trait, 'canvas_name_prop', '')
                if canvas_name:
                    break

    # Gravity and the physics time scale, as Armory's exporter: none when the scene
    # doesn't use gravity, else scaled by the rigid body world's effector weights
    gravity = [0.0, 0.0, 0.0]
    physics_time_scale = 1.0
    rbw = scene.rigidbody_world
    if scene.use_gravity:
        gravity = list(scene.gravity)
        if rbw is not None:
            weight = rbw.effector_weights.all * rbw.effector_weights.gravity
            gravity = [g * weight for g in gravity]
    if rbw is not None:
        physics_time_scale = rbw.time_scale

    # Safe access to Fast64 ambient color with fallback
    try:
        ambient_color = list(scene.fast64.renderSettings.ambientColor)
    except (AttributeError, KeyError):
        ambient_color = [0.2, 0.2, 0.2]  # Default ambient

    # Collections as Armory's groups (Scene.getGroup)
    groups = _scene_groups(exporter, scene)

    exporter.scene_data[scene_name] = {
        "canvas": canvas_name,  # UI canvas for this scene (or None)
        "world": {
            "clear_color": n64_utils.get_clear_color(scene),
            "ambient_color": ambient_color,
            "gravity": gravity,
            "physics_time_scale": physics_time_scale
        },
        "cameras": [],
        "lights": [],
        "objects": [],
        "traits": scene_traits,
        "groups": groups
    }

    for obj, instance_matrix, _ in collect_scene_objects(scene):
        if not getattr(obj, 'arm_spawn', True):
            log.warn(f'N64: "{obj.name}" has Spawn off, which the N64 does not support yet: '
                     'it is in the scene from the start')
        if obj.type == 'LIGHT':
            _process_light(exporter, scene_name, obj, instance_matrix)

    # Collect mesh + empty objects together for topological sort (parent/child ordering).
    # Empties go into the same objects[] array as meshes — they're ArmObjects with
    # dpl=NULL and model_mat=NULL.  This enables full nested parenting:
    #   empty→mesh, mesh→empty, empty→empty, camera→any.
    scene_objects = [
        (obj, inst_mat, iparent) for obj, inst_mat, iparent in collect_scene_objects(scene)
        if obj.type == 'MESH' or obj.type == 'EMPTY'
    ]
    sorted_scene_objects = _topological_sort_objects(scene_objects)

    # Filter exportable:  meshes must be in exported_meshes, empties always pass.
    # Instance collection empties (instance_type == 'COLLECTION') are instancer
    # holders — they should NOT become scene entities (they're consumed above to
    # build instance_matrix).  Only plain empties (instance_type != 'COLLECTION')
    # are actual scene objects.
    exportable_objects = []
    for obj, inst_mat, iparent in sorted_scene_objects:
        if obj.type == 'MESH':
            mesh = obj.data
            if mesh in exporter.exported_meshes:
                exportable_objects.append((obj, inst_mat, iparent))
            else:
                log.warn(f'Object "{obj.name}": mesh not exported, skipping')
        elif obj.type == 'EMPTY':
            # Skip instance collection empties — they are consumed as instancers,
            # not exported as scene entities.  Also skip empties that come through
            # instance collections (inst_mat is not None) to avoid duplicates.
            if obj.instance_type != 'COLLECTION' and inst_mat is None:
                exportable_objects.append((obj, inst_mat, iparent))

    # Build name→index map for parent resolution (indices match objects[] array)
    object_name_to_index = {}
    for i, (obj, _, _) in enumerate(exportable_objects):
        object_name_to_index[obj.name] = i

    for obj, instance_matrix, instancer_parent in exportable_objects:
        if obj.type == 'MESH':
            _process_mesh_object(exporter, scene_name, obj, instance_matrix, object_name_to_index, instancer_parent)
        elif obj.type == 'EMPTY':
            _process_empty_object(exporter, scene_name, obj, object_name_to_index)

    # Now process cameras (after objects are indexed, so camera parent resolution works)
    for obj, instance_matrix, _ in collect_scene_objects(scene):
        if obj.type == 'CAMERA':
            _process_camera(exporter, scene_name, obj, instance_matrix, object_name_to_index)

    # Without a camera the runtime draws no 3D (fine for a UI-only scene)
    data = exporter.scene_data[scene_name]
    if not data["cameras"] and any(not o.get("is_empty") for o in data["objects"]):
        log.warn(f'N64: scene "{scene_name}" has meshes but no camera, so nothing 3D is drawn')


def _process_camera(exporter, scene_name, obj, instance_matrix=None, object_name_to_index=None):
    """Process a camera object and add to scene data.

    Args:
        object_name_to_index: dict mapping object names to indices in the
            unified objects[] array.  Used for camera parent resolution.
    """
    # Compute world matrix considering instance transform
    world_matrix = instance_matrix if instance_matrix else obj.matrix_world
    world_pos = list(world_matrix.to_translation())
    _warn_if_out_of_range(obj, world_pos)

    cam_dir = world_matrix.to_3x3().col[2]
    cam_target = [
        world_pos[0] - cam_dir[0],
        world_pos[1] - cam_dir[1],
        world_pos[2] - cam_dir[2]
    ]
    # Compute vertical FOV — tiny3d's t3d_mat4_perspective expects vertical FOV.
    # Blender's sensor_fit determines which dimension the focal length maps to.
    # For AUTO (default), Blender picks the larger render dimension (horizontal
    # for landscape).  We always need the vertical FOV for tiny3d.
    cam_data_bl = obj.data
    sensor_fit = cam_data_bl.sensor_fit
    # The game's resolution: the start scene's, for every scene (Armory sizes the
    # window from it)
    render = start_scene().render
    res_x, res_y = render.resolution_x, render.resolution_y
    sensor_w = cam_data_bl.sensor_width
    sensor_h = cam_data_bl.sensor_height
    lens = cam_data_bl.lens

    if sensor_fit == 'VERTICAL':
        # Blender fits FOV to vertical sensor dimension
        cam_fov = math.degrees(2 * math.atan((sensor_h * 0.5) / lens))
    elif sensor_fit == 'HORIZONTAL':
        # Blender fits FOV to horizontal sensor dimension — convert to vertical
        # via aspect ratio (render_resolution_y / render_resolution_x)
        hfov = 2 * math.atan((sensor_w * 0.5) / lens)
        cam_fov = math.degrees(2 * math.atan(math.tan(hfov * 0.5) * res_y / res_x))
    else:
        # AUTO: Blender picks the larger render dimension as the fit axis.
        # For landscape (width >= height), it fits horizontally, so convert to vertical.
        # For portrait (height > width), it already is vertical.
        if res_x >= res_y:
            # Landscape: sensor_width maps to horizontal — derive vertical FOV
            hfov = 2 * math.atan((sensor_w * 0.5) / lens)
            cam_fov = math.degrees(2 * math.atan(math.tan(hfov * 0.5) * res_y / res_x))
        else:
            # Portrait: sensor_height maps to vertical — already vertical FOV
            cam_fov = math.degrees(2 * math.atan((sensor_h * 0.5) / lens))

    # Camera parent detection — camera can be child of any object (mesh or empty)
    parent_index = -1
    local_pos = None
    local_target = None
    if object_name_to_index and obj.parent and obj.parent.name in object_name_to_index:
        parent_index = object_name_to_index[obj.parent.name]
        # Compute camera pos/target relative to parent
        parent_world = obj.parent.matrix_world
        parent_inv = parent_world.inverted()
        from mathutils import Vector
        local_pos_vec = parent_inv @ Vector(world_pos)
        local_target_vec = parent_inv @ Vector(cam_target)
        local_pos = list(local_pos_vec)
        local_target = list(local_target_vec)

    cam_data = {
        "name": arm.utils.safesrc(linked_utils.asset_name(obj)),
        "pos": world_pos,
        "target": cam_target,
        "fov": cam_fov,
        "near": obj.data.clip_start,
        "far": obj.data.clip_end,
        "traits": _extract_traits(obj),
        "parent_index": parent_index,
    }
    if local_pos is not None:
        cam_data["local_pos"] = local_pos
        cam_data["local_target"] = local_target

    exporter.scene_data[scene_name]["cameras"].append(cam_data)


def _process_light(exporter, scene_name, obj, instance_matrix=None):
    """Process a light object and add to scene data."""
    # Compute world matrix considering instance transform
    world_matrix = instance_matrix if instance_matrix else obj.matrix_world
    light_dir = world_matrix.to_3x3().col[2]

    lights = exporter.scene_data[scene_name]["lights"]
    if len(lights) >= MAX_LIGHTS:
        log.warn(f'N64: scene "{scene_name}" has more than {MAX_LIGHTS} lights, the most tiny3d draws: '
                 f'"{obj.name}" is left out')
        return
    lights.append({
        "name": arm.utils.safesrc(linked_utils.asset_name(obj)),
        "pos": list(world_matrix.to_translation()),
        "color": list(obj.data.color),
        "dir": list(light_dir),
        "traits": _extract_traits(obj)
    })


def _process_empty_object(exporter, scene_name, obj, object_name_to_index=None):
    """Process an empty object and add to scene data as an ArmObject (no mesh).

    Empties are lightweight transform nodes in the unified objects[] array.
    They support full parent/child hierarchy, traits, and can serve as
    parents for cameras, mesh objects, or other empties.
    """
    world_matrix = obj.matrix_world

    # Parent resolution — empties can parent to any object type in objects[]
    parent_index = -1
    has_parent = False
    if object_name_to_index and obj.parent and obj.parent.name in object_name_to_index:
        parent_index = object_name_to_index[obj.parent.name]
        has_parent = True

    # World-space decomposition
    world_pos = list(world_matrix.to_translation())
    world_quat = world_matrix.to_quaternion()
    world_scale = world_matrix.to_scale()

    # Local transform for parented empties
    if has_parent:
        local_matrix = obj.matrix_local
        local_pos = list(local_matrix.to_translation())
        local_quat = local_matrix.to_quaternion()
        local_scale = local_matrix.to_scale()
    else:
        local_pos = world_pos
        local_quat = world_quat
        local_scale = world_scale

    obj_data = {
        "name": arm.utils.safesrc(obj.name),
        "is_empty": True,
        "pos": world_pos,
        "rot": [world_quat.x, world_quat.y, world_quat.z, world_quat.w],
        "scale": list(world_scale),
        "visible": is_visible(obj, drawn=False),
        "bounds_center": [0, 0, 0],
        "bounds_half": [0, 0, 0],
        "traits": _extract_traits(obj),
        "parent_index": parent_index,
        "local_pos": local_pos,
        "local_rot": [local_quat.x, local_quat.y, local_quat.z, local_quat.w],
        "local_scale": list(local_scale),
    }

    exporter.scene_data[scene_name]["objects"].append(obj_data)


def _process_mesh_object(exporter, scene_name, obj, instance_matrix=None, object_name_to_index=None, instancer_parent=None):
    """Process a mesh object and add to scene data.

    Args:
        instancer_parent: For instance collection objects, the mesh parent
            of the instancer Empty (e.g. Cube). Used to propagate hierarchy
            so instance collection members follow the parent at runtime.
    """
    mesh = obj.data
    if mesh not in exporter.exported_meshes:
        log.warn(f'Object "{obj.name}": mesh not exported, skipping')
        return
    mesh_name = exporter.exported_meshes[mesh]

    # Determine parent index (−1 = root / no parent)
    parent_index = -1
    has_parent = False
    parent_source = None  # 'direct' or 'instancer'

    if instance_matrix:
        # Instance collection object: parent is the instancer Empty's parent.
        # Intra-collection parents (obj.parent within the linked collection)
        # are skipped because names collide across multiple instances of the
        # same collection.  Flattening all members under the instancer's
        # parent is correct for the typical use-case (decoration objects that
        # rotate/move with a mesh parent like Cube).
        if (object_name_to_index and instancer_parent is not None
                and instancer_parent.name in object_name_to_index):
            parent_index = object_name_to_index[instancer_parent.name]
            has_parent = True
            parent_source = 'instancer'
    else:
        # Direct scene object: use Blender's obj.parent
        if object_name_to_index and obj.parent and obj.parent.name in object_name_to_index:
            parent_index = object_name_to_index[obj.parent.name]
            has_parent = True
            parent_source = 'direct'

    # Compute world matrix considering instance transform
    world_matrix = instance_matrix if instance_matrix else obj.matrix_world

    # World-space decomposition (always needed for AABB computation and obj_data["pos"])
    world_pos = list(world_matrix.to_translation())
    _warn_if_out_of_range(obj, world_pos)
    world_quat = world_matrix.to_quaternion()
    world_scale = world_matrix.to_scale()

    # For parented objects, also compute local-space transform relative to parent.
    # For root objects, local == world.
    if has_parent and parent_source == 'instancer':
        # Instance collection member parented to instancer's parent.
        # Compute local transform relative to the instancer's parent world matrix.
        parent_world = instancer_parent.matrix_world
        local_matrix = parent_world.inverted() @ world_matrix
        local_pos = list(local_matrix.to_translation())
        local_quat = local_matrix.to_quaternion()
        local_scale = local_matrix.to_scale()
    elif has_parent and parent_source == 'direct':
        # Direct scene child: use Blender's local matrix (relative to parent)
        local_matrix = obj.matrix_local
        local_pos = list(local_matrix.to_translation())
        local_quat = local_matrix.to_quaternion()
        local_scale = local_matrix.to_scale()
    else:
        local_pos = world_pos
        local_quat = world_quat
        local_scale = world_scale

    # Export rotation as quaternion (XYZW order for T3D)

    # Compute AABB from mesh's bounding box (local space)
    bb = obj.bound_box
    local_min = [min(v[i] for v in bb) for i in range(3)]
    local_max = [max(v[i] for v in bb) for i in range(3)]
    half_extents = [
        (local_max[0] - local_min[0]) * 0.5,
        (local_max[1] - local_min[1]) * 0.5,
        (local_max[2] - local_min[2]) * 0.5
    ]

    # Culling bounds in the mesh's own space (the renderer applies the object's
    # world matrix: scale, rotation and parents)
    bounds_center = [(local_min[i] + local_max[i]) * 0.5 for i in range(3)]

    # Extract rigid body data
    rigid_body_data = _extract_rigid_body(exporter, obj, half_extents, world_scale)

    obj_data = {
        "name": arm.utils.safesrc(linked_utils.asset_name(obj)),
        "mesh": f'MODEL_{mesh_name.upper()}',
        "pos": world_pos,
        "rot": [world_quat.x, world_quat.y, world_quat.z, world_quat.w],
        "scale": list(world_scale),
        "visible": is_visible(obj),
        "bounds_center": bounds_center,
        "bounds_half": half_extents,
        "traits": _extract_traits(obj),
        "parent_index": parent_index,
        "local_pos": local_pos,
        "local_rot": [local_quat.x, local_quat.y, local_quat.z, local_quat.w],
        "local_scale": list(local_scale),
    }

    # Skinned mesh detection — check exporter.skinned_meshes populated by mesh_exporter
    skinned_info = getattr(exporter, 'skinned_meshes', {}).get(mesh_name)
    if skinned_info:
        obj_data["is_skinned"] = True
        obj_data["anim_clips"] = skinned_info.get("anim_clips", [])

        # Promote armature traits to the skinned mesh child.
        # Armature objects are filtered out of the N64 scene (only MESH/EMPTY
        # pass), so any traits attached to the armature in Blender would be
        # lost.  Since animation_init runs on the skinned mesh, the trait
        # needs to live here so `object.animation` is non-NULL.
        armature = find_armature(obj)
        if armature is not None:
            armature_traits = _extract_traits(armature)
            if armature_traits:
                obj_data["traits"] = obj_data["traits"] + armature_traits
                log.info(f'Object "{obj.name}": promoted {len(armature_traits)} trait(s) from armature "{armature.name}"')

    if rigid_body_data is not None:
        obj_data["rigid_body"] = rigid_body_data

    exporter.scene_data[scene_name]["objects"].append(obj_data)



def _extract_rigid_body(exporter, obj, half_extents, world_scale):
    """Extract rigid body data from an object if present.

    Args:
        exporter: N64Exporter instance to update with physics flag
        obj: Blender object with potential rigid body
        half_extents: Pre-computed half extents from bounding box
        world_scale: The object's world scale, the one it's drawn with (its own
            scale times its parents' or collection instancer's)

    Returns:
        dict with rigid body data or None
    """
    wrd = bpy.data.worlds['Arm']
    if obj.rigid_body is None or wrd.arm_physics == 'Disabled':
        return None

    rb = obj.rigid_body
    shape = rb.collision_shape
    scale = [abs(s) for s in world_scale]

    # Body type, as Armory's exporter (rigid_body_static) and oimo_module:
    # Animated -> kinematic; Passive, or Active without Dynamic -> static
    is_static = ((not rb.enabled and not rb.kinematic) or (rb.type == 'PASSIVE' and not rb.kinematic))

    # Sizes as oimo_module's setShape: the object's dimensions, shrunk by the
    # collision margin (dim - dim * margin)
    margin = rb.collision_margin if rb.use_margin else 0.0
    dim = [half_extents[i] * 2.0 * scale[i] * (1.0 - margin) for i in range(3)]

    # N64 supports BOX, SPHERE, CAPSULE, and MESH (static only)
    rb_mesh_data = None
    rb_radius = rb_half_height = rb_half_extents = None
    if shape == 'SPHERE':
        rb_shape = "sphere"
        rb_radius = max(dim) * 0.5
    elif shape == 'CAPSULE':
        rb_shape = "capsule"
        rb_radius = dim[0] * 0.5
        rb_half_height = dim[2] * 0.5 - rb_radius
        if rb_half_height < 0:
            # As oimo_module (RigidBody.hx): Krom gets the same broken capsule
            log.warn(f'Object "{obj.name}": its capsule collider is wider than tall '
                     f'(the height must be at least the width)')
    elif shape == 'MESH' and is_static:
        rb_shape = "mesh"
        rb_mesh_data = n64_utils.extract_collision_mesh(obj, world_scale)
        if rb_mesh_data is None:
            log.warn(f'Object "{obj.name}": failed to extract mesh collision data, using BOX')
            rb_shape = "box"
        elif (rb_mesh_data["num_vertices"] > MESH_COLLIDER_MAX_VERTICES
              or rb_mesh_data["num_triangles"] > MESH_COLLIDER_MAX_TRIANGLES):
            raise ValueError(f'Object "{obj.name}": its mesh collider has {rb_mesh_data["num_vertices"]} vertices '
                             f'and {rb_mesh_data["num_triangles"]} triangles; the N64 holds at most '
                             f'{MESH_COLLIDER_MAX_VERTICES} and {MESH_COLLIDER_MAX_TRIANGLES}')
    else:
        rb_shape = "box"
        if shape not in ('BOX', 'SPHERE', 'CAPSULE'):
            if shape == 'MESH':
                log.warn(f'Object "{obj.name}": MESH collision shape only supported for static bodies, using BOX')
            else:
                log.warn(f'Object "{obj.name}": collision shape "{shape}" not supported on N64, using BOX')
    if rb_shape == "box":
        # Blender (x, y, z) -> Oimo (x, z, y)
        rb_half_extents = [dim[0] * 0.5, dim[2] * 0.5, dim[1] * 0.5]

    # Mass (0 = static)
    rb_mass = 0.0 if is_static else rb.mass

    # Rotation factor (lock rotation: 0), Blender (x, y, z) -> Oimo (x, z, y)
    angular = list(getattr(obj, 'arm_rb_angular_factor', (1.0, 1.0, 1.0)))
    for i in range(3):
        if obj.lock_rotation[i]:
            angular[i] = 0.0

    # Collision groups/masks
    col_group = 0
    for i, b in enumerate(rb.collision_collections):
        if b:
            col_group |= (1 << i)

    col_mask = 0
    if hasattr(obj, 'arm_rb_collision_filter_mask'):
        for i, b in enumerate(obj.arm_rb_collision_filter_mask):
            if b:
                col_mask |= (1 << i)
    else:
        col_mask = 1

    rigid_body_data = {
        "shape": rb_shape,
        "mass": rb_mass,
        "friction": rb.friction,
        "restitution": rb.restitution,
        "linear_damping": rb.linear_damping,
        "angular_damping": rb.angular_damping,
        "collision_group": col_group,
        "collision_mask": col_mask,
        "is_trigger": getattr(obj, 'arm_rb_trigger', False),
        "is_static": is_static,
        "is_animated": rb.kinematic,
        "use_deactivation": rb.use_deactivation,
        # Armory's exporter writes the angular one under a misspelled key that
        # oimo_module never reads; the N64 uses it as intended
        "sleep_linear_velocity": rb.deactivate_linear_velocity,
        "sleep_angular_velocity": rb.deactivate_angular_velocity,
        "angular_friction": getattr(obj, 'arm_rb_angular_friction', 0.1),
        "angular_factor": [angular[0], angular[2], angular[1]],
        "interpolate": getattr(obj, 'arm_rb_interpolate', False)
    }

    if rb_shape == "sphere":
        rigid_body_data["radius"] = rb_radius
    elif rb_shape == "capsule":
        rigid_body_data["radius"] = rb_radius
        rigid_body_data["half_height"] = rb_half_height
    elif rb_shape == "mesh":
        rigid_body_data["mesh_data"] = rb_mesh_data
        # The largest mesh collider sizes the BVH (limits.py)
        exporter.max_mesh_triangles = max(exporter.max_mesh_triangles, rb_mesh_data["num_triangles"])
    else:
        rigid_body_data["half_extents"] = rb_half_extents

    exporter.has_physics = True
    return rigid_body_data


def _extract_traits(obj):
    """Extract traits from an object's trait list.

    Args:
        obj: Blender object (can be object, scene, light, camera)

    Returns:
        list of trait dictionaries
    """
    import json

    traits = []
    if not hasattr(obj, 'arm_traitlist'):
        return traits

    for trait in obj.arm_traitlist:
        if not trait.enabled_prop:
            continue
        if trait.type_prop not in ('Haxe Script', 'UI Canvas'):
            log.warn(f'N64: "{obj.name}" has the {trait.type_prop} trait "{trait.name}": only Haxe Script '
                     'traits are supported on N64 for now, it is left out')
        if trait.type_prop != 'Haxe Script' or not trait.class_name_prop:
            continue

        props = {}
        type_overrides = {}
        if hasattr(trait, 'arm_traitpropslist'):
            for prop in trait.arm_traitpropslist:
                val = prop.get_value()
                override = getattr(prop, 'override_type_prop', 'auto')
                try:
                    val = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass
                props[prop.name] = val
                if override and override != 'auto':
                    type_overrides[prop.name] = override

        traits.append({
            'class_name': trait.class_name_prop,
            'props': props,
            'type_overrides': type_overrides
        })

    return traits


def write_scenes(exporter):
    """Write all scene C files.

    Args:
        exporter: N64Exporter instance with scene_data and trait_info
    """
    write_scenes_c(exporter)
    write_scenes_h(exporter)

    # Apply coordinate conversion (Blender Z-up → N64 Y-up)
    codegen.convert_scene_data(exporter.scene_data)

    # Write converted scene data to C files
    for scene in exported_scenes():
        write_scene_c(exporter, scene)


def write_scene_c(exporter, scene):
    """Write a scene's C file: its definition (scene_def_<name>), constant tables the
    runtime builds the scene from (codegen.generate_scene_file).

    Args:
        exporter: N64Exporter instance with scene_data and trait_info
        scene: Blender scene object
    """
    scene_name = arm.utils.safesrc(scene.name).lower()
    out_path = os.path.join(arm.utils.build_dir(), 'n64', 'src', 'scenes', f'{scene_name}.c')
    text = codegen.generate_scene_file(exporter.scene_data[scene_name], scene_name, scene.name, exporter.trait_info)
    n64_utils.write_if_changed(out_path, text)


def write_scenes_c(exporter):
    """Generate scenes_data.h (for scenes.c): by SceneId, each scene's definition,
    name, groups (collections: name -> object count) and the models it uses.

    Args:
        exporter: N64Exporter instance
    """
    inits, names, group_tables, groups, model_rows = [], [], [], [], []
    for scene in exported_scenes():
        scene_name = arm.utils.safesrc(scene.name).lower()
        scene_id = f'SCENE_{scene_name.upper()}'
        inits.append(f'    [{scene_id}] = &scene_def_{scene_name},')
        names.append(f'    [{scene_id}] = {c_string(scene.name)},')

        scene_data = exporter.scene_data.get(scene_name, {})
        models = sorted({o["mesh"] for o in scene_data.get("objects", []) if o.get("mesh")})
        model_rows.append(f'    [{scene_id}] = {{' + ''.join(f' [{m}] = true,' for m in models) + ' },')
        scene_groups = scene_data.get('groups', {})
        if scene_groups:
            group_tables.append(f'static const SceneGroupEntry g_scene_{scene_name}_groups[] = {{')
            group_tables += [f'    {{{c_string(info["original_name"])}, {info["count"]}}},' for info in scene_groups.values()]
            group_tables += ['};', '']
            groups.append(f'    [{scene_id}] = {{ g_scene_{scene_name}_groups, {len(scene_groups)} }},')

    lines = group_tables
    lines += ['static const ArmSceneDef *const g_scene_defs[SCENE_COUNT] = {', *inits, '};', '']
    lines += ['static const char *const g_scene_names[SCENE_COUNT] = {', *names, '};', '']
    lines += ['static const SceneGroups g_scene_groups[SCENE_COUNT] = {', *groups, '};', '']
    lines += ['static const bool g_scene_models[SCENE_COUNT][MODEL_COUNT] = {', *model_rows, '};']
    body = '\n'.join(lines)
    n64_utils.write_generated('data/scenes_data.h', body, 'scene_exporter.write_scenes_c', 'data/scenes.c')


def write_scenes_h(exporter):
    """Generate scenes_defs.h (for scenes.h): SceneId, each scene's definition,
    and the sizes of the storage the scenes share.

    Args:
        exporter: N64Exporter instance
    """
    scenes = exported_scenes()
    lines = ['typedef enum SceneId {']
    lines += [f'    SCENE_{arm.utils.safesrc(scene.name).upper()} = {i},' for i, scene in enumerate(scenes)]
    lines += [f'    SCENE_COUNT = {len(scenes)}', '} SceneId;', '']
    lines += [f'extern const ArmSceneDef scene_def_{arm.utils.safesrc(scene.name).lower()};' for scene in scenes]

    # The storage the scenes share: the largest scene's (at least 1 of each)
    data = [exporter.scene_data.get(arm.utils.safesrc(scene.name).lower(), {}) for scene in scenes]
    sizes = {
        'SCENE_MAX_OBJECTS': max((len(d.get('objects', [])) for d in data), default=0),
        'SCENE_MAX_CAMERAS': max((len(d.get('cameras', [])) for d in data), default=0),
        'SCENE_MAX_LIGHTS': max((len(d.get('lights', [])) for d in data), default=0),
        'SCENE_MAX_TRAITS': max((codegen.scene_trait_count(d) for d in data), default=0),
    }
    lines.append('')
    lines += [f'#define {name} {max(size, 1)}' for name, size in sizes.items()]
    n64_utils.write_generated('data/scenes_defs.h', '\n'.join(lines), 'scene_exporter.write_scenes_h', 'data/scenes.h')


def start_scene():
    """The scene the game starts in (Armory's exporter list), which also sets
    the game's resolution."""
    wrd = bpy.data.worlds['Arm']
    scene = wrd.arm_exporterlist[wrd.arm_exporterlist_index].arm_project_scene
    if scene not in exported_scenes():
        raise ValueError(f'N64: the start scene "{scene.name}" is not exported (its Export checkbox is off)')
    return scene


def write_main(exporter):
    """Generate main_config.h (for main.c): the start scene, Armory's physics
    fixed step, the physics debug drawing mode and whether there are autoloads.

    Args:
        exporter: N64Exporter instance with autoload_info
    """
    wrd = bpy.data.worlds['Arm']
    has_autoloads = exporter.autoload_info.get('has_autoloads', False)
    body = (f'#define INITIAL_SCENE SCENE_{arm.utils.safesrc(start_scene().name).upper()}\n'
            # Armory's physics fixed step (default 0.02 s = 50 Hz)
            f'#define FIXED_TIMESTEP ({getattr(wrd, "arm_physics_fixed_step", 0.02)}f)\n'
            f'#define PHYSICS_DEBUG_MODE {n64_utils.get_physics_debug_mode()}\n'
            f'#define HAS_AUTOLOADS {1 if has_autoloads else 0}\n')
    n64_utils.write_generated('main_config.h', body, 'scene_exporter.write_main', 'main.c')


def write_models(exporter):
    """Write models.c and models.h files.

    Args:
        exporter: N64Exporter instance with exported_meshes
    """
    write_models_c(exporter)
    write_models_h(exporter)


def _model_names(exporter) -> list:
    """The exported models, in a stable order (by name): models.c and models.h
    don't change between exports of the same scenes, so make doesn't rebuild
    what includes them."""
    return sorted(exporter.exported_meshes.values())


def write_models_c(exporter):
    """Generate models_data.h (for models.c): the model files, by ModelId."""
    lines = ['static const char *MODEL_PATHS[MODEL_COUNT] = {']
    lines += [f'    "rom:/{model_name}.t3dm",' for model_name in _model_names(exporter)]
    lines.append('};')
    n64_utils.write_generated('data/models_data.h', '\n'.join(lines), 'scene_exporter.write_models_c', 'data/models.c')


def write_models_h(exporter):
    """Generate models_defs.h (for models.h): ModelId."""
    lines = ['typedef enum {']
    lines += [f'    MODEL_{model_name.upper()} = {i},' for i, model_name in enumerate(_model_names(exporter))]
    lines += [f'    MODEL_COUNT = {len(exporter.exported_meshes)}', '} ModelId;']
    n64_utils.write_generated('data/models_defs.h', '\n'.join(lines), 'scene_exporter.write_models_h', 'data/models.h')
