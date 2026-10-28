#include <assert.h>
#include <string.h>
#include "control_owner.h"
#include "relay_message.h"
#include "network_health.h"

int main() {
  NetworkHealth::Policy health;
  assert(!health.due(20000,0,0,0,10,false)); // boot grace
  assert(!health.due(60000,0,59000,0,10,false)); // ICMP blocked, LAN still healthy
  assert(!health.due(60000,0,0,59000,10,false)); // internet down, gateway healthy
  assert(!health.due(60000,0,0,0,3,false)); // require repeated probe failures
  assert(!health.due(60000,0,0,0,10,true)); // never interrupt OTA
  assert(health.due(60000,0,0,0,10,false)); // associated but no working IP
  assert(health.recover(60000)==1);
  assert(!health.due(90000,60000,0,0,10,false)); // recovery cooldown
  assert(health.due(106000,60000,0,0,10,false));
  assert(health.recover(106000)==2);
  assert(health.recover(152000)==3);
  health.healthy(165000,164000);
  assert(health.attempts==3); // brief recovery must not suppress escalation
  health.healthy(175000,164000);
  assert(health.healthySince==0); // renewed outage resets healthy window
  for(uint32_t now=180000;now<=240000;now+=5000)health.healthy(now,now-1000);
  assert(health.attempts==0); // only a stable recovery resets escalation
  NetworkHealth::Policy wrapped;
  assert(wrapped.due(40000,0xffff0000U,0xffff0000U,0xffff0000U,10,false));
  using namespace ControlOwner;
  State state;
  assert(!state.owns(Lan) && !state.owns(Relay));
  assert(state.claim(Lan));
  auto generation=state.generation;
  for (int i=0;i<100;++i) {
    assert(!state.lost(Relay)); // CONNECT, DISCONNECT, malformed packet on unused link
    assert(state.owns(Lan) && state.generation==generation);
    assert(!state.claim(Relay));
  }
  assert(state.lost(Lan) && state.owner==None);
  assert(state.claim(Relay));
  assert(!state.allows(Lan));
  assert(state.lost(Relay) && !state.owns(Relay));
  state.clear(); // global emergency stop permits a deliberate owner change
  assert(state.claim(Lan));

  RelayMessage input;
  const char *payload="{\"type\":\"ping\"}";
  int len=int(strlen(payload));
  for(int split=1;split<len;++split) {
    assert(input.append(payload,split,len,0,4)==RelayMessage::More);
    assert(input.append(payload+split,len-split,len,split,4)==RelayMessage::Complete);
    assert(strcmp(input.text,payload)==0);
  }
  assert(input.append(payload,1,len,0,4)==RelayMessage::More);
  assert(input.append(payload+1,len-1,len,1,5)==RelayMessage::Invalid); // previous session
  assert(input.append(payload,len,len,0,5)==RelayMessage::Complete);
  assert(input.append(payload,1,len,0,5)==RelayMessage::More);
  assert(input.append(payload+2,1,len,2,5)==RelayMessage::Invalid); // gap
  assert(input.append(payload,1,len,0,5)==RelayMessage::More);
  assert(input.append(payload,1,len,0,5)==RelayMessage::Invalid); // overlapping message
  assert(input.append(payload,1,1024,0,5)==RelayMessage::Invalid); // bounded
  assert(input.append(payload,3,2,0,5)==RelayMessage::Invalid);
  assert(input.append(nullptr,1,1,0,5)==RelayMessage::Invalid);
  assert(input.append(payload,len,len,0,6)==RelayMessage::Complete); // recover after invalid input
}
