// =====================================================================
//  Host test: the Ariadna link, stand side
//  (firmware/StaticFire_Stand/sf_link_proto.h + sf_stand_link.h)
//
//  The frames are the only thing that keeps two separately built and
//  separately flashed boards agreeing, and the IGNITE / ack handshake is the
//  one place where this link can fire an igniter, so both are checked here
//  where a flipped bit, a stale ack or a late request are easy to arrange.
//
//  Run:  cd tests && make
// =====================================================================
#include <stdio.h>
#include <stdlib.h>
#include "../firmware/StaticFire_Stand/sf_stand_link.h"

static int s_fail = 0, s_pass = 0;
#define CHECK(cond) do { if (cond) s_pass++; else { s_fail++; \
    printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #cond); } } while (0)

static void hb_send(SfStandLink &L, uint8_t flags, uint8_t ack, uint8_t data, uint32_t now_ms)
{
    sfl_hb_t h; h.flags = flags; h.ack_seq = ack; h.data_seq = data;
    uint8_t b[SFL_HB_SIZE];
    sfl_hb_encode(&h, b);
    CHECK(L.on_heartbeat(b, sizeof(b), now_ms) == 1);
}

static void test_crc_and_frames()
{
    const uint8_t nine[] = {'1','2','3','4','5','6','7','8','9'};
    CHECK(sfl_crc8(nine, sizeof(nine)) == 0xF4);            // CRC-8/SMBUS check value

    sfl_status_t s; memset(&s, 0, sizeof(s));
    s.state = SFL_ST_COUNTDOWN; s.flags = SFL_F_READY | SFL_F_IGNITE; s.run_seq = 7;
    s.thrust_mN = -123456; s.t_rel_us = -14999000; s.pages = 38; s.abort_reason = 3;
    uint8_t b[SFL_STATUS_SIZE];
    sfl_status_encode(&s, b);
    sfl_status_t r; memset(&r, 0, sizeof(r));
    CHECK(sfl_status_decode(b, &r));
    CHECK(r.state == s.state && r.flags == s.flags && r.run_seq == 7);
    CHECK(r.thrust_mN == -123456 && r.t_rel_us == -14999000 && r.pages == 38 && r.abort_reason == 3);

    // every single flipped bit must be caught by the CRC
    int missed = 0;
    for (size_t i = 0; i < sizeof(b); i++)
        for (int k = 0; k < 8; k++) {
            uint8_t c[SFL_STATUS_SIZE]; memcpy(c, b, sizeof(c));
            c[i] ^= (uint8_t)(1u << k);
            sfl_status_t x;
            if (sfl_status_decode(c, &x)) missed++;
        }
    CHECK(missed == 0);

    uint8_t ff[SFL_STATUS_SIZE]; memset(ff, 0xFF, sizeof(ff));       // a bus that answers 0xFF
    CHECK(!sfl_status_decode(ff, &r));
    uint8_t zz[SFL_STATUS_SIZE]; memset(zz, 0, sizeof(zz));
    CHECK(!sfl_status_decode(zz, &r));

    sfl_info_t i; memset(&i, 0, sizeof(i));
    i.proto = SFL_PROTO_VERSION; i.fw_major = 2; i.fw_minor = 1; i.burn_id = 12345;
    i.ignition_ms = 300; i.preroll_ms = 3000; i.recording_ms = 10000;
    uint8_t ib[SFL_INFO_SIZE]; sfl_info_encode(&i, ib);
    sfl_info_t j; memset(&j, 0, sizeof(j));
    CHECK(sfl_info_decode(ib, &j));
    CHECK(j.burn_id == 12345 && j.ignition_ms == 300 && j.preroll_ms == 3000 && j.recording_ms == 10000);
    ib[5] ^= 1; CHECK(!sfl_info_decode(ib, &j));

    sfl_hb_t h; h.flags = SFL_A_READY | SFL_A_ARMED; h.ack_seq = 9; h.data_seq = 8;
    uint8_t hb[SFL_HB_SIZE]; sfl_hb_encode(&h, hb);
    sfl_hb_t g; CHECK(sfl_hb_decode(hb, &g) && g.flags == h.flags && g.ack_seq == 9 && g.data_seq == 8);
    hb[1] ^= 0x04; CHECK(!sfl_hb_decode(hb, &g));          // flipped A_FIRED must not get through

    uint8_t req[SFL_PAGE_REQ_SIZE]; sfl_page_req_encode(0x0123, 3, req);
    CHECK(req[0] == SFL_REG_PAGE);
    uint16_t pg; uint8_t ch;
    CHECK(sfl_page_req_decode(req + 1, &pg, &ch) && pg == 0x0123 && ch == 3);
    req[2] ^= 0x10; CHECK(!sfl_page_req_decode(req + 1, &pg, &ch));

    uint8_t data[SFL_CHUNK]; for (unsigned k = 0; k < SFL_CHUNK; k++) data[k] = (uint8_t)(k * 3);
    uint8_t rep[SFL_CHUNK_REPLY]; sfl_chunk_reply_encode(SFL_PG_OK, data, rep);
    uint8_t code, back[SFL_CHUNK];
    CHECK(sfl_chunk_reply_decode(rep, &code, back) && code == SFL_PG_OK && !memcmp(back, data, SFL_CHUNK));
    rep[30] ^= 0x80; CHECK(!sfl_chunk_reply_decode(rep, &code, back));
}

static void test_run_seq()
{
    CHECK(sfl_next_run_seq(0, 0, 0) == 1);
    CHECK(sfl_next_run_seq(1, 2, 0) == 3);          // skips the ack Ariadna holds
    CHECK(sfl_next_run_seq(1, 2, 3) == 4);          // ... and the data level
    CHECK(sfl_next_run_seq(255, 0, 0) == 1);        // never 0
    CHECK(sfl_next_run_seq(254, 255, 1) == 2);      // wraps past all three
}

/** A complete good run: ready handshake, countdown, ignition, ack, copy. */
static void test_full_cycle()
{
    SfStandLink L; L.init(SFL_MODE_ARIADNA, 2, 1);
    uint32_t ms = 1000, us = ms * 1000;
    uint8_t b[SFL_STATUS_SIZE]; sfl_status_t s;

    L.ignition_ms = 300; L.preroll_ms = 3000; L.recording_ms = 10000;

    // ---- both sides announce themselves ---------------------------------
    L.publish(SFL_ST_IDLE, 1, 1, 1 /*key out*/, 0, 0, us, 0);
    CHECK(L.stand_ready());
    CHECK(!L.ariadna_alive(ms));
    CHECK(!L.may_start(ms));                                    // no Ariadna yet
    L.build_status(us, ms, b); CHECK(sfl_status_decode(b, &s));
    CHECK((s.flags & SFL_F_READY) && !(s.flags & SFL_F_LINK_OK) && s.t_rel_us == SFL_T_NONE);

    hb_send(L, SFL_A_READY, 0, 0, ms);
    CHECK(L.ariadna_alive(ms) && L.ariadna_ready(ms) && L.may_start(ms));
    L.build_status(us, ms, b); CHECK(sfl_status_decode(b, &s));
    CHECK((s.flags & SFL_F_LINK_OK) && (s.flags & SFL_F_ARIADNA_RDY));

    // Ariadna goes quiet: not ready after the timeout, never mind what it said last
    CHECK(L.ariadna_ready(ms + SFL_HB_TIMEOUT_MS - 1));
    CHECK(!L.ariadna_ready(ms + SFL_HB_TIMEOUT_MS));

    // ---- countdown ---------------------------------------------------------
    const uint8_t seq = L.countdown_start();
    CHECK(seq != 0 && seq != 0 /*ack*/);
    for (int k = 0; k < 15; k++) {                              // 15 s, heartbeats keep coming
        ms += 1000; us += 1000000;
        hb_send(L, SFL_A_READY | SFL_A_ARMED, 0, 0, ms);
        L.publish(SFL_ST_COUNTDOWN, 1, 1, 1, 0, 15000 - (k + 1) * 1000, us, 0);
        CHECK(!L.must_abort(ms));
    }
    // 5 ms after the last publish the stand says T-5 ms more precisely
    L.build_status(us - 1000000 + 14 * 1000000 /*unused*/, ms, b);   // just must not crash
    L.publish(SFL_ST_COUNTDOWN, 1, 1, 1, 0, 100, us, 0);
    L.build_status(us + 2500, ms, b); CHECK(sfl_status_decode(b, &s));
    CHECK(s.state == SFL_ST_COUNTDOWN && s.t_rel_us == -100 * 1000 + 2500);
    CHECK(s.run_seq == seq);

    // ---- T0: IGNITE raised, recording starts -----------------------------
    ms += 100; us += 100000;
    L.publish(SFL_ST_RECORDING, 1, 1, 1, 0, 0, us, us);
    L.raise_ignite(ms);
    L.build_status(us + 4200, ms, b); CHECK(sfl_status_decode(b, &s));
    CHECK((s.flags & SFL_F_IGNITE) && s.t_rel_us == 4200 && s.state == SFL_ST_RECORDING);

    // Ariadna has not acked: the request stays up for the hold time ...
    L.service(ms + SFL_IGNITE_HOLD_MS - 1); CHECK(L.ignite == 1);
    // ... and is acknowledged
    hb_send(L, SFL_A_READY | SFL_A_FIRED, seq, 0, ms + 8);
    L.service(ms + 8);
    CHECK(L.ignite == 0 && L.ignite_acked());
    L.build_status(us + 9000, ms + 9, b); CHECK(sfl_status_decode(b, &s));
    CHECK(!(s.flags & SFL_F_IGNITE));

    // ---- the burn ends; data waits until Ariadna says it has it ----------
    ms += 12000; us += 12000000;
    L.publish(SFL_ST_IDLE, 1, 1, 1, 0, 0, us, 0);
    L.burn_closed(41, 38);
    L.build_status(us, ms, b); CHECK(sfl_status_decode(b, &s));
    CHECK((s.flags & SFL_F_DATA_READY) && s.pages == 38);
    CHECK(!(s.flags & SFL_F_READY));                             // no new countdown yet
    hb_send(L, SFL_A_READY | SFL_A_BUSY, seq, 0, ms);
    L.service(ms); CHECK(L.data_pending == 1);
    hb_send(L, SFL_A_READY, seq, seq, ms + 700);
    L.service(ms + 700); CHECK(L.data_pending == 0);
    CHECK(L.stand_ready());

    // ---- INFO carries the burn id and the igniter on-time ----------------
    uint8_t ib[SFL_INFO_SIZE]; L.build_info(ib);
    sfl_info_t info; CHECK(sfl_info_decode(ib, &info));
    CHECK(info.burn_id == 41 && info.ignition_ms == 300 && info.proto == SFL_PROTO_VERSION);
}

static void test_ignite_expires_and_stale_ack()
{
    SfStandLink L; L.init(SFL_MODE_ARIADNA, 2, 1);
    hb_send(L, SFL_A_READY, 5, 5, 100);                  // Ariadna remembers an old run (ack 5, data 5)
    L.run_seq = 4;
    const uint8_t seq = L.countdown_start();
    CHECK(seq != 5);                                     // a stale ack cannot answer the new run
    L.publish(SFL_ST_RECORDING, 1, 1, 1, 0, 0, 1000, 1000);
    L.raise_ignite(200);
    L.service(201);
    CHECK(L.ignite == 1 && !L.ignite_acked());

    // nobody acts: the request lapses instead of firing late
    L.service(200 + SFL_IGNITE_HOLD_MS);
    CHECK(L.ignite == 0 && !L.ignite_acked());

    // and it never survives the end of the recording either
    L.raise_ignite(300);
    L.publish(SFL_ST_IDLE, 1, 1, 1, 0, 0, 2000, 0);
    L.service(301);
    CHECK(L.ignite == 0);
}

static void test_heartbeat_rejects_garbage()
{
    SfStandLink L; L.init(SFL_MODE_ARIADNA, 2, 1);
    uint8_t junk[SFL_HB_SIZE]; memset(junk, 0xFF, sizeof(junk));
    CHECK(!L.on_heartbeat(junk, sizeof(junk), 100));
    CHECK(!L.ariadna_alive(100));
    uint8_t good[SFL_HB_SIZE]; sfl_hb_t h = { SFL_A_READY, 0, 0 }; sfl_hb_encode(&h, good);
    CHECK(!L.on_heartbeat(good, SFL_HB_SIZE - 1, 100));          // short write
    CHECK(L.on_heartbeat(good, SFL_HB_SIZE, 100) && L.ariadna_ready(100));
    // millis() == 0 must not read as "never"
    SfStandLink M; M.init(SFL_MODE_ARIADNA, 2, 1);
    CHECK(M.on_heartbeat(good, SFL_HB_SIZE, 0) && M.ariadna_alive(0));
    // wrap of millis()
    SfStandLink W; W.init(SFL_MODE_ARIADNA, 2, 1);
    W.on_heartbeat(good, SFL_HB_SIZE, 0xFFFFFF00u);
    CHECK(W.ariadna_alive(0x00000010u));
    CHECK(!W.ariadna_alive(0x00000010u + SFL_HB_TIMEOUT_MS));
}

static void test_page_codes()
{
    SfStandLink L; L.init(SFL_MODE_ARIADNA, 2, 1);
    L.publish(SFL_ST_IDLE, 1, 1, 1, 0, 0, 0, 0);
    CHECK(L.page_code(0, 0) == SFL_PG_NODATA);
    L.burn_closed(3, 10);
    CHECK(L.page_code(0, 0) == SFL_PG_OK);
    CHECK(L.page_code(9, 3) == SFL_PG_OK);
    CHECK(L.page_code(10, 0) == SFL_PG_RANGE);
    CHECK(L.page_code(0, 4) == SFL_PG_RANGE);
    L.publish(SFL_ST_RECORDING, 1, 1, 1, 0, 0, 0, 0);            // flash is busy being written
    CHECK(L.page_code(0, 0) == SFL_PG_BUSY);
    L.publish(SFL_ST_COUNTDOWN, 1, 1, 1, 0, 5000, 0, 0);
    CHECK(L.page_code(0, 0) == SFL_PG_BUSY);
    L.publish(SFL_ST_DUMPING, 1, 1, 1, 0, 0, 0, 0);
    CHECK(L.page_code(0, 0) == SFL_PG_BUSY);
}

static void test_armed_and_fired_for_the_web_page()
{
    SfStandLink L; L.init(SFL_MODE_ARIADNA, 2, 1);
    CHECK(!L.ariadna_armed(100) && !L.ariadna_fired(100));          // never heard: neither
    hb_send(L, SFL_A_READY, 0, 0, 100);
    CHECK(!L.ariadna_armed(110) && !L.ariadna_fired(110));          // safe
    hb_send(L, SFL_A_READY | SFL_A_ARMED, 0, 0, 200);
    CHECK(L.ariadna_armed(210) && !L.ariadna_fired(210));
    hb_send(L, SFL_A_READY | SFL_A_FIRED, 1, 0, 300);
    CHECK(!L.ariadna_armed(310) && L.ariadna_fired(310));
    // an Ariadna that went quiet is not "armed", whatever it said last: the page says "unknown"
    hb_send(L, SFL_A_ARMED, 0, 0, 400);
    CHECK(!L.ariadna_armed(400 + SFL_HB_TIMEOUT_MS));
}

static void test_abort_rules()
{
    SfStandLink L; L.init(SFL_MODE_ARIADNA, 2, 1);
    hb_send(L, SFL_A_READY, 0, 0, 1000);
    CHECK(!L.must_abort(1100));
    hb_send(L, 0 /*not ready*/, 0, 0, 1100);                     // Ariadna withdraws
    CHECK(L.must_abort(1101));
    hb_send(L, SFL_A_READY, 0, 0, 1200);
    CHECK(!L.must_abort(1201));
    CHECK(L.must_abort(1200 + SFL_HB_TIMEOUT_MS));               // cable pulled
}

int main()
{
    test_crc_and_frames();
    test_run_seq();
    test_full_cycle();
    test_ignite_expires_and_stale_ack();
    test_heartbeat_rejects_garbage();
    test_page_codes();
    test_abort_rules();
    test_armed_and_fired_for_the_web_page();
    printf("link proto test: %d passed, %d failed\n", s_pass, s_fail);
    return s_fail ? 1 : 0;
}
