"""
mqtt_bridge.py — Cliente MQTT del backend hacia el broker HiveMQ.

Se suscribe a lo que publica el ESP32 y despacha cada mensaje a un MANEJADOR
registrado por tipo (programación orientada a eventos):

    {prefijo}/telemetria   -> "telemetria"
    {prefijo}/estado       -> "estado"
    {prefijo}/eventos      -> "evento"
    {prefijo}/comandos/ack -> "ack"

y permite enviar comandos al dispositivo en {prefijo}/comandos.

Usa paho-mqtt 2.x con `CallbackAPIVersion.VERSION2`. La reconexión es
automática: `loop_start()` corre un hilo que reintenta con backoff
(`reconnect_delay_set`) y `on_connect` vuelve a suscribirse cada vez.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from backend.config import Config

log = logging.getLogger(__name__)

# subtopic -> nombre del manejador
SUSCRIPCIONES = {
    "telemetria": "telemetria",
    "estado": "estado",
    "eventos": "evento",
    "comandos/ack": "ack",
}

Manejador = Callable[[dict[str, Any]], None]


def construir_comando(cmd: str, valor: Any, origen: str, id_comando: int | None = None) -> dict:
    """Arma el payload JSON de un comando para {prefijo}/comandos.

    El campo `id` es opcional: el ESP32 lo devuelve en el ACK y así el backend
    sabe qué fila de la tabla `comandos` actualizar.
    """
    payload: dict[str, Any] = {"cmd": cmd, "valor": valor, "origen": origen}
    if id_comando is not None:
        payload["id"] = id_comando
    return payload


class PuenteMQTT:
    """Conexión del backend con HiveMQ.

    Uso:
        puente = PuenteMQTT(cfg)
        puente.on("telemetria", funcion)   # funcion(dict)
        puente.conectar()
        puente.enviar_comando("modo", "MANUAL", "adafruit_io", id_comando=7)
    """

    def __init__(self, cfg: Config, client_id_base: str = "invernadero-backend-",
                 suscripciones: dict[str, str] | None = None):
        self.cfg = cfg
        self._suscripciones = suscripciones if suscripciones is not None else SUSCRIPCIONES
        self._manejadores: dict[str, list[Manejador]] = {}
        self.conectado = False
        client_id = client_id_base + uuid.uuid4().hex[:8]
        self._cliente = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=client_id)
        self._cliente.on_connect = self._al_conectar
        self._cliente.on_disconnect = self._al_desconectar
        self._cliente.on_message = self._al_recibir
        self._cliente.reconnect_delay_set(min_delay=1, max_delay=30)
        log.debug("Cliente MQTT creado con id %s", client_id)

    # ------------------------------------------------------------------
    def on(self, tipo: str, manejador: Manejador) -> None:
        """Registra un manejador para 'telemetria', 'estado', 'evento' o 'ack'."""
        self._manejadores.setdefault(tipo, []).append(manejador)

    def conectar(self) -> None:
        """Conecta de forma asíncrona y arranca el hilo de red."""
        log.info("Conectando a MQTT %s:%s (prefijo %s)",
                 self.cfg.mqtt_broker, self.cfg.mqtt_port, self.cfg.mqtt_prefijo)
        self._cliente.connect_async(self.cfg.mqtt_broker, self.cfg.mqtt_port, keepalive=60)
        self._cliente.loop_start()

    def desconectar(self) -> None:
        """Cierra la sesión y detiene el hilo de red."""
        self._cliente.disconnect()
        self._cliente.loop_stop()

    def publicar(self, subtopic: str, datos: dict) -> bool:
        """Publica `datos` como JSON en {prefijo}/{subtopic}."""
        info = self._cliente.publish(self.cfg.topic(subtopic), json.dumps(datos), qos=0)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def enviar_comando(self, cmd: str, valor: Any, origen: str,
                       id_comando: int | None = None) -> bool:
        """Envía un comando al ESP32 por {prefijo}/comandos."""
        payload = construir_comando(cmd, valor, origen, id_comando)
        ok = self.conectado and self.publicar("comandos", payload)
        if ok:
            log.info("Comando enviado al dispositivo: %s", payload)
        else:
            log.warning("No se pudo enviar el comando (¿sin conexión?): %s", payload)
        return ok

    # ------------------------------------------------------------------
    # Callbacks de paho (corren en el hilo de red)
    # ------------------------------------------------------------------
    def _al_conectar(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("Conexión MQTT rechazada: %s", reason_code)
            return
        self.conectado = True
        for subtopic in self._suscripciones:
            client.subscribe(self.cfg.topic(subtopic), qos=0)
        log.info("Conectado a HiveMQ; suscrito a %s",
                 ", ".join(self.cfg.topic(s) for s in self._suscripciones))

    def _al_desconectar(self, client, userdata, flags, reason_code, properties):
        self.conectado = False
        if reason_code.is_failure:
            log.warning("Desconectado de HiveMQ (%s); reintentando...", reason_code)
        else:
            log.info("Desconectado de HiveMQ")

    def _al_recibir(self, client, userdata, msg: mqtt.MQTTMessage):
        prefijo = self.cfg.mqtt_prefijo + "/"
        subtopic = msg.topic[len(prefijo):] if msg.topic.startswith(prefijo) else msg.topic
        tipo = self._suscripciones.get(subtopic)
        if tipo is None:
            return
        try:
            datos = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("Payload no JSON en %s: %r", msg.topic, msg.payload[:100])
            return
        if not isinstance(datos, dict):
            log.warning("Payload JSON no es un objeto en %s: %r", msg.topic, datos)
            return
        log.debug("MQTT %s <- %s", subtopic, datos)
        for manejador in self._manejadores.get(tipo, []):
            try:
                manejador(datos)
            except Exception:  # noqa: BLE001 - un manejador no debe tumbar el hilo
                log.exception("Error en manejador '%s'", tipo)
