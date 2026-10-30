/**
 * Signal System Implementation
 * ============================
 *
 * Signals with connect/disconnect/emit support.
 * Each signal can have up to ARM_SIGNAL_MAX_SUBS subscribers (default 16).
 *
 * A subscriber is a handler and its context (a trait's data, or NULL for
 * autoloads). Connecting and disconnecting is up to the game code: a trait
 * must disconnect its handlers before its data is freed (e.g. in
 * notifyOnRemove). Handlers can connect and disconnect, also themselves, while
 * the signal is emitting: removed entries are only cleared during an emit and
 * compacted once it finishes, so no subscriber is skipped.
 *
 * Performance: O(n) for all operations where n = subscriber count
 */

#include <libdragon.h>
#include "../types.h"
#include <string.h>

bool signal_is_connected(const ArmSignal *signal, ArmSignalHandler handler, void *ctx)
{
    if (signal == NULL) return false;
    for (uint8_t i = 0; i < signal->count; i++) {
        if (signal->entries[i].handler == handler && signal->entries[i].ctx == ctx) return true;
    }
    return false;
}

void signal_connect(ArmSignal *signal, ArmSignalHandler handler, void *ctx)
{
    if (signal == NULL || handler == NULL) return;

    // Check if already connected (avoid duplicates)
    for (uint8_t i = 0; i < signal->count; i++) {
        ArmSignalEntry *e = &signal->entries[i];
        if (e->handler == handler && e->ctx == ctx) {
            return;  // Already connected
        }
    }

    if (signal->count >= ARM_SIGNAL_MAX_SUBS) {
        ARM_LIMIT_REACHED(ARM_SIGNAL_MAX_SUBS);
        return;
    }

    // Add new subscriber
    ArmSignalEntry *entry = &signal->entries[signal->count++];
    entry->handler = handler;
    entry->ctx = ctx;
}

// Remove entries[i]. During an emit it's only cleared, and compacted afterwards
static void signal_remove_at(ArmSignal *signal, uint8_t i)
{
    if (signal->emitting) {
        signal->entries[i].handler = NULL;
        signal->removed++;
        return;
    }
    // memmove: a signal may have a single slot
    memmove(&signal->entries[i], &signal->entries[i + 1], (size_t)(signal->count - i - 1) * sizeof(signal->entries[0]));
    signal->count--;
}

static void signal_compact(ArmSignal *signal)
{
    uint8_t kept = 0;
    for (uint8_t i = 0; i < signal->count; i++) {
        if (signal->entries[i].handler != NULL) {
            signal->entries[kept++] = signal->entries[i];
        }
    }
    signal->count = kept;
    signal->removed = 0;
}

void signal_disconnect(ArmSignal *signal, ArmSignalHandler handler, void *ctx)
{
    if (signal == NULL || handler == NULL) return;

    for (uint8_t i = 0; i < signal->count; i++) {
        ArmSignalEntry *e = &signal->entries[i];
        if (e->handler == handler && e->ctx == ctx) {
            signal_remove_at(signal, i);
            return;
        }
    }
}

void signal_emit(ArmSignal *signal, void *payload)
{
    if (signal == NULL || signal->count == 0) return;

    signal->emitting++;
    // Subscribers connected by a handler are called from the next emit
    uint8_t count = signal->count;
    for (uint8_t i = 0; i < count; i++) {
        ArmSignalEntry *e = &signal->entries[i];
        if (e->handler != NULL) {
            e->handler(e->ctx, payload);
        }
    }
    if (--signal->emitting == 0 && signal->removed > 0) {
        signal_compact(signal);
    }
}

void signal_clear(ArmSignal *signal)
{
    if (signal == NULL) return;
    memset(signal, 0, sizeof(ArmSignal));
}
