"""Pruebas de integración del backend (backend/main.py) con puente y nube falsos:
telemetría -> SQLite + nube, comandos nube -> dispositivo, ACK -> resultado."""

from backend.config import Config
from backend.main import ServicioInvernadero
from backend.mqtt_bridge import construir_comando


class PuenteFalso:
    def __init__(self, conectado=True):
        self.manejadores = {}
        self.enviados = []
        self.conectado = conectado

    def on(self, tipo, fn):
        self.manejadores.setdefault(tipo, []).append(fn)

    def emitir(self, tipo, datos):
        for fn in self.manejadores[tipo]:
            fn(datos)

    def enviar_comando(self, cmd, valor, origen, id_comando=None):
        self.enviados.append(construir_comando(cmd, valor, origen, id_comando))
        return self.conectado


class NubeFalsa:
    def __init__(self):
        self.telemetria, self.estados = [], []

    def publicar_telemetria(self, datos):
        self.telemetria.append(datos)

    def publicar_estado(self, estado):
        self.estados.append(estado)


def crear(db, conectado=True):
    puente, nube = PuenteFalso(conectado), NubeFalsa()
    servicio = ServicioInvernadero(Config(), db, puente, nube)
    return servicio, puente, nube


def test_telemetria_se_guarda_y_sube_a_la_nube(db):
    _, puente, nube = crear(db)
    datos = {"temperatura": 24.1, "humedad": 60.2, "luz": 72, "suelo": 45, "ts": 1}
    puente.emitir("telemetria", datos)
    assert len(db.ultimas_lecturas(10)) == 4
    assert nube.telemetria == [datos]


def test_estado_y_eventos(db):
    servicio, puente, nube = crear(db)
    estado = {"modo": "AUTO", "bomba": False, "ventilador": True, "luz_cultivo": False,
              "umbral_temp": 30}
    puente.emitir("estado", estado)
    assert servicio.ultimo_estado == estado and nube.estados == [estado]
    puente.emitir("evento", {"tipo": "CRITICO", "detalle": "t>38", "severidad": "CRITICO"})
    assert db.eventos(severidad="CRITICO")[0]["detalle"] == "t>38"


def test_flujo_comando_nube_ack(db):
    servicio, puente, _ = crear(db)
    id_cmd = servicio.enviar_comando("bomba", "ON", "adafruit_io")
    assert puente.enviados == [{"cmd": "bomba", "valor": "ON", "origen": "adafruit_io",
                                "id": id_cmd}]
    assert db.ultimos_comandos(1)[0]["resultado"] == "ENVIADO"
    puente.emitir("ack", {"cmd": "bomba", "valor": "ON", "resultado": "IGNORADO_MODO_AUTO",
                          "origen": "adafruit_io", "id": id_cmd})
    comandos = db.ultimos_comandos(5)
    assert len(comandos) == 1 and comandos[0]["resultado"] == "IGNORADO_MODO_AUTO"


def test_ack_sin_id_se_registra_igual(db):
    _, puente, _ = crear(db)
    puente.emitir("ack", {"cmd": "modo", "valor": "MANUAL", "resultado": "OK",
                          "origen": "hivemq_web"})
    fila = db.ultimos_comandos(1)[0]
    assert (fila["comando"], fila["origen"], fila["resultado"]) == ("modo", "hivemq_web", "OK")


def test_error_de_envio_queda_registrado(db):
    servicio, _, _ = crear(db, conectado=False)
    servicio.enviar_comando("modo", "MANUAL")
    assert db.ultimos_comandos(1)[0]["resultado"] == "ERROR_ENVIO"
