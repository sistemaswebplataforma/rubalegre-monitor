# Rubalegre Monitor (motor de monitoreo)

Responde una sola pregunta: **¿una persona normal puede entrar ahora mismo a la plataforma y usar sus clases?**

No alcanza con que el servidor responda. El monitor entra con un usuario de prueba que NO es administrador y recorre lo mismo que haría un usuario real:

1. Acceso público (debe verse la pantalla de login real, no "Próximamente" ni mantenimiento)
2. Login
3. Sesión (se verifica que sigue iniciada en cada pantalla)
4. Mis Clases
5. Clase de prueba
6. Contenido de la clase

HTTP 200 nunca significa OK. Cada pantalla debe contener sus marcadores positivos.

## Semáforo

| Estado | Significado |
|---|---|
| GREEN | Todo funciona para el usuario de prueba |
| YELLOW | Funciona, pero hay una respuesta lenta (umbral configurable) |
| RED | Algo esencial falla: acceso, login, sesión, Mis Clases, clase, contenido o mantenimiento |
| UNKNOWN | No se pudo evaluar (faltan credenciales o falló un paso previo). Nunca se muestra como verde |

Si un paso falla, los siguientes quedan en UNKNOWN y el error principal indica cuál fue el primero.

## Cómo ejecutarlo (Windows)

1. Instalá Python 3 (con la casilla "Add Python to PATH") y, una sola vez: `pip install -r requirements.txt`
2. Abrí una terminal (cmd) dentro de la carpeta del proyecto.
3. Definí las credenciales solo para esa ventana (no se guardan en ningún archivo):

```
set MONITOR_USERNAME=el_usuario_de_prueba
set MONITOR_PASSWORD=la_contraseña
python run_monitor.py
```

Opciones útiles:

```
python run_monitor.py --json                 # resultado estructurado en JSON
python run_monitor.py --output result.json   # además lo guarda en un archivo
python run_monitor.py --no-retry             # sin repetir la prueba si da rojo
python run_monitor.py --diagnose             # si algo falla, muestra qué pantalla llegó (URL, título, texto)
python run_monitor.py --platform rafael_torre_2026 --profile docente
```

Código de salida: 0 = GREEN, 1 = YELLOW, 2 = UNKNOWN, 3 = RED.

## Configuración

Todo lo que cambia entre plataformas está en `config/platforms.json`, no en el código:

- `base_url`, `timeout_seconds`, `slow_threshold_seconds`
- `retries` y `retry_delay_seconds`: si da rojo, repite la prueba completa antes de darlo por bueno
- `profiles`: cada perfil (docente, alumno, familia) tiene sus propias variables de entorno de credenciales (`username_env`, `password_env`) y sus propios chequeos
- `checks`: URL y textos que deben aparecer en cada pantalla (`must_contain_all`, `must_contain_any`)

Para sumar otra plataforma o perfil se agrega un bloque al JSON. No hay que tocar el código.

Para el login se usan marcadores estructurales de WordPress (`id="loginform"`, `name="log"`, `name="pwd"`) y no textos que cambian, como el nombre o el año del sitio. Además el login solo es verde si WordPress entregó la cookie de sesión (`login_cookie_prefix`, por defecto `wordpress_logged_in`).

Los marcadores salen de pantallas reales de la plataforma. Si el diseño de la plataforma cambia, hay que actualizar los textos en el JSON.

## Tipos de error

| Tipo | Cuándo |
|---|---|
| `public_access_unavailable` | No carga, no hay conexión o la pantalla de login no es la esperada |
| `maintenance_detected` | Aparece "Próximamente", mantenimiento o bloqueo, incluso solo para usuarios normales |
| `login_rejected` | WordPress rechazó las credenciales (no se reintenta, para no bloquear la cuenta) |
| `login_failed` | Tras el login seguimos en el formulario, o hubo un error de red o HTTP |
| `session_lost` | Volvió la pantalla de login o falta el marcador de sesión |
| `mis_clases_unavailable` | Mis Clases no cargó o falta su contenido |
| `test_class_unavailable` | La clase de prueba no cargó, hay bucle de redirecciones o falta su contenido |
| `content_missing` / `content_unavailable` | La clase abre pero falta el contenido esencial |
| `config_missing` | Faltan las variables de entorno de credenciales |

## Pruebas automáticas

Hay un servidor local que imita la plataforma y provoca cada fallo (contraseña incorrecta, mantenimiento, sesión perdida, bucle de redirecciones, HTTP 500, contenido faltante, lentitud, servidor caído). No toca ninguna plataforma real.

```
python -m unittest discover -s tests -v
```

## Seguridad

- Este repositorio es público: no contiene contraseñas ni usuarios. Las credenciales se leen de variables de entorno (en GitHub Actions, de Secrets).
- El monitor nunca imprime ni guarda las credenciales ni las cookies de sesión.
- Usá una cuenta de prueba exclusiva, sin permisos de administrador y con una contraseña larga y única.
- El monitor hace unas 6 peticiones por ejecución. Con una frecuencia de 5 minutos la carga es mínima.

## Próximos pasos

- Ejecución automática cada 5 minutos con GitHub Actions
- Estado, historial y notificaciones con Firebase
- App Android que muestre el estado
