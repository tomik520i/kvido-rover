#pragma once

namespace MotorCapability {
// Legacy or stale UNO telemetry never opts into full PWM power.
constexpr int speedLimit(bool fresh,bool integerCapability,int advertised){
  return fresh&&integerCapability&&advertised==255?255:180;
}
}
