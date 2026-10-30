// Scenes: init, per-frame trait dispatch, clearing, lookups. Scene data: scenes_data.h (generated).
#include <libdragon.h>
#include <t3d/t3d.h>
#include <t3d/t3dmodel.h>
#include <string.h>

#include "../types.h"
#include "../engine.h"
#include "../renderer.h"
#include "../iron/object/animation.h"
#include "../system/time.h"
#include "scenes.h"
#include "models.h"

#if ENGINE_ENABLE_PHYSICS
#include "../oimo/physics.h"
#endif

static ArmScene g_scenes[SCENE_COUNT];
static SceneId g_current_scene = SCENE_COUNT;
static SceneId g_pending_scene = SCENE_COUNT;

// A scene's groups (Blender collections): name -> object count
typedef struct {
    const char *name;
    uint16_t count;
} SceneGroupEntry;

typedef struct {
    const SceneGroupEntry *entries;
    uint16_t count;
} SceneGroups;

// By SceneId: g_scene_defs, g_scene_names, g_scene_groups, and g_scene_models
// (the models a scene uses: a scene switch keeps the next scene's loaded)
#include "scenes_data.h"

// The current scene's contents, in the storage every scene shares (only one
// exists at a time): sized for the largest scene (scenes_defs.h)
ArmObject g_scene_objects[SCENE_MAX_OBJECTS];
ArmCamera g_scene_cameras[SCENE_MAX_CAMERAS];
ArmLight g_scene_lights[SCENE_MAX_LIGHTS];
static ArmTrait g_scene_traits[SCENE_MAX_TRAITS];
static uint16_t g_active_objects[SCENE_MAX_OBJECTS];
static uint16_t g_physics_objects[SCENE_MAX_OBJECTS];
static uint16_t g_animated_objects[SCENE_MAX_OBJECTS];
static uint16_t g_render2d_objects[SCENE_MAX_OBJECTS];

static ArmTrait *scene_traits(uint16_t first, uint8_t count)
{
    return count > 0 ? &g_scene_traits[first] : NULL;
}

static void build_cameras(ArmScene *s, const ArmSceneDef *def)
{
    s->cameras = g_scene_cameras;
    s->camera_count = def->camera_count;
    s->active_camera_id = 0;
    for (uint8_t i = 0; i < def->camera_count; i++) {
        const ArmCameraDef *cd = &def->cameras[i];
        ArmCamera *cam = &g_scene_cameras[i];
        memset(cam, 0, sizeof(*cam));
        cam->transform.loc = cd->loc;
        cam->target = cd->target;
        for (int k = 0; k < 3; k++) cam->look_dir.v[k] = cd->target.v[k] - cd->loc.v[k];
        cam->fov = cd->fov;
        cam->near = cd->near;
        cam->far = cd->far;
        if (cd->parent_index >= 0) {
            cam->parent_world_mat = &g_scene_objects[cd->parent_index].world_mat;
            cam->local_pos = cd->local_pos;
            cam->local_target = cd->local_target;
        }
        cam->traits = scene_traits(cd->first_trait, cd->trait_count);
        cam->trait_count = cd->trait_count;
        cam->lifecycle_flags = cd->lifecycle_flags;
    }
}

static void build_lights(ArmScene *s, const ArmSceneDef *def)
{
    s->lights = g_scene_lights;
    s->light_count = def->light_count;
    for (uint8_t i = 0; i < def->light_count; i++) {
        const ArmLightDef *ld = &def->lights[i];
        ArmLight *light = &g_scene_lights[i];
        memset(light, 0, sizeof(*light));
        light->color[0] = ld->color[0];
        light->color[1] = ld->color[1];
        light->color[2] = ld->color[2];
        light->dir = ld->dir;
        light->traits = scene_traits(ld->first_trait, ld->trait_count);
        light->trait_count = ld->trait_count;
        light->lifecycle_flags = ld->lifecycle_flags;
    }
}

static void build_objects(ArmScene *s, const ArmSceneDef *def)
{
    s->objects = g_scene_objects;
    s->object_count = def->object_count;
    s->has_parented_objects = def->has_parented_objects;
    for (uint16_t i = 0; i < def->object_count; i++) {
        const ArmObjectDef *od = &def->objects[i];
        ArmObject *obj = &g_scene_objects[i];
        memset(obj, 0, sizeof(*obj));
        obj->transform.loc = od->loc;
        obj->transform.rot = od->rot;
        obj->transform.scale = od->scale;
        obj->transform.dirty = TRANSFORM_DIRTY_FRAMES;
        if (od->model >= 0 && od->skinned) {
            // Skinned: the model without a recorded block; the animation records its own
            T3DModel *mdl = models_get_raw((ModelId)od->model);
            obj->model_mat = malloc_uncached(sizeof(T3DMat4FP) * FB_COUNT);
            obj->animation = malloc(sizeof(ArmAnimation));
            animation_init(obj->animation, mdl);
            obj->dpl = obj->animation->dpl_skinned;
        } else if (od->model >= 0) {
            models_get((ModelId)od->model);
            obj->dpl = models_get_dpl((ModelId)od->model);
            obj->model_mat = malloc_uncached(sizeof(T3DMat4FP) * FB_COUNT);
        }
        obj->parent_index = od->parent_index;
        obj->transform.parented = od->parent_index >= 0;
        obj->visible = od->visible;
        obj->bounds_center = od->bounds_center;
        obj->bounds_half = od->bounds_half;
        obj->traits = scene_traits(od->first_trait, od->trait_count);
        obj->trait_count = od->trait_count;
        obj->lifecycle_flags = od->lifecycle_flags;
    }

    // The objects each per-frame pass walks
    s->active_objects = g_active_objects;
    s->render2d_objects = g_render2d_objects;
    s->animated_objects = g_animated_objects;
    for (uint16_t i = 0; i < s->object_count; i++) {
        ArmObject *obj = &g_scene_objects[i];
        if (obj->lifecycle_flags & (ARM_LIFECYCLE_FIXED_UPDATE | ARM_LIFECYCLE_UPDATE | ARM_LIFECYCLE_LATE_UPDATE)) {
            s->active_objects[s->active_trait_count++] = i;
        }
        if (obj->lifecycle_flags & ARM_LIFECYCLE_RENDER2D) {
            s->render2d_objects[s->render2d_count++] = i;
        }
        if (obj->animation) {
            s->animated_objects[s->animated_count++] = i;
        }
    }
}

static void build_physics(ArmScene *s, const ArmSceneDef *def)
{
    s->physics_objects = g_physics_objects;
#if ENGINE_ENABLE_PHYSICS
    physics_set_gravity(def->gravity[0], def->gravity[1], def->gravity[2]);
    physics_set_time_scale(def->physics_time_scale);
    for (uint16_t b = 0; b < def->body_count; b++) {
        physics_create_from_def(&g_scene_objects[def->bodies[b].object], &def->bodies[b]);
    }
    for (uint16_t i = 0; i < s->object_count; i++) {
        if (g_scene_objects[i].rigid_body) s->physics_objects[s->physics_count++] = i;
    }
#else
    (void)def;
#endif
}

// A scene from its definition: its traits (with their initial data), cameras,
// lights, objects and bodies
static void build_scene(ArmScene *s, const ArmSceneDef *def)
{
    memset(s, 0, sizeof(*s));
    s->world.clear_color[0] = def->clear_color[0];
    s->world.clear_color[1] = def->clear_color[1];
    s->world.clear_color[2] = def->clear_color[2];
    s->world.clear_color[3] = 0xFF;
    s->world.ambient_color[0] = def->ambient_color[0];
    s->world.ambient_color[1] = def->ambient_color[1];
    s->world.ambient_color[2] = def->ambient_color[2];
    s->world.ambient_color[3] = 0xFF;

    memcpy(g_scene_traits, def->traits, def->trait_count * sizeof(ArmTrait));
    def->init_trait_data();

    build_cameras(s, def);
    build_lights(s, def);
    build_objects(s, def);
    build_physics(s, def);

    s->traits = scene_traits(def->scene_traits_first, def->scene_trait_count);
    s->trait_count = def->scene_trait_count;
}

const bool *scene_models(SceneId id)
{
    return g_scene_models[id];
}

// Forward declarations
void scene_on_add(SceneId id);
void scene_on_ready(SceneId id);

void scene_init(SceneId id)
{
    if (id >= SCENE_COUNT) return;
    g_current_scene = id;

    build_scene(&g_scenes[id], g_scene_defs[id]);
    scene_on_add(id);
    scene_on_ready(id);
}

void scene_on_add(SceneId id)
{
    ArmScene *s = &g_scenes[id];

    // on_add runs BEFORE on_ready - used for setting up autoload references
    for (uint8_t i = 0; i < s->trait_count; i++) {
        if (s->traits[i].on_add) s->traits[i].on_add(s, s->traits[i].data);
    }
    if (s->cameras) {
        for (uint8_t i = 0; i < s->camera_count; i++) {
            for (uint8_t t = 0; t < s->cameras[i].trait_count; t++) {
                if (s->cameras[i].traits[t].on_add) s->cameras[i].traits[t].on_add(&s->cameras[i], s->cameras[i].traits[t].data);
            }
        }
    }
    if (s->lights) {
        for (uint8_t i = 0; i < s->light_count; i++) {
            for (uint8_t t = 0; t < s->lights[i].trait_count; t++) {
                if (s->lights[i].traits[t].on_add) s->lights[i].traits[t].on_add(&s->lights[i], s->lights[i].traits[t].data);
            }
        }
    }
    if (s->objects) {
        for (uint16_t i = 0; i < s->object_count; i++) {
            if (s->objects[i].is_removed) continue;
            for (uint8_t t = 0; t < s->objects[i].trait_count; t++) {
                if (s->objects[i].traits[t].on_add) s->objects[i].traits[t].on_add(&s->objects[i], s->objects[i].traits[t].data);
            }
        }
    }
}

void scene_on_ready(SceneId id)
{
    ArmScene *s = &g_scenes[id];

    for (uint8_t i = 0; i < s->trait_count; i++) {
        if (s->traits[i].on_ready) s->traits[i].on_ready(s, s->traits[i].data);
    }
    if (s->cameras) {
        for (uint8_t i = 0; i < s->camera_count; i++) {
            for (uint8_t t = 0; t < s->cameras[i].trait_count; t++) {
                if (s->cameras[i].traits[t].on_ready) s->cameras[i].traits[t].on_ready(&s->cameras[i], s->cameras[i].traits[t].data);
            }
        }
    }
    if (s->lights) {
        for (uint8_t i = 0; i < s->light_count; i++) {
            for (uint8_t t = 0; t < s->lights[i].trait_count; t++) {
                if (s->lights[i].traits[t].on_ready) s->lights[i].traits[t].on_ready(&s->lights[i], s->lights[i].traits[t].data);
            }
        }
    }
    if (s->objects) {
        for (uint16_t i = 0; i < s->object_count; i++) {
            if (s->objects[i].is_removed) continue;
            for (uint8_t t = 0; t < s->objects[i].trait_count; t++) {
                if (s->objects[i].traits[t].on_ready) s->objects[i].traits[t].on_ready(&s->objects[i], s->objects[i].traits[t].data);
            }
        }
    }
}

void scene_on_fixed_update(SceneId id, float dt)
{
    ArmScene *s = &g_scenes[id];

    // Scene-level traits
    for (uint8_t i = 0; i < s->trait_count; i++) {
        if (s->traits[i].on_fixed_update) s->traits[i].on_fixed_update(s, dt, s->traits[i].data);
    }

    // Cameras (usually 1, direct iteration is fine)
    if (s->cameras) {
        for (uint8_t i = 0; i < s->camera_count; i++) {
            if (!(s->cameras[i].lifecycle_flags & ARM_LIFECYCLE_FIXED_UPDATE)) continue;
            for (uint8_t t = 0; t < s->cameras[i].trait_count; t++) {
                if (s->cameras[i].traits[t].on_fixed_update) s->cameras[i].traits[t].on_fixed_update(&s->cameras[i], dt, s->cameras[i].traits[t].data);
            }
        }
    }

    // Lights (usually few, direct iteration is fine)
    if (s->lights) {
        for (uint8_t i = 0; i < s->light_count; i++) {
            if (!(s->lights[i].lifecycle_flags & ARM_LIFECYCLE_FIXED_UPDATE)) continue;
            for (uint8_t t = 0; t < s->lights[i].trait_count; t++) {
                if (s->lights[i].traits[t].on_fixed_update) s->lights[i].traits[t].on_fixed_update(&s->lights[i], dt, s->lights[i].traits[t].data);
            }
        }
    }

    // Objects - use active list for O(active) instead of O(total)
    if (s->active_objects && s->active_trait_count > 0) {
        for (uint16_t i = 0; i < s->active_trait_count; i++) {
            ArmObject *obj = &s->objects[s->active_objects[i]];
            if (obj->is_removed || !(obj->lifecycle_flags & ARM_LIFECYCLE_FIXED_UPDATE)) continue;
            for (uint8_t t = 0; t < obj->trait_count; t++) {
                if (obj->traits[t].on_fixed_update) obj->traits[t].on_fixed_update(obj, dt, obj->traits[t].data);
            }
        }
    }
}

// Once per frame, after the physics steps: bodies to their objects (alpha:
// how far the time is between the last step and the next)
void scene_sync_physics(SceneId id, float alpha)
{
#if ENGINE_ENABLE_PHYSICS
    ArmScene *s = &g_scenes[id];
    // Use physics_objects list for O(physics) instead of O(total)
    for (uint16_t i = 0; i < s->physics_count; i++) {
        ArmObject *obj = &s->objects[s->physics_objects[i]];
        if (obj->is_removed) continue;
        if (obj->parent_index >= 0) {
            // The parent's world position, from its world matrix (model units)
            const T3DMat4 *pm = &s->objects[obj->parent_index].world_mat;
            OimoVec3 parent_pos = oimo_vec3(pm->m[3][0] * BU_PER_MODEL_UNIT, pm->m[3][1] * BU_PER_MODEL_UNIT,
                                            pm->m[3][2] * BU_PER_MODEL_UNIT);
            physics_sync_object(obj, &parent_pos, alpha);
        } else {
            physics_sync_object(obj, NULL, alpha);
        }
    }
#else
    (void)id;
    (void)alpha;
#endif
}

void scene_on_update(SceneId id, float dt)
{
    ArmScene *s = &g_scenes[id];

    // Update skeletal animations using animated_objects list (Time.scale applies, as in Iron)
    for (uint16_t i = 0; i < s->animated_count; i++) {
        ArmObject *obj = &s->objects[s->animated_objects[i]];
        if (!obj->is_removed && obj->animation) {
            animation_update(obj->animation, dt * time_scale);
        }
    }

    // Scene-level traits
    for (uint8_t i = 0; i < s->trait_count; i++) {
        if (s->traits[i].on_update) s->traits[i].on_update(s, dt, s->traits[i].data);
    }

    // Cameras
    if (s->cameras) {
        for (uint8_t i = 0; i < s->camera_count; i++) {
            if (!(s->cameras[i].lifecycle_flags & ARM_LIFECYCLE_UPDATE)) continue;
            for (uint8_t t = 0; t < s->cameras[i].trait_count; t++) {
                if (s->cameras[i].traits[t].on_update) s->cameras[i].traits[t].on_update(&s->cameras[i], dt, s->cameras[i].traits[t].data);
            }
        }
    }

    // Lights
    if (s->lights) {
        for (uint8_t i = 0; i < s->light_count; i++) {
            if (!(s->lights[i].lifecycle_flags & ARM_LIFECYCLE_UPDATE)) continue;
            for (uint8_t t = 0; t < s->lights[i].trait_count; t++) {
                if (s->lights[i].traits[t].on_update) s->lights[i].traits[t].on_update(&s->lights[i], dt, s->lights[i].traits[t].data);
            }
        }
    }

    // Objects - use active list
    if (s->active_objects && s->active_trait_count > 0) {
        for (uint16_t i = 0; i < s->active_trait_count; i++) {
            ArmObject *obj = &s->objects[s->active_objects[i]];
            if (obj->is_removed || !(obj->lifecycle_flags & ARM_LIFECYCLE_UPDATE)) continue;
            for (uint8_t t = 0; t < obj->trait_count; t++) {
                if (obj->traits[t].on_update) obj->traits[t].on_update(obj, dt, obj->traits[t].data);
            }
        }
    }
}

void scene_on_late_update(SceneId id, float dt)
{
    ArmScene *s = &g_scenes[id];

    // Scene-level traits
    for (uint8_t i = 0; i < s->trait_count; i++) {
        if (s->traits[i].on_late_update) s->traits[i].on_late_update(s, dt, s->traits[i].data);
    }

    // Cameras
    if (s->cameras) {
        for (uint8_t i = 0; i < s->camera_count; i++) {
            if (!(s->cameras[i].lifecycle_flags & ARM_LIFECYCLE_LATE_UPDATE)) continue;
            for (uint8_t t = 0; t < s->cameras[i].trait_count; t++) {
                if (s->cameras[i].traits[t].on_late_update) s->cameras[i].traits[t].on_late_update(&s->cameras[i], dt, s->cameras[i].traits[t].data);
            }
        }
    }

    // Lights
    if (s->lights) {
        for (uint8_t i = 0; i < s->light_count; i++) {
            if (!(s->lights[i].lifecycle_flags & ARM_LIFECYCLE_LATE_UPDATE)) continue;
            for (uint8_t t = 0; t < s->lights[i].trait_count; t++) {
                if (s->lights[i].traits[t].on_late_update) s->lights[i].traits[t].on_late_update(&s->lights[i], dt, s->lights[i].traits[t].data);
            }
        }
    }

    // Objects - use active list
    if (s->active_objects && s->active_trait_count > 0) {
        for (uint16_t i = 0; i < s->active_trait_count; i++) {
            ArmObject *obj = &s->objects[s->active_objects[i]];
            if (obj->is_removed || !(obj->lifecycle_flags & ARM_LIFECYCLE_LATE_UPDATE)) continue;
            for (uint8_t t = 0; t < obj->trait_count; t++) {
                if (obj->traits[t].on_late_update) obj->traits[t].on_late_update(obj, dt, obj->traits[t].data);
            }
        }
    }
}

void scene_on_render2d(SceneId id)
{
    ArmScene *s = &g_scenes[id];

    // Scene-level traits
    for (uint8_t i = 0; i < s->trait_count; i++) {
        if (s->traits[i].on_render2d) s->traits[i].on_render2d(s, s->traits[i].data);
    }

    // Cameras
    if (s->cameras) {
        for (uint8_t i = 0; i < s->camera_count; i++) {
            if (!(s->cameras[i].lifecycle_flags & ARM_LIFECYCLE_RENDER2D)) continue;
            for (uint8_t t = 0; t < s->cameras[i].trait_count; t++) {
                if (s->cameras[i].traits[t].on_render2d) s->cameras[i].traits[t].on_render2d(&s->cameras[i], s->cameras[i].traits[t].data);
            }
        }
    }

    // Lights
    if (s->lights) {
        for (uint8_t i = 0; i < s->light_count; i++) {
            if (!(s->lights[i].lifecycle_flags & ARM_LIFECYCLE_RENDER2D)) continue;
            for (uint8_t t = 0; t < s->lights[i].trait_count; t++) {
                if (s->lights[i].traits[t].on_render2d) s->lights[i].traits[t].on_render2d(&s->lights[i], s->lights[i].traits[t].data);
            }
        }
    }

    // Objects - use render2d_objects list for O(render2d) instead of O(total)
    for (uint16_t i = 0; i < s->render2d_count; i++) {
        ArmObject *obj = &s->objects[s->render2d_objects[i]];
        if (obj->is_removed) continue;
        for (uint8_t t = 0; t < obj->trait_count; t++) {
            if (obj->traits[t].on_render2d) obj->traits[t].on_render2d(obj, obj->traits[t].data);
        }
    }
}

void scene_on_remove(SceneId id)
{
    ArmScene *s = &g_scenes[id];

    for (uint8_t i = 0; i < s->trait_count; i++) {
        if (s->traits[i].on_remove) s->traits[i].on_remove(s, s->traits[i].data);
    }
    if (s->cameras) {
        for (uint8_t i = 0; i < s->camera_count; i++) {
            for (uint8_t t = 0; t < s->cameras[i].trait_count; t++) {
                if (s->cameras[i].traits[t].on_remove) s->cameras[i].traits[t].on_remove(&s->cameras[i], s->cameras[i].traits[t].data);
            }
        }
    }
    if (s->lights) {
        for (uint8_t i = 0; i < s->light_count; i++) {
            for (uint8_t t = 0; t < s->lights[i].trait_count; t++) {
                if (s->lights[i].traits[t].on_remove) s->lights[i].traits[t].on_remove(&s->lights[i], s->lights[i].traits[t].data);
            }
        }
    }
    if (s->objects) {
        for (uint16_t i = 0; i < s->object_count; i++) {
            if (s->objects[i].is_removed) continue;  // Its on_remove ran when it was removed
            for (uint8_t t = 0; t < s->objects[i].trait_count; t++) {
                if (s->objects[i].traits[t].on_remove) s->objects[i].traits[t].on_remove(&s->objects[i], s->objects[i].traits[t].data);
            }
        }
    }
}

void scene_clear(SceneId id)
{
    if (id >= SCENE_COUNT) return;

    ArmScene *s = &g_scenes[id];
    scene_on_remove(id);

    // The objects' animations and matrices (the traits and their data live in
    // the shared storage: the next scene overwrites them)
    for (uint16_t i = 0; i < s->object_count; i++) {
        ArmObject *obj = &s->objects[i];
        obj->rigid_body = NULL;
        if (obj->animation) { animation_destroy(obj->animation); free(obj->animation); obj->animation = NULL; }
        if (obj->model_mat) { free_uncached(obj->model_mat); obj->model_mat = NULL; }
        obj->dpl = NULL;
    }

    memset(s, 0, sizeof(*s));
    s->world.clear_color[3] = 0xFF;
    s->world.ambient_color[3] = 0xFF;
}

ArmScene *scene_get(SceneId id)
{
    if (id >= SCENE_COUNT) return NULL;
    return &g_scenes[id];
}

SceneId scene_get_id_by_name(const char *name)
{
    if (name == NULL) return SCENE_COUNT;
    for (int i = 0; i < SCENE_COUNT; i++) {
        if (strcmp(g_scene_names[i], name) == 0) return (SceneId)i;
    }
    return SCENE_COUNT; // Not found
}

const char *scene_get_name(SceneId id)
{
    return id < SCENE_COUNT ? g_scene_names[id] : NULL;
}

SceneId scene_get_current_id(void) { return g_current_scene; }
SceneId scene_get_pending(void) { return g_pending_scene; }
void scene_switch_to(SceneId id) { g_pending_scene = id; }
void scene_clear_pending(void) { g_pending_scene = SCENE_COUNT; }

uint16_t scene_get_group_count(const char *group_name)
{
    if (g_current_scene >= SCENE_COUNT || group_name == NULL) return 0;

    const SceneGroups *groups = &g_scene_groups[g_current_scene];
    for (uint16_t i = 0; i < groups->count; i++) {
        if (strcmp(group_name, groups->entries[i].name) == 0) return groups->entries[i].count;
    }
    return 0;
}