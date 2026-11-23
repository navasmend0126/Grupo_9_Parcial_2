"""Pruebas de la lógica del dispositivo (wokwi/controlador.py) sin hardware.

Se inyectan sensores, actuadores y un reloj falsos: así se verifica la lógica
AUTO/MANUAL, la histéresis, el límite de riego, la condición crítica y los
comandos sin necesitar el ESP32 ni el módulo `machine`.
"""

import pytest

from controlador import (MODO_AUTO, MODO_MANUAL, RESULTADO_IGNORADO, RESULTADO_INVALIDO,
                         RESULTADO_OK, Controlador, Parametros, normalizar_onoff)


class Reloj:
    """Reloj controlable (milisegundos)."""

    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms

    def avanzar(self, ms):
        self.ms += ms


class DHTFalso:
    def __init__(self, temp=25.0, hum=60.0):
        self.temp, self.hum, self.falla = temp, hum, False

    def leer(self):
        return None if self.falla else (self.temp, self.hum)


class AnalogFalso:
    def __init__(self, valor):
        self.valor = valor

    def leer(self):
        return self.valor


class SalidaFalsa:
    def __init__(self):
        self.encendido = False

    def establecer(self, encendido):
        self.encendido = bool(encendido)


class BuzzerFalso:
    def __init__(self):
        self.activo = False
        self.actualizaciones = 0

    def activar_alarma(self):
        self.activo = True

    def desactivar(self):
        self.activo = False

    def actualizar(self, ahora):
        self.actualizaciones += 1


class Sistema:
    """Arma un controlador con todo falso y captura eventos/estados emitidos."""

    def __init__(self, temp=25.0, luz=70, suelo=50):
        self.reloj = Reloj()
        self.dht = DHTFalso(temp)
        self.luz = AnalogFalso(luz)
        self.suelo = AnalogFalso(suelo)
        self.bomba, self.vent, self.luz_cultivo = SalidaFalsa(), SalidaFalsa(), SalidaFalsa()
        self.buzzer = BuzzerFalso()
        self.ctrl = Controlador(self.dht, self.luz, self.suelo, self.bomba, self.vent,
                                self.luz_cultivo, self.buzzer, params=Parametros(),
                                reloj=self.reloj)
        self.eventos, self.estados = [], []
        self.ctrl.bus.on("evento", self.eventos.append)
        self.ctrl.bus.on("estado", self.estados.append)
        self.ctrl.iniciar()

    def tipos(self):
        return [e["tipo"] for e in self.eventos]

    def ciclo(self, ms=2000):
        """Simula el paso del tiempo + un ciclo de sensores."""
        self.reloj.avanzar(ms)
        self.ctrl.tick()
        self.ctrl.ciclo_sensores()


@pytest.fixture
def s():
    return Sistema()


# ---------------------------------------------------------------- arranque
def test_inicio_emite_evento_y_estado(s):
    assert s.tipos() == ["INICIO_SISTEMA"]
    assert s.estados[-1]["modo"] == MODO_AUTO
    assert s.estados[-1]["umbral_temp"] == 30
    assert not any([s.bomba.encendido, s.vent.encendido, s.luz_cultivo.encendido])


def test_telemetria_contiene_lecturas(s):
    s.ciclo()
    datos = s.ctrl.telemetria(ts=123)
    assert datos == {"temperatura": 25.0, "humedad": 60.0, "luz": 70, "suelo": 50, "ts": 123}


# ---------------------------------------------------------------- ventilador
def test_ventilador_con_histeresis(s):
    s.dht.temp = 31
    s.ciclo()
    assert s.vent.encendido and "VENTILADOR_ON" in s.tipos()
    s.dht.temp = 29            # entre 28 y 30: no cambia (histéresis)
    s.ciclo()
    assert s.vent.encendido
    s.dht.temp = 27.9          # < 30 - 2
    s.ciclo()
    assert not s.vent.encendido and "VENTILADOR_OFF" in s.tipos()


def test_ventilador_no_repite_eventos(s):
    s.dht.temp = 33
    for _ in range(5):
        s.ciclo()
    assert s.tipos().count("VENTILADOR_ON") == 1


# ---------------------------------------------------------------- riego
def test_riego_maximo_10s_y_enfriamiento_30s(s):
    s.suelo.valor = 20
    s.ciclo()
    assert s.bomba.encendido and s.tipos().count("RIEGO_INICIADO") == 1

    s.reloj.avanzar(10000)     # 10 s después el tick la apaga
    s.ctrl.tick()
    assert not s.bomba.encendido
    assert s.tipos().count("RIEGO_FINALIZADO") == 1

    s.ciclo(2000)              # suelo sigue seco pero está en enfriamiento
    assert not s.bomba.encendido
    s.ciclo(27000)             # total 29 s: aún no
    assert not s.bomba.encendido
    s.ciclo(1000)              # 30 s: vuelve a regar
    assert s.bomba.encendido and s.tipos().count("RIEGO_INICIADO") == 2


def test_riego_se_detiene_si_el_suelo_se_recupera(s):
    s.suelo.valor = 25
    s.ciclo()
    assert s.bomba.encendido
    s.suelo.valor = 35
    s.ciclo()
    assert not s.bomba.encendido
    assert "Suelo recuperado" in s.eventos[-1]["detalle"]


# ---------------------------------------------------------------- luz
def test_luz_cultivo_automatica_con_histeresis(s):
    s.luz.valor = 20
    s.ciclo()
    assert s.luz_cultivo.encendido and "LUZ_CULTIVO_ON" in s.tipos()
    s.luz.valor = 32           # entre 30 y 35: se mantiene
    s.ciclo()
    assert s.luz_cultivo.encendido
    s.luz.valor = 40
    s.ciclo()
    assert not s.luz_cultivo.encendido


def test_luz_cultivo_sigue_automatica_en_manual(s):
    s.ctrl.manejar_comando("modo", "MANUAL")
    s.luz.valor = 10
    s.ciclo()
    assert s.luz_cultivo.encendido


# ---------------------------------------------------------------- crítico
def test_critico_una_sola_vez_y_resuelto(s):
    s.dht.temp = 39
    for _ in range(4):
        s.ciclo()
    assert s.tipos().count("CRITICO") == 1
    assert s.buzzer.activo
    critico = [e for e in s.eventos if e["tipo"] == "CRITICO"][0]
    assert critico["severidad"] == "CRITICO"
    assert s.estados[-1]["critico"] is True

    s.dht.temp = 30
    s.ciclo()
    assert "CRITICO_RESUELTO" in s.tipos()
    assert not s.buzzer.activo


def test_critico_por_suelo(s):
    s.suelo.valor = 5
    s.ciclo()
    assert "CRITICO" in s.tipos()
    assert "suelo" in [e for e in s.eventos if e["tipo"] == "CRITICO"][0]["detalle"]


def test_tick_actualiza_buzzer(s):
    s.ctrl.tick()
    assert s.buzzer.actualizaciones == 1


# ---------------------------------------------------------------- comandos
def test_comandos_actuadores_ignorados_en_auto(s):
    assert s.ctrl.manejar_comando("bomba", "ON") == RESULTADO_IGNORADO
    assert s.ctrl.manejar_comando("ventilador", "ON") == RESULTADO_IGNORADO
    assert not s.bomba.encendido and not s.vent.encendido


def test_modo_manual_obedece_comandos(s):
    assert s.ctrl.manejar_comando("modo", "manual", "adafruit_io") == RESULTADO_OK
    assert s.ctrl.modo == MODO_MANUAL
    assert "CAMBIO_MODO" in s.tipos()
    assert s.ctrl.manejar_comando("bomba", "ON") == RESULTADO_OK
    assert s.bomba.encendido
    assert s.ctrl.manejar_comando("ventilador", "1") == RESULTADO_OK
    assert s.vent.encendido
    # En MANUAL las reglas automáticas no tocan bomba/ventilador
    s.dht.temp = 20
    s.suelo.valor = 90
    s.ciclo(15000)
    assert s.bomba.encendido and s.vent.encendido
    assert s.ctrl.manejar_comando("bomba", "OFF") == RESULTADO_OK
    assert not s.bomba.encendido


def test_volver_a_auto_aplica_limite_de_riego(s):
    s.ctrl.manejar_comando("modo", "MANUAL")
    s.ctrl.manejar_comando("bomba", "ON")
    s.suelo.valor = 20                 # sigue seco
    s.ciclo()
    s.ctrl.manejar_comando("modo", "AUTO")
    assert s.bomba.encendido
    s.reloj.avanzar(10000)
    s.ctrl.tick()
    assert not s.bomba.encendido


def test_umbral_temp_valido_e_invalido(s):
    assert s.ctrl.manejar_comando("umbral_temp", "35") == RESULTADO_OK
    assert s.ctrl.umbral_temp == 35
    assert "UMBRAL_ACTUALIZADO" in s.tipos()
    assert s.ctrl.manejar_comando("umbral_temp", 32.5) == RESULTADO_OK
    assert s.ctrl.umbral_temp == 32.5
    for malo in ("19", "46", "abc", None):
        assert s.ctrl.manejar_comando("umbral_temp", malo) == RESULTADO_INVALIDO
    assert s.ctrl.umbral_temp == 32.5


def test_umbral_temp_aplica_en_manual_y_reevalua_en_auto(s):
    s.dht.temp = 33
    s.ciclo()
    assert s.vent.encendido               # 33 > 30
    s.ctrl.manejar_comando("umbral_temp", 40)
    assert not s.vent.encendido           # 33 < 40 - 2 -> se apaga al instante
    s.ctrl.manejar_comando("modo", "MANUAL")
    assert s.ctrl.manejar_comando("umbral_temp", 25) == RESULTADO_OK
    assert s.ctrl.umbral_temp == 25


def test_comandos_invalidos(s):
    assert s.ctrl.manejar_comando("modo", "TURBO") == RESULTADO_INVALIDO
    assert s.ctrl.manejar_comando("bomba", "QUIZAS") == RESULTADO_INVALIDO
    assert s.ctrl.manejar_comando("desconocido", "ON") == RESULTADO_INVALIDO


def test_mismo_modo_no_genera_evento(s):
    s.ctrl.manejar_comando("modo", "AUTO")
    assert "CAMBIO_MODO" not in s.tipos()


# ---------------------------------------------------------------- robustez
def test_falla_dht_conserva_ultimo_valor_y_alerta_una_vez(s):
    s.ciclo()
    s.dht.falla = True
    s.ciclo()
    s.ciclo()
    assert s.ctrl.lectura["temperatura"] == 25.0
    alertas = [e for e in s.eventos if e["tipo"] == "ERROR_SENSOR"]
    assert len(alertas) == 1 and alertas[0]["severidad"] == "ALERTA"


def test_suscriptor_con_error_no_rompe_el_bus(s):
    def roto(_):
        raise RuntimeError("fallo")
    s.ctrl.bus.on("evento", roto)
    s.dht.temp = 31
    s.ciclo()
    assert "VENTILADOR_ON" in s.tipos()


@pytest.mark.parametrize("valor,esperado", [
    ("ON", True), ("on", True), ("1", True), ("true", True),
    ("OFF", False), ("0", False), (" off ", False), ("x", None), (None, None),
])
def test_normalizar_onoff(valor, esperado):
    assert normalizar_onoff(valor) is esperado
