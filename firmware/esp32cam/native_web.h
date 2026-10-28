#pragma once
#include <WebServer.h>
#include <base64.h>
#include <vector>
#include <functional>
#include "esp_http_server.h"

// Use the vendor firmware's HTTP implementation for LAN requests. A separate
// stream server keeps MJPEG from monopolizing the control server's task.
class NativeWebServer {
  struct Route {String path; HTTPMethod method; std::function<void()> handler; bool locked;};
  struct Header {String name,value;};
  struct RawRoute {String path; HTTPMethod method; esp_err_t (*handler)(httpd_req_t*);};
  std::vector<Route> routes;
  std::vector<RawRoute> rawRoutes;
  std::vector<Header> responseHeaders;
  httpd_handle_t server=nullptr;
  httpd_req_t *request=nullptr;
  uint16_t port;
  uint16_t controlPort;
  SemaphoreHandle_t stateLock;
  String body;
  const char *responseStatus="500 Internal Server Error",*responseType="text/plain";
  uint8_t *responseData=nullptr;
  size_t responseLength=0;
  const char *responseStatic="Missing response";
  void resetResponse(){free(responseData);responseData=nullptr;responseLength=0;responseStatic="Missing response";responseStatus="500 Internal Server Error";responseType="text/plain";responseHeaders.clear();}
  void flushResponse(httpd_req_t *req){
    httpd_resp_set_status(req,responseStatus);httpd_resp_set_type(req,responseType);
    httpd_resp_set_hdr(req,"Connection","close");
    for(auto &h:responseHeaders)httpd_resp_set_hdr(req,h.name.c_str(),h.value.c_str());
    // Never wait for a slow receiver while holding application/camera state.
    size_t length=responseData?responseLength:strlen(responseStatic);int64_t started=esp_timer_get_time();
    esp_err_t result=httpd_resp_send(req,responseData?(const char*)responseData:responseStatic,length);
    Performance::sent(Performance::Lan,length,started,result==ESP_OK);
  }
  static esp_err_t dispatch(httpd_req_t *req){
    auto &w=*static_cast<NativeWebServer*>(req->user_ctx);
    String payload;
    if(req->content_len>512){httpd_resp_set_status(req,"413 Payload Too Large");httpd_resp_send(req,"Body too large",HTTPD_RESP_USE_STRLEN);return ESP_FAIL;}
    while(payload.length()<req->content_len){
      char buf[128];int n=httpd_req_recv(req,buf,min(sizeof(buf),req->content_len-payload.length()));
      if(n<=0)return ESP_FAIL;
      payload.concat(buf,n);
    }
    w.request=req;w.body=payload;w.resetResponse();
    String path=req->uri;int query=path.indexOf('?');if(query>=0)path=path.substring(0,query);
    bool found=false;
    for(auto &r:w.routes)if(r.path==path&&r.method==w.method()){
      found=true;
      bool locked=r.locked&&xSemaphoreTake(w.stateLock,pdMS_TO_TICKS(100))==pdTRUE;
      if(r.locked&&!locked)w.send(503,"text/plain","Busy");
      else r.handler();
      if(locked)xSemaphoreGive(w.stateLock);
      break;
    }
    if(!found)w.send(404,"text/plain","Not found");
    w.flushResponse(req);
    w.request=nullptr;w.body="";w.responseHeaders.clear();
    w.resetResponse();
    // Every response is explicitly closed; idle browser preconnections are
    // bounded separately by the IDF receive timeout and LRU eviction.
    httpd_sess_trigger_close(req->handle,httpd_req_to_sockfd(req));
    return ESP_OK;
  }
public:
  NativeWebServer(uint16_t p,SemaphoreHandle_t lock,uint16_t cp=32769):port(p),controlPort(cp),stateLock(lock){}
  void collectHeaders(const char**,size_t){}
  void enableDelay(bool){}
  void on(const char *path,HTTPMethod method,std::function<void()> handler){routes.push_back({path,method,handler,true});}
  void onUnlocked(const char *path,HTTPMethod method,std::function<void()> handler){routes.push_back({path,method,handler,false});}
  // Streaming handlers (e.g. firmware upload) bypass the 512-byte body and state lock.
  void onRaw(const char *path,HTTPMethod method,esp_err_t (*handler)(httpd_req_t*)){rawRoutes.push_back({path,method,handler});}
  bool begin(){
    httpd_config_t conf=HTTPD_DEFAULT_CONFIG();
    conf.server_port=port;conf.ctrl_port=controlPort;conf.max_open_sockets=3;
    conf.max_uri_handlers=routes.size()+rawRoutes.size();conf.recv_wait_timeout=2;conf.send_wait_timeout=2;
    conf.lru_purge_enable=true;conf.stack_size=8192;
    if(httpd_start(&server,&conf)!=ESP_OK)return false;
    for(auto &r:routes){httpd_uri_t uri={};uri.uri=r.path.c_str();uri.method=r.method;uri.handler=dispatch;uri.user_ctx=this;
      if(httpd_register_uri_handler(server,&uri)!=ESP_OK)return false;}
    for(auto &r:rawRoutes){httpd_uri_t uri={};uri.uri=r.path.c_str();uri.method=r.method;uri.handler=r.handler;uri.user_ctx=this;
      if(httpd_register_uri_handler(server,&uri)!=ESP_OK)return false;}
    return true;
  }
  HTTPMethod method(){return request->method==::HTTP_POST?HTTP_POST:HTTP_GET;}
  String header(const char *name){
    size_t length=httpd_req_get_hdr_value_len(request,name);
    if(!length||length>512)return "";
    std::vector<char> value(length+1);
    if(httpd_req_get_hdr_value_str(request,name,value.data(),value.size())!=ESP_OK)return "";
    return String(value.data());
  }
  bool hasHeader(const char *name){return httpd_req_get_hdr_value_len(request,name)>0;}
  String arg(const char*){return body;}
  bool authenticate(const char *user,const char *password){return header("Authorization")=="Basic "+base64::encode(String(user)+":"+password);}
  void requestAuthentication(){sendHeader("WWW-Authenticate","Basic realm=\"Kvido\"");send(401,"text/plain","Authentication required");}
  void sendHeader(const String &name,const String &value){responseHeaders.push_back({name,value});}
  void sendBytes(int code,const char *type,const char *data,size_t length){
    const char *status="500 Internal Server Error";
    switch(code){case 200:status="200 OK";break;case 202:status="202 Accepted";break;case 400:status="400 Bad Request";break;case 401:status="401 Unauthorized";break;case 403:status="403 Forbidden";break;case 404:status="404 Not Found";break;case 409:status="409 Conflict";break;case 422:status="422 Unprocessable Entity";break;case 503:status="503 Service Unavailable";break;}
    free(responseData);responseData=nullptr;responseStatus=status;responseType=type;
    // Large camera responses belong in PSRAM, not scarce TLS/internal heap.
    responseData=(uint8_t*)(length>4096&&psramFound()?ps_malloc(length):malloc(length?length:1));
    if(!responseData){responseStatus="503 Service Unavailable";responseType="text/plain";responseStatic="Response buffer unavailable";responseLength=0;return;}
    if(length)memcpy(responseData,data,length);responseLength=length;
  }
  void send(int code,const char *type,const String &data){sendBytes(code,type,data.c_str(),data.length());}
  void send_P(int code,const char *type,const char *data){sendBytes(code,type,data,strlen(data));}
};
