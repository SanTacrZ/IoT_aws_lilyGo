// LilyGo ESP32 GENERICO: declara tus sensores en la tabla `sensors[]` y todo lo demas es automatico.
// POST a /api/v2/readings con {device_id, name, fw, sensors:[{sensor_id,type,unit,value}]}
// Quitar un sensor fisico = comentar su linea. Agregarlo = añadir una linea. Sin migraciones.
// Libs: ArduinoJson v7, ESP32 core (WiFi, HTTPClient, mbedtls, NTP).
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <mbedtls/md.h>
#include <time.h>

const char* WIFI_SSID = "TU_WIFI";
const char* WIFI_PASS = "TU_CLAVE";
const char* API_URL = "http://TU-IP:8000/api/v2/readings";
const char* API_KEY = "cambiar-api-key-larga";
const char* HMAC_SECRET = "cambiar-hmac-largo";
const char* DEVICE_ID = "lilygo-01";
const char* DEVICE_NAME = "Finca Norte 01";
const char* FW_VER = "v2.0";

// ---- Delega la lectura de cada sensor a una funcion float f() ----
float leerTemp()  { return 25.0; }   // TODO: DHT/SHT real
float leerHum()   { return 55.0; }   // TODO: DHT/SHT real
float leerSuelo() { return 42.0; }   // TODO: analogRead + calibracion
float leerSolar() { return 480.0; }  // TODO: ADC panel
float leerBatt()  { return 4.0; }    // TODO: ADC bateria

struct SensorDef { const char* sensor_id; const char* type; const char* unit; float (*read)(); };

// >>> PARA AGREGAR UN SENSOR: añade una linea aqui. PARA QUITAR: comentala. <<<
SensorDef sensors[] = {
  { "temp1",  "temperature", "°C",   leerTemp  },
  { "hum1",   "humidity",    "%",    leerHum   },
  { "soil1",  "soil",        "%",    leerSuelo },
  { "solar1", "solar",       "W/m2", leerSolar },
  { "batt1",  "battery",     "V",    leerBatt  },
  // { "soil2", "soil", "%", leerSuelo },   // ejemplo: segundo sensor de suelo
};
const int N_SENSORS = sizeof(sensors) / sizeof(sensors[0]);

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
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 40) { delay(500); Serial.print("."); tries++; }
  Serial.printf("\n[IOT] WiFi %s ip=%s\n", WiFi.status() == WL_CONNECTED ? "OK" : "FALLO",
                WiFi.localIP().toString().c_str());
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  time_t now = 0;
  for (int i = 0; i < 20 && now < 1700000000; i++) { delay(500); time(&now); }
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) { WiFi.reconnect(); delay(5000); return; }

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["name"] = DEVICE_NAME;
  doc["fw"] = FW_VER;
  JsonArray arr = doc["sensors"].to<JsonArray>();
  for (int i = 0; i < N_SENSORS; i++) {
    JsonObject o = arr.add<JsonObject>();
    o["sensor_id"] = sensors[i].sensor_id;
    o["type"] = sensors[i].type;
    o["unit"] = sensors[i].unit;
    o["value"] = sensors[i].read();
  }
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
  delay(60000);
}
