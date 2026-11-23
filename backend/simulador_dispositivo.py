"""
simulador_dispositivo.py — Imita al ESP32 para probar el backend sin Wokwi.

Publica en HiveMQ exactamente lo mismo que el dispositivo real (telemetría,
estado, eventos y ACKs) y obedece los comandos de {prefijo}/comandos.

Lo interesante: NO reimplementa la lógica. Importa `wokwi/controlador.py`, el
MISMO módulo que corre en el ESP32, y le inyecta sensores y actuadores
simulados. Así se demuestra que el controlador es independiente del hardware.

Uso (desde la raíz del proyecto, con el backend corriendo en otra terminal):
    python -m backend.simulador_dispositivo
    python -m backend.simulador_dispositivo --escenario calor --telemetria 5
    python -m backend.simulador_dispositivo --escenario sequia

Escenarios:
    normal  -> valores estables alrededor de 26 °C, suelo 50 %
    calor   -> la temperatura sube hasta superar 38 °C (evento CRITICO)
    sequia  -> el suelo se seca rápido (riegos y luego CRITICO por suelo < 10 %)
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import datetime

from backend.config import RAIZ_PROYECTO, cargar_config
from backend.mqtt_bridge import PuenteMQTT

# Reutilizamos el controlador del dispositivo (wokwi/controlador.py).
sys.path.insert(0, str(RAIZ_PROYECTO / "wokwi"))
from controlador import Controlador, Parametros  # noqa: E402

log = logging.getLogger("simulador")


# ----------------------------------------------------------------------
# Física simplificada del invernadero
# ----------------------------------------------------------------------
class Ambiente:
    """Estado físico simulado. Los actuadores influyen en los sensores:
    la bomba humedece el suelo y el ventilador enfría el aire."""

    ESCENARIOS = {
        #          temp, hum, luz, suelo, deriva_temp, deriva_suelo
        "normal": (26.0, 60.0, 65.0, 50.0, 0.0, -0.3),
        "calor":  (33.0, 45.0, 85.0, 45.0, 0.6, -0.3),
        "sequia": (27.0, 40.0, 70.0, 32.0, 0.0, -2.0),
    }

    def __init__(self, escenario: str = "normal"):
        t, h, l, s, self.deriva_temp, self.deriva_suelo = self.ESCENARIOS[escenario]
        self.temperatura, self.humedad, self.luz, self.suelo = t, h, l, s
        self.bomba = None       # se enlazan después de crear los actuadores
        self.ventilador = None

    def evolucionar(self) -> None:
        """Avanza un paso (se llama en cada lectura, cada 2 s)."""
        self.temperatura += self.deriva_temp + random.uniform(-0.2, 0.2)
        if self.ventilador and self.ventilador.encendido:
            self.temperatura -= 0.5
        self.suelo += self.deriva_suelo + random.uniform(-0.3, 0.3)
        if self.bomba and self.bomba.encendido:
            self.suelo += 1.5
        self.humedad += random.uniform(-0.5, 0.5)
        self.luz += random.uniform(-2, 2)
        self.temperatura = min(50.0, max(5.0, self.temperatura))
        self.suelo = min(100.0, max(0.0, self.suelo))
        self.humedad = min(100.0, max(0.0, self.humedad))
        self.luz = min(100.0, max(0.0, self.luz))


class SensorDHTSimulado:
    def __init__(self, amb: Ambiente):
        self.amb = amb

    def leer(self):
        self.amb.evolucionar()
        return round(self.amb.temperatura, 1), round(self.amb.humedad, 1)


class SensorSimulado:
    def __init__(self, amb: Ambiente, campo: str):
        self.amb, self.campo = amb, campo

    def leer(self):
        return int(round(getattr(self.amb, self.campo)))


class SalidaSimulada:
    """Relé/LED simulado: solo guarda el estado y lo registra."""

    def __init__(self, nombre: str):
        self.nombre = nombre
        self.encendido = False

    def establecer(self, encendido: bool) -> None:
        if encendido != self.encendido:
            log.info("[HW] %s -> %s", self.nombre, "ON" if encendido else "OFF")
        self.encendido = encendido


class BuzzerSimulado:
    def __init__(self):
        self.activo = False

    def activar_alarma(self):
        self.activo = True
        log.warning("[HW] BUZZER: alarma activada")

    def desactivar(self):
        if self.activo:
            log.info("[HW] BUZZER: alarma desactivada")
        self.activo = False

    def actualizar(self, ahora):
        pass


# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Simulador del ESP32 del invernadero")
    parser.add_argument("--escenario", choices=sorted(Ambiente.ESCENARIOS), default="normal")
    parser.add_argument("--telemetria", type=float, default=15.0,
                        help="segundos entre mensajes de telemetría (15)")
    parser.add_argument("--duracion", type=float, default=0,
                        help="segundos a ejecutar (0 = hasta Ctrl+C)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    cfg = cargar_config()

    amb = Ambiente(args.escenario)
    bomba, ventilador = SalidaSimulada("Bomba"), SalidaSimulada("Ventilador")
    amb.bomba, amb.ventilador = bomba, ventilador
    ctrl = Controlador(
        sensor_dht=SensorDHTSimulado(amb),
        sensor_luz=SensorSimulado(amb, "luz"),
        sensor_suelo=SensorSimulado(amb, "suelo"),
        bomba=bomba, ventilador=ventilador,
        luz_cultivo=SalidaSimulada("Luz cultivo"),
        buzzer=BuzzerSimulado(),
        params=Parametros(),
    )

    # El simulador se suscribe a los COMANDOS (lo contrario que el backend).
    puente = PuenteMQTT(cfg, client_id_base="invernadero-simulador-",
                        suscripciones={"comandos": "comando"})

    def al_recibir_comando(datos):
        resultado = ctrl.manejar_comando(datos.get("cmd"), datos.get("valor"),
                                         datos.get("origen", "desconocido"))
        ack = {"cmd": datos.get("cmd"), "valor": datos.get("valor"),
               "resultado": resultado, "origen": datos.get("origen", "desconocido")}
        if "id" in datos:
            ack["id"] = datos["id"]
        log.info("Comando %s=%s -> %s", ack["cmd"], ack["valor"], resultado)
        puente.publicar("comandos/ack", ack)

    puente.on("comando", al_recibir_comando)
    ctrl.bus.on("evento", lambda ev: (log.info("Evento %s: %s", ev["tipo"], ev["detalle"]),
                                      puente.publicar("eventos", ev)))
    ctrl.bus.on("estado", lambda est: puente.publicar("estado", est))

    puente.conectar()
    for _ in range(300):                      # espera hasta 30 s la conexión
        if puente.conectado:
            break
        time.sleep(0.1)
    if not puente.conectado:
        log.error("No se pudo conectar a %s", cfg.mqtt_broker)
        return
    log.info("Simulador conectado (escenario %s, prefijo %s). Ctrl+C para salir.",
             args.escenario, cfg.mqtt_prefijo)

    ctrl.iniciar()
    inicio = ultimo_sensores = ultimo_telemetria = time.monotonic()
    ctrl.ciclo_sensores()
    try:
        while True:
            ahora = time.monotonic()
            if args.duracion and ahora - inicio >= args.duracion:
                break
            if ahora - ultimo_sensores >= 2:
                ultimo_sensores = ahora
                ctrl.ciclo_sensores()
            if ahora - ultimo_telemetria >= args.telemetria:
                ultimo_telemetria = ahora
                datos = ctrl.telemetria(int(datetime.now().timestamp()))
                puente.publicar("telemetria", datos)
                log.info("Telemetría: %s", datos)
            ctrl.tick()
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        puente.desconectar()
        log.info("Simulador detenido")


if __name__ == "__main__":
    main()
