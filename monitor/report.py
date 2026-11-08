"""Reporte de texto para la consola."""

from __future__ import annotations

from datetime import datetime

from monitor.engine import GREEN, RED, RESPONSE_KEYS, STEP_LABELS, STEP_ORDER, UNKNOWN, YELLOW

ICONS = {GREEN: "🟢", YELLOW: "🟡", RED: "🔴", UNKNOWN: "⚪"}
BAR = "=" * 40


def render(result):
    lines = [
        BAR,
        "RUBALEGRE MONITOR",
        f"{result['platform_name']} ({result['profile_label']})",
        BAR,
        "",
    ]
    for key in STEP_ORDER:
        status = result["checks"][key]
        seconds = result["response_time"].get(RESPONSE_KEYS.get(key, ""))
        timing = f"{seconds:.2f} s" if seconds is not None else ""
        lines.append(f"{ICONS[status]} {STEP_LABELS[key]:<17} {status:<8} {timing}".rstrip())
        detail = result["details"].get(key, "")
        if status != GREEN and detail:
            lines.append(f"    -> {detail}")

    overall = result["overall"]
    lines += ["", f"ESTADO GENERAL: {ICONS[overall]} {overall}"]

    error = result.get("error")
    if error:
        lines += ["", f"Error principal: {error['type']}", f"  {error['message']}"]

    checked = datetime.fromisoformat(result["checked_at"]).astimezone().strftime("%H:%M:%S")
    lines += ["", f"Tiempo total: {result['total_time']:.2f} s"]
    if result.get("attempts", 1) > 1:
        lines.append(f"Intentos: {result['attempts']}")
    lines += [f"Última comprobación: {checked}", BAR]
    return "\n".join(lines)


def render_many(results):
    return "\n\n".join(render(result) for result in results)
