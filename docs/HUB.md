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

## 3. Instalar el agente en cada PC

### Windows: instalador MSI (recomendado)

`FoxProfileAgent-<versión>.msi` trae todo lo necesario (Python incluido), así que la PC no necesita nada instalado. Se descarga de los artefactos del workflow *Agent MSI* en GitHub Actions, o se arma con `installer/windows/build-msi.sh` (Linux/macOS con `uv` y `msitools`).

- Instalar con doble clic (pide permisos de administrador), o sin preguntas: `msiexec /i FoxProfileAgent-<versión>.msi /qn`.
- El agente arranca solo **cada vez que un usuario inicia sesión en Windows** y queda en una ventana de consola. También está en el menú Inicio como *FoxProfile Agent*.
- La primera vez descarga Camoufox y pide la URL del hub, el usuario y la contraseña, igual que el script de abajo. Si el hub no responde (por ejemplo, la red todavía no está lista), reintenta cada 10 segundos.
- Los datos de cada usuario de Windows quedan en `%LOCALAPPDATA%\FoxProfile` (`.env`, `logs`, copias locales de perfiles); desinstalar no los borra.
- No es un servicio de Windows: los servicios corren sin escritorio y no podrían mostrar los navegadores al operador.

### Script (Windows y macOS)

Requisitos: Python 3.10 o superior de [python.org](https://www.python.org/downloads/) (en Windows marcar *Add python.exe to PATH*), y descargar este repositorio (git clone o ZIP de GitHub).

- **Windows:** doble clic en `foxprofile-agent.bat`.
- **macOS:** doble clic en `foxprofile-agent.command` (la primera vez: clic derecho → Abrir).

La primera ejecución instala las dependencias y descarga Camoufox (unos minutos), y pregunta:

- URL del hub, p. ej. `http://192.168.1.10:8000`
- usuario y contraseña del operador

La URL y el usuario se guardan en `.env` de esa carpeta; la contraseña se pide en cada arranque (o se define `FOXPROFILE_AGENT_PASSWORD` en ese `.env` si la PC es de uso personal). **Deje la ventana del agente abierta** mientras trabaja.

Un agente por operador: si el mismo usuario abre el agente en dos PCs, cada orden la recibe la primera que consulte al hub.

## Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| "Your PC is not connected" al abrir | El agente no está corriendo en la PC de ese usuario, o no alcanza al hub (firewall, IP). |
| Perfil queda "Running" y la PC se apagó | Pulsar *Stop*: con el agente desconectado el hub libera el perfil al instante. Se conserva la última versión subida. |
| El agente se cerró con un navegador abierto | Al volver a abrir el agente sube automáticamente lo que quedó pendiente. |
| "hub refused the upload (409)" en el agente | Otro operador abrió el perfil mientras tanto; la copia local queda en `camoufox_data/<perfil>` para recuperarla a mano. |

## Seguridad

- El panel va por HTTP dentro de la LAN: las contraseñas viajan sin cifrar. Si la red no es de confianza, ponga el hub detrás de HTTPS (nginx, ver [DEPLOY.md](DEPLOY.md)).
- Los perfiles contienen sesiones iniciadas (cookies). `./data` y las copias de seguridad deben tratarse como credenciales.
- `FOXPROFILE_API_TOKEN` sigue funcionando como clave de administrador para scripts (`Authorization: Bearer <token>`). Cambiarlo cierra todas las sesiones.

## Visor remoto, cookies y MCP

Todo se ejecuta en la PC del operador; el hub solo reenvía las órdenes a su agente.

- **Check proxy / Check IP** se ejecutan en la PC del operador a través de su agente, igual que los navegadores. Así funcionan proxies que solo esa PC alcanza (por ejemplo, direcciones de Tailscale); sin el agente conectado, el panel lo avisa.
- **Visor remoto** (icono de monitor): ver y controlar un perfil abierto desde el panel. El admin puede ver los perfiles de cualquier operador; la acción ocurre en la PC de quien lo tiene abierto.
- **Cookies** (icono de galleta, con el perfil detenido): exportar JSON / cookies.txt o importar. El agente de quien lo pide descarga el perfil, lee o escribe las cookies con Camoufox y, al importar, lo sube de nuevo. Requiere que ese agente esté conectado.
- **MCP** (Claude, Cursor, VS Code): en *Connect AI (MCP)* pulse *Create my personal key*. La clave se muestra una sola vez y reemplaza la anterior. Un asistente con esa clave ve solo sus perfiles y los abre en su PC. La clave no caduca: trátela como una contraseña y regenérela si se filtra.

## Limitaciones actuales

Exportar/importar ZIP por ruta del servidor no está disponible en modo hub. Los datos se transfieren completos (sin caché del navegador) en cada apertura y cierre. El visor envía cada captura a través del hub (unos cientos de KB cada 2 s).
