// =====================================================================
// LilyGo T-Beam — LoRaWAN OTAA (fase 5b: parcelas SIN WiFi)
// Nodo: sensores -> payload compacto 4 bytes -> gateway -> TTN -> backend
// Libs: MCCI LoRaWAN LMIC library + ArduinoJson no necesario (binario).
// IMPORTANTE: verificar pinout DIO de tu revision de T-Beam (abajo).
// Configura los keys de OTAA desde tu consola TTN (APPEUI/APPEKEY).
// =====================================================================
#include <lmic.h>
#include <hal/hal.h>
#include <SPI.h>

// ---- OTAA keys desde TTN (msb) ----
static const u1_t PROGMEM APPEUI[8] = { 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00 }; // JoinEUI
static const u1_t PROGMEM DEVEUI[8] = { 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00 }; // DevEUI (puede leerse del chip)
void os_getArtEui(u1_t* buf) { memcpy_P(buf, APPEUI, 8); }
void os_getDevEui(u1_t* buf) { memcpy_P(buf, DEVEUI, 8); }
static const u1_t PROGMEM APPKEY[16] = { 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00 };
void os_getDevKey(u1_t* buf) { memcpy_P(buf, APPKEY, 16); }

// Pinout T-Beam v1.1 (SX1276): SS=18 SCK=5 MOSI=27 MISO=19 RST=23 DIO0=26
// SI TU REVISION DIFIERE, cambia aqui (DIO1 suele ser 35/13 segun version).
const lmic_pinmap lmic_pins = {
  .nss = 18, .rxtx = LMIC_UNUSED_PIN, .rst = 23,
  .dio = { 26, 35, LMIC_UNUSED_PIN },
};

static osjob_t sendjob;
const unsigned TX_INTERVAL_SEC = 900;  // 15 min (ahorro de airtime)

float leerSuelo() { float a = analogRead(34);
  return constrain((3200.0 - a) / (3200.0 - 1500.0) * 100.0, 0, 100); }
float leerBatt()  { return analogRead(35) * 0.00586; }

void do_send(osjob_t* j) {
  if (LMIC.opmode & OP_TXRXPEND) { Serial.println("[LORA] tx pendiente, omitido"); return; }

  // payload compacto: [soil u8][temp s8][hum u8][batt u8 = V/0.02]
  uint8_t soil = (uint8_t)leerSuelo();
  float batt = leerBatt();
  int8_t temp = 22;   // TODO: DHT22 real
  uint8_t hum = 55;   // TODO: DHT22 real
  uint8_t mydata[4] = { soil, (uint8_t)temp, hum, (uint8_t)(batt / 0.02) };

  LMIC_setTxData2(1, mydata, sizeof(mydata), 0);  // fPort 1, sin confirmacion
  Serial.printf("[LORA] tx soil=%u temp=%d hum=%u batt=%.2fV\n", soil, temp, hum, batt);
}

void onEvent(ev_t ev) {
  switch (ev) {
    case EV_TXCOMPLETE:
      Serial.println("[LORA] TX completo (no confirmado)");
      os_setTimedCallback(&sendjob, os_getTime() + sec2osticks(TX_INTERVAL_SEC), do_send);
      break;
    case EV_JOINED:
      Serial.println("[LORA] JOINED (OTAA ok)");
      break;
    case EV_JOINFAILED:
    case EV_REJOIN_FAILED:
      Serial.println("[LORA] JOIN FALLO, reintentando");
      break;
    default:
      break;
  }
}

void setup() {
  Serial.begin(115200); delay(400);
  analogReadResolution(12);
  os_init();
  LMIC_reset();
  LMIC_setDrTxpow(DR_SF7, 14);
  LMIC_selectSubBand(1);   // banda us (o ajustar segun region 868/915)
  do_send(&sendjob);
}

void loop() {
  os_runloop_once();
}
