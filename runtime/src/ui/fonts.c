// UI fonts: loaded at boot. Font files: fonts_data.h (generated).
#include <libdragon.h>
#include "fonts.h"

// FONT_PATHS: the .font64 files (converted from .ttf by the Makefile)
#include "fonts_data.h"

// rdpq font IDs start at 2 (1 is reserved for libdragon debug font)
#define FONT_ID_BASE 2

typedef struct {
    rdpq_font_t *font;
    uint8_t loaded;
} FontEntry;

static FontEntry g_fonts[FONT_COUNT];

void fonts_init(void)
{
    for (uint8_t i = 0; i < FONT_COUNT; i++) {
        g_fonts[i].font = NULL;
        g_fonts[i].loaded = 0;
    }
}

void fonts_shutdown(void)
{
    for (uint8_t i = 0; i < FONT_COUNT; i++) {
        if (g_fonts[i].loaded) {
            if (g_fonts[i].font) {
                rdpq_font_free(g_fonts[i].font);
                g_fonts[i].font = NULL;
            }
            g_fonts[i].loaded = 0;
        }
    }
}

rdpq_font_t *fonts_get(FontId id)
{
    if (id >= FONT_COUNT) {
        return NULL;
    }

    FontEntry *e = &g_fonts[id];
    if (!e->loaded) {
        const char *path = FONT_PATHS[id];
        e->font = rdpq_font_load(path);
        if (e->font) {
            rdpq_text_register_font(FONT_ID_BASE + id, e->font);
            e->loaded = 1;
        }
    }
    return e->font;
}

uint8_t fonts_get_rdpq_id(FontId id)
{
    return FONT_ID_BASE + id;
}
