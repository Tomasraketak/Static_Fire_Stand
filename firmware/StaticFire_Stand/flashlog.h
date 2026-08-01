// =====================================================================
//  Power-fail tolerant burn recorder.
//
//  Design goals, in order:
//    1. A reset / brownout at ANY instant must never lose more than the
//       one 256 byte page that was being filled at that moment.
//    2. A reset must never corrupt data that was already written.
//    3. After a reset in the middle of a burn the firmware must be able
//       to pick the recording back up without re-firing the igniter.
//
//  How:
//    * The log lives in RAW flash, immediately below the LittleFS area.
//      No filesystem metadata is touched during a burn, so there is no
//      window where a half updated directory can eat the whole log.
//    * The slot for the next burn is erased ahead of time, while the
//      stand is idle. During the burn only 256 byte page PROGRAM
//      operations happen (~400 us each), never an erase (~50 ms each).
//    * Every page is self describing: magic + burn id + page index +
//      sample count + CRC32. Recovery is a linear scan; nothing depends
//      on a pointer that could be stale.
//    * A tiny A/B journal in two dedicated sectors records the coarse
//      state (which slot is live, was a burn in progress, calibration).
//
//  Worst case data loss on a power cut is therefore one page,
//  i.e. 29 samples, ~360 ms at 80 SPS.
// =====================================================================
#pragma once
#include <Arduino.h>
#include <hardware/flash.h>
#include "config.h"

#define SF_PAGE_SIZE        256u
#define SF_SECTOR_SIZE      4096u
#define SF_SAMPLES_PER_PAGE 29u

#define SF_MAGIC_JOURNAL 0x314A4653UL  // "SFJ1"
#define SF_MAGIC_HEADER  0x31484653UL  // "SFH1"
#define SF_MAGIC_DATA    0x31504653UL  // "SFP1"
#define SF_MAGIC_FOOTER  0x31464653UL  // "SFF1"

// state values stored in the journal
enum : uint32_t {
  SF_STATE_IDLE      = 0,
  SF_STATE_RECORDING = 1,
};

// per-sample flag bits (top 8 bits of the packed word)
enum : uint8_t {
  SF_SF_PYRO = 0x01,
  SF_SF_CONT = 0x02,
  SF_SF_RBF  = 0x04,
  SF_SF_SAT  = 0x08,   // ADC saturated / clipped
};

// page flag bits
enum : uint32_t {
  SF_PF_RESUME = 0x00000001UL,  // first page written after an unexpected reset
  SF_PF_GAP    = 0x00000002UL,  // timeline has an unknown length hole before it
};

#pragma pack(push, 1)

// One sample = 8 bytes.
//   t_us   : signed microseconds relative to T0 (pyro command). Negative
//            during the pre-roll.
//   packed : bits 0..23 = raw 24 bit signed HX711 count
//            bits 24..31 = SF_SF_* flags
struct SfSample {
  int32_t  t_us;
  uint32_t packed;
};

// 20 byte header + 29 samples (232 B) + 4 B pad = 256 B
struct SfDataPage {
  uint32_t magic;
  uint32_t burn_id;
  uint16_t page_index;
  uint16_t n_samples;
  uint32_t flags;
  uint32_t crc32;        // over the whole page with this field zeroed
  SfSample samples[SF_SAMPLES_PER_PAGE];
  uint8_t  pad[4];
};

// Page 0 of every slot. Written before the pre-roll starts.
struct SfHeaderPage {
  uint32_t magic;
  uint32_t burn_id;
  uint32_t crc32;
  uint32_t boot_count;
  uint32_t fw_version;      // major<<16 | minor<<8 | patch
  float    cal_counts_per_n; // raw counts per newton
  int32_t  tare_offset;
  uint32_t preroll_ms;
  uint32_t recording_ms;
  uint32_t ignition_ms;
  uint32_t countdown_ms;
  uint32_t t0_unix;          // 0 = unknown, set by the host if it ever syncs
  uint32_t reserved[4];
  uint8_t  pad[SF_PAGE_SIZE - 4 * 16];
};

// Last page of a completed burn. Its absence means the burn was cut short.
struct SfFooterPage {
  uint32_t magic;
  uint32_t burn_id;
  uint32_t crc32;
  uint32_t n_pages;
  uint32_t n_samples;
  int32_t  t_last_us;
  uint32_t t_pyro_on_us;   // always 0, T0 is the definition
  uint32_t t_pyro_off_us;  // measured, microseconds after T0
  uint32_t resume_count;
  uint32_t clean_finish;   // 1 = the sequence ran to completion
  uint32_t reserved[6];
  uint8_t  pad[SF_PAGE_SIZE - 4 * 16];
};

struct SfJournal {
  uint32_t magic;
  uint32_t seq;
  uint32_t crc32;
  uint32_t total_burns;
  uint32_t active_slot;
  uint32_t state;
  uint32_t boot_count;
  uint32_t resume_count;
  float    cal_counts_per_n;
  int32_t  tare_offset;
  uint32_t slot_erased_mask;  // bit N set = slot N is erased and ready
  uint32_t reserved[5];
  uint8_t  pad[SF_PAGE_SIZE - 4 * 16];
};

#pragma pack(pop)

static_assert(sizeof(SfDataPage)   == SF_PAGE_SIZE, "data page must be 256 B");
static_assert(sizeof(SfHeaderPage) == SF_PAGE_SIZE, "header page must be 256 B");
static_assert(sizeof(SfFooterPage) == SF_PAGE_SIZE, "footer page must be 256 B");
static_assert(sizeof(SfJournal)    == SF_PAGE_SIZE, "journal must be 256 B");

uint32_t sfCrc32(const void *data, size_t len);

class FlashLog {
public:
  // Locates the region, validates it, loads the journal. Returns false if
  // the region overlaps the sketch or the filesystem, in which case
  // logging is disabled rather than silently destroying something.
  bool begin();

  bool     ok() const           { return _ok; }
  const char *error() const     { return _error; }
  uint32_t regionOffset() const { return _regionOffset; }
  uint32_t regionBytes() const  { return _regionBytes; }
  uint32_t slotCount() const    { return LOG_SLOTS; }
  uint32_t pagesPerSlot() const { return LOG_SLOT_BYTES / SF_PAGE_SIZE; }

  SfJournal &journal() { return _j; }
  bool commitJournal();          // writes a new journal record

  // --- housekeeping (idle only, these erase and are slow) ---
  bool slotIsReady(uint32_t slot) const { return _j.slot_erased_mask & (1u << slot); }
  bool eraseSlot(uint32_t slot);            // ~800 ms for 64 KB
  bool eraseSlotStep(uint32_t slot, uint32_t &sectorCursor); // one sector, ~50 ms
  void eraseAll();

  // --- recording ---
  bool startBurn(uint32_t slot, uint32_t burnId, const SfHeaderPage &hdr);
  bool resumeBurn(uint32_t slot, uint32_t burnId, int32_t &lastTusOut, uint32_t &nSamplesOut);
  inline bool addSample(int32_t t_us, int32_t raw, uint8_t flags) {
    if (!_recording) return false;
    if (_page.n_samples >= SF_SAMPLES_PER_PAGE) {
      if (!flushPage()) return false;
    }
    SfSample &s = _page.samples[_page.n_samples++];
    s.t_us   = t_us;
    s.packed = ((uint32_t)raw & 0x00FFFFFFu) | ((uint32_t)flags << 24);
    _nSamples++;
    _lastTus = t_us;
    return true;
  }
  bool flushPage();                       // programs the partially filled page
  bool endBurn(uint32_t pyroOffUs, bool cleanFinish);
  bool recording() const { return _recording; }
  uint32_t pageCursor() const { return _pageCursor; }
  uint32_t sampleCount() const { return _nSamples; }
  bool full() const { return _pageCursor + 1 >= pagesPerSlot(); }

  // --- readback ---
  const uint8_t *pagePtr(uint32_t slot, uint32_t page) const {
    return (const uint8_t *)(XIP_BASE + _regionOffset + LOG_JOURNAL_BYTES
                             + slot * LOG_SLOT_BYTES + page * SF_PAGE_SIZE);
  }
  uint32_t validPages(uint32_t slot) const;   // scan until an invalid page

private:
  bool writePage(uint32_t absOffset, const void *data);
  bool eraseSectors(uint32_t absOffset, uint32_t bytes);
  uint32_t slotOffset(uint32_t slot) const {
    return _regionOffset + LOG_JOURNAL_BYTES + slot * LOG_SLOT_BYTES;
  }
  bool loadJournal();

  bool     _ok = false;
  const char *_error = "not initialised";
  uint32_t _regionOffset = 0;
  uint32_t _regionBytes  = 0;
  uint32_t _jSector = 0;     // 0 or 1
  uint32_t _jIndex  = 0;     // next free record in that sector
  SfJournal _j;

  bool     _recording  = false;
  uint32_t _slot       = 0;
  uint32_t _burnId     = 0;
  uint32_t _pageCursor = 1;  // page 0 is the header
  uint32_t _nSamples   = 0;
  int32_t  _lastTus    = 0;
  uint32_t _pendingFlags = 0;
  SfDataPage _page;
};

extern FlashLog flashLog;
