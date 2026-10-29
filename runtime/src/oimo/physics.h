// physics.h - Mini-engine physics wrapper
#pragma once

#include "../types.h"
#include "../limits.h"
#include "oimo.h"

#ifdef __cplusplus
extern "C" {
#endif


// Physics body parameters (matches Blender's rigid body settings)
typedef struct PhysicsBodyParams {
    float mass;
    float friction;
    float restitution;
    float linear_damping;
    float angular_damping;
    float angular_friction;      // Rotation inertia (0.1 default)
    float angular_factor[3];     // Rotation factor, Oimo axes (lock rotation: 0)
    float sleep_linear_velocity; // Sleeping thresholds (with use_deactivation)
    float sleep_angular_velocity;
    int collision_group;         // Collision group bitmask
    int collision_mask;          // Collision mask bitmask
    bool animated;               // If true, use KINEMATIC type
    bool trigger;                // If true, shape is sensor only
    bool use_deactivation;       // Sleep with the thresholds above (else Oimo's)
    bool interpolate;            // The object shows the body between physics steps
} PhysicsBodyParams;

// Default physics parameters (matches Blender defaults)
static inline PhysicsBodyParams physics_body_params_default(void) {
    PhysicsBodyParams p;
    p.mass = 1.0f;
    p.friction = 0.5f;
    p.restitution = 0.0f;
    p.linear_damping = 0.04f;
    p.angular_damping = 0.1f;
    p.angular_friction = 0.1f;
    p.angular_factor[0] = p.angular_factor[1] = p.angular_factor[2] = 1.0f;
    p.sleep_linear_velocity = OIMO_SLEEPING_VELOCITY_THRESHOLD;
    p.sleep_angular_velocity = OIMO_SLEEPING_ANGULAR_VELOCITY_THRESHOLD;
    p.collision_group = 1;
    p.collision_mask = 1;
    p.animated = false;
    p.trigger = false;
    p.use_deactivation = true;
    p.interpolate = false;
    return p;
}

// World management
void physics_init(void);
void physics_shutdown(void);
void physics_reset(void);
void physics_step(float dt);

#ifdef ARM_DEBUG_HUD
// This frame's physics (debug HUD): world step time (ms, smoothed), steps, the
// broadphase's pair tests and the contacts of the last step. Resets the frame
void physics_debug_frame(float *ms, int *steps, int *tests, int *contacts);
#endif
void physics_pause(void);
void physics_resume(void);
OimoWorld* physics_get_world(void);

// Gravity
void physics_set_gravity(float x, float y, float z);
void physics_set_time_scale(float scale);  // Blender's rigid body world time scale
float physics_get_time_scale(void);
OimoVec3 physics_get_gravity(void);

// Body creation with full parameters
bool physics_create_box_full(ArmObject* obj, int type, float hx, float hy, float hz,
                             const PhysicsBodyParams* params);
bool physics_create_sphere_full(ArmObject* obj, int type, float radius,
                                const PhysicsBodyParams* params);
bool physics_create_capsule_full(ArmObject* obj, int type, float radius, float half_height,
                                 const PhysicsBodyParams* params);

// Extended versions with collision group/mask (legacy compatibility)
bool physics_create_box_ex(ArmObject* obj, int type, float hx, float hy, float hz,
                           float mass, float friction, float restitution,
                           int collision_group, int collision_mask);
bool physics_create_sphere_ex(ArmObject* obj, int type, float radius,
                              float mass, float friction, float restitution,
                              int collision_group, int collision_mask);
bool physics_create_capsule_ex(ArmObject* obj, int type, float radius, float half_height,
                               float mass, float friction, float restitution,
                               int collision_group, int collision_mask);

// Static mesh collider (always static body type)
// vertices: array of Vec3 positions
// indices: array of triangle indices (3 per triangle)
// vertex_count: number of vertices
// index_count: number of indices (must be multiple of 3)
bool physics_create_mesh(ArmObject* obj,
                         const OimoVec3* vertices, const uint16_t* indices,
                         int vertex_count, int index_count,
                         float friction, float restitution,
                         int collision_group, int collision_mask);

// Static mesh with full parameters
bool physics_create_mesh_full(ArmObject* obj,
                              const OimoVec3* vertices, const uint16_t* indices,
                              int vertex_count, int index_count,
                              const PhysicsBodyParams* params);

// A rigid body as the exporter writes it (ArmSceneDef.bodies)
typedef enum { PHYSICS_SHAPE_BOX, PHYSICS_SHAPE_SPHERE, PHYSICS_SHAPE_CAPSULE, PHYSICS_SHAPE_MESH } PhysicsShape;

typedef struct PhysicsBodyDef {
    uint16_t object;              // Index in the scene's objects
    int8_t type;                  // OIMO_RIGID_BODY_*
    uint8_t shape;                // PhysicsShape
    float size[3];                // Box: half extents; sphere: radius; capsule: radius, half height
    const OimoVec3* mesh_vertices;  // Static mesh collider
    const uint16_t* mesh_indices;
    uint16_t mesh_vertex_count;
    uint16_t mesh_index_count;
    PhysicsBodyParams params;
} PhysicsBodyDef;

// The body a scene definition describes, on its object
bool physics_create_from_def(ArmObject* obj, const PhysicsBodyDef* def);

// Convenience wrappers with default collision group=1, mask=1
static inline bool physics_create_box(ArmObject* obj, int type, float hx, float hy, float hz,
                                      float mass, float friction, float restitution) {
    return physics_create_box_ex(obj, type, hx, hy, hz, mass, friction, restitution, 1, 1);
}
static inline bool physics_create_sphere(ArmObject* obj, int type, float radius,
                                         float mass, float friction, float restitution) {
    return physics_create_sphere_ex(obj, type, radius, mass, friction, restitution, 1, 1);
}
static inline bool physics_create_capsule(ArmObject* obj, int type, float radius, float half_height,
                                          float mass, float friction, float restitution) {
    return physics_create_capsule_ex(obj, type, radius, half_height, mass, friction, restitution, 1, 1);
}

void physics_remove_body(ArmObject* obj);

// Transform sync (call after physics_step)
// Once per frame: the body's transform to its object, as oimo_module's RigidBody.update
// (alpha: Time.fixedStepInterpolation; parent_world_pos: NULL for roots)
void physics_sync_object(ArmObject* obj, const OimoVec3* parent_world_pos, float alpha);

// Contact/collision info
typedef struct PhysicsContactPair {
    ArmObject* obj_a;
    ArmObject* obj_b;
    OimoVec3 pos_a;
    OimoVec3 pos_b;
    OimoVec3 normal;
    float impulse;
} PhysicsContactPair;

// Callback for contact events
typedef void (*PhysicsContactCallback)(const PhysicsContactPair* contact, void* user_data);

// Set contact callback (called for each active contact after physics_step)
void physics_set_contact_callback(PhysicsContactCallback callback, void* user_data);

// Get contacts for a specific rigid body (returns count, fills contacts array up to max_contacts)
int physics_get_contacts(OimoRigidBody* rb, PhysicsContactPair* contacts, int max_contacts);

// Damping setters
static inline void physics_set_linear_damping(OimoRigidBody* rb, float damping) {
    if (rb) rb->_linearDamping = damping;
}

static inline void physics_set_angular_damping(OimoRigidBody* rb, float damping) {
    if (rb) rb->_angularDamping = damping;
}

static inline float physics_get_linear_damping(const OimoRigidBody* rb) {
    return rb ? rb->_linearDamping : 0.0f;
}

static inline float physics_get_angular_damping(const OimoRigidBody* rb) {
    return rb ? rb->_angularDamping : 0.0f;
}

// Trigger (sensor) mode
static inline void physics_set_trigger(OimoRigidBody* rb, int is_trigger) {
    if (rb && rb->_shapeList) {
        rb->_shapeList->_isTrigger = is_trigger;
    }
}

static inline int physics_is_trigger(const OimoRigidBody* rb) {
    return (rb && rb->_shapeList) ? rb->_shapeList->_isTrigger : 0;
}

// Gravity scale
static inline void physics_set_gravity_scale(OimoRigidBody* rb, float scale) {
    if (rb) rb->_gravityScale = scale;
}

// Rigid body helpers
static inline void physics_apply_force(OimoRigidBody* rb, const OimoVec3* force) {
    if (rb) oimo_rigid_body_apply_force_to_center(rb, force);
}

static inline void physics_apply_impulse(OimoRigidBody* rb, const OimoVec3* impulse) {
    if (rb) {
        OimoVec3 pos = oimo_rigid_body_get_position(rb);
        oimo_rigid_body_apply_impulse(rb, impulse, &pos);
    }
}

static inline void physics_set_linear_velocity(OimoRigidBody* rb, const OimoVec3* vel) {
    if (rb) oimo_rigid_body_set_linear_velocity(rb, vel);
}

static inline OimoVec3 physics_get_linear_velocity(const OimoRigidBody* rb) {
    return rb ? oimo_rigid_body_get_linear_velocity(rb) : oimo_vec3_zero();
}

static inline void physics_set_angular_velocity(OimoRigidBody* rb, const OimoVec3* vel) {
    if (rb) oimo_rigid_body_set_angular_velocity(rb, vel);
}

static inline OimoVec3 physics_get_angular_velocity(const OimoRigidBody* rb) {
    return rb ? oimo_rigid_body_get_angular_velocity(rb) : oimo_vec3_zero();
}

static inline void physics_wake_up(OimoRigidBody* rb) {
    if (rb) oimo_rigid_body_wake_up(rb);
}

static inline void physics_disable_sleep(OimoRigidBody* rb) {
    if (rb) rb->_autoSleep = 0;
}

static inline void physics_set_mass(OimoRigidBody* rb, float mass) {
    if (rb) {
        rb->_mass = mass;
        oimo_rigid_body_update_mass(rb);
    }
}

#ifdef __cplusplus
}
#endif
