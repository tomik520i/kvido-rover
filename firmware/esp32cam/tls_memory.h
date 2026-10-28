#pragma once
#include "mbedtls/platform.h"

// The bundled IDF allocates mbedTLS exclusively in internal RAM by default.
// Register its supported allocator before creating any TLS clients. External
// buffers leave internal/DMA RAM available to Wi-Fi and the camera driver.
namespace TlsMemory {
static bool external=false,configured=false;
static std::atomic<uint32_t> calls{0},bytes{0},failures{0};
static void *allocate(size_t count,size_t size){
  calls.fetch_add(1);
  if(size&&count>SIZE_MAX/size){failures.fetch_add(1);return nullptr;}
  void *p=heap_caps_calloc(count,size,(external?MALLOC_CAP_SPIRAM:MALLOC_CAP_INTERNAL)|MALLOC_CAP_8BIT);
  if(p)bytes.fetch_add(count*size);else failures.fetch_add(1);
  return p;
}
static void release(void *p){heap_caps_free(p);}
static void begin(){external=psramFound();configured=mbedtls_platform_set_calloc_free(allocate,release)==0;}
}
