#pragma once
#include <stdint.h>

// Motion profile. The default is the prepared full-power profile (not yet
// verified on the car); build env uno_legacy keeps today's 180 / 350 ms / 20 cm.
#ifndef KVIDO_MAX_DURATION_MS
#define KVIDO_MAX_DURATION_MS 2000
#endif
#ifndef KVIDO_MAX_SPEED
#define KVIDO_MAX_SPEED 255
#endif
#ifndef KVIDO_FORWARD_STOP_CM
#define KVIDO_FORWARD_STOP_CM 10
#endif

namespace MotionSafety {
constexpr uint16_t maxDurationMs=KVIDO_MAX_DURATION_MS;
constexpr uint8_t maxSpeed=KVIDO_MAX_SPEED;
constexpr uint8_t forwardStopCm=KVIDO_FORWARD_STOP_CM;
constexpr uint32_t maxSensorAgeMs=500;
// Camera clearance: free floor ahead measured by the server's camera radar.
// Packet byte 5 of a DRIVE carries 0x80|cm/2; older ESP firmware always sends
// 90 there, which decodes as "no clearance", so it can never unlock anything.
constexpr uint8_t cameraClearanceMinCm=40;
constexpr uint16_t cameraPulseMaxMs=350;
enum Guard { clear, sensor_null, sensor_near, sensor_stale };
constexpr bool forward(uint8_t dir){return dir==163||dir==34||dir==129;}
constexpr uint8_t speed(uint16_t value){return value>maxSpeed?maxSpeed:value;}
constexpr uint16_t duration(uint16_t ms){return ms<50?50:ms>maxDurationMs?maxDurationMs:ms;}
constexpr uint16_t clearanceCm(uint8_t wire){return wire&0x80?uint16_t(wire&0x7F)*2:0;}
constexpr uint8_t clearanceWire(uint16_t cm){return cm<cameraClearanceMinCm?90:uint8_t(0x80|(cm>254?254:cm)/2);}
constexpr Guard guard(int distance,uint32_t age,bool sampled){
  return distance<0?sensor_null:distance<=forwardStopCm?sensor_near:
    !sampled||age>maxSensorAgeMs?sensor_stale:clear;
}
// No echo (open room, soft or slanted faces) is the only case the camera may
// stand in for the sonar; a near or stale reading always stops.
constexpr Guard guardWithCamera(int distance,uint32_t age,bool sampled,uint16_t clearance){
  return distance<0&&sampled&&age<=maxSensorAgeMs&&clearance>=cameraClearanceMinCm?clear:guard(distance,age,sampled);
}
}
