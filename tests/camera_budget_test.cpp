#include <cassert>
#include "camera_budget.h"
int main(){
  CameraBudget::Policy p;
  assert(!p.update(0,70000,30000)&&p.interval()==500);
  assert(!p.update(50,39432,32756)); // observed brief TLS/HTTP peak still has headroom
  assert(!p.update(100,35000,30000)&&p.tier==2);
  assert(!p.update(200,55000,20000)); // short peak must not degrade quality
  assert(!p.update(300,35000,30000));
  assert(!p.update(6299,35000,30000));
  assert(p.update(6300,35000,30000)&&p.tier==1&&p.interval()==1000);
  assert(p.update(200,20000,30000)&&p.tier==0);
  assert(!p.update(300,70000,40000));
  assert(!p.update(60299,70000,40000));
  assert(p.update(60300,70000,40000)&&p.tier==1);
  p.update(60400,70000,40000);
  assert(p.update(120400,70000,40000)&&p.tier==2&&p.interval()==500);
  assert(p.update(120500,70000,8000)); // fragmented RAM also reduces resolution
  p.update(121000,70000,30000);p.update(180999,39000,30000); // break recovery streak
  assert(!p.update(181001,70000,30000)&&p.tier==0);
  assert(!p.update(241000,70000,30000));
  assert(p.update(241001,70000,30000)&&p.tier==1);
  p.update(0xffff0000U,10000,2000);p.update(0xffff1000U,70000,30000);
  assert(p.update(uint32_t(0xffff1000U+60000U),70000,30000));
  CameraBudget::Policy fragmented;
  assert(!fragmented.update(0xfffff000U,70000,10000));
  assert(fragmented.update(uint32_t(0xfffff000U+6000U),70000,10000)&&fragmented.tier==1);
}
