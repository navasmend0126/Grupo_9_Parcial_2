"""
db.py — Capa de persistencia SQLite del invernadero.

¿Por qué SQLite vive en el backend y no en el ESP32?
    El ESP32 con MicroPython no tiene un motor SQL (no existe sqlite3 en
    MicroPython), tiene poca RAM (~100 KB libres) y una flash que se desgasta
    con escrituras frecuentes. Por eso el dispositivo solo PUBLICA datos por
    MQTT y el backend en la PC los persiste.

Responsabilidades:
    * Crear la BD aplicando sql/schema.sql si no existe.
    * Insertar lecturas (una fila por sensor), eventos y comandos.
    * Ejecutar las consultas de históricos definidas en sql/consultas.sql.

Es segura para hilos: los callbacks de paho-mqtt y de Adafruit IO corren en
hilos distintos, así que todas las operaciones se serializan con un Lock.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.config import RAIZ_PROYECTO, UNIDADES

log = logging.getLogger(__name__)

RUTA_SCHEMA = RAIZ_PROYECTO / "sql" / "schema.sql"
RUTA_CONSULTAS = RAIZ_PROYECTO / "sql" / "consultas.sql"


def ahora_texto() -> str:
    """Hora local actual en el formato que usa la BD ('YYYY-MM-DD HH:MM:SS')."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cargar_consultas(ruta: Path = RUTA_CONSULTAS) -> dict[str, str]:
    """Lee consultas.sql y devuelve {nombre: sql} usando los marcadores
    `-- name: <nombre>`."""
    texto = ruta.read_text(encoding="utf-8")
    consultas: dict[str, str] = {}
    partes = re.split(r"^--\s*name:\s*(\w+)\s*$", texto, flags=re.MULTILINE)
    # partes = [preámbulo, nombre1, cuerpo1, nombre2, cuerpo2, ...]
    for nombre, cuerpo in zip(partes[1::2], partes[2::2]):
        lineas = [l for l in cuerpo.splitlines() if not l.strip().startswith("--")]
        consultas[nombre] = "\n".join(lineas).strip()
    return consultas


class BaseDatos:
    """Acceso a la base SQLite del invernadero.

    Args:
        ruta: archivo .db (usa ":memory:" en pruebas).
        schema: ruta de schema.sql.
    """

    def __init__(self, ruta: Path | str, schema: Path = RUTA_SCHEMA):
        self.ruta = str(ruta)
        if self.ruta != ":memory:":
            Path(self.ruta).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(self.ruta, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._aplicar_schema(schema)
        self._consultas = cargar_consultas()

    # ------------------------------------------------------------------
    def _aplicar_schema(self, schema: Path) -> None:
        """Ejecuta schema.sql (idempotente gracias a IF NOT EXISTS)."""
        with self._lock:
            self._con.executescript(schema.read_text(encoding="utf-8"))
            self._con.commit()
        log.info("Base de datos lista en %s", self.ruta)

    def cerrar(self) -> None:
        """Cierra la conexión."""
        with self._lock:
            self._con.close()

    def _ejecutar(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._con.execute(sql, params)
            self._con.commit()
            return cursor

    # ------------------------------------------------------------------
    # Inserciones
    # ------------------------------------------------------------------
    def insertar_lectura(self, sensor: str, valor: float, unidad: str | None = None,
                         timestamp: str | None = None) -> int:
        """Inserta una lectura de un sensor. Devuelve su id."""
        unidad = unidad or UNIDADES.get(sensor, "")
        cursor = self._ejecutar(
            "INSERT INTO lecturas (sensor, valor, unidad, timestamp) VALUES (?, ?, ?, ?)",
            (sensor, float(valor), unidad, timestamp or ahora_texto()),
        )
        return cursor.lastrowid

    def insertar_telemetria(self, datos: dict[str, Any], timestamp: str | None = None) -> int:
        """Guarda un mensaje de telemetría como una fila por sensor.

        Ignora campos desconocidos (p. ej. `ts`) y valores nulos o no numéricos.
        Devuelve la cantidad de filas insertadas.
        """
        timestamp = timestamp or ahora_texto()
        filas = 0
        for sensor in UNIDADES:
            valor = datos.get(sensor)
            if isinstance(valor, (int, float)) and not isinstance(valor, bool):
                self.insertar_lectura(sensor, valor, UNIDADES[sensor], timestamp)
                filas += 1
        return filas

    def insertar_evento(self, tipo: str, detalle: str = "", severidad: str = "INFO",
                        timestamp: str | None = None) -> int:
        """Registra un evento del dispositivo. Devuelve su id."""
        severidad = severidad if severidad in ("INFO", "ALERTA", "CRITICO") else "INFO"
        cursor = self._ejecutar(
            "INSERT INTO eventos (tipo, detalle, severidad, timestamp) VALUES (?, ?, ?, ?)",
            (tipo, detalle, severidad, timestamp or ahora_texto()),
        )
        return cursor.lastrowid

    def insertar_comando(self, comando: str, valor: Any, origen: str,
                         resultado: str = "ENVIADO", timestamp: str | None = None) -> int:
        """Registra un comando enviado al dispositivo. Devuelve su id."""
        cursor = self._ejecutar(
            "INSERT INTO comandos (comando, valor, origen, resultado, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (comando, str(valor), origen, resultado, timestamp or ahora_texto()),
        )
        return cursor.lastrowid

    def actualizar_resultado_comando(self, id_comando: int, resultado: str) -> bool:
        """Actualiza el resultado de un comando cuando llega su ACK.

        Returns:
            True si existía el comando.
        """
        cursor = self._ejecutar(
            "UPDATE comandos SET resultado = ? WHERE id = ?", (resultado, id_comando))
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Consultas de históricos (definidas en sql/consultas.sql)
    # ------------------------------------------------------------------
    def consultar(self, nombre: str, **params: Any) -> list[dict[str, Any]]:
        """Ejecuta la consulta `nombre` de consultas.sql y devuelve filas como dicts."""
        sql = self._consultas[nombre]
        with self._lock:
            filas = self._con.execute(sql, params).fetchall()
        return [dict(f) for f in filas]

    def ultimas_lecturas(self, n: int = 20, sensor: str | None = None) -> list[dict]:
        """Últimas N lecturas (opcionalmente de un solo sensor)."""
        return self.consultar("ultimas_lecturas", n=n, sensor=sensor)

    def estadisticas(self, desde: str, hasta: str, sensor: str | None = None) -> list[dict]:
        """Promedio/mín/máx por sensor en el rango [desde, hasta]."""
        return self.consultar("estadisticas_sensor", desde=desde, hasta=hasta, sensor=sensor)

    def promedio_por_hora(self, desde: str, hasta: str, sensor: str | None = None) -> list[dict]:
        """Promedio horario por sensor en el rango [desde, hasta]."""
        return self.consultar("promedio_por_hora", desde=desde, hasta=hasta, sensor=sensor)

    def eventos(self, tipo: str | None = None, severidad: str | None = None,
                n: int = 50) -> list[dict]:
        """Eventos filtrados por tipo y/o severidad."""
        return self.consultar("eventos_filtrados", tipo=tipo, severidad=severidad, n=n)

    def conteo_eventos(self, desde: str, hasta: str) -> list[dict]:
        """Cantidad de eventos por tipo/severidad en un rango."""
        return self.consultar("conteo_eventos", desde=desde, hasta=hasta)

    def riegos_del_dia(self, fecha: str) -> tuple[int, list[dict]]:
        """Riegos de una fecha 'YYYY-MM-DD'.

        Returns:
            (total de riegos iniciados, lista de eventos de inicio/fin)
        """
        total = self.consultar("total_riegos_del_dia", fecha=fecha)[0]["total_riegos"]
        return total, self.consultar("riegos_del_dia", fecha=fecha)

    def ultimos_comandos(self, n: int = 20) -> list[dict]:
        """Últimos N comandos y su resultado."""
        return self.consultar("ultimos_comandos", n=n)

    def resumen_comandos(self) -> list[dict]:
        """Cantidad de comandos por tipo y resultado."""
        return self.consultar("resumen_comandos")
