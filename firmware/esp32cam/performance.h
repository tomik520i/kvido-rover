#pragma once
#include "esp_freertos_hooks.h"
#include "esp_timer.h"
#include "tls_memory.h"

// Stock Arduino SDK has no task runtime counters. Sample the task interrupted
// by each core's tick: this is an estimate, not exact CPU/ISR accounting.
namespace Performance {
static std::atomic<uint32_t> ticks[2], idle[2];
static TaskHandle_t idleTask[2];
static bool hooks[2]={false,false};
static portMUX_TYPE mux=portMUX_INITIALIZER_UNLOCKED;
struct Sample {uint32_t at=0,window=0,total[2]={0,0},idle[2]={0,0},lag=0;};
static Sample sample;
static std::atomic<uint32_t> cameraCalls{0},cameraErrors{0},cameraUs{0},cameraMaxUs{0},cameraLastUs{0},jpegBytes{0};
static std::atomic<uint32_t> lanBytes{0},streamBytes{0},relayBytes{0},sendErrors{0},sendMaxUs{0};
static std::atomic<uint32_t> loopLockMisses{0},loopLastUs{0},loopStack{0},relayStack{0},networkStack{0};
static std::atomic<uint32_t> allocFailures{0},allocFailedBytes{0},allocFailedCaps{0},allocFailedAt{0};
static void allocationFailed(size_t size,uint32_t caps,const char*){allocFailures.fetch_add(1);allocFailedBytes=size;allocFailedCaps=caps;allocFailedAt=uint32_t(esp_timer_get_time()/1000);}
static void maximum(std::atomic<uint32_t> &value,uint32_t n){uint32_t old=value.load();while(n>old&&!value.compare_exchange_weak(old,n)){};}
static void IRAM_ATTR tick0(){ticks[0].fetch_add(1,std::memory_order_relaxed);if(xTaskGetCurrentTaskHandleForCPU(0)==idleTask[0])idle[0].fetch_add(1,std::memory_order_relaxed);}
static void IRAM_ATTR tick1(){ticks[1].fetch_add(1,std::memory_order_relaxed);if(xTaskGetCurrentTaskHandleForCPU(1)==idleTask[1])idle[1].fetch_add(1,std::memory_order_relaxed);}
static void worker(void*){
  uint32_t last=millis(),priorTicks[2]={ticks[0].load(),ticks[1].load()},priorIdle[2]={idle[0].load(),idle[1].load()};
  for(;;){
    vTaskDelay(pdMS_TO_TICKS(2000));uint32_t now=millis();Sample next;next.at=now;next.window=now-last;next.lag=next.window>2000?next.window-2000:0;last=now;
    for(int i=0;i<2;++i){uint32_t t=ticks[i].load(),v=idle[i].load();next.total[i]=t-priorTicks[i];next.idle[i]=v-priorIdle[i];priorTicks[i]=t;priorIdle[i]=v;}
    portENTER_CRITICAL(&mux);sample=next;portEXIT_CRITICAL(&mux);
  }
}
static void begin(){
  heap_caps_register_failed_alloc_callback(allocationFailed);
  idleTask[0]=xTaskGetIdleTaskHandleForCPU(0);idleTask[1]=xTaskGetIdleTaskHandleForCPU(1);
  hooks[0]=esp_register_freertos_tick_hook_for_cpu(tick0,0)==ESP_OK;
  hooks[1]=esp_register_freertos_tick_hook_for_cpu(tick1,1)==ESP_OK;
  xTaskCreatePinnedToCore(worker,"kvido-perf",3072,nullptr,1,nullptr,1);
}
static camera_fb_t *frame(){
  int64_t start=esp_timer_get_time();camera_fb_t *fb=esp_camera_fb_get();uint32_t us=uint32_t(esp_timer_get_time()-start);
  cameraCalls.fetch_add(1);cameraUs.fetch_add(us);cameraLastUs=us;maximum(cameraMaxUs,us);
  if(fb)jpegBytes.fetch_add(fb->len);else cameraErrors.fetch_add(1);return fb;
}
enum Transport {Lan,Stream,Relay};
static void sent(Transport route,size_t bytes,int64_t started,bool ok){
  maximum(sendMaxUs,uint32_t(esp_timer_get_time()-started));
  if(!ok){sendErrors.fetch_add(1);return;}
  (route==Lan?lanBytes:route==Stream?streamBytes:relayBytes).fetch_add(bytes);
}
static void append(JsonDocument &d){
  Sample s;portENTER_CRITICAL(&mux);s=sample;portEXIT_CRITICAL(&mux);
  JsonObject p=d.createNestedObject("performance");p["cpu_method"]="tick_idle_sampling_estimate";
  auto cpu=p.createNestedArray("cpu_busy_pct");
  for(int i=0;i<2;++i){if(hooks[i]&&s.total[i]&&s.idle[i]<=s.total[i])cpu.add(100.0f*(s.total[i]-s.idle[i])/s.total[i]);else cpu.add(nullptr);}
  p["window_ms"]=s.window;p["sample_age_ms"]=s.at?millis()-s.at:UINT32_MAX;p["sampler_lag_ms"]=s.lag;
  p["cpu_mhz"]=ESP.getCpuFreqMHz();p["free_internal"]=heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
  p["free_psram"]=ESP.getFreePsram();p["min_free_psram"]=ESP.getMinFreePsram();p["tasks"]=uxTaskGetNumberOfTasks();
  p["camera_calls"]=cameraCalls.load();p["camera_errors"]=cameraErrors.load();p["camera_total_us"]=cameraUs.load();
  p["camera_last_us"]=cameraLastUs.load();p["camera_max_us"]=cameraMaxUs.load();p["jpeg_bytes"]=jpegBytes.load();
  p["lan_tx_bytes"]=lanBytes.load();p["stream_tx_bytes"]=streamBytes.load();p["relay_jpeg_tx_bytes"]=relayBytes.load();
  p["send_errors"]=sendErrors.load();p["send_max_us"]=sendMaxUs.load();p["loop_lock_misses"]=loopLockMisses.load();p["loop_last_us"]=loopLastUs.load();
  p["loop_stack_min_bytes"]=loopStack.load();p["relay_stack_min_bytes"]=relayStack.load();p["network_stack_min_bytes"]=networkStack.load();
  p["alloc_failures"]=allocFailures.load();p["alloc_failed_bytes"]=allocFailedBytes.load();p["alloc_failed_caps"]=allocFailedCaps.load();p["alloc_failed_at_ms"]=allocFailedAt.load();
  p["tls_psram"]=TlsMemory::configured&&TlsMemory::external;p["tls_alloc_calls"]=TlsMemory::calls.load();p["tls_alloc_total_bytes"]=TlsMemory::bytes.load();p["tls_alloc_failures"]=TlsMemory::failures.load();
}
}
