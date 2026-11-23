-- =====================================================================
-- schema.sql — Esquema de la base de datos SQLite del Invernadero IoT
-- El backend lo aplica automáticamente al arrancar (CREATE ... IF NOT EXISTS).
-- Manualmente:  sqlite3 invernadero.db < sql/schema.sql
--
-- Los timestamps se guardan como TEXT en hora local con formato
-- 'YYYY-MM-DD HH:MM:SS' (ordenable y comparable como texto).
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Lecturas de sensores: UNA fila por sensor y por mensaje de telemetría.
-- sensor: temperatura | humedad | luz | suelo
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lecturas (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor    TEXT    NOT NULL,
    valor     REAL    NOT NULL,
    unidad    TEXT    NOT NULL,
    timestamp TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_lecturas_timestamp ON lecturas (timestamp);
CREATE INDEX IF NOT EXISTS idx_lecturas_sensor_ts ON lecturas (sensor, timestamp);

-- ---------------------------------------------------------------------
-- Eventos emitidos por el dispositivo (RIEGO_INICIADO, CRITICO, ...).
-- severidad: INFO | ALERTA | CRITICO
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eventos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo      TEXT    NOT NULL,
    detalle   TEXT,
    severidad TEXT    NOT NULL DEFAULT 'INFO'
              CHECK (severidad IN ('INFO', 'ALERTA', 'CRITICO')),
    timestamp TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_eventos_timestamp ON eventos (timestamp);
CREATE INDEX IF NOT EXISTS idx_eventos_tipo_ts   ON eventos (tipo, timestamp);
CREATE INDEX IF NOT EXISTS idx_eventos_severidad ON eventos (severidad);

-- ---------------------------------------------------------------------
-- Comandos enviados al dispositivo y su resultado (según el ACK).
-- comando:   bomba | ventilador | modo | umbral_temp
-- origen:    adafruit_io | simulador | ...
-- resultado: ENVIADO (sin ACK aún) | OK | IGNORADO_MODO_AUTO | INVALIDO
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS comandos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    comando   TEXT    NOT NULL,
    valor     TEXT,
    origen    TEXT    NOT NULL DEFAULT 'desconocido',
    resultado TEXT    NOT NULL DEFAULT 'ENVIADO',
    timestamp TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_comandos_timestamp  ON comandos (timestamp);
CREATE INDEX IF NOT EXISTS idx_comandos_comando_ts ON comandos (comando, timestamp);
