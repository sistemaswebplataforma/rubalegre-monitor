"""Publica el resultado en Firebase: estado actual, historial de cambios y aviso push.

* Estado actual:  colección `monitor_state`, un documento por plataforma y perfil.
* Historial:      colección `monitor_events`, un documento por CAMBIO de estado.
* Aviso push:     solo cuando el estado general cambia (caída, mejora o recuperación),
                  mediante un tópico de FCM al que se suscribe la app. Así no hace falta
                  guardar tokens de dispositivos ni usar Cloud Functions (plan gratuito).

Credenciales: la variable de entorno FIREBASE_SERVICE_ACCOUNT con el JSON de la cuenta de
servicio (en GitHub Actions, un Secret). Si falta, no se publica nada y el monitor sigue
funcionando igual.

Importante: un fallo al publicar NUNCA cambia el resultado del monitor.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

TOPIC = "rubalegre-monitor"
STATE_COLLECTION = "monitor_state"
EVENTS_COLLECTION = "monitor_events"
SCOPES = [
    "https://www.googleapis.com/auth/datastore",
    "https://www.googleapis.com/auth/firebase.messaging",
]

ICONS = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "UNKNOWN": "⚪"}


# ---------------------------------------------------------------------------
# Codificación de valores para la API REST de Firestore
# ---------------------------------------------------------------------------

def encode_value(value):
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: encode_value(v) for k, v in value.items()}}}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [encode_value(v) for v in value]}}
    return {"stringValue": str(value)}


def encode_fields(data):
    return {key: encode_value(value) for key, value in data.items()}


def decode_value(value):
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return value["doubleValue"]
    if "booleanValue" in value:
        return value["booleanValue"]
    if "mapValue" in value:
        return {k: decode_value(v) for k, v in value["mapValue"].get("fields", {}).items()}
    if "arrayValue" in value:
        return [decode_value(v) for v in value["arrayValue"].get("values", [])]
    return None


# ---------------------------------------------------------------------------
# Lógica de cambio de estado (pura, fácil de probar)
# ---------------------------------------------------------------------------

def doc_id(result):
    return f"{result['platform']}__{result['profile']}"


def describe_change(result, previous):
    """Devuelve (título, texto) del aviso, o None si no hay que avisar."""
    new = result["overall"]
    if previous is None:
        return None if new == "GREEN" else _alert(result)
    if previous == new:
        return None
    name = result.get("platform_name", result["platform"])
    if new == "GREEN":
        return (f"{ICONS[new]} {name} volvió a funcionar",
                "Un usuario normal ya puede entrar y usar la plataforma.")
    return _alert(result)


def _alert(result):
    name = result.get("platform_name", result["platform"])
    new = result["overall"]
    error = result.get("error") or {}
    detail = error.get("message") or ""
    if new == "RED":
        title = f"{ICONS[new]} {name}: un usuario normal NO puede usarla"
    elif new == "YELLOW":
        title = f"{ICONS[new]} {name}: funciona, pero lenta"
        slow = [v for v in result["details"].values() if v and v != "OK"]
        detail = slow[0] if slow else detail
    else:
        title = f"{ICONS[new]} {name}: no se pudo comprobar"
    return title, detail


def should_push(previous, new):
    """El celular suena solo si hay algo importante. Pasar entre verde y amarillo
    (lentitud) queda en el historial y en el indicador, pero no suena."""
    if previous is None:
        return new != "GREEN"
    return not ({previous, new} <= {"GREEN", "YELLOW"})


def build_state_doc(result, previous_doc, now):
    changed = previous_doc is None or previous_doc.get("overall") != result["overall"]
    return {
        "platform": result["platform"],
        "platform_name": result.get("platform_name", result["platform"]),
        "profile": result["profile"],
        "profile_label": result.get("profile_label", result["profile"]),
        "overall": result["overall"],
        "checks": result["checks"],
        "details": result["details"],
        "response_time": result["response_time"],
        "total_time": result["total_time"],
        "error": result.get("error"),
        "checked_at": result["checked_at"],
        "since": now if changed else previous_doc.get("since", now),
        "previous_overall": (previous_doc or {}).get("overall") if changed else
                            previous_doc.get("previous_overall"),
    }


def build_event_doc(result, previous, title, text, now):
    return {
        "platform": result["platform"],
        "profile": result["profile"],
        "from": previous,
        "to": result["overall"],
        "title": title,
        "text": text,
        "checks": result["checks"],
        "at": now,
    }


# ---------------------------------------------------------------------------
# Comunicación con Firebase
# ---------------------------------------------------------------------------

def make_session(service_account_json):
    """Devuelve (sesión autenticada, project_id)."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession

    info = json.loads(service_account_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(creds), info["project_id"]


class Firebase:
    def __init__(self, session, project_id):
        self.http = session
        self.project = project_id
        self.docs = (
            f"https://firestore.googleapis.com/v1/projects/{project_id}"
            "/databases/(default)/documents"
        )

    def get_state(self, key):
        resp = self.http.get(f"{self.docs}/{STATE_COLLECTION}/{key}", timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        fields = resp.json().get("fields", {})
        return {k: decode_value(v) for k, v in fields.items()}

    def set_state(self, key, data):
        resp = self.http.patch(
            f"{self.docs}/{STATE_COLLECTION}/{key}",
            json={"fields": encode_fields(data)},
            timeout=20,
        )
        resp.raise_for_status()

    def add_event(self, data):
        resp = self.http.post(
            f"{self.docs}/{EVENTS_COLLECTION}",
            json={"fields": encode_fields(data)},
            timeout=20,
        )
        resp.raise_for_status()

    def push(self, title, text, data):
        # Mensaje solo de datos: la app arma la notificación (aviso + indicador fijo),
        # incluso con la app cerrada.
        body = {
            "message": {
                "topic": TOPIC,
                "data": {**{k: str(v) for k, v in data.items()}, "title": title, "text": text},
                "android": {"priority": "HIGH"},
            }
        }
        resp = self.http.post(
            f"https://fcm.googleapis.com/v1/projects/{self.project}/messages:send",
            json=body,
            timeout=20,
        )
        resp.raise_for_status()


def publish(results, env, firebase=None, now=None):
    """Publica los resultados. Devuelve una lista de textos con lo que pasó."""
    log = []
    if firebase is None:
        raw = env.get("FIREBASE_SERVICE_ACCOUNT")
        if not raw:
            return ["Firebase: sin FIREBASE_SERVICE_ACCOUNT, no se publica nada."]
        try:
            session, project_id = make_session(raw)
            firebase = Firebase(session, project_id)
        except Exception as exc:  # credenciales mal copiadas, librería ausente, etc.
            return [f"Firebase: no se pudo iniciar ({type(exc).__name__})."]

    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    for result in results:
        key = doc_id(result)
        try:
            previous_doc = firebase.get_state(key)
            previous = previous_doc.get("overall") if previous_doc else None
            firebase.set_state(key, build_state_doc(result, previous_doc, now))
            change = describe_change(result, previous)
            if change:
                title, text = change
                firebase.add_event(build_event_doc(result, previous, title, text, now))
                if should_push(previous, result["overall"]):
                    firebase.push(title, text, {
                        "platform": result["platform"],
                        "overall": result["overall"],
                    })
                    sent = "aviso enviado"
                else:
                    sent = "solo historial, sin sonar"
                log.append(f"Firebase: {key} {previous or 'nuevo'} -> {result['overall']} ({sent})")
            else:
                log.append(f"Firebase: {key} {result['overall']} (sin cambios)")
        except Exception as exc:
            log.append(f"Firebase: error al publicar {key} ({type(exc).__name__}: {exc})")
    return log


def log_to_stderr(lines):
    for line in lines:
        print(line, file=sys.stderr)
