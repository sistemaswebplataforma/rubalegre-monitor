#!/usr/bin/env python3
"""Ejecuta el monitor desde la línea de comandos.

Ejemplos:
    python run_monitor.py
    python run_monitor.py --json
    python run_monitor.py --platform rafael_torre_2026 --profile docente
    python run_monitor.py --output result.json

Las credenciales se leen de las variables de entorno que indica el archivo de
configuración (por defecto MONITOR_USERNAME y MONITOR_PASSWORD).

Código de salida (el más alto entre todos los resultados):
    0 = GREEN, 1 = YELLOW, 2 = UNKNOWN, 3 = RED
"""

import argparse
import json
import os
import sys
from pathlib import Path

from monitor import engine, report

EXIT_CODES = {engine.GREEN: 0, engine.YELLOW: 1, engine.UNKNOWN: 2, engine.RED: 3}


def main():
    parser = argparse.ArgumentParser(description="Rubalegre Monitor")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config" / "platforms.json"))
    parser.add_argument("--platform", help="id de una plataforma (por defecto, todas)")
    parser.add_argument("--profile", help="id de un perfil (por defecto, todos)")
    parser.add_argument("--json", action="store_true", help="mostrar el resultado en JSON")
    parser.add_argument("--output", help="guardar el resultado en este archivo JSON")
    parser.add_argument("--no-retry", action="store_true", help="no repetir la prueba si da ROJO")
    parser.add_argument("--diagnose", action="store_true",
                        help="si algo falla, mostrar qué pantalla llegó (URL, título y texto visible)")
    parser.add_argument("--publish", action="store_true",
                        help="publicar estado e historial en Firebase y avisar si cambió")
    args = parser.parse_args()

    # Evita errores con los emojis en consolas de Windows.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        with open(args.config, encoding="utf-8") as handle:
            config = json.load(handle)
        if args.no_retry:
            for platform in config.get("platforms", []):
                platform["retries"] = 0
        results = engine.run_all(config, args.platform, args.profile, diagnose=args.diagnose)
    except (OSError, ValueError) as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        return 3

    if not results:
        print("No hay ninguna plataforma/perfil que coincida con el filtro.", file=sys.stderr)
        return 3

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump({"results": results}, handle, ensure_ascii=False, indent=2)

    if args.publish:
        from monitor import notify
        notify.log_to_stderr(notify.publish(results, os.environ))

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        print(report.render_many(results))

    return max(EXIT_CODES[result["overall"]] for result in results)


if __name__ == "__main__":
    sys.exit(main())
