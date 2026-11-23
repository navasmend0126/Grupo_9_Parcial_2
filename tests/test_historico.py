"""Pruebas de la CLI de históricos (backend/historico.py)."""

import argparse

import pytest

from backend import historico
from backend.db import BaseDatos


def test_formatear_tabla():
    tabla = historico.formatear_tabla([{"a": 1, "b": "xyz"}, {"a": 22.5, "b": None}])
    lineas = tabla.splitlines()
    assert lineas[1] == "| a    | b   |"
    assert "| 22.5 | -   |" in lineas
    assert lineas[-1] == "2 fila(s)"
    assert historico.formatear_tabla([]) == "(sin resultados)"


def test_normalizar_fecha():
    assert historico.normalizar_fecha("2026-11-23") == "2026-11-23 00:00:00"
    assert historico.normalizar_fecha("2026-11-23", fin_de_dia=True) == "2026-11-23 23:59:59"
    assert historico.normalizar_fecha("2026-11-23 08:30") == "2026-11-23 08:30:00"
    with pytest.raises(argparse.ArgumentTypeError):
        historico.normalizar_fecha("23/11/2026")


@pytest.fixture
def ruta_db(tmp_path):
    ruta = tmp_path / "hist.db"
    db = BaseDatos(ruta)
    db.insertar_telemetria({"temperatura": 25, "humedad": 50, "luz": 60, "suelo": 40},
                           "2026-11-23 10:00:00")
    db.insertar_evento("RIEGO_INICIADO", "Suelo seco", "INFO", "2026-11-23 10:00:01")
    db.insertar_comando("modo", "MANUAL", "adafruit_io", "OK", "2026-11-23 10:05:00")
    db.cerrar()
    return ruta


@pytest.mark.parametrize("argumentos,texto", [
    (["lecturas", "-n", "5"], "temperatura"),
    (["lecturas", "--sensor", "suelo"], "| suelo"),
    (["estadisticas", "--desde", "2026-11-23", "--hasta", "2026-11-23"], "promedio"),
    (["estadisticas", "--desde", "2026-11-23", "--hasta", "2026-11-23", "--por-hora"],
     "2026-11-23 10:00"),
    (["eventos", "--tipo", "riego_iniciado"], "RIEGO_INICIADO"),
    (["eventos", "--resumen", "--desde", "2026-11-23", "--hasta", "2026-11-23"], "cantidad"),
    (["riegos", "--fecha", "2026-11-23"], "1 riego(s)"),
    (["comandos"], "MANUAL"),
    (["comandos", "--resumen"], "OK"),
])
def test_cli(ruta_db, capsys, argumentos, texto):
    assert historico.main(["--db", str(ruta_db), *argumentos]) == 0
    assert texto in capsys.readouterr().out


def test_cli_sin_bd(tmp_path, capsys):
    assert historico.main(["--db", str(tmp_path / "no.db"), "lecturas"]) == 1
