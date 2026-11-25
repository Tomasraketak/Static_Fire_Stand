// =====================================================================
//  Stand side of the Ariadna link - the logic, without the hardware.
//
//  No Arduino dependency: the firmware wraps this in ariadna_i2c.h (the
//  Wire glue) and tests/link_proto_test.cpp drives the very same code
//  through a whole countdown -> ignition -> copy cycle on a workstation.
//
//  Two threads touch it. Core 0 (the main loop) publishes the stand's state
//  and raises / clears requests; core 1 (the I2C interrupt) builds replies
//  and stores heartbeats. Every shared field is one aligned 32 bit word,
//  which is atomic on Cortex-M, so there is no lock to get wrong.
//
//  Modes
//    0  standalone  the stand fires its own MOSFET exactly as before; this
//                   file does nothing and the I2C pins are left alone
//    1  ariadna     the stand NEVER drives its igniter output. At T0 it
//                   raises IGNITE and Ariadna fires pyro 1. Needs Ariadna
//                   ready for every countdown, aborts the countdown when
//                   Ariadna drops out.
// =====================================================================
#ifndef SF_STAND_LINK_H
#define SF_STAND_LINK_H

#include "sf_link_proto.h"

#define SFL_MODE_STANDALONE  0u
#define SFL_MODE_ARIADNA     1u

// IGNITE stays raised this long waiting for Ariadna's ack, then drops: a
// request that nobody has acted on within half a second is a failed
// ignition, not one to be honoured late.
#define SFL_IGNITE_HOLD_MS   500u

struct SfStandLink {
    // ---- configuration (set at boot, from the journal) -------------------
    volatile uint32_t mode;
    uint8_t  fw_major;
    uint8_t  fw_minor;

    // ---- from Ariadna, written by the interrupt --------------------------
    sfl_rx_t rx;

    // ---- published by core 0 ---------------------------------------------
    volatile uint32_t state;           // SFL_ST_*
    volatile uint32_t hx_ok;
    volatile uint32_t log_ok;
    volatile uint32_t rbf_out;         // RBF key removed
    volatile int32_t  thrust_mN;
    volatile int32_t  rem_ms;          // countdown remaining, ms
    volatile uint32_t pub_us;          // micros() when rem_ms was published
    volatile uint32_t t0_us;           // micros() at T0, valid in RECORDING
    volatile uint32_t abort_reason;
    volatile uint32_t burn_id;         // id of the burn PAGE serves
    volatile uint32_t pages;           // its pages, 0 = none
    volatile uint32_t ignition_ms;
    volatile uint32_t preroll_ms;
    volatile uint32_t recording_ms;

    // ---- requests, raised by core 0 --------------------------------------
    volatile uint32_t run_seq;
    volatile uint32_t ignite;          // 1 while IGNITE is raised
    volatile uint32_t ignite_since_ms;
    volatile uint32_t data_pending;    // a finished burn is waiting to be copied

    // =====================================================================
    //  core 0
    // =====================================================================
    void init(uint32_t mode_, uint8_t maj, uint8_t min_)
    {
        memset((void *)this, 0, sizeof(*this));
        mode = mode_;
        fw_major = maj;
        fw_minor = min_;
        state = SFL_ST_BOOT;
    }

    int active() const { return mode == SFL_MODE_ARIADNA; }

    void publish(uint32_t st, int hx, int log, int rbf_removed, int32_t thrust,
                 int32_t rem, uint32_t now_us, uint32_t t0)
    {
        hx_ok = hx ? 1u : 0u;
        log_ok = log ? 1u : 0u;
        rbf_out = rbf_removed ? 1u : 0u;
        thrust_mN = thrust;
        rem_ms = rem;
        t0_us = t0;
        pub_us = now_us;
        state = st;                    // last: the interrupt keys off it
    }

    int ariadna_alive(uint32_t now_ms) const { return sfl_rx_alive(&rx, now_ms); }
    int ariadna_ready(uint32_t now_ms) const { return sfl_rx_ready(&rx, now_ms); }
    int ariadna_cont(uint32_t now_ms) const
    {
        return sfl_rx_alive(&rx, now_ms) && (sfl_rx_get(&rx).flags & SFL_A_CONT);
    }

    /** Pyro 1 on Ariadna is armed / has been commanded (0 when Ariadna is not heard). */
    int ariadna_armed(uint32_t now_ms) const
    {
        return sfl_rx_alive(&rx, now_ms) && (sfl_rx_get(&rx).flags & SFL_A_ARMED);
    }
    int ariadna_fired(uint32_t now_ms) const
    {
        return sfl_rx_alive(&rx, now_ms) && (sfl_rx_get(&rx).flags & SFL_A_FIRED);
    }

    /** Can a countdown start right now? (the stand's own interlocks are
     *  checked by the caller; this adds Ariadna's.) */
    int may_start(uint32_t now_ms) const
    {
        return ariadna_ready(now_ms) && !data_pending;
    }

    /** The countdown must stop: Ariadna dropped out or says it is not ready. */
    int must_abort(uint32_t now_ms) const { return !ariadna_ready(now_ms); }

    uint8_t countdown_start()
    {
        const sfl_hb_t hb = sfl_rx_get(&rx);
        run_seq = sfl_next_run_seq((uint8_t)run_seq, hb.ack_seq, hb.data_seq);
        ignite = 0;
        return (uint8_t)run_seq;
    }

    void raise_ignite(uint32_t now_ms)
    {
        ignite_since_ms = now_ms;
        ignite = 1;
    }

    void burn_closed(uint32_t id, uint32_t n_pages)
    {
        burn_id = id;
        pages = n_pages;
        data_pending = 1;
    }

    /** Call every loop. Drops IGNITE once acknowledged or stale, and drops
     *  data_pending once Ariadna says it has the data. */
    void service(uint32_t now_ms)
    {
        const sfl_hb_t hb = sfl_rx_get(&rx);
        if (ignite) {
            if (hb.ack_seq == (uint8_t)run_seq ||
                (uint32_t)(now_ms - ignite_since_ms) >= SFL_IGNITE_HOLD_MS ||
                state != SFL_ST_RECORDING)
                ignite = 0;
        }
        if (data_pending && hb.data_seq == (uint8_t)run_seq)
            data_pending = 0;
    }

    /** Ariadna acknowledged the ignition of this run. */
    int ignite_acked() const { return sfl_rx_get(&rx).ack_seq == (uint8_t)run_seq; }

    int stand_ready() const
    {
        return state == SFL_ST_IDLE && hx_ok && log_ok && rbf_out && !data_pending;
    }

    // =====================================================================
    //  core 1 (interrupt context: no printf, no waiting)
    // =====================================================================
    void build_status(uint32_t now_us, uint32_t now_ms, uint8_t out[SFL_STATUS_SIZE]) const
    {
        sfl_status_t s;
        memset(&s, 0, sizeof(s));
        const uint32_t st = state;
        s.state        = (uint8_t)st;
        s.run_seq      = (uint8_t)run_seq;
        s.abort_reason = (uint8_t)abort_reason;
        s.thrust_mN    = thrust_mN;
        s.pages        = (uint16_t)pages;

        if (st == SFL_ST_COUNTDOWN)
            s.t_rel_us = -(rem_ms * 1000) + (int32_t)(now_us - pub_us);
        else if (st == SFL_ST_RECORDING)
            s.t_rel_us = (int32_t)(now_us - t0_us);
        else
            s.t_rel_us = SFL_T_NONE;

        const int alive = sfl_rx_alive(&rx, now_ms);
        uint8_t f = 0;
        if (stand_ready())                                       f |= SFL_F_READY;
        if (rbf_out)                                             f |= SFL_F_RBF_OUT;
        if (ignite)                                              f |= SFL_F_IGNITE;
        if (data_pending)                                        f |= SFL_F_DATA_READY;
        if (alive)                                               f |= SFL_F_LINK_OK;
        if (alive && (sfl_rx_get(&rx).flags & SFL_A_READY))      f |= SFL_F_ARIADNA_RDY;
        if (hx_ok)                                               f |= SFL_F_HX_OK;
        if (log_ok)                                              f |= SFL_F_LOG_OK;
        s.flags = f;
        sfl_status_encode(&s, out);
    }

    void build_info(uint8_t out[SFL_INFO_SIZE]) const
    {
        sfl_info_t i;
        memset(&i, 0, sizeof(i));
        i.proto        = (uint8_t)SFL_PROTO_VERSION;
        i.fw_major     = fw_major;
        i.fw_minor     = fw_minor;
        i.burn_id      = burn_id;
        i.ignition_ms  = (uint16_t)(ignition_ms > 65535u ? 65535u : ignition_ms);
        i.preroll_ms   = (uint16_t)(preroll_ms > 65535u ? 65535u : preroll_ms);
        i.recording_ms = (uint16_t)(recording_ms > 65535u ? 65535u : recording_ms);
        sfl_info_encode(&i, out);
    }

    /** The 8 bytes that follow SFL_REG_HEARTBEAT. */
    int on_heartbeat(const uint8_t *buf, size_t n, uint32_t now_ms)
    {
        if (n != SFL_HB_SIZE) return 0;
        sfl_hb_t h;
        if (!sfl_hb_decode(buf, &h)) return 0;
        sfl_rx_store(&rx, &h, now_ms);
        return 1;
    }

    /** What a PAGE read should answer with, given the request. */
    uint8_t page_code(uint16_t page, uint8_t chunk) const
    {
        if (state != SFL_ST_IDLE)           return SFL_PG_BUSY;
        if (pages == 0)                     return SFL_PG_NODATA;
        if (page >= pages || chunk >= SFL_CHUNKS_PER_PAGE) return SFL_PG_RANGE;
        return SFL_PG_OK;
    }
};

#endif // SF_STAND_LINK_H
