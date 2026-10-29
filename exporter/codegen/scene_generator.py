"""
Scene Generator - Generates C code blocks for scene data.

Each scene becomes constant tables (an ArmSceneDef: cameras, lights, objects,
traits, rigid bodies) that the runtime builds the scene from.
"""

import math
from typing import Dict, List

from armor64 import utils as n64_utils
from armor64.utils import convert_vec3_list, convert_quat_list, convert_scale_list, SCALE_FACTOR


# =============================================================================
# Scene Data Conversion
# =============================================================================

def convert_scene_data(scene_data: dict) -> dict:
    """Apply coordinate conversion to all scene data (modifies in place).

    Converts from Blender coordinates (X=right, Y=forward, Z=up)
    to N64/T3D coordinates (X=right, Y=up, Z=back).
    """
    for scene_name, scene in scene_data.items():
        # Convert cameras
        for cam in scene.get('cameras', []):
            cam['pos'] = convert_vec3_list(cam['pos'])
            cam['target'] = convert_vec3_list(cam['target'])
            # Convert local transforms for parented cameras.
            # Must divide by SCALE_FACTOR to compensate for the parent's
            # scaled world_mat — same as object local_pos conversion.
            if 'local_pos' in cam:
                swapped = convert_vec3_list(cam['local_pos'])
                cam['local_pos'] = [v / SCALE_FACTOR for v in swapped]
            if 'local_target' in cam:
                swapped = convert_vec3_list(cam['local_target'])
                cam['local_target'] = [v / SCALE_FACTOR for v in swapped]

        # Convert lights
        for light in scene.get('lights', []):
            light['dir'] = convert_vec3_list(light['dir'])
            # Normalize direction
            d = light['dir']
            length = math.sqrt(d[0]**2 + d[1]**2 + d[2]**2)
            if length > 0:
                light['dir'] = [d[0]/length, d[1]/length, d[2]/length]

        # Convert objects
        for obj in scene.get('objects', []):
            obj['pos'] = convert_vec3_list(obj['pos'])
            obj['rot'] = convert_quat_list(obj['rot'])
            obj['scale'] = convert_scale_list(obj['scale'])
            # Convert local transforms for parented objects.
            # SCALE_FACTOR must NOT be applied to local_scale — it's already
            # inherited through the parent chain's world matrix (just like how
            # tiny3d skeleton bone local transforms carry no extra scale).
            # local_pos must be divided by SCALE_FACTOR to compensate for the
            # parent's SF shrinking the offset during matrix composition.
            if 'local_pos' in obj:
                swapped = convert_vec3_list(obj['local_pos'])
                obj['local_pos'] = [v / SCALE_FACTOR for v in swapped]
            if 'local_rot' in obj:
                obj['local_rot'] = convert_quat_list(obj['local_rot'])
            if 'local_scale' in obj:
                obj['local_scale'] = convert_scale_list(obj['local_scale'], factor=1.0)
            if 'bounds_center' in obj:
                # Model units: Blender units × 64 (tiny3d's base scale), as the world matrix expects
                obj['bounds_center'] = [v / SCALE_FACTOR for v in convert_vec3_list(obj['bounds_center'])]
                obj['bounds_half'] = [abs(v) / SCALE_FACTOR for v in convert_vec3_list(obj['bounds_half'])]

    return scene_data


# =============================================================================
# Scene Code Generation: each scene as constant tables (an ArmSceneDef) that
# scene_init (data/scenes.c) builds the scene from
# =============================================================================

def _f(v: float) -> str:
    return f'{v:.6f}f'


def _vec3(v: List[float]) -> str:
    return f'{{{{{_f(v[0])}, {_f(v[1])}, {_f(v[2])}}}}}'


def _quat(q: List[float]) -> str:
    return f'{{{{{_f(q[0])}, {_f(q[1])}, {_f(q[2])}, {_f(q[3])}}}}}'


def _bool(v) -> str:
    return 'true' if v else 'false'


# The trait events each ARM_LIFECYCLE_* flag stands for (types.h)
_LIFECYCLE_FLAGS = (('on_fixed_update', 'ARM_LIFECYCLE_FIXED_UPDATE'), ('on_update', 'ARM_LIFECYCLE_UPDATE'),
                    ('on_late_update', 'ARM_LIFECYCLE_LATE_UPDATE'), ('on_render2d', 'ARM_LIFECYCLE_RENDER2D'))


def _lifecycle_flags(traits: List[Dict], trait_info: dict) -> str:
    """ARM_LIFECYCLE_* of an entity: the events its traits (and their ancestors) handle."""
    events = set()
    for trait in traits:
        events |= n64_utils.trait_chain_events(trait_info, trait["class_name"])
    return ' | '.join(flag for event, flag in _LIFECYCLE_FLAGS if event in events) or '0'


def scene_trait_count(scene: dict) -> int:
    """Every trait of a scene: its cameras', lights', objects' and its own."""
    entities = scene.get('cameras', []) + scene.get('lights', []) + scene.get('objects', [])
    return sum(len(e.get('traits', [])) for e in entities) + len(scene.get('traits', []))


class _Traits:
    """A scene's traits (one table, in entity order) and their data."""

    def __init__(self, trait_info: dict):
        self.trait_info = trait_info
        self.rows = []       # ArmTrait initializers
        self.data = []       # static data variables
        self.data_init = []  # their initial values (init_trait_data)

    def add(self, traits: List[Dict], entity_ptr, entity: str) -> int:
        """Adds an entity's traits; returns the index of its first."""
        first = len(self.rows)
        for trait in traits:
            trait_class = trait["class_name"]
            c_name = n64_utils.get_trait(self.trait_info, trait_class).get("c_name", "")
            if not c_name:
                raise ValueError(f'Trait "{trait_class}" of {entity} has no N64 code: the Haxe build '
                                 f'didn\'t convert it (see the Haxe errors above)')
            events = n64_utils.trait_chain_events(self.trait_info, trait_class)

            def handler(event):  # NULL when neither the trait nor an ancestor handles it
                return f'{c_name}_{event}' if event in events else 'NULL'

            data = 'NULL'
            if n64_utils.trait_needs_data(self.trait_info, trait_class):
                var = f'g_trait_data_{len(self.rows)}'
                struct_name = f'{c_name}Data'
                initializer = n64_utils.build_trait_initializer(
                    self.trait_info, trait_class, trait.get("props", {}),
                    trait.get("type_overrides", {}), entity_ptr)
                self.data.append(f'static {struct_name} {var};')
                self.data_init.append(f'    {var} = ({struct_name}){{{initializer}}};')
                data = f'&{var}'
            self.rows.append(f'    {{ .on_add = {c_name}_on_add, .on_ready = {c_name}_on_ready, '
                             f'.on_fixed_update = {handler("on_fixed_update")}, .on_update = {handler("on_update")}, '
                             f'.on_late_update = {handler("on_late_update")}, .on_remove = {handler("on_remove")}, '
                             f'.on_render2d = {handler("on_render2d")}, .data = {data} }},')
        return first


def _entity(traits: _Traits, entity: dict, entity_ptr: str, kind: str, trait_info: dict) -> str:
    """The trait fields of a camera, light or object row."""
    entity_traits = entity.get("traits", [])
    first = traits.add(entity_traits, entity_ptr, f'{kind} "{entity["name"]}"')
    return (f'.first_trait = {first}, .trait_count = {len(entity_traits)}, '
            f'.lifecycle_flags = {_lifecycle_flags(entity_traits, trait_info)}')


def _camera_row(i: int, cam: dict, traits: _Traits, trait_info: dict) -> str:
    """A camera: parented ones also keep their position and target relative to the parent."""
    parent = cam["parent_index"]
    local_pos = cam["local_pos"] if parent >= 0 else [0, 0, 0]
    local_target = cam["local_target"] if parent >= 0 else [0, 0, 0]
    return (f'    {{ .loc = {_vec3(cam["pos"])}, .target = {_vec3(cam["target"])}, .fov = {_f(cam["fov"])}, '
            f'.near = {_f(cam["near"])}, .far = {_f(cam["far"])}, .parent_index = {parent}, '
            f'.local_pos = {_vec3(local_pos)}, .local_target = {_vec3(local_target)}, '
            f'{_entity(traits, cam, f"&g_scene_cameras[{i}]", "camera", trait_info)} }},')


def _light_row(i: int, light: dict, traits: _Traits, trait_info: dict) -> str:
    color = ', '.join(str(n64_utils.to_uint8(c)) for c in light["color"][:3])
    return (f'    {{ .color = {{{color}}}, .dir = {_vec3(light["dir"])}, '
            f'{_entity(traits, light, f"&g_scene_lights[{i}]", "light", trait_info)} }},')


def _object_row(i: int, obj: dict, traits: _Traits, trait_info: dict) -> str:
    """An object: parented ones keep their local transform, root ones their world one.
    Empties (no mesh) are transform nodes in the hierarchy that can have traits."""
    parent = obj["parent_index"]
    if parent >= 0:
        loc, rot, scale = obj["local_pos"], obj["local_rot"], obj["local_scale"]
    else:
        loc, rot, scale = obj["pos"], obj["rot"], obj["scale"]
    is_empty = obj.get("is_empty", False)  # Set on empties only, is_skinned on skinned meshes only
    model = '-1' if is_empty else obj["mesh"]
    return (f'    {{ .loc = {_vec3(loc)}, .rot = {_quat(rot)}, .scale = {_vec3(scale)}, '
            f'.bounds_center = {_vec3(obj["bounds_center"])}, .bounds_half = {_vec3(obj["bounds_half"])}, '
            f'.model = {model}, .parent_index = {parent}, '
            f'{_entity(traits, obj, f"&g_scene_objects[{i}]", "object", trait_info)}, '
            f'.visible = {_bool(obj["visible"])}, .skinned = {_bool(obj.get("is_skinned", False) and not is_empty)} }},')


def _physics(objects: List[Dict]) -> tuple:
    """(mesh collider arrays, PhysicsBodyDef rows): the rigid bodies, in object order."""
    meshes, rows = [], []
    for i, obj in enumerate(objects):
        rb = obj.get("rigid_body")
        if rb is None:
            continue
        # Oimo body type as oimo_module: animated -> kinematic, static (decided by
        # the exporter as Armory's) -> static, else dynamic
        is_animated = rb["is_animated"]
        if is_animated:
            rb_type = "OIMO_RIGID_BODY_KINEMATIC"
        elif rb["is_static"]:
            rb_type = "OIMO_RIGID_BODY_STATIC"
        else:
            rb_type = "OIMO_RIGID_BODY_DYNAMIC"
        factor = rb["angular_factor"]
        params = (f'.mass = {_f(rb["mass"])}, .friction = {_f(rb["friction"])}, '
                  f'.restitution = {_f(rb["restitution"])}, '
                  f'.linear_damping = {_f(rb["linear_damping"])}, '
                  f'.angular_damping = {_f(rb["angular_damping"])}, '
                  f'.angular_friction = {_f(rb["angular_friction"])}, '
                  f'.angular_factor = {{{_f(factor[0])}, {_f(factor[1])}, {_f(factor[2])}}}, '
                  f'.sleep_linear_velocity = {_f(rb["sleep_linear_velocity"])}, '
                  f'.sleep_angular_velocity = {_f(rb["sleep_angular_velocity"])}, '
                  f'.collision_group = {rb["collision_group"]}, .collision_mask = {rb["collision_mask"]}, '
                  f'.animated = {_bool(is_animated)}, .trigger = {_bool(rb["is_trigger"])}, '
                  f'.use_deactivation = {_bool(rb["use_deactivation"])}, '
                  f'.interpolate = {_bool(rb["interpolate"])}')

        # The shapes scene_exporter makes (it turns the others into boxes, with a warning)
        shape, size, mesh = rb["shape"], [0.0, 0.0, 0.0], ''
        if shape == "mesh":
            mesh_data = rb["mesh_data"]
            vertices, indices = mesh_data["vertices"], mesh_data["indices"]
            meshes.append(f'static const OimoVec3 k_mesh_{i}_vertices[] = {{')
            meshes += [f'    {{{_f(v[0])}, {_f(v[1])}, {_f(v[2])}}},' for v in vertices]
            meshes += ['};', f'static const uint16_t k_mesh_{i}_indices[] = {{']
            meshes += ['    ' + ', '.join(str(x) for x in indices[t:t + 6]) + ',' for t in range(0, len(indices), 6)]
            meshes += ['};', '']
            shape_c = 'PHYSICS_SHAPE_MESH'
            mesh = (f', .mesh_vertices = k_mesh_{i}_vertices, .mesh_indices = k_mesh_{i}_indices, '
                    f'.mesh_vertex_count = {len(vertices)}, .mesh_index_count = {len(indices)}')
        elif shape == "sphere":
            shape_c, size = 'PHYSICS_SHAPE_SPHERE', [rb["radius"], 0, 0]
        elif shape == "capsule":
            shape_c, size = 'PHYSICS_SHAPE_CAPSULE', [rb["radius"], rb["half_height"], 0]
        else:
            shape_c, size = 'PHYSICS_SHAPE_BOX', rb["half_extents"]
        rows.append(f'    {{ .object = {i}, .type = {rb_type}, .shape = {shape_c}, '
                    f'.size = {{{_f(size[0])}, {_f(size[1])}, {_f(size[2])}}}{mesh}, .params = {{ {params} }} }},  '
                    f'// {obj["name"]}')
    return meshes, rows


def generate_scene_file(scene: dict, scene_name: str, original_name: str, trait_info: dict) -> str:
    """The C file of a scene: its definition (scene_def_<name>, an ArmSceneDef)."""
    traits = _Traits(trait_info)
    cameras = [_camera_row(i, c, traits, trait_info) for i, c in enumerate(scene['cameras'])]
    lights = [_light_row(i, l, traits, trait_info) for i, l in enumerate(scene['lights'])]
    objects = [_object_row(i, o, traits, trait_info) for i, o in enumerate(scene['objects'])]
    scene_traits_first = traits.add(scene.get('traits', []), None, f'scene "{original_name}"')
    meshes, bodies = _physics(scene['objects'])
    world = scene['world']
    gravity = convert_vec3_list(world["gravity"])
    has_parented = any(o.get('parent_index', -1) >= 0 for o in scene['objects'])

    def table(c_type, name, rows):
        return [f'static const {c_type} {name}[] = {{', *rows, '};', ''] if rows else []

    def colors(c):
        return ', '.join(str(n64_utils.to_uint8(v)) for v in c[:3])

    lines = [f'// Generated by armor_64 (scene_generator) from the Blender scene "{original_name}". Do not edit.',
             '#include <libdragon.h>',
             '#include "../types.h"',
             '#include "../engine.h"',
             '#include "../data/models.h"',
             '#include "../data/traits.h"',
             '#include "../data/scenes.h"',
             '#if ENGINE_ENABLE_PHYSICS',
             '#include "../oimo/physics.h"',
             '#endif',
             '']
    lines += ['// The traits\' data, set to their initial values at each scene init', *traits.data, '',
              'static void init_trait_data(void)', '{', *traits.data_init, '}', '']
    lines += table('ArmTrait', 'k_traits', traits.rows)
    lines += table('ArmCameraDef', 'k_cameras', cameras)
    lines += table('ArmLightDef', 'k_lights', lights)
    lines += table('ArmObjectDef', 'k_objects', objects)
    if bodies:
        lines += ['#if ENGINE_ENABLE_PHYSICS', *meshes, *table('PhysicsBodyDef', 'k_bodies', bodies), '#endif', '']

    def ref(name, rows):
        return name if rows else 'NULL'

    lines += [f'const ArmSceneDef scene_def_{scene_name} = {{',
              f'    .clear_color = {{{colors(world["clear_color"])}}},',
              f'    .ambient_color = {{{colors(world["ambient_color"])}}},',
              f'    .cameras = {ref("k_cameras", cameras)}, .camera_count = {len(cameras)},',
              f'    .lights = {ref("k_lights", lights)}, .light_count = {len(lights)},',
              f'    .objects = {ref("k_objects", objects)}, .object_count = {len(objects)},',
              f'    .traits = {ref("k_traits", traits.rows)}, .trait_count = {len(traits.rows)},',
              f'    .scene_traits_first = {scene_traits_first}, .scene_trait_count = {len(scene.get("traits", []))},',
              '    .init_trait_data = init_trait_data,',
              f'    .has_parented_objects = {_bool(has_parented)},']
    if bodies:
        lines += ['#if ENGINE_ENABLE_PHYSICS', f'    .bodies = k_bodies, .body_count = {len(bodies)},', '#endif']
    lines += [f'    .gravity = {{{_f(gravity[0])}, {_f(gravity[1])}, {_f(gravity[2])}}},',
              f'    .physics_time_scale = {_f(world["physics_time_scale"])},',
              '};']
    return '\n'.join(lines) + '\n'
