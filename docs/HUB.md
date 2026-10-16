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
#   FOXPROFILE_ADMIN_PASSWORD=   (contraseña del usuario admin, se crea al primer arranque)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # genera un token
docker compose up -d --build
```

El panel queda en `http://<ip-del-servidor>:8000/`. Los datos viven en `./data` (respáldelo con el contenedor detenido o sin perfiles abiertos).

Actualizar: `git pull && docker compose up -d --build`.

## 2. Crear usuarios y asignar perfiles

1. Entrar al panel como `admin`.
2. **Users** (barra lateral): crear un usuario por operador (rol *Operator*), cambiar contraseñas o borrar usuarios.
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

`FoxProfileAgent-<versión>.msi` trae todo lo necesario (Python incluido), así que la PC no necesita nada instalado. Se descarga de los artefactos del workflow *Agent MSI* en GitHub Actions, o se arma con `installer/windows/build-msi.sh` (Linux/macOS con `uv` y `msitools`).

- Instalar con doble clic (pide permisos de administrador), o sin preguntas: `msiexec /i FoxProfileAgent-<versión>.msi /qn`.
- El agente arranca solo **cada vez que un usuario inicia sesión en Windows**, sin ventana de consola: queda como icono del zorro en la bandeja del sistema. También está en el menú Inicio como *FoxProfile Agent*.
- La primera vez descarga Camoufox (el icono muestra *Descargando el navegador…*) y abre el navegador para iniciar sesión (ver [Inicio de sesión del agente](#inicio-de-sesión-del-agente)). Si el hub no responde (por ejemplo, la red todavía no está lista), reintenta cada 10 segundos.
- Los datos de cada usuario de Windows quedan en `%LOCALAPPDATA%\FoxProfile` (`.env`, `agent.json`, `logs`, copias locales de perfiles); desinstalar no los borra.
- No es un servicio de Windows: los servicios corren sin escritorio y no podrían mostrar los navegadores al operador.

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
- **Ver logs**: abre una terminal (PowerShell en Windows, Terminal en macOS) que sigue el log del agente.
- **Salir**: cierra los navegadores; lo que quede sin subir se sube al volver a abrir el agente.

Un agente por usuario: si el mismo usuario abre el agente en dos PCs, cada orden la recibe la primera que consulte al hub. Para trabajar los mismos perfiles desde varias PCs, cree un usuario por PC u operador y póngalos en un mismo **grupo**.

## Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| "Your PC is not connected" al abrir | El agente no está corriendo en la PC de ese usuario, o no alcanza al hub (firewall, IP). |
| Perfil queda "Running" y la PC se apagó | Pulsar *Stop*: con el agente desconectado el hub libera el perfil al instante. Se conserva la última versión subida. |
| El agente se cerró con un navegador abierto | Al volver a abrir el agente sube automáticamente lo que quedó pendiente. |
| El agente vuelve a abrir el navegador pidiendo autorización | Su clave ya no vale: se autorizó ese usuario en otra PC o se cambió la clave. Autorice de nuevo. |
| "hub refused the upload (409)" en el agente | Otro operador abrió el perfil mientras tanto; la copia local queda en `camoufox_data/<perfil>` para recuperarla a mano. |

## Seguridad

- El panel va por HTTP dentro de la LAN: las contraseñas viajan sin cifrar. Si la red no es de confianza, ponga el hub detrás de HTTPS (nginx, ver [DEPLOY.md](DEPLOY.md)).
- Los perfiles contienen sesiones iniciadas (cookies). `./data` y las copias de seguridad deben tratarse como credenciales.
- `agent.json` contiene la clave del agente: quien la copie puede actuar como ese operador. Para revocarla, autorice de nuevo el agente.
- `FOXPROFILE_API_TOKEN` sigue funcionando como clave de administrador para scripts (`Authorization: Bearer <token>`). Cambiarlo cierra todas las sesiones.

## Visor remoto, cookies y MCP

Todo se ejecuta en la PC del operador; el hub solo reenvía las órdenes a su agente.

- **Check proxy / Check IP** se ejecutan en la PC del operador a través de su agente, igual que los navegadores. Así funcionan proxies que solo esa PC alcanza (por ejemplo, direcciones de Tailscale); sin el agente conectado, el panel lo avisa.
- **Visor remoto** (icono de monitor): ver y controlar un perfil abierto desde el panel. El admin puede ver los perfiles de cualquier operador; la acción ocurre en la PC de quien lo tiene abierto.
- **Cookies** (icono de galleta, con el perfil detenido): exportar JSON / cookies.txt o importar. El agente de quien lo pide descarga el perfil, lee o escribe las cookies con Camoufox y, al importar, lo sube de nuevo. Requiere que ese agente esté conectado.
- **MCP** (Claude, Cursor, VS Code): en *Connect AI (MCP)* pulse *Create my personal key*. La clave se muestra una sola vez y reemplaza la anterior. Un asistente con esa clave ve solo sus perfiles y los abre en su PC. La clave no caduca: trátela como una contraseña y regenérela si se filtra.

## Limitaciones actuales

Exportar/importar ZIP por ruta del servidor no está disponible en modo hub. Los datos se transfieren completos (sin caché del navegador) en cada apertura y cierre. El visor envía cada captura a través del hub (unos cientos de KB cada 2 s).
