#pragma once
#include <stdint.h>

namespace ControlOwner {
enum Source : uint8_t { None, Lan, Relay };
struct State {
  Source owner = None;
  uint32_t generation = 0;
  bool allows(Source source) const { return owner == None || owner == source; }
  bool owns(Source source) const { return owner == source; }
  bool claim(Source source) {
    if (source == None || !allows(source)) return false;
    owner = source; ++generation; return true;
  }
  void clear() { owner = None; ++generation; }
  bool lost(Source source) {
    if (!owns(source)) return false;
    clear(); return true;
  }
};
inline const char *name(Source source) {
  return source == Lan ? "lan" : source == Relay ? "relay" : "none";
}
}
