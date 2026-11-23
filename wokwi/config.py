"""
config.py — Configuración central del dispositivo (ESP32 + MicroPython en Wokwi).

Todo valor "mágico" del sistema (pines, tiempos, umbrales, MQTT) vive aquí para
que el resto de los módulos no tenga constantes repetidas. Si cambias el prefijo
MQTT aquí, recuerda cambiar también MQTT_PREFIJO en el archivo .env del backend.

NOTA DE SEGURIDAD: el ESP32 NO conoce credenciales de Adafruit IO. Solo habla con
el broker público HiveMQ; el backend es quien se autentica contra la nube.
"""

# ---------------------------------------------------------------------------
# Red WiFi (Wokwi ofrece un punto de acceso virtual abierto)
# ---------------------------------------------------------------------------
WIFI_SSID = "Wokwi-GUEST"
WIFI_PASSWORD = ""

# ---------------------------------------------------------------------------
# MQTT (broker público HiveMQ, sin TLS ni usuario)
# ---------------------------------------------------------------------------
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_PREFIJO = "utp/omar/invernadero"   # Debe coincidir con MQTT_PREFIJO del .env
MQTT_KEEPALIVE = 60                     # segundos
MQTT_CLIENT_ID_BASE = "invernadero-esp32-"  # se le añade un sufijo aleatorio

# ---------------------------------------------------------------------------
# Pines GPIO (ver tabla en README.md)
# Los analógicos usan SOLO ADC1 (GPIO32-39) porque ADC2 no funciona con WiFi.
# ---------------------------------------------------------------------------
PIN_DHT = 15            # DHT22: temperatura + humedad del aire
PIN_LDR = 34            # Fotorresistencia (salida AO) -> ADC1_CH6
PIN_SUELO = 35          # Potenciómetro que simula humedad de suelo -> ADC1_CH7
PIN_BOMBA = 26          # Relé de la bomba de riego (+ LED azul por contacto NO)
PIN_VENTILADOR = 27     # Relé del ventilador (+ LED amarillo por contacto NO)
PIN_LUZ_CULTIVO = 25    # LED blanco = luz de cultivo
PIN_BUZZER = 14         # Buzzer de alerta crítica (PWM)
PIN_I2C_SDA = 21        # OLED SSD1306
PIN_I2C_SCL = 22
OLED_DIRECCION = 0x3C
OLED_ANCHO = 128
OLED_ALTO = 64

# El módulo de relé de Wokwi (configuración por defecto) se activa con IN=HIGH:
# COM se conecta a NO y se enciende el LED indicador (verificado en la
# simulación). Si usas un relé activo en bajo, cambia esto a False.
RELE_ACTIVO_ALTO = True

# ---------------------------------------------------------------------------
# Temporización del bucle orientado a eventos (milisegundos)
# ---------------------------------------------------------------------------
PERIODO_BUCLE_MS = 100            # el bucle principal solo despacha cada ~100 ms
INTERVALO_SENSORES_MS = 2000      # lectura de sensores
INTERVALO_TELEMETRIA_MS = 15000   # publicación de telemetría
INTERVALO_PING_MS = 30000         # ping MQTT para mantener viva la sesión
REINTENTO_WIFI_MS = 10000         # espera entre reintentos de WiFi
REINTENTO_MQTT_MS = 5000          # espera entre reintentos de MQTT

# ---------------------------------------------------------------------------
# Lógica de control (ver controlador.py)
# ---------------------------------------------------------------------------
UMBRAL_TEMP_DEFECTO = 30          # °C: ventilador ON por encima
HISTERESIS_TEMP = 2               # °C: ventilador OFF por debajo de umbral - 2
UMBRAL_TEMP_MIN = 20              # rango válido para el comando umbral_temp
UMBRAL_TEMP_MAX = 45
UMBRAL_SUELO_SECO = 30            # %: regar por debajo
UMBRAL_LUZ_BAJA = 30              # %: luz de cultivo ON por debajo
HISTERESIS_LUZ = 5                # %: luz de cultivo OFF por encima de 30 + 5
RIEGO_MAX_MS = 10000              # máximo 10 s de bomba por ciclo (AUTO)
RIEGO_ENFRIAMIENTO_MS = 30000     # 30 s de espera antes de volver a regar
TEMP_CRITICA = 38                 # °C: condición crítica
SUELO_CRITICO = 10                # %: condición crítica

# Buzzer intermitente
BUZZER_FRECUENCIA = 2000          # Hz
BUZZER_PERIODO_MS = 400           # alterna sonido/silencio cada 400 ms
