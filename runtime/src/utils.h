// utils.h - Engine utility functions
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "limits.h"

#ifdef __cplusplus
extern "C" {
#endif

// Largest value tiny3d's 16.16 fixed-point matrices hold (t3d_mat4_to_fixed). In the
// renderer's ×64 space that's ±511 BU. Converting a larger float, or a NaN, raises an
// exception on the N64's CPU (emulators may just clamp).
#define FIXED_POINT_MAX 32767.0f

// Whether a float converts to tiny3d's fixed point (false for NaN and infinity too)
static inline bool fits_fixed_point(float f) {
    return fabsf(f) <= FIXED_POINT_MAX;
}

// String formatting - rotating buffer for multiple calls per frame
static char _str_fmt_buffers[STR_FMT_BUFFER_COUNT][STR_FMT_BUFFER_SIZE];
static int _str_fmt_buffer_idx = 0;

static inline char* _str_get_buffer(void) {
    char* buf = _str_fmt_buffers[_str_fmt_buffer_idx];
    _str_fmt_buffer_idx = (_str_fmt_buffer_idx + 1) % STR_FMT_BUFFER_COUNT;
    return buf;
}

// StringTools.endsWith
static inline bool str_ends_with(const char* s, const char* end) {
    size_t s_len = strlen(s), end_len = strlen(end);
    return s_len >= end_len && memcmp(s + s_len - end_len, end, end_len) == 0;
}

// Strings built as Haxe builds them (system/str.c): each part is appended to a
// buffer from _str_get_buffer() at length n, the new length is returned. The
// caller writes the terminator at the end.
int str_add_cut(char* s, int n, const char* text, int len);
int str_add_int(char* s, int n, int32_t v);
int str_add_float(char* s, int n, float v);
int str_add_str(char* s, int n, const char* text);

// Haxe's text of an Int / a Float, without terminator; returns its length.
// out holds 11 / 24 bytes
int str_int(char* out, int32_t v);
int str_float(char* out, float f);

static inline int str_add(char* s, int n, const char* text, int len) {
    // Never negative (lets GCC bound the copy: no -Warray-bounds)
    if (n < 0 || len < 0) __builtin_unreachable();
    if (__builtin_expect(n + len >= STR_FMT_BUFFER_SIZE, 0)) return str_add_cut(s, n, text, len);
    memcpy(s + n, text, len);
    return n + len;
}

static inline int str_add_bool(char* s, int n, bool v) {
    return v ? str_add(s, n, "true", 4) : str_add(s, n, "false", 5);
}

// A String as Haxe prints it: null is "null"
static inline const char* str_or_null(const char* s) {
    return s != NULL ? s : "null";
}

#ifdef __cplusplus
}
#endif
