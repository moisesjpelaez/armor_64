/**
 * The runtime's core types: objects, cameras, lights, scenes, traits, signals.
 *
 * Units: positions are in Blender units (BU), Y-up (Blender's x, y, z is x, z, -y
 * here). tiny3d stores models at MODEL_UNITS_PER_BU (64) model units per BU; the
 * renderer scales them when it draws.
 *
 * Haxe to C: Vec2/Vec3/Vec4 -> ArmVec2/ArmVec3/ArmVec4 (values), Object ->
 * ArmObject*, Transform -> ArmTransform (inside ArmObject), Camera/Light ->
 * ArmCamera* and ArmLight*, Float/Int/Bool -> float/int32_t/bool, String -> const char*.
 *
 * Capacities (objects, bodies, contacts, signal subscribers...) are in limits.h,
 * computed for each project by exporter/codegen/limits.py.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include "limits.h"
#include "engine.h"
#include <rspq.h>
#include <t3d/t3dmath.h>
#include <t3d/t3dmodel.h>

#ifdef __cplusplus
extern "C" {
#endif

// Forward declaration — full definition in iron/object/animation.h
typedef struct ArmAnimation ArmAnimation;

#define FB_COUNT 3

// A changed transform is written into each framebuffer's matrix slot, one per frame
// (the RSP may still read the other slots for the frames in flight)
#define TRANSFORM_DIRTY_FRAMES FB_COUNT

// The renderer works in model units: tiny3d's converter stores 1 Blender unit (BU)
// as 64 (gltf_to_t3d --base-scale). Positions are kept in BU and scaled when drawn.
#define MODEL_UNITS_PER_BU 64.0f
#define BU_PER_MODEL_UNIT 0.015625f

struct OimoRigidBody;

typedef struct { float x, y; } ArmVec2;
typedef struct { float x, y, z; } ArmVec3;
typedef struct { float x, y, z, w; } ArmVec4;

typedef void (*ArmTraitAddFn)(void *entity, void *data);
typedef void (*ArmTraitReadyFn)(void *entity, void *data);
typedef void (*ArmTraitFixedUpdateFn)(void *entity, float dt, void *data);
typedef void (*ArmTraitUpdateFn)(void *entity, float dt, void *data);
typedef void (*ArmTraitLateUpdateFn)(void *entity, float dt, void *data);
typedef void (*ArmTraitRemoveFn)(void *entity, void *data);
typedef void (*ArmTraitRender2DFn)(void *entity, void *data);

// Generic callback typedefs - no closure capture, raw Haxe semantics
typedef void (*ArmVoidCallback)(void);
typedef void (*ArmFloatCallback)(float);

// Callback typedefs with obj/data parameters for trait method callbacks
// These are used when passing trait methods as callbacks (e.g., fadeOut(initTransitionDone))
typedef void (*ArmCallback)(void *obj, void *data);
typedef void (*ArmFloatObjCallback)(float value, void *obj, void *data);

typedef struct {
    ArmTraitAddFn on_add;
    ArmTraitReadyFn on_ready;
    ArmTraitFixedUpdateFn on_fixed_update;
    ArmTraitUpdateFn on_update;
    ArmTraitLateUpdateFn on_late_update;
    ArmTraitRemoveFn on_remove;
    ArmTraitRender2DFn on_render2d;
    void *data;
} ArmTrait;

// =============================================================================
// Signal System - Per-instance event dispatch
// =============================================================================

// Haxe string equality: compares the text, and a null string only equals null
static inline bool arm_string_equals(const char *a, const char *b) {
    if (a == b) return true;
    return a != NULL && b != NULL && strcmp(a, b) == 0;
}

/**
 * Signal handler function type.
 * Called when a signal is emitted with: (context, payload)
 * The payload of a signal with arguments points to its argument struct
 * (<signal>_payload_t in data/signals.h), built by the emitter for the call.
 */
typedef void (*ArmSignalHandler)(void *ctx, void *payload);

/**
 * Signal subscriber entry - keeps handler + context together for cache efficiency
 */
typedef struct {
    ArmSignalHandler handler;
    void *ctx;  // Handler's context (passed as first arg)
} ArmSignalEntry;

/**
 * ArmSignal - Per-instance signal with up to ARM_SIGNAL_MAX_SUBS subscribers.
 * Uses array-of-structs (AoS) for better cache locality on N64.
 *
 * Usage in traits:
 *   - Declare: var onDeath:Signal; (Haxe) → ArmSignal onDeath; (C struct member)
 *   - Connect: onDeath.connect(callback) → signal_connect(&data->onDeath, handler, data)
 *   - Emit: onDeath.emit(damage) → { payload_t _p = {damage}; signal_emit(&data->onDeath, &_p); }
 *   - Disconnect: onDeath.disconnect(callback) → signal_disconnect(&data->onDeath, handler, data)
 */
typedef struct {
    uint8_t count;
    uint8_t emitting;  // Nested emits in progress: removals are deferred meanwhile
    uint8_t removed;   // Entries cleared during an emit, compacted after it
    ArmSignalEntry entries[ARM_SIGNAL_MAX_SUBS];
} ArmSignal;

// Signal functions (implemented in signal.c)
void signal_connect(ArmSignal *signal, ArmSignalHandler handler, void *ctx);
void signal_disconnect(ArmSignal *signal, ArmSignalHandler handler, void *ctx);
bool signal_is_connected(const ArmSignal *signal, ArmSignalHandler handler, void *ctx);
void signal_emit(ArmSignal *signal, void *payload);
void signal_clear(ArmSignal *signal);

typedef struct {
    T3DVec3 loc;
    T3DQuat rot;
    T3DVec3 scale;
    uint8_t dirty;
    uint8_t parented;  // An object with a parent: local loc x64, scale without the factor (transform.h)
} ArmTransform;

/**
 * ArmObject - Core entity type
 *
 * Field layout optimized for cache:
 *   - Hot data first (accessed every frame): transform, visibility, matrices
 *   - Cold data after (accessed occasionally): bounds, physics, traits
 *
 * The world matrix and cached world bounds are rebuilt only in the frame the
 * transform changes; the next frames copy the matrix into their own slot.
 */
typedef struct ArmObject {
    // === HOT DATA (accessed every frame) ===
    ArmTransform transform;       // 44 bytes: position, rotation, scale (world-space for roots, local-space for parented objects)
    T3DMat4FP *model_mat;         // 4 bytes: FB_COUNT world matrices for rendering, one per framebuffer (RSP reads via DMA)
    T3DMat4 world_mat;            // 64 bytes: float world matrix (used for parent/child hierarchy composition)
    rspq_block_t *dpl;            // 4 bytes: display list pointer
    int16_t parent_index;         // 2 bytes: index into scene->objects, -1 = root (no parent)
    bool visible;                 // 1 byte
    bool matrix_valid;            // 1 byte: world_mat fits tiny3d's fixed point (within ±511 BU); not drawn otherwise
    bool is_removed;              // 1 byte: object marked for removal
    uint8_t trait_count;          // 1 byte

    // === CACHED BOUNDS (updated when dirty) ===
    T3DVec3 cached_world_aabb_min; // 12 bytes: world-space AABB min for culling
    T3DVec3 cached_world_aabb_max; // 12 bytes: world-space AABB max for culling

    // === COLD DATA (accessed occasionally) ===
    T3DVec3 bounds_center;        // 12 bytes: local AABB center, in model units (BU × 64)
    T3DVec3 bounds_half;          // 12 bytes: local AABB half size, in model units
    struct OimoRigidBody *rigid_body;  // 4 bytes
    ArmAnimation *animation;      // 4 bytes: skeletal animation (NULL = static mesh)
    ArmTrait *traits;             // 4 bytes

    // === LIFECYCLE FLAGS (ARM_LIFECYCLE_*: which callbacks the traits have) ===
    uint8_t lifecycle_flags;      // 1 byte
} ArmObject;

// Lifecycle flag bits
#define ARM_LIFECYCLE_FIXED_UPDATE (1 << 0)
#define ARM_LIFECYCLE_UPDATE       (1 << 1)
#define ARM_LIFECYCLE_LATE_UPDATE  (1 << 2)
#define ARM_LIFECYCLE_RENDER2D     (1 << 3)

typedef struct ArmLight {
    ArmTransform transform;
    uint8_t color[4];
    T3DVec3 dir;
    uint8_t trait_count;
    uint8_t lifecycle_flags;
    ArmTrait *traits;
} ArmLight;

typedef struct ArmCamera {
    ArmTransform transform;
    T3DVec3 target;               // World-space look-at target (updated per-frame)
    T3DVec3 look_dir;             // target - position: the camera keeps its rotation when moved
    float fov;
    float near;
    float far;

    // Parent hierarchy (camera follows parent's world transform)
    T3DMat4 *parent_world_mat;    // NULL = no parent; else points to parent ArmObject.world_mat
    T3DVec3 local_pos;            // Camera position relative to parent
    T3DVec3 local_target;         // Look-at target relative to parent

    uint8_t trait_count;
    uint8_t lifecycle_flags;
    ArmTrait *traits;
} ArmCamera;

typedef struct {
    uint8_t clear_color[4];
    uint8_t ambient_color[4];
} ArmWorld;

typedef struct ArmScene {
    ArmWorld world;

    uint8_t camera_count;
    ArmCamera *cameras;
    uint8_t active_camera_id;

    uint8_t light_count;
    ArmLight *lights;

    uint16_t object_count;
    ArmObject *objects;

    // Active object tracking - indices of objects that need lifecycle updates
    // Avoids iterating entire object array when most objects have no traits
    uint16_t *active_objects;     // Array of object indices with active traits
    uint16_t active_trait_count;  // Count of objects with traits

    // Physics objects - indices of objects with rigid bodies
    // Avoids iterating all objects for physics sync
    uint16_t *physics_objects;    // Array of object indices with physics bodies
    uint16_t physics_count;       // Count of objects with physics

    // Animated objects - indices of objects with skeletal animation
    // Avoids iterating all objects for animation update
    uint16_t *animated_objects;   // Array of object indices with animation
    uint16_t animated_count;      // Count of animated objects

    // Render2D objects - indices of objects with on_render2d callbacks
    uint16_t *render2d_objects;   // Array of object indices with render2d
    uint16_t render2d_count;      // Count of render2d objects

    // Scene flags
    bool has_parented_objects;    // True if any object has parent_index >= 0

    uint8_t trait_count;
    ArmTrait *traits;
} ArmScene;

// =============================================================================
// Scene definitions: what the exporter writes for each Blender scene (constant
// tables in scenes/<scene>.c). scene_init builds the scene from them, in the
// storage the scenes share (only one exists at a time).
// =============================================================================

struct PhysicsBodyDef;

typedef struct {
    T3DVec3 loc;                  // Local to the parent for parented objects
    T3DQuat rot;
    T3DVec3 scale;
    T3DVec3 bounds_center;        // Model units
    T3DVec3 bounds_half;
    int16_t model;                // ModelId; -1: an empty (no mesh)
    int16_t parent_index;         // -1: none
    uint16_t first_trait;         // In the scene's traits
    uint8_t trait_count;
    uint8_t lifecycle_flags;
    bool visible;
    bool skinned;
} ArmObjectDef;

typedef struct {
    T3DVec3 loc;
    T3DVec3 target;
    float fov, near, far;
    int16_t parent_index;         // An object, -1: none
    T3DVec3 local_pos;            // Relative to the parent
    T3DVec3 local_target;
    uint16_t first_trait;
    uint8_t trait_count;
    uint8_t lifecycle_flags;
} ArmCameraDef;

typedef struct {
    uint8_t color[3];
    T3DVec3 dir;
    uint16_t first_trait;
    uint8_t trait_count;
    uint8_t lifecycle_flags;
} ArmLightDef;

typedef struct {
    uint8_t clear_color[3];
    uint8_t ambient_color[3];
    const ArmCameraDef *cameras;
    uint8_t camera_count;
    const ArmLightDef *lights;
    uint8_t light_count;
    const ArmObjectDef *objects;
    uint16_t object_count;
    // Every trait of the scene: the objects', cameras', lights' and the scene's
    // own (scene_traits_first..), each with its data pointer
    const ArmTrait *traits;
    uint16_t trait_count;
    uint16_t scene_traits_first;
    uint8_t scene_trait_count;
    void (*init_trait_data)(void);  // The traits' initial data (their members' initial values)
    bool has_parented_objects;
    // Physics: the bodies (in object order), the world's gravity and time scale
    const struct PhysicsBodyDef *bodies;
    uint16_t body_count;
    float gravity[3];
    float physics_time_scale;
} ArmSceneDef;

#ifdef __cplusplus
}
#endif