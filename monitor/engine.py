"""Motor de monitoreo.

Simula el recorrido de una persona real en una plataforma WordPress/BuddyBoss:

    acceso público -> login -> sesión -> Mis Clases -> clase de prueba -> contenido

Reglas que nunca se rompen:
  * HTTP 200 por sí solo jamás significa OK: cada pantalla debe contener sus
    marcadores positivos (texto que solo existe cuando la plataforma funciona).
  * Si tras enviar las credenciales seguimos viendo el formulario de login, es ROJO.
  * Si la configuración o las credenciales están incompletas, el estado es
    UNKNOWN, nunca GREEN.

Las credenciales se leen de variables de entorno. Nunca se guardan ni se imprimen.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import requests

GREEN = "GREEN"
YELLOW = "YELLOW"
RED = "RED"
UNKNOWN = "UNKNOWN"

USER_AGENT = "RubalegreMonitor/1.0"

STEP_LABELS = {
    "public_access": "ACCESO PÚBLICO",
    "maintenance": "MANTENIMIENTO",
    "login": "LOGIN",
    "session": "SESIÓN",
    "mis_clases": "MIS CLASES",
    "test_class": "CLASE DE PRUEBA",
    "content": "CONTENIDO",
}
STEP_ORDER = list(STEP_LABELS)

# Nombre corto usado en el campo "response_time" del resultado.
RESPONSE_KEYS = {
    "public_access": "public",
    "login": "login",
    "mis_clases": "mis_clases",
    "test_class": "test_class",
    "content": "content",
}

DEFAULT_MAINTENANCE_WORDS = [
    "próximamente",
    "proximamente",
    "mantenimiento",
    "en construcción",
    "en construccion",
    "estamos trabajando",
    "coming soon",
    "maintenance",
    "under construction",
]

MAINTENANCE_MESSAGE = (
    "Plataforma inaccesible para usuarios "
    "(pantalla de mantenimiento, próximamente o bloqueo)"
)

UNAVAILABLE_TYPE = {
    "mis_clases": "mis_clases_unavailable",
    "test_class": "test_class_unavailable",
    "content": "content_unavailable",
}
MISSING_TYPE = {
    "mis_clases": "mis_clases_unavailable",
    "test_class": "test_class_unavailable",
    "content": "content_missing",
}

# Errores por los que reintentar no tiene sentido (y podría bloquear la cuenta).
NO_RETRY_TYPES = {"config_missing", "login_rejected"}


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

def validate_config(config):
    """Revisa que la configuración tenga lo mínimo. Lanza ValueError si no."""
    if not isinstance(config, dict) or not config.get("platforms"):
        raise ValueError("La configuración debe tener una lista 'platforms'.")
    for platform in config["platforms"]:
        for key in ("id", "name", "base_url", "profiles"):
            if key not in platform:
                raise ValueError(f"Una plataforma no tiene el campo '{key}'.")
        if not platform["profiles"]:
            raise ValueError(f"La plataforma '{platform['id']}' no tiene perfiles.")
        for profile in platform["profiles"]:
            for key in ("id", "username_env", "password_env", "checks"):
                if key not in profile:
                    raise ValueError(
                        f"El perfil de '{platform['id']}' no tiene el campo '{key}'."
                    )
            checks = profile["checks"]
            for name in ("public", "authenticated", "mis_clases", "test_class", "content"):
                if name not in checks:
                    raise ValueError(
                        f"El perfil '{profile['id']}' no define el chequeo '{name}'."
                    )
            for name in ("mis_clases", "test_class", "content"):
                if "url" not in checks[name]:
                    raise ValueError(
                        f"El chequeo '{name}' del perfil '{profile['id']}' no tiene 'url'."
                    )


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _new_step():
    return {"status": UNKNOWN, "detail": "", "response_time": None}


def _fetch(session, method, url, timeout, **kwargs):
    """Hace un pedido. Devuelve (respuesta, segundos, error).

    error es None o una tupla (tipo, mensaje).
    """
    start = time.monotonic()
    try:
        resp = session.request(method, url, timeout=timeout, allow_redirects=True, **kwargs)
    except requests.TooManyRedirects:
        return None, time.monotonic() - start, (
            "redirect_loop",
            "Demasiadas redirecciones seguidas (posible bucle de redirección)",
        )
    except requests.Timeout:
        return None, time.monotonic() - start, (
            "timeout",
            f"Sin respuesta en {timeout} s",
        )
    except requests.exceptions.SSLError:
        return None, time.monotonic() - start, ("ssl_error", "Error de certificado SSL")
    except requests.ConnectionError:
        return None, time.monotonic() - start, (
            "connection_error",
            "No se pudo conectar con el servidor",
        )
    except requests.RequestException as exc:
        return None, time.monotonic() - start, ("request_error", type(exc).__name__)
    return resp, time.monotonic() - start, None


def _missing(body, check):
    """Marcadores positivos que NO aparecen en la página."""
    missing = [m for m in check.get("must_contain_all", []) if m not in body]
    any_of = check.get("must_contain_any", [])
    if any_of and not any(m in body for m in any_of):
        missing.append("alguno de: " + " / ".join(any_of))
    return missing


def _has_words(body, words):
    low = body.lower()
    return any(w.lower() in low for w in words)


def _is_login_screen(body, public_check):
    markers = public_check.get("must_contain_all", [])
    return bool(markers) and all(m in body for m in markers)


def _timed_status(elapsed, slow):
    if elapsed > slow:
        return YELLOW, f"Respuesta lenta ({elapsed:.1f} s, umbral {slow:g} s)"
    return GREEN, "OK"


def _overall(statuses):
    if RED in statuses:
        return RED
    if UNKNOWN in statuses:
        return UNKNOWN
    if YELLOW in statuses:
        return YELLOW
    return GREEN


# ---------------------------------------------------------------------------
# Una ejecución completa del recorrido
# ---------------------------------------------------------------------------

class _Run:
    def __init__(self, platform, profile, env):
        self.platform = platform
        self.profile = profile
        self.env = env
        self.base = platform["base_url"].rstrip("/")
        self.timeout = platform.get("timeout_seconds", 15)
        self.slow = platform.get("slow_threshold_seconds", 3.0)
        self.words = platform.get("maintenance_words", DEFAULT_MAINTENANCE_WORDS)
        self.checks = profile["checks"]
        self.steps = {key: _new_step() for key in STEP_ORDER}
        self.session_ok_count = 0
        self.error = None
        self.unreachable = False
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    # -- helpers de estado --------------------------------------------------

    def _set(self, key, status, detail="", elapsed=None):
        step = self.steps[key]
        step["status"] = status
        step["detail"] = detail
        if elapsed is not None:
            step["response_time"] = elapsed

    def _fail(self, key, error_type, message, elapsed=None):
        self._set(key, RED, message, elapsed)
        if self.error is None:
            self.error = {"type": error_type, "step": key, "message": message}

    def _skip(self, keys, reason):
        for key in keys:
            step = self.steps[key]
            if step["status"] == UNKNOWN and not step["detail"]:
                step["detail"] = reason

    # -- recorrido -----------------------------------------------------------

    def execute(self):
        started = time.monotonic()
        user_var = self.profile["username_env"]
        pass_var = self.profile["password_env"]
        username = self.env.get(user_var)
        password = self.env.get(pass_var)
        others = [k for k in STEP_ORDER if k != "login"]

        if not username or not password:
            message = f"Faltan las variables de entorno {user_var} y {pass_var}"
            self.error = {"type": "config_missing", "step": "login", "message": message}
            self._set("login", UNKNOWN, message)
            self._skip(others, "No evaluable: falta configurar las credenciales")
            return self._result(started)

        self._check_public()
        if self.unreachable:
            self._skip(
                [k for k in STEP_ORDER if k not in ("public_access", "maintenance")],
                "No evaluable: no hay conexión con la plataforma",
            )
            return self._result(started)

        if not self._login(username, password):
            self._skip(
                ["session", "mis_clases", "test_class", "content"],
                "No evaluable: el login falló",
            )
            return self._result(started)

        stages = ["mis_clases", "test_class", "content"]
        for index, key in enumerate(stages):
            if not self._auth_stage(key):
                self._skip(stages[index + 1:], "No evaluable: falló un paso anterior")
                break

        session = self.steps["session"]
        if session["status"] == UNKNOWN and self.session_ok_count:
            self._set(
                "session",
                GREEN,
                f"Sesión mantenida en {self.session_ok_count} pantallas",
            )
        elif session["status"] == UNKNOWN and not session["detail"]:
            session["detail"] = "No evaluable: falló un paso anterior"
        return self._result(started)

    def _check_public(self):
        check = self.checks["public"]
        url = self.base + check.get("url", "/")
        resp, elapsed, err = _fetch(self.session, "GET", url, self.timeout)
        if err:
            self.unreachable = True
            self._fail("public_access", "public_access_unavailable", err[1], elapsed)
            self._set("maintenance", UNKNOWN, "No evaluable: sin respuesta")
            return

        body = resp.text
        missing = _missing(body, check)
        if resp.status_code != 200 or missing:
            if _has_words(body, self.words):
                self._fail("public_access", "maintenance_detected", MAINTENANCE_MESSAGE, elapsed)
                self._set("maintenance", RED, MAINTENANCE_MESSAGE)
            else:
                if resp.status_code != 200:
                    reason = f"HTTP {resp.status_code}"
                else:
                    reason = (
                        "No se encontró la pantalla de login esperada. Falta: "
                        + "; ".join(missing)
                    )
                self._fail("public_access", "public_access_unavailable", reason, elapsed)
                self._set("maintenance", GREEN, "No se detectó pantalla de mantenimiento")
            return

        status, detail = _timed_status(elapsed, self.slow)
        self._set("public_access", status, detail, elapsed)
        self._set("maintenance", GREEN, "No se detectó pantalla de mantenimiento")

    def _login(self, username, password):
        url = self.base + self.profile.get("login_url", "/wp-login.php")
        payload = {
            "log": username,
            "pwd": password,
            "wp-submit": "Log In",
            "redirect_to": self.base + "/",
            "testcookie": "1",
        }
        resp, elapsed, err = _fetch(self.session, "POST", url, self.timeout, data=payload)
        if err:
            self._fail("login", "login_failed", err[1], elapsed)
            return False
        if resp.status_code != 200:
            self._fail("login", "login_failed", f"HTTP {resp.status_code}", elapsed)
            return False

        body = resp.text
        if _is_login_screen(body, self.checks["public"]):
            if "login_error" in body:
                self._fail(
                    "login",
                    "login_rejected",
                    "WordPress rechazó las credenciales del usuario de prueba",
                    elapsed,
                )
            else:
                self._fail(
                    "login",
                    "login_failed",
                    "Tras enviar las credenciales seguimos en la pantalla de login",
                    elapsed,
                )
            return False

        status, detail = _timed_status(elapsed, self.slow)
        self._set("login", status, "Login aceptado" if status == GREEN else detail, elapsed)
        return True

    def _auth_stage(self, key):
        """Abre una pantalla autenticada. Devuelve False si hay que cortar."""
        check = self.checks[key]
        label = STEP_LABELS[key].title()
        resp, elapsed, err = _fetch(self.session, "GET", self.base + check["url"], self.timeout)
        if err:
            self._fail(key, UNAVAILABLE_TYPE[key], err[1], elapsed)
            return False

        body = resp.text
        code = resp.status_code

        # Si volvió el formulario de login, la sesión se perdió.
        if code == 200 and _is_login_screen(body, self.checks["public"]):
            self._fail(
                "session",
                "session_lost",
                f"Sesión perdida: al abrir {label} volvió la pantalla de login",
            )
            self._set(key, UNKNOWN, "No evaluable: la sesión se perdió", elapsed)
            return False

        page_missing = _missing(body, check)
        verify_session = check.get("check_session_marker", True)
        auth_missing = _missing(body, self.checks["authenticated"]) if verify_session else []

        if code != 200 or page_missing:
            if _has_words(body, self.words):
                self._fail(key, "maintenance_detected", MAINTENANCE_MESSAGE, elapsed)
                self._set("maintenance", RED, MAINTENANCE_MESSAGE)
            elif code != 200:
                self._fail(key, UNAVAILABLE_TYPE[key], f"HTTP {code}", elapsed)
            else:
                self._fail(
                    key,
                    MISSING_TYPE[key],
                    "Elemento esperado NO ENCONTRADO (HTTP 200). Falta: "
                    + "; ".join(page_missing),
                    elapsed,
                )
            if verify_session and code == 200 and not auth_missing:
                self.session_ok_count += 1
            return False

        status, detail = _timed_status(elapsed, self.slow)
        self._set(key, status, detail, elapsed)

        if verify_session:
            if auth_missing:
                self._fail(
                    "session",
                    "session_lost",
                    f"No se encontró el marcador de sesión autenticada en {label}",
                )
                return False
            self.session_ok_count += 1
        return True

    # -- resultado -------------------------------------------------------------

    def _result(self, started):
        checks = {key: self.steps[key]["status"] for key in STEP_ORDER}
        response_time = {
            short: round(self.steps[key]["response_time"], 2)
            for key, short in RESPONSE_KEYS.items()
            if self.steps[key]["response_time"] is not None
        }
        return {
            "platform": self.platform["id"],
            "platform_name": self.platform["name"],
            "profile": self.profile["id"],
            "profile_label": self.profile.get("label", self.profile["id"]),
            "overall": _overall(list(checks.values())),
            "checks": checks,
            "details": {key: self.steps[key]["detail"] for key in STEP_ORDER},
            "response_time": response_time,
            "total_time": round(time.monotonic() - started, 2),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "attempts": 1,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def run_check(platform, profile, env=None, sleep=time.sleep):
    """Ejecuta el recorrido de un perfil, con reintentos si el resultado es ROJO.

    Un ROJO aislado (un corte de red de segundos) no debería disparar una alerta,
    por eso se repite la prueba completa antes de darlo por bueno. No se reintenta
    cuando las credenciales fueron rechazadas, para no provocar bloqueos de cuenta.
    """
    env = os.environ if env is None else env
    retries = int(platform.get("retries", 1))
    delay = float(platform.get("retry_delay_seconds", 20))
    attempt = 1
    while True:
        result = _Run(platform, profile, env).execute()
        result["attempts"] = attempt
        error_type = (result["error"] or {}).get("type")
        if result["overall"] != RED or error_type in NO_RETRY_TYPES or attempt > retries:
            return result
        sleep(delay)
        attempt += 1


def run_all(config, platform_id=None, profile_id=None, env=None, sleep=time.sleep):
    """Ejecuta todas las plataformas y perfiles (o los filtrados)."""
    validate_config(config)
    results = []
    for platform in config["platforms"]:
        if platform_id and platform["id"] != platform_id:
            continue
        for profile in platform["profiles"]:
            if profile_id and profile["id"] != profile_id:
                continue
            results.append(run_check(platform, profile, env=env, sleep=sleep))
    return results
