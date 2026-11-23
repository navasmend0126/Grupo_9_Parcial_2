"""
sensores.py — Abstracciones de los sensores del invernadero (capa de hardware).

Cada clase encapsula el acceso a GPIO mediante las librerías nativas de
MicroPython (machine.Pin, machine.ADC y el módulo dht) y expone un método
`leer()` sencillo. El controlador nunca toca `machine` directamente: recibe estos
objetos ya construidos (inyección de dependencias), lo que permite probar la
lógica en la PC con sensores falsos.

Sensores:
    DHTSensor   -> DHT22 en GPIO15: temperatura (°C) y humedad relativa (%).
    SensorLuz   -> módulo LDR en GPIO34 (salida AO): luz 0–100 %.
    SensorSuelo -> potenciómetro en GPIO35 que simula humedad de suelo 0–100 %.
"""

import dht
from machine import ADC, Pin


class DHTSensor:
    """Sensor DHT22 de temperatura y humedad del aire.

    El DHT22 puede fallar ocasionalmente (OSError por timeout). En ese caso se
    devuelve `None` y el controlador decide qué hacer (conserva el último valor
    válido y registra un evento de alerta).
    """

    def __init__(self, pin):
        """Crea el sensor en el pin indicado (número de GPIO)."""
        self._sensor = dht.DHT22(Pin(pin))

    def leer(self):
        """Realiza una medición.

        Returns:
            tuple | None: (temperatura_c, humedad_pct) redondeados a 1 decimal,
            o None si la lectura falló.
        """
        try:
            self._sensor.measure()
            return (round(self._sensor.temperature(), 1),
                    round(self._sensor.humidity(), 1))
        except OSError:
            return None


class SensorAnalogico:
    """Base para sensores analógicos conectados al ADC1 del ESP32.

    - Atenuación de 11 dB: rango de entrada ~0–3.3 V (el máximo del pin).
    - Se usa `read_u16()` (0–65535), API portable entre versiones de MicroPython.
    - Se promedian varias muestras para filtrar ruido.
    """

    MUESTRAS = 4

    def __init__(self, pin):
        """Configura el ADC sobre el pin indicado (debe ser de ADC1: 32–39)."""
        self._adc = ADC(Pin(pin))
        self._adc.atten(ADC.ATTN_11DB)

    def leer_crudo(self):
        """Devuelve el promedio de varias muestras en escala 0–65535."""
        total = 0
        for _ in range(self.MUESTRAS):
            total += self._adc.read_u16()
        return total // self.MUESTRAS

    def leer_porcentaje(self):
        """Convierte la lectura cruda a porcentaje 0–100 (proporcional al voltaje)."""
        return min(100, max(0, round(self.leer_crudo() * 100 / 65535)))


class SensorLuz(SensorAnalogico):
    """Módulo fotorresistor (wokwi-photoresistor-sensor), salida AO.

    POLARIDAD: en este módulo la LDR está en la parte alta del divisor, así que
    MÁS luz => MENOR resistencia => MENOR voltaje en AO. Por eso se invierte la
    escala: 0 V = 100 % de luz y 3.3 V = 0 % de luz.
    """

    def leer(self):
        """Devuelve el nivel de luz en porcentaje (0 = oscuro, 100 = pleno sol)."""
        return 100 - self.leer_porcentaje()


class SensorSuelo(SensorAnalogico):
    """Humedad de suelo simulada con un potenciómetro.

    Potenciómetro al máximo (3.3 V) = suelo empapado (100 %);
    al mínimo (0 V) = suelo totalmente seco (0 %).
    """

    def leer(self):
        """Devuelve la humedad del suelo en porcentaje."""
        return self.leer_porcentaje()
