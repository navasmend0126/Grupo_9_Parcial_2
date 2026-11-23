# Flujo de datos — Invernadero Inteligente

Dos flujos principales:

1. **Ascendente** (sensor → nube): lectura, reglas, eventos, telemetría, SQLite y dashboard.
2. **Descendente** (nube → actuador): comando del dashboard, backend, ESP32, relé y ACK.

## 1. Sensor → SQLite → Adafruit IO

![Flujo de telemetría](img/flujo_telemetria.png)

```mermaid
sequenceDiagram
    autonumber
    participant S as Sensores<br/>(DHT22, LDR, pot.)
    participant C as ESP32<br/>controlador.py
    participant H as HiveMQ
    participant B as Backend<br/>main.py
    participant D as SQLite
    participant A as Adafruit IO

    loop cada 2 s (temporizador no bloqueante)
        C->>S: leer()
        S-->>C: temp, hum, luz, suelo
        C->>C: aplicar reglas AUTO + evaluar crítico
        opt cambió un actuador o la condición crítica
            C->>H: {prefijo}/eventos (RIEGO_INICIADO, CRITICO, ...)
            C->>H: {prefijo}/estado
            H->>B: evento / estado
            B->>D: INSERT INTO eventos
            B->>A: feed estado (solo si cambió el texto)
        end
    end
    loop cada 15 s
        C->>H: {prefijo}/telemetria {"temperatura":..,"humedad":..,"luz":..,"suelo":..,"ts":..}
        H->>B: telemetría
        B->>D: INSERT INTO lecturas (4 filas, una por sensor)
        B->>A: feeds temperatura, humedad, luz, suelo (limitador ≤ 25/min)
        A-->>A: el dashboard se actualiza en tiempo real
    end
```

**Presupuesto de datos hacia Adafruit IO:** 4 feeds × 4 mensajes/min = **16 datos/min**, más el feed `estado` solo cuando cambia. El `LimitadorTasa` del backend corta en 25/min, por debajo del límite de 30/min del plan gratis. Si no hay cupo, el estado queda pendiente y se envía en cuanto se libera la ventana.

## 2. Dashboard → actuador (con ACK)

![Flujo de comando](img/flujo_comando.png)

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuario
    participant A as Adafruit IO<br/>dashboard
    participant N as Backend<br/>nube.py
    participant B as Backend<br/>main.py
    participant D as SQLite
    participant H as HiveMQ
    participant C as ESP32<br/>controlador.py
    participant R as Relé / LED

    U->>A: toggle "bomba" = ON
    A->>N: MQTT {usuario}/feeds/bomba "ON"
    N->>N: ¿valor viejo al arrancar? → ignorar<br/>traducir_comando("bomba","ON")
    N->>B: on_comando("bomba","ON")
    B->>D: INSERT comandos (resultado = ENVIADO) → id
    B->>H: {prefijo}/comandos {"cmd":"bomba","valor":"ON","origen":"adafruit_io","id":7}
    H->>C: callback MQTT
    alt modo MANUAL
        C->>R: Pin(26) = 1 → COM-NO cierra, LED azul ON
        C->>H: {prefijo}/eventos RIEGO_INICIADO + {prefijo}/estado
        C->>H: {prefijo}/comandos/ack resultado = OK
    else modo AUTO
        C->>H: {prefijo}/comandos/ack resultado = IGNORADO_MODO_AUTO
    end
    H->>B: ACK
    B->>D: UPDATE comandos SET resultado = ... WHERE id = 7
    B->>A: feed estado "MANUAL | Bomba ON | ..."
```

## 3. Traducción de feeds de comando

| Feed de Adafruit IO | Valor recibido | Comando MQTT al ESP32 |
|---|---|---|
| `modo` | `AUTO` / `MANUAL` | `{"cmd":"modo","valor":"AUTO"\|"MANUAL"}` |
| `bomba` | `ON` / `OFF` (también `1`/`0`) | `{"cmd":"bomba","valor":"ON"\|"OFF"}` |
| `ventilador` | `ON` / `OFF` (también `1`/`0`) | `{"cmd":"ventilador","valor":"ON"\|"OFF"}` |
| `umbral-temp` | número (20–45) | `{"cmd":"umbral_temp","valor":32}` |

Los valores no reconocidos se reenvían tal cual: el ESP32 responde `INVALIDO` y queda registrado en `comandos`.

## 4. Resultados posibles de un comando

| Resultado | Significado |
|---|---|
| `ENVIADO` | El backend lo publicó y aún no llega el ACK. |
| `OK` | El dispositivo lo ejecutó (o ya estaba en ese estado). |
| `IGNORADO_MODO_AUTO` | Comando de bomba/ventilador recibido en modo AUTO. |
| `INVALIDO` | Comando o valor fuera de rango o desconocido. |
| `ERROR_ENVIO` | El backend no tenía sesión con HiveMQ al enviarlo. |

## 5. Condición crítica

```mermaid
stateDiagram-v2
    [*] --> Normal
    Normal --> Critico: temp > 38 °C o suelo < 10 %\n/ evento CRITICO + buzzer intermitente
    Critico --> Critico: siguen las condiciones\n/ sin eventos repetidos
    Critico --> Normal: temp ≤ 38 y suelo ≥ 10\n/ evento CRITICO_RESUELTO + buzzer OFF
```
