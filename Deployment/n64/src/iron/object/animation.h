/**
 * N64 Skeletal Animation System
 * ==============================
 *
 * Wraps tiny3d's T3DSkeleton / T3DAnim API into an engine-friendly interface
 * attached to ArmObject via the animation pointer.
 *
 * Runtime flow (per frame):
 *   1. animation_update()  — advances time, updates bone transforms
 *   2. animation_use()     — selects correct skeleton buffer for RSP
 *   3. rspq_block_run(obj->dpl)  — renders with the current pose
 *
 * The skinned display list is pre-recorded during animation_init() using
 * t3d_model_draw_skinned(). Skeleton buffer swapping happens via
 * t3d_skeleton_use() before the DPL is dispatched.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <t3d/t3dmodel.h>
#include <t3d/t3dskeleton.h>
#include <t3d/t3danim.h>
#include <rspq.h>

#ifdef __cplusplus
extern "C" {
#endif

#define ANIM_MAX_CLIPS 8

/**
 * ArmAnimation — per-object skeletal animation instance.
 *
 * Memory ownership:
 *   - skeleton / skeleton_blend: owned, freed by animation_destroy()
 *   - anims[]:  owned, freed by animation_destroy()
 *   - dpl_skinned: owned, freed by animation_destroy()
 *   - model:    weak reference (owned by models system, NOT freed here)
 *   - anim_names[]: weak references to static strings (NOT freed)
 */
typedef struct ArmAnimation {
    // --- Skeleton ---
    T3DSkeleton skeleton;           // Buffered skeleton (has FP matrices for RSP)
    T3DSkeleton skeleton_blend;     // Clone for blending (no matrices)

    // --- Animation clips ---
    T3DAnim anims[ANIM_MAX_CLIPS];
    const char *anim_names[ANIM_MAX_CLIPS];
    uint8_t anim_count;

    // --- Playback state ---
    uint8_t current_anim;           // Index of primary clip (attached to skeleton)
    uint8_t blend_anim;             // Index of blend clip (0xFF = none)
    float blend_factor;             // 0.0 = current only, 1.0 = blend only
    bool playing;
    float speed;

    // --- Rendering ---
    T3DModel *model;                // Weak ref to loaded model
    rspq_block_t *dpl_skinned;      // Pre-recorded skinned draw DPL
} ArmAnimation;

/**
 * Initialize animation for a skinned model.
 * Creates buffered skeleton, auto-discovers all animation clips from the
 * model, and records the skinned DPL.
 *
 * @param anim       Struct to initialize (caller-allocated)
 * @param model      Loaded T3DModel with skeleton + animation data
 */
void animation_init(ArmAnimation *anim, T3DModel *model);

/** Free all resources owned by this animation instance. */
void animation_destroy(ArmAnimation *anim);

/**
 * Play a clip by exact name.
 * Rewinds to t=0, attaches to main skeleton, starts playback.
 * @return true if clip was found and started
 */
bool animation_play(ArmAnimation *anim, const char *name);

/** Pause the current clip. */
void animation_pause(ArmAnimation *anim);

/** Resume the current clip. */
void animation_resume(ArmAnimation *anim);

/** Set playback speed (1.0 = normal, 0.5 = half, etc.). */
void animation_set_speed(ArmAnimation *anim, float speed);

/** Set looping on current clip. */
void animation_set_looping(ArmAnimation *anim, bool loop);

/** Advance animation time and recalculate skeleton matrices. */
void animation_update(ArmAnimation *anim, float dt);

/** Select correct skeleton buffer for the next draw call (RSP segment). */
void animation_use(const ArmAnimation *anim);

/** Find clip index by name. Returns -1 if not found. */
int animation_find(const ArmAnimation *anim, const char *name);

/** Check if current (non-looping) clip has finished. */
bool animation_is_finished(const ArmAnimation *anim);

#ifdef __cplusplus
}
#endif
