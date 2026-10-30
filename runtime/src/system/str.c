// str.c - Ints, Floats and Strings as text, as Haxe writes them (see utils.h)
#include "../utils.h"

static const double POW10[23] = {
    1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10, 1e11,
    1e12, 1e13, 1e14, 1e15, 1e16, 1e17, 1e18, 1e19, 1e20, 1e21, 1e22
};

static const uint32_t POW10_INT[10] = {
    1, 10, 100, 1000, 10000, 100000, 1000000, 10000000, 100000000, 1000000000
};

// d * 10^e, rounded once while 10^|e| is exact (|e| <= 22)
static double scale10(double d, int e) {
    int n = e < 0 ? -e : e;
    double p = 1.0;
    while (n > 22) { p *= 1e22; n -= 22; }
    p *= POW10[n];
    return e < 0 ? d / p : d * p;
}

int str_int(char* out, int32_t v) {
    char digits[10];
    int count = 0, n = 0;
    uint32_t u = v < 0 ? 0u - (uint32_t)v : (uint32_t)v;
    do { digits[count++] = (char)('0' + u % 10); u /= 10; } while (u != 0);
    if (v < 0) out[n++] = '-';
    while (count > 0) out[n++] = digits[--count];
    return n;
}

// Haxe on Krom prints a Float as JavaScript does: the fewest digits that read
// back as the same number (here the same single-precision float), in fixed
// notation from 1e-6 to below 1e21, else as 1.5e+21 / 1e-7
int str_float(char* out, float f) {
    int n = 0;
    // NaN and infinity from the bits: -ffast-math (libdragon's flags) assumes
    // floats are finite and may drop f != f
    union { float f; uint32_t u; } bits = { f };
    int biased = (int)((bits.u >> 23) & 0xff);
    if (biased == 0xff && (bits.u & 0x7fffff) != 0) { memcpy(out, "NaN", 3); return 3; }
    if (bits.u >> 31) { out[n++] = '-'; f = -f; }
    if (biased == 0xff) { memcpy(out + n, "Infinity", 8); return n + 8; }
    // Integers below 2^24 are exact: their digits are the shortest
    if (f < 16777216.0f && f == (float)(int32_t)f) {
        if (f == 0) n = 0;  // -0 is "0"
        return n + str_int(out + n, (int32_t)f);
    }

    // Decimal exponent of the first digit: f is in [10^exp10, 10^(exp10 + 1))
    double d = f;
    int exp2 = biased - 127;
    int exp10 = (exp2 * 1233) >> 12;  // floor(exp2 * log10(2)), at most 1 too small
    double scaled = scale10(d, -exp10);
    while (scaled >= 10.0) scaled = scale10(d, -(++exp10));
    while (scaled < 1.0) scaled = scale10(d, -(--exp10));  // subnormals

    // The fewest digits that read back as f, each count rounded from f itself
    // (the nearest, the even one on a tie, as JavaScript; 9 always read back)
    uint32_t value = 0;
    int count = 9;
    for (int len = 1; len <= 9; len++) {
        double x = scale10(d, len - 1 - exp10);
        uint32_t c = (uint32_t)x;
        double frac = x - (double)c;
        if (frac > 0.5 || (frac == 0.5 && (c & 1))) c++;
        int e = exp10;
        if (c >= POW10_INT[len]) { c /= 10; e++; }
        if (len == 9 || (float)scale10((double)c, e - len + 1) == f) {
            value = c;
            count = len;
            exp10 = e;
            break;
        }
    }
    while (count > 1 && value % 10 == 0) { value /= 10; count--; }

    char s[9];
    for (int i = count - 1; i >= 0; i--) { s[i] = (char)('0' + value % 10); value /= 10; }

    // Digits before the decimal point
    int point = exp10 + 1;
    if (count <= point && point <= 21) {
        memcpy(out + n, s, count);
        n += count;
        for (int i = count; i < point; i++) out[n++] = '0';
    } else if (0 < point && point <= 21) {
        memcpy(out + n, s, point);
        n += point;
        out[n++] = '.';
        memcpy(out + n, s + point, count - point);
        n += count - point;
    } else if (-6 < point && point <= 0) {
        out[n++] = '0';
        out[n++] = '.';
        for (int i = point; i < 0; i++) out[n++] = '0';
        memcpy(out + n, s, count);
        n += count;
    } else {
        out[n++] = s[0];
        if (count > 1) {
            out[n++] = '.';
            memcpy(out + n, s + 1, count - 1);
            n += count - 1;
        }
        out[n++] = 'e';
        out[n++] = exp10 < 0 ? '-' : '+';
        n += str_int(out + n, exp10 < 0 ? -exp10 : exp10);
    }
    return n;
}

// The part doesn't fit: what fits of it (a UTF-8 character may be cut)
int str_add_cut(char* s, int n, const char* text, int len) {
    (void)len;
    int room = STR_FMT_BUFFER_SIZE - 1 - n;
    memcpy(s + n, text, room);
    ARM_LIMIT_REACHED(STR_FMT_BUFFER_SIZE);
    return n + room;
}

int str_add_int(char* s, int n, int32_t v) {
    char text[11];
    return str_add(s, n, text, str_int(text, v));
}

int str_add_float(char* s, int n, float v) {
    char text[24];
    return str_add(s, n, text, str_float(text, v));
}

int str_add_str(char* s, int n, const char* text) {
    if (text == NULL) text = "null";
    return str_add(s, n, text, (int)strlen(text));
}
