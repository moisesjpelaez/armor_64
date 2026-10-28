// Time system using libdragon's get_ticks()
#include "time.h"
#include <libdragon.h>

float time_delta = 0.0f;
float time_scale = 1.0f;
float time_fixed_step = 0.0f;  // Set by main.c from FIXED_TIMESTEP

static uint64_t time_start_ticks = 0;
static uint64_t time_last_ticks = 0;

void time_init(void)
{
    time_start_ticks = get_ticks();
    time_last_ticks = time_start_ticks;
    time_delta = 0.0f;
    time_scale = 1.0f;
}

// Real seconds since the last update, as Iron's Time.delta: not scaled by
// Time.scale (code that wants it multiplies, as Iron's animations and physics do)
void time_update(void)
{
    uint64_t now = get_ticks();
    uint64_t elapsed_ticks = now - time_last_ticks;
    time_last_ticks = now;

    time_delta = (float)((double)elapsed_ticks / (double)TICKS_PER_SECOND);
}

void time_restart_frame(void)
{
    time_last_ticks = get_ticks();
}

float time_get(void)
{
    uint64_t elapsed = get_ticks() - time_start_ticks;
    return (float)((double)elapsed / (double)TICKS_PER_SECOND);
}

uint64_t time_get_ms(void)
{
    return get_ticks_ms() - (time_start_ticks * 1000 / TICKS_PER_SECOND);
}

uint64_t time_get_us(void)
{
    return get_ticks_us() - (time_start_ticks * 1000000 / TICKS_PER_SECOND);
}
