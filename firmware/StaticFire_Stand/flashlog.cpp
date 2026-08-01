#include "flashlog.h"
#include <hardware/flash.h>
#include <hardware/sync.h>

FlashLog flashLog;

// Linker symbols provided by the arduino-pico core.
extern uint8_t _FS_start;
extern uint8_t _FS_end;
extern "C" char __flash_binary_end;

// ---------------------------------------------------------------------
//  CRC32 (IEEE 802.3, same polynomial and reflection as zlib.crc32)
// ---------------------------------------------------------------------
uint32_t sfCrc32(const void *data, size_t len) {
  const uint8_t *p = (const uint8_t *)data;
  uint32_t crc = 0xFFFFFFFFUL;
  while (len--) {
    crc ^= *p++;
    for (uint8_t k = 0; k < 8; k++)
      crc = (crc >> 1) ^ (0xEDB88320UL & (-(int32_t)(crc & 1)));
  }
  return ~crc;
}

// ---------------------------------------------------------------------
//  Low level flash access
//
//  Both cores are running. The other core must be parked before touching
//  flash, otherwise it will execute from XIP while XIP is disabled and
//  the chip hard faults. This is the same dance the core's own LittleFS
//  driver performs.
// ---------------------------------------------------------------------
bool FlashLog::writePage(uint32_t absOffset, const void *data) {
  if (!_ok) return false;
  rp2040.idleOtherCore();
  uint32_t save = save_and_disable_interrupts();
  flash_range_program(absOffset, (const uint8_t *)data, SF_PAGE_SIZE);
  restore_interrupts(save);
  rp2040.resumeOtherCore();
  // Read back through XIP and verify. A page that did not land is worth
  // knowing about immediately, not at analysis time.
  return memcmp((const void *)(XIP_BASE + absOffset), data, SF_PAGE_SIZE) == 0;
}

bool FlashLog::eraseSectors(uint32_t absOffset, uint32_t bytes) {
  if (!_ok) return false;
  if (absOffset % SF_SECTOR_SIZE || bytes % SF_SECTOR_SIZE) return false;
  rp2040.idleOtherCore();
  uint32_t save = save_and_disable_interrupts();
  flash_range_erase(absOffset, bytes);
  restore_interrupts(save);
  rp2040.resumeOtherCore();
  return true;
}

// ---------------------------------------------------------------------
//  Region discovery
// ---------------------------------------------------------------------
bool FlashLog::begin() {
  _regionBytes  = LOG_JOURNAL_BYTES + (uint32_t)LOG_SLOTS * LOG_SLOT_BYTES;
  uint32_t fsStart = (uint32_t)&_FS_start - XIP_BASE;
  uint32_t fsEnd   = (uint32_t)&_FS_end   - XIP_BASE;

  // If no filesystem is configured both symbols sit at the end of flash;
  // either way the space directly below _FS_start is ours to use.
  uint32_t regionEnd = fsStart;
  if (regionEnd < _regionBytes) {
    _error = "flash too small for log region";
    return false;
  }
  _regionOffset = (regionEnd - _regionBytes) & ~(SF_SECTOR_SIZE - 1);

  uint32_t sketchEnd = (uint32_t)&__flash_binary_end - XIP_BASE;
  if (_regionOffset < sketchEnd + 64u * 1024u) {
    _error = "log region would collide with the sketch";
    return false;
  }
  if (fsEnd > fsStart && _regionOffset + _regionBytes > fsStart) {
    _error = "log region would collide with LittleFS";
    return false;
  }

  _ok    = true;
  _error = "";
  if (!loadJournal()) {
    // Virgin device: wipe the journal sectors and lay down record 0.
    eraseSectors(_regionOffset, LOG_JOURNAL_BYTES);
    memset(&_j, 0, sizeof(_j));
    _j.magic            = SF_MAGIC_JOURNAL;
    _j.seq              = 1;
    _j.state            = SF_STATE_IDLE;
    _j.cal_counts_per_n = 1.0f;
    _jSector = 0;
    _jIndex  = 0;
    commitJournal();
  }
  return true;
}

bool FlashLog::loadJournal() {
  const SfJournal *best = nullptr;
  uint32_t bestSeq = 0;
  uint32_t recordsPerSector = SF_SECTOR_SIZE / SF_PAGE_SIZE;

  for (uint32_t s = 0; s < 2; s++) {
    for (uint32_t i = 0; i < recordsPerSector; i++) {
      const SfJournal *r = (const SfJournal *)(XIP_BASE + _regionOffset
                                               + s * SF_SECTOR_SIZE + i * SF_PAGE_SIZE);
      if (r->magic != SF_MAGIC_JOURNAL) continue;
      SfJournal tmp = *r;
      uint32_t stored = tmp.crc32;
      tmp.crc32 = 0;
      if (sfCrc32(&tmp, sizeof(tmp)) != stored) continue;
      if (r->seq >= bestSeq) {
        bestSeq  = r->seq;
        best     = r;
        _jSector = s;
        _jIndex  = i + 1;
      }
    }
  }
  if (!best) return false;
  _j = *best;
  return true;
}

bool FlashLog::commitJournal() {
  if (!_ok) return false;
  uint32_t recordsPerSector = SF_SECTOR_SIZE / SF_PAGE_SIZE;

  if (_jIndex >= recordsPerSector) {
    // Rotate: erase the *other* sector first, write there, then erase the
    // one we came from. At no point are both sectors without a valid
    // record, so a reset mid rotation still leaves the journal readable.
    uint32_t other = _jSector ^ 1u;
    eraseSectors(_regionOffset + other * SF_SECTOR_SIZE, SF_SECTOR_SIZE);
    uint32_t oldSector = _jSector;
    _jSector = other;
    _jIndex  = 0;
    _j.seq++;
    _j.crc32 = 0;
    _j.crc32 = sfCrc32(&_j, sizeof(_j));
    bool r = writePage(_regionOffset + _jSector * SF_SECTOR_SIZE, &_j);
    _jIndex = 1;
    eraseSectors(_regionOffset + oldSector * SF_SECTOR_SIZE, SF_SECTOR_SIZE);
    return r;
  }

  _j.magic = SF_MAGIC_JOURNAL;
  _j.seq++;
  _j.crc32 = 0;
  _j.crc32 = sfCrc32(&_j, sizeof(_j));
  bool r = writePage(_regionOffset + _jSector * SF_SECTOR_SIZE + _jIndex * SF_PAGE_SIZE, &_j);
  _jIndex++;
  return r;
}

// ---------------------------------------------------------------------
//  Erase management
// ---------------------------------------------------------------------
bool FlashLog::eraseSlot(uint32_t slot) {
  if (!_ok || slot >= LOG_SLOTS) return false;
  if (!eraseSectors(slotOffset(slot), LOG_SLOT_BYTES)) return false;
  _j.slot_erased_mask |= (1u << slot);
  return commitJournal();
}

// One sector per call so the caller can keep feeding the watchdog and
// servicing the web UI. Returns true when the whole slot is done.
bool FlashLog::eraseSlotStep(uint32_t slot, uint32_t &sectorCursor) {
  if (!_ok || slot >= LOG_SLOTS) return true;
  uint32_t sectors = LOG_SLOT_BYTES / SF_SECTOR_SIZE;
  if (sectorCursor >= sectors) {
    _j.slot_erased_mask |= (1u << slot);
    commitJournal();
    return true;
  }
  eraseSectors(slotOffset(slot) + sectorCursor * SF_SECTOR_SIZE, SF_SECTOR_SIZE);
  sectorCursor++;
  return false;
}

void FlashLog::eraseAll() {
  if (!_ok) return;
  for (uint32_t s = 0; s < LOG_SLOTS; s++)
    eraseSectors(slotOffset(s), LOG_SLOT_BYTES);
  _j.slot_erased_mask = (LOG_SLOTS >= 32) ? 0xFFFFFFFFu : ((1u << LOG_SLOTS) - 1u);
  _j.total_burns = 0;
  _j.state       = SF_STATE_IDLE;
  commitJournal();
}

// ---------------------------------------------------------------------
//  Recording
// ---------------------------------------------------------------------
bool FlashLog::startBurn(uint32_t slot, uint32_t burnId, const SfHeaderPage &hdrIn) {
  if (!_ok || slot >= LOG_SLOTS) return false;
  if (!slotIsReady(slot)) {
    if (!eraseSlot(slot)) return false;
  }

  SfHeaderPage hdr = hdrIn;
  hdr.magic   = SF_MAGIC_HEADER;
  hdr.burn_id = burnId;
  hdr.crc32   = 0;
  hdr.crc32   = sfCrc32(&hdr, sizeof(hdr));
  if (!writePage(slotOffset(slot), &hdr)) return false;

  _slot       = slot;
  _burnId     = burnId;
  _pageCursor = 1;
  _nSamples   = 0;
  _lastTus    = 0;
  _recording  = true;
  _pendingFlags = 0;
  memset(&_page, 0, sizeof(_page));

  // The slot is live from now on. If the lights go out after this point
  // the next boot knows a burn was in progress.
  _j.state            = SF_STATE_RECORDING;
  _j.active_slot      = slot;
  _j.slot_erased_mask &= ~(1u << slot);
  return commitJournal();
}

bool FlashLog::resumeBurn(uint32_t slot, uint32_t burnId, int32_t &lastTusOut, uint32_t &nSamplesOut) {
  if (!_ok || slot >= LOG_SLOTS) return false;

  uint32_t pages = pagesPerSlot();
  uint32_t cursor = 1;
  uint32_t samples = 0;
  int32_t  lastT = 0;

  for (uint32_t p = 1; p < pages; p++) {
    const SfDataPage *dp = (const SfDataPage *)pagePtr(slot, p);
    if (dp->magic != SF_MAGIC_DATA) break;
    SfDataPage tmp = *dp;
    uint32_t stored = tmp.crc32;
    tmp.crc32 = 0;
    if (sfCrc32(&tmp, sizeof(tmp)) != stored) break;   // torn page, stop here
    if (dp->n_samples > 0 && dp->n_samples <= SF_SAMPLES_PER_PAGE) {
      samples += dp->n_samples;
      lastT = dp->samples[dp->n_samples - 1].t_us;
    }
    cursor = p + 1;
  }

  _slot       = slot;
  _burnId     = burnId;
  _pageCursor = cursor;
  _nSamples   = samples;
  _lastTus    = lastT;
  _recording  = (cursor + 1 < pages);
  _pendingFlags = SF_PF_RESUME | SF_PF_GAP;
  memset(&_page, 0, sizeof(_page));

  lastTusOut  = lastT;
  nSamplesOut = samples;
  return _recording;
}

bool FlashLog::flushPage() {
  if (!_recording || _page.n_samples == 0) return true;
  if (_pageCursor + 1 >= pagesPerSlot()) { _recording = false; return false; }

  _page.magic      = SF_MAGIC_DATA;
  _page.burn_id    = _burnId;
  _page.page_index = (uint16_t)_pageCursor;
  _page.flags      = _pendingFlags;
  _pendingFlags    = 0;
  _page.crc32      = 0;
  _page.crc32      = sfCrc32(&_page, sizeof(_page));

  bool r = writePage(slotOffset(_slot) + _pageCursor * SF_PAGE_SIZE, &_page);
  _pageCursor++;
  memset(&_page, 0, sizeof(_page));
  return r;
}

bool FlashLog::endBurn(uint32_t pyroOffUs, bool cleanFinish) {
  if (!_ok) return false;
  flushPage();

  if (_pageCursor < pagesPerSlot()) {
    SfFooterPage f;
    memset(&f, 0, sizeof(f));
    f.magic        = SF_MAGIC_FOOTER;
    f.burn_id      = _burnId;
    f.n_pages      = _pageCursor;
    f.n_samples    = _nSamples;
    f.t_last_us    = _lastTus;
    f.t_pyro_off_us = pyroOffUs;
    f.resume_count = _j.resume_count;
    f.clean_finish = cleanFinish ? 1u : 0u;
    f.crc32        = 0;
    f.crc32        = sfCrc32(&f, sizeof(f));
    writePage(slotOffset(_slot) + _pageCursor * SF_PAGE_SIZE, &f);
    _pageCursor++;
  }

  _recording  = false;
  _j.state    = SF_STATE_IDLE;
  return commitJournal();
}

uint32_t FlashLog::validPages(uint32_t slot) const {
  if (!_ok || slot >= LOG_SLOTS) return 0;
  uint32_t pages = pagesPerSlot();
  uint32_t n = 0;
  for (uint32_t p = 0; p < pages; p++) {
    const uint32_t *m = (const uint32_t *)pagePtr(slot, p);
    if (*m != SF_MAGIC_HEADER && *m != SF_MAGIC_DATA && *m != SF_MAGIC_FOOTER) break;
    n = p + 1;
    if (*m == SF_MAGIC_FOOTER) break;
  }
  return n;
}
