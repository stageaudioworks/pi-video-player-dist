/*
 * Stage Audio Works Raspberry Pi Video Player
 * Copyright (C) 2026 Stage Audio Works
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 *
 * =============================================================================
 * bridge_logic.h — every decision the AES67 bridge makes, with no sound card
 * =============================================================================
 * Nothing here touches ALSA, a socket, a thread or a clock, so all of it runs
 * under tools/test_aes67_bridge.sh on a machine with no audio hardware. The
 * program around it (saw-aes67-bridge.c) only moves bytes and asks these
 * functions what to do.
 *
 * The rules come from the aes67-integration skill (SKILL.md sections 7 and 8,
 * reference.md "Clock recovery"). Where a number below looks arbitrary, the
 * comment says where it was measured.
 */
#ifndef SAW_BRIDGE_LOGIC_H
#define SAW_BRIDGE_LOGIC_H

#include <ctype.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#define BRIDGE_RATE      48000      /* AES67: the only rate the daemon runs at */
#define BRIDGE_IN_CH     2          /* the loopback is always stereo; see pack_block */

/* ── Waiting on a card that has stopped ───────────────────────────────────────
 * Every wait is at most a slice, so a card that stops taking samples is noticed
 * in steps and never blocks a thread for good (obs-multisite alsa_stall.h: a
 * stalled write once froze a whole player). No progress for STALL_MS with no
 * error reported is a stall: the daemon restarting freezes the RAVENNA engine
 * while both substreams still say RUNNING (SKILL.md section 7). Reopening is
 * throttled, as the skill does, so a card that keeps stalling is not hammered. */
#define WAIT_SLICE_MS        100
#define STALL_MS             2000
#define REOPEN_THROTTLE_MS   15000

/* The card registers a moment after modprobe: retry with backoff, never give
 * up (SKILL.md section 7: 2, 4, 6, 10 s...). failures = opens failed in a row. */
static inline long open_backoff_ms(int failures)
{
    static const long steps[] = { 0, 2000, 4000, 6000, 10000 };
    if (failures < 0) failures = 0;
    return failures < 5 ? steps[failures] : 10000;
}

/* When the next open may be tried. After a stall, never sooner than
 * REOPEN_THROTTLE_MS after the last successful open. */
static inline int64_t next_open_ms(int64_t now, int64_t last_open, int failures,
                                   int after_stall)
{
    int64_t t = now + open_backoff_ms(failures);
    if (after_stall && last_open + REOPEN_THROTTLE_MS > t)
        t = last_open + REOPEN_THROTTLE_MS;
    return t;
}

/* ── PTP: open the card only while locked ─────────────────────────────────────
 * The driver only moves samples while PTP is locked, and an open card that is
 * not being serviced goes XRUN (SKILL.md section 7). The daemon is polled once
 * a second; a single missed poll must not close a card that is working, so
 * opening needs PTP_LOCK_POLLS locked answers in a row and closing needs
 * PTP_UNLOCK_POLLS unlocked or unanswered ones. */
#define PTP_LOCK_POLLS    2
#define PTP_UNLOCK_POLLS  3

typedef struct {
    int good_run, bad_run;
    int open_ok;
} ptp_gate;

static inline int ptp_gate_update(ptp_gate *g, int locked)
{
    if (locked) {
        g->bad_run = 0;
        if (++g->good_run >= PTP_LOCK_POLLS) g->open_ok = 1;
    } else {
        g->good_run = 0;
        if (++g->bad_run >= PTP_UNLOCK_POLLS) g->open_ok = 0;
    }
    return g->open_ok;
}

/* ── A little JSON, read and written ──────────────────────────────────────────
 * The daemon's /api/ptp/status is one flat object:
 *   {"status": "locked", "gmid": "00-1D-C1-FF-FE-12-34-56", "jitter": 0}
 * A full parser is not worth linking for that. json_value() finds a key at any
 * depth and returns where its value starts, or NULL. It matches only a quoted
 * key followed by a colon, so a string VALUE that happens to equal the key is
 * not mistaken for it. */
static inline const char *json_value(const char *s, const char *key)
{
    size_t kl = strlen(key);
    const char *p;
    if (!s) return NULL;
    for (p = strchr(s, '"'); p; p = strchr(p + 1, '"')) {
        const char *q;
        if (strncmp(p + 1, key, kl) != 0 || p[1 + kl] != '"') continue;
        q = p + 2 + kl;
        while (isspace((unsigned char)*q)) q++;
        if (*q != ':') continue;
        q++;
        while (isspace((unsigned char)*q)) q++;
        return q;
    }
    return NULL;
}

/* Copies a JSON string value, keeping only the characters `keep` accepts, so
 * nothing the daemon says can put a quote or a control character into the
 * status file. Returns 0, or -1 if the value is not a string. */
static inline int json_string(const char *v, char *out, size_t n,
                              int (*keep)(int))
{
    size_t o = 0;
    if (!v || *v != '"' || n == 0) { if (n) out[0] = 0; return -1; }
    for (v++; *v && *v != '"'; v++) {
        if (*v == '\\' && v[1]) v++;
        if (o + 1 < n && keep((unsigned char)*v)) out[o++] = *v;
    }
    out[o] = 0;
    return 0;
}

static inline int keep_word(int c) { return isalnum(c) || c == '_' || c == '-' || c == ' '; }
static inline int keep_gmid(int c) { return isxdigit(c) || c == '-' || c == ':'; }

typedef struct {
    int  reachable;          /* the daemon answered 200 */
    int  locked;
    char status[24];         /* the daemon's own word: locked, locking, unlocked */
    char gmid[40];
} ptp_info;

/* Only the word "locked" counts. aes67-daemon v4.0.2 answers with a string
 * (unlocked / locking / locked); anything else is treated as not locked, which
 * keeps the card closed rather than open and starved. */
static inline void ptp_parse(const char *body, ptp_info *out)
{
    out->locked = 0;
    out->status[0] = 0;
    out->gmid[0] = 0;
    if (json_string(json_value(body, "status"), out->status,
                    sizeof out->status, keep_word) == 0)
        out->locked = strcasecmp(out->status, "locked") == 0;
    json_string(json_value(body, "gmid"), out->gmid, sizeof out->gmid, keep_gmid);
}

/* Splits an HTTP response held in a NUL-terminated buffer. Returns the status
 * code and points *body at the body, or returns -1 if it is not HTTP. */
static inline int http_split(const char *buf, const char **body)
{
    int code;
    const char *b;
    *body = NULL;
    if (strncmp(buf, "HTTP/1.", 7) != 0 || !buf[7] || buf[8] != ' ') return -1;
    if (!isdigit((unsigned char)buf[9]) || !isdigit((unsigned char)buf[10]) ||
        !isdigit((unsigned char)buf[11])) return -1;
    code = (buf[9] - '0') * 100 + (buf[10] - '0') * 10 + (buf[11] - '0');
    b = strstr(buf, "\r\n\r\n");
    *body = b ? b + 4 : buf + strlen(buf);
    return code;
}

/* Escapes a string for the status file. Always NUL-terminates; truncates. */
static inline void json_escape(char *out, size_t n, const char *s)
{
    size_t o = 0;
    if (n == 0) return;
    for (; s && *s; s++) {
        unsigned char c = (unsigned char)*s;
        char tmp[8];
        size_t l;
        if (c == '"' || c == '\\') { tmp[0] = '\\'; tmp[1] = (char)c; l = 2; }
        else if (c < 0x20)         { snprintf(tmp, sizeof tmp, "\\u%04x", c); l = 6; }
        else                       { tmp[0] = (char)c; l = 1; }
        if (o + l >= n) break;
        memcpy(out + o, tmp, l);
        o += l;
    }
    out[o] = 0;
}

/* ── The channel setting ──────────────────────────────────────────────────────
 * The player writes "1" (mono) or "2" (stereo) to a small file; the bridge
 * re-reads it every second, so changing it needs no restart. Anything it does
 * not recognise - including a missing file - is stereo. */
static inline int parse_channels(const char *text)
{
    char w[16];
    size_t o = 0;
    if (!text) return 2;
    while (isspace((unsigned char)*text)) text++;
    while (*text && !isspace((unsigned char)*text) && o + 1 < sizeof w)
        w[o++] = (char)tolower((unsigned char)*text++);
    w[o] = 0;
    return (strcmp(w, "1") == 0 || strcmp(w, "mono") == 0) ? 1 : 2;
}

/* ── Samples onto the card ────────────────────────────────────────────────────
 * The card is opened with every channel it has - fewer can read silence or
 * shift the stride (SKILL.md section 7) - in its native format. The program
 * audio goes on channels 1 and 2 (index 0 and 1), or as one mono mix on
 * channel 1; every other channel carries digital silence. The AES67 source the
 * player creates maps exactly those channels.
 *
 * Mono is (L+R)/2: content that is already mono comes out unchanged, and a
 * full-scale stereo pair cannot clip. */
enum { FMT_S24_3LE, FMT_S32_LE, FMT_FLOAT_LE, FMT_S16_LE, FMT_COUNT };

static const char *const fmt_names[FMT_COUNT] = { "S24_3LE", "S32_LE", "FLOAT_LE", "S16_LE" };

static inline size_t fmt_bytes(int fmt)
{
    switch (fmt) {
    case FMT_S24_3LE: return 3;
    case FMT_S16_LE:  return 2;
    default:          return 4;
    }
}

/* Full scale is [-1, 1). NaN becomes silence rather than full-scale noise.
 * In double: 2^31 does not fit a float's mantissa. */
static inline int32_t float_to_int(float x, int bits)
{
    double scale = ldexp(1.0, bits - 1);
    double v;
    if (x != x) return 0;
    v = floor((double)x * scale + 0.5);
    if (v > scale - 1) v = scale - 1;
    if (v < -scale)    v = -scale;
    return (int32_t)v;
}

static inline void put_sample(uint8_t *d, int fmt, float x)
{
    int32_t v;
    switch (fmt) {
    case FMT_S24_3LE:
        v = float_to_int(x, 24);
        d[0] = (uint8_t)(v & 0xff); d[1] = (uint8_t)((v >> 8) & 0xff);
        d[2] = (uint8_t)((v >> 16) & 0xff);
        break;
    case FMT_S32_LE:
        v = float_to_int(x, 32);
        d[0] = (uint8_t)(v & 0xff); d[1] = (uint8_t)((v >> 8) & 0xff);
        d[2] = (uint8_t)((v >> 16) & 0xff); d[3] = (uint8_t)((uint32_t)v >> 24);
        break;
    case FMT_S16_LE:
        v = float_to_int(x, 16);
        d[0] = (uint8_t)(v & 0xff); d[1] = (uint8_t)((v >> 8) & 0xff);
        break;
    default: {
        union { float f; uint32_t u; } u;
        u.f = (x != x) ? 0.0f : x;
        d[0] = (uint8_t)(u.u & 0xff); d[1] = (uint8_t)((u.u >> 8) & 0xff);
        d[2] = (uint8_t)((u.u >> 16) & 0xff); d[3] = (uint8_t)(u.u >> 24);
    }
    }
}

/* stereo: n interleaved L/R frames. dst: n frames of card_ch channels. */
static inline void pack_block(uint8_t *dst, int fmt, unsigned card_ch,
                              const float *stereo, size_t n, int mono)
{
    size_t bs = fmt_bytes(fmt), f;
    memset(dst, 0, n * card_ch * bs);   /* zero is silence in all four formats */
    if (card_ch == 0) return;
    for (f = 0; f < n; f++) {
        float l = stereo[2 * f], r = stereo[2 * f + 1];
        uint8_t *d = dst + f * card_ch * bs;
        if (mono || card_ch == 1) {
            put_sample(d, fmt, 0.5f * (l + r));
        } else {
            put_sample(d, fmt, l);
            put_sample(d + bs, fmt, r);
        }
    }
}

/* ── A ring of stereo frames ──────────────────────────────────────────────────
 * One writer (the loopback thread) and one reader (the card thread), under the
 * engine's lock. The writer never discards old audio: when the ring is full it
 * drops what is arriving instead, and only the reader ever removes frames.
 * That keeps "peek, resample outside the lock, then pop what was used" correct
 * - an old frame cannot vanish between the peek and the pop. */
typedef struct {
    float  *buf;          /* cap * BRIDGE_IN_CH floats */
    size_t  cap, head, count;
} fring;

static inline size_t fring_push(fring *r, const float *src, size_t n)
{
    size_t i, room = r->cap - r->count;
    if (n > room) n = room;
    for (i = 0; i < n; i++) {
        size_t at = (r->head + r->count + i) % r->cap;
        r->buf[at * 2]     = src[i * 2];
        r->buf[at * 2 + 1] = src[i * 2 + 1];
    }
    r->count += n;
    return n;
}

static inline size_t fring_peek(const fring *r, float *dst, size_t n)
{
    size_t i;
    if (n > r->count) n = r->count;
    for (i = 0; i < n; i++) {
        size_t at = (r->head + i) % r->cap;
        dst[i * 2]     = r->buf[at * 2];
        dst[i * 2 + 1] = r->buf[at * 2 + 1];
    }
    return n;
}

static inline void fring_pop(fring *r, size_t n)
{
    if (n > r->count) n = r->count;
    r->head = (r->head + n) % r->cap;
    r->count -= n;
}

/* Keeps the newest n frames; returns how many were dropped. */
static inline size_t fring_keep_newest(fring *r, size_t n)
{
    size_t drop = r->count > n ? r->count - n : 0;
    fring_pop(r, drop);
    return drop;
}

/* ── Clock steering ───────────────────────────────────────────────────────────
 * The loopback runs on the Pi's own clock and the AES67 card on the network's
 * PTP clock. They differ by tens of ppm: 10 ppm is one sample every two
 * seconds, and a 120 ms buffer is gone in 3.3 hours (reference.md). So the
 * audio is resampled by a tiny, continuously steered amount (ASRC).
 *
 * What is steered on is the BUFFER LEVEL - the audio waiting between the two
 * clocks - not a measured rate. That plant is an integrator: one ppm of error
 * moves the level by 1/1000 ms per second. A PI controller on it is a second-
 * order loop with natural period T and damping z:
 *     k_i = 1000 w^2,  k_p = 2000 z w,  w = 2 pi / T        (reference.md)
 * PI, not I alone: an I-only loop swung to its clamp and under-ran 36 times in
 * four hours. With anti-windup, because draining a backlog legitimately sits at
 * the clamp for minutes and must not wind the integrator up.
 *
 * Output is ppm, positive = consume input faster. The resampler ratio
 * (output/input, libsamplerate's convention - the inverse is silent and wrong)
 * is 1 / (1 + ppm * 1e-6): see ppm_to_ratio. */
typedef struct {
    double target_ms;        /* the level held between the clocks */
    double kp, ki;           /* ppm per ms; ppm per ms per second */
    double clamp_ppm;
    double slew_ppm_s;       /* the most the output may move per second */
    double warmup_s;         /* ignored after a (re)start: priming transients */
    double clamp_alarm_s;    /* pinned this long = something is broken */
} pi_params;

typedef struct {
    pi_params p;
    double integ;            /* ppm */
    double out;              /* ppm, after the clamp and the slew limit */
    double warm;             /* warm-up seconds left */
    double pinned_s;         /* seconds at the clamp, less seconds off it */
} pi_ctl;

/* Defaults, and why they differ from the skill's aes67-srt numbers:
 *   target 60 ms - snd-aloop delivers every few milliseconds, steadily, where
 *     the skill's 120 ms covered PulseAudio's 30 ms gaps. The player tells mpv
 *     about this delay, so it costs no lip-sync.
 *   period 300 s, damping 0.8 - aes67-srt's 2000 s loop rides two network
 *     clocks and steers on a level quantised to 1 ms. Here one side is the
 *     Pi's system clock, which NTP can slew by hundreds of ppm, and the level
 *     is an average of a thousand exact frame counts a second, so it is not
 *     noisy. In the simulation a 480 ppm slew pulled the level down to 10 ms
 *     at 600 s; at 300 s it stayed within 7-11 ms of target.
 *   clamp +-500 ppm - the kernel's maximum NTP slew is 500 ppm, and the skill's
 *     PulseAudio-to-PTP bridge (the closest case to this one) used 500.
 *   slew 50 ppm/s - 1 ppm/s throttled the proportional term (reference.md).
 *   warm-up 2 s, clamp alarm after 600 s pinned (SKILL.md section 11). */
static inline pi_params pi_default_params(double target_ms)
{
    pi_params p;
    double period_s = 300.0, damping = 0.8, w = 2.0 * M_PI / period_s;
    p.target_ms     = target_ms;
    p.kp            = 2000.0 * damping * w;
    p.ki            = 1000.0 * w * w;
    p.clamp_ppm     = 500.0;
    p.slew_ppm_s    = 50.0;
    p.warmup_s      = 2.0;
    p.clamp_alarm_s = 600.0;
    return p;
}

static inline void pi_init(pi_ctl *c, pi_params p)
{
    memset(c, 0, sizeof *c);
    c->p = p;
    c->warm = p.warmup_s;
}

/* After the card reopens or the level re-primes. The integrator is kept: it
 * holds the clock offset between the two crystals, which outlives a reopen. */
static inline void pi_restart(pi_ctl *c) { c->warm = c->p.warmup_s; }

/* One step, normally once a second, with the level averaged over that second. */
static inline double pi_step(pi_ctl *c, double level_ms, double dt)
{
    double e, integ, want, lim = c->p.clamp_ppm, step;
    if (dt <= 0) return c->out;
    if (c->warm > 0) { c->warm -= dt; return c->out; }

    e = level_ms - c->p.target_ms;
    integ = c->integ + c->p.ki * e * dt;
    if (integ > lim)  integ = lim;
    if (integ < -lim) integ = -lim;
    want = c->p.kp * e + integ;
    if (want > lim) {
        want = lim;
        if (e > 0) integ = c->integ;        /* anti-windup */
    } else if (want < -lim) {
        want = -lim;
        if (e < 0) integ = c->integ;
    }
    c->integ = integ;

    step = c->p.slew_ppm_s * dt;
    if (want > c->out + step) want = c->out + step;
    if (want < c->out - step) want = c->out - step;
    c->out = want;

    /* Counts down off the clamp rather than resetting. An offset the loop
     * cannot follow fills the ring until an overrun trims it, every ten
     * minutes or so, and each trim takes the loop off the clamp for a few
     * seconds - a reset there kept the alarm from ever firing. */
    if (fabs(c->out) >= lim - 1e-9) c->pinned_s += dt;
    else c->pinned_s = c->pinned_s > dt ? c->pinned_s - dt : 0;
    return c->out;
}

static inline int pi_clamp_alarm(const pi_ctl *c)
{
    return c->pinned_s >= c->p.clamp_alarm_s;
}

static inline double ppm_to_ratio(double ppm) { return 1.0 / (1.0 + ppm * 1e-6); }

#endif /* SAW_BRIDGE_LOGIC_H */
