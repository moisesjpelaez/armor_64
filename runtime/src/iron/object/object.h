// object.h - Object lifecycle management
#pragma once

#include "../../types.h"
#include "../../data/scenes.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Remove an object from the scene.
 * - Queues the object for deferred removal (safe during physics callbacks)
 * - Immediately hides the object
 * - Actual cleanup happens in object_process_removals()
 *
 * Safe to call multiple times. Memory cleanup happens during scene_clear().
 */
void object_remove(ArmObject* obj);

/**
 * Process all queued object removals: their physics bodies leave the world (with
 * the contact subscriptions on them). Called after each contact dispatch, once per
 * frame after the updates, and at a scene switch.
 */
void object_process_removals(void);

/**
 * obj.getTrait(RigidBody): a RigidBody is its object's pointer on N64 (the body
 * is obj->rigid_body), or NULL when the object has none.
 */
static inline ArmObject* arm_rigid_body_of(ArmObject* obj) {
    return (obj && obj->rigid_body) ? obj : NULL;
}

/**
 * Get the parent of an object, or NULL if it has no parent.
 * Uses the scene's flat object array + parent_index.
 */
static inline ArmObject* object_get_parent(ArmObject* obj, ArmScene* scene) {
    if (!obj || obj->parent_index < 0 || obj->parent_index >= scene->object_count)
        return NULL;
    return &scene->objects[obj->parent_index];
}

// Future: light_remove, camera_remove

#ifdef __cplusplus
}
#endif
