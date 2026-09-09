// =====================================================================
// LilyGo AgroSense - Riego de precision REAL (actuadores + offline)
// ---------------------------------------------------------------------
// Hardware: ESP32 (LilyGo T-Beam/T-Display u otro) + reley + caudalimetro
//   RELAY_PIN 26   (bomba 12V via reley/modulo; ACTIVO ALTO)
//   FLOW_PIN 27    (caudalimetro YF-S201: 450 pulsos/litro, pull-up)
//   SOIL_PIN 34    (sensor capacitivo de suelo, ADC1)
// Comportamiento:
//   1. POST /api/v2/readings  (sensores, firmado HMAC) cada SEND_S
//   2. GET  /api/v2/config    (umbrales de zona -> cache NVS, cada N ciclos)
//   3. GET  /api/v2/commands/pending -> ejecuta on/off con tope local
//   4. POST /api/v2/commands/<id>/done {result, liters} al terminar
//   5. OFFLINE: si no hay red, riega con regla local (histéresis NVS) y
//      encola los riegos; al reconectar POST /api/v2/irrigation/report
// Seguridad: watchdog local - la bomba NUNCA supera max_irrigation_min+2
//   aunque el backend muera o envie una orden maliciosa.
// Libs: ArduinoJson v7, ESP32 core. Conexiones: HTTPS en prod.
// =====================================================================
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <mbedtls/md.h>
#include <time.h>

// ---------- Config ----------
const char* WIFI_SSID   = "TU_WIFI";
const char* WIFI_PASS   = "TU_CLAVE";
const char* API_BASE    = "http://TU-IP:8001";   // prod: https://tu-dominio
const char* API_KEY     = "cambiar-api-key-larga";
const char* HMAC_SECRET = "cambiar-hmac-largo";
const char* DEVICE_ID   = "lilygo-01";
const char* DEVICE_NAME = "Finca Norte 01";
const char* FW_VER      = "v3-irrigation";

const int   RELAY_PIN   = 26;
const int   FLOW_PIN    = 27;
const int   SOIL_PIN    = 34;
const bool  RELAY_ACTIVE_HIGH = true;
const float FLOW_PULSES_PER_L  = 450.0;   // YF-S201
const float SOIL_DRY_ADC       = 3200.0;  // calibrar: lectura en aire
const float SOIL_WET_ADC       = 1500.0;  // calibrar: lectura en agua
const unsigned long SEND_MS    = 60000;   // 1 muestra/min
const int   CONFIG_EVERY_N     = 10;      // refresca umbrales cada N ciclos

// ---------- Estado ----------
Preferences prefs;
struct Cfg { float soil_min = 30, soil_max = 60; int max_min = 20; long zone = 0; } cfg;
volatile unsigned long flowPulses = 0;
bool relayOn = false;
unsigned long cmdEndMs = 0; long cmdActive = -1; int cmdOffPending = -1;
unsigned long lastSend = 0; int cycle = 0; int offlineFails = 0;
struct OffEv { time_t start; float min_; float lit; };
OffEv offQueue[10]; int offCount = 0;

// ---------- Helpers HTTP ----------
String hmacHex(const String& key, const String& msg) {
  byte out[32]; mbedtls_md_context_t ctx; mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char*)key.c_str(), key.length());
  mbedtls_md_hmac_update(&ctx, (const unsigned char*)msg.c_str(), msg.length());
  mbedtls_md_hmac_finish(&ctx, out); mbedtls_md_free(&ctx);
  String s; for (int i = 0; i < 32; i++) { char b[3]; sprintf(b, "%02x", out[i]); s += b; }
  return s;
}
void addAuth(HTTPClient& h, const String& body) {
  h.addHeader("Content-Type", "application/json");
  h.addHeader("X-Api-Key", API_KEY);
  if (body.length()) {
    time_t t; time(&t);
    String ts = String((long)t);
    h.addHeader("X-Timestamp", ts);
    h.addHeader("X-Signature", hmacHex(HMAC_SECRET, ts + "." + body));
  }
}
int httpPost(const char* path, const String& body) {
  WiFiClient c; HTTPClient h;
  h.begin(c, String(API_BASE) + path); h.setTimeout(8000); addAuth(h, body);
  int code = h.POST(body); h.end(); return code;
}
int httpGet(const char* path, String& out) {
  WiFiClient c; HTTPClient h;
  h.begin(c, String(API_BASE) + path); h.setTimeout(8000);
  h.addHeader("X-Api-Key", API_KEY);
  int code = h.GET(); if (code == 200) out = h.getString(); h.end(); return code;
}

// ---------- Sensores ----------
float leerSuelo() {
  float adc = analogRead(SOIL_PIN);
  float pct = (SOIL_DRY_ADC - adc) / (SOIL_DRY_ADC - SOIL_WET_ADC) * 100.0;
  return constrain(pct, 0.0, 100.0);
}
struct SensorDef { const char* id; const char* type; const char* unit; float (*read)(); };
float leerBatt() { return analogRead(35) * 0.00586; }  // divisor en T-Beam
SensorDef sensors[] = {
  { "soil1", "soil",      "%",  leerSuelo },
  { "batt1", "battery",   "V",  leerBatt  },
};
const int N_SENSORS = sizeof(sensors) / sizeof(sensors[0]);

// ---------- Reley / caudal ----------
void relay(bool on) {
  digitalWrite(RELAY_PIN, on ? (RELAY_ACTIVE_HIGH ? HIGH : LOW) : (RELAY_ACTIVE_HIGH ? LOW : HIGH));
  relayOn = on;
}
float flowLiters() { return flowPulses / FLOW_PULSES_PER_L; }
void IRAM_ATTR onFlow() { flowPulses++; }

// ---------- NVS ----------
void cfgLoad() { prefs.begin("agro", true);
  cfg.soil_min = prefs.getFloat("smin", 30); cfg.soil_max = prefs.getFloat("smax", 60);
  cfg.max_min = prefs.getInt("maxm", 20); cfg.zone = prefs.getLong("zone", 0); prefs.end(); }
void cfgSave() { prefs.begin("agro", false);
  prefs.putFloat("smin", cfg.soil_min); prefs.putFloat("smax", cfg.soil_max);
  prefs.putInt("maxm", cfg.max_min); prefs.putLong("zone", cfg.zone); prefs.end(); }

// ---------- Modo offline: regla local + cola de reportes ----------
void offQueuePush(time_t start, float min_, float lit) {
  if (offCount < 10) { offQueue[offCount++] = {start, min_, lit}; }
  else Serial.println("[OFF] cola llena (10 max), riego no auditado");
}
void offIrrigateIfNeeded(float soil) {
  if (relayOn || cfg.zone == 0) return;
  if (soil < cfg.soil_min) {
    float need = cfg.soil_max - soil;
    int dur = constrain((int)(need / 2.0) + 1, 2, cfg.max_min);  // ~2%/min calibrado
    Serial.printf("[OFF] riego local %d min (soil %.1f%% < %.1f%%)\n", dur, soil, cfg.soil_min);
    relay(true); cmdActive = -2;  // -2 = orden local offline
    cmdEndMs = millis() + (unsigned long)dur * 60000UL;
    time_t t; time(&t); offQueuePush(t, dur, 0);
  }
}
void offReport() {
  if (offCount == 0) return;
  JsonDocument doc; doc["device_id"] = DEVICE_ID;
  JsonArray evs = doc["events"].to<JsonArray>();
  for (int i = 0; i < offCount; i++) {
    JsonObject o = evs.add<JsonObject>();
    o["started_at"] = (long)offQueue[i].start;
    o["duration_min"] = offQueue[i].min_; o["liters"] = offQueue[i].lit;
  }
  String b; serializeJson(doc, b);
  if (httpPost("/api/v2/irrigation/report", b) == 201) { offCount = 0; Serial.println("[OFF] reporte enviado"); }
}

// ---------- Comandos del backend ----------
void execCommand(JsonObject c) {
  long id = c["cmd_id"]; String act = c["action"] | ""; float dur = c["payload"]["duration_min"] | 0;
  if (act == "off") { relay(false); cmdActive = -1; cmdEndMs = 0; return; }
  if (act == "on") {
    if (cmdActive != -1) return;                      // ya hay riego activo
    float tope = min((float)cfg.max_min + 2, max(dur, 0.5f));  // WATCHDOG local
    cmdActive = id; flowPulses = 0;
    relay(true); cmdEndMs = millis() + (unsigned long)(tope * 60000UL);
    Serial.printf("[CMD] riego %s min (tope local %.1f)\n", String(dur).c_str(), tope);
  }
}
void cmdFinishOk() {
  relay(false); cmdEndMs = 0;
  if (cmdActive >= 0) {
    JsonDocument d; d["result"] = "ok"; d["liters"] = flowLiters();
    String b; serializeJson(d, b);
    int code = httpPost(("/api/v2/commands/" + String(cmdActive) + "/done").c_str(), b);
    Serial.printf("[CMD] %ld done (%d, %.2f L)\n", cmdActive, code, flowLiters());
  } else if (cmdActive == -2) {
    if (offCount > 0) offQueue[offCount - 1].lit = flowLiters();  // litros del riego local
  }
  cmdActive = -1;
}

// ---------- Ciclo principal ----------
void sendReadings() {
  JsonDocument doc; doc["device_id"] = DEVICE_ID; doc["name"] = DEVICE_NAME; doc["fw"] = FW_VER;
  JsonArray arr = doc["sensors"].to<JsonArray>();
  for (int i = 0; i < N_SENSORS; i++) {
    JsonObject o = arr.add<JsonObject>();
    o["sensor_id"] = sensors[i].id; o["type"] = sensors[i].type;
    o["unit"] = sensors[i].unit; o["value"] = sensors[i].read();
  }
  String b; serializeJson(doc, b);
  int code = httpPost("/api/v2/readings", b);
  if (code == 201) offlineFails = 0; else offlineFails++;
}

void fetchConfig() {
  String out;
  if (httpGet(("/api/v2/config?device_id=" + String(DEVICE_ID)).c_str(), out) == 200) {
    JsonDocument d; if (deserializeJson(d, out) == DeserializationError::Ok && d["zone_id"]) {
      cfg.zone = d["zone_id"]; cfg.soil_min = d["soil_min_pct"] | cfg.soil_min;
      cfg.soil_max = d["soil_max_pct"] | cfg.soil_max; cfg.max_min = d["max_irrigation_min"] | cfg.max_min;
      cfgSave(); Serial.printf("[CFG] zona=%ld min=%.0f max=%.0f tope=%d\n", cfg.zone, cfg.soil_min, cfg.soil_max, cfg.max_min);
    }
  }
}

void pollCommands() {
  String out;
  if (httpGet(("/api/v2/commands/pending?device_id=" + String(DEVICE_ID)).c_str(), out) != 200) return;
  JsonDocument d; if (deserializeJson(d, out) != DeserializationError::Ok) return;
  for (JsonObject c : d.as<JsonArray>()) execCommand(c);
}

void setup() {
  Serial.begin(115200); delay(400);
  pinMode(RELAY_PIN, OUTPUT); relay(false);
  pinMode(FLOW_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(FLOW_PIN), onFlow, FALLING);
  analogReadResolution(12);
  cfgLoad();
  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID, WIFI_PASS);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
  Serial.printf("\n[IOT] WiFi %s\n", WiFi.status() == WL_CONNECTED ? "OK" : "FALLO (modo offline)");
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  time_t n = 0; for (int i = 0; i < 20 && n < 1700000000; i++) { delay(500); time(&n); }
}

void loop() {
  unsigned long now = millis();
  bool online = WiFi.status() == WL_CONNECTED;

  // 1) watchdog del reley (prioridad maxima, corre siempre)
  if (relayOn && (long)(now - cmdEndMs) >= 0) cmdFinishOk();

  // 2) ciclo de datos
  if (now - lastSend >= SEND_MS) {
    lastSend = now; cycle++;
    if (online) {
      sendReadings();
      if (offlineFails == 0) {                    // backend sano
        if (cycle % CONFIG_EVERY_N == 1) fetchConfig();
        offReport();                              // riegos offline pendientes
        pollCommands();
      }
    }
    float soil = leerSuelo();
    if (offlineFails >= 2 || !online) offIrrigateIfNeeded(soil);  // regla local
  }

  // 3) reconexion
  if (!online) { WiFi.reconnect(); delay(5000); }
  delay(500);
}
