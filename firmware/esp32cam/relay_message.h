#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// An IDF DATA callback may contain only part of one WebSocket payload.
// Bounded reassembly; no partial JSON can reach the command queue.
struct RelayMessage {
  enum Result { Invalid, More, Complete };
  char text[1024] = {};
  size_t used = 0, total = 0;
  uint32_t generation = 0;
  void reset() { used = total = 0; }
  Result append(const char *data, int size, int length, int offset, uint32_t gen) {
    if (!data || size <= 0 || length <= 0 || length >= int(sizeof(text)) ||
        offset < 0 || size > length || offset > length-size) { reset(); return Invalid; }
    if (offset == 0) {
      // A second payload must not silently overwrite an incomplete first one.
      if (used) { reset(); return Invalid; }
      total = length; generation = gen;
    }
    if (generation != gen || total != size_t(length) || used != size_t(offset)) {
      reset(); return Invalid;
    }
    memcpy(text+used, data, size); used += size;
    if (used != total) return More;
    text[used] = 0; reset(); return Complete;
  }
};
