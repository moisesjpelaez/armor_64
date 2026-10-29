// UI fonts: one per size the canvases use. Font ids: fonts_defs.h (generated).
#pragma once

#include <libdragon.h>

#ifdef __cplusplus
extern "C" {
#endif

// FontId: FONT_<name>_<size>, FONT_COUNT
#include "fonts_defs.h"

void fonts_init(void);
void fonts_shutdown(void);

rdpq_font_t *fonts_get(FontId id);
uint8_t fonts_get_rdpq_id(FontId id);

#ifdef __cplusplus
}
#endif
