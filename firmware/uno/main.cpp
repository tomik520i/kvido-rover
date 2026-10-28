#include <Arduino.h>
#include <Servo.h>
#include "motion_safety.h"

// AD176: same 74HC595 motor direction bytes and pins as the vendor example.
constexpr uint8_t PWM1=5, PWM2=6, SHCP=2, ENABLE=7, DATA=8, STCP=4, TRIG=12, ECHO=13;
Servo cameraServo;
uint8_t angle=90, direction=0;
bool estop=true;
uint32_t deadline=0, lastMeasure=0, lastReport=0, measuredAt=0;
int distance=-1;
uint8_t packet[9], used=0;
uint32_t byteAt=0;
// Diagnostic values never substitute a failed current distance measurement.
uint32_t measureSeq=0,rawPulseUs=0,validCount=0,nullCount=0,lastValidAt=0;
int lastValidDistance=-1;
bool echoBefore=false,echoAfter=false;
uint32_t commandSeq=0,commandAt=0,motorStartedAt=0;
uint8_t commandCode=0,requestedMotor=0,requestedSpeed=0,actualSpeed=0;
uint16_t requestedDuration=0;
bool commandAccepted=false;
uint32_t driveSeq=0,driveAt=0,driveAcceptedCount=0,driveDeniedCount=0;
uint8_t driveMotor=0,driveSpeed=0;uint16_t driveDuration=0;
bool driveAccepted=false;const char *driveReason="none";
uint32_t nullStops=0,nearStops=0,deadlineStops=0;
// Camera clearance of the running forward pulse (0 = sonar only), and of the last accepted drive.
uint16_t activeClearance=0,lastClearance=0;
const char *commandReason="none",*lastStopReason="boot";
char report[896];uint16_t reportLength=0,reportPosition=0;

uint8_t crc8(const uint8_t *p,uint8_t n){uint8_t c=0;while(n--){c^=*p++;for(uint8_t b=0;b<8;b++)c=(c&0x80)?(c<<1)^0x07:c<<1;}return c;}
void motor(uint8_t dir,uint8_t speed,const char *stopReason=nullptr){
  bool wasActive=direction&&actualSpeed;
  if(!dir&&wasActive&&stopReason){
    if(!strcmp(stopReason,"sensor_null"))++nullStops;
    else if(!strcmp(stopReason,"sensor_near"))++nearStops;
    else if(!strcmp(stopReason,"deadline"))++deadlineStops;
  }
  direction=dir;actualSpeed=dir?speed:0;
  if(!dir)activeClearance=0;
  if(dir&&speed)motorStartedAt=millis();
  if(!dir&&stopReason)lastStopReason=stopReason;
  digitalWrite(ENABLE,LOW);analogWrite(PWM1,dir?speed:0);analogWrite(PWM2,dir?speed:0);
  digitalWrite(STCP,LOW);shiftOut(DATA,SHCP,MSBFIRST,dir);digitalWrite(STCP,HIGH);
}
bool forward(uint8_t dir){return MotionSafety::forward(dir);}
const char *forwardBlock(uint32_t now,uint16_t clearance){
  switch(MotionSafety::guardWithCamera(distance,now-measuredAt,measureSeq!=0,clearance)){
    case MotionSafety::sensor_null:return "sensor_null";
    case MotionSafety::sensor_near:return "sensor_near";
    case MotionSafety::sensor_stale:return "sensor_stale";
    default:return nullptr;
  }
}
bool validDirection(uint8_t dir){
  const uint8_t dirs[]={0,163,92,106,149,34,72,129,20,83,172};
  for(auto d:dirs){if(dir==d)return true;}
  return false;
}
void markCommand(bool accepted,const char *reason){
  commandAccepted=accepted;commandReason=reason;
  // Preserve the last DRIVE verdict through later STOP/release/tilt packets.
  if(commandCode==0){
    driveSeq=commandSeq;driveAt=commandAt;driveMotor=requestedMotor;
    driveSpeed=requestedSpeed;driveDuration=requestedDuration;
    driveAccepted=accepted;driveReason=reason;
    if(accepted)++driveAcceptedCount;else ++driveDeniedCount;
  }
}
void handlePacket(){
  ++commandSeq;commandAt=millis();commandCode=packet[2];
  requestedMotor=packet[3];requestedSpeed=packet[4];requestedDuration=packet[6]|(uint16_t(packet[7])<<8);
  if(crc8(packet,8)!=packet[8]){markCommand(false,"crc");return;}
  uint8_t cmd=packet[2];
  if(cmd==2){markCommand(true,"ok");estop=true;motor(0,0,"estop");return;}
  if(cmd==3){markCommand(true,"ok");motor(0,0,"release");estop=false;return;}
  if(cmd==4){markCommand(true,"ok");motor(0,0,"stop_command");return;}
  if(estop){markCommand(false,"estop");return;}
  if(cmd==1){markCommand(true,"ok");motor(0,0,"tilt");angle=constrain(packet[5],35,145);cameraServo.write(angle);return;}
  if(cmd!=0){markCommand(false,"unknown_command");return;}
  if(!validDirection(packet[3])){markCommand(false,"invalid_direction");return;}
  uint16_t duration=packet[6]|(uint16_t(packet[7])<<8);
  duration=MotionSafety::duration(duration);
  uint16_t clearance=MotionSafety::clearanceCm(packet[5]);
  const char *reason=forward(packet[3])?forwardBlock(millis(),clearance):nullptr;
  // A camera-cleared pulse without a sonar echo stays short whatever was requested.
  bool cameraOnly=!reason&&forward(packet[3])&&distance<0;
  if(cameraOnly&&duration>MotionSafety::cameraPulseMaxMs)duration=MotionSafety::cameraPulseMaxMs;
  if(reason){
    markCommand(false,reason);motor(0,0,reason);return;
  }
  markCommand(true,"ok");
  motor(packet[3],MotionSafety::speed(packet[4]),"zero_direction");deadline=millis()+duration;
  activeClearance=forward(packet[3])?clearance:0;lastClearance=cameraOnly?clearance:0;
}
void receive(){
  if(used&&millis()-byteAt>30)used=0;
  while(Serial.available()){
    uint8_t b=Serial.read();byteAt=millis();
    if(used==0&&b!=0xAA)continue;
    if(used==1&&b!=0x55){used=b==0xAA?1:0;continue;}
    packet[used++]=b;
    if(used==sizeof(packet)){used=0;handlePacket();}
  }
}
void setup(){
  pinMode(ENABLE,OUTPUT);digitalWrite(ENABLE,HIGH);
  const uint8_t outputs[]={PWM1,PWM2,SHCP,DATA,STCP,TRIG};
  for(uint8_t p:outputs)pinMode(p,OUTPUT);
  pinMode(ECHO,INPUT);motor(0,0,"boot");
  cameraServo.attach(9);cameraServo.write(angle);Serial.begin(115200);
}
// One bounded packet in flight. Queue only free UART TX bytes, so diagnostics
// cannot block receive(), ultrasonic checks, or the physical motor deadline.
void flushReport(){
  if(reportPosition>=reportLength)return;
  int available=Serial.availableForWrite();if(available<=0)return;
  uint16_t room=available;
  uint16_t count=min(uint16_t(32),uint16_t(reportLength-reportPosition));
  if(room<count)count=room;
  if(count)reportPosition+=Serial.write((const uint8_t*)report+reportPosition,count);
}
void makeReport(uint32_t now){
  if(reportPosition<reportLength)return;
  char current[8],previous[8];
  if(distance<0)strcpy(current,"null");else itoa(distance,current,10);
  if(lastValidDistance<0)strcpy(previous,"null");else itoa(lastValidDistance,previous,10);
  int length=snprintf_P(report,sizeof(report),PSTR(
    "{\"distance_cm\":%s,\"servo\":%u,\"estop\":%s,\"motor_byte\":%u,\"line\":[%u,%u,%u],\"motor_max_duration_ms\":%u,\"motor_max_speed\":%u,\"forward_stop_cm\":%u,\"camera_clearance_min_cm\":%u,\"camera_clearance_cm\":%u,"
    "\"ultra\":{\"seq\":%lu,\"us\":%lu,\"pre\":%s,\"post\":%s,\"ok\":%lu,\"null\":%lu,\"last_cm\":%s,\"last_age_ms\":%lu,\"age_ms\":%lu},"
    "\"cmd\":{\"seq\":%lu,\"code\":%u,\"dir\":%u,\"speed\":%u,\"ms\":%u,\"ok\":%s,\"why\":\"%s\",\"age_ms\":%lu},"
    "\"drive\":{\"seq\":%lu,\"dir\":%u,\"speed\":%u,\"ms\":%u,\"ok\":%s,\"why\":\"%s\",\"age_ms\":%lu,\"accepted\":%lu,\"denied\":%lu},"
    "\"motor\":{\"active\":%s,\"speed\":%u,\"start_ms\":%lu,\"deadline_ms\":%lu,\"stop\":\"%s\",\"null_stops\":%lu,\"near_stops\":%lu,\"deadline_stops\":%lu}}\n"),
    current,unsigned(angle),estop?"true":"false",unsigned(direction),unsigned(analogRead(A2)),unsigned(analogRead(A1)),unsigned(analogRead(A0)),
    unsigned(MotionSafety::maxDurationMs),unsigned(MotionSafety::maxSpeed),unsigned(MotionSafety::forwardStopCm),
    unsigned(MotionSafety::cameraClearanceMinCm),unsigned(lastClearance),
    (unsigned long)measureSeq,(unsigned long)rawPulseUs,echoBefore?"true":"false",echoAfter?"true":"false",
    (unsigned long)validCount,(unsigned long)nullCount,previous,(unsigned long)(lastValidDistance<0?999999UL:now-lastValidAt),(unsigned long)(measureSeq?now-measuredAt:999999UL),
    (unsigned long)commandSeq,unsigned(commandCode),unsigned(requestedMotor),unsigned(requestedSpeed),unsigned(requestedDuration),
    commandAccepted?"true":"false",commandReason,(unsigned long)(commandSeq?now-commandAt:999999UL),
    (unsigned long)driveSeq,unsigned(driveMotor),unsigned(driveSpeed),unsigned(driveDuration),driveAccepted?"true":"false",driveReason,
    (unsigned long)(driveSeq?now-driveAt:999999UL),(unsigned long)driveAcceptedCount,(unsigned long)driveDeniedCount,
    direction&&actualSpeed?"true":"false",unsigned(actualSpeed),(unsigned long)motorStartedAt,(unsigned long)deadline,lastStopReason,
    (unsigned long)nullStops,(unsigned long)nearStops,(unsigned long)deadlineStops);
  if(length>0&&length<int(sizeof(report))){reportLength=length;reportPosition=0;}
  else {reportLength=0;reportPosition=0;} // Never transmit truncated JSON.
}
void loop(){
  receive();uint32_t now=millis();
  if(direction&&((int32_t)(now-deadline)>=0||estop))motor(0,0,estop?"estop":"deadline");
  if(forward(direction)){const char *reason=forwardBlock(now,activeClearance);if(reason)motor(0,0,reason);}
  if(now-lastMeasure>=75){
    lastMeasure=now;echoBefore=digitalRead(ECHO)==HIGH;
    digitalWrite(TRIG,LOW);delayMicroseconds(2);digitalWrite(TRIG,HIGH);delayMicroseconds(10);digitalWrite(TRIG,LOW);
    // A bounded pulse read: never vendor's one-second default blocking timeout.
    unsigned long pulse=pulseIn(ECHO,HIGH,24000);
    rawPulseUs=pulse;echoAfter=digitalRead(ECHO)==HIGH;
    distance=pulse?int(pulse/58):-1;if(distance<2||distance>400)distance=-1;
    measuredAt=millis();++measureSeq;
    if(distance<0)++nullCount;else {++validCount;lastValidDistance=distance;lastValidAt=measuredAt;}
    if(forward(direction)){const char *reason=forwardBlock(measuredAt,activeClearance);if(reason)motor(0,0,reason);}
  }
  if(now-lastReport>=150){lastReport=now;makeReport(millis());}
  flushReport();
}
