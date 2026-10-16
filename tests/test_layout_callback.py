"""Comprueba que el dashboard de Dash sea coherente.

Bug que motivó este test: el callback declaraba un Output ('ultima-lectura')
que no existía en el layout. Dash aborta el callback ENTERO al arrancar, la
página se queda en blanco y no hay ningún error visible en el servidor: solo
aparece en la consola del navegador. Con curl el callback "funcionaba" porque
uno manda a mano la respuesta, así que el fallo se escapaba.
"""
import re
import sys
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SRC = (RAIZ / "services/frontend/app.py").read_text()


def ids_del_layout():
    bloque = SRC[SRC.index("app.layout = html.Div(["):SRC.index("def create_time_series")]
    return set(re.findall(r"id='([^']+)'", bloque))


def outputs_del_callback():
    bloque = SRC[SRC.index("@app.callback("):]
    return re.findall(r"Output\('([^']+)'", bloque[:bloque.index("Input(")])


def test_todo_output_existe_en_el_layout():
    faltan = [o for o in outputs_del_callback() if o not in ids_del_layout()]
    assert not faltan, f"Outputs sin elemento en el layout: {faltan}"


def test_el_layout_no_usa_colores_del_tema_claro():
    """Los colores van en assets/dark.css; si vuelven al codigo, el tema oscuro
    se rompe en silencio."""
    cabecera = SRC[:SRC.index("@app.callback(")]
    pendientes = re.findall(r"'(#[0-9a-fA-F]{6})'", cabecera)
    claro = [c for c in pendientes if c.lower() in ("#2c3e50", "#7f8c8d", "#ecf0f1")]
    assert not claro, f"colores del tema claro dentro del layout: {claro}"


if __name__ == "__main__":
    fallos = 0
    for nombre, fn in sorted(globals().items()):
        if nombre.startswith("test_"):
            try:
                fn()
                print(f"  OK   {nombre}")
            except AssertionError as e:
                print(f"  FALLA {nombre}: {e}")
                fallos += 1
    sys.exit(1 if fallos else 0)
