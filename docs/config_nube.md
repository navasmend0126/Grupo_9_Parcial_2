# Configuración de la plataforma en la nube — Adafruit IO

Plataforma elegida: **Adafruit IO** (plan gratuito). El backend se conecta por **MQTT sobre TLS** (`io.adafruit.com:8883`) con la librería oficial `adafruit-io`.

## 1. Límites del plan gratuito (y cómo se respetan)

| Límite | Valor | Uso en este proyecto |
|---|---|---|
| Datos por minuto | 30 | 16/min de telemetría (4 feeds cada 15 s) + `estado` solo al cambiar. El `LimitadorTasa` de `backend/nube.py` corta en **25/min**. |
| Feeds | 10 | Se usan **9** (5 de datos + 4 de comandos). |
| Dashboards | 5 | Se usa 1. |
| Retención | 30 días | El histórico completo queda en SQLite local. |

Si se supera el límite, Adafruit IO publica un aviso en `{usuario}/throttle`. El backend está suscrito a ese topic y a `{usuario}/errors`, y registra todo en `logs/backend.log`.

## 2. Credenciales

1. Entrar a <https://io.adafruit.com> → ícono de **llave** (*My Key*).
2. Copiar *Username* y *Active Key* al archivo `.env` (a partir de `.env.example`):
   ```
   AIO_USERNAME=tu_usuario_adafruit
   AIO_KEY=tu_clave_aio
   ```
3. `.env` está en `.gitignore`: **nunca** se sube al repositorio. El ESP32 no conoce estas credenciales.

## 3. Feeds (las *keys* deben ser exactamente estas)

*Feeds → New Feed*. Adafruit IO genera la *key* a partir del nombre (minúsculas y guiones). Hay que revisar que la **key** coincida.

| Key del feed | Dirección | Quién publica | Contenido |
|---|---|---|---|
| `temperatura` | dispositivo → nube | backend, cada 15 s | °C (número) |
| `humedad` | dispositivo → nube | backend, cada 15 s | % (número) |
| `luz` | dispositivo → nube | backend, cada 15 s | % (número) |
| `suelo` | dispositivo → nube | backend, cada 15 s | % (número) |
| `estado` | dispositivo → nube | backend, solo al cambiar | texto, p. ej. `AUTO \| Bomba OFF \| Vent ON \| Luz OFF \| Umbral 30C` |
| `modo` | nube → dispositivo | dashboard | `AUTO` / `MANUAL` |
| `bomba` | nube → dispositivo | dashboard | `ON` / `OFF` |
| `ventilador` | nube → dispositivo | dashboard | `ON` / `OFF` |
| `umbral-temp` | nube → dispositivo | dashboard | número 20–45 |

> El feed `umbral-temp` lleva **guion** (las keys de Adafruit no admiten `_`); el backend lo traduce al comando `umbral_temp`.

## 4. Dashboard

*Dashboards → New Dashboard* → "Invernadero Inteligente" → ícono de engranaje → *Create New Block*.

| Bloque | Feed | Configuración sugerida |
|---|---|---|
| **Gauge** | `temperatura` | Min 0, Max 50, *Label* "°C", *High warning* 38 |
| **Gauge** | `humedad` | Min 0, Max 100, "%" |
| **Gauge** | `luz` | Min 0, Max 100, "%", *Low warning* 30 |
| **Gauge** | `suelo` | Min 0, Max 100, "%", *Low warning* 30 |
| **Line Chart** | `temperatura`, `humedad`, `suelo` | *Hours of history* 1 (tendencia en tiempo real) |
| **Text** | `estado` | Tamaño de fuente mediano |
| **Toggle** | `modo` | *Button On Text* `MANUAL`, *Button On Value* `MANUAL`; *Button Off Text* `AUTO`, *Button Off Value* `AUTO` |
| **Toggle** | `bomba` | On Value `ON`, Off Value `OFF` |
| **Toggle** | `ventilador` | On Value `ON`, Off Value `OFF` |
| **Slider** | `umbral-temp` | Min 20, Max 45, Step 1 |
| **Stream** (opcional) | `estado` | Historial de cambios de estado |

Sugerencia de layout: los 4 gauges arriba, el gráfico de líneas en el centro y a la derecha el texto de estado con los controles (modo, bomba, ventilador, umbral).

**Nota sobre los toggles de bomba y ventilador:** en modo AUTO el dispositivo responde `IGNORADO_MODO_AUTO`, y el toggle puede quedar visualmente en ON aunque el relé no cambie. La fuente de verdad es el bloque **Text `estado`**, que refleja el estado real que reporta el ESP32. No se reescriben los feeds de comando desde el backend para no gastar cupo de datos ni provocar bucles de comandos.

## 5. Comportamiento del backend con Adafruit IO

| Aspecto | Implementación (`backend/nube.py`) |
|---|---|
| Conexión | `MQTTClient(usuario, key)` → TLS 8883, hilo en segundo plano (`loop_background`). |
| Reconexión | Reintentos automáticos de paho con backoff de 2 a 60 s. Con credenciales malas, se registra el error (rc=5) sin detener el programa. |
| Suscripciones | `modo`, `bomba`, `ventilador`, `umbral-temp`, más `{usuario}/throttle` y `{usuario}/errors`. |
| Valores viejos al arrancar | Se **ignoran** los mensajes de comandos recibidos en los primeros 5 s tras suscribirse (`aio_gracia_arranque_s`). Así no se ejecutan órdenes antiguas. No se pide el último valor (`/get`), de modo que el ESP32 siempre arranca en AUTO. |
| Publicación de estado | Solo si el texto cambió. Si no hay cupo en el limitador, queda pendiente y se reintenta cada segundo. |
| Sin credenciales | Si `.env` no tiene `AIO_USERNAME`/`AIO_KEY` (o se usa `--sin-nube`), el backend funciona igual con HiveMQ + SQLite. |

## 6. Verificación rápida

1. Ejecutar `python -m backend.main` y buscar en el log `Conectado a Adafruit IO; suscrito a feeds ...`.
2. Con el ESP32 (o `python -m backend.simulador_dispositivo`) corriendo, los gauges deben moverse cada 15 s.
3. Cambiar el toggle `modo` a MANUAL: en el log aparece `Comando desde Adafruit IO: feed 'modo'='MANUAL'` y luego `ACK: modo=MANUAL -> OK`. La OLED de Wokwi muestra `INVERNADERO MAN.`.
4. En Adafruit IO, *Feeds → (feed) → Monitor/Live data* muestra los valores en vivo y los errores.
