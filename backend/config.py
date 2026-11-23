"""
config.py — Configuración del backend leída desde variables de entorno / .env.

Las credenciales de Adafruit IO NUNCA se escriben en el código: se cargan del
archivo `.env` (ignorado por git) con python-dotenv. Ver `.env.example`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Raíz del proyecto (carpeta que contiene backend/, sql/, wokwi/...)
RAIZ_PROYECTO = Path(__file__).resolve().parent.parent

# Feeds de Adafruit IO (keys exactas configuradas en la cuenta)
FEEDS_TELEMETRIA = {        # campo de la telemetría -> feed key
    "temperatura": "temperatura",
    "humedad": "humedad",
    "luz": "luz",
    "suelo": "suelo",
}
FEED_ESTADO = "estado"
FEEDS_COMANDOS = {          # feed key -> comando MQTT hacia el ESP32
    "modo": "modo",
    "bomba": "bomba",
    "ventilador": "ventilador",
    "umbral-temp": "umbral_temp",
}

# Unidades con que se guardan las lecturas en SQLite
UNIDADES = {"temperatura": "C", "humedad": "%", "luz": "%", "suelo": "%"}


@dataclass
class Config:
    """Parámetros de ejecución del backend."""

    aio_username: str = ""
    aio_key: str = ""
    mqtt_broker: str = "broker.hivemq.com"
    mqtt_port: int = 1883
    mqtt_prefijo: str = "utp/omar/invernadero"
    db_path: Path = field(default_factory=lambda: RAIZ_PROYECTO / "invernadero.db")
    log_path: Path = field(default_factory=lambda: RAIZ_PROYECTO / "logs" / "backend.log")
    schema_path: Path = field(default_factory=lambda: RAIZ_PROYECTO / "sql" / "schema.sql")
    # Límite del plan gratuito de Adafruit IO = 30 datos/min. Dejamos margen.
    aio_limite_por_minuto: int = 25
    # Segundos tras suscribirse en los que se ignoran mensajes de comandos
    # (valores viejos/retenidos que no deben ejecutarse al arrancar).
    aio_gracia_arranque_s: float = 5.0

    @property
    def nube_habilitada(self) -> bool:
        """True si hay credenciales de Adafruit IO configuradas."""
        return bool(self.aio_username and self.aio_key
                    and not self.aio_username.startswith("tu_"))

    def topic(self, subtopic: str) -> str:
        """Topic completo de HiveMQ: {prefijo}/{subtopic}."""
        return f"{self.mqtt_prefijo}/{subtopic}"


def cargar_config(env_path: Path | None = None) -> Config:
    """Carga .env (si existe) y construye la configuración.

    Rutas relativas de DB_PATH se resuelven respecto a la raíz del proyecto.
    """
    load_dotenv(env_path or RAIZ_PROYECTO / ".env")
    db_path = Path(os.getenv("DB_PATH", "invernadero.db"))
    if not db_path.is_absolute():
        db_path = RAIZ_PROYECTO / db_path
    return Config(
        aio_username=os.getenv("AIO_USERNAME", "").strip(),
        aio_key=os.getenv("AIO_KEY", "").strip(),
        mqtt_broker=os.getenv("MQTT_BROKER", "broker.hivemq.com").strip(),
        mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        mqtt_prefijo=os.getenv("MQTT_PREFIJO", "utp/omar/invernadero").strip().rstrip("/"),
        db_path=db_path,
    )
