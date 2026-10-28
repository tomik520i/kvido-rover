#pragma once
#include <stdint.h>
namespace CameraBudget {
// Critical pressure is immediate; debounce ordinary HTTP/TLS allocation peaks.
// Recover only after a full minute of continuous headroom.
struct Policy {
  uint8_t tier=2;bool healthy=false;uint32_t healthyAt=0;
  bool pressured=false;uint32_t pressureAt=0;
  bool update(uint32_t now,uint32_t freeInternal,uint32_t largest){
    uint8_t before=tier;
    if(freeInternal<24576||largest<8192){tier=0;healthy=false;pressured=false;}
    else if(freeInternal<36864||largest<12288){
      healthy=false;
      if(!pressured){pressured=true;pressureAt=now;}
      else if(uint32_t(now-pressureAt)>=6000&&tier>1)tier=1;
    }
    else if(tier<2&&freeInternal>=(tier==0?40960U:49152U)&&largest>=(tier==0?12288U:16384U)){
      pressured=false;
      if(!healthy){healthy=true;healthyAt=now;}
      else if(uint32_t(now-healthyAt)>=60000){++tier;healthy=false;}
    }else {healthy=false;pressured=false;}
    return tier!=before;
  }
  uint32_t interval()const{return tier==2?500:1000;}
};
}
