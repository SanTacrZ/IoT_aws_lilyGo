"""Reglas de alerta: funciones puras, sin base de datos ni red.

evaluar() recibe el ultimo estado de cada nodo y devuelve las condiciones que
se cumplen ahora. El motor (motor.py) compara eso con las alertas abiertas
para decidir que abrir, recordar, escalar o cerrar.
"""
import os
from dataclasses import dataclass, field
from fnmatch import fnmatch

from mensajes import duracion as _duracion


def _env_float(nombre, defecto):
    return float(os.getenv(nombre, defecto))


@dataclass(frozen=True)
class Umbrales:
    nodo_caido_min: float = 15
    suelo_min: float = 30
    suelo_histeresis: float = 5
    suelo_max: float = 90
    hr_min: float = 30
    hr_max: float = 90
    temp_max: float = 35
    temp_min: float = 5
    bateria_min: float = 3.4
    rssi_min: float = -85
    ignorar: tuple = ("sim-*", "prueba-*")

    @classmethod
    def desde_env(cls):
        return cls(
            nodo_caido_min=_env_float("ALERT_NODO_CAIDO_MIN", 15),
            suelo_min=_env_float("ALERT_SUELO_MIN", 30),
            suelo_histeresis=_env_float("ALERT_SUELO_HISTERESIS", 5),
            suelo_max=_env_float("ALERT_SUELO_MAX", 90),
            hr_min=_env_float("ALERT_HR_MIN", 30),
            hr_max=_env_float("ALERT_HR_MAX", 90),
            temp_max=_env_float("ALERT_TEMP_MAX", 35),
            temp_min=_env_float("ALERT_TEMP_MIN", 5),
            bateria_min=_env_float("ALERT_BATERIA_MIN", 3.4),
            rssi_min=_env_float("ALERT_RSSI_MIN", -85),
            ignorar=tuple(p.strip() for p in os.getenv("ALERT_IGNORAR", "sim-*,prueba-*").split(",") if p.strip()),
        )


@dataclass(frozen=True)
class Condicion:
    clave: str
    tipo: str
    entidad: str
    severidad: str            # "atencion" | "critico"
    titulo: str
    detalle: str
    accion: str = ""
    valor: float | None = None


@dataclass
class Evaluacion:
    condiciones: dict = field(default_factory=dict)   # clave -> Condicion
    # Entidades cuyas alertas abiertas no se pueden juzgar ahora (nodo mudo:
    # sus ultimos valores son viejos). Esas alertas se congelan, no se cierran.
    congeladas: set = field(default_factory=set)

    def agregar(self, c):
        self.condiciones[c.clave] = c


def _num(valor):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _por_debajo(valor, umbral, histeresis, abierta):
    """Con histeresis: abre bajo el umbral y solo cierra al superar umbral+margen,
    asi un valor que oscila alrededor del umbral no manda mensajes en rafaga."""
    if valor is None:
        return abierta          # sin dato: no se decide nada nuevo
    return valor < (umbral + histeresis if abierta else umbral)


def _por_encima(valor, umbral, histeresis, abierta):
    if valor is None:
        return abierta
    return valor > (umbral - histeresis if abierta else umbral)


def evaluar(nodos, analisis, u: Umbrales, abiertas=frozenset()):
    """nodos: [{dispositivo, segundos_sin_datos, payload}], analisis: ultimo
    dictamen por entidad [{entidad, estado, diagnostico, accion_recomendada, modelo}],
    abiertas: claves de alertas abiertas (para la histeresis)."""
    ev = Evaluacion()
    nodos = [n for n in nodos if not any(fnmatch(n["dispositivo"], p) for p in u.ignorar)]
    caidos = [n for n in nodos if n["segundos_sin_datos"] > u.nodo_caido_min * 60]

    if nodos and len(caidos) == len(nodos):
        # Todos callados a la vez: casi siempre es el internet o la luz de la
        # finca, no N fallas distintas. Un solo aviso en vez de uno por nodo.
        menos = min(n["segundos_sin_datos"] for n in nodos)
        quienes = "El unico nodo lleva" if len(nodos) == 1 else f"Los {len(nodos)} nodos llevan al menos"
        ev.agregar(Condicion(
            "sin_datos_finca", "sin_datos_finca", "finca", "critico",
            "Ningun nodo reporta",
            f"{quienes} {_duracion(menos)} sin enviar datos.",
            "Revisar internet/router y energia de la finca.", menos / 60))
        ev.congeladas.update(n["dispositivo"] for n in nodos)
        return ev

    for n in caidos:
        disp = n["dispositivo"]
        ev.agregar(Condicion(
            f"nodo_caido:{disp}", "nodo_caido", disp, "critico", "Nodo sin datos",
            f"{disp} lleva {_duracion(n['segundos_sin_datos'])} sin reportar.",
            "Revisar bateria, cobertura Wi-Fi y que el nodo no este colgado.",
            n["segundos_sin_datos"] / 60))
        ev.congeladas.add(disp)

    for n in nodos:
        disp = n["dispositivo"]
        if disp in ev.congeladas:
            continue
        p = n.get("payload") or {}

        suelo = _num(p.get("humedad_suelo_pct"))
        clave = f"suelo_seco:{disp}"
        if _por_debajo(suelo, u.suelo_min, u.suelo_histeresis, clave in abiertas):
            ev.agregar(Condicion(clave, "suelo_seco", disp, "critico", "Suelo seco",
                                 f"Humedad del suelo {suelo:.0f}% (umbral {u.suelo_min:.0f}%).",
                                 "Regar la zona hoy.", suelo))

        clave = f"suelo_saturado:{disp}"
        if _por_encima(suelo, u.suelo_max, u.suelo_histeresis, clave in abiertas):
            ev.agregar(Condicion(clave, "suelo_saturado", disp, "atencion", "Suelo encharcado",
                                 f"Humedad del suelo {suelo:.0f}% (maximo {u.suelo_max:.0f}%).",
                                 "Suspender el riego y revisar el drenaje.", suelo))

        hr = _num(p.get("humedad_aire"))
        clave = f"hr_alta:{disp}"
        if _por_encima(hr, u.hr_max, 3, clave in abiertas):
            ev.agregar(Condicion(clave, "hr_alta", disp, "atencion", "Humedad del aire alta",
                                 f"Aire al {hr:.0f}% HR (maximo {u.hr_max:.0f}%).",
                                 "Riesgo de hongos: ventilar y no mojar el follaje.", hr))
        clave = f"hr_baja:{disp}"
        if _por_debajo(hr, u.hr_min, 3, clave in abiertas):
            ev.agregar(Condicion(clave, "hr_baja", disp, "atencion", "Humedad del aire baja",
                                 f"Aire al {hr:.0f}% HR (minimo {u.hr_min:.0f}%).",
                                 "Estres por transpiracion: revisar sombra y riego.", hr))

        temp = _num(p.get("temperatura_aire"))
        clave = f"temp_alta:{disp}"
        if _por_encima(temp, u.temp_max, 1, clave in abiertas):
            ev.agregar(Condicion(clave, "temp_alta", disp, "atencion", "Temperatura alta",
                                 f"Aire a {temp:.1f} °C (maximo {u.temp_max:.0f} °C).",
                                 "Revisar sombra y riego.", temp))
        clave = f"temp_baja:{disp}"
        if _por_debajo(temp, u.temp_min, 1, clave in abiertas):
            ev.agregar(Condicion(clave, "temp_baja", disp, "atencion", "Temperatura baja",
                                 f"Aire a {temp:.1f} °C (minimo {u.temp_min:.0f} °C).",
                                 "Riesgo de helada: proteger el cultivo.", temp))

        bateria = _num(p.get("bateria_v"))
        clave = f"bateria_baja:{disp}"
        if _por_debajo(bateria, u.bateria_min, 0.1, clave in abiertas):
            ev.agregar(Condicion(clave, "bateria_baja", disp, "atencion", "Bateria baja",
                                 f"Bateria en {bateria:.2f} V (minimo {u.bateria_min:.2f} V).",
                                 "Cargar o cambiar la bateria, revisar el panel solar.", bateria))

        rssi = _num(p.get("wifi_rssi"))
        clave = f"senal_debil:{disp}"
        if _por_debajo(rssi, u.rssi_min, 5, clave in abiertas):
            ev.agregar(Condicion(clave, "senal_debil", disp, "atencion", "Senal Wi-Fi debil",
                                 f"RSSI {rssi:.0f} dBm (minimo {u.rssi_min:.0f} dBm).",
                                 "Acercar el router/repetidor o reorientar la antena.", rssi))

    for a in analisis:
        # Solo los dictamenes de Gemini: los de reglas locales repiten lo que
        # ya cubren las reglas de arriba.
        if a.get("estado") != "critico" or not str(a.get("modelo") or "").startswith("gemini"):
            continue
        ent = str(a["entidad"]).removeprefix("planta:")
        ent = "finca" if ent in ("todas", "-") else ent
        if ent in ev.congeladas:
            continue
        ev.agregar(Condicion(f"ia_critico:{ent}", "ia_critico", ent, "atencion",
                             "El agente IA marca estado critico",
                             a.get("diagnostico") or "", a.get("accion_recomendada") or ""))
    return ev
