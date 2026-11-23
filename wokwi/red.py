"""
red.py — Conectividad del dispositivo: WiFi + cliente MQTT con reconexión.

Diseño NO bloqueante (orientado a eventos):
    * `mantener(ahora)` se llama en cada vuelta del bucle principal. Funciona
      como una pequeña máquina de estados: si no hay WiFi, lanza la conexión y
      vuelve; si hay WiFi pero no MQTT, intenta conectar al broker (con espera
      entre reintentos); si todo está conectado, revisa mensajes entrantes con
      `check_msg()` (no bloqueante) y envía PING periódicos.
    * Los mensajes entrantes disparan el CALLBACK `on_comando(dict)`.
    * Al (re)conectar se dispara el callback `on_conectado()`.
    * Eventos, estados y ACKs que no se pudieron enviar por estar desconectado
      se guardan en una cola pequeña y se envían al reconectar.

Usa `umqtt.simple`, incluido en el firmware MicroPython del ESP32. Si no
estuviera disponible, se usa la copia de respaldo `umqtt_simple.py` (archivo
plano: el editor de Wokwi no admite carpetas).
"""

import json
import os
import time

import network

try:
    from umqtt.simple import MQTTClient
except ImportError:  # respaldo incluido en el proyecto
    from umqtt_simple import MQTTClient


def _hex(datos):
    """Convierte bytes a texto hexadecimal (sin depender de ubinascii)."""
    return "".join("{:02x}".format(b) for b in datos)


class GestorRed:
    """Mantiene WiFi y MQTT conectados y ofrece publicar/recibir JSON.

    Args:
        cfg: módulo config.py.
        on_comando: callback(dict) para cada mensaje de {prefijo}/comandos.
        on_conectado: callback() cada vez que se establece la sesión MQTT.
    """

    MAX_COLA = 20

    def __init__(self, cfg, on_comando, on_conectado=None):
        self._cfg = cfg
        self._on_comando = on_comando
        self._on_conectado = on_conectado
        self.prefijo = cfg.MQTT_PREFIJO
        self.client_id = cfg.MQTT_CLIENT_ID_BASE + _hex(os.urandom(3))

        self._wlan = network.WLAN(network.STA_IF)
        self._wlan.active(True)
        self._mqtt = None
        self.mqtt_conectado = False
        self._cola = []                      # [(subtopic, payload_str)]

        ahora = time.ticks_ms()
        # Se inicializan "en el pasado" para intentar de inmediato.
        self._ultimo_intento_wifi = time.ticks_add(ahora, -cfg.REINTENTO_WIFI_MS)
        self._ultimo_intento_mqtt = time.ticks_add(ahora, -cfg.REINTENTO_MQTT_MS)
        self._ultimo_ping = ahora
        self._reloj_sincronizado = False

    # ------------------------------------------------------------------
    # Bucle de mantenimiento (llamar cada ~100 ms)
    # ------------------------------------------------------------------
    def mantener(self, ahora):
        """Avanza la máquina de estados de conexión sin bloquear."""
        if not self._wlan.isconnected():
            if self.mqtt_conectado:
                self._marcar_desconectado("WiFi perdido")
            if time.ticks_diff(ahora, self._ultimo_intento_wifi) >= self._cfg.REINTENTO_WIFI_MS:
                self._ultimo_intento_wifi = ahora
                print("[WiFi] conectando a", self._cfg.WIFI_SSID, "...")
                try:
                    self._wlan.connect(self._cfg.WIFI_SSID, self._cfg.WIFI_PASSWORD)
                except OSError as error:
                    print("[WiFi] error:", error)
            return

        if not self._reloj_sincronizado:
            self._sincronizar_reloj()

        if not self.mqtt_conectado:
            if time.ticks_diff(ahora, self._ultimo_intento_mqtt) >= self._cfg.REINTENTO_MQTT_MS:
                self._ultimo_intento_mqtt = ahora
                self._conectar_mqtt()
            return

        try:
            self._mqtt.check_msg()          # dispara _al_recibir si hay mensaje
            if time.ticks_diff(ahora, self._ultimo_ping) >= self._cfg.INTERVALO_PING_MS:
                self._ultimo_ping = ahora
                self._mqtt.ping()
        except OSError as error:
            self._marcar_desconectado(error)

    def _sincronizar_reloj(self):
        """Intenta poner la hora por NTP (solo para el campo `ts`)."""
        self._reloj_sincronizado = True     # un solo intento, no es crítico
        try:
            import ntptime
            ntptime.settime()
            print("[NTP] hora sincronizada")
        except Exception as error:  # noqa: BLE001
            print("[NTP] no se pudo sincronizar:", error)

    def _conectar_mqtt(self):
        """Conecta al broker y se suscribe a los comandos.

        Nota: el connect de umqtt es bloqueante por unos cientos de ms; es el
        único punto que puede demorar el bucle y solo ocurre al (re)conectar.
        """
        print("[MQTT] conectando a", self._cfg.MQTT_BROKER, "como", self.client_id)
        try:
            self._mqtt = MQTTClient(self.client_id, self._cfg.MQTT_BROKER,
                                    port=self._cfg.MQTT_PORT,
                                    keepalive=self._cfg.MQTT_KEEPALIVE)
            self._mqtt.set_callback(self._al_recibir)
            self._mqtt.connect()
            self._mqtt.subscribe(self.topic("comandos"))
        except OSError as error:
            print("[MQTT] fallo de conexión:", error)
            self._mqtt = None
            return
        self.mqtt_conectado = True
        self._ultimo_ping = time.ticks_ms()
        print("[MQTT] conectado; suscrito a", self.topic("comandos"))
        if self._on_conectado:
            self._on_conectado()
        self._vaciar_cola()

    def _marcar_desconectado(self, motivo):
        print("[MQTT] desconectado:", motivo)
        self.mqtt_conectado = False
        try:
            if self._mqtt:
                self._mqtt.disconnect()
        except OSError:
            pass
        self._mqtt = None

    # ------------------------------------------------------------------
    # Recepción (callback MQTT)
    # ------------------------------------------------------------------
    def _al_recibir(self, topic, mensaje):
        """Callback de umqtt: decodifica el JSON y lo entrega a on_comando."""
        try:
            datos = json.loads(mensaje)
        except ValueError:
            print("[MQTT] JSON inválido en", topic, ":", mensaje)
            return
        if isinstance(datos, dict):
            self._on_comando(datos)

    # ------------------------------------------------------------------
    # Publicación
    # ------------------------------------------------------------------
    def topic(self, subtopic):
        """Construye el topic completo: {prefijo}/{subtopic}."""
        return self.prefijo + "/" + subtopic

    def publicar(self, subtopic, datos, encolar=False):
        """Publica `datos` (dict) como JSON en {prefijo}/{subtopic}.

        Args:
            encolar: si es True y no hay conexión, se guarda para enviarlo al
                reconectar (eventos, estado, ACKs). La telemetría no se encola:
                solo interesa el valor más reciente.
        Returns:
            bool: True si se envió.
        """
        payload = json.dumps(datos)
        if self.mqtt_conectado:
            try:
                self._mqtt.publish(self.topic(subtopic), payload)
                return True
            except OSError as error:
                self._marcar_desconectado(error)
        if encolar:
            if len(self._cola) >= self.MAX_COLA:
                self._cola.pop(0)           # descarta el más antiguo
            self._cola.append((subtopic, payload))
        return False

    def _vaciar_cola(self):
        """Envía lo pendiente tras reconectar."""
        while self._cola and self.mqtt_conectado:
            subtopic, payload = self._cola.pop(0)
            try:
                self._mqtt.publish(self.topic(subtopic), payload)
            except OSError as error:
                self._cola.insert(0, (subtopic, payload))
                self._marcar_desconectado(error)
