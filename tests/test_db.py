"""Pruebas de backend/db.py y de las consultas de sql/consultas.sql."""

import sqlite3

import pytest

from backend.db import RUTA_CONSULTAS, BaseDatos, cargar_consultas


def poblar(db):
    """Datos de prueba con timestamps fijos."""
    for ts, t, h, l, s in [
        ("2026-11-22 10:00:00", 24.0, 60, 70, 45),
        ("2026-11-22 10:30:00", 28.0, 58, 75, 40),
        ("2026-11-22 11:00:00", 32.0, 55, 80, 25),
        ("2026-11-23 09:00:00", 26.0, 62, 20, 50),
    ]:
        db.insertar_telemetria({"temperatura": t, "humedad": h, "luz": l, "suelo": s}, ts)
    db.insertar_evento("RIEGO_INICIADO", "Suelo seco 25%", "INFO", "2026-11-22 11:00:02")
    db.insertar_evento("RIEGO_FINALIZADO", "Duración máxima", "INFO", "2026-11-22 11:00:12")
    db.insertar_evento("RIEGO_INICIADO", "Suelo seco 22%", "INFO", "2026-11-22 11:00:45")
    db.insertar_evento("CRITICO", "temperatura 39 C > 38 C", "CRITICO", "2026-11-22 12:00:00")
    db.insertar_evento("ERROR_SENSOR", "Fallo DHT22", "ALERTA", "2026-11-23 09:00:00")
    db.insertar_evento("RIEGO_INICIADO", "Suelo seco", "INFO", "2026-11-23 09:10:00")
    c1 = db.insertar_comando("modo", "MANUAL", "adafruit_io", timestamp="2026-11-22 12:00:00")
    db.insertar_comando("bomba", "ON", "adafruit_io", "IGNORADO_MODO_AUTO", "2026-11-22 12:01:00")
    db.actualizar_resultado_comando(c1, "OK")


def test_schema_crea_tablas_e_indices(db):
    con = db._con
    tablas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"lecturas", "eventos", "comandos"} <= tablas
    indices = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_lecturas_timestamp", "idx_lecturas_sensor_ts", "idx_eventos_tipo_ts",
            "idx_comandos_timestamp"} <= indices


def test_schema_es_idempotente(tmp_path):
    ruta = tmp_path / "x.db"
    BaseDatos(ruta).cerrar()
    db = BaseDatos(ruta)            # segunda vez no falla
    db.insertar_evento("INICIO_SISTEMA")
    assert len(db.eventos()) == 1
    db.cerrar()


def test_telemetria_una_fila_por_sensor(db):
    filas = db.insertar_telemetria({"temperatura": 24.1, "humedad": 60.2, "luz": 72,
                                    "suelo": 45, "ts": 1700000000})
    assert filas == 4
    lecturas = db.ultimas_lecturas(10)
    assert {l["sensor"] for l in lecturas} == {"temperatura", "humedad", "luz", "suelo"}
    unidades = {l["sensor"]: l["unidad"] for l in lecturas}
    assert unidades["temperatura"] == "C" and unidades["suelo"] == "%"
    assert all(l["timestamp"] for l in lecturas)


def test_telemetria_ignora_valores_invalidos(db):
    assert db.insertar_telemetria({"temperatura": None, "humedad": "x", "luz": True,
                                   "suelo": 10}) == 1


def test_severidad_invalida_se_normaliza(db):
    db.insertar_evento("X", "y", "RARA")
    assert db.eventos()[0]["severidad"] == "INFO"


def test_check_de_severidad_en_schema(db):
    with pytest.raises(sqlite3.IntegrityError):
        db._con.execute("INSERT INTO eventos (tipo, severidad) VALUES ('X', 'MALA')")


def test_comando_y_ack(db):
    id_cmd = db.insertar_comando("bomba", "ON", "adafruit_io")
    assert db.ultimos_comandos(1)[0]["resultado"] == "ENVIADO"
    assert db.actualizar_resultado_comando(id_cmd, "OK")
    assert db.ultimos_comandos(1)[0]["resultado"] == "OK"
    assert not db.actualizar_resultado_comando(9999, "OK")


# ------------------------------------------------------------ consultas.sql
def test_consultas_sql_se_cargan_todas():
    consultas = cargar_consultas(RUTA_CONSULTAS)
    assert set(consultas) == {
        "ultimas_lecturas", "estadisticas_sensor", "promedio_por_hora", "eventos_filtrados",
        "conteo_eventos", "riegos_del_dia", "total_riegos_del_dia", "ultimos_comandos",
        "resumen_comandos"}
    assert all(sql.upper().startswith("SELECT") for sql in consultas.values())


def test_ultimas_lecturas_orden_y_filtro(db):
    poblar(db)
    ultimas = db.ultimas_lecturas(4)
    assert len(ultimas) == 4
    assert all(l["timestamp"] == "2026-11-23 09:00:00" for l in ultimas)
    temps = db.ultimas_lecturas(10, "temperatura")
    assert [t["valor"] for t in temps] == [26.0, 32.0, 28.0, 24.0]


def test_estadisticas_por_rango(db):
    poblar(db)
    filas = db.estadisticas("2026-11-22 00:00:00", "2026-11-22 23:59:59")
    temp = next(f for f in filas if f["sensor"] == "temperatura")
    assert temp["muestras"] == 3
    assert temp["promedio"] == 28.0
    assert temp["minimo"] == 24.0 and temp["maximo"] == 32.0
    solo_suelo = db.estadisticas("2026-11-22 00:00:00", "2026-11-23 23:59:59", "suelo")
    assert len(solo_suelo) == 1 and solo_suelo[0]["muestras"] == 4


def test_promedio_por_hora(db):
    poblar(db)
    filas = db.promedio_por_hora("2026-11-22 00:00:00", "2026-11-22 23:59:59", "temperatura")
    assert [(f["hora"], f["promedio"]) for f in filas] == [
        ("2026-11-22 10:00", 26.0), ("2026-11-22 11:00", 32.0)]


def test_eventos_por_tipo_y_severidad(db):
    poblar(db)
    assert len(db.eventos(tipo="RIEGO_INICIADO")) == 3
    criticos = db.eventos(severidad="CRITICO")
    assert len(criticos) == 1 and criticos[0]["tipo"] == "CRITICO"
    assert len(db.eventos(n=2)) == 2
    conteo = db.conteo_eventos("2026-11-22 00:00:00", "2026-11-22 23:59:59")
    assert {(c["tipo"], c["cantidad"]) for c in conteo} == {
        ("RIEGO_INICIADO", 2), ("RIEGO_FINALIZADO", 1), ("CRITICO", 1)}


def test_riegos_del_dia(db):
    poblar(db)
    total, filas = db.riegos_del_dia("2026-11-22")
    assert total == 2
    assert [f["tipo"] for f in filas] == ["RIEGO_INICIADO", "RIEGO_FINALIZADO", "RIEGO_INICIADO"]
    assert db.riegos_del_dia("2026-11-23")[0] == 1
    assert db.riegos_del_dia("2026-01-01") == (0, [])


def test_ultimos_comandos_y_resumen(db):
    poblar(db)
    ultimos = db.ultimos_comandos(5)
    assert [c["comando"] for c in ultimos] == ["bomba", "modo"]
    assert ultimos[1]["resultado"] == "OK"
    resumen = {(r["comando"], r["resultado"]): r["cantidad"] for r in db.resumen_comandos()}
    assert resumen == {("bomba", "IGNORADO_MODO_AUTO"): 1, ("modo", "OK"): 1}
