import base64
import hmac
import os
from datetime import datetime, timezone

import pandas as pd
import psycopg2
import dash
import plotly.graph_objs as go
import requests
from dash import dcc, html, dash_table
from dash.dependencies import Input, Output
from flask import Flask, jsonify, redirect, request, session

# Flask server
server = Flask(__name__)

# El dashboard se sirve en la red local (para abrirlo desde el celular), asi que
# pide usuario y contraseña.
#
# Login con cookie de sesión y NO Basic Auth: con Basic, si el navegador cancela
# el aviso de credenciales los scripts de Dash responden 401 y la página se
# queda en "Cargando" para siempre (el usuario no ve ni el login).
WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")
WEB_SESSION_SECRET = os.getenv("WEB_SESSION_SECRET", WEB_PASSWORD or "cambia-esto-sesion")

# La cookie va firmada; sin esto Flask no puede guardarla.
server.secret_key = WEB_SESSION_SECRET

# Cookies de sesion bien declaradas. Sin SameSite explicito el navegador aplica
# su valor por defecto y el POST que Dash hace a /_dash-update-component puede
# volver sin la cookie -> 302 al login -> la pagina se queda en "Cargando".
server.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_AGE=60 * 60 * 24 * 7,      # 7 dias
    SESSION_COOKIE_NAME="iot_cultivo",
)


@server.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        usuario = request.form.get("usuario", "")
        clave = request.form.get("clave", "")
        # compare_digest en ambos: comparar con == filtra por tiempo.
        ok = hmac.compare_digest(usuario, WEB_USER) & hmac.compare_digest(clave, WEB_PASSWORD)
        if ok and WEB_PASSWORD:
            session["autenticado"] = True
            destino = request.args.get("next") or "/dashboard/"
            if not destino.startswith("/dashboard"):
                destino = "/dashboard/"
            return redirect(destino)
        error = "Usuario o contrasena incorrectos."
    # La plantilla tiene CSS con "%" (width:100%): se rellena con replace, no
    # con % (daria ValueError: unsupported format character).
    aviso = f'<p class="err">{error}</p>' if error else ""
    return PAGINA_LOGIN.replace("@AVISO@", aviso), (401 if error else 200)


@server.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@server.before_request
def exigir_sesion():
    if not WEB_PASSWORD:                      # sin clave configurada: abierto
        return None
    if session.get("autenticado"):
        return None
    if request.path in ("/login", "/logout", "/health"):
        return None
    if request.path.startswith("/dashboard/_dash"):
        # Las llamadas internas de Dash son XHR: si les devolviéramos un 302 al
        # login, Dash espera JSON, no HTML, y se queda en "Cargando" para
        # siempre. Un 401 limpio deja claro que hay que volver a entrar.
        return jsonify({"error": "sesion expirada", "login": "/login"}), 401
    return redirect("/login?next=" + request.path)


PAGINA_LOGIN = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IoT Cultivo - acceso</title>
<style>
 body{background:#000;color:#e8e8ec;font-family:Arial,Helvetica,sans-serif;
      display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
 form{background:#0d0d0f;border:1px solid #24242b;border-radius:12px;padding:28px;width:290px}
 h1{font-size:18px;margin:0 0 4px}
 p.sub{color:#9a9aa5;font-size:13px;margin:0 0 20px}
 label{display:block;font-size:12px;color:#9a9aa5;margin:12px 0 4px}
 input{width:100%;box-sizing:border-box;background:#0a0a0c;color:#e8e8ec;
       border:1px solid #24242b;border-radius:8px;padding:10px;font-size:14px}
 button{width:100%;margin-top:20px;background:#22c55e;color:#04140b;border:0;
        border-radius:8px;padding:11px;font-size:15px;font-weight:bold;cursor:pointer}
 .err{color:#f87171;font-size:13px;margin-top:14px}
</style></head><body>
<form method="post">
  <h1>IoT Cultivo</h1>
  <p class="sub">Monitoreo del cultivo</p>
  <label>Usuario</label><input name="usuario" autocomplete="username" autofocus>
  <label>Contrasena</label><input name="clave" type="password" autocomplete="current-password">
  <button type="submit">Entrar</button>
  @AVISO@
</form></body></html>"""

# Dash app embedded in Flask
app = dash.Dash(__name__, server=server, url_base_pathname="/dashboard/")

PG_HOST = os.getenv("PG_HOST", "postgres")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_DB = os.getenv("PG_DB", "iot")
PG_USER = os.getenv("PG_USER", "iot")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")

AI_AGENT_URL = f"http://{os.getenv('AI_AGENT_HOST', 'ai-agent')}:{os.getenv('AI_AGENT_PORT', '5050')}"
AGENT_URL = f"http://{os.getenv('IOT_AGENT_HOST', 'iot-agent')}:{os.getenv('IOT_AGENT_PORT', '5000')}"

# A partir de estos minutos sin lecturas, el dashboard avisa: si no,画出 la
# ultima lectura congelada como si fuera del vivo.
MINUTOS_AVISO_SIN_DATOS = float(os.getenv("MINUTOS_AVISO_SIN_DATOS", "15"))
MINUTOS_CRITICO_SIN_DATOS = float(os.getenv("MINUTOS_CRITICO_SIN_DATOS", "60"))

TEMPLATE = "plotly_dark"
PLOT_BG = "rgba(0,0,0,0)"      # deja ver el fondo negro del panel
FUENTE = 12

COLUMNAS = [
    "time_index",
    "entity_id",
    "dispositivo",
    "temperatura_aire",
    "humedad_aire",
    "luz_lux",
    "luz_pct",
    "humedad_suelo_pct",
    "alerta_seco",
]


def get_sensor_data(limit=100):
    """Query the TimescaleDB hypertable via the lecturas_planta view."""
    try:
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            dbname=PG_DB,
            user=PG_USER,
            password=PG_PASSWORD,
            connect_timeout=5,
        )
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT time_index, entity_id, dispositivo, temperatura_aire,
                       humedad_aire, luz_lux, luz_pct, humedad_suelo_pct, alerta_seco
                FROM lecturas_planta
                ORDER BY time_index DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        conn.close()
        if rows:
            df = pd.DataFrame(rows, columns=COLUMNAS)
            df["time_index"] = pd.to_datetime(df["time_index"])
            return df
        return pd.DataFrame(columns=COLUMNAS)
    except Exception as e:
        print(f"Error querying PostgreSQL: {e}")
        return pd.DataFrame(columns=COLUMNAS)


def get_nodos():
    """Estado de cada nodo desde el iot-agent (vivo/caido + salud del equipo)."""
    try:
        r = requests.get(f"{AGENT_URL}/api/nodos", timeout=4,
                         headers={"X-Api-Key": os.getenv("READ_API_KEY") or os.getenv("DEVICE_API_KEY", "")})
        if r.ok:
            return r.json().get("nodos", [])
    except requests.RequestException as e:
        # El dashboard debe poder cargar aunque el iot-agent esté reiniciando.
        print(f"Error consultando nodos en iot-agent: {e}")
    return []


def _frescor(valor, minutos):
    """Valido = hay dato; fresco = el sensor se movio respecto a la lectura anterior."""
    if valor is None:
        return "sin-dato"
    return "ok" if minutos is not None and minutos < 15 else "reciente"


def panel_nodo(nodos):
    """Tarjeta de informacion del nodo y estado en vivo de cada sensor."""
    if not nodos:
        return html.Div([
            html.P('🔌 Sin datos de nodos.', style={'margin': '0 0 6px 0', 'fontWeight': 'bold'}),
            html.P('El iot-agent no reporta ningun dispositivo en las ultimas 24 h.',
                   style={'margin': '0', 'fontSize': '13px', 'color': '#9a9aa5'}),
        ], className='agent-card agent-vacio')

    n = nodos[0]
    mins = (n.get("segundos_sin_datos") or 0) / 60.0
    online = n.get("en_linea")
    info = n.get("nodo") or {}
    sens = n.get("sensores") or {}

    estado_txt = "EN VIVO" if online else (f"CAÍDO hace {mins:.0f} min" if mins < 60
                                            else f"CAÍDO hace {mins/60:.0f} h")

    # --- Sensors: dato presente? valor? ---
    filas_sensor = []
    etiquetas = [
        ("🌡️ Temperatura", "temperatura_aire", "°C"),
        ("💧 Humedad aire", "humedad_aire", "%"),
        ("☀️ Luz", "luz_lux", "lux"),
        ("🌓 Índice luz", "luz_pct", "%"),
        ("🌱 Humedad suelo", "humedad_suelo_pct", "%"),
        ("🛰️ GPS", "satelites", "satélites"),
    ]
    for nombre, clave, unidad in etiquetas:
        val = sens.get(clave)
        if val is None:
            estado, color, texto = "sin-dato", "#6b6b76", "sin dato"
        elif clave == "satelites":
            ok_sat = val > 0
            estado = "ok" if ok_sat else "sin-fix"
            color = "#4ade80" if ok_sat else "#fbbf24"
            texto = f"{val} sat" if ok_sat else "sin fix"
        else:
            estado, color = "ok", "#4ade80"
            texto = f"{val} {unidad}"
        filas_sensor.append(html.Div([
            html.Span(f"{nombre}", style={'flex': '1 1 auto'}),
            html.Span([texto, html.Span(' ●', style={'color': color})],
                      style={'fontWeight': 'bold', 'color': '#e8e8ec'}),
        ], className='sensor-fila'))

    # --- Salud del equipo ---
    if online:
        reinicio = info.get("reinicia_en_s")
        if reinicio is None:
            texto_reinicio = "sin dato"
        elif reinicio <= 0:
            texto_reinicio = "reiniciando ahora"
        elif reinicio > 3600:
            texto_reinicio = f"en {reinicio/3600:.1f} h"
        else:
            texto_reinicio = f"en {reinicio//60} min"
        rssi = info.get("wifi_rssi")
        salud = [
            ("⏱️ Uptime", f"{(info.get('uptime_s') or 0)//3600} h {((info.get('uptime_s') or 0)%3600)//60} min"),
            ("🔄 Arranques", str(info.get("arranques") if info.get("arranques") is not None else "—")),
            ("♻️ Reinicio auto.", texto_reinicio),
            ("📶 Wi-Fi", f"{rssi} dBm" if rssi else "sin señal"),
            ("🧠 Memoria libre", f"{(info.get('heap_libre') or 0)//1024} KB"),
            ("📤 Pendientes", str(info.get("pendientes") if info.get("pendientes") is not None else "—")),
        ]
    else:
        salud = [("⏱️ Uptime", "—"), ("🔄 Arranques", "—"), ("♻️ Reinicio auto.", "—"),
                 ("📶 Wi-Fi", "—"), ("🧠 Memoria libre", "—"), ("📤 Pendientes", "—")]

    return html.Div([
        html.Div([
            html.Span(f"🔌 {n.get('dispositivo')}", style={'fontWeight': 'bold', 'fontSize': '15px',
                                                          'color': '#e8e8ec'}),
            html.Span(f"   {'🟢' if online else '🔴'} {estado_txt}",
                      style={'marginLeft': '10px', 'fontSize': '13px', 'fontWeight': 'bold',
                             'color': '#4ade80' if online else '#f87171'}),
        ]),
        html.Div([
            html.Div([
                html.Div([html.Span(k, style={'color': '#9a9aa5', 'fontSize': '12px'}),
                          html.Span(f"  {v}", style={'fontSize': '13px', 'color': '#e8e8ec',
                                                     'fontWeight': 'bold'})],
                         style={'padding': '3px 0'})
                for k, v in salud
            ], style={'flex': '1 1 260px'}),
            html.Div([f for f in filas_sensor], style={'flex': '1 1 260px'}),
        ], style={'display': 'flex', 'flexWrap': 'wrap', 'gap': '22px', 'marginTop': '10px'}),
        html.P(f"Última lectura: {str(n.get('ultima_lectura'))[:19].replace('T',' ')} · id {n.get('lectura_id')}",
               className='agent-foot'),
    ], className=f'agent-card {"agent-ok" if online else "agent-critico"}')


def get_analisis_ia():
    """Ultimo dictamen del agente IA y su historial."""
    try:
        r = requests.get(f"{AI_AGENT_URL}/api/agente/ultimo", timeout=4)
        ultimo = r.json().get("analisis") if r.ok else None
    except requests.RequestException:
        ultimo = None

    try:
        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB,
                                user=PG_USER, password=PG_PASSWORD, connect_timeout=5)
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT created_at, entidad, estado, diagnostico, accion_recomendada, confianza
                FROM analisis_ia ORDER BY created_at DESC LIMIT 10
                """
            )
            cols = ['created_at', 'entidad', 'estado', 'diagnostico', 'accion_recomendada', 'confianza']
            df = pd.DataFrame(cur.fetchall(), columns=cols)
        conn.close()
        if not df.empty:
            df['created_at'] = pd.to_datetime(df['created_at']).dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception as e:
        print(f"Error leyendo analisis_ia: {e}")
        df = pd.DataFrame(columns=['created_at', 'entidad', 'estado', 'diagnostico',
                                   'accion_recomendada', 'confianza'])
    return ultimo, df


def agente_panel(ultimo):
    """Tarjeta del agente con su veredicto. Colores por clase segun el estado."""
    if ultimo is None:
        return html.Div([
            html.P('🤖 El agente todavía no ha emitido ningún dictamen.',
                   style={'margin': '0 0 6px 0', 'fontWeight': 'bold'}),
            html.P('Analiza las lecturas cada pocos minutos; su veredicto aparece aquí y en Orion.',
                   style={'margin': '0', 'fontSize': '13px', 'color': '#9a9aa5'}),
        ], className='agent-card agent-vacio')

    estado = (ultimo.get('estado') or 'error').lower()
    etiquetas = {'ok': '✅ SALUDABLE', 'atencion': '⚠️ ATENCIÓN',
                 'critico': '🚨 CRÍTICO', 'error': '⛔ ERROR'}
    confianza = float(ultimo.get('confianza') or 0) * 100
    creado = str(ultimo.get('created_at') or '')[:19].replace('T', ' ')

    return html.Div([
        html.Div([
            html.Span(etiquetas.get(estado, estado.upper()), className='agent-head'),
            html.Span(f'confianza {confianza:.0f}%   ·   {ultimo.get("modelo") or "s/modelo"}   ·   {creado}',
                      className='agent-meta'),
        ]),
        html.P(ultimo.get('diagnostico') or '', className='agent-diagnostico'),
        html.P([html.Strong('Acción sugerida: '),
                html.Span(ultimo.get('accion_recomendada') or '—')],
               className='agent-accion'),
        html.P(f'Vigilando: {ultimo.get("entidad")}', className='agent-foot'),
    ], className=f'agent-card agent-{estado}')


# Dashboard layout
app.layout = html.Div([
    html.Div([
        html.H1('🌿 IoT Monitoring Dashboard', style={'textAlign': 'center', 'marginBottom': '5px'}),
        html.P([
            'LilyGo T-Beam · AP3216 + HDC1080 + humedad de suelo + GPS · agente Gemini',
            # Sello de frescura de los datos (lo rellena el callback).
            html.Span(id='ultima-lectura', style={'marginLeft': '14px', 'color': '#6b6b76'}),
        ], style={'textAlign': 'center', 'marginTop': '0'}),
    ], className='dash-header'),

    # Alert banner
    html.Div(id='alert-banner'),

    # Nodo + Agente IA
    html.Div([
        html.H3('🔌 Nodo sensor', className='agent-title'),
        html.Div(id='node-panel'),
    ], className='dash-panel'),

    html.Div([
        html.H3('🤖 Agente IA del cultivo', className='agent-title'),
        html.Div(id='agent-panel'),
    ], className='dash-panel'),

    # KPI Cards
    html.Div(id='kpi-cards', className='dash-kpis'),

    # Charts
    html.Div([
        html.Div([dcc.Graph(id='temp-chart')], className='dash-chart'),
        html.Div([dcc.Graph(id='humidity-chart')], className='dash-chart'),
    ], style={'marginBottom': '20px'}),

    html.Div([
        html.Div([dcc.Graph(id='light-chart')], className='dash-chart'),
        html.Div([dcc.Graph(id='soil-chart')], className='dash-chart'),
    ], style={'marginBottom': '20px'}),

    # Data Table
    html.Div([
        html.H3('📋 Lecturas recientes', className='agent-title'),
        dash_table.DataTable(
            id='data-table',
            style_table={'overflowX': 'auto'},
            style_cell={'textAlign': 'center', 'padding': '10px', 'fontFamily': 'Arial',
                        'backgroundColor': '#0d0d0f', 'color': '#e8e8ec', 'border': '1px solid #24242b'},
            style_header={'backgroundColor': '#1c1c22', 'color': '#ffffff', 'fontWeight': 'bold',
                          'border': '1px solid #24242b'},
            style_data_conditional=[{'if': {'row_index': 'odd'}, 'backgroundColor': '#111116'}],
            page_size=15,
        ),
    ]),

    # Auto-refresh interval (10 seconds)
    dcc.Interval(id='interval-component', interval=10*1000, n_intervals=0),

], style={'maxWidth': '1400px', 'margin': '0 auto', 'padding': '20px', 'fontFamily': 'Arial, sans-serif'})


def create_time_series(df, column, title, color, unit):
    """Create a Plotly time series chart."""
    fig = go.Figure()
    if not df.empty:
        fig.add_trace(go.Scatter(
            x=df['time_index'], y=df[column],
            mode='lines+markers', name=title,
            line=dict(color=color, width=2),
            marker=dict(size=4),
            fill='tozeroy', fillcolor=f'rgba({int(color[1:3],16)},{int(color[3:5],16)},{int(color[5:7],16)},0.1)'
        ))
    fig.update_layout(
        title=dict(text=f'{title} ({unit})', font=dict(size=15, color='#e8e8ec')),
        xaxis_title='Tiempo', yaxis_title=unit,
        template=TEMPLATE, height=340,
        paper_bgcolor=PLOT_BG, plot_bgcolor=PLOT_BG,
        font=dict(color='#9a9aa5', size=FUENTE),
        margin=dict(l=55, r=20, t=45, b=40),
        legend=dict(font=dict(size=11), bgcolor='rgba(0,0,0,0)')
    )
    fig.update_xaxes(gridcolor='#24242b', zerolinecolor='#24242b', linecolor='#24242b')
    fig.update_yaxes(gridcolor='#24242b', zerolinecolor='#24242b', linecolor='#24242b')
    return fig


def create_kpi_card(title, value, unit, icon, color):
    """Create a KPI card."""
    texto_valor = "—" if value is None or pd.isna(value) else f"{value:.1f}"
    return html.Div([
        html.Div(icon, className='kpi-icon'),
        html.Div(title, className='kpi-title'),
        html.Div(texto_valor, className='kpi-value', style={'color': color}),
        html.Div(unit, className='kpi-unit'),
    ], className='kpi')


@app.callback(
    [Output('ultima-lectura', 'children'),
     Output('node-panel', 'children'),
     Output('alert-banner', 'children'),
     Output('agent-panel', 'children'),
     Output('kpi-cards', 'children'),
     Output('temp-chart', 'figure'),
     Output('humidity-chart', 'figure'),
     Output('light-chart', 'figure'),
     Output('soil-chart', 'figure'),
     Output('data-table', 'data'),
     Output('data-table', 'columns')],
    [Input('interval-component', 'n_intervals')]
)
def update_dashboard(n):
    df = get_sensor_data(100)
    ultimo, _historial = get_analisis_ia()

    # KPI values (latest reading)
    if not df.empty:
        latest = df.iloc[0]
        temp_val = latest.get('temperatura_aire')
        hum_val = latest.get('humedad_aire')
        luz_val = latest.get('luz_lux')
        suelo_val = latest.get('humedad_suelo_pct')
        seco = bool(latest.get('alerta_seco', False))
    else:
        temp_val = hum_val = luz_val = suelo_val = None
        seco = False

    # Antiguedad de la ultima lectura: sin esto el dashboard muestra datos
    # congelados como si fueran en vivo.
    minutos_sin_datos = None
    if not df.empty:
        ultima = df['time_index'].max().to_pydatetime()
        if ultima.tzinfo is None:
            ultima = ultima.replace(tzinfo=timezone.utc)
        minutos_sin_datos = (datetime.now(timezone.utc) - ultima).total_seconds() / 60.0
        minutos_sin_datos = round(minutos_sin_datos, 1)

    if minutos_sin_datos is not None and minutos_sin_datos > MINUTOS_CRITICO_SIN_DATOS:
        alert_banner = html.Div(
            f'⛔ SIN DATOS del sensor hace {minutos_sin_datos:.0f} min — el nodo está caído o desconectado',
            className='banner banner-critico')
    elif minutos_sin_datos is not None and minutos_sin_datos > MINUTOS_AVISO_SIN_DATOS:
        alert_banner = html.Div(
            f'⚠️ Última lectura hace {minutos_sin_datos:.0f} min — se esperaba una cada 15 s',
            className='banner banner-vacio')
    elif df.empty:
        alert_banner = html.Div('⚠️ Sin lecturas en la base de datos todavía',
                                className='banner banner-vacio')
    elif suelo_val is None or pd.isna(suelo_val):
        alert_banner = html.Div('⚠️ La última lectura no contiene humedad de suelo',
                                className='banner banner-vacio')
    elif seco:
        alert_banner = html.Div(
            f'🚨 ALERTA: suelo seco ({suelo_val:.0f}%) en {latest["dispositivo"]}',
            className='banner banner-critico')
    else:
        alert_banner = html.Div(
            f'✅ Suelo en rango normal ({suelo_val:.0f}%) · última lectura {latest["dispositivo"]}',
            className='banner banner-ok')

    kpi_cards = [
        create_kpi_card('Temp. Aire', temp_val, '°C', '🌡️', '#e74c3c'),
        create_kpi_card('Humedad Aire', hum_val, '%', '💧', '#3498db'),
        create_kpi_card('Luz Ambiente', luz_val, 'Lux', '☀️', '#f39c12'),
        create_kpi_card('Humedad Suelo', suelo_val, '%', '🌱', '#2ecc71'),
    ]

    # Sort ascending for charts
    df_sorted = df.sort_values('time_index') if not df.empty else df

    temp_fig = create_time_series(df_sorted, 'temperatura_aire', 'Temperature', '#e74c3c', '°C')
    hum_fig = create_time_series(df_sorted, 'humedad_aire', 'Humidity', '#3498db', '%')
    light_fig = create_time_series(df_sorted, 'luz_lux', 'Ambient Light', '#f39c12', 'Lux')
    soil_fig = create_time_series(df_sorted, 'humedad_suelo_pct', 'Soil Moisture', '#2ecc71', '%')

    # Table data
    table_df = df.head(50).copy() if not df.empty else df
    if not table_df.empty:
        table_df['time_index'] = table_df['time_index'].dt.strftime('%Y-%m-%d %H:%M:%S')
    table_data = table_df.to_dict('records') if not table_df.empty else []
    table_columns = [{'name': c.replace('_', ' ').title(), 'id': c} for c in COLUMNAS]

    if minutos_sin_datos is None:
        sello = ""
    elif minutos_sin_datos < 1:
        sello = "· datos en vivo"
    else:
        sello = f"· última lectura hace {minutos_sin_datos:.0f} min"

    return (sello, panel_nodo(get_nodos()), alert_banner, agente_panel(ultimo), kpi_cards, temp_fig, hum_fig,
            light_fig, soil_fig, table_data, table_columns)


@server.route('/')
def index():
    return '<html><head><meta http-equiv="refresh" content="0;url=/dashboard/"></head></html>'


@server.route('/health')
def health():
    return {
        "service": "frontend",
        "postgres": f"{PG_HOST}:{PG_PORT}/{PG_DB}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=80)
