"""
main.py — Servicio backend del Invernadero IoT.

Ejecutar desde la raíz del proyecto:
    python -m backend.main

Flujo (todo dirigido por eventos MQTT):

    ESP32 --(HiveMQ)--> PuenteMQTT --> ServicioInvernadero --> SQLite
                                                        └----> Adafruit IO (dashboard)

    Adafruit IO (feed de comando) --> NubeAdafruit --> ServicioInvernadero
        --> SQLite (comando ENVIADO) --> PuenteMQTT --(HiveMQ)--> ESP32
        <-- ACK del ESP32 --> SQLite (resultado OK / IGNORADO_MODO_AUTO / INVALIDO)
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
import warnings
from logging.handlers import RotatingFileHandler
from typing import Any

from backend.config import UNIDADES, Config, cargar_config
from backend.db import BaseDatos
from backend.mqtt_bridge import PuenteMQTT

log = logging.getLogger("backend")


def configurar_logging(cfg: Config, nivel: int = logging.INFO) -> None:
    """Logging a consola y a logs/backend.log (rotativo, 1 MB x 3)."""
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    formato = logging.Formatter("%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                                "%Y-%m-%d %H:%M:%S")
    consola = logging.StreamHandler()
    consola.setFormatter(formato)
    archivo = RotatingFileHandler(cfg.log_path, maxBytes=1_000_000, backupCount=3,
                                  encoding="utf-8")
    archivo.setFormatter(formato)
    raiz = logging.getLogger()
    raiz.handlers.clear()
    raiz.setLevel(nivel)
    raiz.addHandler(consola)
    raiz.addHandler(archivo)
    # La librería adafruit-io usa la API v1 de paho (aviso de deprecación inofensivo).
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="Adafruit_IO")


class ServicioInvernadero:
    """Orquesta los componentes del backend y reacciona a sus eventos.

    Args:
        cfg: configuración.
        db: base de datos.
        puente: cliente MQTT hacia HiveMQ.
        nube: cliente de Adafruit IO (None = modo solo-local, sin nube).
    """

    def __init__(self, cfg: Config, db: BaseDatos, puente: PuenteMQTT, nube=None):
        self.cfg = cfg
        self.db = db
        self.puente = puente
        self.nube = nube
        self.ultimo_estado: dict[str, Any] | None = None
        # Suscripción a los eventos del dispositivo
        puente.on("telemetria", self.al_recibir_telemetria)
        puente.on("estado", self.al_recibir_estado)
        puente.on("evento", self.al_recibir_evento)
        puente.on("ack", self.al_recibir_ack)

    # ------------------------------------------------------------------
    # Manejadores de mensajes del dispositivo
    # ------------------------------------------------------------------
    def al_recibir_telemetria(self, datos: dict[str, Any]) -> None:
        """Guarda la telemetría (1 fila por sensor) y la reenvía a la nube."""
        filas = self.db.insertar_telemetria(datos)
        resumen = ", ".join(f"{s}={datos.get(s)}{UNIDADES[s]}" for s in UNIDADES)
        log.info("Telemetría: %s (%d filas guardadas)", resumen, filas)
        if self.nube:
            self.nube.publicar_telemetria(datos)

    def al_recibir_estado(self, estado: dict[str, Any]) -> None:
        """Actualiza el estado conocido y lo publica en la nube si cambió."""
        if estado != self.ultimo_estado:
            log.info("Estado del dispositivo: %s", estado)
        self.ultimo_estado = estado
        if self.nube:
            self.nube.publicar_estado(estado)

    def al_recibir_evento(self, evento: dict[str, Any]) -> None:
        """Registra un evento del dispositivo en SQLite."""
        tipo = str(evento.get("tipo", "DESCONOCIDO"))
        severidad = str(evento.get("severidad", "INFO"))
        detalle = str(evento.get("detalle", ""))
        self.db.insertar_evento(tipo, detalle, severidad)
        nivel = {"CRITICO": logging.ERROR, "ALERTA": logging.WARNING}.get(severidad, logging.INFO)
        log.log(nivel, "Evento %s [%s]: %s", tipo, severidad, detalle)

    def al_recibir_ack(self, ack: dict[str, Any]) -> None:
        """Actualiza el resultado del comando con el ACK del dispositivo.

        Si el ACK trae un `id` conocido se actualiza esa fila; si no (p. ej. el
        comando lo publicó otra herramienta directamente en HiveMQ) se inserta
        una fila nueva para no perder la trazabilidad.
        """
        resultado = str(ack.get("resultado", "DESCONOCIDO"))
        id_comando = ack.get("id")
        actualizado = False
        if isinstance(id_comando, int):
            actualizado = self.db.actualizar_resultado_comando(id_comando, resultado)
        if not actualizado:
            self.db.insertar_comando(str(ack.get("cmd")), ack.get("valor"),
                                     str(ack.get("origen", "desconocido")), resultado)
        log.info("ACK: %s=%s -> %s", ack.get("cmd"), ack.get("valor"), resultado)

    # ------------------------------------------------------------------
    # Comandos desde la nube
    # ------------------------------------------------------------------
    def enviar_comando(self, cmd: str, valor: Any, origen: str = "adafruit_io") -> int:
        """Registra el comando (resultado ENVIADO) y lo manda al ESP32."""
        id_comando = self.db.insertar_comando(cmd, valor, origen, "ENVIADO")
        if not self.puente.enviar_comando(cmd, valor, origen, id_comando):
            self.db.actualizar_resultado_comando(id_comando, "ERROR_ENVIO")
        return id_comando


def main(argv: list[str] | None = None) -> None:
    """Arranca el servicio y espera hasta Ctrl+C."""
    parser = argparse.ArgumentParser(description="Backend del Invernadero IoT")
    parser.add_argument("--sin-nube", action="store_true",
                        help="no conectar a Adafruit IO (solo HiveMQ + SQLite)")
    parser.add_argument("-v", "--verbose", action="store_true", help="logging DEBUG")
    args = parser.parse_args(argv)

    cfg = cargar_config()
    configurar_logging(cfg, logging.DEBUG if args.verbose else logging.INFO)
    log.info("=== Backend Invernadero IoT ===")

    db = BaseDatos(cfg.db_path)
    puente = PuenteMQTT(cfg)
    servicio = ServicioInvernadero(cfg, db, puente)

    if args.sin_nube:
        log.warning("Modo --sin-nube: Adafruit IO deshabilitado")
    elif not cfg.nube_habilitada:
        log.warning("AIO_USERNAME/AIO_KEY no configurados en .env: se ejecuta sin nube")
    else:
        from backend.nube import NubeAdafruit
        servicio.nube = NubeAdafruit(cfg, on_comando=servicio.enviar_comando)

    # Cierre limpio con Ctrl+C (SIGINT) o SIGTERM
    detener = threading.Event()

    def _senal(signum, _frame):
        log.info("Señal %s recibida, cerrando...", signum)
        detener.set()

    signal.signal(signal.SIGINT, _senal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _senal)

    puente.conectar()
    if servicio.nube:
        servicio.nube.conectar()

    try:
        while not detener.is_set():
            if servicio.nube:
                servicio.nube.mantener()
            detener.wait(1.0)
    finally:
        log.info("Desconectando...")
        if servicio.nube:
            servicio.nube.desconectar()
        puente.desconectar()
        time.sleep(0.2)
        db.cerrar()
        log.info("Backend detenido correctamente")


if __name__ == "__main__":
    main()
