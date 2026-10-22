# Revisión de `feature/hub-agent` (21-oct-2026)

Revisión hecha sobre el commit `d51abbf`; los números de línea se refieren a ese commit.
Lint y formato: sin errores. Tests: 539 pasan, 2 fallan en Windows (ver B3) y 37 se saltan porque los navegadores no están descargados.

## A. Panel web (`src/web/index.html`)

- [ ] **A1. Bug que modifica datos: el editor puede dejar un perfil sin operador** (`:1333-1339`, `:1377`).
  El selector de operador arranca solo con "Sin asignar" y la lista de usuarios llega después por `/hub/users`. Si el admin guarda antes de que llegue, o si esa petición falla, se envía `PUT /hub/owners/<perfil>` con `user: null` y el operador deja de ver el perfil. Además, ese `PUT` sale en cada guardado aunque el operador no haya cambiado.
  Arreglo: agregar desde el inicio la opción del operador actual, enviar el `PUT` solo si el valor cambió y ponerle `.catch` a la carga de la lista.

- [ ] **A2. El panel se vuelve lento con muchos perfiles** (`:939`, `:2077`).
  `refresh()` pide el fingerprint de cada perfil por separado y no dibuja la lista hasta tenerlos todos. Además corre cada 4 s aunque el refresco anterior siga en curso, y en cada vuelta vuelve a pedir los fingerprints pendientes. Con cientos o miles de perfiles importados de AdsPower, la lista tarda en aparecer y el hub recibe miles de peticiones.
  Arreglo corto: no lanzar un refresco mientras haya otro en curso, y dibujar la lista primero y cargar los fingerprints después. Lo ideal sería que `/profiles` ya incluyera el resumen del fingerprint.

- [ ] **A3. En las acciones masivas se pierden los errores** (`:1189`, `:1238`, `:1270`).
  Cada aviso de error reemplaza al anterior: si fallan 5 perfiles, solo se ve el último.
  Arreglo: juntar los fallos y mostrar un solo aviso ("3 fallaron: a, b, c").

- [ ] **A4. "Abrir" en bloque lanza todos los perfiles a la vez** (`:1195`).
  Con 50 seleccionados salen 50 aperturas simultáneas, y cada una dispara después un refresco completo. En modo hub son 50 navegadores abriéndose al mismo tiempo en una sola PC.
  Arreglo: abrir de 2 o 3 en 2 o 3 y hacer un solo refresco al final.

- [ ] **A5. Menores.**
  - La importación de AdsPower sigue corriendo aunque se cierre la ventana (`:1784`) y no tiene botón para cancelar.
  - La edición masiva ofrece "Android (Chrome)" también para perfiles Camoufox, que no lo aceptan; cada uno falla por separado y, por A3, solo se ve el último error.
  - Los textos en español mezclan "profiles" (36 veces) con "perfiles" (7), por ejemplo en "Buscar perfiles" y "{n} perfiles abiertos". Conviene unificar.

Lo que está bien: el panel no usa `innerHTML` (todo se arma con `createElement` y `textContent`), así que ni los nombres que vienen de AdsPower, ni los de las PCs, ni los correos pueden inyectar código. El token solo viaja en la cabecera `Authorization`. La clave del agente se entrega por un formulario POST a `127.0.0.1`, no por la URL, y el `state` se valida.

## B. Hub y agente

- [ ] **B1. Grave: un atacante en la red podría ejecutar código en todas las PCs** (`src/agent.py:376`, `src/hub/agent_code.py`).
  El agente descarga `/hub/agent/code`, lo instala y se reinicia. Antes de instalarlo solo lo compara contra un valor que llega del mismo hub, en la misma conexión: eso detecta un archivo dañado, pero no a alguien que se ponga en medio. Como `docs/HUB.md` usa el hub por `http://` dentro de la LAN, cualquiera en la red puede meterle su propio código al agente de cada operador.
  Arreglo corto: que el agente no se actualice si la URL del hub no es `https://`. Arreglo completo: firmar el código con una clave que vaya grabada en el instalador, igual que ya se graba `HUB_URL`.

- [ ] **B2. Media: el login no verifica el permiso `foxprofile:access`** (`src/hub/routes.py:73`).
  El hub confía en que la plataforma de identidad rechace a quien no tiene acceso. Si esa plataforma queda mal configurada, cualquier usuario suyo entra como operador.
  Arreglo: `if not has_permission(user["permisos"], "foxprofile:access")` → 403.

- [ ] **B3. Bug: la ventana de logs muestra mal los saltos de línea en Windows** (`src/agent_logs.py:42`).
  En Windows los logs se escriben con `\r\n` y `read_new` se los pasa tal cual a Tk. Por esto fallan `tests/test_agent_logs.py` (2 tests), y el CI en `windows-latest` debería estar en rojo.
  Arreglo: `.replace("\r\n", "\n")` antes del `return`.

- [ ] **B4. Menor: un miembro de grupo puede pisar los datos de un perfil** (`src/hub/routes.py:479`, `src/hub/remote.py:132`).
  `upload_bundle` acepta la subida de cualquiera que vea el perfil mientras esté libre, aunque nunca lo haya abierto. Y `touch` le reserva un perfil a cualquier agente que lo incluya en `running=`. Es razonable si los operadores son de confianza en la LAN. Esa ruta tampoco tiene límite de tamaño (ya está marcado con `ponytail:`).

- [ ] **B5. Documentación desactualizada:** `CLAUDE.md:52` dice que el hub guarda los usuarios con "scrypt", pero ya no guarda contraseñas: el inicio de sesión lo resuelve la plataforma de identidad. (El enlace a `z0m0dan/foxprofile` en `docs/HUB.md` está bien: es el fork de la empresa.)
