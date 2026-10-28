// physics.c - Mini-engine physics implementation
#include "physics.h"
#include "../limits.h"
#include "../events/physics_events.h"
#include "../engine.h"
#include "../iron/object/object.h"
#if ENGINE_ENABLE_PHYSICS_DEBUG
#include "debug/physics_debug.h"
#endif
#include "../utils.h"
#include "collision/geometry/static_mesh_geometry.h"
#include <t3d/t3d.h>
#include <string.h>


// Static pools
static OimoWorld g_world;
static bool g_paused = false;
static bool g_initialized = false;

// Contact callback
static PhysicsContactCallback g_contact_callback = NULL;
static void* g_contact_user_data = NULL;

static OimoRigidBody g_bodies[MAX_PHYSICS_BODIES];
// Interpolated bodies: their transform after the last two steps (oimo_module's
// prevPos/currentPos)
typedef struct {
    bool on;
    OimoVec3 prev_pos, cur_pos;
    OimoQuat prev_rot, cur_rot;
} BodyInterpolation;
static BodyInterpolation g_interpolation[MAX_PHYSICS_BODIES];
static OimoShape g_shapes[MAX_PHYSICS_BODIES];

typedef union {
    OimoSphereGeometry sphere;
    OimoBoxGeometry box;
    OimoCapsuleGeometry capsule;
} GeometryUnion;
static GeometryUnion g_geometries[MAX_PHYSICS_BODIES];

// Free list for O(1) body allocation/deallocation
static int16_t g_free_list[MAX_PHYSICS_BODIES];
static int16_t g_free_count = 0;

// Mesh collider storage (separate due to dynamic allocation)
static OimoStaticMeshGeometry g_mesh_geometries[MAX_MESH_COLLIDERS];
static OimoRigidBody g_mesh_bodies[MAX_MESH_COLLIDERS];
static OimoShape g_mesh_shapes[MAX_MESH_COLLIDERS];
static bool g_mesh_used[MAX_MESH_COLLIDERS];

// Pool helpers - O(1) free list implementation
static OimoRigidBody* alloc_body(void) {
    if (g_free_count == 0) {
        ARM_LIMIT_REACHED(MAX_PHYSICS_BODIES);
        return NULL;
    }
    int16_t idx = g_free_list[--g_free_count];
    return &g_bodies[idx];
}

static void free_body(OimoRigidBody* rb) {
    int idx = (int)(rb - g_bodies);
    if (idx >= 0 && idx < MAX_PHYSICS_BODIES) {
        g_free_list[g_free_count++] = (int16_t)idx;
        g_interpolation[idx].on = false;
    }
}

static int get_body_index(OimoRigidBody* rb) {
    return (int)(rb - g_bodies);
}

// World management
void physics_init(void) {
    if (g_initialized) return;

    memset(g_bodies, 0, sizeof(g_bodies));
    memset(g_shapes, 0, sizeof(g_shapes));
    memset(g_geometries, 0, sizeof(g_geometries));

    // Initialize free list - all bodies available, in reverse order for LIFO
    g_free_count = MAX_PHYSICS_BODIES;
    for (int i = 0; i < MAX_PHYSICS_BODIES; i++) {
        g_free_list[i] = (int16_t)(MAX_PHYSICS_BODIES - 1 - i);
    }

    // Mesh colliders
    memset(g_mesh_geometries, 0, sizeof(g_mesh_geometries));
    memset(g_mesh_bodies, 0, sizeof(g_mesh_bodies));
    memset(g_mesh_shapes, 0, sizeof(g_mesh_shapes));
    memset(g_mesh_used, 0, sizeof(g_mesh_used));

    // Contact event subscriptions
    physics_contact_init();

    OimoVec3 gravity = oimo_vec3(0.0f, -9.80665f, 0.0f);
    oimo_world_init(&g_world, &gravity);

    g_paused = false;
    g_initialized = true;
}

void physics_shutdown(void) {
    if (!g_initialized) return;
    physics_reset();
    g_initialized = false;
}

void physics_reset(void) {
    if (!g_initialized) return;

    // Clear contact subscriptions
    physics_contact_clear();

    // Free dynamically allocated mesh geometry data before zeroing pools.
    // This is the only resource that requires explicit freeing - everything
    // else (broadphase proxies, contacts, linked lists) lives in static
    // pools that get wiped by oimo_world_init below.
    for (int i = 0; i < MAX_MESH_COLLIDERS; i++) {
        if (g_mesh_used[i]) {
            oimo_static_mesh_geometry_free(&g_mesh_geometries[i]);
        }
    }

    // Nuclear reset: zero all body/shape/geometry pools.
    // This safely clears all stale pointers (_next, _prev, _contactLinkList,
    // _shapeList, _world, etc.) without walking the world's linked list or
    // contact graph, which corrupts when bodies have cross-referencing contacts.
    memset(g_bodies, 0, sizeof(g_bodies));
    memset(g_shapes, 0, sizeof(g_shapes));
    memset(g_geometries, 0, sizeof(g_geometries));
    memset(g_interpolation, 0, sizeof(g_interpolation));
    memset(g_mesh_bodies, 0, sizeof(g_mesh_bodies));
    memset(g_mesh_shapes, 0, sizeof(g_mesh_shapes));
    memset(g_mesh_geometries, 0, sizeof(g_mesh_geometries));
    memset(g_mesh_used, 0, sizeof(g_mesh_used));

    // Reinitialize free list - all bodies available
    g_free_count = MAX_PHYSICS_BODIES;
    for (int i = 0; i < MAX_PHYSICS_BODIES; i++) {
        g_free_list[i] = (int16_t)(MAX_PHYSICS_BODIES - 1 - i);
    }

    // Re-initialize world (wipes linked list, contact manager, broadphase)
    OimoVec3 gravity = g_world._gravity;
    oimo_world_init(&g_world, &gravity);
}

#ifdef ARM_DEBUG_HUD
// Time of this frame's physics steps (debug HUD)
static uint32_t g_debug_step_ticks = 0;
static int g_debug_steps = 0;
static float g_debug_ms = 0.0f;

void physics_debug_frame(float *ms, int *steps, int *tests, int *contacts) {
    // Smoothed over frames, for a readable number
    g_debug_ms = g_debug_ms * 0.9f + (TICKS_TO_US(g_debug_step_ticks) / 1000.0f) * 0.1f;
    *ms = g_debug_ms;
    *steps = g_debug_steps;
    *tests = g_world._broadPhase->_testCount;
    *contacts = g_world._contactManager._numContacts;
    g_debug_step_ticks = 0;
    g_debug_steps = 0;
}
#endif

// Kinematic bodies and bodies of animated objects follow their object, as
// oimo_module's syncTransform: the object's world position and its rotation
// (local, as Armory), and the body is woken. Setting the transform only when it
// differs gives the same result (both come from the same conversion).
static bool object_drives_body(const ArmObject* obj, const OimoRigidBody* rb) {
    return rb->_type == OIMO_RIGID_BODY_KINEMATIC || obj->animation != NULL;
}

static void sync_driven_bodies(void) {
    for (OimoRigidBody* rb = g_world._rigidBodyList; rb != NULL; rb = rb->_next) {
        ArmObject* obj = (ArmObject*)rb->userData;
        if (!obj || obj->is_removed || !object_drives_body(obj, rb)) continue;

        OimoVec3 pos;
        if (obj->parent_index >= 0) {
            // World position from the object's world matrix (model units)
            pos = oimo_vec3(obj->world_mat.m[3][0] * BU_PER_MODEL_UNIT,
                            obj->world_mat.m[3][1] * BU_PER_MODEL_UNIT,
                            obj->world_mat.m[3][2] * BU_PER_MODEL_UNIT);
        } else {
            pos = oimo_vec3(obj->transform.loc.x, obj->transform.loc.y, obj->transform.loc.z);
        }
        OimoQuat q = oimo_quat(obj->transform.rot.x, obj->transform.rot.y,
                               obj->transform.rot.z, obj->transform.rot.w);
        OimoMat3 rot = oimo_quat_to_mat3(&q);

        if (memcmp(&pos, &rb->_transform.position, sizeof(pos)) != 0 ||
            memcmp(&rot, &rb->_transform.rotation, sizeof(rot)) != 0) {
            OimoTransform tf = rb->_transform;
            tf.position = pos;
            tf.rotation = rot;
            oimo_rigid_body_set_transform(rb, &tf);
        }
        oimo_rigid_body_wake_up(rb);
    }
}

// After each step, as oimo_module's physicsUpdate: interpolated bodies keep their
// transform of the previous step too
static void record_interpolation(void) {
    for (int i = 0; i < MAX_PHYSICS_BODIES; i++) {
        BodyInterpolation* interp = &g_interpolation[i];
        if (!interp->on) continue;  // Off for free slots (free_body)
        interp->prev_pos = interp->cur_pos;
        interp->prev_rot = interp->cur_rot;
        interp->cur_pos = g_bodies[i]._transform.position;
        interp->cur_rot = oimo_rigid_body_get_orientation(&g_bodies[i]);
    }
}

void physics_step(float dt) {
#ifdef ARM_DEBUG_HUD
    uint32_t debug_start = TICKS_READ();
#endif
    if (!g_initialized || g_paused) return;
    sync_driven_bodies();
    oimo_world_step(&g_world, dt);
    record_interpolation();
#ifdef ARM_DEBUG_HUD
    g_debug_step_ticks += TICKS_DISTANCE(debug_start, TICKS_READ());
    g_debug_steps++;
#endif

    // Dispatch per-body contact subscriptions
    physics_contact_dispatch();

    // Objects removed by contact handlers leave the world before the next step
    // (main.c also processes removals once per frame, after the updates)
    object_process_removals();

    // Process global contact callbacks if registered
    if (g_contact_callback) {
        OimoContact* contact = g_world._contactManager._contactList;
        while (contact != NULL) {
            if (contact->_touching && contact->_manifold._numPoints > 0) {
                OimoManifoldPoint* pt = &contact->_manifold._points[0];
                if (pt->_depth > 0) {
                    PhysicsContactPair pair;
                    pair.obj_a = (ArmObject*)contact->_b1->userData;
                    pair.obj_b = (ArmObject*)contact->_b2->userData;
                    pair.pos_a = pt->_pos1;
                    pair.pos_b = pt->_pos2;
                    pair.normal = contact->_manifold._normal;
                    pair.impulse = pt->_impulse.impulseN;
                    g_contact_callback(&pair, g_contact_user_data);
                }
            }
            contact = contact->_next;
        }
    }
}

void physics_pause(void) { g_paused = true; }
void physics_resume(void) { g_paused = false; }
OimoWorld* physics_get_world(void) { return &g_world; }

// The rigid body world's time scale (Blender), set by each scene
static float g_time_scale = 1.0f;
void physics_set_time_scale(float scale) { g_time_scale = scale; }
float physics_get_time_scale(void) { return g_time_scale; }

// Gravity
void physics_set_gravity(float x, float y, float z) {
    if (!g_initialized) return;
    oimo_world_set_gravity(&g_world, oimo_vec3(x, y, z));
}

OimoVec3 physics_get_gravity(void) {
    return g_initialized ? oimo_world_get_gravity(&g_world) : oimo_vec3_zero();
}

static int resolve_body_type(int requested_type, const PhysicsBodyParams* params) {
    return (params && params->animated) ? OIMO_RIGID_BODY_KINEMATIC : requested_type;
}

typedef struct {
    OimoRigidBody* rb;
    int idx;
    int final_type;
} BodySetupResult;

static bool setup_body_common(ArmObject* obj, int type, const PhysicsBodyParams* params,
                              BodySetupResult* out) {
    if (!g_initialized || !obj || !params) return false;

    OimoRigidBody* rb = alloc_body();
    if (!rb) return false;  // alloc_body() reported the limit

    out->rb = rb;
    out->idx = get_body_index(rb);
    out->final_type = resolve_body_type(type, params);

    OimoRigidBodyConfig config = oimo_rigid_body_config();
    config.type = out->final_type;
    config.position = oimo_vec3(obj->transform.loc.x, obj->transform.loc.y, obj->transform.loc.z);
    OimoQuat q = oimo_quat(obj->transform.rot.x, obj->transform.rot.y,
                           obj->transform.rot.z, obj->transform.rot.w);
    config.rotation = oimo_quat_to_mat3(&q);
    config.linearDamping = params->linear_damping;
    config.angularDamping = params->angular_damping;
    // As oimo_module: bodies always sleep; deactivation only sets the thresholds
    if (params->use_deactivation) {
        config.sleepingVelocityThreshold = params->sleep_linear_velocity;
        config.sleepingAngularVelocityThreshold = params->sleep_angular_velocity;
    }
    oimo_rigid_body_init(rb, &config);
    return true;
}

static void setup_shape_common(OimoShape* shape, OimoGeometry* geom, int final_type,
                               float density, const PhysicsBodyParams* params) {
    OimoShapeConfig shapeConfig = oimo_shape_config();
    shapeConfig.geometry = geom;
    shapeConfig.friction = params->friction;
    shapeConfig.restitution = params->restitution;
    shapeConfig.density = (final_type == OIMO_RIGID_BODY_STATIC) ? 0.0f : density;
    shapeConfig.collisionGroup = params->collision_group;
    shapeConfig.collisionMask = params->collision_mask;
    oimo_shape_init(shape, &shapeConfig);
    shape->_isTrigger = params->trigger;
}

static void finalize_body(OimoRigidBody* rb, ArmObject* obj, int final_type,
                          const PhysicsBodyParams* params) {
    rb->_rotFactor = oimo_vec3(params->angular_factor[0], params->angular_factor[1], params->angular_factor[2]);
    BodyInterpolation* interp = &g_interpolation[get_body_index(rb)];
    interp->on = params->interpolate;
    interp->prev_pos = interp->cur_pos = rb->_transform.position;
    interp->prev_rot = interp->cur_rot = oimo_rigid_body_get_orientation(rb);
    if (final_type == OIMO_RIGID_BODY_DYNAMIC) {
        rb->_mass = params->mass;
        float af = params->angular_friction;
        if (af < 0.001f) af = 0.001f;
        rb->_localInertia = oimo_mat3(af, 0, 0, 0, af, 0, 0, 0, af);
        oimo_rigid_body_complete_mass_data(rb);
    }
    oimo_world_add_rigid_body(&g_world, rb);
    obj->rigid_body = rb;
    rb->userData = obj;
}

bool physics_create_box_full(ArmObject* obj, int type, float hx, float hy, float hz,
                             const PhysicsBodyParams* params) {
    BodySetupResult setup;
    if (!setup_body_common(obj, type, params, &setup)) return false;

    oimo_box_geometry_init3(&g_geometries[setup.idx].box, hx, hy, hz);
    setup_shape_common(&g_shapes[setup.idx], &g_geometries[setup.idx].box.base,
                       setup.final_type, 1.0f, params);
    oimo_rigid_body_add_shape(setup.rb, &g_shapes[setup.idx]);
    finalize_body(setup.rb, obj, setup.final_type, params);
    return true;
}

bool physics_create_sphere_full(ArmObject* obj, int type, float radius,
                                const PhysicsBodyParams* params) {
    BodySetupResult setup;
    if (!setup_body_common(obj, type, params, &setup)) return false;

    oimo_sphere_geometry_init(&g_geometries[setup.idx].sphere, radius);
    setup_shape_common(&g_shapes[setup.idx], &g_geometries[setup.idx].sphere.base,
                       setup.final_type, 1.0f, params);
    oimo_rigid_body_add_shape(setup.rb, &g_shapes[setup.idx]);
    finalize_body(setup.rb, obj, setup.final_type, params);
    return true;
}

bool physics_create_capsule_full(ArmObject* obj, int type, float radius, float half_height,
                                 const PhysicsBodyParams* params) {
    BodySetupResult setup;
    if (!setup_body_common(obj, type, params, &setup)) return false;

    oimo_capsule_geometry_init(&g_geometries[setup.idx].capsule, radius, half_height);
    setup_shape_common(&g_shapes[setup.idx], &g_geometries[setup.idx].capsule.base,
                       setup.final_type, 1.0f, params);
    oimo_rigid_body_add_shape(setup.rb, &g_shapes[setup.idx]);
    finalize_body(setup.rb, obj, setup.final_type, params);
    return true;
}

// Legacy body creation (wraps _full versions)
bool physics_create_box_ex(ArmObject* obj, int type, float hx, float hy, float hz,
                           float mass, float friction, float restitution,
                           int collision_group, int collision_mask) {
    PhysicsBodyParams params = physics_body_params_default();
    params.mass = mass;
    params.friction = friction;
    params.restitution = restitution;
    params.collision_group = collision_group;
    params.collision_mask = collision_mask;
    return physics_create_box_full(obj, type, hx, hy, hz, &params);
}

bool physics_create_sphere_ex(ArmObject* obj, int type, float radius,
                              float mass, float friction, float restitution,
                              int collision_group, int collision_mask) {
    PhysicsBodyParams params = physics_body_params_default();
    params.mass = mass;
    params.friction = friction;
    params.restitution = restitution;
    params.collision_group = collision_group;
    params.collision_mask = collision_mask;
    return physics_create_sphere_full(obj, type, radius, &params);
}

bool physics_create_capsule_ex(ArmObject* obj, int type, float radius, float half_height,
                               float mass, float friction, float restitution,
                               int collision_group, int collision_mask) {
    PhysicsBodyParams params = physics_body_params_default();
    params.mass = mass;
    params.friction = friction;
    params.restitution = restitution;
    params.collision_group = collision_group;
    params.collision_mask = collision_mask;
    return physics_create_capsule_full(obj, type, radius, half_height, &params);
}

void physics_remove_body(ArmObject* obj) {
    if (!g_initialized || !obj || !obj->rigid_body) return;

    OimoRigidBody* rb = obj->rigid_body;
    physics_contact_forget_body(rb);

    // Check if this is a mesh collider body (lives in g_mesh_bodies[])
    ptrdiff_t mesh_idx = rb - g_mesh_bodies;
    if (mesh_idx >= 0 && mesh_idx < MAX_MESH_COLLIDERS && &g_mesh_bodies[mesh_idx] == rb) {
        // Mesh body: remove from world, free geometry, mark slot as unused
        oimo_world_remove_rigid_body(&g_world, rb);
        oimo_static_mesh_geometry_free(&g_mesh_geometries[mesh_idx]);
        g_mesh_used[mesh_idx] = false;
    } else {
        // Primitive body (box/sphere/capsule): remove from world and return to free list
        oimo_world_remove_rigid_body(&g_world, rb);
        free_body(rb);
    }
    obj->rigid_body = NULL;
}

// Static mesh collider (full version with all params)
bool physics_create_mesh_full(ArmObject* obj,
                              const OimoVec3* vertices, const int16_t* indices,
                              int vertex_count, int index_count,
                              const PhysicsBodyParams* params) {
    if (!g_initialized || !obj || !params) return false;

    // Find free mesh slot
    int idx = -1;
    for (int i = 0; i < MAX_MESH_COLLIDERS; i++) {
        if (!g_mesh_used[i]) {
            idx = i;
            break;
        }
    }

    if (idx < 0) {
        ARM_LIMIT_REACHED(MAX_MESH_COLLIDERS);
        return false;
    }

    // Initialize mesh geometry
    if (!oimo_static_mesh_geometry_init(&g_mesh_geometries[idx], vertices, indices, vertex_count, index_count)) {
        debugf("Physics: failed to create mesh geometry\n");
        return false;
    }

    // Body config (always static for mesh colliders)
    OimoRigidBodyConfig config = oimo_rigid_body_config();
    config.type = OIMO_RIGID_BODY_STATIC;
    config.position = oimo_vec3(obj->transform.loc.x, obj->transform.loc.y, obj->transform.loc.z);
    OimoQuat q = oimo_quat(obj->transform.rot.x, obj->transform.rot.y,
                           obj->transform.rot.z, obj->transform.rot.w);
    config.rotation = oimo_quat_to_mat3(&q);
    config.autoSleep = false;
    oimo_rigid_body_init(&g_mesh_bodies[idx], &config);

    // Shape config
    OimoShapeConfig shapeConfig = oimo_shape_config();
    shapeConfig.geometry = &g_mesh_geometries[idx].base;
    shapeConfig.friction = params->friction;
    shapeConfig.restitution = params->restitution;
    shapeConfig.density = 0.0f;  // Static
    shapeConfig.collisionGroup = params->collision_group;
    shapeConfig.collisionMask = params->collision_mask;

    oimo_shape_init(&g_mesh_shapes[idx], &shapeConfig);
    g_mesh_shapes[idx]._isTrigger = params->trigger;
    oimo_rigid_body_add_shape(&g_mesh_bodies[idx], &g_mesh_shapes[idx]);

    oimo_world_add_rigid_body(&g_world, &g_mesh_bodies[idx]);
    obj->rigid_body = &g_mesh_bodies[idx];
    g_mesh_bodies[idx].userData = obj;
    g_mesh_used[idx] = true;

    return true;
}

// Legacy mesh collider (wraps _full version)
bool physics_create_mesh(ArmObject* obj,
                         const OimoVec3* vertices, const int16_t* indices,
                         int vertex_count, int index_count,
                         float friction, float restitution,
                         int collision_group, int collision_mask) {
    PhysicsBodyParams params = physics_body_params_default();
    params.friction = friction;
    params.restitution = restitution;
    params.collision_group = collision_group;
    params.collision_mask = collision_mask;
    return physics_create_mesh_full(obj, vertices, indices, vertex_count, index_count, &params);
}

// oimo_module's nlerp: the shorter way between two rotations, normalized
static OimoQuat nlerp(OimoQuat q1, OimoQuat q2, float t) {
    float dot = q1.x * q2.x + q1.y * q2.y + q1.z * q2.z + q1.w * q2.w;
    if (dot < 0.0f) q2 = oimo_quat(-q2.x, -q2.y, -q2.z, -q2.w);
    OimoQuat q = oimo_quat(q1.x * (1.0f - t) + q2.x * t, q1.y * (1.0f - t) + q2.y * t,
                           q1.z * (1.0f - t) + q2.z * t, q1.w * (1.0f - t) + q2.w * t);
    float inv_len = 1.0f / oimo_sqrt(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w);
    return oimo_quat(q.x * inv_len, q.y * inv_len, q.z * inv_len, q.w * inv_len);
}

// Transform sync
void physics_sync_object(ArmObject* obj, const OimoVec3* parent_world_pos, float alpha) {
    if (!obj || !obj->rigid_body) return;

    OimoRigidBody* rb = obj->rigid_body;
    if (rb->_type == OIMO_RIGID_BODY_STATIC) return;
    // Kinematic and animated: the object drives the body (sync_driven_bodies)
    if (object_drives_body(obj, rb)) return;

    OimoVec3 pos;
    OimoQuat rot;
    const BodyInterpolation* interp = &g_interpolation[get_body_index(rb)];
    if (interp->on) {
        // Between the last two steps (also while asleep: it settles on the last one)
        pos = oimo_vec3(interp->prev_pos.x * (1.0f - alpha) + interp->cur_pos.x * alpha,
                        interp->prev_pos.y * (1.0f - alpha) + interp->cur_pos.y * alpha,
                        interp->prev_pos.z * (1.0f - alpha) + interp->cur_pos.z * alpha);
        rot = nlerp(interp->prev_rot, interp->cur_rot, alpha);
    } else {
        if (rb->_sleeping) return;  // Unchanged since it fell asleep
        pos = oimo_rigid_body_get_position(rb);
        rot = oimo_rigid_body_get_orientation(rb);
    }

    // A parented object's location is relative to its parent's world position
    // (as oimo_module: position only)
    if (parent_world_pos) {
        pos.x -= parent_world_pos->x;
        pos.y -= parent_world_pos->y;
        pos.z -= parent_world_pos->z;
    }

    obj->transform.loc.x = pos.x;
    obj->transform.loc.y = pos.y;
    obj->transform.loc.z = pos.z;

    obj->transform.rot.x = rot.x;
    obj->transform.rot.y = rot.y;
    obj->transform.rot.z = rot.z;
    obj->transform.rot.w = rot.w;

    obj->transform.dirty = TRANSFORM_DIRTY_FRAMES;
}

// Contact callback management
void physics_set_contact_callback(PhysicsContactCallback callback, void* user_data) {
    g_contact_callback = callback;
    g_contact_user_data = user_data;
}

// Get contacts for a specific rigid body
int physics_get_contacts(OimoRigidBody* rb, PhysicsContactPair* contacts, int max_contacts) {
    if (!g_initialized || !rb || !contacts || max_contacts <= 0) return 0;

    int count = 0;
    OimoContactLink* link = rb->_contactLinkList;

    while (link != NULL && count < max_contacts) {
        OimoContact* contact = link->_contact;
        if (contact && contact->_touching && contact->_manifold._numPoints > 0) {
            OimoManifoldPoint* pt = &contact->_manifold._points[0];
            if (pt->_depth > 0) {
                PhysicsContactPair* pair = &contacts[count];

                // Determine which body is the "other"
                if (contact->_b1 == rb) {
                    pair->obj_a = (ArmObject*)rb->userData;
                    pair->obj_b = (ArmObject*)contact->_b2->userData;
                } else {
                    pair->obj_a = (ArmObject*)contact->_b1->userData;
                    pair->obj_b = (ArmObject*)rb->userData;
                }

                pair->pos_a = pt->_pos1;
                pair->pos_b = pt->_pos2;
                pair->normal = contact->_manifold._normal;
                pair->impulse = pt->_impulse.impulseN;
                count++;
            }
        }
        link = link->_next;
    }

    return count;
}