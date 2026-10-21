#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <Preferences.h>
#include <Wire.h>
#include <AP3216_WE.h>
#include <TinyGPS++.h>
#include <mbedtls/md.h>
#include <time.h>
#include <esp_task_wdt.h>
#include "secrets.h"
#include "calibracion.h"

// Cada placa se conecta al Wi-Fi de la finca y publica directamente al servidor.
// Cambia DEVICE_ID para cada unidad; no compartas el mismo ID entre nodos.
// URL_SERVIDOR es la IP de la maquina que corre los contenedores; en UPBWiFi
// la IP sale por DHCP, asi que revisa con "ip -4 addr" si cambia.
// El servidor por defecto es el local; define URL_SERVIDOR_NODO en secrets.h
// para apuntar a otro (AWS, dominio con https://...).
#ifndef URL_SERVIDOR_NODO
#define URL_SERVIDOR_NODO "http://10.8.69.125:5000/api/plantas"
#endif
static const char* URL_SERVIDOR = URL_SERVIDOR_NODO;
// scripts/nuevo_nodo.py genera un secrets.h con DEVICE_ID_NODO para cada placa.
#ifndef DEVICE_ID_NODO
#define DEVICE_ID_NODO "lilygo-tbeam-01"
#endif
static const char* DEVICE_ID = DEVICE_ID_NODO;
static constexpr uint32_t INTERVALO_LECTURA_S = 60;
// Si el loop se cuelga (I2C trabado, pila Wi-Fi bloqueada) el watchdog reinicia
// la placa en vez de dejar el nodo mudo en el campo hasta que alguien vaya.
static constexpr uint32_t WATCHDOG_S = 120;

// T-Beam SX1276: GPIO32 está conectado al DIO2 del radio. Usamos GPIO39
// (ADC1_CH3, entrada) para AO del sensor de suelo.
static constexpr int PIN_SUELO = 39;
static constexpr int PIN_GPS_RX = 34;
static constexpr int PIN_GPS_TX = 12;
static constexpr int SDA_PIN = 21;
static constexpr int SCL_PIN = 22;
static constexpr int PIN_HDC1080 = 0x40;
static constexpr int PIN_AP3216 = 0x1E;      // sensor de luz, mismo bus I2C
static constexpr int REG_AP3216_ALS = 0x0C;  // 0x0C/0x0D guardan el dato ALS
static constexpr float AP3216_LUX_POR_CUENTA = 0.35f;  // factor del rango RANGE_20661
static constexpr int PIN_PMU = 0x34;         // AXP192 (T-Beam v1.0/1.1) o AXP2101 (v1.2)

AP3216_WE sensorLuz(&Wire);

// --- Sensor de luz ----------------------------------------------------------
// La libreria AP3216_WE no comprueba el resultado de las transacciones de Wire:
// si el bus falla, readReg() devuelve 0 y getAmbientLight() reporta "0 lux", que
// es indistinguible de tener el sensor en oscuridad. Aqui cada transaccion se
// verifica y se reintenta, para que un sensor que no responde se note como tal
// y no como una medida valida.
// El init() de la libreria hace el reset por software y acto seguido escribe
// el modo y el rango, sin esperar los 10 ms que pide el datasheet (rev 0.86,
// "For SW reset"). Ahi se queda el ALS descolocado y devuelve 0. Ademas se
// activa solo el ALS: el modo ALS+PS enciende el LED de proximidad cada 112.5
// ms y aqui no se usa el sensor de distancia para nada.
void reiniciarLuz() {
  sensorLuz.init();
  sensorLuz.setMode(AP3216_ALS);
  sensorLuz.setLuxRange(RANGE_20661);
  delay(250);  // 10 ms del reset + una conversion completa (~100 ms)
}

bool leerAp3216(int reg, uint8_t* dest, uint8_t n) {
  for (uint8_t intento = 0; intento < 3; ++intento) {
    Wire.beginTransmission(PIN_AP3216);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) continue;
    if (Wire.requestFrom(PIN_AP3216, n) != n) continue;
    for (uint8_t i = 0; i < n; ++i) dest[i] = Wire.read();
    return true;
  }
  return false;
}

float leerLux() {
  uint8_t als[2] = {0, 0};
  if (!leerAp3216(REG_AP3216_ALS, als, 2)) {
    Serial.println("AP3216 sin respuesta; se reinicia el sensor de luz.");
    reiniciarLuz();
    if (!leerAp3216(REG_AP3216_ALS, als, 2)) return NAN;
  }
  uint16_t raw = (uint16_t(als[1]) << 8) | als[0];
  // El ALS no tiene bit de validez (0x0C/0x0D son solo el ADC), asi que un 0
  // puede ser una conversion que todavia no ha terminado. Se reintenta unas
  // cuantas veces y, si el chip nunca entrega dato, se devuelve NAN para que la
  // lectura se omita en lugar de publicar un 0 lux inventado.
  for (uint8_t intento = 0; intento < 4 && raw == 0; ++intento) {
    delay(150);
    if (!leerAp3216(REG_AP3216_ALS, als, 2)) break;
    raw = (uint16_t(als[1]) << 8) | als[0];
  }
  if (raw == 0) {
    Serial.println("AP3216 entrega ALS=0 tras varios intentos; lectura omitida.");
    return NAN;
  }
  return AP3216_LUX_POR_CUENTA * raw;
}

// --- Bateria ------------------------------------------------------------------
// El T-Beam mide la bateria con su PMU en el mismo bus I2C. Segun la revision
// de la placa es un AXP192 o un AXP2101, con registros distintos: se detecta
// por el chip id (registro 0x03) y si no responde se omite el dato.
enum class Pmu : uint8_t { Ninguno, AXP192, AXP2101 };
Pmu pmu = Pmu::Ninguno;

bool pmuLeer(uint8_t reg, uint8_t* v) {
  Wire.beginTransmission(PIN_PMU);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(PIN_PMU, 1) != 1) return false;
  *v = Wire.read();
  return true;
}

bool pmuEscribir(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(PIN_PMU);
  Wire.write(reg);
  Wire.write(v);
  return Wire.endTransmission() == 0;
}

void detectarPmu() {
  uint8_t id = 0, r = 0;
  if (!pmuLeer(0x03, &id)) return;
  if (id == 0x4A) {                                    // AXP2101: habilita el ADC de VBAT
    pmu = Pmu::AXP2101;
    if (pmuLeer(0x30, &r)) pmuEscribir(0x30, r | 0x01);
  } else if (id == 0x03) {                             // AXP192: ADC de voltaje de bateria
    pmu = Pmu::AXP192;
    if (pmuLeer(0x82, &r)) pmuEscribir(0x82, r | 0x80);
  }
  Serial.printf("PMU: %s\n", pmu == Pmu::AXP2101 ? "AXP2101" : pmu == Pmu::AXP192 ? "AXP192" : "desconocido");
}

float leerBateria() {
  uint8_t h = 0, l = 0;
  float v = NAN;
  if (pmu == Pmu::AXP192 && pmuLeer(0x78, &h) && pmuLeer(0x79, &l)) {
    v = ((uint16_t(h) << 4) | (l & 0x0F)) * 1.1f / 1000.0f;        // 1.1 mV por cuenta
  } else if (pmu == Pmu::AXP2101 && pmuLeer(0x34, &h) && pmuLeer(0x35, &l)) {
    v = ((uint16_t(h & 0x1F) << 8) | l) / 1000.0f;                  // mV directos
  }
  // Sin bateria conectada el ADC devuelve valores sin sentido: mejor omitirlo.
  return (v > 2.5f && v < 4.5f) ? v : NAN;
}

TinyGPSPlus gps;
HardwareSerial gpsSerial(1);
uint32_t ultimoEnvio = 0;
uint32_t ultimoWifi = 0;
uint32_t arranques = 0;
Preferences colaNvs;
static constexpr uint8_t MAX_PENDIENTES = 8;
static constexpr uint8_t MUESTRAS_SUELO = 16;

// --- Calibracion del sensor de suelo ----------------------------------------
// Los extremos medidos se guardan en NVS para poder reajustar desde el monitor
// serie sin recompilar; calibracion.h solo aporta el punto de partida.

struct CalSuelo { uint16_t seco; uint16_t agua; };
struct CalLuz { float max; float min; };

CalLuz calLuz;

CalLuz leerCalLuz() {
  CalLuz c{LUX_PCT_MAX, LUX_PCT_MIN};
  colaNvs.begin("lecturas", false);
  if (colaNvs.isKey("luz_max")) c.max = colaNvs.getFloat("luz_max", LUX_PCT_MAX);
  if (colaNvs.isKey("luz_min")) c.min = colaNvs.getFloat("luz_min", LUX_PCT_MIN);
  if (c.max - c.min < 1.0f) { c.max = LUX_PCT_MAX; c.min = LUX_PCT_MIN; }
  return c;
}

// Indice de luz 0-100%. Es relativo al rango real de esta instalacion, no lux
// fotometricos: el AP3216C satura en 20.661 lux (Range 1, datasheet rev 0.86)
// y el sol de mediodía ecuatorial ronda los 100.000, asi que un porcentaje
// sobre lux reales no seria creible.
int pctLuz(float lux) {
  const float delta = lux - calLuz.min;
  const float span = calLuz.max - calLuz.min;
  return constrain((int)(delta * 100.0f / span), 0, 100);
}

CalSuelo leerCalSuelo() {
  CalSuelo c{RAW_SUELO_SECO, RAW_SUELO_AGUA};
  colaNvs.begin("lecturas", false);
  if (colaNvs.isKey("suelo_seco")) c.seco = colaNvs.getUShort("suelo_seco", RAW_SUELO_SECO);
  if (colaNvs.isKey("suelo_agua")) c.agua = colaNvs.getUShort("suelo_agua", RAW_SUELO_AGUA);
  if (c.seco == c.agua) c.seco = RAW_SUELO_SECO;
  return c;
}

bool guardarCalSuelo(uint16_t seco, uint16_t agua) {
  if (seco == agua || abs((int)seco - (int)agua) < 200) {
    Serial.printf("Calibracion rechazada: seco=%u agua=%u son demasiado parecidos.\n", seco, agua);
    return false;
  }
  colaNvs.putUShort("suelo_seco", seco);
  colaNvs.putUShort("suelo_agua", agua);
  Serial.printf("Calibracion guardada: seco=%u agua=%u (0%%..100%%).\n", seco, agua);
  return true;
}

// El sensor de suelo baja el voltaje conforme se moja la tierra, asi que el
// mapeo va del extremo seco (0%) al de agua (100%). Si al sensor le da por
// mostrarlos al reves, el signo del span ya sale bien sin tocar nada.
int pctDesdeRaw(uint16_t raw, const CalSuelo& c) {
  const int span = (int)c.seco - (int)c.agua;
  const int delta = (int)c.seco - (int)raw;
  return constrain(delta * 100 / span, 0, 100);
}

bool rangoCalibrado(uint16_t raw, const CalSuelo& c) {
  const uint16_t alto = (uint16_t)max((int)c.seco, (int)c.agua) + RAW_SUELO_TOLERANCIA;
  const uint16_t bajo = (uint16_t)max(0, min((int)c.seco, (int)c.agua) - (int)RAW_SUELO_TOLERANCIA);
  return raw >= bajo && raw <= alto;
}

uint16_t leerRawSuelo() {
  uint32_t acc = 0;
  for (uint8_t i = 0; i < MUESTRAS_SUELO; ++i) acc += analogRead(PIN_SUELO);
  return acc / MUESTRAS_SUELO;
}

// --- Consola de calibracion --------------------------------------------------

CalSuelo calSuelo;
char lineaConsola[48];
uint8_t posConsola = 0;

void ejecutarComandoCal(const char* cmd) {
  int seco = 0, agua = 0;
  if (sscanf(cmd, "guardar %d %d", &seco, &agua) == 2) {
    if (seco >= 0 && agua >= 0 && seco <= 4095 && agua <= 4095) {
      guardarCalSuelo((uint16_t)seco, (uint16_t)agua);
      calSuelo = leerCalSuelo();
    }
    return;
  }
  float lmax = 0, lmin = 0;
  if (sscanf(cmd, "guardar_luz %f %f", &lmax, &lmin) == 2) {
    if (lmin >= 0.0f && lmax <= 20661.0f && lmax - lmin >= 1.0f) {
      colaNvs.putFloat("luz_max", lmax);
      colaNvs.putFloat("luz_min", lmin);
      calLuz = leerCalLuz();
      Serial.printf("Indice de luz: 100%% a %.1f lux, 0%% a %.1f lux.\n", calLuz.max, calLuz.min);
    } else {
      Serial.println("Valores invalidos: se necesita 0 <= min < max <= 20661.");
    }
    return;
  }
  if (strcmp(cmd, "ver") == 0) {
    Serial.printf("Calibracion activa: suelo seco=%u agua=%u | luz 100%%=%.1f 0%%=%.1f\n",
                  calSuelo.seco, calSuelo.agua, calLuz.max, calLuz.min);
    return;
  }
  Serial.println("Comandos: guardar <seco> <agua> | guardar_luz <max> <min> | ver");
}

void atenderConsolaCal() {
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      lineaConsola[posConsola] = '\0';
      if (posConsola > 0) ejecutarComandoCal(lineaConsola);
      posConsola = 0;
    } else if (posConsola < sizeof(lineaConsola) - 1) {
      lineaConsola[posConsola++] = c;
    }
  }
}

void modoCalibracion() {
  static uint32_t ultimo = 0;
  atenderConsolaCal();
  if (millis() - ultimo < 500) return;
  ultimo = millis();
  const uint16_t raw = leerRawSuelo();
  Serial.printf("raw=%u (%.2f V)  pct=%d%%  [seco=%u agua=%u]%s\n", raw,
                raw * 3.3f / 4095.0f, pctDesdeRaw(raw, calSuelo), calSuelo.seco, calSuelo.agua,
                rangoCalibrado(raw, calSuelo) ? "" : "  <-- FUERA DE RANGO: revisa VCC/AO/sonda");
}

float leerHDC(uint8_t registro, float escala, float offset) {
  Wire.beginTransmission(PIN_HDC1080);
  Wire.write(registro);
  if (Wire.endTransmission() != 0) return NAN;
  delay(20);
  if (Wire.requestFrom(PIN_HDC1080, 2) != 2) return NAN;
  uint16_t raw = (uint16_t(Wire.read()) << 8) | Wire.read();
  return raw / 65536.0f * escala + offset;
}

String firmaHmac(const String& mensaje) {
  uint8_t digest[32];
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md_hmac(info, (const unsigned char*)HMAC_SECRET, strlen(HMAC_SECRET),
                  (const unsigned char*)mensaje.c_str(), mensaje.length(), digest);
  static const char hex[] = "0123456789abcdef";
  String out; out.reserve(64);
  for (uint8_t b : digest) { out += hex[b >> 4]; out += hex[b & 15]; }
  return out;
}

void conectarWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t inicio = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - inicio < 15000) delay(250);
  if (WiFi.status() == WL_CONNECTED) {
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    Serial.printf("Wi-Fi conectado. IP: %s\n", WiFi.localIP().toString().c_str());
  } else Serial.println("Wi-Fi no disponible; se reintentará sin reiniciar el nodo.");
}

// Ruta + query de la URL. La firma v2 los cubre: sin eso, un POST capturado se
// podia reenviar cambiando ?dispositivo= o ?lectura_id= sin romper la firma.
String recursoDe(const String& url) {
  const int esquema = url.indexOf("://");
  const int barra = url.indexOf('/', esquema < 0 ? 0 : esquema + 3);
  return barra < 0 ? String("/") : url.substring(barra);
}

bool enviar(const String& body, uint64_t lecturaId) {
  if (WiFi.status() != WL_CONNECTED) return false;
  time_t ahora = time(nullptr);
  if (ahora < 1700000000) return false; // el servidor exige hora sincronizada para anti-replay
  String ts = String((uint32_t)ahora);
  String url = String(URL_SERVIDOR) + "?dispositivo=" + DEVICE_ID + "&lectura_id=" + String((uint32_t)lecturaId);
  String firma = firmaHmac(ts + ".POST." + recursoDe(url) + "." + body);
  esp_task_wdt_reset();
  HTTPClient http;
  // El cliente TLS vive hasta el final de la funcion: si se declara dentro del
  // if, se destruye antes del POST y HTTPClient queda usando memoria liberada.
  WiFiClientSecure clienteTls;
  bool ok = false;
  if (url.startsWith("https://")) {
#ifdef SERVER_CA_PEM
    clienteTls.setCACert(SERVER_CA_PEM);
#else
    clienteTls.setInsecure();  // define SERVER_CA_PEM en secrets.h para validar el servidor
#endif
    ok = http.begin(clienteTls, url);
  } else {
    ok = http.begin(url);
  }
  if (!ok) return false;
  http.setTimeout(8000);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Api-Key", DEVICE_API_KEY);
  http.addHeader("X-Timestamp", ts);
  http.addHeader("X-Firma-Version", "2");
  http.addHeader("X-Signature", firma);
  int codigo = http.POST(body);
  String respuesta = http.getString();
  http.end();
  Serial.printf("POST HTTP %d: %s\n", codigo, respuesta.c_str());
  return codigo >= 200 && codigo < 300;
}

uint8_t pendientes() { return colaNvs.getUChar("count", 0); }

void guardarPendiente(const String& body, uint32_t lecturaId) {
  uint8_t count = pendientes();
  if (count >= MAX_PENDIENTES) {
    Serial.println("Cola local llena; se conserva lo pendiente y se omite esta lectura.");
    return;
  }
  String item = String(lecturaId) + "|" + body;
  colaNvs.putString(("q" + String(count)).c_str(), item);
  colaNvs.putUChar("count", count + 1);
}

void enviarPendientes() {
  while (WiFi.status() == WL_CONNECTED && pendientes() > 0) {
    String item = colaNvs.getString("q0", "");
    int sep = item.indexOf('|');
    if (sep <= 0) {
      Serial.println("Elemento inválido en cola local; se descarta ese elemento.");
    } else {
      uint32_t id = (uint32_t)item.substring(0, sep).toInt();
      if (id == 0 || !enviar(item.substring(sep + 1), id)) break;
    }
    uint8_t count = pendientes();
    for (uint8_t i = 1; i < count; ++i) {
      String next = colaNvs.getString(("q" + String(i)).c_str(), "");
      colaNvs.putString(("q" + String(i - 1)).c_str(), next);
    }
    colaNvs.remove(("q" + String(count - 1)).c_str());
    colaNvs.putUChar("count", count - 1);
  }
}

void drenarGPS() {
  while (gpsSerial.available()) gps.encode(gpsSerial.read());
}

void tomarYEnviar() {
  const float temperatura = leerHDC(0x00, 165.0f, -40.0f);
  const float humedad = leerHDC(0x01, 100.0f, 0.0f);
  const float lux = leerLux();
  const int luzPct = pctLuz(lux);
  const uint16_t rawSuelo = leerRawSuelo();
  // No se inventan lecturas si falla un sensor esencial: se registra y se omite el envío.
  if (rawSuelo == 0) {
    Serial.println("Lectura omitida: AO del sensor de suelo marca 0; revisar AO, GND y GPIO39.");
    return;
  }
  const int sueloPct = pctDesdeRaw(rawSuelo, calSuelo);
  if (!rangoCalibrado(rawSuelo, calSuelo)) {
    Serial.printf("AVISO: sensor de suelo fuera de rango (raw=%u). Revisa VCC del sensor, que "
                  "el AO este en GPIO39 y que la sonda no este en corto.\n", rawSuelo);
  }
  drenarGPS();
  const bool gpsValido = gps.location.isValid() && gps.location.age() < 30000;
  const float lat = gpsValido ? gps.location.lat() : 0.0f;
  const float lon = gpsValido ? gps.location.lng() : 0.0f;
  const float alt = gps.altitude.isValid() ? gps.altitude.meters() : 0.0f;
  const int sats = gps.satellites.isValid() ? gps.satellites.value() : 0;

  // No se inventan lecturas si falla un sensor esencial: se registra y se omite el envío.
  if (!isfinite(temperatura) || !isfinite(humedad) || !isfinite(lux)) {
    Serial.println("Lectura omitida: error de sensor ambiental; no se envían ceros.");
    return;
  }
  const uint64_t lecturaId = (uint64_t)time(nullptr);
  String body = "{";
  body += "\"temperatura_aire\":" + String(temperatura, 2);
  body += ",\"humedad_aire\":" + String(humedad, 2);
  body += ",\"luz_lux\":" + String(lux, 2);
  body += ",\"luz_pct\":" + String(luzPct);
  body += ",\"humedad_suelo_pct\":" + String(sueloPct);
  body += ",\"humedad_suelo_raw\":" + String(rawSuelo);
  body += ",\"alerta_seco\":" + String(sueloPct < 30 ? "true" : "false");
  body += ",\"latitud\":" + String(lat, 6) + ",\"longitud\":" + String(lon, 6);
  body += ",\"altitud\":" + String(alt, 2) + ",\"satelites\":" + String(sats);
  // Salud del nodo: con esto el servidor puede avisar de bateria baja, senal
  // debil o reinicios en bucle antes de que el nodo se quede mudo.
  body += ",\"uptime_s\":" + String(millis() / 1000UL);
  body += ",\"wifi_rssi\":" + String(WiFi.RSSI());
  body += ",\"heap_libre\":" + String(ESP.getFreeHeap());
  body += ",\"arranques\":" + String(arranques);
  body += ",\"pendientes\":" + String(pendientes());
  const float bateria = leerBateria();
  if (isfinite(bateria)) body += ",\"bateria_v\":" + String(bateria, 2);
  body += "}";
  Serial.printf("Lectura real: T=%.2f H=%.2f lux=%.2f (%d%%) suelo=%d%% raw=%d\n",
                temperatura, humedad, lux, luzPct, sueloPct, rawSuelo);
  if (lecturaId > 0) guardarPendiente(body, (uint32_t)lecturaId);
  enviarPendientes();
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_SUELO, INPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_SUELO, ADC_11db);
  Wire.begin(SDA_PIN, SCL_PIN, 100000);
  reiniciarLuz();
  gpsSerial.begin(9600, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  WiFi.setSleep(false);
  colaNvs.begin("lecturas", false);
  arranques = colaNvs.getULong("arranques", 0) + 1;
  colaNvs.putULong("arranques", arranques);
  detectarPmu();
  esp_task_wdt_init(WATCHDOG_S, true);
  esp_task_wdt_add(nullptr);
  calSuelo = leerCalSuelo();
  calLuz = leerCalLuz();
  if (CALIBRAR_SUELO) {
    Serial.println("MODO CALIBRACION: no se envian lecturas. Mide seco y agua, luego: guardar <seco> <agua>");
    return;
  }
  conectarWifi();
  ultimoEnvio = millis() - INTERVALO_LECTURA_S * 1000UL;
  Serial.printf("Arranque #%lu\n", (unsigned long)arranques);
  Serial.printf("Nodo %s listo; envío cada %lu s. Suelo: seco=%u agua=%u | luz: 100%%=%.1f 0%%=%.1f\n",
                DEVICE_ID, (unsigned long)INTERVALO_LECTURA_S, calSuelo.seco, calSuelo.agua,
                calLuz.max, calLuz.min);
}

void loop() {
  esp_task_wdt_reset();
  if (CALIBRAR_SUELO) {
    modoCalibracion();
    delay(10);
    return;
  }
  drenarGPS();
  atenderConsolaCal();  // "guardar <seco> <agua>" reajusta la calibracion en caliente
  if (WiFi.status() != WL_CONNECTED && millis() - ultimoWifi >= 10000) {
    ultimoWifi = millis(); conectarWifi();
  }
  const uint32_t ahora = millis();
  if (ahora - ultimoEnvio >= INTERVALO_LECTURA_S * 1000UL) {
    ultimoEnvio = ahora;
    tomarYEnviar();
  }
  delay(10);
}
