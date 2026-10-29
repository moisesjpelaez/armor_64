// Scenes: ids, lifecycle dispatch, switching. Scene ids: scenes_defs.h (generated).
#pragma once

#include "../types.h"

#ifdef __cplusplus
extern "C" {
#endif

// SceneId (SCENE_<name>, SCENE_COUNT), each scene's definition (scene_def_<name>),
// and the shared storage's sizes (SCENE_MAX_*: the largest scene's)
#include "scenes_defs.h"

// The current scene's objects, cameras and lights (trait data points into them)
extern ArmObject g_scene_objects[SCENE_MAX_OBJECTS];
extern ArmCamera g_scene_cameras[SCENE_MAX_CAMERAS];
extern ArmLight g_scene_lights[SCENE_MAX_LIGHTS];

void scene_init(SceneId id);
const bool *scene_models(SceneId id);  // The models it uses, indexed by ModelId
void scene_on_ready(SceneId id);
void scene_on_fixed_update(SceneId id, float dt);
void scene_on_update(SceneId id, float dt);
void scene_on_late_update(SceneId id, float dt);
void scene_on_render2d(SceneId id);
void scene_on_remove(SceneId id);
void scene_clear(SceneId id);

void scene_sync_physics(SceneId id, float alpha);

void scene_switch_to(SceneId id);
SceneId scene_get_id_by_name(const char *name);
const char *scene_get_name(SceneId id);
SceneId scene_get_current_id(void);
SceneId scene_get_pending(void);
void scene_clear_pending(void);

ArmScene *scene_get(SceneId id);

// Get count of objects in a named group for the current scene
// Returns 0 if group not found
uint16_t scene_get_group_count(const char *group_name);

#ifdef __cplusplus
}
#endif