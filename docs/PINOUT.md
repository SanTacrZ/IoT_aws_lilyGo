# Conexiones del nodo LilyGo T-Beam

Este pinout corresponde al firmware activo `firmware/lilygo_planta/src/main.cpp`.
Los sensores HDC1080 y AP3216 comparten el bus I2C.

| Dispositivo / señal | Pin T-Beam | Uso |
|---|---:|---|
| Bus I2C SDA | GPIO21 | HDC1080 y AP3216 |
| Bus I2C SCL | GPIO22 | HDC1080 y AP3216 |
| Salida analógica AO del sensor de suelo | GPIO39 (ADC1_CH3) | Entrada analógica, solo lectura |
| GPS TX hacia ESP32 RX | GPIO34 | UART1, 9600 baudios |
| GPS RX desde ESP32 TX | GPIO12 | UART1, 9600 baudios |

En T-Beam SX1276, GPIO32 está conectado internamente a DIO2 del radio aunque
el firmware no use LoRa; por eso no se usa para el sensor. GPIO39 es ADC1_CH3
y funciona como entrada analógica junto con Wi-Fi. Conecta **AO** a GPIO39,
VCC y GND correctamente; deja DO sin conectar. Verifica que AO no supere 3.3 V:
los GPIO del ESP32 no toleran 5 V. Los extremos de calibración del porcentaje
son provisionales y deben ajustarse con lecturas reales en suelo seco y húmedo.
