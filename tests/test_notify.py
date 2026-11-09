"""Pruebas de la publicación en Firebase con un Firebase falso (sin red)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor import notify  # noqa: E402


def result(overall, error=None, details=None):
    return {
        "platform": "rt", "platform_name": "Rafael Torre 2026",
        "profile": "docente", "profile_label": "Docente",
        "overall": overall, "checks": {"login": overall},
        "details": details or {"login": "OK"}, "response_time": {"login": 1.0},
        "total_time": 2.0, "checked_at": "2026-11-08T20:00:00+00:00",
        "attempts": 1, "error": error,
    }


class FakeFirebase:
    def __init__(self, states=None, fail=False):
        self.states, self.events, self.pushes, self.fail = states or {}, [], [], fail

    def get_state(self, key):
        if self.fail:
            raise RuntimeError("sin red")
        return self.states.get(key)

    def set_state(self, key, data):
        self.states[key] = data

    def add_event(self, data):
        self.events.append(data)

    def push(self, title, text, data):
        self.pushes.append((title, text))


ERR = {"type": "login_failed", "step": "login", "message": "Login fallido"}


class NotifyTests(unittest.TestCase):
    def run_pub(self, fb, res):
        return notify.publish([res], {}, firebase=fb, now="t1")

    def test_primera_vez_verde_no_avisa(self):
        fb = FakeFirebase()
        self.run_pub(fb, result("GREEN"))
        self.assertEqual(fb.pushes, [])
        self.assertEqual(fb.states["rt__docente"]["overall"], "GREEN")

    def test_primera_vez_rojo_avisa(self):
        fb = FakeFirebase()
        self.run_pub(fb, result("RED", ERR))
        self.assertEqual(len(fb.pushes), 1)

    def test_verde_a_rojo_avisa_una_vez_y_guarda_historial(self):
        fb = FakeFirebase({"rt__docente": {"overall": "GREEN", "since": "t0"}})
        self.run_pub(fb, result("RED", ERR))
        self.assertEqual(len(fb.pushes), 1)
        self.assertIn("NO puede", fb.pushes[0][0])
        self.assertEqual(fb.events[0]["from"], "GREEN")
        self.assertEqual(fb.events[0]["to"], "RED")
        self.assertEqual(fb.states["rt__docente"]["since"], "t1")

    def test_rojo_sostenido_no_repite_aviso(self):
        fb = FakeFirebase({"rt__docente": {"overall": "RED", "since": "t0"}})
        self.run_pub(fb, result("RED", ERR))
        self.assertEqual(fb.pushes, [])
        self.assertEqual(fb.events, [])
        self.assertEqual(fb.states["rt__docente"]["since"], "t0")

    def test_verde_a_amarillo_queda_en_historial_pero_no_suena(self):
        fb = FakeFirebase({"rt__docente": {"overall": "GREEN", "since": "t0"}})
        self.run_pub(fb, result("YELLOW", details={"login": "Respuesta lenta (4.0 s)"}))
        self.assertEqual(fb.pushes, [])
        self.assertEqual(len(fb.events), 1)

    def test_amarillo_a_rojo_si_suena(self):
        fb = FakeFirebase({"rt__docente": {"overall": "YELLOW", "since": "t0"}})
        self.run_pub(fb, result("RED", ERR))
        self.assertEqual(len(fb.pushes), 1)

    def test_recuperacion_avisa(self):
        fb = FakeFirebase({"rt__docente": {"overall": "RED", "since": "t0"}})
        self.run_pub(fb, result("GREEN"))
        self.assertIn("volvió a funcionar", fb.pushes[0][0])

    def test_un_fallo_de_firebase_no_rompe(self):
        fb = FakeFirebase(fail=True)
        log = self.run_pub(fb, result("RED", ERR))
        self.assertIn("error al publicar", log[0])

    def test_sin_credenciales_no_hace_nada(self):
        log = notify.publish([result("GREEN")], {})
        self.assertIn("no se publica", log[0])

    def test_codificacion_de_firestore(self):
        fields = notify.encode_fields({"a": "x", "b": 1, "c": 1.5, "d": None,
                                       "e": {"k": True}})
        self.assertEqual(fields["b"], {"integerValue": "1"})
        self.assertEqual(notify.decode_value(fields["e"]), {"k": True})


if __name__ == "__main__":
    unittest.main()
