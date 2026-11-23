"""
historico.py — Consultas de históricos del invernadero desde la consola.

Ejemplos (desde la raíz del proyecto):
    python -m backend.historico lecturas -n 20
    python -m backend.historico lecturas -n 10 --sensor temperatura
    python -m backend.historico estadisticas --desde 2026-11-23 --hasta 2026-11-24
    python -m backend.historico estadisticas --desde "2026-11-23 08:00" --por-hora --sensor suelo
    python -m backend.historico eventos --tipo CRITICO
    python -m backend.historico eventos --severidad ALERTA -n 10
    python -m backend.historico eventos --resumen --desde 2026-11-23
    python -m backend.historico riegos                 # hoy
    python -m backend.historico riegos --fecha 2026-11-23
    python -m backend.historico comandos -n 15
    python -m backend.historico comandos --resumen

Todas las consultas están en sql/consultas.sql.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from backend.config import cargar_config
from backend.db import BaseDatos

SENSORES = ["temperatura", "humedad", "luz", "suelo"]
SEVERIDADES = ["INFO", "ALERTA", "CRITICO"]


# ----------------------------------------------------------------------
# Formato de salida
# ----------------------------------------------------------------------
def formatear_tabla(filas: Sequence[dict[str, Any]], columnas: Sequence[str] | None = None,
                    ancho_max: int = 60) -> str:
    """Devuelve las filas como una tabla de texto con bordes.

    Args:
        filas: lista de diccionarios.
        columnas: orden de columnas (por defecto, las claves de la primera fila).
        ancho_max: recorta celdas más largas que esto.
    """
    if not filas:
        return "(sin resultados)"
    columnas = list(columnas or filas[0].keys())

    def celda(valor: Any) -> str:
        if valor is None:
            texto = "-"
        elif isinstance(valor, float):
            texto = f"{valor:g}"
        else:
            texto = str(valor)
        return texto if len(texto) <= ancho_max else texto[: ancho_max - 3] + "..."

    datos = [[celda(f.get(c)) for c in columnas] for f in filas]
    anchos = [max(len(c), *(len(fila[i]) for fila in datos)) for i, c in enumerate(columnas)]
    separador = "+" + "+".join("-" * (a + 2) for a in anchos) + "+"

    def linea(valores: Sequence[str]) -> str:
        return "| " + " | ".join(v.ljust(a) for v, a in zip(valores, anchos)) + " |"

    salida = [separador, linea(columnas), separador.replace("-", "=")]
    salida += [linea(fila) for fila in datos]
    salida.append(separador)
    salida.append(f"{len(filas)} fila(s)")
    return "\n".join(salida)


def normalizar_fecha(texto: str, fin_de_dia: bool = False) -> str:
    """Acepta 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM' o 'YYYY-MM-DD HH:MM:SS' y
    devuelve 'YYYY-MM-DD HH:MM:SS'. Una fecha sola en `--hasta` incluye el día
    completo (23:59:59)."""
    for formato in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            momento = datetime.strptime(texto.strip(), formato)
        except ValueError:
            continue
        if formato == "%Y-%m-%d" and fin_de_dia:
            momento = momento + timedelta(days=1) - timedelta(seconds=1)
        return momento.strftime("%Y-%m-%d %H:%M:%S")
    raise argparse.ArgumentTypeError(f"fecha inválida: {texto!r} (use YYYY-MM-DD [HH:MM[:SS]])")


def _rango(args: argparse.Namespace) -> tuple[str, str]:
    """Rango [desde, hasta]; por defecto, las últimas 24 horas."""
    ahora = datetime.now()
    desde = normalizar_fecha(args.desde) if args.desde else \
        (ahora - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    hasta = normalizar_fecha(args.hasta, fin_de_dia=True) if args.hasta else \
        ahora.strftime("%Y-%m-%d %H:%M:%S")
    return desde, hasta


# ----------------------------------------------------------------------
# Subcomandos
# ----------------------------------------------------------------------
def cmd_lecturas(db: BaseDatos, args: argparse.Namespace) -> str:
    filas = db.ultimas_lecturas(args.n, args.sensor)
    titulo = f"Últimas {args.n} lecturas" + (f" de {args.sensor}" if args.sensor else "")
    return titulo + "\n" + formatear_tabla(filas)


def cmd_estadisticas(db: BaseDatos, args: argparse.Namespace) -> str:
    desde, hasta = _rango(args)
    if args.por_hora:
        filas = db.promedio_por_hora(desde, hasta, args.sensor)
        titulo = f"Promedio por hora entre {desde} y {hasta}"
    else:
        filas = db.estadisticas(desde, hasta, args.sensor)
        titulo = f"Estadísticas por sensor entre {desde} y {hasta}"
    return titulo + "\n" + formatear_tabla(filas)


def cmd_eventos(db: BaseDatos, args: argparse.Namespace) -> str:
    if args.resumen:
        desde, hasta = _rango(args)
        return (f"Eventos por tipo/severidad entre {desde} y {hasta}\n"
                + formatear_tabla(db.conteo_eventos(desde, hasta)))
    filtros = [f"tipo={args.tipo}" if args.tipo else "", f"severidad={args.severidad}"
               if args.severidad else ""]
    filtros_txt = ", ".join(f for f in filtros if f) or "sin filtros"
    filas = db.eventos(args.tipo, args.severidad, args.n)
    return f"Eventos ({filtros_txt})\n" + formatear_tabla(filas)


def cmd_riegos(db: BaseDatos, args: argparse.Namespace) -> str:
    fecha = args.fecha or date.today().isoformat()
    total, filas = db.riegos_del_dia(fecha)
    return f"Riegos del {fecha}: {total} riego(s) iniciado(s)\n" + formatear_tabla(filas)


def cmd_comandos(db: BaseDatos, args: argparse.Namespace) -> str:
    if args.resumen:
        return "Comandos por tipo y resultado\n" + formatear_tabla(db.resumen_comandos())
    return f"Últimos {args.n} comandos\n" + formatear_tabla(db.ultimos_comandos(args.n))


def construir_parser() -> argparse.ArgumentParser:
    """Define la interfaz de línea de comandos."""
    parser = argparse.ArgumentParser(
        prog="python -m backend.historico",
        description="Consultas de históricos del Invernadero IoT (SQLite).")
    parser.add_argument("--db", type=Path, help="ruta de la BD (por defecto DB_PATH del .env)")
    sub = parser.add_subparsers(dest="subcomando", required=True)

    p = sub.add_parser("lecturas", help="últimas N lecturas")
    p.add_argument("-n", type=int, default=20, help="cantidad (20)")
    p.add_argument("--sensor", choices=SENSORES)
    p.set_defaults(func=cmd_lecturas)

    p = sub.add_parser("estadisticas", help="promedio/mín/máx por sensor en un rango")
    p.add_argument("--desde", help="inicio (YYYY-MM-DD [HH:MM]); por defecto hace 24 h")
    p.add_argument("--hasta", help="fin (YYYY-MM-DD [HH:MM]); por defecto ahora")
    p.add_argument("--sensor", choices=SENSORES)
    p.add_argument("--por-hora", action="store_true", help="agrupar por hora")
    p.set_defaults(func=cmd_estadisticas)

    p = sub.add_parser("eventos", help="eventos por tipo y/o severidad")
    p.add_argument("--tipo", type=str.upper, help="p. ej. CRITICO, RIEGO_INICIADO")
    p.add_argument("--severidad", type=str.upper, choices=SEVERIDADES)
    p.add_argument("-n", type=int, default=30, help="cantidad (30)")
    p.add_argument("--resumen", action="store_true", help="conteo por tipo/severidad")
    p.add_argument("--desde", help="inicio del rango para --resumen")
    p.add_argument("--hasta", help="fin del rango para --resumen")
    p.set_defaults(func=cmd_eventos)

    p = sub.add_parser("riegos", help="riegos de un día")
    p.add_argument("--fecha", help="YYYY-MM-DD (por defecto hoy)")
    p.set_defaults(func=cmd_riegos)

    p = sub.add_parser("comandos", help="últimos comandos y su resultado")
    p.add_argument("-n", type=int, default=20, help="cantidad (20)")
    p.add_argument("--resumen", action="store_true", help="conteo por comando/resultado")
    p.set_defaults(func=cmd_comandos)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada de la CLI."""
    args = construir_parser().parse_args(argv)
    ruta = args.db or cargar_config().db_path
    if not Path(ruta).exists():
        print(f"No existe la base de datos {ruta}. Ejecute primero el backend.", file=sys.stderr)
        return 1
    db = BaseDatos(ruta)
    try:
        print(args.func(db, args))
    finally:
        db.cerrar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
