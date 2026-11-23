"""
main.py — Punto de entrada del ESP32 (MicroPython, Wokwi).

Invernadero Inteligente IoT — Parcial #2 Desarrollo de Software VIII.

Arquitectura del firmware (programación modular y orientada a eventos):

    sensores.py   -> lectura de DHT22, LDR y potenciómetro (machine.ADC / dht)
    actuadores.py -> relés, LED, buzzer PWM y OLED I2C (machine.Pin / PWM / I2C)
    controlador.py-> reglas AUTO/MANUAL, umbrales y emisión de eventos
    red.py        -> WiFi + MQTT (umqtt.simple) con reconexión automática
    main.py       -> CABLEADO de todo lo anterior + bucle despachador

¿Dónde están los eventos?
    1. CALLBACK MQTT: cada mensaje en {prefijo}/comandos llama a `al_recibir_comando`.
    2. TEMPORIZADORES no bloqueantes (ticks_ms/ticks_diff): sensores cada 2 s y
       telemetría cada 15 s llaman a sus manejadores cuando vencen.
    3. BUS DE EVENTOS del controlador: cada cambio de actuador, de modo o de
       condición crítica emite "evento"/"estado", y los suscriptores registrados
       aquí los publican por MQTT AL MOMENTO.
    4. El bucle principal es corto (~100 ms) y solo despacha: no hay sleeps
       largos ni esperas activas.
"""

import time

import config
from actuadores import Buzzer, Led, Pantalla, Rele
from controlador import Controlador, Parametros
from red import GestorRed
from sensores import DHTSensor, SensorLuz, SensorSuelo

# MicroPython en ESP32 cuenta los segundos desde 2000-01-01; el backend espera
# epoch Unix (1970). Este desfase corrige el campo `ts` de la telemetría.
_DESFASE_EPOCH = 946684800 if time.gmtime(0)[0] == 2000 else 0


class Temporizador:
    """Temporizador periódico NO bloqueante.

    `revisar(ahora)` se llama en cada vuelta del bucle; cuando ha pasado el
    periodo, ejecuta el manejador asociado (patrón "evento de tiempo").
    """

    def __init__(self, periodo_ms, manejador):
        self.periodo = periodo_ms
        self.manejador = manejador
        self._ultimo = time.ticks_ms()

    def revisar(self, ahora):
        """Dispara el manejador si venció el periodo."""
        if time.ticks_diff(ahora, self._ultimo) >= self.periodo:
            self._ultimo = ahora
            self.manejador()


def timestamp_unix():
    """Segundos epoch Unix (válidos si NTP sincronizó; si no, aproximados)."""
    return time.time() + _DESFASE_EPOCH


def main():
    """Construye los objetos, conecta los eventos y ejecuta el bucle."""
    print("=== Invernadero Inteligente IoT (ESP32 + MicroPython) ===")

    # --- Hardware -----------------------------------------------------
    pantalla = Pantalla(config.PIN_I2C_SDA, config.PIN_I2C_SCL, config.OLED_DIRECCION,
                        config.OLED_ANCHO, config.OLED_ALTO)
    controlador = Controlador(
        sensor_dht=DHTSensor(config.PIN_DHT),
        sensor_luz=SensorLuz(config.PIN_LDR),
        sensor_suelo=SensorSuelo(config.PIN_SUELO),
        bomba=Rele(config.PIN_BOMBA, config.RELE_ACTIVO_ALTO),
        ventilador=Rele(config.PIN_VENTILADOR, config.RELE_ACTIVO_ALTO),
        luz_cultivo=Led(config.PIN_LUZ_CULTIVO),
        buzzer=Buzzer(config.PIN_BUZZER, config.BUZZER_FRECUENCIA, config.BUZZER_PERIODO_MS),
        pantalla=pantalla,
        params=Parametros.desde_config(config),
    )

    # --- Manejadores de eventos ---------------------------------------
    red = None  # se asigna abajo; los closures lo usan cuando ya existe

    def al_recibir_comando(datos):
        """CALLBACK MQTT: ejecuta el comando y responde con un ACK."""
        cmd = datos.get("cmd")
        valor = datos.get("valor")
        origen = datos.get("origen", "desconocido")
        resultado = controlador.manejar_comando(cmd, valor, origen)
        print("[CMD]", cmd, "=", valor, "desde", origen, "->", resultado)
        ack = {"cmd": cmd, "valor": valor, "resultado": resultado, "origen": origen}
        if "id" in datos:
            ack["id"] = datos["id"]       # permite al backend asociar el ACK
        red.publicar("comandos/ack", ack, encolar=True)

    def al_conectar():
        """Al (re)conectar MQTT se publica el estado actual."""
        controlador.conectado = True
        red.publicar("estado", controlador.estado())
        controlador.refrescar_pantalla()

    def publicar_evento(evento):
        print("[EVENTO]", evento["severidad"], evento["tipo"], "-", evento["detalle"])
        red.publicar("eventos", evento, encolar=True)

    def publicar_estado(estado):
        red.publicar("estado", estado, encolar=True)

    def publicar_telemetria():
        """Manejador del temporizador de telemetría (cada 15 s)."""
        if controlador.lectura["temperatura"] is None:
            return  # aún no hay lectura válida
        datos = controlador.telemetria(timestamp_unix())
        if red.publicar("telemetria", datos):
            print("[TELEMETRIA]", datos)

    red = GestorRed(config, on_comando=al_recibir_comando, on_conectado=al_conectar)
    controlador.bus.on("evento", publicar_evento)
    controlador.bus.on("estado", publicar_estado)

    temporizadores = [
        Temporizador(config.INTERVALO_SENSORES_MS, controlador.ciclo_sensores),
        Temporizador(config.INTERVALO_TELEMETRIA_MS, publicar_telemetria),
    ]

    controlador.iniciar()           # emite INICIO_SISTEMA (queda en cola hasta conectar)
    controlador.ciclo_sensores()    # primera lectura inmediata

    # --- Bucle despachador --------------------------------------------
    while True:
        inicio = time.ticks_ms()
        red.mantener(inicio)
        if controlador.conectado != red.mqtt_conectado:
            controlador.conectado = red.mqtt_conectado
            controlador.refrescar_pantalla()
        for temporizador in temporizadores:
            temporizador.revisar(inicio)
        controlador.tick(inicio)
        # Espera solo lo que falta para completar ~100 ms por vuelta.
        transcurrido = time.ticks_diff(time.ticks_ms(), inicio)
        if transcurrido < config.PERIODO_BUCLE_MS:
            time.sleep_ms(config.PERIODO_BUCLE_MS - transcurrido)


main()
