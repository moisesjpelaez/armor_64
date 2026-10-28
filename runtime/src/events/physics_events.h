// physics_events.h - Per-object physics contact event subscriptions
#pragma once

#include "../types.h"
#include "../limits.h"
#include "../oimo/physics.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Physics Contact Event System
 *
 * Traits subscribe to contacts of a rigid body where their code calls
 * rb.notifyOnContact(f) / rb.removeContact(f), as in oimo_module:
 *   physics_contact_subscribe(rb, handler, obj, data);    // the trait's obj and data
 *   physics_contact_unsubscribe(rb, handler, obj, data);
 *   physics_contact_dispatch();                           // Called by physics_step
 */



// Contact handler signature: (obj, data, other_obj)
// - obj: the object this trait is attached to
// - data: trait data pointer
// - other_obj: the object we collided with
typedef void (*PhysicsContactHandler)(void* obj, void* data, ArmObject* other);

// Subscription entry
typedef struct {
    PhysicsContactHandler handler;
    void* obj;
    void* data;
} PhysicsContactSub;

// Per-body subscriptions
typedef struct {
    OimoRigidBody* rb;
    PhysicsContactSub subs[MAX_CONTACT_SUBSCRIBERS];
    uint8_t count;
} BodyContactSubs;

// Initialize contact event system
void physics_contact_init(void);

// Clear all subscriptions (call on scene change)
void physics_contact_clear(void);

// Subscribe to contact events for a rigid body
// handler: function to call on contact
// obj: object pointer (usually the ArmObject with this rigid body)
// data: trait data pointer
void physics_contact_subscribe(OimoRigidBody* rb, PhysicsContactHandler handler, void* obj, void* data);

// Unsubscribe the first matching handler (the trait's), as Haxe's Array.remove
void physics_contact_unsubscribe(OimoRigidBody* rb, PhysicsContactHandler handler, void* obj, void* data);

// A body leaves the world: drop the subscriptions on it (its slot may be reused)
void physics_contact_forget_body(OimoRigidBody* rb);

// Dispatch contact events for all touching bodies
// Called internally by physics_step after world simulation
void physics_contact_dispatch(void);

#ifdef __cplusplus
}
#endif