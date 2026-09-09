// LilyGo ESP32: Moisture + HyT + radiacion solar -> POST JSON firmado al backend.
// Seguridad: X-Api-Key + X-Timestamp + X-Signature (HMAC-SHA256 de "timestamp.body").
// Version PRUEBA en HTTP. En prod cambiar API_URL a https:// y usar WiFiClientSecure.
// Libs: ArduinoJson (v7), ESP32 core (WiFi, HTTPClient, mbedtls, NTP).
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <mbedtls/md.h>
#include <time.h>

const char* WIFI_SSID = "TU_WIFI";
const char* WIFI_PASS = "TU_CLAVE";
const char* API_URL = "http://TU-IP:8000/api/v1/readings"; // HTTP solo para prueba
const char* API_KEY = "cambiar-api-key-larga";
const char* HMAC_SECRET = "cambiar-hmac-largo";

String hmacHex(const String& key, const String& msg) {
  byte out[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char*)key.c_str(), key.length());
  mbedtls_md_hmac_update(&ctx, (const unsigned char*)msg.c_str(), msg.length());
  mbedtls_md_hmac_finish(&ctx, out);
  mbedtls_md_free(&ctx);
  String s;
  for (int i = 0; i < 32; i++) { char b[3]; sprintf(b, "%02x", out[i]); s += b; }
  return s;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n[IOT] conectando WiFi...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 40) { delay(500); Serial.print("."); tries++; }
  if (WiFi.status() != WL_CONNECTED) { Serial.println("\n[IOT] WiFi FALLO, reintentando en loop"); return; }
  Serial.printf("\n[IOT] WiFi OK ip=%s\n", WiFi.localIP().toString().c_str());
  configTime(0, 0, "pool.ntp.org", "time.nist.gov"); // NTP para timestamp anti-replay
  time_t now = 0;
  for (int i = 0; i < 20 && now < 1700000000; i++) { delay(500); time(&now); }
  Serial.printf("[IOT] epoch=%ld\n", (long)now);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) { Serial.println("[IOT] sin WiFi, reconectando..."); WiFi.reconnect(); delay(5000); return; }

  // TODO: reemplazar por lecturas reales (DHT/SHT, analog moisture, ADC solar)
  float tempC = 25.0, hum = 55.0, soil = 42.0, solar = 480.0, batt = 4.0;

  JsonDocument doc;
  doc["device_id"] = "lilygo-01";
  doc["temperature_c"] = tempC;
  doc["humidity_pct"] = hum;
  doc["soil_moisture_pct"] = soil;
  doc["solar_w_m2"] = solar;
  doc["battery_v"] = batt;
  String body;
  serializeJson(doc, body);

  time_t now;
  time(&now);
  String ts = String((long)now);
  String sig = hmacHex(HMAC_SECRET, ts + "." + body);

  WiFiClient plain;
  HTTPClient http;
  http.begin(plain, API_URL);
  http.setTimeout(10000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Api-Key", API_KEY);
  http.addHeader("X-Timestamp", ts);
  http.addHeader("X-Signature", sig);
  int code = http.POST(body);
  Serial.printf("POST %d %s\n", code, http.getString().c_str());
  http.end();
  delay(60000); // 1 muestra/min
}
