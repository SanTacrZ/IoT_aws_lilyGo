/* Calibracion del sensor de humedad de suelo (AO en GPIO32).
 *
 * La conversion NO usa los extremos teoricos del ADC: usa los dos valores que
 * devuelve de verdad ESTE sensor, medidos con la sonda al aire (seco) y
 * sumergida en agua hasta cubrir el cobre (agua). Con los extremos medidos el
 * nodo reparte bien el 0-100% y deja de saturarse por un rango mal elegido.
 *
 * Como calibrar (una sola vez, y otra vez si el sensor deriva):
 *
 *   1. Pon CALIBRAR_SUELO en true, compila y sube el firmware.
 *   2. Con la sonda FUERA de la tierra y al aire, mira el monitor serie
 *      (115200): cuando el numero se quede quieto, ese es el seco.
 *   3. Mete la sonda en un vaso de agua hasta cubrir el cobre (sin tocar la
 *      placa), espera a que se estabilice: ese es el agua.
 *   4. Escribe en el monitor:   guardar <seco> <agua>
 *      por ejemplo               guardar 3410 1180
 *   5. Pon CALIBRAR_SUELO en false, compila y sube otra vez. La calibracion
 *      queda guardada en NVS, asi que ya no hay que medirla mas veces.
 *
 * Si el numero sale pegado siempre (por ejemplo 900 clavado con la sonda seca
 * y con la sonda mojada) el problema no es la calibracion: el sensor no esta
 * recibiendo alimentacion, el AO no esta en GPIO32 o la sonda esta en corto.
 *
 * Los valores de abajo son solo el punto de partida mientras no midas; en
 * cuanto guardes los tuyos quedan pisados por los de NVS.
 */
#pragma once

#define CALIBRAR_SUELO   false

/* seco: 3700, agua: 1400. MEDIDOS el 2026-10-16 con la sonda al aire y con la
 * punta dentro de agua. Con estos extremos la punta empapada da 100% y la
 * tierra seca da 0%. Si vuelve a derivar, reajusta sin recompilar:
 *
 *     guardar <seco> <agua>
 *
 * en el monitor serie (funciona con el firmware normal, no hace falta modo
 * calibracion); queda en NVS y sobrevive a los reinicios. */
#define RAW_SUELO_SECO   3700
#define RAW_SUELO_AGUA   1400

/* Margen en cuentas de ADC fuera del rango medido. Si el sensor se queda por
 * debajo del extremo de agua es que no esta alimentado, el AO esta suelto o
 * la sonda esta en corto: se avisa en serie en vez depublishar un 100% falso. */
#define RAW_SUELO_TOLERANCIA 300

/* Indice de luz 0-100%: NO es lux, es una medida relativa al rango que alcanza
 * ESTE sensor en ESTE sitio. El AP3216C se topa en 20.661 lux (Range 1 del
 * datasheet rev 0.86), muy por debajo del sol de mediodía ecuatorial
 * (~100.000 lux), asi que un porcentaje sobre lux reales nunca seria fiel: de ahi
 * que se calibre contra los extremos observados aqui.
 *
 *   guardar_luz <max> <min>   en el monitor serie; queda en NVS.
 *
 * Valores de partida, sin medir todavia: con el sensor tapado se midio 1 lux y
 * destapado 47-161 lux, o sea que de momento vive en sombra. Medi el valor al
 * mediodia y a las 18:00 y ajusta con el comando. */
#define LUX_PCT_MAX  200.0f
#define LUX_PCT_MIN    5.0f