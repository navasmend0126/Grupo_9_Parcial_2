"""
actuadores.py — Abstracciones de los actuadores del invernadero (capa de hardware).

Usa machine.Pin (salidas digitales), machine.PWM (buzzer) y machine.I2C (OLED).
Todas las clases exponen una interfaz mínima (encender/apagar/encendido, etc.)
para que el controlador pueda usarlas sin conocer el hardware y para poder
sustituirlas por objetos falsos en las pruebas.

Actuadores:
    Rele      -> relés de bomba (GPIO26) y ventilador (GPIO27).
    Led       -> LED blanco de luz de cultivo (GPIO25).
    Buzzer    -> alarma crítica intermitente por PWM (GPIO14).
    Pantalla  -> OLED SSD1306 128x64 por I2C (SDA 21, SCL 22).
"""

import time
from machine import I2C, PWM, Pin


class SalidaDigital:
    """Salida digital genérica con lógica activa en alto o en bajo."""

    def __init__(self, pin, activo_alto=True):
        """Configura el pin como salida y lo deja APAGADO."""
        self._activo_alto = activo_alto
        self._pin = Pin(pin, Pin.OUT)
        self._encendido = False
        self.apagar()

    def encender(self):
        """Activa la salida."""
        self._pin.value(1 if self._activo_alto else 0)
        self._encendido = True

    def apagar(self):
        """Desactiva la salida."""
        self._pin.value(0 if self._activo_alto else 1)
        self._encendido = False

    def establecer(self, encendido):
        """Enciende o apaga según el booleano recibido."""
        if encendido:
            self.encender()
        else:
            self.apagar()

    @property
    def encendido(self):
        """bool: estado lógico actual (True = activo)."""
        return self._encendido


class Rele(SalidaDigital):
    """Módulo relé. Al activarse conecta COM con NO, encendiendo el LED indicador
    (y, en un sistema real, la bomba o el ventilador)."""


class Led(SalidaDigital):
    """LED simple conectado directamente al GPIO (con resistencia de 220 Ω)."""


class Buzzer:
    """Buzzer piezoeléctrico manejado por PWM con patrón intermitente.

    El patrón se genera de forma NO bloqueante: el controlador llama a
    `actualizar(ahora_ms)` en cada vuelta del bucle y el buzzer alterna entre
    sonido y silencio cada `periodo_ms`, sin usar sleep.
    """

    def __init__(self, pin, frecuencia=2000, periodo_ms=400):
        """Crea el PWM en silencio (duty 0)."""
        self._pwm = PWM(Pin(pin))
        self._pwm.freq(frecuencia)
        self._periodo = periodo_ms
        self._alarma = False
        self._sonando = False
        self._ultimo_cambio = time.ticks_ms()
        self._silenciar()

    def _duty(self, valor_u16):
        """Fija el ciclo de trabajo de forma compatible con varias versiones."""
        try:
            self._pwm.duty_u16(valor_u16)
        except AttributeError:          # firmwares antiguos solo tienen duty()
            self._pwm.duty(valor_u16 >> 6)

    def _silenciar(self):
        self._duty(0)
        self._sonando = False

    def _sonar(self):
        self._duty(32768)               # 50 % de ciclo de trabajo
        self._sonando = True

    def activar_alarma(self):
        """Inicia el patrón intermitente."""
        if not self._alarma:
            self._alarma = True
            self._ultimo_cambio = time.ticks_ms()
            self._sonar()

    def desactivar(self):
        """Detiene la alarma y deja el buzzer en silencio."""
        self._alarma = False
        self._silenciar()

    @property
    def activo(self):
        """bool: True si la alarma está activada."""
        return self._alarma

    def actualizar(self, ahora):
        """Alterna sonido/silencio si pasó el periodo (llamar en cada ciclo)."""
        if not self._alarma:
            return
        if time.ticks_diff(ahora, self._ultimo_cambio) >= self._periodo:
            self._ultimo_cambio = ahora
            if self._sonando:
                self._silenciar()
            else:
                self._sonar()


class Pantalla:
    """Pantalla OLED SSD1306 (128x64, I2C).

    Muestra el resumen del invernadero en 8 líneas de 16 caracteres. Si la
    pantalla no responde en el bus I2C, el sistema sigue funcionando sin ella.
    """

    def __init__(self, sda, scl, direccion=0x3C, ancho=128, alto=64):
        """Inicializa el bus I2C y el driver ssd1306."""
        self._oled = None
        try:
            import ssd1306
            i2c = I2C(0, sda=Pin(sda), scl=Pin(scl), freq=400000)
            self._oled = ssd1306.SSD1306_I2C(ancho, alto, i2c, addr=direccion)
            self.mensaje("INVERNADERO IoT", "Iniciando...")
        except (OSError, ImportError) as error:
            print("[OLED] no disponible:", error)

    def mensaje(self, *lineas):
        """Muestra hasta 8 líneas de texto libre (útil al arrancar)."""
        if self._oled is None:
            return
        self._oled.fill(0)
        for i, linea in enumerate(lineas[:8]):
            self._oled.text(str(linea)[:16], 0, i * 8)
        self._oled.show()

    def mostrar(self, lectura, estado, conectado):
        """Dibuja el panel principal.

        Args:
            lectura (dict): temperatura, humedad, luz, suelo (pueden ser None).
            estado (dict): modo, bomba, ventilador, luz_cultivo, umbral_temp, critico.
            conectado (bool): True si hay conexión MQTT.
        """
        def fmt(valor, decimales=0):
            if valor is None:
                return "--"
            return ("{:." + str(decimales) + "f}").format(valor)

        def onoff(b):
            return "ON" if b else "OFF"

        modo = "AUTO" if estado["modo"] == "AUTO" else "MAN."
        pie = "!!! CRITICO !!!" if estado["critico"] else (
            "MQTT: OK" if conectado else "MQTT: --")
        self.mensaje(
            "INVERNADERO " + modo,
            "T:" + fmt(lectura["temperatura"], 1) + "C H:" + fmt(lectura["humedad"]) + "%",
            "Luz:" + fmt(lectura["luz"]) + "% Sue:" + fmt(lectura["suelo"]) + "%",
            "Bomba: " + onoff(estado["bomba"]),
            "Vent.: " + onoff(estado["ventilador"]),
            "LuzCult: " + onoff(estado["luz_cultivo"]),
            "Umbral T: " + fmt(estado["umbral_temp"]) + "C",
            pie,
        )
