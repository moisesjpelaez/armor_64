// Models: loading, recorded draw blocks, unloading. Model files: models_data.h (generated).
#include <libdragon.h>
#include <t3d/t3d.h>
#include <t3d/t3dmodel.h>

#include "models.h"
#include "../engine.h"
#if ENGINE_ENABLE_AUDIO
#include "../audio/audio.h"
#endif

// MODEL_PATHS: the .t3dm files, by ModelId
#include "models_data.h"

typedef struct {
    T3DModel *model;
    rspq_block_t *dpl;
    uint8_t loaded;
} ModelEntry;

static ModelEntry g_models[MODEL_COUNT];

void models_init(void)
{
    for (uint8_t i = 0; i < MODEL_COUNT; i++) {
        g_models[i].model = NULL;
        g_models[i].dpl = NULL;
        g_models[i].loaded = 0;
    }
}

void models_shutdown(void)
{
    for (uint8_t i = 0; i < MODEL_COUNT; i++) {
        if (g_models[i].loaded) {
            // Free display list FIRST since it references model data
            if (g_models[i].dpl) {
                rspq_block_free(g_models[i].dpl);
                g_models[i].dpl = NULL;
            }
            if (g_models[i].model) {
                t3d_model_free(g_models[i].model);
                g_models[i].model = NULL;
            }
            g_models[i].loaded = 0;
        }
    }
}

T3DModel *models_get(ModelId id)
{
    if (id < 0 || id >= MODEL_COUNT) {
        return NULL;
    }

    ModelEntry *e = &g_models[id];
    if (!e->loaded) {
#if ENGINE_ENABLE_AUDIO
        // Keep audio fed between loads (outside any rspq block: the mixer uses the RSP)
        arm_audio_mixer_poll();
#endif
        const char *path = MODEL_PATHS[id];
        e->model = t3d_model_load(path);
        if (!e->model) {
            debugf("Failed to load model: %s\n", path);
            return NULL;
        }
        e->loaded = 1;
    }
    // Also when a skinned object loaded it first (models_get_raw records no block)
    if (!e->dpl) {
        rspq_block_begin();
        t3d_model_draw(e->model);
        e->dpl = rspq_block_end();
    }
    return e->model;
}

T3DModel *models_get_raw(ModelId id)
{
    if (id < 0 || id >= MODEL_COUNT) {
        return NULL;
    }

    ModelEntry *e = &g_models[id];
    if (!e->loaded) {
        const char *path = MODEL_PATHS[id];
        e->model = t3d_model_load(path);
        if (!e->model) {
            debugf("Failed to load model: %s\n", path);
            return NULL;
        }
        // No DPL baking — skinned models record their own via animation_init()
        e->loaded = 1;
    }
    return e->model;
}

rspq_block_t *models_get_dpl(ModelId id)
{
    return g_models[id].dpl;
}

// Unload a model and free its resources
void models_unload(ModelId id)
{
    if (id < 0 || id >= MODEL_COUNT) {
        return;
    }

    ModelEntry *e = &g_models[id];
    if (e->loaded) {
        // Free display list FIRST since it references model data
        if (e->dpl) {
            rspq_block_free(e->dpl);
            e->dpl = NULL;
        }
        if (e->model) {
            t3d_model_free(e->model);
            e->model = NULL;
        }
        e->loaded = 0;
    }
}

// Unload the loaded models `keep` (indexed by ModelId) doesn't mark: the next
// scene's models stay loaded across a scene switch
void models_unload_except(const bool *keep)
{
    for (uint8_t i = 0; i < MODEL_COUNT; i++) {
        if (g_models[i].loaded && !keep[i]) {
            models_unload(i);
        }
    }
}
