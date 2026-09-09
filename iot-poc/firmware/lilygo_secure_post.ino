// LilyGo (ESP32) sketch conceptual: sensores Moisture + HyT (SHT3x/DHT) + radiacion solar.
// Envia JSON por HTTPS con X-Api-Key + X-Timestamp + X-Signature (HMAC-SHA256 de "timestamp.body").
// Requiere libs: WiFiClientSecure, HTTPClient, ArduinoJson, mbedTLS (incluida en ESP32 core).
// Sustituir lecturas analogRead()/dht por vuestros pines/modelo reales.
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include "mbedtls/md.h" // mbedtls HMAC-SHA256

const char* WIFI_SSID = "TU_WIFI";
const char* WIFI_PASS = "TU_CLAVE";
const char* API_URL = "https://TU-HOST/api/v1/readings"; // <-- SIEMPRE HTTPS en prod
const char* API_KEY = "cambiar-api-key-larga";
const char* HMAC_SECRET = "cambiar-hmac-largo";

String hmacHex(const String& key, const String& msg) {
  byte out[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_hmac_starts(&ctx, (const unsigned char*)key.c_str(), key.length());
  mbedtls_hmac_update(&ctx, (const unsigned char*)msg.c_str(), msg.length());
  mbedtls_hmac_finish(&ctx, out);
  mbedtls_md_free(&ctx);
  String s;
  for (int i = 0; i < 32; i++) { char b[3]; sprintf(b, "%02x", out[i]); s += b; }
  return s;
}

void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) delay(500);
}

void loop() {
  // TODO: reemplazar por lecturas reales
  float tempC = 25.0, hum = 55.0, soil = 42.0, solar = 480.0, batt = 4.0;

  StaticJsonDocument<256> doc;
  doc["device_id"] = "lilygo-01";
  doc["temperature_c"] = tempC;
  doc["humidity_pct"] = hum;
  doc["soil_moisture_pct"] = soil;
  doc["solar_w_m2"] = solar;
  doc["battery_v"] = batt;
  String body;
  serializeJson(doc, body);

  String ts = String((long)time(nullptr));
  String sig = hmacHex(HMAC_SECRET, ts + "." + body);

  WiFiClientSecure tls;
  tls.setInsecure(); // POC: en prod usar tls.setCACert() con cert real
  HTTPClient http;
  http.begin(tls, API_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Api-Key", API_KEY);
  http.addHeader("X-Timestamp", ts);
  http.addHeader("X-Signature", sig);
  int code = http.POST(body);
  Serial.printf("POST %d %s\n", code, http.getString().c_str());
  http.end();
  delay(60000); // 1 muestra/min
}
