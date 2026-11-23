-- =====================================================================
-- consultas.sql — Consultas de históricos del Invernadero IoT
--
-- Este archivo es la FUENTE ÚNICA de las consultas: backend/db.py lo lee y
-- ejecuta cada bloque por su nombre (línea "-- name: <nombre>"), y
-- backend/historico.py las expone como comandos de consola.
--
-- Usan parámetros con nombre (:n, :sensor, :desde, ...). Un parámetro NULL
-- significa "sin filtro". Para probarlas en la consola de sqlite3:
--     sqlite3 invernadero.db
--     .param set :n 10
--     .param set :sensor NULL
--     (y pegar la consulta)
-- =====================================================================

-- name: ultimas_lecturas
-- Últimas N lecturas (opcionalmente de un solo sensor).
SELECT id, sensor, valor, unidad, timestamp
FROM lecturas
WHERE (:sensor IS NULL OR sensor = :sensor)
ORDER BY timestamp DESC, id DESC
LIMIT :n;

-- name: estadisticas_sensor
-- Promedio, mínimo y máximo por sensor en un rango de fechas
-- (:desde y :hasta en formato 'YYYY-MM-DD HH:MM:SS').
SELECT sensor,
       COUNT(*)            AS muestras,
       ROUND(AVG(valor), 2) AS promedio,
       MIN(valor)          AS minimo,
       MAX(valor)          AS maximo,
       MIN(unidad)         AS unidad
FROM lecturas
WHERE timestamp BETWEEN :desde AND :hasta
  AND (:sensor IS NULL OR sensor = :sensor)
GROUP BY sensor
ORDER BY sensor;

-- name: promedio_por_hora
-- Evolución horaria del promedio de un sensor en un rango de fechas.
SELECT strftime('%Y-%m-%d %H:00', timestamp) AS hora,
       sensor,
       ROUND(AVG(valor), 2) AS promedio,
       COUNT(*)            AS muestras
FROM lecturas
WHERE timestamp BETWEEN :desde AND :hasta
  AND (:sensor IS NULL OR sensor = :sensor)
GROUP BY hora, sensor
ORDER BY hora, sensor;

-- name: eventos_filtrados
-- Eventos filtrados por tipo y/o severidad (más recientes primero).
SELECT id, tipo, severidad, detalle, timestamp
FROM eventos
WHERE (:tipo IS NULL OR tipo = :tipo)
  AND (:severidad IS NULL OR severidad = :severidad)
ORDER BY timestamp DESC, id DESC
LIMIT :n;

-- name: conteo_eventos
-- Cantidad de eventos por tipo y severidad en un rango de fechas.
SELECT tipo, severidad, COUNT(*) AS cantidad, MAX(timestamp) AS ultimo
FROM eventos
WHERE timestamp BETWEEN :desde AND :hasta
GROUP BY tipo, severidad
ORDER BY cantidad DESC, tipo;

-- name: riegos_del_dia
-- Inicio y fin de cada riego de una fecha (:fecha = 'YYYY-MM-DD').
SELECT id, tipo, detalle, timestamp
FROM eventos
WHERE tipo IN ('RIEGO_INICIADO', 'RIEGO_FINALIZADO')
  AND date(timestamp) = :fecha
ORDER BY timestamp, id;

-- name: total_riegos_del_dia
-- Número de riegos iniciados en una fecha.
SELECT COUNT(*) AS total_riegos
FROM eventos
WHERE tipo = 'RIEGO_INICIADO'
  AND date(timestamp) = :fecha;

-- name: ultimos_comandos
-- Últimos N comandos enviados al dispositivo y su resultado.
SELECT id, comando, valor, origen, resultado, timestamp
FROM comandos
ORDER BY timestamp DESC, id DESC
LIMIT :n;

-- name: resumen_comandos
-- Cuántos comandos hubo por tipo y resultado.
SELECT comando, resultado, COUNT(*) AS cantidad
FROM comandos
GROUP BY comando, resultado
ORDER BY comando, resultado;
