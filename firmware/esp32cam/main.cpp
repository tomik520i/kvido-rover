#include <Arduino.h>
#include <atomic>
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <ESPmDNS.h>
#include <ArduinoJson.h>
#include "esp_camera.h"
#include "esp_http_server.h"
#include "esp_wifi.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_task_wdt.h"
#include "performance.h"
#include "device_secrets.h"
#include "relay_secrets.h"
#ifndef KVIDO_PUBLIC_URL
#define KVIDO_PUBLIC_URL "https://rover.example.com/kvido/"
#endif
#include "portal.h"
#include "native_web.h"
#include "esp_ota_ops.h"
#include "mbedtls/sha256.h"
#include "motor_capabilities.h"
#include "control_owner.h"
#include "camera_budget.h"
#include "../uno/motion_safety.h"

// Existing generated headers used one password for both services.
#ifndef WIFI_AP_PASSWORD
#define WIFI_AP_PASSWORD SETUP_PASSWORD
#endif

// GPIO13/14 match the vendor UART2 declarations. Confirm actual cabling before driving.
// For a board wired to U0T/U0R compile with MOTOR_UART=0 instead; never print logs there.
#ifndef MOTOR_UART
#define MOTOR_UART 2
#endif
#if MOTOR_UART == 2
HardwareSerial &motorSerial=Serial2;
#else
HardwareSerial &motorSerial=Serial;
#endif
Preferences prefs;
NativeWebServer *setupWeb=nullptr;
NativeWebServer *lanWeb;
httpd_handle_t streamServer=nullptr;
SemaphoreHandle_t cameraLock;
SemaphoreHandle_t stateLock;
uint16_t lanPort;
String serialLine, pendingSsid, pendingPass, wifiError;
DynamicJsonDocument telemetry(2048);
uint32_t telemetryAt=0,joinAt=0,wifiRetryAt=0;
std::atomic<uint32_t> wifiDisconnectCount{0},wifiDisconnectAt{0},wifiRetryCount{0},wifiRoamCount{0};
bool roamScanning=false;uint32_t weakSince=0,roamAt=0;
std::atomic<int> wifiDisconnectReason{0};
void wifiEvent(WiFiEvent_t event,WiFiEventInfo_t info){
  if(event==ARDUINO_EVENT_WIFI_STA_DISCONNECTED){wifiDisconnectReason=info.wifi_sta_disconnected.reason;wifiDisconnectAt=millis();wifiDisconnectCount.fetch_add(1);}
}
const char *firmwareVersion="2026-10-25k-adaptive-vga-memory";
constexpr uint8_t kvidoWifiProtocols=WIFI_PROTOCOL_11B|WIFI_PROTOCOL_11G|WIFI_PROTOCOL_11N;
std::atomic<uint32_t> networkTrafficAt{0};
std::atomic<bool> wifiRecovering{false};
std::atomic<bool> setupOnly{false},setupOnlyRequest{false};
bool loopWatchdogReady=false;
std::atomic<uint32_t> httpRequests{0},captureCount{0},captureFailures{0};
uint32_t loopMaxMs=0,uartFrames=0,uartParseErrors=0,uartOverflows=0,loopGapMaxMs=0,lastLoopAt=0;
ControlOwner::State controlOwner;
std::atomic<bool> otaInProgress{false};
const char *lastStopReason="boot";
uint32_t lastStopAt=0;
bool wasConnected=false, wifiCompatibilityReady=false;
bool joining=false, mdnsReady=false;
std::atomic<bool> cameraReady{false};
uint8_t cameraMode=1; // 0 off, 1 adaptive single buffer, 2 original diagnostic VGA.
CameraBudget::Policy cameraBudget;
std::atomic<uint32_t> cameraWidth{640},cameraHeight{480},cameraInterval{500},cameraReconfigs{0},cameraInitErrors{0};
std::atomic<uint32_t> cameraDelivered{0},cameraThrottled{0};
std::atomic<uint32_t> cameraMinInternal{UINT32_MAX},cameraMinBlock{UINT32_MAX};
std::atomic<uint32_t> cameraChangeInternal{0},cameraChangeBlock{0};
uint32_t cameraLastDeliveredAt=0; // protected by cameraLock, shared by all transports
static void appendCameraProfile(JsonDocument &d){
  auto c=d.createNestedObject("camera_profile");c["adaptive"]=cameraMode==1;c["width"]=cameraWidth.load();c["height"]=cameraHeight.load();
  c["interval_ms"]=cameraInterval.load();c["max_fps"]=1000.0f/cameraInterval.load();c["buffers"]=cameraMode==2&&psramFound()?2:1;
  c["reconfigurations"]=cameraReconfigs.load();c["init_errors"]=cameraInitErrors.load();
  c["delivered_frames"]=cameraDelivered.load();c["throttled_requests"]=cameraThrottled.load();
  c["budget_min_internal"]=cameraMinInternal.load();c["budget_min_block"]=cameraMinBlock.load();
  c["change_free_internal"]=cameraChangeInternal.load();c["change_largest_block"]=cameraChangeBlock.load();
}
bool lightOn=false,otaConfirmed=false;
uint32_t lightUntil=0;
uint32_t motorUntil=0;
// 96/255: the AI could not see in dim rooms at 32; still far below the LED's full current.
void light(bool on){lightOn=on;ledcWrite(7,on?96:0);lightUntil=millis()+60000;}
const char *headerKeys[]={"X-Device-Key","Origin","Host"};

uint8_t crc8(const uint8_t *p,uint8_t n){uint8_t c=0;while(n--){c^=*p++;for(uint8_t b=0;b<8;b++)c=(c&0x80)?(c<<1)^0x07:c<<1;}return c;}
void command(uint8_t cmd,uint8_t dir=0,uint8_t speed=0,uint8_t angle=90,uint16_t ms=100){
  if(cmd==2)controlOwner.clear();
  motorUntil=cmd==0&&dir&&speed?millis()+ms:0;
  uint8_t p[9]={0xAA,0x55,cmd,dir,speed,angle,uint8_t(ms),uint8_t(ms>>8),0};p[8]=crc8(p,8);motorSerial.write(p,9);
}
void stopControl(const char *reason){lastStopReason=reason;lastStopAt=millis();command(2);}
template<class Web> bool auth(Web &w){
  ++httpRequests;
  if(w.hasHeader("X-Device-Key")&&w.header("X-Device-Key")==DEVICE_KEY){networkTrafficAt=millis();return true;}
  if(!w.authenticate("admin",SETUP_PASSWORD)){w.requestAuthentication();return false;}
  networkTrafficAt=millis();
  if(w.method()!=HTTP_GET&&w.hasHeader("Origin")&&w.header("Origin")!="http://"+w.header("Host")){w.send(403,"text/plain","Origin rejected");return false;}
  return true;
}
template<class Web> void json(Web &w,const JsonDocument &d,int code=200){String s;serializeJson(d,s);w.sendHeader("Cache-Control","no-store");w.send(code,"application/json",s);}
String deviceURL(){return "http://"+WiFi.localIP().toString()+":"+String(lanPort);}
template<class Web> void network(Web &w){if(!auth(w))return;StaticJsonDocument<384>d;d["connected"]=WiFi.status()==WL_CONNECTED;d["joining"]=joining;d["error"]=wifiError;d["device_url"]=WiFi.status()==WL_CONNECTED?deviceURL():"";d["ssid"]=WiFi.SSID();json(w,d);}
static void appendRelayStatus(JsonDocument &d);
static void appendNetworkHealth(JsonDocument &d);
void fillStatus(JsonDocument &d){
  d.set(telemetry);
  d["telemetry_age_ms"]=telemetryAt?millis()-telemetryAt:999999;d["rssi"]=WiFi.status()==WL_CONNECTED?WiFi.RSSI():0;
  d["uptime_ms"]=millis();d["connected"]=true;d["camera_ready"]=cameraReady.load();d["battery_v"]=nullptr;d["free_heap"]=ESP.getFreeHeap();d["port"]=lanPort;d["uart"]=MOTOR_UART;
  d["local_ip"]=WiFi.status()==WL_CONNECTED?WiFi.localIP().toString():"";d["ota_confirmed"]=otaConfirmed;d["ota_partition"]=esp_ota_get_running_partition()->label;
  d["light_supported"]=true;d["light_on"]=lightOn;
  d["esp_motor_max_speed"]=255;d["esp_camera_clearance"]=true;d["firmware"]=firmwareVersion;d["camera_rotation"]=180;
  uint8_t protocol=0;wifi_ps_type_t ps=WIFI_PS_MIN_MODEM;
  esp_wifi_get_protocol(WIFI_IF_STA,&protocol);esp_wifi_get_ps(&ps);
  d["wifi_protocol"]=protocol;d["wifi_power_save"]=int(ps);d["wifi_channel"]=WiFi.channel();
  d["wifi_disconnect_reason"]=wifiDisconnectReason.load();d["wifi_disconnect_count"]=wifiDisconnectCount.load();
  d["wifi_disconnect_at_ms"]=wifiDisconnectAt.load();d["wifi_retry_count"]=wifiRetryCount.load();d["wifi_roam_count"]=wifiRoamCount.load();d["wifi_bssid"]=WiFi.status()==WL_CONNECTED?WiFi.BSSIDstr():"";
  d["wifi_compatibility_ready"]=wifiCompatibilityReady;d["http_requests"]=httpRequests.load();
  d["loop_max_ms"]=loopMaxMs;d["min_free_heap"]=ESP.getMinFreeHeap();d["reset_reason"]=int(esp_reset_reason());
  d["largest_internal_block"]=heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
  d["control_owner"]=ControlOwner::name(controlOwner.owner);d["control_generation"]=controlOwner.generation;
  d["last_stop_reason"]=lastStopReason;d["last_stop_at_ms"]=lastStopAt;
  d["uart_frames"]=uartFrames;d["uart_parse_errors"]=uartParseErrors;d["uart_overflows"]=uartOverflows;
  d["loop_gap_max_ms"]=loopGapMaxMs;d["capture_count"]=captureCount.load();d["capture_failures"]=captureFailures.load();
  d["setup_only"]=setupOnly.load();d["ap_ip"]=WiFi.softAPIP().toString();d["wifi_mode"]=int(WiFi.getMode());d["loop_watchdog"]=loopWatchdogReady;
  d["camera_mode"]=cameraMode;
  Performance::append(d);
  appendCameraProfile(d);
  if(!telemetryAt||millis()-telemetryAt>500){
    d["distance_cm"]=nullptr;d["motor_max_duration_ms"]=nullptr;d["motor_max_speed"]=nullptr;
d["camera_clearance_min_cm"]=nullptr;d["forward_stop_cm"]=nullptr;
  }
  appendRelayStatus(d);
  appendNetworkHealth(d);
}
template<class Web> void status(Web &w){
  if(!auth(w))return;DynamicJsonDocument d(8192);fillStatus(d);json(w,d);
}
int applyControl(const JsonDocument &d,const char *&error,ControlOwner::Source source=ControlOwner::Lan){
  String a=d["action"]|"";
  if(a=="estop"){stopControl("operator_estop");light(false);return 200;}
  if(otaInProgress){error="OTA in progress";return 409;}
  if(!controlOwner.allows(source)){error="Other transport owns control; STOP before switching";return 409;}
  if(a=="stop")command(4);
  else if(a=="light_on"||a=="light_off"){command(4);light(a=="light_on");}
  else if(a=="release"){controlOwner.claim(source);command(3);}
  else if(a=="tilt")command(1,0,0,constrain(d["tilt"]|90,35,145));
  else{
    if(!controlOwner.owns(source)){error="Release required for this transport";return 409;}
    uint8_t dir=0;
    if(a=="forward")dir=163;else if(a=="backward")dir=92;else if(a=="left")dir=106;else if(a=="right")dir=149;
    else if(a=="forward_left")dir=34;else if(a=="forward_right")dir=129;else if(a=="back_left")dir=72;else if(a=="back_right")dir=20;
    else if(a=="rotate_left")dir=83;else if(a=="rotate_right")dir=172;else{error="Unknown action";return 422;}
    if(!telemetryAt||millis()-telemetryAt>500){command(4);error="UNO telemetry missing";return 409;}
    // Long pulses require the UNO itself to advertise the matching capability.
    bool motor2s=telemetry["motor_max_duration_ms"].as<int>()==2000&&telemetry["forward_stop_cm"].as<int>()==10;
    int requested=d["duration_ms"]|(motor2s?1000:200);
    if(requested>350&&!motor2s){command(4);error="UNO long motor capability missing";return 409;}
    int speedMax=MotorCapability::speedLimit(telemetryAt&&millis()-telemetryAt<=500,
      telemetry["motor_max_speed"].is<int>(),telemetry["motor_max_speed"]|0);
    // Camera radar's free floor ahead (server) for when the sonar has no echo; the UNO decides.
    int clearance=d["clearance_cm"]|0;
    command(0,dir,constrain(d["speed"]|100,0,speedMax),MotionSafety::clearanceWire(constrain(clearance,0,254)),constrain(requested,50,2000));
  }
  return 200;
}
template<class Web> void control(Web &w){
  if(!auth(w))return;StaticJsonDocument<384>d;
  if(w.arg("plain").length()>512||deserializeJson(d,w.arg("plain"))){w.send(400,"text/plain","Invalid JSON");return;}
  const char *error=nullptr;int code=applyControl(d,error);
  if(error){w.send(code,"text/plain",error);return;}
  w.send(200,"application/json","{\"ok\":true}");
}
void sendJpeg(NativeWebServer &w,camera_fb_t *fb){w.sendBytes(200,"image/jpeg",(const char*)fb->buf,fb->len);}
static camera_fb_t *freshCameraFrame(){
  if(!cameraReady)return nullptr;
  if(cameraLastDeliveredAt&&uint32_t(millis()-cameraLastDeliveredAt)<cameraInterval.load()){
    cameraThrottled.fetch_add(1);return nullptr;
  }
  int64_t requestedAt=esp_timer_get_time();
  for(int i=0;i<3;++i){camera_fb_t *fb=Performance::frame();if(!fb)return nullptr;
    if(int64_t(fb->timestamp.tv_sec)*1000000+fb->timestamp.tv_usec>=requestedAt){cameraLastDeliveredAt=millis();cameraDelivered.fetch_add(1);return fb;}
    esp_camera_fb_return(fb);
  }return nullptr;
}
template<class Web> void capture(Web &w){
  if(!auth(w))return;
  if(!cameraReady||xSemaphoreTake(cameraLock,pdMS_TO_TICKS(2000))!=pdTRUE){captureFailures.fetch_add(1);w.send(503,"text/plain","Camera busy");return;}
  camera_fb_t *fb=freshCameraFrame();
  if(!fb){captureFailures.fetch_add(1);xSemaphoreGive(cameraLock);w.send(503,"text/plain","Camera failed");return;}
  // NativeWebServer copies the JPEG to PSRAM, then sends after this handler
  // returns and both the driver buffer and camera lock have been released.
  w.sendHeader("Cache-Control","no-store");sendJpeg(w,fb);
  esp_camera_fb_return(fb);xSemaphoreGive(cameraLock);captureCount.fetch_add(1);
}
static void requestRelayReconnect();
template<class Web> void installRoutes(Web &w){
  w.collectHeaders(headerKeys,3);
  w.on("/",HTTP_GET,[&w](){if(auth(w)){String page=FPSTR(PORTAL);page.replace("https://rover.example.com/kvido/",KVIDO_PUBLIC_URL);w.send(200,"text/html; charset=utf-8",page);}});
  w.on("/api/status",HTTP_GET,[&w](){status(w);});
  w.on("/api/network",HTTP_GET,[&w](){network(w);});
  w.on("/api/control",HTTP_POST,[&w](){control(w);});
  w.onUnlocked("/capture",HTTP_GET,[&w](){capture(w);});
  w.on("/api/relay/reconnect",HTTP_POST,[&w](){if(!auth(w))return;requestRelayReconnect();w.send(200,"application/json","{\"ok\":true}");});
  w.on("/api/wifi/scan",HTTP_POST,[&w](){if(!auth(w))return;if(joining){w.send(409,"application/json","{\"error\":\"Prave se pripojuji\"}");return;}command(4);WiFi.scanDelete();WiFi.scanNetworks(true);w.send(200,"application/json","{\"scanning\":true}");});
  w.on("/api/wifi/scan",HTTP_GET,[&w](){if(!auth(w))return;DynamicJsonDocument d(4096);int n=WiFi.scanComplete();d["scanning"]=n==WIFI_SCAN_RUNNING;auto a=d.createNestedArray("networks");if(n>=0)for(int i=0;i<n&&i<25;i++){auto o=a.createNestedObject();o["ssid"]=WiFi.SSID(i);o["rssi"]=WiFi.RSSI(i);}json(w,d);});
  w.on("/api/wifi",HTTP_POST,[&w](){
    if(!auth(w))return;StaticJsonDocument<256>d;
    if(joining||w.arg("plain").length()>512||deserializeJson(d,w.arg("plain"))){w.send(400,"text/plain","Invalid request");return;}
    String ssid=d["ssid"]|"",pass=d["password"]|"";
    if(ssid.length()<1||ssid.length()>32||pass.length()>63||(pass.length()>0&&pass.length()<8)){w.send(422,"text/plain","Invalid SSID/password");return;}
    stopControl("wifi_configuration");pendingSsid=ssid;pendingPass=pass;wifiError="";joining=true;joinAt=millis();
    setupOnly=false;prefs.putBool("setup_only",false);WiFi.mode(WIFI_AP_STA);WiFi.setAutoReconnect(true);
    w.send(202,"application/json","{\"joining\":true}");WiFi.disconnect(false,false);WiFi.begin(ssid.c_str(),pass.c_str());
  });
  w.on("/connection.json",HTTP_GET,[&w](){
    if(!auth(w))return;if(WiFi.status()!=WL_CONNECTED){w.send(409,"text/plain","No LAN address yet");return;}
    StaticJsonDocument<384>d;d["kind"]="rover-connection-v1";d["device_url"]=deviceURL();d["device_key"]=DEVICE_KEY;d["mdns"]="http://kvido.local:"+String(lanPort);
    w.sendHeader("Content-Disposition","attachment; filename=rover-connection.json");json(w,d);
  });
  w.on("/connection.html",HTTP_GET,[&w](){
    if(!auth(w))return;if(WiFi.status()!=WL_CONNECTED){w.send(409,"text/plain","No LAN address yet");return;}
    String html="<!doctype html><meta charset=utf-8><title>Kvido</title><h1>Pripojeni k auticku</h1><p>Odkudkoliv pres internet: <a href=\""+String(KVIDO_PUBLIC_URL)+"\">"+String(KVIDO_PUBLIC_URL)+"</a>. Auticko musi byt pripojene k Wi-Fi s internetem.</p><a href='"+deviceURL()+"'>"+deviceURL()+"</a><p>Telefon musi byt na stejne domaci Wi-Fi. Pri zmene IP pouzij <a href='http://kvido.local:"+String(lanPort)+"'>kvido.local</a> nebo znovu otevri Kvido-Setup.</p>";
    w.sendHeader("Content-Disposition","attachment; filename=Kvido-auticko.html");w.send(200,"text/html",html);
  });
  w.begin();
  w.enableDelay(false);
}
esp_err_t stream(httpd_req_t *req){
  char key[96];if(httpd_req_get_hdr_value_str(req,"X-Device-Key",key,sizeof(key))!=ESP_OK||String(key)!=DEVICE_KEY){httpd_resp_set_status(req,"401 Unauthorized");return httpd_resp_send(req,"Unauthorized",HTTPD_RESP_USE_STRLEN);}
  httpd_resp_set_type(req,"multipart/x-mixed-replace;boundary=frame");httpd_resp_set_hdr(req,"Cache-Control","no-store");
  while(cameraReady){
    if(xSemaphoreTake(cameraLock,pdMS_TO_TICKS(2000))!=pdTRUE)break;
    camera_fb_t *fb=freshCameraFrame();if(!fb){xSemaphoreGive(cameraLock);break;}
    size_t frameLen=fb->len;uint8_t *copy=(uint8_t*)ps_malloc(frameLen);
    if(copy)memcpy(copy,fb->buf,frameLen);
    esp_camera_fb_return(fb);xSemaphoreGive(cameraLock);
    if(!copy){captureFailures.fetch_add(1);break;}
    char header[96];int len=snprintf(header,sizeof(header),"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",unsigned(frameLen));
    int64_t sendStarted=esp_timer_get_time();esp_err_t res=httpd_resp_send_chunk(req,header,len);
    if(res==ESP_OK)res=httpd_resp_send_chunk(req,(const char*)copy,frameLen);
    if(res==ESP_OK)res=httpd_resp_send_chunk(req,"\r\n",2);
    Performance::sent(Performance::Stream,frameLen,sendStarted,res==ESP_OK);
    free(copy);if(res!=ESP_OK)break;networkTrafficAt=millis();vTaskDelay(pdMS_TO_TICKS(cameraInterval.load()));
  }
  return ESP_OK;
}
static bool initCameraDriver(uint8_t tier){
  camera_config_t c={};c.ledc_channel=LEDC_CHANNEL_0;c.ledc_timer=LEDC_TIMER_0;
  c.pin_d0=5;c.pin_d1=18;c.pin_d2=19;c.pin_d3=21;c.pin_d4=36;c.pin_d5=39;c.pin_d6=34;c.pin_d7=35;
  c.pin_xclk=0;c.pin_pclk=22;c.pin_vsync=25;c.pin_href=23;c.pin_sccb_sda=26;c.pin_sccb_scl=27;c.pin_pwdn=32;c.pin_reset=-1;
  c.xclk_freq_hz=20000000;c.pixel_format=PIXFORMAT_JPEG;c.frame_size=FRAMESIZE_VGA;c.jpeg_quality=18;c.fb_count=psramFound()?2:1;
  c.fb_location=psramFound()?CAMERA_FB_IN_PSRAM:CAMERA_FB_IN_DRAM;c.grab_mode=CAMERA_GRAB_LATEST;
  if(cameraMode==1){c.fb_count=1;c.grab_mode=CAMERA_GRAB_WHEN_EMPTY;c.frame_size=tier==2?FRAMESIZE_VGA:tier==1?FRAMESIZE_QVGA:FRAMESIZE_QQVGA;c.jpeg_quality=tier==2?16:tier==1?18:20;}
  if(!psramFound())c.frame_size=FRAMESIZE_QVGA;
  cameraReady=esp_camera_init(&c)==ESP_OK;
  if(!cameraReady){cameraInitErrors.fetch_add(1);return false;}
  cameraWidth=c.frame_size==FRAMESIZE_VGA?640:c.frame_size==FRAMESIZE_QVGA?320:160;
  cameraHeight=c.frame_size==FRAMESIZE_VGA?480:c.frame_size==FRAMESIZE_QVGA?240:120;
  cameraInterval=cameraMode==1?(tier==2?500:1000):500;
  // Rotate at the source so snapshots, MJPEG and AI all see the same orientation.
  if(cameraReady){sensor_t *s=esp_camera_sensor_get();s->set_vflip(s,1);s->set_hmirror(s,1);}
  return true;
}
static void cameraBudgetWorker(void*){
  for(;;){
    vTaskDelay(pdMS_TO_TICKS(2000));if(cameraMode!=1||otaInProgress)continue;
    if(xSemaphoreTake(cameraLock,pdMS_TO_TICKS(20))!=pdTRUE)continue;
    uint32_t freeInternal=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    uint32_t largest=heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    if(freeInternal<cameraMinInternal.load())cameraMinInternal=freeInternal;
    if(largest<cameraMinBlock.load())cameraMinBlock=largest;
    bool changed=cameraBudget.update(millis(),freeInternal,largest);
    // No outstanding driver frame can survive this mutex. Reinitialization
    // actually frees/reallocates DMA buffers; just set_framesize would not.
    if(changed||!cameraReady){
      cameraChangeInternal=freeInternal;cameraChangeBlock=largest;
      if(cameraReady){cameraReady=false;esp_camera_deinit();}
      if(!initCameraDriver(cameraBudget.tier)&&cameraBudget.tier){cameraBudget.tier=0;cameraBudget.healthy=false;initCameraDriver(0);}
      cameraReconfigs.fetch_add(1);
    }
    xSemaphoreGive(cameraLock);
  }
}
void setupCamera(){
  cameraMode=prefs.getUChar("camera_mode",1);if(cameraMode>2)cameraMode=1;
  if(cameraMode==0){pinMode(32,OUTPUT);digitalWrite(32,HIGH);cameraReady=false;return;}
  initCameraDriver(2);
  if(cameraReady){httpd_config_t conf=HTTPD_DEFAULT_CONFIG();conf.server_port=lanPort+1;conf.ctrl_port=32768;conf.send_wait_timeout=2;conf.recv_wait_timeout=2;conf.max_open_sockets=3;
    if(httpd_start(&streamServer,&conf)==ESP_OK){httpd_uri_t route={};route.uri="/stream";route.method=HTTP_GET;route.handler=stream;httpd_register_uri_handler(streamServer,&route);}}
  if(cameraMode==1)xTaskCreatePinnedToCore(cameraBudgetWorker,"kvido-camera",4096,nullptr,1,nullptr,1);
}
#include "relay.h"
#include "network_recovery.h"

#if defined(KVIDO_BENCH_DIAG) && KVIDO_BENCH_DIAG
static void benchMark(const char *s){Serial.printf("BENCH %lu %s\n",(unsigned long)millis(),s);}
static void benchPoll(){
  static uint32_t last=0;if(millis()-last<2000)return;last=millis();
  Serial.printf("BENCH %lu wifi=%d ip=%s heap=%u minheap=%u ",(unsigned long)millis(),int(WiFi.status()),WiFi.localIP().toString().c_str(),ESP.getFreeHeap(),ESP.getMinFreeHeap());
  DynamicJsonDocument diag(1536);appendRelayStatus(diag);serializeJson(diag,Serial);Serial.println();
}
#else
static void benchMark(const char*){}
static void benchPoll(){}
#endif

// Service-only UART0 diagnostic: disarm and physically disconnect the UNO before
// attaching a USB console. The exact query always stops; it can never release/move.
static bool serviceStatusQuery(const String &line){
#if MOTOR_UART == 0
  String query=line;query.trim();
  if(query=="KVIDO_CAMERA_OFF_V1"||query=="KVIDO_CAMERA_SINGLE_V1"||query=="KVIDO_CAMERA_CONTINUOUS_V1"||query=="KVIDO_NETWORK_V1"){
    stopControl("service_profile");light(false);relayArmed=false;
    if(query=="KVIDO_NETWORK_V1"){
      if(!prefs.getString("ssid","").length()){motorSerial.println("KVIDO_NO_SAVED_NETWORK");return true;}
      prefs.putBool("setup_only",false);
    }else prefs.putUChar("camera_mode",query=="KVIDO_CAMERA_OFF_V1"?0:query=="KVIDO_CAMERA_SINGLE_V1"?1:2);
    motorSerial.println("KVIDO_PROFILE_RESTART");motorSerial.flush();delay(50);ESP.restart();return true;
  }
  if(query=="KVIDO_PERF_V1"){
    stopControl("service_console");light(false);relayArmed=false;
    static uint32_t at=0;if(at&&millis()-at<2000)return true;at=millis();
    DynamicJsonDocument d(4096);Performance::append(d);
    d["firmware"]=firmwareVersion;d["uptime_ms"]=millis();d["camera_mode"]=cameraMode;d["camera_ready"]=cameraReady.load();appendCameraProfile(d);
    d["wifi_status"]=int(WiFi.status());d["wifi_mode"]=int(WiFi.getMode());d["local_ip"]=WiFi.localIP().toString();
    d["wifi_disconnect_count"]=wifiDisconnectCount.load();d["wifi_disconnect_reason"]=wifiDisconnectReason.load();
    d["rssi"]=WiFi.status()==WL_CONNECTED?WiFi.RSSI():0;d["free_heap"]=ESP.getFreeHeap();
    d["largest_internal_block"]=heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    d["loop_max_ms"]=loopMaxMs;d["loop_gap_max_ms"]=loopGapMaxMs;d["relay_online"]=relayOnline.load();d["reset_reason"]=int(esp_reset_reason());
    appendNetworkHealth(d);motorSerial.print("KVIDO_PERF ");serializeJson(d,motorSerial);motorSerial.println();return true;
  }
  if(line=="KVIDO_SETUP_V1"||line=="KVIDO_SETUP_V1\r"){
    stopControl("service_setup");light(false);relayArmed=false;setupOnly=true;setupOnlyRequest=true;
    joining=false;pendingPass="";prefs.putBool("setup_only",true);
    motorSerial.println("KVIDO_SETUP_REQUESTED");return true;
  }
  if(line!="KVIDO_STATUS_V1"&&line!="KVIDO_STATUS_V1\r")return false;
  stopControl("service_console");light(false);relayArmed=false;relayFailStop=true;
  static bool answered=false;static uint32_t answeredAt=0;
  if(answered&&millis()-answeredAt<2000)return true;
  answered=true;answeredAt=millis();
  DynamicJsonDocument d(8192);fillStatus(d);
  d["wifi_status"]=int(WiFi.status());d["local_ip"]=WiFi.localIP().toString();
  d["saved_ssid_present"]=prefs.getString("ssid","").length()>0;
  motorSerial.print("KVIDO_STATUS ");serializeJson(d,motorSerial);motorSerial.println();
  return true;
#else
  return false;
#endif
}

// Arduino would mark a fresh OTA image valid at boot; instead the image must
// prove itself (otaConfirm), otherwise the bootloader rolls back on next reset.
bool verifyRollbackLater(){return true;}
void otaConfirm(){
  if(otaConfirmed)return;
  bool unoTalks=telemetryAt&&millis()-telemetryAt<=500;
  if(relayOnline||(WiFi.status()==WL_CONNECTED&&unoTalks&&millis()>30000)){esp_ota_mark_app_valid_cancel_rollback();otaConfirmed=true;}
}
// LAN-only firmware upload: device key, standing car, SHA-256 of the whole image.
static esp_err_t otaFail(httpd_req_t *req,const char *status,const char *message){
  httpd_resp_set_status(req,status);httpd_resp_set_hdr(req,"Connection","close");httpd_resp_send(req,message,HTTPD_RESP_USE_STRLEN);return ESP_FAIL;
}
static esp_err_t otaUpload(httpd_req_t *req){
  char key[96]={0},expected[72]={0};
  if(httpd_req_get_hdr_value_str(req,"X-Device-Key",key,sizeof(key))!=ESP_OK||String(key)!=DEVICE_KEY)return otaFail(req,"401 Unauthorized","Device key required");
  if(httpd_req_get_hdr_value_str(req,"X-Firmware-SHA256",expected,sizeof(expected))!=ESP_OK||strlen(expected)!=64)return otaFail(req,"400 Bad Request","X-Firmware-SHA256 required");
  const esp_partition_t *target=esp_ota_get_next_update_partition(nullptr);
  if(!target||req->content_len<65536||req->content_len>target->size)return otaFail(req,"400 Bad Request","No OTA partition or bad image size");
  if(xSemaphoreTake(stateLock,pdMS_TO_TICKS(2500))!=pdTRUE)return otaFail(req,"503 Service Unavailable","Busy");
  // Never flash a driving car. Without fresh UNO telemetry (bench) nothing can move.
  bool unoTalks=telemetryAt&&millis()-telemetryAt<=500;
  bool idle=!motorUntil&&controlOwner.owner==ControlOwner::None&&(!unoTalks||telemetry["estop"].as<bool>());
  if(idle){otaInProgress=true;stopControl("ota");light(false);relayArmed=false;relayFailStop=true;}
  xSemaphoreGive(stateLock);
  if(!idle)return otaFail(req,"409 Conflict","Press STOP and wait for the car to stand still");
  struct OtaGuard { ~OtaGuard(){otaInProgress=false;} } otaGuard;
  esp_ota_handle_t ota=0;
  if(esp_ota_begin(target,req->content_len,&ota)!=ESP_OK)return otaFail(req,"500 Internal Server Error","OTA begin failed");
  mbedtls_sha256_context sha;mbedtls_sha256_init(&sha);mbedtls_sha256_starts(&sha,0);
  std::unique_ptr<char[]> buf(new char[4096]);size_t left=req->content_len;int timeouts=0;
  while(left){
    int n=httpd_req_recv(req,buf.get(),std::min<size_t>(4096,left));
    if(n==HTTPD_SOCK_ERR_TIMEOUT&&++timeouts<10)continue;
    if(n<=0||esp_ota_write(ota,buf.get(),n)!=ESP_OK){esp_ota_abort(ota);mbedtls_sha256_free(&sha);return otaFail(req,"400 Bad Request","Upload interrupted");}
    mbedtls_sha256_update(&sha,(const unsigned char*)buf.get(),n);left-=n;timeouts=0;
  }
  unsigned char digest[32];char actual[65];mbedtls_sha256_finish(&sha,digest);mbedtls_sha256_free(&sha);
  for(int i=0;i<32;++i)snprintf(actual+2*i,3,"%02x",digest[i]);
  if(strcasecmp(actual,expected)){esp_ota_abort(ota);return otaFail(req,"422 Unprocessable Entity","SHA-256 mismatch");}
  if(esp_ota_end(ota)!=ESP_OK||esp_ota_set_boot_partition(target)!=ESP_OK)return otaFail(req,"500 Internal Server Error","Image rejected");
  httpd_resp_set_type(req,"application/json");httpd_resp_set_hdr(req,"Connection","close");
  httpd_resp_sendstr(req,"{\"ok\":true,\"rebooting\":true}");
  delay(500);ESP.restart();
  return ESP_OK;
}
void setup(){
#if MOTOR_UART == 2
  motorSerial.begin(115200,SERIAL_8N1,14,13);
#else
  // UART0 belongs exclusively to the UNO protocol, including during TLS errors.
  esp_log_level_set("*",ESP_LOG_NONE);
  motorSerial.begin(115200);
#endif
  benchMark("uart ready, UNO MUST BE DISCONNECTED");
  command(2);ledcSetup(7,5000,8);ledcAttachPin(4,7);light(false);
  prefs.begin("kvido",false);lanPort=prefs.getUShort("port",0);if(lanPort<20000||lanPort>39998){lanPort=20000+esp_random()%19998;prefs.putUShort("port",lanPort);}
  benchMark("before WiFi.mode");
  WiFi.onEvent(wifiEvent);
  setupOnly=prefs.getBool("setup_only",false);
  WiFi.mode(setupOnly?WIFI_AP:WIFI_AP_STA);WiFi.setSleep(false);WiFi.setAutoReconnect(!setupOnly);
  // Several access points share the SSID: scan all channels and join the strongest.
  WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
  esp_err_t staProtocol=esp_wifi_set_protocol(WIFI_IF_STA,kvidoWifiProtocols);
  esp_err_t apProtocol=esp_wifi_set_protocol(WIFI_IF_AP,kvidoWifiProtocols);
  wifiCompatibilityReady=staProtocol==ESP_OK&&apProtocol==ESP_OK;
  WiFi.softAP("Kvido-Setup",WIFI_AP_PASSWORD);
  benchMark("before camera/web setup");
  stateLock=xSemaphoreCreateMutex();cameraLock=xSemaphoreCreateMutex();setupCamera();benchMark("camera setup done");
  setupWeb=new NativeWebServer(80,stateLock,32770);installRoutes(*setupWeb);
  lanWeb=new NativeWebServer(lanPort,stateLock);lanWeb->onRaw("/api/ota",HTTP_POST,otaUpload);installRoutes(*lanWeb);
  benchMark("camera/web setup done");
  TlsMemory::begin();
  benchMark("before relaySetup");relaySetup();benchMark("after relaySetup");
  String ssid=prefs.getString("ssid","");benchMark("before WiFi.begin");if(ssid.length()&&!setupOnly)WiFi.begin(ssid.c_str(),prefs.getString("pass","").c_str());benchMark("after WiFi.begin");
  esp_task_wdt_init(10,true);loopWatchdogReady=esp_task_wdt_add(nullptr)==ESP_OK;
  networkRecoverySetup();Performance::begin();
}
void loop(){
  benchPoll();
  uint32_t loopStart=millis();
  int64_t loopStartUs=esp_timer_get_time();
  if(xSemaphoreTake(stateLock,pdMS_TO_TICKS(10))!=pdTRUE){Performance::loopLockMisses.fetch_add(1);delay(1);return;}
  if(lastLoopAt)loopGapMaxMs=max(loopGapMaxMs,uint32_t(millis()-lastLoopAt));lastLoopAt=millis();
  bool connected=WiFi.status()==WL_CONNECTED;
  if(connected&&!wasConnected)esp_wifi_set_ps(WIFI_PS_NONE);
  wasConnected=connected;
  if(lightOn&&(int32_t)(millis()-lightUntil)>=0)light(false);
  // Read UART first: stale telemetry must not be presented as fresh.
  for(unsigned budget=0;budget<256&&motorSerial.available();++budget){char ch=motorSerial.read();if(ch=='\n'){if(serviceStatusQuery(serialLine)){serialLine="";continue;}DynamicJsonDocument d(2048);if(!deserializeJson(d,serialLine)&&d.containsKey("distance_cm")&&d.containsKey("servo")){telemetry.set(d);telemetryAt=millis();++uartFrames;}else ++uartParseErrors;serialLine="";}else if(serialLine.length()<1024)serialLine+=ch;else{serialLine="";++uartOverflows;}}
  if(motorUntil){
    if(!connected||!telemetryAt||millis()-telemetryAt>500)stopControl(!connected?"wifi_lost":"uno_stale");
    else if((int32_t)(millis()-motorUntil)>=0)motorUntil=0;
  }
  relayPoll();
  otaConfirm();
  if(joining&&WiFi.status()==WL_CONNECTED){prefs.putString("ssid",pendingSsid);prefs.putString("pass",pendingPass);pendingPass="";joining=false;}
  if(joining&&millis()-joinAt>25000){joining=false;pendingPass="";wifiError="Pripojeni selhalo. Over heslo a sit 2,4 GHz.";setupOnly=true;setupOnlyRequest=true;prefs.putBool("setup_only",true);}
  // Retry only the saved successful network; an active provisioning attempt owns Wi-Fi.
  if(connected)wifiRetryAt=millis();
  if(!joining&&!setupOnly&&!wifiRecovering&&WiFi.status()!=WL_CONNECTED&&millis()-wifiRetryAt>=5000){
    wifiRetryAt=millis();String savedSsid=prefs.getString("ssid","");
    if(savedSsid.length()){
      wifiRetryCount.fetch_add(1);wifi_config_t cfg={};
      bool sameConfig=esp_wifi_get_config(WIFI_IF_STA,&cfg)==ESP_OK&&savedSsid.length()==strnlen((char*)cfg.sta.ssid,sizeof(cfg.sta.ssid))&&memcmp(savedSsid.c_str(),cfg.sta.ssid,savedSsid.length())==0;
      if(!sameConfig||!WiFi.reconnect())WiFi.begin(savedSsid.c_str(),prefs.getString("pass","").c_str());
    }
  }
  // Roam to a clearly stronger access point of the same SSID, only while no motor
  // pulse runs: the reconnect drops the relay briefly, which also fail-stops.
  int rssi=connected?WiFi.RSSI():0;
  if(!connected||joining||setupOnly||wifiRecovering||motorUntil||rssi>-75)weakSince=0;
  else if(!weakSince)weakSince=millis();
  if(weakSince&&millis()-weakSince>15000&&millis()-roamAt>120000&&!roamScanning&&WiFi.scanComplete()!=WIFI_SCAN_RUNNING){
    roamAt=millis();roamScanning=WiFi.scanNetworks(true)==WIFI_SCAN_RUNNING;
  }
  int found=roamScanning?WiFi.scanComplete():WIFI_SCAN_RUNNING;
  if(roamScanning&&found!=WIFI_SCAN_RUNNING){
    roamScanning=false;bool stronger=false;String ssid=WiFi.SSID();
    for(int i=0;i<found;++i)if(WiFi.SSID(i)==ssid&&WiFi.RSSI(i)>=rssi+8&&memcmp(WiFi.BSSID(i),WiFi.BSSID(),6))stronger=true;
    WiFi.scanDelete();
    // No BSSID pinning: the all-channel scan picks the strongest, also on later reconnects.
    if(stronger&&connected&&!motorUntil&&!joining){wifiRoamCount.fetch_add(1);WiFi.disconnect(false,false);WiFi.begin(ssid.c_str(),prefs.getString("pass","").c_str());}
  }
  if(!mdnsReady&&WiFi.status()==WL_CONNECTED){mdnsReady=MDNS.begin("kvido");if(mdnsReady)MDNS.addService("http","tcp",lanPort);}
  loopMaxMs=max(loopMaxMs,uint32_t(millis()-loopStart));Performance::loopLastUs=uint32_t(esp_timer_get_time()-loopStartUs);
  static uint32_t stackAt=0;if(millis()-stackAt>2000){stackAt=millis();Performance::loopStack=uxTaskGetStackHighWaterMark(nullptr);}
  if(loopWatchdogReady)esp_task_wdt_reset();xSemaphoreGive(stateLock);delay(1);
}
