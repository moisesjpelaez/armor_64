// Models: loading, recorded draw blocks, unloading. Model ids: models_defs.h (generated).
#pragma once

#include <t3d/t3dmodel.h>
#include <rspq.h>

#ifdef __cplusplus
extern "C" {
#endif

// ModelId: MODEL_<name>, MODEL_COUNT
#include "models_defs.h"

void models_init(void);
void models_shutdown(void);

T3DModel *models_get(ModelId id);
T3DModel *models_get_raw(ModelId id);     // Load without DPL baking (for skinned models)
rspq_block_t *models_get_dpl(ModelId id);

void models_unload(ModelId id);           // Force unload a model
void models_unload_except(const bool *keep);  // Unload the models keep[id] doesn't mark

#ifdef __cplusplus
}
#endif
