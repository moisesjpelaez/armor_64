/**
 * Skeletal animation runtime — see animation.h for API docs.
 *
 * Implementation follows tiny3d example 08_animation:
 *   - Buffered skeleton for triple-buffered rendering
 *   - Blend skeleton (no matrices) for cross-fade support
 *   - First clip auto-attached to main skeleton and playing
 *   - Skinned DPL recorded once at init time
 */

#include "animation.h"

#include <string.h>
#include <libdragon.h>
#include "../../types.h"   // FB_COUNT

// ---------------------------------------------------------------------------
// Init / Destroy
// ---------------------------------------------------------------------------

void animation_init(ArmAnimation *anim, T3DModel *model)
{
    memset(anim, 0, sizeof(ArmAnimation));
    anim->model        = model;
    anim->speed        = 1.0f;
    anim->playing      = false;  // Start disabled until skeleton verified
    anim->blend_anim   = 0xFF;
    anim->current_anim = 0;

    // Guard: check if model actually has skeleton data
    // (GLB export can fail to include skin data for some scene structures)
    if (!t3d_model_get_skeleton(model)) {
        debugf("[animation] ERROR: Model has no skeleton data!\n");
        debugf("[animation] Check that the GLB export included skin/joint data.\n");
        anim->dpl_skinned = NULL;
        anim->anim_count  = 0;
        return;
    }

    // Create buffered skeleton from model's skeleton definition
    anim->skeleton       = t3d_skeleton_create_buffered(model, FB_COUNT);
    anim->skeleton_blend = t3d_skeleton_clone(&anim->skeleton, false);
    anim->playing        = true;  // Now safe to enable

    // Auto-discover animation clips from the model
    uint32_t model_anim_count = t3d_model_get_animation_count(model);
    uint16_t count = model_anim_count < ANIM_MAX_CLIPS ? (uint16_t)model_anim_count : ANIM_MAX_CLIPS;
    anim->anim_count = count;

    if (count > 0) {
        T3DChunkAnim *anim_chunks[ANIM_MAX_CLIPS];
        t3d_model_get_animations(model, anim_chunks);

        debugf("[animation] Model has %d clips:\n", count);
        for (uint16_t i = 0; i < count; i++) {
            debugf("[animation]   [%d] \"%s\"\n", i, anim_chunks[i]->name);
            anim->anim_names[i] = anim_chunks[i]->name;
            anim->anims[i]      = t3d_anim_create(model, anim_chunks[i]->name);
            t3d_anim_attach(&anim->anims[i], &anim->skeleton);

            T3DAnim *ta = &anim->anims[i];
            for (int c = 0; c < ta->animRef->channelsQuat; c++) {
                ta->targetsQuat[c].kfCurr = *ta->targetsQuat[c].targetQuat;
                ta->targetsQuat[c].kfNext = *ta->targetsQuat[c].targetQuat;
            }
            for (int c = 0; c < ta->animRef->channelsScalar; c++) {
                ta->targetsScalar[c].kfCurr = *ta->targetsScalar[c].targetScalar;
                ta->targetsScalar[c].kfNext = *ta->targetsScalar[c].targetScalar;
            }
        }
        // Auto-play first clip if available
        t3d_anim_set_playing(&anim->anims[0], true);
    }

    // Record skinned draw display list (uses main skeleton)
    rspq_block_begin();
    t3d_model_draw_skinned(model, &anim->skeleton);
    anim->dpl_skinned = rspq_block_end();
}

void animation_destroy(ArmAnimation *anim)
{
    if (!anim) return;

    if (anim->dpl_skinned) {
        rspq_block_free(anim->dpl_skinned);
        anim->dpl_skinned = NULL;
    }

    for (uint16_t i = 0; i < anim->anim_count; i++) {
        t3d_anim_destroy(&anim->anims[i]);
    }

    t3d_skeleton_destroy(&anim->skeleton_blend);
    t3d_skeleton_destroy(&anim->skeleton);

    anim->anim_count = 0;
    anim->model = NULL;
}

// ---------------------------------------------------------------------------
// Lookup
// ---------------------------------------------------------------------------

int animation_find(const ArmAnimation *anim, const char *name)
{
    if (!anim || !name) return -1;
    for (uint16_t i = 0; i < anim->anim_count; i++) {
        if (strcmp(anim->anim_names[i], name) == 0) {
            return i;
        }
    }
    return -1;
}

// ---------------------------------------------------------------------------
// Playback control
// ---------------------------------------------------------------------------

bool animation_play(ArmAnimation *anim, const char *name)
{
    int idx = animation_find(anim, name);
    if (idx < 0) return false;

    // Stop current clip
    if (anim->current_anim < anim->anim_count) {
        t3d_anim_set_playing(&anim->anims[anim->current_anim], false);
    }

    // Start new clip (already attached in init)
    anim->current_anim = (uint16_t)idx;
    t3d_anim_set_playing(&anim->anims[idx], true);
    t3d_anim_set_time(&anim->anims[idx], 0.0f);
    anim->playing      = true;
    anim->blend_anim   = 0xFF;
    anim->blend_factor = 0.0f;

    return true;
}

void animation_pause(ArmAnimation *anim)
{
    if (!anim) return;
    anim->playing = false;
    if (anim->current_anim < anim->anim_count) {
        t3d_anim_set_playing(&anim->anims[anim->current_anim], false);
    }
}

void animation_resume(ArmAnimation *anim)
{
    if (!anim) return;
    anim->playing = true;
    if (anim->current_anim < anim->anim_count) {
        t3d_anim_set_playing(&anim->anims[anim->current_anim], true);
    }
}

void animation_set_speed(ArmAnimation *anim, float speed)
{
    if (!anim) return;
    anim->speed = speed;
    if (anim->current_anim < anim->anim_count) {
        t3d_anim_set_speed(&anim->anims[anim->current_anim], speed);
    }
}

void animation_set_looping(ArmAnimation *anim, bool loop)
{
    if (!anim) return;
    if (anim->current_anim < anim->anim_count) {
        t3d_anim_set_looping(&anim->anims[anim->current_anim], loop);
    }
}

// ---------------------------------------------------------------------------
// Per-frame update
// ---------------------------------------------------------------------------

void animation_update(ArmAnimation *anim, float dt)
{
    if (!anim || !anim->playing) return;

    // Update primary clip
    if (anim->current_anim < anim->anim_count) {
        t3d_anim_update(&anim->anims[anim->current_anim], dt);
    }

    // Cross-fade blend if active
    if (anim->blend_anim != 0xFF && anim->blend_anim < anim->anim_count) {
        t3d_anim_update(&anim->anims[anim->blend_anim], dt);
        t3d_skeleton_blend(&anim->skeleton, &anim->skeleton,
                           &anim->skeleton_blend, anim->blend_factor);
    }

    // Recalculate bone matrices for rendering
    t3d_skeleton_update(&anim->skeleton);
}

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

void animation_use(const ArmAnimation *anim)
{
    if (!anim) return;
    t3d_skeleton_use(&anim->skeleton);
}

bool animation_is_finished(const ArmAnimation *anim)
{
    if (!anim || anim->current_anim >= anim->anim_count) return true;
    return !t3d_anim_is_playing(&anim->anims[anim->current_anim]);
}
