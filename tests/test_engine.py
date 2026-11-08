"""Pruebas del motor contra un servidor local que imita la plataforma.

No toca ninguna plataforma real. Cada prueba provoca un fallo controlado y
comprueba que el monitor lo detecta con el semáforo y el error correctos.

Ejecutar:  python -m unittest discover -s tests -v
"""

import copy
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from monitor import engine, report  # noqa: E402

LOGIN_PAGE = (
    "<html><head><title>Acceder &lt; Tu plataforma del 2026. &#8212; WordPress</title>"
    "<style>.login_error{}</style></head><body>"
    '<form id="loginform" name="loginform"><h2>Iniciar sesión</h2>'
    '<input name="log" id="user_login" aria-describedby="login_error">'
    '<input name="pwd" id="user_pass" aria-describedby="login_error"></form>%s'
    "</body></html>"
)
HOME_PAGE = (
    "<html><body><nav>Calificaciones Asistencia</nav>"
    "<h1>Bienvenido/a, monitor</h1><section>Avisos Institucionales</section></body></html>"
)
CLASS_PAGE = (
    "<html><body><nav>Calificaciones Asistencia</nav>"
    "<h1>TESTEOS Mis clases de Testeos</h1>"
    "<p>Grupo permanente de infraestructura QA y desarrollo.</p></body></html>"
)
TASKS_PAGE = "<html><body><nav>Calificaciones</nav><h3>Testeo. No responder</h3></body></html>"
MAINTENANCE_PAGE = "<html><body><h1>Próximamente</h1><p>Estamos trabajando.</p></body></html>"

CLASS_PATH = "/groups/test/mc-actividad/"
TASKS_PATH = "/groups/test/mc-tareas/"
COOKIE = "wordpress_logged_in_abc"

STATE = {"mode": "ok", "delay": 0.0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, headers=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location, headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _logged_in(self):
        return COOKIE in self.headers.get("Cookie", "")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        if STATE["mode"] == "maintenance":
            return self._send(200, MAINTENANCE_PAGE)
        if STATE["mode"] == "fake_login":
            return self._send(200, HOME_PAGE)  # 200 sin cookie de sesión
        if form.get("pwd", [""])[0] == "good":
            return self._redirect("/", {"Set-Cookie": f"{COOKIE}=ok; Path=/"})
        return self._send(200, LOGIN_PAGE % '<div id="login_error">Error</div>')

    def do_GET(self):
        mode = STATE["mode"]
        path = self.path.split("?")[0]

        if mode == "maintenance":
            return self._send(200, MAINTENANCE_PAGE)
        if path == "/wp-login.php":
            return self._send(200, LOGIN_PAGE % "")
        if path == "/":
            if self._logged_in():
                return self._send(200, HOME_PAGE)
            return self._redirect("/wp-login.php?action=bpnoaccess")
        if path == CLASS_PATH:
            if mode == "loop":
                return self._redirect(CLASS_PATH)
            if mode == "http500":
                return self._send(500, "error")
            if mode == "slow":
                time.sleep(STATE["delay"])
            if mode == "session_drop" or not self._logged_in():
                return self._redirect("/wp-login.php?action=bpnoaccess")
            if mode == "class_missing":
                return self._send(200, "<html><nav>Calificaciones</nav>vacío</html>")
            if mode == "user_maintenance":
                return self._send(200, MAINTENANCE_PAGE)
            return self._send(200, CLASS_PAGE)
        if path == TASKS_PATH:
            if not self._logged_in():
                return self._redirect("/wp-login.php?action=bpnoaccess")
            if mode == "content_missing":
                return self._send(200, "<html><nav>Calificaciones</nav>sin tareas</html>")
            return self._send(200, TASKS_PAGE)
        return self._send(404, "no existe")


def make_platform(base_url, **overrides):
    platform = {
        "id": "mock",
        "name": "Plataforma simulada",
        "base_url": base_url,
        "timeout_seconds": 3,
        "slow_threshold_seconds": 1.0,
        "retries": 0,
        "retry_delay_seconds": 0,
        "profiles": [
            {
                "id": "docente",
                "label": "Docente simulado",
                "username_env": "TEST_USER",
                "password_env": "TEST_PASS",
                "login_url": "/wp-login.php",
                "checks": {
                    "public": {
                        "url": "/",
                        "must_contain_all": ['id="loginform"', 'name="log"', 'name="pwd"'],
                    },
                    "authenticated": {"must_contain_any": ["Calificaciones", "Asistencia"]},
                    "mis_clases": {
                        "url": "/",
                        "must_contain_all": ["Bienvenido/a,", "Avisos Institucionales"],
                    },
                    "test_class": {
                        "url": CLASS_PATH,
                        "must_contain_all": [
                            "Mis clases de Testeos",
                            "Grupo permanente de infraestructura QA y desarrollo.",
                        ],
                    },
                    "content": {
                        "url": TASKS_PATH,
                        "must_contain_all": ["Testeo. No responder"],
                        "check_session_marker": False,
                    },
                },
            }
        ],
    }
    platform.update(overrides)
    return platform


GOOD_ENV = {"TEST_USER": "monitor", "TEST_PASS": "good"}


class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        STATE.update(mode="ok", delay=0.0)

    def run_check(self, env=None, **overrides):
        platform = make_platform(self.base, **overrides)
        return engine.run_check(
            platform,
            platform["profiles"][0],
            env=GOOD_ENV if env is None else env,
            sleep=lambda seconds: None,
        )

    def test_todo_funciona_da_verde(self):
        result = self.run_check()
        self.assertEqual(result["overall"], engine.GREEN, result["details"])
        self.assertTrue(all(status == engine.GREEN for status in result["checks"].values()))
        self.assertIsNone(result["error"])
        for key in ("public", "login", "mis_clases", "test_class", "content"):
            self.assertIn(key, result["response_time"])

    def test_contrasena_incorrecta_da_rojo_y_no_sigue(self):
        result = self.run_check(env={"TEST_USER": "monitor", "TEST_PASS": "mala"})
        self.assertEqual(result["overall"], engine.RED)
        self.assertEqual(result["checks"]["login"], engine.RED)
        self.assertEqual(result["error"]["type"], "login_rejected")
        for key in ("session", "mis_clases", "test_class", "content"):
            self.assertEqual(result["checks"][key], engine.UNKNOWN)

    def test_login_sin_cookie_de_sesion_no_es_verde(self):
        STATE["mode"] = "fake_login"
        result = self.run_check()
        self.assertEqual(result["checks"]["login"], engine.RED)
        self.assertEqual(result["error"]["type"], "login_failed")
        self.assertEqual(result["checks"]["mis_clases"], engine.UNKNOWN)

    def test_cambio_de_nombre_del_sitio_no_rompe_el_monitor(self):
        # La pantalla de login ya no dice "Nuestra plataforma": debe seguir verde.
        result = self.run_check()
        self.assertEqual(result["overall"], engine.GREEN)

    def test_diagnose_muestra_la_pantalla_sin_secretos(self):
        STATE["mode"] = "class_missing"
        platform = make_platform(self.base)
        result = engine.run_check(
            platform, platform["profiles"][0], env=GOOD_ENV,
            sleep=lambda s: None, diagnose=True,
        )
        snap = result["diagnostics"]["test_class"]
        self.assertIn("vacío", snap["text"])
        self.assertNotIn("good", str(result["diagnostics"]))
        self.assertNotIn(COOKIE, str(result["diagnostics"]))

    def test_pantalla_de_proximamente_da_rojo(self):
        STATE["mode"] = "maintenance"
        result = self.run_check()
        self.assertEqual(result["overall"], engine.RED)
        self.assertEqual(result["checks"]["public_access"], engine.RED)
        self.assertEqual(result["checks"]["maintenance"], engine.RED)
        self.assertEqual(result["error"]["type"], "maintenance_detected")
        self.assertIn("inaccesible", result["error"]["message"])

    def test_mantenimiento_solo_para_usuarios_normales_da_rojo(self):
        STATE["mode"] = "user_maintenance"
        result = self.run_check()
        self.assertEqual(result["checks"]["public_access"], engine.GREEN)
        self.assertEqual(result["checks"]["login"], engine.GREEN)
        self.assertEqual(result["checks"]["test_class"], engine.RED)
        self.assertEqual(result["checks"]["maintenance"], engine.RED)
        self.assertEqual(result["error"]["type"], "maintenance_detected")
        self.assertEqual(result["overall"], engine.RED)

    def test_bucle_de_redirecciones_en_la_clase_da_rojo(self):
        STATE["mode"] = "loop"
        result = self.run_check()
        self.assertEqual(result["overall"], engine.RED)
        self.assertEqual(result["checks"]["mis_clases"], engine.GREEN)
        self.assertEqual(result["checks"]["test_class"], engine.RED)
        self.assertEqual(result["checks"]["content"], engine.UNKNOWN)
        self.assertEqual(result["error"]["type"], "test_class_unavailable")
        self.assertIn("redirecci", result["error"]["message"])

    def test_http_500_en_la_clase_da_rojo(self):
        STATE["mode"] = "http500"
        result = self.run_check()
        self.assertEqual(result["checks"]["test_class"], engine.RED)
        self.assertIn("HTTP 500", result["details"]["test_class"])

    def test_sesion_perdida_da_rojo(self):
        STATE["mode"] = "session_drop"
        result = self.run_check()
        self.assertEqual(result["overall"], engine.RED)
        self.assertEqual(result["checks"]["session"], engine.RED)
        self.assertEqual(result["error"]["type"], "session_lost")
        self.assertEqual(result["checks"]["test_class"], engine.UNKNOWN)

    def test_http_200_sin_el_elemento_esperado_da_rojo(self):
        STATE["mode"] = "class_missing"
        result = self.run_check()
        self.assertEqual(result["checks"]["test_class"], engine.RED)
        self.assertEqual(result["checks"]["login"], engine.GREEN)
        self.assertEqual(result["error"]["type"], "test_class_unavailable")
        self.assertIn("NO ENCONTRADO", result["details"]["test_class"])

    def test_contenido_faltante_da_rojo_pero_la_clase_sigue_verde(self):
        STATE["mode"] = "content_missing"
        result = self.run_check()
        self.assertEqual(result["checks"]["test_class"], engine.GREEN)
        self.assertEqual(result["checks"]["content"], engine.RED)
        self.assertEqual(result["error"]["type"], "content_missing")
        self.assertEqual(result["overall"], engine.RED)

    def test_respuesta_lenta_da_amarillo(self):
        STATE.update(mode="slow", delay=1.4)
        result = self.run_check()
        self.assertEqual(result["checks"]["test_class"], engine.YELLOW)
        self.assertEqual(result["overall"], engine.YELLOW)
        self.assertIsNone(result["error"])
        self.assertIn("lenta", result["details"]["test_class"])

    def test_sin_credenciales_da_unknown_nunca_verde(self):
        result = self.run_check(env={})
        self.assertEqual(result["overall"], engine.UNKNOWN)
        self.assertEqual(result["error"]["type"], "config_missing")
        self.assertNotIn(engine.GREEN, result["checks"].values())

    def test_servidor_caido_da_rojo(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        platform = make_platform(f"http://127.0.0.1:{closed_port}")
        result = engine.run_check(
            platform, platform["profiles"][0], env=GOOD_ENV, sleep=lambda s: None
        )
        self.assertEqual(result["overall"], engine.RED)
        self.assertEqual(result["checks"]["public_access"], engine.RED)
        self.assertEqual(result["checks"]["login"], engine.UNKNOWN)
        self.assertEqual(result["error"]["type"], "public_access_unavailable")

    def test_reintenta_un_rojo_pero_no_una_contrasena_rechazada(self):
        STATE["mode"] = "http500"
        self.assertEqual(self.run_check(retries=1)["attempts"], 2)
        STATE["mode"] = "ok"
        bad = {"TEST_USER": "monitor", "TEST_PASS": "mala"}
        self.assertEqual(self.run_check(env=bad, retries=1)["attempts"], 1)

    def test_configuracion_invalida_se_rechaza(self):
        with self.assertRaises(ValueError):
            engine.validate_config({"platforms": []})
        broken = {"platforms": [make_platform(self.base)]}
        del broken["platforms"][0]["profiles"][0]["checks"]["content"]
        with self.assertRaises(ValueError):
            engine.validate_config(broken)
        engine.validate_config({"platforms": [copy.deepcopy(make_platform(self.base))]})

    def test_el_reporte_muestra_el_estado_general(self):
        text = report.render(self.run_check())
        self.assertIn("ESTADO GENERAL", text)
        self.assertIn("ACCESO PÚBLICO", text)
        self.assertNotIn("good", text)


if __name__ == "__main__":
    unittest.main()
