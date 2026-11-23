"""
controlador.py — Lógica de control del invernadero (independiente del hardware).

Este módulo NO importa `machine`: recibe sensores y actuadores ya construidos
(inyección de dependencias). Gracias a eso:
    * corre igual en el ESP32 (MicroPython) que en la PC (CPython),
    * se prueba con pytest usando objetos falsos (tests/test_controlador.py),
    * el simulador del backend (backend/simulador_dispositivo.py) reutiliza
      exactamente la misma lógica que el dispositivo real.

PROGRAMACIÓN ORIENTADA A EVENTOS
--------------------------------
El controlador no publica nada por sí mismo: cuando algo relevante ocurre
(un actuador cambia, se entra en condición crítica, llega un comando...) EMITE
un evento en su `BusEventos`. Quien esté suscrito (en main.py, el módulo de red)
reacciona publicándolo por MQTT al instante. Hay dos tipos de evento:
    "evento" -> dict {"tipo", "detalle", "severidad"}   => topic {prefijo}/eventos
    "estado" -> dict con modo y estado de actuadores    => topic {prefijo}/estado

Modos de operación
------------------
AUTO (por defecto):
    - Ventilador ON si temp > umbral_temp; OFF si temp < umbral_temp - 2.
    - Bomba ON si suelo < 30 %; máx. 10 s por ciclo y 30 s de enfriamiento.
    - Luz de cultivo ON si luz < 30 % (OFF por encima de 35 %, histéresis).
MANUAL:
    - Bomba y ventilador solo obedecen comandos de la nube.
    - La luz de cultivo sigue siendo automática.
SIEMPRE:
    - Crítico si temp > 38 °C o suelo < 10 %: buzzer intermitente y evento
      CRITICO una sola vez al entrar en la condición (CRITICO_RESUELTO al salir).
"""

try:  # MicroPython: temporizadores con desbordamiento seguro
    from time import ticks_ms, ticks_diff
except ImportError:  # CPython (pruebas / simulador): equivalentes simples
    import time as _time

    def ticks_ms():
        """Milisegundos monótonos (equivalente a time.ticks_ms de MicroPython)."""
        return int(_time.monotonic() * 1000)

    def ticks_diff(a, b):
        """Diferencia a - b en ms (equivalente a time.ticks_diff)."""
        return a - b


# Modos
MODO_AUTO = "AUTO"
MODO_MANUAL = "MANUAL"

# Resultados de un comando (se envían en el ACK)
RESULTADO_OK = "OK"
RESULTADO_IGNORADO = "IGNORADO_MODO_AUTO"
RESULTADO_INVALIDO = "INVALIDO"

# Severidades de evento
INFO = "INFO"
ALERTA = "ALERTA"
CRITICO = "CRITICO"


class BusEventos:
    """Bus publicador/suscriptor mínimo.

    Ejemplo:
        bus.on("evento", funcion)     # suscribirse
        bus.emitir("evento", datos)   # todas las funciones suscritas se ejecutan
    """

    def __init__(self):
        self._suscriptores = {}

    def on(self, nombre, funcion):
        """Registra `funcion(dato)` para el evento `nombre`."""
        self._suscriptores.setdefault(nombre, []).append(funcion)

    def emitir(self, nombre, dato):
        """Notifica a todos los suscriptores. Un suscriptor que falla no
        impide que los demás reciban el evento."""
        for funcion in self._suscriptores.get(nombre, ()):
            try:
                funcion(dato)
            except Exception as error:  # noqa: BLE001 - robustez en el dispositivo
                print("[bus] error en suscriptor de", nombre, ":", error)


class Parametros:
    """Umbrales y tiempos de la lógica de control.

    Los valores por defecto coinciden con wokwi/config.py; `desde_config`
    permite construirlos a partir de ese módulo.
    """

    def __init__(self, umbral_temp=30, histeresis_temp=2, umbral_temp_min=20,
                 umbral_temp_max=45, umbral_suelo_seco=30, umbral_luz_baja=30,
                 histeresis_luz=5, riego_max_ms=10000, riego_enfriamiento_ms=30000,
                 temp_critica=38, suelo_critico=10):
        self.umbral_temp = umbral_temp
        self.histeresis_temp = histeresis_temp
        self.umbral_temp_min = umbral_temp_min
        self.umbral_temp_max = umbral_temp_max
        self.umbral_suelo_seco = umbral_suelo_seco
        self.umbral_luz_baja = umbral_luz_baja
        self.histeresis_luz = histeresis_luz
        self.riego_max_ms = riego_max_ms
        self.riego_enfriamiento_ms = riego_enfriamiento_ms
        self.temp_critica = temp_critica
        self.suelo_critico = suelo_critico

    @classmethod
    def desde_config(cls, cfg):
        """Crea los parámetros a partir del módulo config.py."""
        return cls(
            umbral_temp=cfg.UMBRAL_TEMP_DEFECTO,
            histeresis_temp=cfg.HISTERESIS_TEMP,
            umbral_temp_min=cfg.UMBRAL_TEMP_MIN,
            umbral_temp_max=cfg.UMBRAL_TEMP_MAX,
            umbral_suelo_seco=cfg.UMBRAL_SUELO_SECO,
            umbral_luz_baja=cfg.UMBRAL_LUZ_BAJA,
            histeresis_luz=cfg.HISTERESIS_LUZ,
            riego_max_ms=cfg.RIEGO_MAX_MS,
            riego_enfriamiento_ms=cfg.RIEGO_ENFRIAMIENTO_MS,
            temp_critica=cfg.TEMP_CRITICA,
            suelo_critico=cfg.SUELO_CRITICO,
        )


def normalizar_onoff(valor):
    """Interpreta un valor ON/OFF tolerante a variantes.

    Returns:
        True (ON), False (OFF) o None si el valor no es válido.
    """
    texto = str(valor).strip().upper()
    if texto in ("ON", "1", "TRUE", "ENCENDER"):
        return True
    if texto in ("OFF", "0", "FALSE", "APAGAR"):
        return False
    return None


class Controlador:
    """Cerebro del invernadero: aplica reglas, atiende comandos y emite eventos.

    Args:
        sensor_dht: objeto con `leer() -> (temp, hum) | None`.
        sensor_luz: objeto con `leer() -> int` (0–100 %).
        sensor_suelo: objeto con `leer() -> int` (0–100 %).
        bomba, ventilador, luz_cultivo: objetos con `establecer(bool)` y `encendido`.
        buzzer: objeto con `activar_alarma()`, `desactivar()`, `actualizar(ahora)`.
        pantalla: (opcional) objeto con `mostrar(lectura, estado, conectado)`.
        params: instancia de Parametros.
        reloj: función que devuelve milisegundos (inyectable para pruebas).
    """

    def __init__(self, sensor_dht, sensor_luz, sensor_suelo, bomba, ventilador,
                 luz_cultivo, buzzer, pantalla=None, params=None, reloj=ticks_ms):
        self.bus = BusEventos()
        self.p = params or Parametros()
        self._reloj = reloj

        self._dht = sensor_dht
        self._sensor_luz = sensor_luz
        self._sensor_suelo = sensor_suelo
        self._bomba = bomba
        self._ventilador = ventilador
        self._luz = luz_cultivo
        self._buzzer = buzzer
        self._pantalla = pantalla

        self.modo = MODO_AUTO
        self.umbral_temp = self.p.umbral_temp
        self.critico = False
        self.conectado = False          # lo actualiza main.py (solo para la OLED)
        self.lectura = {"temperatura": None, "humedad": None, "luz": None, "suelo": None}

        self._inicio_riego = None       # ms en que arrancó la bomba
        self._fin_riego = None          # ms en que se apagó (para el enfriamiento)
        self._error_dht = False         # evita repetir el evento de error del DHT

    # ------------------------------------------------------------------
    # Emisión de eventos
    # ------------------------------------------------------------------
    def _evento(self, tipo, detalle="", severidad=INFO):
        """Emite un evento de negocio (se publicará en {prefijo}/eventos)."""
        self.bus.emitir("evento", {"tipo": tipo, "detalle": detalle, "severidad": severidad})

    def _notificar_estado(self):
        """Emite el estado completo (se publicará en {prefijo}/estado)."""
        self.bus.emitir("estado", self.estado())
        self.refrescar_pantalla()

    def estado(self):
        """dict: modo, estado de actuadores y umbral actual."""
        return {
            "modo": self.modo,
            "bomba": self._bomba.encendido,
            "ventilador": self._ventilador.encendido,
            "luz_cultivo": self._luz.encendido,
            "umbral_temp": self.umbral_temp,
            "critico": self.critico,
        }

    def telemetria(self, ts=None):
        """dict con la última lectura, listo para publicar en {prefijo}/telemetria."""
        datos = dict(self.lectura)
        datos["ts"] = ts
        return datos

    def refrescar_pantalla(self):
        """Redibuja la OLED (si existe)."""
        if self._pantalla is not None:
            self._pantalla.mostrar(self.lectura, self.estado(), self.conectado)

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def iniciar(self):
        """Deja todo apagado y anuncia el arranque del sistema."""
        self._bomba.establecer(False)
        self._ventilador.establecer(False)
        self._luz.establecer(False)
        self._buzzer.desactivar()
        self._evento("INICIO_SISTEMA", "Invernadero iniciado en modo " + self.modo)
        self._notificar_estado()

    def ciclo_sensores(self):
        """Manejador del temporizador de sensores (cada 2 s).

        Lee todos los sensores, aplica las reglas automáticas y evalúa la
        condición crítica.
        """
        dht_valores = self._dht.leer()
        if dht_valores is None:
            if not self._error_dht:
                self._error_dht = True
                self._evento("ERROR_SENSOR", "Fallo de lectura del DHT22", ALERTA)
        else:
            self._error_dht = False
            self.lectura["temperatura"], self.lectura["humedad"] = dht_valores
        self.lectura["luz"] = self._sensor_luz.leer()
        self.lectura["suelo"] = self._sensor_suelo.leer()

        cambio = self._aplicar_reglas()
        cambio = self._evaluar_critico() or cambio
        if cambio:
            self._notificar_estado()
        else:
            self.refrescar_pantalla()

    def tick(self, ahora=None):
        """Tareas rápidas que se revisan en cada vuelta del bucle (~100 ms):
        límite de duración del riego y patrón intermitente del buzzer."""
        if ahora is None:
            ahora = self._reloj()
        if (self.modo == MODO_AUTO and self._bomba.encendido
                and self._inicio_riego is not None
                and ticks_diff(ahora, self._inicio_riego) >= self.p.riego_max_ms):
            self._cambiar_bomba(False, "Duración máxima de riego alcanzada")
            self._notificar_estado()
        self._buzzer.actualizar(ahora)

    # ------------------------------------------------------------------
    # Reglas automáticas
    # ------------------------------------------------------------------
    def _aplicar_reglas(self):
        """Aplica las reglas según el modo. Devuelve True si cambió algún actuador."""
        cambio = False
        temp = self.lectura["temperatura"]
        luz = self.lectura["luz"]
        suelo = self.lectura["suelo"]

        # Luz de cultivo: automática en AMBOS modos.
        if luz is not None:
            if luz < self.p.umbral_luz_baja and not self._luz.encendido:
                cambio = self._cambiar_luz(True, "Luz ambiente {}%".format(luz)) or cambio
            elif luz >= self.p.umbral_luz_baja + self.p.histeresis_luz and self._luz.encendido:
                cambio = self._cambiar_luz(False, "Luz ambiente {}%".format(luz)) or cambio

        if self.modo != MODO_AUTO:
            return cambio

        # Ventilador con histéresis.
        if temp is not None:
            if temp > self.umbral_temp and not self._ventilador.encendido:
                cambio = self._cambiar_ventilador(
                    True, "Temperatura {} C > umbral {} C".format(temp, self.umbral_temp)) or cambio
            elif temp < self.umbral_temp - self.p.histeresis_temp and self._ventilador.encendido:
                cambio = self._cambiar_ventilador(
                    False, "Temperatura {} C < {} C".format(
                        temp, self.umbral_temp - self.p.histeresis_temp)) or cambio

        # Bomba: riego por suelo seco con duración máxima y enfriamiento.
        if suelo is not None:
            if suelo < self.p.umbral_suelo_seco and not self._bomba.encendido:
                if self._enfriamiento_cumplido():
                    cambio = self._cambiar_bomba(True, "Suelo seco {}%".format(suelo)) or cambio
            elif suelo >= self.p.umbral_suelo_seco and self._bomba.encendido:
                cambio = self._cambiar_bomba(False, "Suelo recuperado {}%".format(suelo)) or cambio
        return cambio

    def _enfriamiento_cumplido(self):
        """True si ya pasaron los 30 s de espera desde el último riego."""
        if self._fin_riego is None:
            return True
        return ticks_diff(self._reloj(), self._fin_riego) >= self.p.riego_enfriamiento_ms

    def _evaluar_critico(self):
        """Detecta flancos de entrada/salida de la condición crítica.

        El evento CRITICO se emite UNA vez al entrar (no en cada ciclo) y
        CRITICO_RESUELTO al salir. Devuelve True si cambió la condición.
        """
        temp = self.lectura["temperatura"]
        suelo = self.lectura["suelo"]
        motivos = []
        if temp is not None and temp > self.p.temp_critica:
            motivos.append("temperatura {} C > {} C".format(temp, self.p.temp_critica))
        if suelo is not None and suelo < self.p.suelo_critico:
            motivos.append("suelo {}% < {}%".format(suelo, self.p.suelo_critico))
        ahora_critico = len(motivos) > 0

        if ahora_critico and not self.critico:
            self.critico = True
            self._buzzer.activar_alarma()
            self._evento("CRITICO", "; ".join(motivos), CRITICO)
            return True
        if not ahora_critico and self.critico:
            self.critico = False
            self._buzzer.desactivar()
            self._evento("CRITICO_RESUELTO", "Condiciones normalizadas", INFO)
            return True
        return False

    # ------------------------------------------------------------------
    # Cambios de actuadores (cada cambio genera un evento)
    # ------------------------------------------------------------------
    def _cambiar_bomba(self, encender, motivo):
        if self._bomba.encendido == encender:
            return False
        self._bomba.establecer(encender)
        ahora = self._reloj()
        if encender:
            self._inicio_riego = ahora
            self._evento("RIEGO_INICIADO", motivo)
        else:
            self._inicio_riego = None
            self._fin_riego = ahora
            self._evento("RIEGO_FINALIZADO", motivo)
        return True

    def _cambiar_ventilador(self, encender, motivo):
        if self._ventilador.encendido == encender:
            return False
        self._ventilador.establecer(encender)
        self._evento("VENTILADOR_ON" if encender else "VENTILADOR_OFF", motivo)
        return True

    def _cambiar_luz(self, encender, motivo):
        if self._luz.encendido == encender:
            return False
        self._luz.establecer(encender)
        self._evento("LUZ_CULTIVO_ON" if encender else "LUZ_CULTIVO_OFF", motivo)
        return True

    # ------------------------------------------------------------------
    # Comandos remotos (manejador del callback MQTT)
    # ------------------------------------------------------------------
    def manejar_comando(self, cmd, valor, origen="desconocido"):
        """Ejecuta un comando recibido desde la nube.

        Args:
            cmd: "bomba" | "ventilador" | "modo" | "umbral_temp".
            valor: "ON"/"OFF", "AUTO"/"MANUAL" o número.
            origen: quién envió el comando (solo para trazabilidad).

        Returns:
            str: "OK", "IGNORADO_MODO_AUTO" o "INVALIDO" (va en el ACK).
        """
        cmd = str(cmd).strip().lower()
        sufijo = " (origen: {})".format(origen)

        if cmd == "modo":
            nuevo = str(valor).strip().upper()
            if nuevo not in (MODO_AUTO, MODO_MANUAL):
                return RESULTADO_INVALIDO
            if nuevo != self.modo:
                anterior = self.modo
                self.modo = nuevo
                self._evento("CAMBIO_MODO", anterior + " -> " + nuevo + sufijo)
                if nuevo == MODO_AUTO:
                    # Si la bomba quedó encendida desde MANUAL, empieza a contar
                    # el límite de 10 s desde ahora.
                    if self._bomba.encendido:
                        self._inicio_riego = self._reloj()
                    self._aplicar_reglas()
                self._notificar_estado()
            return RESULTADO_OK

        if cmd == "umbral_temp":
            try:
                umbral = float(valor)
            except (TypeError, ValueError):
                return RESULTADO_INVALIDO
            if not (self.p.umbral_temp_min <= umbral <= self.p.umbral_temp_max):
                return RESULTADO_INVALIDO
            if umbral == int(umbral):
                umbral = int(umbral)
            if umbral != self.umbral_temp:
                anterior = self.umbral_temp
                self.umbral_temp = umbral
                self._evento("UMBRAL_ACTUALIZADO",
                             "umbral_temp {} -> {} C{}".format(anterior, umbral, sufijo))
                self._aplicar_reglas()
                self._notificar_estado()
            return RESULTADO_OK

        if cmd in ("bomba", "ventilador"):
            encender = normalizar_onoff(valor)
            if encender is None:
                return RESULTADO_INVALIDO
            if self.modo == MODO_AUTO:
                return RESULTADO_IGNORADO
            if cmd == "bomba":
                cambio = self._cambiar_bomba(encender, "Comando manual" + sufijo)
            else:
                cambio = self._cambiar_ventilador(encender, "Comando manual" + sufijo)
            if cambio:
                self._notificar_estado()
            return RESULTADO_OK

        return RESULTADO_INVALIDO
