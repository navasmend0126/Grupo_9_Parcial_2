"""Pruebas de la traducción de comandos de Adafruit IO, el texto del feed
`estado` y el limitador de tasa (sin conectarse a la nube)."""

import pytest

from backend.nube import LimitadorTasa, formatear_estado, traducir_comando


@pytest.mark.parametrize("feed,valor,esperado", [
    ("modo", "MANUAL", ("modo", "MANUAL")),
    ("modo", " auto ", ("modo", "AUTO")),
    ("bomba", "ON", ("bomba", "ON")),
    ("bomba", "1", ("bomba", "ON")),
    ("ventilador", "0", ("ventilador", "OFF")),
    ("ventilador", "false", ("ventilador", "OFF")),
    ("umbral-temp", "32", ("umbral_temp", 32)),
    ("umbral-temp", "32.5", ("umbral_temp", 32.5)),
    ("umbral-temp", "abc", ("umbral_temp", "abc")),   # el ESP32 responderá INVALIDO
    ("bomba", "QUIZAS", ("bomba", "QUIZAS")),
])
def test_traducir_comando(feed, valor, esperado):
    assert traducir_comando(feed, valor) == esperado


@pytest.mark.parametrize("feed", ["temperatura", "estado", "otro"])
def test_feeds_que_no_son_comandos(feed):
    assert traducir_comando(feed, "ON") is None


def test_formatear_estado():
    estado = {"modo": "AUTO", "bomba": False, "ventilador": True, "luz_cultivo": False,
              "umbral_temp": 30, "critico": False}
    assert formatear_estado(estado) == "AUTO | Bomba OFF | Vent ON | Luz OFF | Umbral 30C"
    estado["critico"] = True
    assert formatear_estado(estado).endswith("| CRITICO")


class RelojFalso:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_limitador_respeta_maximo_por_minuto():
    reloj = RelojFalso()
    lim = LimitadorTasa(5, 60, reloj)
    assert all(lim.permitir() for _ in range(5))
    assert not lim.permitir()
    assert lim.disponibles() == 0
    reloj.t = 59.9
    assert not lim.permitir()
    reloj.t = 60.0             # los primeros envíos salen de la ventana
    assert lim.disponibles() == 5
    assert lim.permitir(4)
    assert not lim.permitir(2)


def test_telemetria_cada_15s_cabe_en_el_limite():
    """4 feeds cada 15 s = 16 datos/min < 25 (margen bajo el límite de 30)."""
    reloj = RelojFalso()
    lim = LimitadorTasa(25, 60, reloj)
    for paso in range(20):                 # 5 minutos
        reloj.t = paso * 15
        assert lim.permitir(4)
    assert lim.disponibles() >= 9          # queda cupo para el feed `estado`
