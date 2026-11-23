"""Configuración común de pytest.

Agrega la raíz del proyecto (para `import backend...`) y la carpeta `wokwi/`
(para importar `controlador.py`, que no depende de `machine`) al sys.path.
"""

import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "wokwi"))

from backend.db import BaseDatos  # noqa: E402


@pytest.fixture
def db():
    """Base de datos SQLite en memoria con el esquema aplicado."""
    base = BaseDatos(":memory:")
    yield base
    base.cerrar()
