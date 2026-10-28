// physics_events.c - Per-object physics contact event subscriptions
#include "physics_events.h"
#include "../oimo/physics.h"
#include <string.h>

// Static storage for body contact subscriptions. A slot with rb == NULL is free.
static BodyContactSubs g_body_subs[MAX_CONTACT_BODIES];
static int g_body_sub_count = 0;

// Handlers subscribe and unsubscribe themselves while contacts are dispatched
// (rb.removeContact(f) in f): entries removed then are only cleared, and
// compacted once the dispatch finishes, so no other handler is skipped
static int g_dispatching = 0;
static bool g_compact_pending = false;

void physics_contact_init(void)
{
    memset(g_body_subs, 0, sizeof(g_body_subs));
    g_body_sub_count = 0;
}

void physics_contact_clear(void)
{
    memset(g_body_subs, 0, sizeof(g_body_subs));
    g_body_sub_count = 0;
    g_compact_pending = false;
}

// Find subscription slot for a rigid body
static BodyContactSubs* find_body_subs(OimoRigidBody* rb)
{
    for (int i = 0; i < g_body_sub_count; i++) {
        if (g_body_subs[i].rb == rb) {
            return &g_body_subs[i];
        }
    }
    return NULL;
}

// Find or create subscription slot for a rigid body (free slots are reused)
static BodyContactSubs* find_or_create_body_subs(OimoRigidBody* rb)
{
    BodyContactSubs* slot = find_body_subs(rb);
    if (slot) return slot;
    slot = find_body_subs(NULL);
    if (!slot) {
        if (g_body_sub_count >= MAX_CONTACT_BODIES) {
            ARM_LIMIT_REACHED(MAX_CONTACT_BODIES);
            return NULL;
        }
        slot = &g_body_subs[g_body_sub_count++];
    }
    slot->rb = rb;
    slot->count = 0;
    return slot;
}

// Drop cleared entries; a slot left without handlers becomes free
static void compact_body_subs(BodyContactSubs* subs)
{
    int kept = 0;
    for (int i = 0; i < subs->count; i++) {
        if (subs->subs[i].handler != NULL) subs->subs[kept++] = subs->subs[i];
    }
    subs->count = (uint8_t)kept;
    if (kept == 0) subs->rb = NULL;
}

// Remove entry i (cleared only while dispatching)
static void remove_sub(BodyContactSubs* subs, int i)
{
    subs->subs[i].handler = NULL;
    if (g_dispatching) {
        g_compact_pending = true;
    } else {
        compact_body_subs(subs);
    }
}

void physics_contact_subscribe(OimoRigidBody* rb, PhysicsContactHandler handler, void* obj, void* data)
{
    if (!rb || !handler) return;

    BodyContactSubs* subs = find_or_create_body_subs(rb);
    if (!subs) return;
    if (subs->count >= MAX_CONTACT_SUBSCRIBERS) {
        ARM_LIMIT_REACHED(MAX_CONTACT_SUBSCRIBERS);
        return;
    }

    // Subscribing twice calls the handler twice, as oimo_module's array
    subs->subs[subs->count].handler = handler;
    subs->subs[subs->count].obj = obj;
    subs->subs[subs->count].data = data;
    subs->count++;
}

void physics_contact_unsubscribe(OimoRigidBody* rb, PhysicsContactHandler handler, void* obj, void* data)
{
    if (!rb || !handler) return;

    BodyContactSubs* subs = find_body_subs(rb);
    if (!subs) return;

    for (int i = 0; i < subs->count; i++) {
        PhysicsContactSub* sub = &subs->subs[i];
        if (sub->handler == handler && sub->obj == obj && sub->data == data) {
            remove_sub(subs, i);
            return;
        }
    }
}

void physics_contact_forget_body(OimoRigidBody* rb)
{
    if (!rb) return;
    BodyContactSubs* subs = find_body_subs(rb);
    if (!subs) return;
    for (int i = 0; i < subs->count; i++) subs->subs[i].handler = NULL;
    subs->rb = NULL;  // Free now: the body's slot may be reused for another body
    if (g_dispatching) {
        g_compact_pending = true;
    } else {
        subs->count = 0;
    }
}

// A contact as oimo_module's PhysicsWorld counts it: a point with some penetration
static bool contact_has_depth(const OimoContact* contact)
{
    const OimoManifold* manifold = &contact->_manifold;
    for (int i = 0; i < manifold->_numPoints; i++) {
        if (manifold->_points[i]._depth > 0) return true;
    }
    return false;
}

void physics_contact_dispatch(void)
{
    // Fast path: skip entirely if no bodies have contact subscriptions
    if (g_body_sub_count == 0) return;

    // Called after physics step
    // Iterate all touching contacts and dispatch to subscribed handlers
    OimoWorld* world = physics_get_world();
    if (!world) return;

    g_dispatching++;
    OimoContact* contact = world->_contactManager._contactList;
    while (contact != NULL) {
        // Save next before dispatching - handlers may trigger body removal
        // which destroys contacts, invalidating contact->_next
        OimoContact* next = contact->_next;

        if (contact->_touching && contact_has_depth(contact)) {
            // Get both bodies' ArmObject pointers
            OimoRigidBody* rb1 = contact->_b1;
            OimoRigidBody* rb2 = contact->_b2;

            // Early exit: check if either body has subscribers before getting objects
            BodyContactSubs* subs1 = find_body_subs(rb1);
            BodyContactSubs* subs2 = find_body_subs(rb2);

            // Skip this contact if neither body has subscribers
            if (!subs1 && !subs2) {
                contact = next;
                continue;
            }

            ArmObject* obj1 = (ArmObject*)rb1->userData;
            ArmObject* obj2 = (ArmObject*)rb2->userData;

            // Skip if either object has been marked for removal
            if (!obj1 || !obj2 || obj1->is_removed || obj2->is_removed) {
                contact = next;
                continue;
            }

            // Dispatch to body1's subscribers with body2 as "other"
            if (subs1) {
                for (int i = 0; i < subs1->count; i++) {
                    if (subs1->subs[i].handler == NULL) continue;
                    subs1->subs[i].handler(subs1->subs[i].obj, subs1->subs[i].data, obj2);
                }
            }

            // Dispatch to body2's subscribers with body1 as "other"
            if (subs2) {
                for (int i = 0; i < subs2->count; i++) {
                    if (subs2->subs[i].handler == NULL) continue;
                    subs2->subs[i].handler(subs2->subs[i].obj, subs2->subs[i].data, obj1);
                }
            }
        }
        contact = next;
    }
    if (--g_dispatching == 0 && g_compact_pending) {
        g_compact_pending = false;
        for (int b = 0; b < g_body_sub_count; b++) {
            if (g_body_subs[b].count > 0) compact_body_subs(&g_body_subs[b]);
        }
    }
}
