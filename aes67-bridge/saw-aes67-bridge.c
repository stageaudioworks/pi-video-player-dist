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
 * saw-aes67-bridge — keeps the AES67 card fed, whatever mpv is doing
 * =============================================================================
 *   mpv --> SAWLoop (snd-aloop) --> this bridge --> RAVENNA card --> network
 *
 * EXPERIMENTAL: built from the aes67-integration skill; not yet run against a
 * PTP grandmaster and a receiver. Built and started by install-aes67.sh.
 *
 * Why it exists. The skill's first rule for the RAVENNA card is that it stays
 * open and is written continuously - close it, or stop feeding it, and every
 * receiver drops the stream and has to find it again. mpv cannot keep that
 * rule: it closes its audio device on every restart (every trigger clip, every
 * return to the loop) and for clips with no audio. And mpv paces its video by
 * its audio device, so mpv writing to the card directly would freeze the
 * picture whenever PTP did.
 *
 * So mpv plays into one end of a kernel loopback, which runs on the Pi's own
 * clock, never blocks and never cares whether anyone is listening. This bridge
 * reads the other end and owns the card for good:
 *   - the card is opened once PTP is locked and kept open, fed silence when
 *     mpv is between clips (the loopback delivers silence then by itself);
 *   - the Pi's clock and the network's PTP clock are bridged by a resampler
 *     steered on the buffer level (bridge_logic.h);
 *   - no wait is unbounded, a stalled card is reopened, and nothing the card
 *     does can reach mpv - the bridge can stop, crash or restart and the
 *     picture carries on.
 *
 * Status goes to a JSON file in /run (RAM, not the SD card) once a second for
 * the player's web UI. Log lines go to stderr with a syslog priority prefix,
 * which the journal understands.
 */
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <samplerate.h>

#include <arpa/inet.h>
#include <errno.h>
#include <getopt.h>
#include <malloc.h>
#include <netinet/in.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include "bridge_logic.h"
#include "bridge_engine.h"

#define BRIDGE_VERSION "1"

/* ── Options ──────────────────────────────────────────────────────────────── */
static struct {
    const char *card_dev, *loop_dev, *channels_file, *status_file;
    int api_port, card_channels, target_ms, ignore_ptp, tone;
    unsigned period, buffer;
} opt = {
    .card_dev      = "hw:CARD=RAVENNA,DEV=0",
    .loop_dev      = "plughw:CARD=SAWLoop,DEV=1,SUBDEV=0",
    .channels_file = "/var/lib/saw-aes67/channels",
    .status_file   = "/run/saw-aes67/bridge.json",
    .api_port      = 8080,
    .card_channels = 0,           /* 0 = every channel the card has */
    .target_ms     = 60,
    /* 1 ms periods in a 32 ms buffer: the skill's measured clean setting on
     * this driver (48/384 distorted, 48/768 less so, 48/1536 clean). */
    .period        = 48,
    .buffer        = 1536,
};

/* ── Shared state ─────────────────────────────────────────────────────────── */
static volatile sig_atomic_t g_stop;
static int g_card_may_open;            /* PTP gate; atomics via __atomic_* */
static int g_mono;

static engine g_eng;
static SRC_STATE *g_src;

static pthread_mutex_t g_st_lock = PTHREAD_MUTEX_INITIALIZER;
static struct {
    ptp_info ptp;
    int card_open, loop_open, card_channels, card_fmt;
    unsigned period, buffer;
    unsigned long xruns, stalls, reopens, card_open_failures;
} g_st = { .card_fmt = -1 };

/* ── Small helpers ────────────────────────────────────────────────────────── */
static int64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void sleep_ms(long ms)
{
    struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR && !g_stop) {}
}

/* "<N>" is a syslog priority the journal reads from stderr: 3 err, 4 warning,
 * 6 info. */
static void logmsg(int prio, const char *fmt, ...)
{
    va_list ap;
    char line[512];
    va_start(ap, fmt);
    vsnprintf(line, sizeof line, fmt, ap);
    va_end(ap);
    fprintf(stderr, "<%d>%s\n", prio, line);
}

/* A thread doing audio must not wait behind the rest of the system. Allowed by
 * LimitRTPRIO in the unit; without it the bridge still runs, and says so. */
static void go_realtime(const char *who, int prio)
{
    struct sched_param sp = { .sched_priority = prio };
    int rc = pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp);
    if (rc != 0)
        logmsg(4, "%s thread: no real-time priority (%s) - running without it",
               who, strerror(rc));
}

/* ── libsamplerate as the engine's resampler ──────────────────────────────── */
/* src_process with a new ratio each call glides to it; src_set_ratio would
 * step the pitch (reference.md). SINC_FASTEST: 11% of a Pi 5 core at 64
 * channels in the skill's measurements - two channels here. */
static int src_proc(void *ctx, const float *in, long in_frames, float *out,
                    long out_frames, double ratio, long *used, long *gen)
{
    SRC_DATA d;
    memset(&d, 0, sizeof d);
    d.data_in = in;
    d.input_frames = in_frames;
    d.data_out = out;
    d.output_frames = out_frames;
    d.src_ratio = ratio;
    d.end_of_input = 0;
    if (src_process((SRC_STATE *)ctx, &d) != 0) return -1;
    *used = d.input_frames_used;
    *gen = d.output_frames_gen;
    return 0;
}

static void src_rst(void *ctx) { src_reset((SRC_STATE *)ctx); }

/* ── The loopback: mpv's audio in ─────────────────────────────────────────── */
/* plughw, so that if mpv happens to open its end first with another format,
 * the loopback's format rules still give this side 48 kHz float stereo. The
 * player asks mpv for exactly that, so normally nothing is converted. */
static snd_pcm_t *open_loop(void)
{
    snd_pcm_t *pcm = NULL;
    snd_pcm_hw_params_t *hw;
    snd_pcm_sw_params_t *sw;
    unsigned rate = BRIDGE_RATE, ptime = 5000, btime = 200000;
    snd_pcm_uframes_t psize = 0, bsize = 0;
    int rc;

    if ((rc = snd_pcm_open(&pcm, opt.loop_dev, SND_PCM_STREAM_CAPTURE, SND_PCM_NONBLOCK)) < 0) {
        logmsg(3, "loopback %s: cannot open (%s)", opt.loop_dev, snd_strerror(rc));
        return NULL;
    }
    snd_pcm_hw_params_alloca(&hw);
    snd_pcm_hw_params_any(pcm, hw);
    if ((rc = snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED)) < 0 ||
        (rc = snd_pcm_hw_params_set_format(pcm, hw, SND_PCM_FORMAT_FLOAT_LE)) < 0 ||
        (rc = snd_pcm_hw_params_set_channels(pcm, hw, BRIDGE_IN_CH)) < 0 ||
        (rc = snd_pcm_hw_params_set_rate(pcm, hw, rate, 0)) < 0) {
        logmsg(3, "loopback %s: will not do 48 kHz float stereo (%s)", opt.loop_dev, snd_strerror(rc));
        snd_pcm_close(pcm);
        return NULL;
    }
    snd_pcm_hw_params_set_period_time_near(pcm, hw, &ptime, NULL);
    snd_pcm_hw_params_set_buffer_time_near(pcm, hw, &btime, NULL);
    if ((rc = snd_pcm_hw_params(pcm, hw)) < 0) {
        logmsg(3, "loopback %s: hw params refused (%s)", opt.loop_dev, snd_strerror(rc));
        snd_pcm_close(pcm);
        return NULL;
    }
    snd_pcm_get_params(pcm, &bsize, &psize);
    snd_pcm_sw_params_alloca(&sw);
    snd_pcm_sw_params_current(pcm, sw);
    snd_pcm_sw_params_set_avail_min(pcm, sw, psize);
    snd_pcm_sw_params_set_start_threshold(pcm, sw, 1);
    snd_pcm_sw_params(pcm, sw);
    if ((rc = snd_pcm_prepare(pcm)) < 0 || (rc = snd_pcm_start(pcm)) < 0) {
        logmsg(3, "loopback %s: cannot start (%s)", opt.loop_dev, snd_strerror(rc));
        snd_pcm_close(pcm);
        return NULL;
    }
    logmsg(6, "loopback %s open: period %lu, buffer %lu frames", opt.loop_dev,
           (unsigned long)psize, (unsigned long)bsize);
    return pcm;
}

/* --tone: a 1 kHz left / 2 kHz right test signal at -20 dBFS in place of the
 * loopback, paced by the system clock the way the loopback is. Proves the
 * network side on the bench with no player involved. */
static void *tone_thread(void *arg)
{
    float buf[240 * BRIDGE_IN_CH];
    double ph = 0;
    int64_t next = now_ms();
    (void)arg;
    pthread_mutex_lock(&g_st_lock); g_st.loop_open = 1; pthread_mutex_unlock(&g_st_lock);
    while (!g_stop) {
        int i;
        for (i = 0; i < 240; i++, ph += 1.0 / BRIDGE_RATE) {
            buf[2 * i]     = (float)(0.1 * sin(2 * M_PI * 1000 * ph));
            buf[2 * i + 1] = (float)(0.1 * sin(2 * M_PI * 2000 * ph));
        }
        if (ph > 1.0) ph -= 1.0;
        engine_push(&g_eng, buf, 240);
        next += 5;
        {
            int64_t d = next - now_ms();
            if (d > 0) sleep_ms((long)d);
        }
    }
    pthread_mutex_lock(&g_st_lock); g_st.loop_open = 0; pthread_mutex_unlock(&g_st_lock);
    return NULL;
}

static void *loop_thread(void *arg)
{
    snd_pcm_t *pcm = NULL;
    float buf[1024 * BRIDGE_IN_CH];
    int64_t next_open = 0, last_data = 0;
    int failures = 0;
    (void)arg;
    go_realtime("loopback", 79);

    while (!g_stop) {
        snd_pcm_sframes_t avail, got;
        if (!pcm) {
            if (now_ms() < next_open) { sleep_ms(100); continue; }
            pcm = open_loop();
            if (!pcm) {
                failures++;
                next_open = next_open_ms(now_ms(), 0, failures, 0);
                continue;
            }
            failures = 0;
            last_data = now_ms();
            pthread_mutex_lock(&g_st_lock); g_st.loop_open = 1; pthread_mutex_unlock(&g_st_lock);
        }

        snd_pcm_wait(pcm, WAIT_SLICE_MS);
        avail = snd_pcm_avail_update(pcm);
        if (avail < 0) {
            /* Overrun (the bridge fell behind): restart this side, carry on. */
            if (snd_pcm_recover(pcm, (int)avail, 1) < 0 || snd_pcm_start(pcm) < 0) {
                logmsg(4, "loopback: %s - reopening", snd_strerror((int)avail));
                snd_pcm_close(pcm);
                pcm = NULL;
                pthread_mutex_lock(&g_st_lock); g_st.loop_open = 0; pthread_mutex_unlock(&g_st_lock);
                next_open = next_open_ms(now_ms(), 0, 1, 0);
            }
            continue;
        }
        if (avail == 0) {
            /* The loopback delivers silence even with nothing playing, so
             * nothing at all for this long means it has stopped. */
            if (now_ms() - last_data >= STALL_MS) {
                logmsg(4, "loopback: no audio for %d ms - reopening", STALL_MS);
                snd_pcm_close(pcm);
                pcm = NULL;
                pthread_mutex_lock(&g_st_lock); g_st.loop_open = 0; pthread_mutex_unlock(&g_st_lock);
                next_open = now_ms();
            }
            continue;
        }
        if (avail > 1024) avail = 1024;
        got = snd_pcm_readi(pcm, buf, (snd_pcm_uframes_t)avail);
        if (got > 0) {
            last_data = now_ms();
            engine_push(&g_eng, buf, (size_t)got);
        } else if (got < 0 && got != -EAGAIN) {
            snd_pcm_recover(pcm, (int)got, 1);
            snd_pcm_start(pcm);
        }
    }
    if (pcm) snd_pcm_close(pcm);
    pthread_mutex_lock(&g_st_lock); g_st.loop_open = 0; pthread_mutex_unlock(&g_st_lock);
    return NULL;
}

/* ── The card: the network's audio out ────────────────────────────────────── */
typedef struct {
    snd_pcm_t *pcm;
    int fmt;
    unsigned ch;
    snd_pcm_uframes_t period, buffer;
    float *stereo;      /* period frames of L/R */
    uint8_t *packed;    /* period frames of every card channel */
} card_t;

static void card_close(card_t *c)
{
    if (c->pcm) {
        snd_pcm_drop(c->pcm);
        snd_pcm_close(c->pcm);
    }
    free(c->stereo);
    free(c->packed);
    memset(c, 0, sizeof *c);
    pthread_mutex_lock(&g_st_lock); g_st.card_open = 0; pthread_mutex_unlock(&g_st_lock);
}

static const snd_pcm_format_t alsa_fmt[FMT_COUNT] = {
    SND_PCM_FORMAT_S24_3LE, SND_PCM_FORMAT_S32_LE, SND_PCM_FORMAT_FLOAT_LE, SND_PCM_FORMAT_S16_LE
};

/* A full buffer of silence, then start. The driver's tick only moves samples
 * while the stream is triggered, and a PREPARED stream sits at hw_ptr 0 for
 * ever with no error; an empty buffer under-runs on the first tick and the
 * driver stops (SKILL.md section 7). */
static int card_prime(card_t *c)
{
    snd_pcm_uframes_t left = c->buffer;
    int rc;
    if ((rc = snd_pcm_prepare(c->pcm)) < 0) return rc;
    memset(c->packed, 0, c->period * c->ch * fmt_bytes(c->fmt));
    while (left > 0) {
        snd_pcm_uframes_t n = left < c->period ? left : c->period;
        snd_pcm_sframes_t w = snd_pcm_writei(c->pcm, c->packed, n);
        if (w == -EAGAIN) break;          /* full already */
        if (w < 0) return (int)w;
        left -= (snd_pcm_uframes_t)w;
    }
    if (snd_pcm_state(c->pcm) == SND_PCM_STATE_PREPARED && (rc = snd_pcm_start(c->pcm)) < 0)
        return rc;
    return 0;
}

static int card_open(card_t *c)
{
    snd_pcm_hw_params_t *hw;
    snd_pcm_sw_params_t *sw;
    snd_pcm_uframes_t period = opt.period, buffer = opt.buffer;
    unsigned ch = 0, maxch = 0;
    int rc, f;

    memset(c, 0, sizeof *c);
    c->fmt = -1;
    if ((rc = snd_pcm_open(&c->pcm, opt.card_dev, SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK)) < 0) {
        c->pcm = NULL;
        return rc;
    }
    snd_pcm_hw_params_alloca(&hw);
    snd_pcm_hw_params_any(c->pcm, hw);
    snd_pcm_hw_params_set_access(c->pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED);

    /* Native formats first; S24_3LE is the one proven at 64 channels on a Pi 5. */
    for (f = 0; f < FMT_COUNT; f++)
        if (snd_pcm_hw_params_test_format(c->pcm, hw, alsa_fmt[f]) == 0) break;
    if (f == FMT_COUNT) { rc = -EINVAL; logmsg(3, "card: no usable sample format"); goto fail; }
    c->fmt = f;
    snd_pcm_hw_params_set_format(c->pcm, hw, alsa_fmt[f]);

    /* Every channel a real card has. Anything that is not a hardware device -
     * plughw:, null - claims to take any number, so it gets stereo: that is
     * what --device plughw:CARD=vc4hdmi0 on the bench means. */
    snd_pcm_hw_params_get_channels_max(hw, &maxch);
    if (opt.card_channels > 0)                     ch = (unsigned)opt.card_channels;
    else if (snd_pcm_type(c->pcm) == SND_PCM_TYPE_HW) ch = maxch;
    else                                           ch = 2;
    if (ch == 0 || (rc = snd_pcm_hw_params_set_channels(c->pcm, hw, ch)) < 0) {
        logmsg(3, "card: cannot open with %u channels (%s)", ch, snd_strerror(rc));
        if (rc >= 0) rc = -EINVAL;
        goto fail;
    }
    c->ch = ch;
    if ((rc = snd_pcm_hw_params_set_rate(c->pcm, hw, BRIDGE_RATE, 0)) < 0) {
        logmsg(3, "card: will not run at 48 kHz (%s)", snd_strerror(rc));
        goto fail;
    }
    snd_pcm_hw_params_set_period_size_near(c->pcm, hw, &period, NULL);
    snd_pcm_hw_params_set_buffer_size_near(c->pcm, hw, &buffer);
    if ((rc = snd_pcm_hw_params(c->pcm, hw)) < 0) {
        logmsg(3, "card: hw params refused (%s)", snd_strerror(rc));
        goto fail;
    }
    snd_pcm_get_params(c->pcm, &c->buffer, &c->period);
    if (c->period == 0 || c->buffer < c->period) { rc = -EINVAL; goto fail; }

    snd_pcm_sw_params_alloca(&sw);
    snd_pcm_sw_params_current(c->pcm, sw);
    snd_pcm_sw_params_set_avail_min(c->pcm, sw, c->period);
    snd_pcm_sw_params_set_start_threshold(c->pcm, sw, c->buffer);   /* started by card_prime */
    snd_pcm_sw_params(c->pcm, sw);

    c->stereo = calloc(c->period * BRIDGE_IN_CH, sizeof(float));
    c->packed = calloc(c->period * c->ch, fmt_bytes(c->fmt));
    if (!c->stereo || !c->packed) { rc = -ENOMEM; goto fail; }
    if ((rc = card_prime(c)) < 0) {
        logmsg(3, "card: cannot start (%s)", snd_strerror(rc));
        goto fail;
    }

    /* *_near overwrites what was asked for: log both. */
    logmsg(6, "card %s open: %u ch %s, period %lu (asked %u), buffer %lu (asked %u), %s",
           opt.card_dev, c->ch, fmt_names[c->fmt], (unsigned long)c->period, opt.period,
           (unsigned long)c->buffer, opt.buffer, snd_pcm_state_name(snd_pcm_state(c->pcm)));
    pthread_mutex_lock(&g_st_lock);
    g_st.card_open = 1;
    g_st.card_channels = (int)c->ch;
    g_st.card_fmt = c->fmt;
    g_st.period = (unsigned)c->period;
    g_st.buffer = (unsigned)c->buffer;
    pthread_mutex_unlock(&g_st_lock);
    return 0;

fail:
    snd_pcm_close(c->pcm);
    free(c->stereo);
    free(c->packed);
    memset(c, 0, sizeof *c);
    return rc;
}

static void count(unsigned long *what)
{
    pthread_mutex_lock(&g_st_lock);
    (*what)++;
    pthread_mutex_unlock(&g_st_lock);
}

static void *card_thread(void *arg)
{
    card_t c;
    int64_t next_open = 0, last_open = 0, last_progress = 0, last_fail_log = -60000;
    int failures = 0, ever_opened = 0;
    (void)arg;
    memset(&c, 0, sizeof c);
    go_realtime("card", 80);

    while (!g_stop) {
        snd_pcm_sframes_t avail;
        int64_t t = now_ms();

        if (!opt.ignore_ptp && !__atomic_load_n(&g_card_may_open, __ATOMIC_RELAXED)) {
            if (c.pcm) {
                logmsg(4, "PTP not locked - closing the card until it is");
                card_close(&c);
            }
            sleep_ms(100);
            continue;
        }

        if (!c.pcm) {
            int rc;
            if (t < next_open) { sleep_ms(50); continue; }
            rc = card_open(&c);
            if (rc < 0) {
                failures++;
                count(&g_st.card_open_failures);
                if (t - last_fail_log >= 60000) {   /* not every 10 s for ever */
                    logmsg(3, "card %s: cannot open (%s) - retrying", opt.card_dev, snd_strerror(rc));
                    last_fail_log = t;
                }
                next_open = next_open_ms(t, last_open, failures, 0);
                continue;
            }
            if (ever_opened) count(&g_st.reopens);
            ever_opened = 1;
            failures = 0;
            last_open = last_progress = now_ms();
            engine_resume(&g_eng);
        }

        snd_pcm_wait(c.pcm, WAIT_SLICE_MS);
        avail = snd_pcm_avail_update(c.pcm);
        if (avail == -EPIPE) {
            /* XRUN: prepare stops this direction's tick, so prepare, re-prime
             * and start - never just prepare (SKILL.md section 7). */
            count(&g_st.xruns);
            if (card_prime(&c) < 0) {
                logmsg(4, "card: could not recover from an underrun - reopening");
                card_close(&c);
                next_open = next_open_ms(now_ms(), last_open, 1, 0);
            }
            continue;
        }
        if (avail < 0) {
            if (snd_pcm_recover(c.pcm, (int)avail, 1) < 0 || card_prime(&c) < 0) {
                logmsg(4, "card: %s - reopening", snd_strerror((int)avail));
                card_close(&c);
                next_open = next_open_ms(now_ms(), last_open, 1, 0);
            }
            continue;
        }
        if ((snd_pcm_uframes_t)avail < c.period) {
            if (now_ms() - last_progress >= STALL_MS) {
                /* The daemon restarting freezes the engine while the stream
                 * still says RUNNING. Reopen, but not more than every 15 s. */
                count(&g_st.stalls);
                logmsg(4, "card: no room for %d ms - closing and reopening", STALL_MS);
                card_close(&c);
                next_open = next_open_ms(now_ms(), last_open, 0, 1);
            }
            continue;
        }
        last_progress = now_ms();

        while ((snd_pcm_uframes_t)avail >= c.period && !g_stop) {
            snd_pcm_sframes_t w;
            engine_pull(&g_eng, c.stereo, c.period);
            pack_block(c.packed, c.fmt, c.ch, c.stereo, c.period,
                       __atomic_load_n(&g_mono, __ATOMIC_RELAXED));
            w = snd_pcm_writei(c.pcm, c.packed, c.period);
            if (w < 0) break;            /* handled on the next pass */
            avail -= w;
        }
    }
    if (c.pcm) card_close(&c);
    return NULL;
}

/* ── The daemon: is PTP locked? ───────────────────────────────────────────── */
/* GET on loopback, bounded by socket timeouts. Returns the HTTP status, or -1. */
static int http_get(int port, const char *path, char *buf, size_t n)
{
    struct sockaddr_in a;
    struct timeval tv = { 0, 500000 };
    char req[256];
    size_t have = 0;
    int fd, len;

    buf[0] = 0;
    if ((fd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0)) < 0) return -1;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)port);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(fd, (struct sockaddr *)&a, sizeof a) < 0) { close(fd); return -1; }
    len = snprintf(req, sizeof req,
                   "GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n", path);
    if (send(fd, req, (size_t)len, MSG_NOSIGNAL) != len) { close(fd); return -1; }
    while (have + 1 < n) {
        ssize_t r = recv(fd, buf + have, n - 1 - have, 0);
        if (r <= 0) break;
        have += (size_t)r;
    }
    close(fd);
    buf[have] = 0;
    {
        const char *body;
        int code = http_split(buf, &body);
        if (code < 0) return -1;
        memmove(buf, body, strlen(body) + 1);
        return code;
    }
}

static void poll_ptp(ptp_gate *gate)
{
    char buf[4096];
    ptp_info p;
    int code = http_get(opt.api_port, "/api/ptp/status", buf, sizeof buf);
    memset(&p, 0, sizeof p);
    p.reachable = code == 200;
    if (p.reachable) ptp_parse(buf, &p);
    {
        int was = __atomic_load_n(&g_card_may_open, __ATOMIC_RELAXED);
        int now = ptp_gate_update(gate, p.locked);
        if (now != was) {
            if (now) logmsg(6, "PTP locked to %s - opening the card", p.gmid[0] ? p.gmid : "?");
            else     logmsg(4, "PTP %s - the card will close", p.reachable ? (p.status[0] ? p.status : "?")
                                                                       : "daemon not answering");
        }
        __atomic_store_n(&g_card_may_open, now, __ATOMIC_RELAXED);
    }
    pthread_mutex_lock(&g_st_lock);
    if (strcmp(g_st.ptp.gmid, p.gmid) != 0 && gmid_known(g_st.ptp.gmid) && gmid_known(p.gmid))
        logmsg(4, "PTP grandmaster changed: %s -> %s", g_st.ptp.gmid, p.gmid);
    g_st.ptp = p;
    pthread_mutex_unlock(&g_st_lock);
}

static void read_channels(void)
{
    char buf[32] = "";
    FILE *f = fopen(opt.channels_file, "r");
    int mono, was;
    if (f) {
        size_t r = fread(buf, 1, sizeof buf - 1, f);
        buf[r] = 0;
        fclose(f);
    }
    mono = parse_channels(buf) == 1;
    was = __atomic_exchange_n(&g_mono, mono, __ATOMIC_RELAXED);
    if (was != mono) logmsg(6, "output: %s", mono ? "mono (L+R)/2 on channel 1" : "stereo on channels 1-2");
}

/* ── Status for the player ────────────────────────────────────────────────── */
/* In /run, which is RAM: once a second for the life of the box would be a
 * steady stream of writes to the SD card anywhere else. */
static void write_status(const char *state)
{
    char tmp[512], dev[256], loopdev[256], gm[64], word[48];
    engine_stats es = engine_snapshot(&g_eng);
    FILE *f;
    int card_open, loop_open, ch, fmt;
    unsigned period, buffer;
    unsigned long xruns, stalls, reopens, cfails;
    ptp_info p;

    pthread_mutex_lock(&g_st_lock);
    p = g_st.ptp; card_open = g_st.card_open; loop_open = g_st.loop_open;
    ch = g_st.card_channels; fmt = g_st.card_fmt; period = g_st.period; buffer = g_st.buffer;
    xruns = g_st.xruns; stalls = g_st.stalls; reopens = g_st.reopens; cfails = g_st.card_open_failures;
    pthread_mutex_unlock(&g_st_lock);

    json_escape(dev, sizeof dev, opt.card_dev);
    json_escape(loopdev, sizeof loopdev, opt.tone ? "tone" : opt.loop_dev);
    json_escape(gm, sizeof gm, p.gmid);
    json_escape(word, sizeof word, p.status);

    snprintf(tmp, sizeof tmp, "%s.tmp", opt.status_file);
    if (!(f = fopen(tmp, "w"))) return;
    fprintf(f,
        "{\"version\": \"%s\", \"state\": \"%s\", \"updated\": %lld,\n"
        " \"ptp\": {\"checked\": %s, \"reachable\": %s, \"locked\": %s, \"status\": \"%s\", \"gmid\": \"%s\"},\n"
        " \"card\": {\"open\": %s, \"device\": \"%s\", \"channels\": %d, \"format\": \"%s\","
        " \"period\": %u, \"buffer\": %u},\n"
        " \"input\": {\"open\": %s, \"device\": \"%s\"},\n"
        " \"mode\": \"%s\", \"target_ms\": %d, \"level_ms\": %.1f, \"latency_ms\": %.1f,\n"
        " \"ppm\": %.2f, \"clamp_alarm\": %s,\n"
        " \"underruns\": %lu, \"overruns\": %lu, \"input_dropped\": %lu,\n"
        " \"xruns\": %lu, \"stalls\": %lu, \"reopens\": %lu, \"open_failures\": %lu}\n",
        BRIDGE_VERSION, state, (long long)time(NULL),
        opt.ignore_ptp ? "false" : "true", p.reachable ? "true" : "false",
        p.locked ? "true" : "false", word, gm,
        card_open ? "true" : "false", dev, ch, fmt >= 0 ? fmt_names[fmt] : "", period, buffer,
        loop_open ? "true" : "false", loopdev,
        __atomic_load_n(&g_mono, __ATOMIC_RELAXED) ? "mono" : "stereo",
        opt.target_ms, es.level_ms,
        /* What mpv should be told: the ring plus the card's own buffer, which
         * is kept full. The receiver's latency is on top and is not known here. */
        card_open ? es.level_ms + buffer * 1000.0 / BRIDGE_RATE : 0.0,
        es.ppm, es.clamp_alarm ? "true" : "false",
        es.underruns, es.overruns, es.input_dropped, xruns, stalls, reopens, cfails);
    if (fclose(f) == 0) rename(tmp, opt.status_file);
    else unlink(tmp);
}

static const char *state_word(void)
{
    engine_stats es = engine_snapshot(&g_eng);
    int card_open, reachable;
    pthread_mutex_lock(&g_st_lock);
    card_open = g_st.card_open;
    reachable = g_st.ptp.reachable;
    pthread_mutex_unlock(&g_st_lock);
    if (g_stop) return "stopped";
    if (!opt.ignore_ptp && !__atomic_load_n(&g_card_may_open, __ATOMIC_RELAXED))
        return reachable ? "waiting_for_ptp" : "no_daemon";
    if (!card_open) return "opening_card";
    return es.running ? "running" : "priming";
}

/* ── main ─────────────────────────────────────────────────────────────────── */
static void on_signal(int sig) { (void)sig; g_stop = 1; }

static void usage(FILE *to)
{
    fprintf(to,
        "saw-aes67-bridge " BRIDGE_VERSION " - feeds the AES67 card from the player's loopback\n"
        "  --device DEV         card to feed            (default %s)\n"
        "  --loop DEV           loopback to read        (default %s)\n"
        "  --api-port N         aes67-daemon REST port  (default %d)\n"
        "  --card-channels N    open the card with N channels (default: all it has)\n"
        "  --target-ms N        audio held between the clocks (default %d, 20-500)\n"
        "  --period N --buffer N  card period/buffer in frames (default %u/%u)\n"
        "  --channels-file F    \"1\" = mono, else stereo (default %s)\n"
        "  --status-file F      JSON status, rewritten each second (default %s)\n"
        "  --ignore-ptp         open the card without asking the daemon (bench only)\n"
        "  --tone               1 kHz left / 2 kHz right at -20 dBFS instead of the loopback\n",
        opt.card_dev, opt.loop_dev, opt.api_port, opt.target_ms, opt.period, opt.buffer,
        opt.channels_file, opt.status_file);
}

static int parse_int(const char *s, int lo, int hi, int *out)
{
    char *end;
    long v = strtol(s, &end, 10);
    if (*s == 0 || *end != 0 || v < lo || v > hi) return -1;
    *out = (int)v;
    return 0;
}

int main(int argc, char **argv)
{
    static const struct option lo[] = {
        { "device", 1, 0, 'd' }, { "loop", 1, 0, 'l' }, { "api-port", 1, 0, 'p' },
        { "card-channels", 1, 0, 'c' }, { "target-ms", 1, 0, 't' },
        { "period", 1, 0, 'P' }, { "buffer", 1, 0, 'B' },
        { "channels-file", 1, 0, 'm' }, { "status-file", 1, 0, 's' },
        { "ignore-ptp", 0, 0, 'i' }, { "tone", 0, 0, 'T' }, { "help", 0, 0, 'h' },
        { 0, 0, 0, 0 }
    };
    pthread_t loop_tid, card_tid;
    pthread_attr_t attr;
    struct sigaction sa;
    ptp_gate gate;
    resampler rs;
    int o, v, err = 0;

    while ((o = getopt_long(argc, argv, "h", lo, NULL)) != -1) {
        switch (o) {
        case 'd': opt.card_dev = optarg; break;
        case 'l': opt.loop_dev = optarg; break;
        case 'm': opt.channels_file = optarg; break;
        case 's': opt.status_file = optarg; break;
        case 'i': opt.ignore_ptp = 1; break;
        case 'T': opt.tone = 1; break;
        case 'p': if (parse_int(optarg, 1, 65535, &opt.api_port)) err = 1; break;
        case 'c': if (parse_int(optarg, 1, 256, &opt.card_channels)) err = 1; break;
        case 't': if (parse_int(optarg, 20, 500, &opt.target_ms)) err = 1; break;
        case 'P': if (parse_int(optarg, 16, 4096, &v)) err = 1; else opt.period = (unsigned)v; break;
        case 'B': if (parse_int(optarg, 64, 65536, &v)) err = 1; else opt.buffer = (unsigned)v; break;
        case 'h': usage(stdout); return 0;
        default:  err = 1;
        }
    }
    if (err || optind != argc) { usage(stderr); return 2; }

    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    /* Page faults in an audio thread are glitches, so everything is locked
     * in RAM (allowed by LimitMEMLOCK). Locking covers future mappings too,
     * so keep them small: one malloc arena - glibc otherwise reserves 64 MB
     * per thread that allocates - and 256 KB thread stacks, not 8 MB. */
    mallopt(M_ARENA_MAX, 1);
    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0)
        logmsg(4, "memory not locked (%s) - running without it", strerror(errno));

    {
        int e2 = 0;
        g_src = src_new(SRC_SINC_FASTEST, BRIDGE_IN_CH, &e2);
        if (!g_src) { logmsg(3, "libsamplerate: %s", src_strerror(e2)); return 1; }
    }
    rs.ctx = g_src;
    rs.process = src_proc;
    rs.reset = src_rst;
    engine_init(&g_eng, rs, opt.target_ms);

    logmsg(6, "saw-aes67-bridge %s: %s -> %s, target %d ms, REST port %d%s",
           BRIDGE_VERSION, opt.tone ? "test tone" : opt.loop_dev, opt.card_dev,
           opt.target_ms, opt.api_port, opt.ignore_ptp ? ", ignoring PTP" : "");

    memset(&gate, 0, sizeof gate);
    read_channels();
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 256 * 1024);
    if (pthread_create(&loop_tid, &attr, opt.tone ? tone_thread : loop_thread, NULL) != 0 ||
        pthread_create(&card_tid, &attr, card_thread, NULL) != 0) {
        logmsg(3, "cannot start threads");
        return 1;
    }

    /* Once a second: PTP, the channel setting, the status file. */
    while (!g_stop) {
        int64_t next = now_ms() + 1000;
        if (!opt.ignore_ptp) poll_ptp(&gate);
        read_channels();
        write_status(state_word());
        while (!g_stop && now_ms() < next) sleep_ms(50);
    }

    pthread_join(loop_tid, NULL);
    pthread_join(card_tid, NULL);
    write_status("stopped");
    src_delete(g_src);
    logmsg(6, "stopped");
    return 0;
}
