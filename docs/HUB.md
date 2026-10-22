# FoxProfile Hub: servidor central + navegadores en cada PC

El **hub** es un servidor FoxProfile en Docker para una red local. Guarda los perfiles (configuración, cookies, fingerprint) y los usuarios, y sirve el panel web. **No abre navegadores.** Cada operador instala el **agente** en su PC (Windows o macOS); cuando pulsa *Launch* en el panel, el navegador se abre en su propia computadora.

```
PC de ana ── navegador ──> http://hub:8000  (panel)
PC de ana ── agente ─────> hub  (recibe "abrir perfil", descarga/sube datos)
                            │
                            └── ./data  (profiles.json, camoufox_data/, hub.json)
```

## Cómo funciona

1. El operador inicia sesión en el panel con su usuario y ve **solo sus perfiles** (el admin ve todos).
2. *Launch* reserva el perfil para ese operador y envía la orden a **su** agente. Si el agente no está conectado, el panel lo dice ("Your PC: agent not running").
3. El agente descarga el perfil, lo abre con Camoufox en la PC y avisa al hub.
4. Al cerrar el navegador (o pulsar *Stop* en el panel), el agente sube los datos al hub y borra su copia local. Mientras tanto nadie más puede abrir ese perfil.

El agente se conecta hacia el hub: las PCs no necesitan abrir puertos.

## 1. Instalar el hub (servidor)

Requisitos: Docker con Compose.

```bash
git clone https://github.com/z0m0dan/foxprofile.git
cd foxprofile
cp .env.hub.example .env
# Editar .env:
#   FOXPROFILE_API_TOKEN=   (24+ caracteres aleatorios; firma las sesiones)
#   FOXPROFILE_AUTH_URL=      (API de la plataforma de identidad, p. ej. https://api.example.com)
#   FOXPROFILE_AUTH_ORIGIN=   (origen registrado para FoxProfile en esa plataforma)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # genera un token
docker compose up -d --build
```

El panel queda en `http://<ip-del-servidor>:8000/`. Los datos viven en `./data` (respáldelo con el contenedor detenido o sin perfiles abiertos).

Actualizar: `git pull && docker compose up -d --build`.

### Dokploy

1. **Create Service → Compose**, tipo *Docker Compose*, con el repositorio y la rama. En *Compose Path* poner `./compose.dokploy.yaml`.
2. **Environment** (Dokploy lo escribe en el `.env` que lee el contenedor):

   ```env
   FOXPROFILE_API_TOKEN=<24+ caracteres aleatorios>
   FOXPROFILE_AUTH_URL=https://api.example.com
   FOXPROFILE_AUTH_ORIGIN=https://foxprofile.example.com
   FOXPROFILE_LANG=es
   # Puerto publicado en el servidor (8000 si no se indica); dentro del contenedor es siempre 8000
   FOXPROFILE_HUB_PORT=4242
   ```

   `FOXPROFILE_AUTH_ORIGIN` tiene que ser un origen registrado para la app `foxprofile` en la plataforma de identidad (lo normal es el dominio del hub). Las variables `FOXPROFILE_S3_*` son opcionales, igual que en `.env.hub.example`.
3. **Domains → Add Domain**: el dominio del hub, servicio `hub`, puerto `8000` (el del contenedor, no `FOXPROFILE_HUB_PORT`), HTTPS con Let's Encrypt. Traefik entra por la red de Dokploy; el puerto publicado solo sirve para entrar directo por `http://<ip>:<FOXPROFILE_HUB_PORT>`.
4. **Deploy.** Comprobar `https://<dominio>/api/v1/info` (debe decir `"mode": "hub"`) y entrar al panel.
5. **Volume Backups** (opcional, recomendado): respaldar el volumen `foxprofile-data` a S3. Contiene `hub.json`, `profiles.json`, las sesiones de cada perfil (cookies) y los logs; trátelo como credenciales.

Notas:

- **Una sola réplica.** Los datos son archivos JSON escritos por un solo proceso; dos contenedores sobre el mismo volumen se pisarían.
- **No use `./data` en Dokploy.** Cada despliegue vuelve a clonar el repositorio y deja vacía esa carpeta; por eso este compose usa un volumen con nombre. Para pasar datos de un hub anterior, copie su `./data` dentro del volumen con el contenedor detenido.
- **Plataforma en el mismo servidor.** Si la API de la plataforma también corre en Dokploy, `FOXPROFILE_AUTH_URL` puede apuntar a su servicio en la red interna (`http://<servicio>:<puerto>`) en vez del dominio público: no pasa por Cloudflare ni por su protección contra bots. El `Origin` se sigue mandando igual.
- **Cloudflare delante del hub.** Con el proxy naranja, Cloudflare corta las peticiones de más de 100 MB (planes Free y Pro), y el agente sube el perfil completo al cerrarlo. Si hay perfiles grandes, deje el dominio del hub en *DNS only*.
- **Subidas lentas.** Traefik v3 corta en 60 s la lectura de una petición (`respondingTimeouts.readTimeout`). Si un perfil grande tarda más en subir desde una conexión lenta, suba ese límite en la configuración de Traefik de Dokploy (*Settings → Traefik*).
- Los agentes usan `https://<dominio>` como dirección del hub.

## 2. Usuarios y perfiles

### Inicio de sesión con la plataforma de identidad

El hub no guarda contraseñas. Cada operador entra con el correo y la contraseña de la plataforma de identidad interna: el hub los manda a `POST {FOXPROFILE_AUTH_URL}/auth/login` con la cabecera `Origin: {FOXPROFILE_AUTH_ORIGIN}`, y si la plataforma acepta, abre su propia sesión (12 h). El token de la plataforma no se guarda.

- La cuenta local se crea sola en el primer inicio de sesión, con el correo como nombre. Guarda solo lo de FoxProfile: rol, perfiles asignados, grupos y claves.
- El rol se toma de la plataforma en cada inicio de sesión: quien tenga el permiso `FOXPROFILE_AUTH_ADMIN_PERMISSION` (por defecto `foxprofile:admin`; `*:*` también vale) es *Admin*, los demás *Operator*.
- Los mensajes de la plataforma llegan traducidos: credenciales incorrectas, sin acceso (403), espera por intentos fallidos o plataforma caída (502).

Alta de FoxProfile en la plataforma (una vez):

1. Registrar la app `foxprofile` con un origen igual a `FOXPROFILE_AUTH_ORIGIN` y permiso requerido `foxprofile:access`. El hub llama desde el servidor, así que no hace falta agregar ese origen al CORS de la plataforma.
2. Crear los permisos `foxprofile:access` y `foxprofile:admin` y asignarlos a los roles que correspondan.
3. Todos los inicios de sesión llegan a la plataforma desde la IP del hub: si limita intentos fallidos por IP, una racha de errores bloquea un rato el acceso a FoxProfile para todos.

Quitar el acceso a alguien: desactivarlo en la plataforma **y** borrarlo en **Users**. Las claves del agente y de MCP no caducan y no pasan por la plataforma; borrar el usuario las invalida.

### Asignar perfiles

1. Entrar al panel con una cuenta que tenga `foxprofile:admin`.
2. **Users** (barra lateral): lista de quienes ya entraron alguna vez, con su rol. Un operador aparece después de su primer inicio de sesión.
3. Cada perfil tiene un **Operator** (en el diálogo *Edit*). Los perfiles que crea un operador quedan asignados a él. Un perfil sin asignar solo lo ven los admins.

### Grupos

Los grupos sirven para que varios operadores, cada uno en su PC, compartan un conjunto de perfiles sin reasignarlos uno por uno.

- Un perfil está en **un grupo como máximo** (campo *Group* del diálogo *Edit*). Lo ven y lo abren su operador, los admins y **todos los miembros del grupo**.
- Mientras un miembro tiene el perfil abierto, nadie más puede abrirlo; el panel muestra quién lo tiene.
- **Groups** (barra lateral): el admin crea y borra grupos y decide quién es *Lead* (líder) o *Member*.
- Un **líder** puede sumar o quitar miembros de su grupo y mover a él los perfiles que ve (o sacarlos). No crea grupos ni usuarios, y no puede nombrar ni quitar a otros líderes.
- Borrar un grupo no borra sus perfiles: quedan con su operador.

### Idioma

El panel arranca en español; cada usuario puede cambiarlo en **Settings → General** (Español, English, Tiếng Việt), y los mensajes de error del servidor siguen ese idioma. `FOXPROFILE_LANG=es` en el `.env` del hub fija el idioma de los agentes de todas las PCs (salvo que una PC defina su propio `FOXPROFILE_LANG`).

## 3. Instalar el agente en cada PC

### Windows: instalador MSI (recomendado)

`FoxProfileAgent-<versión>.msi` trae todo lo necesario (Python y el navegador del motor Chrome incluidos), así que la PC no necesita nada instalado. Se descarga de los artefactos del workflow *Agent installers* en GitHub Actions, o se arma con `installer/windows/build-msi.sh` (Linux/macOS con `uv` y `msitools`).

- **Hub fijo por build (dev / prod), obligatorio:** `HUB_URL=https://hub.example.com installer/windows/build-msi.sh` (igual con `build-pkg.sh`) deja esa URL dentro del instalador, y el agente se conecta ahí sin preguntar; sin una URL `http(s)://` el build se detiene. En el workflow sale del campo *hub_url* al lanzarlo a mano o, si no, de la variable del repositorio `AGENT_HUB_URL`. `FOXPROFILE_HUB_URL` en el `.env` del usuario o `--hub` siguen teniendo prioridad.

- Instalar con doble clic (pide permisos de administrador), o sin preguntas: `msiexec /i FoxProfileAgent-<versión>.msi /qn`.
- Al terminar una instalación con doble clic el agente arranca enseguida (una instalación silenciosa `/qn` lo deja para el siguiente inicio de sesión). Después arranca solo **cada vez que un usuario inicia sesión en Windows**, sin ventana de consola: queda como icono del zorro en la bandeja del sistema. También está en el menú Inicio como *FoxProfile Agent*.
- La primera vez descarga Camoufox (~1.3 GB; el icono muestra el porcentaje y avisa con una notificación cuando está listo) mientras abre el navegador para iniciar sesión, las dos cosas a la vez (ver [Inicio de sesión del agente](#inicio-de-sesión-del-agente)). Si el hub no responde (por ejemplo, la red todavía no está lista), reintenta cada 10 segundos.
- Los datos de cada usuario de Windows quedan en `%LOCALAPPDATA%\FoxProfile` (`.env`, `agent.json`, `logs`, copias locales de perfiles); desinstalar no los borra.
- Para actualizar, abrir el `.msi` nuevo con doble clic: detecta la versión instalada y la reemplaza (también si tiene el mismo número de versión; no instala una versión más vieja encima de una nueva). Antes de copiar archivos cierra el agente y los navegadores que tenía abiertos, sin pedir reiniciar; los datos de esos perfiles se suben al hub cuando arranca el agente nuevo. La sesión del agente se conserva.
- No es un servicio de Windows: los servicios corren sin escritorio y no podrían mostrar los navegadores al operador.

### macOS: instalador .pkg (recomendado)

`FoxProfileAgent-<versión>-macos.pkg` trae su propio Python y el navegador del motor Chrome, así que el Mac no necesita nada instalado. Requiere **Apple silicon (M1 o posterior) y macOS 15 o superior**; el instalador rechaza otros Mac (la librería que usa Camoufox para generar fingerprints no tiene versión funcional para Intel ni para macOS anteriores). Se descarga de los artefactos del workflow *Agent installers* o se arma con `installer/macos/build-pkg.sh` (en un Mac, con `uv`).

- El paquete no está firmado: la primera vez, clic derecho sobre el `.pkg` → Abrir → Abrir. Pide la contraseña de administrador.
- Instala *FoxProfile Agent* en Aplicaciones y lo arranca enseguida para el usuario actual, y luego **en cada inicio de sesión** de cualquier usuario del Mac (macOS avisa de un nuevo ítem en segundo plano). Queda como icono del zorro en la barra de menú, sin ventana ni icono en el Dock. Si se cerró con *Salir*, se vuelve a abrir desde Aplicaciones.
- La primera vez descarga Camoufox (~1.3 GB, con el porcentaje en el icono y una notificación al terminar) mientras abre el navegador para iniciar sesión (ver [Inicio de sesión del agente](#inicio-de-sesión-del-agente)).
- Los datos de cada usuario quedan en `~/Library/Application Support/FoxProfile` (`.env`, `agent.json`, `logs`, copias locales de perfiles).
- Para actualizar, abrir el `.pkg` nuevo: el instalador ofrece *Actualizar*, cierra el agente y los navegadores que tenía abiertos, reemplaza la app entera (sin dejar archivos de la versión anterior) y vuelve a arrancar el agente. Los datos de los perfiles abiertos se suben al hub cuando arranca el agente nuevo, y la sesión se conserva.
- Desinstalar (en Terminal):

  ```bash
  launchctl bootout gui/$(id -u) /Library/LaunchAgents/io.github.chienbm98.foxprofile-agent.plist
  sudo rm -rf "/Applications/FoxProfile Agent.app" /Library/LaunchAgents/io.github.chienbm98.foxprofile-agent.plist
  sudo pkgutil --forget io.github.chienbm98.foxprofile-agent
  ```

### Script (Windows y macOS)

Requisitos: Python 3.10 o superior de [python.org](https://www.python.org/downloads/) (en Windows marcar *Add python.exe to PATH*), y descargar este repositorio (git clone o ZIP de GitHub).

- **Windows:** doble clic en `foxprofile-agent.bat`.
- **macOS:** doble clic en `foxprofile-agent.command` (la primera vez: clic derecho → Abrir).

La primera ejecución instala las dependencias y descarga Camoufox (unos minutos). Después el agente sigue en segundo plano, con el icono del zorro en la bandeja (Windows) o en la barra de menú (macOS), y la ventana del script se puede cerrar. En Linux, o con `python -m src.agent --console`, el agente se queda en la terminal.

### Inicio de sesión del agente

El agente no pide contraseña: abre el navegador en el panel del hub.

1. Si todavía no conoce el hub, primero muestra una página para escribir su URL, p. ej. `http://192.168.1.10:8000` (o se pasa con `--hub` / `FOXPROFILE_HUB_URL`).
2. En el panel, el operador inicia sesión. Si ya tenía la sesión abierta en ese navegador, pasa directo al paso 3.
3. La página **Autorizar el inicio de sesión en el agente** muestra la cuenta (usuario, rol, grupos, hub) y la PC que lo pide. *Autorizar* conecta el agente; *Cambiar cuenta* cierra la sesión del panel para entrar con otro usuario; *Cancelar* no conecta nada.

El panel entrega al agente una clave propia (no la contraseña ni la clave MCP), que se guarda en `agent.json` junto al `.env` y no caduca. Volver a autorizar ese usuario, desde esta PC u otra, reemplaza la clave anterior, y el agente que la tenía vuelve a pedir inicio de sesión.

Menú del icono:

- el estado: *Conectado como ana · hub*, *Inicia sesión para conectar*, *Sin conexión con el hub*…
- **Iniciar sesión… / Cambiar cuenta…**: repite el inicio de sesión en el navegador. Al cambiar de cuenta, los navegadores abiertos se cierran y se suben antes.
- **Abrir panel**
- **Ver logs**: abre una ventana que muestra el log del agente y sigue las líneas nuevas (se puede seleccionar y copiar).
- **Salir**: cierra los navegadores; lo que quede sin subir se sube al volver a abrir el agente.

Un agente por usuario: si el mismo usuario abre el agente en dos PCs, cada orden la recibe la primera que consulte al hub. Para trabajar los mismos perfiles desde varias PCs, cree un usuario por PC u operador y póngalos en un mismo **grupo**.

### Actualizaciones del agente (desde el panel)

Los agentes instalados con el MSI o el `.pkg` siguen solos la versión del hub: **actualizar el hub actualiza los agentes**, sin repartir instaladores.

- En cada consulta el agente le dice al hub qué código tiene. Si el hub corre otro, el agente lo descarga (`/api/v1/hub/agent/code`), lo guarda en su carpeta de datos (`agent-code/`) y se reinicia. Lo hace en cuanto no tiene perfiles abiertos, así que nadie pierde una sesión a mitad de trabajo.
- Solo viaja el código (`src/` y `chrome_engine/`). Python y las librerías vienen con el instalador. Si la versión nueva necesita otras librerías (cambió `requirements.txt`), el agente no se actualiza: avisa con una notificación de que hace falta el instalador nuevo.
- **Ajustes › Agentes** en el panel:
  - Cualquier usuario descarga ahí el instalador de Windows o de macOS para un PC nuevo. La descarga no necesita sesión, así que el enlace se puede compartir.
  - Los administradores suben los instaladores (uno por sistema; el nuevo reemplaza al anterior). Sin bucket quedan en `installers/`, junto a `hub.json` (`./data/installers` con Docker); detrás de nginx, esa subida necesita el bloque `location /api/v1/hub/installers/` de [deploy/nginx-foxprofile.conf](../deploy/nginx-foxprofile.conf), porque supera el límite general de 20 MB. Con bucket, ver abajo.
  - Los administradores ven cada agente con su PC y su estado: *Al día*, *Se actualiza al cerrar sus perfiles*, *Necesita el instalador nuevo*, *Versión anterior* (instalado antes de que existieran las actualizaciones: necesita el instalador nuevo una vez) o *Corre con el script* (se actualiza con `git pull`). *Actualizar ahora* lo actualiza aunque tenga perfiles abiertos: se cierran y sus datos se suben al hub cuando arranca el agente nuevo.
- **Instaladores en un bucket S3 (RustFS, MinIO, AWS).** Con `FOXPROFILE_S3_ENDPOINT`, `FOXPROFILE_S3_BUCKET`, `FOXPROFILE_S3_ACCESS_KEY` y `FOXPROFILE_S3_SECRET_KEY` en el `.env` del hub (ver `.env.hub.example`), los instaladores se guardan en `<bucket>/fox-profiles/<entorno>/installers/windows/` y `…/installers/macos/`, donde `<entorno>` es `prod` o `dev` según `FOXPROFILE_S3_ENV` (`prod` si no se indica). FoxProfile no toca nada fuera de `fox-profiles/`, así que el bucket se puede compartir con otras apps, y un hub de pruebas con `dev` no pisa los instaladores de producción. El bucket sigue privado: el hub firma URLs temporales (prefirmadas), así que el archivo va directo entre el navegador y el bucket, sin pasar por el hub ni por nginx.
  - **Descargar**: el enlace del panel redirige a una URL firmada válida 15 minutos.
  - **Subir desde el panel**: la subida va por partes de 8 MB (multipart de S3). El hub abre la subida y firma un `PUT` por parte; el navegador sube cada parte directo al bucket y, al terminar, el hub las une y borra el instalador anterior. Si algo falla, el hub cancela la subida para que las partes no ocupen espacio. Las partes existen porque un proxy delante del bucket suele limitar el tamaño de cada petición: Cloudflare corta en 100 MB en sus planes Free y Pro, o cuando el servidor tarda más de 100 s en responder (error 524, típico de una parte grande en una conexión lenta), y lo hace sin cabeceras CORS, así que el navegador lo ve como un error de red.
  - El bucket necesita una regla **CORS** que permita `PUT` desde la dirección del panel y que **exponga `ETag`** (el navegador necesita leer el ETag de cada parte para unirlas). Sin ella el panel avisa de que el navegador no pudo subir. Ejemplo (`aws s3api put-bucket-cors --bucket foxprofile --cors-configuration file://cors.json --endpoint-url …`):

    ```json
    {"CORSRules": [{"AllowedOrigins": ["https://foxprofile.example.com"], "AllowedMethods": ["PUT"], "AllowedHeaders": ["*"], "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 3600}]}
    ```

  - Si el bucket está detrás de Cloudflare, su protección contra bots puede bloquear clientes que no son navegadores (por ejemplo `curl` recibe *Attention Required*); el panel no se ve afectado.

  - **Subir con otra herramienta**: un archivo `.msi` o `.pkg` puesto en esas carpetas (por ejemplo `fox-profiles/prod/installers/windows/`) con la consola de RustFS, `mc` o `aws s3 cp` también aparece en el panel (se ofrece el más reciente). No necesita CORS.
  - Si los navegadores llegan al bucket por otra dirección que el hub (por ejemplo, el hub usa `http://rustfs:9000` dentro de Docker), poner esa dirección pública en `FOXPROFILE_S3_PUBLIC_URL`: una URL firmada solo vale para el host con el que se firmó.
  - La clave de acceso solo necesita listar, leer, escribir y borrar en ese bucket. La imagen Docker ya incluye `boto3`; fuera de Docker, `pip install -r requirements-hub.txt`.
- Instalar un instalador más nuevo encima vuelve a usar su propio código, hasta que el hub mande uno distinto.
- El agente ejecuta el código que le da el hub. Quien controle el hub controla los agentes, así que conviene protegerlo como un servidor de despliegue (HTTPS, contraseñas fuertes).

### Importar desde AdsPower

*Importar desde AdsPower* (Ajustes del panel) lee el AdsPower del PC de quien lo usa a través de su agente, porque el hub no puede llegar a él: el agente debe estar conectado y AdsPower abierto con la API local activada. Los perfiles se crean en el hub con su dueño y su persona Android, y las cookies se cargan en ese mismo PC. La lista se agrupa por grupo de AdsPower, con el recuento de importados, pendientes y errores de cada uno, y cada perfil entra al grupo del hub con ese nombre: si no existe, lo crea un admin al importar; un operador solo puede llenar grupos de los que es líder. Los nombres se reducen a letras, números, `-` y `_` ("Ventas México" pasa a `Ventas-Mexico`). Reimportar coloca en su grupo también los perfiles importados antes. Detalles de lo que se migra en el [README](../README.md#migrating-from-adspower).

## Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| "Your PC is not connected" al abrir | El agente no está corriendo en la PC de ese usuario, o no alcanza al hub (firewall, IP). |
| Perfil queda "Running" y la PC se apagó | Pulsar *Stop*: con el agente desconectado el hub libera el perfil al instante. Se conserva la última versión subida. |
| El agente se cerró con un navegador abierto | Al volver a abrir el agente sube automáticamente lo que quedó pendiente. |
| El agente vuelve a abrir el navegador pidiendo autorización | Su clave ya no vale: se autorizó ese usuario en otra PC o se cambió la clave. Autorice de nuevo. |
| "hub refused the upload (409)" en el agente | Otro operador abrió el perfil mientras tanto; la copia local queda en `camoufox_data/<perfil>` para recuperarla a mano. |

## Seguridad

- El panel va por HTTP dentro de la LAN: las contraseñas de la plataforma viajan sin cifrar hasta el hub. Si la red no es de confianza, ponga el hub detrás de HTTPS (nginx, ver [DEPLOY.md](DEPLOY.md)).
- Los perfiles contienen sesiones iniciadas (cookies). `./data` y las copias de seguridad deben tratarse como credenciales.
- Los agentes instalados ejecutan el código que reciben del hub (ver [Actualizaciones del agente](#actualizaciones-del-agente-desde-el-panel)): un hub comprometido llega a todas las PCs. Sirva el hub por HTTPS.
- `agent.json` contiene la clave del agente: quien la copie puede actuar como ese operador. Para revocarla, autorice de nuevo el agente.
- `FOXPROFILE_API_TOKEN` sigue funcionando como clave de administrador para scripts (`Authorization: Bearer <token>`), también si la plataforma de identidad está caída. Cambiarlo cierra todas las sesiones.

## Panel de Army

Army (`/perfiles-users` y `/perfiles-admin`) abre los perfiles en nombre de cada usuario con una clave propia de ese usuario, nunca con `FOXPROFILE_API_TOKEN`:

1. En Army, el usuario pulsa *Vincular con Fox*. Army abre el panel del hub en `#army=<state>`.
2. Inicia sesión (la misma cuenta de Army) y pulsa *Autorizar*. El panel crea una clave `army_key` (`POST /hub/me/army-key`, separada de la de MCP y la del agente) y la envía por POST, con el `state`, **solo** a `FOXPROFILE_ARMY_CALLBACK`. Esa dirección sale de la configuración del hub, no del enlace.
3. Army comprueba el `state` y que la clave sea de ese mismo correo, y la guarda cifrada.

Sin `FOXPROFILE_ARMY_CALLBACK` la vinculación está apagada. Para asignar perfiles a quien todavía no entró al hub, un admin crea la cuenta con `PUT /hub/users/{correo}` (si ya existe, conserva su rol).

## Visor remoto, cookies y MCP

Todo se ejecuta en la PC del operador; el hub solo reenvía las órdenes a su agente.

- **Check proxy / Check IP** se ejecutan en la PC del operador a través de su agente, igual que los navegadores. Así funcionan proxies que solo esa PC alcanza (por ejemplo, direcciones de Tailscale); sin el agente conectado, el panel lo avisa.
- **Visor remoto** (icono de monitor): ver y controlar un perfil abierto desde el panel. El admin puede ver los perfiles de cualquier operador; la acción ocurre en la PC de quien lo tiene abierto.
- **Cookies** (icono de galleta, con el perfil detenido): exportar JSON / cookies.txt o importar. El agente de quien lo pide descarga el perfil, lee o escribe las cookies con Camoufox y, al importar, lo sube de nuevo. Requiere que ese agente esté conectado.
- **MCP** (Claude, Cursor, VS Code): en *Connect AI (MCP)* pulse *Create my personal key*. La clave se muestra una sola vez y reemplaza la anterior. Un asistente con esa clave ve solo sus perfiles y los abre en su PC. La clave no caduca: trátela como una contraseña y regenérela si se filtra.

## Limitaciones actuales

Exportar/importar ZIP por ruta del servidor no está disponible en modo hub. Los datos se transfieren completos (sin caché del navegador) en cada apertura y cierre. El visor envía cada captura a través del hub (unos cientos de KB cada 2 s).
