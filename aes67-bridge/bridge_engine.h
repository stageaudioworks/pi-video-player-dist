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
 * bridge_engine.h — the audio between the two clocks
 * =============================================================================
 * The loopback thread pushes what mpv played; the card thread pulls exactly
 * one card period at a time. In between: a ring, a resampler steered by the PI
 * loop in bridge_logic.h, and the bookkeeping that says how much audio is
 * really waiting.
 *
 * The resampler is passed in as two function pointers, so the test can drive
 * the engine with an exact fake that hoards input the way libsamplerate does,
 * as well as with libsamplerate itself (SKILL.md section 11: "Selftest the loop
 * with an exact fake resampler that hoards").
 */
#ifndef SAW_BRIDGE_ENGINE_H
#define SAW_BRIDGE_ENGINE_H

#include <pthread.h>
#include "bridge_logic.h"

/* ratio is output/input. Convert at most in_frames into at most out_frames of
 * interleaved stereo; report what was used and what was made. 0, or -1. */
typedef struct {
    void *ctx;
    int  (*process)(void *ctx, const float *in, long in_frames, float *out,
                    long out_frames, double ratio, long *used, long *gen);
    void (*reset)(void *ctx);
} resampler;

enum { ENG_PRIMING, ENG_RUNNING };

#define ENG_RING_FRAMES     BRIDGE_RATE          /* one second */
#define ENG_OVERRUN_MS      250                  /* above target: drop to target */
#define ENG_SCRATCH_FRAMES  2048
#define ENG_HELD_MAX        512.0

typedef struct {
    pthread_mutex_t lock;     /* the ring and the counters; never held to resample */
    fring  ring;
    float  ring_buf[ENG_RING_FRAMES * BRIDGE_IN_CH];
    float  scratch[ENG_SCRATCH_FRAMES * BRIDGE_IN_CH];

    resampler rs;
    pi_ctl pi;
    int    state;
    size_t target_frames, overrun_frames;

    /* Frames handed to the resampler and not yet given back, in input frames.
     * libsamplerate swallows whatever it is offered; steering on the ring alone
     * then reads 0 ms with no underruns while the loop swings +-300 ppm
     * (SKILL.md section 11). So it is offered only what each block needs, and
     * this counts what it kept: held += used - made / ratio. */
    double held;

    /* The level averaged over each second, sampled before every pull. */
    double level_sum;
    long   level_n;
    long   frames_since_step;

    /* Read by the status writer, under the lock. */
    double        level_ms, ppm;
    unsigned long underruns, overruns, input_dropped;
    int           clamp_alarm;
} engine;

/* The card thread owns the state; the status writer only reads it. */
static inline void eng_set_state(engine *e, int st)
{
    __atomic_store_n(&e->state, st, __ATOMIC_RELAXED);
}

static inline void engine_init(engine *e, resampler rs, int target_ms)
{
    memset(e, 0, sizeof *e);
    pthread_mutex_init(&e->lock, NULL);
    e->ring.buf = e->ring_buf;
    e->ring.cap = ENG_RING_FRAMES;
    e->rs = rs;
    e->target_frames  = (size_t)target_ms * BRIDGE_RATE / 1000;
    e->overrun_frames = e->target_frames + (size_t)ENG_OVERRUN_MS * BRIDGE_RATE / 1000;
    pi_init(&e->pi, pi_default_params(target_ms));
    e->state = ENG_PRIMING;
}

/* The loopback thread. Never blocks on the card: when the ring is full the
 * newest audio is the audio dropped (see fring). */
static inline void engine_push(engine *e, const float *frames, size_t n)
{
    size_t took;
    pthread_mutex_lock(&e->lock);
    took = fring_push(&e->ring, frames, n);
    e->input_dropped += n - took;
    pthread_mutex_unlock(&e->lock);
}

static inline void engine_reset_averages(engine *e)
{
    e->level_sum = 0;
    e->level_n = 0;
    e->frames_since_step = 0;
}

/* After the card (re)opens. Whatever piled up while it was closed is stale;
 * keep only the newest target's worth, so the delay is right from the start. */
static inline void engine_resume(engine *e)
{
    size_t have;
    pthread_mutex_lock(&e->lock);
    fring_keep_newest(&e->ring, e->target_frames);
    have = e->ring.count;
    pthread_mutex_unlock(&e->lock);
    e->rs.reset(e->rs.ctx);
    e->held = 0;
    eng_set_state(e, have >= e->target_frames ? ENG_RUNNING : ENG_PRIMING);
    pi_restart(&e->pi);
    engine_reset_averages(e);
}

/* The card thread: exactly n frames of interleaved stereo into out. */
static inline void engine_pull(engine *e, float *out, size_t n)
{
    size_t have, made = 0;
    double ratio = ppm_to_ratio(e->pi.out);
    int underrun = 0, overrun = 0, tries;

    pthread_mutex_lock(&e->lock);
    have = e->ring.count;
    if (e->state == ENG_RUNNING && have > e->overrun_frames) {
        /* A backlog the loop could take minutes to drain: drop to target. */
        fring_keep_newest(&e->ring, e->target_frames);
        have = e->ring.count;
        overrun = 1;
    }
    pthread_mutex_unlock(&e->lock);
    if (overrun) {
        /* The jump is deliberate; do not play out older audio the resampler
         * was still holding first. */
        e->rs.reset(e->rs.ctx);
        e->held = 0;
    }

    if (e->state == ENG_PRIMING) {
        /* Silence until a target's worth has arrived. Not trimmed when it gets
         * there: with bursty input that would discard audio every refill. */
        if (have < e->target_frames) {
            memset(out, 0, n * BRIDGE_IN_CH * sizeof(float));
            return;
        }
        eng_set_state(e, ENG_RUNNING);
        pi_restart(&e->pi);
        engine_reset_averages(e);
    }

    e->level_sum += (double)have + e->held;
    e->level_n++;

    for (tries = 0; made < n && tries < 8; tries++) {
        long used = 0, gen = 0;
        /* What this block still needs, less what the resampler already holds.
         * Offering a block's worth regardless is the trap: a resampler that
         * takes everything it is given then grows by the margin every block.
         * Nothing needed still calls it, with no input, so it drains. A second
         * pass means the estimate was optimistic: offer a few frames anyway. */
        double need = (double)(n - made) / ratio - e->held;
        size_t want = need > 0 ? (size_t)ceil(need) + 2 : 0, got;
        if (tries > 0 && want < 4) want = 4;
        if (want > ENG_SCRATCH_FRAMES) want = ENG_SCRATCH_FRAMES;

        pthread_mutex_lock(&e->lock);
        got = fring_peek(&e->ring, e->scratch, want);
        pthread_mutex_unlock(&e->lock);

        if (e->rs.process(e->rs.ctx, e->scratch, (long)got,
                          out + made * BRIDGE_IN_CH, (long)(n - made),
                          ratio, &used, &gen) != 0 ||
            (used == 0 && gen == 0 && got == 0 && want > 0)) {
            underrun = 1;               /* nothing to give it, nothing in it */
            break;
        }
        pthread_mutex_lock(&e->lock);
        fring_pop(&e->ring, (size_t)used);
        pthread_mutex_unlock(&e->lock);

        /* Bounded: the gliding ratio makes this inexact by a few thousandths
         * of a frame per change, which must not add up over a long show. */
        e->held += (double)used - (double)gen / ratio;
        if (e->held < 0) e->held = 0;
        if (e->held > ENG_HELD_MAX) e->held = ENG_HELD_MAX;
        made += (size_t)gen;
    }
    if (made < n) underrun = 1;

    if (underrun) {
        /* Pad with silence - never repeat old audio, 48 held samples is a DC
         * step (reference.md) - and wait for a target's worth again. */
        memset(out + made * BRIDGE_IN_CH, 0, (n - made) * BRIDGE_IN_CH * sizeof(float));
        e->rs.reset(e->rs.ctx);
        e->held = 0;
        eng_set_state(e, ENG_PRIMING);
    }

    e->frames_since_step += (long)n;
    if (!underrun && e->frames_since_step >= BRIDGE_RATE && e->level_n > 0) {
        double avg_ms = e->level_sum / (double)e->level_n * 1000.0 / BRIDGE_RATE;
        double dt = (double)e->frames_since_step / BRIDGE_RATE;
        pi_step(&e->pi, avg_ms, dt);
        pthread_mutex_lock(&e->lock);
        e->level_ms = avg_ms;
        e->ppm = e->pi.out;
        e->clamp_alarm = pi_clamp_alarm(&e->pi);
        pthread_mutex_unlock(&e->lock);
        engine_reset_averages(e);
    }

    if (underrun || overrun) {
        pthread_mutex_lock(&e->lock);
        e->underruns += (unsigned long)underrun;
        e->overruns  += (unsigned long)overrun;
        pthread_mutex_unlock(&e->lock);
        engine_reset_averages(e);
    }
}

typedef struct {
    int           running;      /* ENG_RUNNING, not priming */
    double        level_ms, ppm;
    unsigned long underruns, overruns, input_dropped;
    int           clamp_alarm;
} engine_stats;

static inline engine_stats engine_snapshot(engine *e)
{
    engine_stats s;
    pthread_mutex_lock(&e->lock);
    s.running       = __atomic_load_n(&e->state, __ATOMIC_RELAXED) == ENG_RUNNING;
    s.level_ms      = e->level_ms;
    s.ppm           = e->ppm;
    s.underruns     = e->underruns;
    s.overruns      = e->overruns;
    s.input_dropped = e->input_dropped;
    s.clamp_alarm   = e->clamp_alarm;
    pthread_mutex_unlock(&e->lock);
    return s;
}

#endif /* SAW_BRIDGE_ENGINE_H */
