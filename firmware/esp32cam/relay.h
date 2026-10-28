#pragma once
#include "esp_websocket_client.h"
#include <time.h>
#include <sys/time.h>
#include <atomic>
#include <stdarg.h>
#include <stdio.h>
#include "esp_timer.h"
#include "esp_sntp.h"
#include "relay_message.h"
#include "relay_secrets.h"
#ifndef KVIDO_RELAY_URL
#define KVIDO_RELAY_URL "wss://rover.example.com/kvido/device-ws"
#endif
#ifndef KVIDO_RELAY_ENABLED
#define KVIDO_RELAY_ENABLED 0
#endif
// The callback only copies bounded messages. UART and state mutations stay in loop().
struct RelayInput { char text[1024]; uint32_t generation,receivedAt; };
struct RelayCommand { char text[768]; uint32_t generation; };
struct RelayAck { uint32_t id,generation; int status; char error[64]; };
static QueueHandle_t relayInputs,relayCommands,relayAcks;
static std::atomic<bool> relayFailStop{false},relayOnline{false},relayTransportConnected{false},relayReconnect{false};
static std::atomic<uint32_t> relayGeneration{0},relayTrafficAt{0};
static esp_websocket_client_handle_t relayClient=nullptr;
static std::atomic<uint32_t> relayAttempts{0},relayConnects{0},relayDisconnects{0},relayErrors{0},relayLastEventAt{0},relayWorkerAt{0},relayNtpAt{0},relayNtpAttempts{0};
static std::atomic<int> relayLastEvent{-1},relayLastStartError{0},relayTaskError{0};
enum RelayFault { RfDisconnect, RfSdkError, RfClosed, RfRxShape, RfInputFull,
  RfTextSend, RfFrameSend, RfJson, RfWelcome, RfSession, RfCommandFull,
  RfWifiLost, RfSilence, RfStart, RfAckFull, RfDiagnostic, RfCount };
static const char *relayFaultNames[]={"disconnect","sdk_error","closed","rx_shape","input_full",
  "text_send","frame_send","json","welcome","session","command_full","wifi_lost","silence","start","ack_full","diagnostic_reconnect"};
static std::atomic<uint32_t> relayFaultCounts[RfCount],relayFaultAt{0},relaySendMaxMs{0},relayFrameMaxMs{0};
static std::atomic<int> relayFirstFault{-1},relayRxOpcode{0},relayRxLength{0},relayRxOffset{0},relayRxChunk{0};
static RelayMessage relayMessage;
static void relayMax(std::atomic<uint32_t> &value,uint32_t sample){uint32_t old=value.load();while(sample>old&&!value.compare_exchange_weak(old,sample)){};}
// All SDK log output is diverted into RAM; UART0 remains UNO-only.
static portMUX_TYPE relayLogMux=portMUX_INITIALIZER_UNLOCKED;
static char relayLastError[193]={};
static uint32_t relayLastErrorAt=0;
static char relayLastTlsError[193]={};
static uint32_t relayLastTlsErrorAt=0;
static int relayLogToRam(const char *fmt,va_list args){
  char raw[384];int result=vsnprintf(raw,sizeof(raw),fmt,args);
  if(!strstr(raw,"ssl")&&!strstr(raw,"SSL")&&!strstr(raw,"tls")&&!strstr(raw,"TLS")&&!strstr(raw,"transport")&&!strstr(raw,"websocket"))return result;
  if(strstr(raw,"Authorization")||strstr(raw,"Bearer")||strstr(raw,"header")||strstr(raw,"Header")||strstr(raw,KVIDO_RELAY_TOKEN))return result;
  unsigned priority=(strstr(raw,"mbedtls")||strstr(raw,"certificate")||strstr(raw,"-0x"))?4:
    (strstr(raw,"ssl")||strstr(raw,"SSL")||strstr(raw,"tls")||strstr(raw,"TLS"))?3:strstr(raw,"transport")?2:1;
  char clean[193]={};size_t n=0;
  // Strip ANSI escape sequences and controls, keeping a compact diagnostic line.
  for(size_t i=0;raw[i]&&n<sizeof(clean)-1;++i){
    unsigned char ch=raw[i];
    if(ch==27&&raw[i+1]=='['){i+=2;while(raw[i]&&!(raw[i]>='@'&&raw[i]<='~'))++i;if(!raw[i])break;continue;}
    if(ch>=32&&ch<127)clean[n++]=ch;
  }
  uint32_t capturedAt=millis();
  portENTER_CRITICAL(&relayLogMux);
  memcpy(relayLastError,clean,sizeof(clean));relayLastErrorAt=capturedAt;
  if(priority==4){memcpy(relayLastTlsError,clean,sizeof(clean));relayLastTlsErrorAt=capturedAt;}
  portEXIT_CRITICAL(&relayLogMux);
  return result;
}
static String relaySession;
static uint32_t relayLastId=0;
static bool relayHaveId=false;
static bool relayArmed=false;
static int64_t relayNow(){struct timeval tv;gettimeofday(&tv,nullptr);return int64_t(tv.tv_sec)*1000+tv.tv_usec/1000;}
static bool relayTimeReady(){return time(nullptr)>1767225600;}
static void appendRelayStatus(JsonDocument &d){
  char lastError[193],tlsError[193];uint32_t errorAt,tlsAt;
  portENTER_CRITICAL(&relayLogMux);memcpy(lastError,relayLastError,sizeof(lastError));errorAt=relayLastErrorAt;memcpy(tlsError,relayLastTlsError,sizeof(tlsError));tlsAt=relayLastTlsErrorAt;portEXIT_CRITICAL(&relayLogMux);
  JsonObject r=d.createNestedObject("relay");r["last_error"]=lastError;r["last_error_at_ms"]=errorAt;
  r["enabled"]=bool(KVIDO_RELAY_ENABLED);r["online"]=relayOnline.load();r["transport_connected"]=relayTransportConnected.load();
  r["clock_ready"]=relayTimeReady();r["epoch_s"]=int64_t(time(nullptr));r["connect_attempts"]=relayAttempts.load();r["connections"]=relayConnects.load();
  r["disconnects"]=relayDisconnects.load();r["error_events"]=relayErrors.load();r["last_event"]=relayLastEvent.load();r["last_event_at_ms"]=relayLastEventAt.load();
  r["last_start_error"]=relayLastStartError.load();r["task_error"]=relayTaskError.load();r["worker_at_ms"]=relayWorkerAt.load();
  r["ntp_attempts"]=relayNtpAttempts.load();r["ntp_sync_at_ms"]=relayNtpAt.load();
  r["last_tls_error"]=tlsError;r["last_tls_error_at_ms"]=tlsAt;
  int fault=relayFirstFault.load();r["invalidate_reason"]=fault>=0?relayFaultNames[fault]:"none";r["invalidate_at_ms"]=relayFaultAt.load();
  r["send_max_ms"]=relaySendMaxMs.load();r["frame_send_max_ms"]=relayFrameMaxMs.load();
  r["rx_opcode"]=relayRxOpcode.load();r["rx_payload"]=relayRxLength.load();r["rx_offset"]=relayRxOffset.load();r["rx_chunk"]=relayRxChunk.load();
  JsonObject counts=r.createNestedObject("fault_counts");for(int i=0;i<RfCount;++i)counts[relayFaultNames[i]]=relayFaultCounts[i].load();
  r["input_queued"]=relayInputs?uxQueueMessagesWaiting(relayInputs):0;
}
static void relayNtpSynced(struct timeval*){relayNtpAt=millis();}
// A brief Wi-Fi stall must not tear down TLS: a new handshake costs seconds and
// the operator's control lease. Motors never depend on these: the UNO ends every
// pulse itself and loop() stops on stale UNO telemetry.
constexpr uint32_t RELAY_TEXT_SEND_MS=1000,RELAY_FRAME_SEND_MS=1500,RELAY_SILENCE_MS=6000;
static void relayInvalidate(RelayFault cause){
  relayFaultCounts[cause].fetch_add(1);
  // Preserve the initiating cause; stop()/destroy() may emit a second disconnect.
  if(!relayReconnect.exchange(true)){relayFirstFault=cause;relayFaultAt=millis();}
  relayTransportConnected=false;relayOnline=false;relayGeneration.fetch_add(1);relayFailStop=true;
}
static void requestRelayReconnect(){if(KVIDO_RELAY_ENABLED)relayInvalidate(RfDiagnostic);}
static void relayEvent(void*,esp_event_base_t,int32_t event,void *raw){
  if(event!=WEBSOCKET_EVENT_DATA){relayLastEvent=event;relayLastEventAt=millis();}
  if(event==WEBSOCKET_EVENT_DISCONNECTED||event==WEBSOCKET_EVENT_ERROR||event==WEBSOCKET_EVENT_CLOSED){
    if(event==WEBSOCKET_EVENT_ERROR)relayErrors.fetch_add(1);else relayDisconnects.fetch_add(1);
    relayMessage.reset();relayInvalidate(event==WEBSOCKET_EVENT_ERROR?RfSdkError:event==WEBSOCKET_EVENT_CLOSED?RfClosed:RfDisconnect);return;
  }
  if(event==WEBSOCKET_EVENT_CONNECTED){relayConnects.fetch_add(1);relayMessage.reset();relayGeneration.fetch_add(1);relayFailStop=true;relayOnline=false;relayReconnect=false;relayTransportConnected=true;relayTrafficAt=millis();RelayInput in={};in.generation=relayGeneration.load();strcpy(in.text,"{\"type\":\"_connected\"}");if(xQueueSend(relayInputs,&in,0)!=pdTRUE)relayInvalidate(RfInputFull);return;}
  if(event!=WEBSOCKET_EVENT_DATA)return;
  auto *d=(esp_websocket_event_data_t*)raw;
  if(d->op_code==9||d->op_code==10)return;
  relayRxOpcode=d->op_code;relayRxLength=d->payload_len;relayRxOffset=d->payload_offset;relayRxChunk=d->data_len;
  if(d->op_code!=1){relayMessage.reset();relayInvalidate(RfRxShape);return;}
  uint32_t generation=relayGeneration.load();auto result=relayMessage.append(d->data_ptr,d->data_len,d->payload_len,d->payload_offset,generation);
  if(result==RelayMessage::Invalid){relayInvalidate(RfRxShape);return;}
  if(result==RelayMessage::More)return;
  RelayInput in={};in.generation=generation;in.receivedAt=millis();strlcpy(in.text,relayMessage.text,sizeof(in.text));
  if(xQueueSend(relayInputs,&in,0)!=pdTRUE)relayInvalidate(RfInputFull);
}
static bool relaySend(const JsonDocument &d){String out;serializeJson(d,out);uint32_t start=millis();bool ok=esp_websocket_client_send_text(relayClient,out.c_str(),out.length(),pdMS_TO_TICKS(RELAY_TEXT_SEND_MS))==int(out.length());relayMax(relaySendMaxMs,millis()-start);if(!ok)relayInvalidate(RfTextSend);return ok;}
static void relayAck(uint32_t id,int status,const char *error=nullptr){StaticJsonDocument<256>d;d["type"]="ack";d["session"]=relaySession;d["id"]=id;d["status"]=status;if(error)d["error"]=error;relaySend(d);}
static bool relayFresh(const JsonDocument &d,bool commandMessage){
  if(!relayTimeReady()||!relayOnline||relaySession!=String(d["session"]|""))return false;
  int64_t now=relayNow(),expires=d["expires_at_ms"]|int64_t(0),issued=d["issued_at_ms"]|int64_t(0);
  if(expires<=now+100||expires-now>1000)return false;
  if(commandMessage&&(issued>now+100||issued<=0||expires<issued||expires-issued>1000||now-issued>1000))return false;
  return d["id"].is<uint32_t>();
}
static void relayCapture(const JsonDocument &d){
  uint32_t id=d["id"]|0U;
  if(!relayFresh(d,false)){relayAck(id,409,"stale capture");return;}
  // One frame in flight. Release the driver frame and camera mutex before TLS I/O.
  uint8_t *copy=nullptr;size_t len=0;
  if(cameraReady&&xSemaphoreTake(cameraLock,pdMS_TO_TICKS(50))==pdTRUE){
    camera_fb_t *fb=freshCameraFrame();
    if(fb){if(fb->len<=160*1024-8){len=fb->len;copy=(uint8_t*)ps_malloc(len+8);if(copy)memcpy(copy+8,fb->buf,len);}esp_camera_fb_return(fb);}
    xSemaphoreGive(cameraLock);
  }
  if(!copy){relayAck(id,503,"camera unavailable");return;}
  if(relayFresh(d,false)){memcpy(copy,"KVF1",4);copy[4]=id>>24;copy[5]=id>>16;copy[6]=id>>8;copy[7]=id;
    int64_t sendStarted=esp_timer_get_time();uint32_t start=millis();bool sent=esp_websocket_client_send_bin(relayClient,(char*)copy,len+8,pdMS_TO_TICKS(RELAY_FRAME_SEND_MS))==int(len+8);relayMax(relayFrameMaxMs,millis()-start);Performance::sent(Performance::Relay,len,sendStarted,sent);if(!sent)relayInvalidate(RfFrameSend);
  }else relayAck(id,409,"capture expired");
  free(copy);
}
static void relayWorker(void*){
  sntp_set_time_sync_notification_cb(relayNtpSynced);
  bool ntpWasConnected=false;uint32_t lastNtpAttempt=0;
  String headers="Authorization: Bearer ";headers+=KVIDO_RELAY_TOKEN;headers+="\r\n";
  uint32_t lastTelemetry=0,lastConnect=0,lastCaptureDone=0;char boot[17];snprintf(boot,sizeof(boot),"%08lx%08lx",(unsigned long)esp_random(),(unsigned long)esp_random());
  for(;;){
    static uint32_t stackAt=0;if(millis()-stackAt>2000){stackAt=millis();Performance::relayStack=uxTaskGetStackHighWaterMark(nullptr);}
    relayWorkerAt=millis();bool wifiConnected=WiFi.status()==WL_CONNECTED;
    // Start after DHCP/DNS exist, and retry promptly when changing Wi-Fi or hotspot.
    if(wifiConnected&&(!ntpWasConnected||(!relayTimeReady()&&millis()-lastNtpAttempt>30000))){
      lastNtpAttempt=millis();relayNtpAttempts.fetch_add(1);configTime(0,0,"pool.ntp.org","time.cloudflare.com","time.google.com");
    }
    ntpWasConnected=wifiConnected;
    if(!relayClient&&WiFi.status()==WL_CONNECTED&&relayTimeReady()&&millis()-lastConnect>5000){
      lastConnect=millis();relayAttempts.fetch_add(1);esp_websocket_client_config_t cfg={};cfg.uri=KVIDO_RELAY_URL;cfg.cert_pem=KVIDO_RELAY_ROOT_CA;cfg.headers=headers.c_str();cfg.buffer_size=1024;cfg.task_stack=6144;cfg.task_prio=3;cfg.ping_interval_sec=1;cfg.pingpong_timeout_sec=RELAY_SILENCE_MS/1000;
      relayClient=esp_websocket_client_init(&cfg);if(relayClient){esp_websocket_register_events(relayClient,WEBSOCKET_EVENT_ANY,relayEvent,nullptr);relayLastStartError=esp_websocket_client_start(relayClient);if(relayLastStartError.load()!=ESP_OK)relayInvalidate(RfStart);}else relayLastStartError=ESP_ERR_NO_MEM;
    }
    RelayInput in;
    for(int budget=0;budget<4&&xQueueReceive(relayInputs,&in,0)==pdTRUE;++budget){
      if(in.generation!=relayGeneration.load()||!relayTransportConnected)continue;
      StaticJsonDocument<1024>d;if(deserializeJson(d,in.text)){relayInvalidate(RfJson);continue;}
      String type=d["type"]|"";
      if(type=="_connected"){
        relaySession="";relayHaveId=false;StaticJsonDocument<256>hello;hello["type"]="hello";hello["protocol"]=1;hello["device_id"]="kvido-e465b8788f40";hello["boot_id"]=boot;relaySend(hello);continue;
      }
      if(type=="welcome"){
        String session=d["session"]|"";int64_t server=d["server_time_ms"]|int64_t(0);
        if(session.length()<1||session.length()>96||llabs(server-relayNow())>30000){relayInvalidate(RfWelcome);continue;}
        relaySession=session;relayTrafficAt=millis();networkTrafficAt=millis();relayOnline=true;continue;
      }
      if(!relayOnline||relaySession!=String(d["session"]|"")){relayInvalidate(RfSession);continue;}
      relayTrafficAt=millis();networkTrafficAt=millis();
      if(type=="ping"){StaticJsonDocument<192>pong;pong["type"]="pong";pong["session"]=relaySession;relaySend(pong);}
      else if(type=="capture"){
        if(lastCaptureDone&&(int32_t)(in.receivedAt-lastCaptureDone)<=0)relayAck(d["id"]|0U,429,"capture busy");
        else {relayCapture(d);lastCaptureDone=millis();}
      }
      else if(type=="command"){
        uint32_t id=d["id"]|0U;
        if(!relayFresh(d,true)){relayAck(id,409,"stale command");continue;}
        if(relayHaveId&&(int32_t)(id-relayLastId)<=0){relayAck(id,409,"duplicate command");continue;}
        relayLastId=id;relayHaveId=true;
        RelayCommand c={};c.generation=relayGeneration.load();serializeJson(d,c.text,sizeof(c.text));
        String action=d["action"]|"";
        if(action=="stop"||action=="estop"){
          xQueueReset(relayCommands);if(xQueueSendToFront(relayCommands,&c,0)!=pdTRUE)relayInvalidate(RfCommandFull);
        }else if(xQueueSend(relayCommands,&c,0)!=pdTRUE){relayInvalidate(RfCommandFull);relayAck(id,503,"command queue full");}
      }
    }
    if(relayTransportConnected&&(WiFi.status()!=WL_CONNECTED||millis()-relayTrafficAt.load()>RELAY_SILENCE_MS))relayInvalidate(WiFi.status()!=WL_CONNECTED?RfWifiLost:RfSilence);
    RelayAck ack;while(xQueueReceive(relayAcks,&ack,0)==pdTRUE)if(relayOnline&&ack.generation==relayGeneration.load())relayAck(ack.id,ack.status,ack.error[0]?ack.error:nullptr);
    if(relayOnline&&millis()-lastTelemetry>=250){
      lastTelemetry=millis();int64_t sampledAt=relayNow();DynamicJsonDocument statusDoc(8192);
      if(xSemaphoreTake(stateLock,pdMS_TO_TICKS(5))==pdTRUE){fillStatus(statusDoc);xSemaphoreGive(stateLock);DynamicJsonDocument out(8704);out["type"]="telemetry";out["session"]=relaySession;out["sampled_at_ms"]=sampledAt;out["data"].set(statusDoc);relaySend(out);}
    }
    // A liveness failure tears down transport; reconnection always establishes a new session.
    if(relayClient&&relayReconnect.load()){
      esp_websocket_client_stop(relayClient);esp_websocket_client_destroy(relayClient);relayClient=nullptr;relayReconnect=false;relaySession="";xQueueReset(relayInputs);
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}
static void relaySetup(){
  if(!KVIDO_RELAY_ENABLED)return;
#if defined(KVIDO_BENCH_DIAG) && KVIDO_BENCH_DIAG
  Serial.println("BENCH before log hook");
#endif
  esp_log_set_vprintf(relayLogToRam);
#if defined(KVIDO_BENCH_DIAG) && KVIDO_BENCH_DIAG
  Serial.println("BENCH after log hook, before log level");
#endif
  esp_log_level_set("*",ESP_LOG_ERROR);
#if defined(KVIDO_BENCH_DIAG) && KVIDO_BENCH_DIAG
  Serial.println("BENCH after log level, before queues");
#endif
  relayInputs=xQueueCreate(6,sizeof(RelayInput));relayCommands=xQueueCreate(4,sizeof(RelayCommand));relayAcks=xQueueCreate(6,sizeof(RelayAck));
  if(!relayInputs||!relayCommands||!relayAcks){relayTaskError=ESP_ERR_NO_MEM;relayFailStop=true;return;}
#if defined(KVIDO_BENCH_DIAG) && KVIDO_BENCH_DIAG
  Serial.println("BENCH queues allocated, before relay task");
#endif
  if(xTaskCreatePinnedToCore(relayWorker,"kvido-relay",8192,nullptr,2,nullptr,0)!=pdPASS){relayTaskError=ESP_ERR_NO_MEM;relayFailStop=true;}
}
static void relayPoll(){
  if(!KVIDO_RELAY_ENABLED||!relayCommands)return;
  if(relayTransportConnected&&(WiFi.status()!=WL_CONNECTED||millis()-relayTrafficAt.load()>RELAY_SILENCE_MS))relayInvalidate(WiFi.status()!=WL_CONNECTED?RfWifiLost:RfSilence);
  if(relayFailStop.exchange(false)){
    relayArmed=false;
    if(controlOwner.lost(ControlOwner::Relay)){stopControl("relay_lost");light(false);}
    xQueueReset(relayCommands);
  }
  RelayCommand c;
  for(int budget=0;budget<2&&xQueueReceive(relayCommands,&c,0)==pdTRUE;++budget){
    StaticJsonDocument<1024>d;RelayAck ack={};ack.generation=c.generation;
    if(deserializeJson(d,c.text)){relayInvalidate(RfJson);break;}
    ack.id=d["id"]|0U;const char *error=nullptr;
    int64_t now=relayNow();
    if(c.generation!=relayGeneration.load()||!relayOnline||!relayTimeReady()||int64_t(d["expires_at_ms"]|int64_t(0))<=now+100){ack.status=409;error="expired command";}
    else {
      String action=d["action"]|"";
      if(action=="release"){
        if(!telemetryAt||millis()-telemetryAt>500){ack.status=409;error="UNO telemetry missing";}
        else {ack.status=applyControl(d,error,ControlOwner::Relay);relayArmed=ack.status==200;}
      }else if(action=="estop"){relayArmed=false;ack.status=applyControl(d,error,ControlOwner::Relay);}
      else if(action!="stop"&&action!="light_on"&&action!="light_off"&&action!="tilt"&&!relayArmed){ack.status=409;error="release required";}
      else ack.status=applyControl(d,error,ControlOwner::Relay);
    }
    if(error)strlcpy(ack.error,error,sizeof(ack.error));
    if(xQueueSend(relayAcks,&ack,0)!=pdTRUE){relayInvalidate(RfAckFull);break;}
  }
}
