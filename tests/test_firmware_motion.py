"""Compile actual shared UNO safety predicates; no serial or hardware access."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_actual_uno_safety_boundaries_compile(tmp_path):
    compiler=Path.home()/'.platformio/packages/toolchain-atmelavr/bin/avr-g++.exe'
    source=tmp_path/'safety.cpp'
    source.write_text('''#include "motion_safety.h"
#include "motor_capabilities.h"
using namespace MotionSafety;
static_assert(speed(0)==0 && speed(180)==180 && speed(255)==255 && speed(256)==255 && speed(65535)==255,"full PWM bounds");
static_assert(MotorCapability::speedLimit(true,true,255)==255,"fresh UNO full power capability");
static_assert(MotorCapability::speedLimit(false,true,255)==180,"stale capability legacy fallback");
static_assert(MotorCapability::speedLimit(true,false,255)==180,"noninteger capability rejected");
static_assert(MotorCapability::speedLimit(true,true,0)==180 && MotorCapability::speedLimit(true,true,180)==180 && MotorCapability::speedLimit(true,true,256)==180,"only exact 255 opts in");
static_assert(duration(0)==50 && duration(1000)==1000 && duration(2000)==2000 && duration(65535)==2000,"duration bounds");
static_assert(forward(163)&&forward(34)&&forward(129),"forward directions");
static_assert(!forward(92)&&!forward(106)&&!forward(149)&&!forward(83)&&!forward(172),"escape directions ignore front sonar");
static_assert(guard(11,500,true)==clear,"11cm fresh allowed");
static_assert(guard(10,0,true)==sensor_near && guard(1,0,true)==sensor_near,"10cm stops");
static_assert(guard(-1,0,true)==sensor_null,"invalid stops");
static_assert(guard(100,501,true)==sensor_stale && guard(100,0,false)==sensor_stale,"stale or absent stops");
static_assert(guardWithCamera(-1,0,true,40)==clear && guardWithCamera(-1,0,true,39)==sensor_null,"camera unlocks only a missing echo");
static_assert(guardWithCamera(5,0,true,200)==sensor_near && guardWithCamera(100,501,true,200)==sensor_stale,"near or stale always stops");
static_assert(guardWithCamera(-1,501,true,200)==sensor_null && guardWithCamera(-1,0,false,200)==sensor_null,"no camera override without live sampling");
static_assert(clearanceCm(90)==0 && clearanceCm(clearanceWire(100))==100 && clearanceWire(39)==90 && clearanceCm(clearanceWire(300))==254,"old ESP byte 90 means none");
''',encoding='utf-8')
    subprocess.run([str(compiler),'-std=c++11','-mmcu=atmega328p','-fsyntax-only',
                    '-I',str(ROOT/'firmware/uno'),'-I',str(ROOT/'firmware/esp32cam'),str(source)],check=True,capture_output=True)


def test_runtime_guards_capabilities_and_wire_duration_are_connected():
    uno=(ROOT/'firmware/uno/main.cpp').read_text(encoding='utf-8')
    esp=(ROOT/'firmware/esp32cam/main.cpp').read_text(encoding='utf-8')
    # Entry and continuous checks both call the compiled predicates.
    assert 'duration=MotionSafety::duration(duration)' in uno
    assert 'forward(packet[3])?forwardBlock(millis(),clearance)' in uno
    assert 'forwardBlock(measuredAt,activeClearance)' in uno
    assert 'pulseIn(ECHO,HIGH,24000)' in uno
    assert 'uint8_t(ms),uint8_t(ms>>8)' in esp
    assert 'constrain(requested,50,2000)' in esp
    assert 'requested>350&&!motor2s' in esp
    assert 'd["motor_max_duration_ms"]=nullptr' in esp
    assert 'd["forward_stop_cm"]=nullptr' in esp
    assert 'millis()-telemetryAt>500)stopControl(!connected?"wifi_lost":"uno_stale")' in esp
    assert 'MotionSafety::speed(packet[4])' in uno
    assert '\\"motor_max_speed\\":%u' in uno and 'unsigned(MotionSafety::maxSpeed)' in uno
    assert 'MotionSafety::clearanceCm(packet[5])' in uno and 'forwardBlock(now,activeClearance)' in uno
    assert 'MotionSafety::clearanceWire(constrain(clearance,0,254))' in esp
    assert 'd["esp_motor_max_speed"]=255' in esp
    assert 'd["motor_max_speed"]=nullptr' in esp
    assert 'MotorCapability::speedLimit(telemetryAt&&millis()-telemetryAt<=500' in esp
    assert 'telemetry["motor_max_speed"].is<int>()' in esp
    assert 'constrain(d["speed"]|100,0,speedMax)' in esp
