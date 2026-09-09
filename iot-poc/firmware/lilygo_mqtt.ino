// =====================================================================
// LilyGo AgroSense - MQTT (fase 5): mismo esquema JSON que HTTP v2.
// Topic: agrosense/<device_id>/data  (QoS 1)
// Dev:   broker local Mosquitto (1883, sin TLS)
// AWS:   host = <xxx>.iot.<region>.amazonaws.com:8883, TLS con certificado
//        X.509 (IoT Core) -> WiFiClientSecure + attachClientCert. Comandos
//        via topic agrosense/<device_id>/cmd (IoT Rule -> MQTT).
// Libs: ArduinoJson v7 + PubSubClient.
// =====================================================================
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

const char* WIFI_SSID = "TU_WIFI";
const char* WIFI_PASS = "TU_CLAVE";
const char* MQTT_HOST = "TU-BROKER";   // dev: IP del PC; AWS: endpoint IoT Core
const int   MQTT_PORT = 1883;
const char* DEVICE_ID = "lilygo-01";
const char* DEVICE_NAME = "Finca Norte 01";
const char* FW_VER = "v5-mqtt";

WiFiClient net; PubSubClient mqtt(net);

float leerSuelo() { float a = analogRead(34);
  return constrain((3200.0 - a) / (3200.0 - 1500.0) * 100.0, 0, 100); }
float leerBatt()  { return analogRead(35) * 0.00586; }

void setup() {
  Serial.begin(115200); delay(400);
  analogReadResolution(12);
  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID, WIFI_PASS);
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) { delay(500); }
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(1024);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) { WiFi.reconnect(); delay(5000); return; }
  if (!mqtt.connected()) {
    if (mqtt.connect(DEVICE_ID)) Serial.println("[MQTT] conectado");
    else { Serial.printf("[MQTT] fallo rc=%d, reintentando\n", mqtt.state()); delay(5000); return; }
  }
  mqtt.loop();

  static unsigned long last = 0;
  if (millis() - last < 60000) { delay(200); return; }
  last = millis();

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["name"] = DEVICE_NAME;
  doc["fw"] = FW_VER;
  JsonArray arr = doc["sensors"].to<JsonArray>();
  JsonObject o1 = arr.add<JsonObject>();
  o1["sensor_id"] = "soil1"; o1["type"] = "soil"; o1["unit"] = "%"; o1["value"] = leerSuelo();
  JsonObject o2 = arr.add<JsonObject>();
  o2["sensor_id"] = "batt1"; o2["type"] = "battery"; o2["unit"] = "V"; o2["value"] = leerBatt();

  String topic = String("agrosense/") + DEVICE_ID + "/data";
  char buf[512];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(topic.c_str(), (const uint8_t*)buf, n, false);
  Serial.printf("[MQTT] publicado %s (%u bytes)\n", topic.c_str(), n);
  delay(200);
}
