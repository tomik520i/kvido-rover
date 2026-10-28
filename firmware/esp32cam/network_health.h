#pragma once
#include <stdint.h>

// An associated station can stop passing IP packets without a disconnect event.
// Recent authenticated traffic OR a gateway reply proves the network is alive.
// Missing ICMP alone must never interrupt a working LAN/relay connection.
namespace NetworkHealth {
struct Policy {
  uint32_t lastRecovery=0;
  uint32_t healthySince=0;
  uint8_t attempts=0;
  bool due(uint32_t now,uint32_t associatedAt,uint32_t trafficAt,
           uint32_t gatewayAt,uint32_t failedPings,bool suspended) const {
    return !suspended && uint32_t(now-associatedAt)>=30000 &&
      uint32_t(now-trafficAt)>=30000 && uint32_t(now-gatewayAt)>=30000 &&
      failedPings>=4 && (!lastRecovery || uint32_t(now-lastRecovery)>=45000);
  }
  uint8_t recover(uint32_t now) {
    lastRecovery=now;healthySince=0;
    if(attempts<3)++attempts;
    return attempts; // reassociate, restart radio, restart ESP
  }
  void healthy(uint32_t now,uint32_t proofAt) {
    if(!lastRecovery || int32_t(proofAt-lastRecovery)<=0 || uint32_t(now-proofAt)>=10000){healthySince=0;return;}
    if(!healthySince)healthySince=now;
    // A few working seconds between outages are not a stable recovery.
    if(uint32_t(now-healthySince)>=60000)attempts=0;
  }
};
}
