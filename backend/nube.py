"""
nube.py — Integración con la plataforma en la nube Adafruit IO (MQTT).

Funciones:
    * Publicar la telemetría en los feeds temperatura, humedad, luz y suelo.
    * Publicar el feed `estado` (texto legible) SOLO cuando cambia.
    * Suscribirse a los feeds de comandos (modo, bomba, ventilador, umbral-temp)
      y traducir cada mensaje a un comando para el ESP32.
    * Suscribirse a {usuario}/throttle y {usuario}/errors y registrar lo que llegue.
    * Respetar el límite del plan gratuito (30 datos/min) con un limitador.

Se usa la librería oficial `adafruit-io` (`from Adafruit_IO import MQTTClient`).
Notas sobre la librería (v3.0.0), resueltas aquí de forma documentada:
    1. Su parser de topics asume `usuario/feeds/clave`; los topics
       `usuario/throttle` y `usuario/errors` le provocarían un IndexError. Por eso
       se registran con `message_callback_add` del cliente paho interno, que
       atiende esos topics ANTES del parser de la librería.
    2. Sus callbacks internos de conexión/desconexión lanzan excepciones ante
       errores (credenciales malas, caída de red), lo que detendría el hilo de
       red. Se reemplazan por manejadores propios que solo registran el problema
       y dejan que paho reintente la conexión.

Valores viejos al arrancar: Adafruit IO no retiene mensajes MQTT, pero por
seguridad se IGNORA cualquier comando recibido durante los primeros segundos
tras suscribirse (`aio_gracia_arranque_s`), para no ejecutar órdenes antiguas.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable

from backend.config import FEED_ESTADO, FEEDS_COMANDOS, FEEDS_TELEMETRIA, Config

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Funciones puras (fáciles de probar)
# ----------------------------------------------------------------------
def traducir_comando(feed: str, valor: str) -> tuple[str, Any] | None:
    """Traduce un mensaje de un feed de comandos de Adafruit IO al comando
    que entiende el ESP32.

    Args:
        feed: key del feed (modo, bomba, ventilador, umbral-temp).
        valor: texto recibido del dashboard.

    Returns:
        (cmd, valor_normalizado) o None si el feed no es de comandos.
        Valores no reconocidos se reenvían tal cual: el dispositivo responderá
        INVALIDO y quedará registrado.
    """
    cmd = FEEDS_COMANDOS.get(feed)
    if cmd is None:
        return None
    texto = str(valor).strip()
    if cmd in ("bomba", "ventilador"):
        alias = {"1": "ON", "TRUE": "ON", "ON": "ON", "0": "OFF", "FALSE": "OFF", "OFF": "OFF"}
        return cmd, alias.get(texto.upper(), texto)
    if cmd == "modo":
        return cmd, texto.upper()
    if cmd == "umbral_temp":
        try:
            numero = float(texto)
        except ValueError:
            return cmd, texto
        return cmd, int(numero) if numero.is_integer() else numero
    return cmd, texto


def formatear_estado(estado: dict[str, Any]) -> str:
    """Convierte el estado del dispositivo en el texto del feed `estado`.

    Ejemplo: "AUTO | Bomba OFF | Vent ON | Luz OFF | Umbral 30C"
    """
    def onoff(clave: str) -> str:
        return "ON" if estado.get(clave) else "OFF"

    texto = (f"{estado.get('modo', '?')} | Bomba {onoff('bomba')} | "
             f"Vent {onoff('ventilador')} | Luz {onoff('luz_cultivo')} | "
             f"Umbral {estado.get('umbral_temp', '?')}C")
    if estado.get("critico"):
        texto += " | CRITICO"
    return texto


class LimitadorTasa:
    """Ventana deslizante: permite como máximo `maximo` envíos por `ventana_s`.

    Args:
        maximo: envíos permitidos en la ventana.
        ventana_s: tamaño de la ventana en segundos.
        reloj: función de tiempo (inyectable para pruebas).
    """

    def __init__(self, maximo: int, ventana_s: float = 60.0,
                 reloj: Callable[[], float] = time.monotonic):
        self.maximo = maximo
        self.ventana_s = ventana_s
        self._reloj = reloj
        self._envios: deque[float] = deque()
        self._lock = threading.Lock()

    def _purgar(self, ahora: float) -> None:
        while self._envios and ahora - self._envios[0] >= self.ventana_s:
            self._envios.popleft()

    def disponibles(self) -> int:
        """Cuántos envíos quedan en la ventana actual."""
        with self._lock:
            self._purgar(self._reloj())
            return self.maximo - len(self._envios)

    def permitir(self, cantidad: int = 1) -> bool:
        """Reserva `cantidad` envíos si hay cupo. Devuelve True si se permitió."""
        with self._lock:
            ahora = self._reloj()
            self._purgar(ahora)
            if len(self._envios) + cantidad > self.maximo:
                return False
            self._envios.extend([ahora] * cantidad)
            return True


# ----------------------------------------------------------------------
# Cliente de Adafruit IO
# ----------------------------------------------------------------------
class NubeAdafruit:
    """Puente entre el backend y Adafruit IO.

    Args:
        cfg: configuración (usuario y key salen del .env).
        on_comando: callback(cmd, valor) para cada comando recibido de la nube.
    """

    REINTENTO_S = 30

    def __init__(self, cfg: Config, on_comando: Callable[[str, Any], None]):
        from Adafruit_IO import MQTTClient  # import diferido: no es necesario en tests

        self.cfg = cfg
        self._on_comando = on_comando
        self.limitador = LimitadorTasa(cfg.aio_limite_por_minuto, 60.0)
        self._aio = MQTTClient(cfg.aio_username, cfg.aio_key)   # TLS, puerto 8883
        self._paho = self._aio._client                          # cliente paho interno
        self._aio.on_message = self._al_recibir_feed
        # Ver nota 2 del docstring del módulo.
        self._paho.on_connect = self._al_conectar
        self._paho.on_disconnect = self._al_desconectar
        self._paho.reconnect_delay_set(min_delay=2, max_delay=60)
        # Ver nota 1: throttle y errors con manejador propio.
        self._topic_throttle = f"{cfg.aio_username}/throttle"
        self._topic_errors = f"{cfg.aio_username}/errors"
        self._paho.message_callback_add(self._topic_throttle, self._al_recibir_throttle)
        self._paho.message_callback_add(self._topic_errors, self._al_recibir_error)

        self._inicio_suscripcion = 0.0
        self._ultimo_estado_enviado: str | None = None
        self._estado_pendiente: str | None = None
        self._ultimo_intento = 0.0
        self._hilo_iniciado = False

    @property
    def conectado(self) -> bool:
        """True si la sesión MQTT con Adafruit IO está activa."""
        return self._aio.is_connected()

    # ------------------------------------------------------------------
    # Conexión
    # ------------------------------------------------------------------
    def conectar(self) -> None:
        """Conecta a io.adafruit.com:8883 (TLS) y arranca el hilo de red."""
        self._ultimo_intento = time.monotonic()
        try:
            log.info("Conectando a Adafruit IO como '%s'...", self.cfg.aio_username)
            self._aio.connect()
        except OSError as error:
            log.error("No se pudo conectar a Adafruit IO: %s (se reintentará)", error)
            return
        if not self._hilo_iniciado:
            self._aio.loop_background()
            self._hilo_iniciado = True

    def desconectar(self) -> None:
        """Cierra la sesión con Adafruit IO."""
        try:
            self._paho.disconnect()
            self._paho.loop_stop()
        except Exception:  # noqa: BLE001
            log.debug("Error al desconectar de Adafruit IO", exc_info=True)

    def mantener(self) -> None:
        """Llamar periódicamente: reintenta la conexión inicial si falló y
        envía el estado que quedó pendiente por el límite de tasa."""
        if not self._hilo_iniciado and time.monotonic() - self._ultimo_intento > self.REINTENTO_S:
            self.conectar()
        if self._estado_pendiente and self.conectado:
            self._enviar_estado(self._estado_pendiente)

    # ------------------------------------------------------------------
    # Callbacks del cliente paho (API v1, la que usa la librería)
    # ------------------------------------------------------------------
    def _al_conectar(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("Adafruit IO rechazó la conexión (rc=%s). ¿AIO_USERNAME/AIO_KEY "
                      "correctos en .env?", rc)
            return
        self._aio._connected = True
        self._inicio_suscripcion = time.monotonic()
        for feed in FEEDS_COMANDOS:
            self._aio.subscribe(feed)
        client.subscribe(self._topic_throttle)
        client.subscribe(self._topic_errors)
        log.info("Conectado a Adafruit IO; suscrito a feeds %s + throttle/errors",
                 ", ".join(FEEDS_COMANDOS))

    def _al_desconectar(self, client, userdata, rc):
        self._aio._connected = False
        if rc != 0:
            log.warning("Desconexión inesperada de Adafruit IO (rc=%s); reintentando...", rc)
        else:
            log.info("Desconectado de Adafruit IO")

    def _al_recibir_throttle(self, client, userdata, msg):
        log.warning("Adafruit IO THROTTLE: %s", msg.payload.decode("utf-8", "replace"))

    def _al_recibir_error(self, client, userdata, msg):
        log.error("Adafruit IO ERROR: %s", msg.payload.decode("utf-8", "replace"))

    def _al_recibir_feed(self, client, feed_id: str, payload: str):
        """EVENTO: llegó un valor a un feed de comandos desde el dashboard."""
        transcurrido = time.monotonic() - self._inicio_suscripcion
        if transcurrido < self.cfg.aio_gracia_arranque_s:
            log.info("Ignorado %s=%s (valor viejo recibido al arrancar)", feed_id, payload)
            return
        traducido = traducir_comando(feed_id, payload)
        if traducido is None:
            log.debug("Mensaje de feed no manejado: %s=%s", feed_id, payload)
            return
        cmd, valor = traducido
        log.info("Comando desde Adafruit IO: feed '%s'='%s' -> %s=%s", feed_id, payload, cmd, valor)
        self._on_comando(cmd, valor)

    # ------------------------------------------------------------------
    # Publicación (respetando 30 datos/min)
    # ------------------------------------------------------------------
    def publicar_telemetria(self, datos: dict[str, Any]) -> int:
        """Publica cada sensor en su feed. Devuelve cuántos valores se enviaron."""
        if not self.conectado:
            log.debug("Adafruit IO desconectado; telemetría no enviada")
            return 0
        enviados = 0
        for campo, feed in FEEDS_TELEMETRIA.items():
            valor = datos.get(campo)
            if valor is None:
                continue
            if not self.limitador.permitir():
                log.warning("Límite de Adafruit IO alcanzado; se omite %s=%s", feed, valor)
                continue
            self._aio.publish(feed, valor)
            enviados += 1
        log.debug("Telemetría enviada a Adafruit IO (%d valores)", enviados)
        return enviados

    def publicar_estado(self, estado: dict[str, Any]) -> None:
        """Publica el feed `estado` solo si el texto cambió."""
        texto = formatear_estado(estado)
        if texto == self._ultimo_estado_enviado:
            return
        self._estado_pendiente = texto
        if self.conectado:
            self._enviar_estado(texto)

    def _enviar_estado(self, texto: str) -> None:
        if not self.limitador.permitir():
            log.warning("Límite de Adafruit IO alcanzado; estado '%s' queda pendiente", texto)
            return
        self._aio.publish(FEED_ESTADO, texto)
        self._ultimo_estado_enviado = texto
        self._estado_pendiente = None
        log.info("Estado publicado en Adafruit IO: %s", texto)
