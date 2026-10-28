#pragma once
#include "ping/ping_sock.h"
#include "network_health.h"

static std::atomic<uint32_t> networkGatewayAt{0},networkPingOk{0},networkPingFail{0},networkPingConsecutive{0};
static std::atomic<uint32_t> networkRecoveryCount{0},networkRecoveryAt{0},networkRecoveryStage{0};
static std::atomic<int> networkProbeError{0};
static std::atomic<bool> networkProbeActive{false};
static NetworkHealth::Policy networkPolicy;
// Preserved through a software restart; never contains credentials.
RTC_NOINIT_ATTR static uint32_t recoveryRtcMagic,recoveryRtcRestarts;
static void networkPingSuccess(esp_ping_handle_t,void*){networkGatewayAt=millis();networkPingOk.fetch_add(1);networkPingConsecutive=0;}
static void networkPingTimeout(esp_ping_handle_t,void*){networkPingFail.fetch_add(1);networkPingConsecutive.fetch_add(1);}
static void networkPingEnd(esp_ping_handle_t,void*){networkProbeActive=false;}
static void appendNetworkHealth(JsonDocument &d){
  JsonObject n=d.createNestedObject("network_health");
  n["traffic_age_ms"]=millis()-networkTrafficAt.load();n["gateway_age_ms"]=millis()-networkGatewayAt.load();
  n["ping_ok"]=networkPingOk.load();n["ping_failed"]=networkPingFail.load();n["consecutive_failures"]=networkPingConsecutive.load();
  n["probe_error"]=networkProbeError.load();n["recovering"]=wifiRecovering.load();
  n["recoveries"]=networkRecoveryCount.load();n["recovery_at_ms"]=networkRecoveryAt.load();n["recovery_stage"]=networkRecoveryStage.load();
  n["software_restarts"]=recoveryRtcRestarts;
}
static void networkWorker(void*){
  bool watched=esp_task_wdt_add(nullptr)==ESP_OK;
  esp_ping_handle_t probe=nullptr;uint32_t target=0,associatedAt=millis(),lastProbe=0;bool associated=false;
  for(;;){
    static uint32_t stackAt=0;if(millis()-stackAt>2000){stackAt=millis();Performance::networkStack=uxTaskGetStackHighWaterMark(nullptr);}
    if(setupOnlyRequest.exchange(false)){
      wifiRecovering=true;WiFi.setAutoReconnect(false);WiFi.disconnect(false,false);
      WiFi.mode(WIFI_AP);WiFi.softAP("Kvido-Setup",WIFI_AP_PASSWORD);wifiRecovering=false;
    }
    bool connected=WiFi.status()==WL_CONNECTED;
    if(connected&&!associated)associatedAt=millis();associated=connected;
    uint32_t gateway=connected?uint32_t(WiFi.gatewayIP()):0;
    // A finished session is reused, avoiding a new task/socket allocation each poll.
    if(probe&&gateway!=target&&!networkProbeActive){esp_ping_delete_session(probe);probe=nullptr;target=0;}
    if(connected&&gateway&&!probe){
      esp_ping_config_t cfg=ESP_PING_DEFAULT_CONFIG();cfg.count=1;cfg.timeout_ms=1000;cfg.data_size=16;
      cfg.target_addr.type=IPADDR_TYPE_V4;cfg.target_addr.u_addr.ip4.addr=gateway;
      esp_ping_callbacks_t cb={};cb.on_ping_success=networkPingSuccess;cb.on_ping_timeout=networkPingTimeout;cb.on_ping_end=networkPingEnd;
      networkProbeError=esp_ping_new_session(&cfg,&cb,&probe);if(probe)target=gateway;
    }
    if(probe&&!networkProbeActive&&millis()-lastProbe>=3000){
      lastProbe=millis();networkProbeActive=true;esp_err_t err=esp_ping_start(probe);networkProbeError=err;
      if(err!=ESP_OK){networkProbeActive=false;networkPingTimeout(nullptr,nullptr);}
    }
    uint32_t now=millis(),traffic=networkTrafficAt.load(),gatewayAt=networkGatewayAt.load();
    networkPolicy.healthy(now,uint32_t(now-traffic)<uint32_t(now-gatewayAt)?traffic:gatewayAt);
    // Recovery waits and radio stop/start run outside the UART loop and state lock.
    // Escalate even if the first reconnect leaves STA disconnected. Requiring
    // WL_CONNECTED here would strand the very recovery we are trying to finish.
    if(!setupOnly&&(connected||networkPolicy.attempts>0)&&networkPolicy.due(now,associatedAt,traffic,gatewayAt,networkPingConsecutive.load(),otaInProgress.load())){
      bool recover=false;
      if(xSemaphoreTake(stateLock,pdMS_TO_TICKS(100))==pdTRUE){
        if(!joining&&!otaInProgress){wifiRecovering=true;stopControl("network_stalled");light(false);relayArmed=false;requestRelayReconnect();recover=true;}
        xSemaphoreGive(stateLock);
      }
      if(recover){
        uint8_t stage=networkPolicy.recover(now);networkRecoveryStage=stage;networkRecoveryAt=now;networkRecoveryCount.fetch_add(1);
        if(stage>=3){++recoveryRtcRestarts;vTaskDelay(pdMS_TO_TICKS(100));ESP.restart();}
        WiFi.disconnect(false,false);
        vTaskDelay(pdMS_TO_TICKS(250));
        if(stage==2){
          WiFi.mode(WIFI_OFF);vTaskDelay(pdMS_TO_TICKS(250));WiFi.mode(WIFI_AP_STA);
          WiFi.setSleep(false);WiFi.setAutoReconnect(true);
          WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
          esp_wifi_set_protocol(WIFI_IF_STA,kvidoWifiProtocols);
          esp_wifi_set_protocol(WIFI_IF_AP,kvidoWifiProtocols);
          WiFi.softAP("Kvido-Setup",WIFI_AP_PASSWORD);
          WiFi.begin(prefs.getString("ssid","").c_str(),prefs.getString("pass","").c_str());
        }else WiFi.reconnect();
        wifiRecovering=false;associated=false;
      }
    }
    if(watched)esp_task_wdt_reset();vTaskDelay(pdMS_TO_TICKS(250));
  }
}
static void networkRecoverySetup(){
  if(esp_reset_reason()==ESP_RST_POWERON||recoveryRtcMagic!=0x4b564e31){recoveryRtcMagic=0x4b564e31;recoveryRtcRestarts=0;}
  if(xTaskCreatePinnedToCore(networkWorker,"kvido-network",4096,nullptr,1,nullptr,0)!=pdPASS)networkProbeError=ESP_ERR_NO_MEM;
}
