# Apps registradas y orígenes

**Toda app tiene que identificarse para usar la API.** Un token lleva el slug de la app que
lo emitió (claim `app`) y cada petición autenticada lleva su `Origin`; los dos tienen que
apuntar a la misma `AppCliente` **activa**. Si no, no se pasa.

Dos gates:

1. **Al entrar** (`POST /auth/login`): se resuelve la app por el `Origin`. Si esa app tiene
   `permisoRequerido` y el usuario no lo tiene, 403 y sin token.
2. **En cada petición** (`AuthGuard` y los sockets): el claim `app` del token tiene que
   coincidir con el `Origin` de la petición.

Falla **en cerrado**, sin excepciones: sin `Origin`, sin claim, origen no registrado, app
desactivada, o token de una app usado desde otra → 401 `Aplicación no identificada`.

## Piezas

| Archivo | Qué hace |
|---|---|
| `src/entities/AppCliente.ts` | El registro: slug, orígenes, permiso requerido, activa |
| `src/lib/origins.ts` | `normalizeOrigin()` — la forma canónica de un origen |
| `src/apps/apps.service.ts` | `findByOrigin()` y **`coincideConOrigen()`**, la regla compartida |
| `src/apps/` | CRUD (`/apps`), protegido con `settings:read` / `settings:update` |
| `src/auth/auth.service.ts` | El gate del login + `verifyPermission()` |
| `src/guards/AuthGuard.ts` | Identificación en HTTP |
| `src/guards/SocketAuthGuard.ts`, `src/chat/chat.gateway.ts`, `src/gateways/analytics.gateway.ts` | Identificación en WebSocket |
| `src/config/initialConfig.ts` | Siembra `panel` y `scripts` |

Los cuatro puntos de entrada autenticados usan **`AppsService.coincideConOrigen()`**: si hay
que cambiar la regla, se cambia ahí y no en cuatro lados.

## Restricción dura: el panel y la API en orígenes distintos

Esto es ahora **load-bearing**. El navegador manda `Origin` en peticiones *cross-origin*,
pero lo **omite** en same-origin para GET/HEAD. Si algún día se sirven el panel y la API
bajo el mismo origen (un reverse proxy, `/api` en el mismo dominio), los GET del panel
llegarían sin `Origin` y `AuthGuard` los rechazaría todos.

Hoy se cumple: `devarmy.maxtres.org` → `api.maxtres.org` en producción, `:3000` → `:4000` en
local. Si eso cambia, hay que revisar este gate antes.

## Dos listas, a propósito

| | `FRONTEND_URL` (env) | `AppCliente.origins` (base) |
|---|---|---|
| Pregunta | ¿este navegador puede leer nuestras respuestas? | ¿qué app es y qué permiso exige? |
| Quién la aplica | el navegador (CORS) | nuestro código |
| Cuándo se lee | una vez al arrancar | por petición |

**CORS no lee de la base, y no es un olvido.** `chat.gateway.ts` y `analytics.gateway.ts`
llaman `allowedOrigins()` dentro del decorador `@WebSocketGateway`, que se evalúa al importar
el módulo — antes de que exista el ORM. Sumado a que sería un hit de Mongo por preflight y a
que una caída de Mongo dejaría a todos los navegadores fuera, no compensa.

## Alta de una app nueva

1. **Panel → Configuración → Aplicaciones → Agregar**: slug, nombre, **todos** sus orígenes
   (producción, staging y `http://localhost:PUERTO`) y el permiso que debe exigir.
2. **Agregar esos mismos orígenes a `FRONTEND_URL`** en las variables de Dokploy (separados
   por coma, sin espacios ni diagonal final) y **redesplegar**. Sin este paso el navegador no
   pasa ni el preflight y los sockets tampoco conectan.
3. **Verificar que algún rol tenga ese permiso**, o nadie podrá entrar — salvo ADMIN, que con
   `*:*` pasa cualquier gate.
4. Apps **sin navegador** van solo en el paso 1: meterlas en `FRONTEND_URL` abriría CORS para
   nada.

El listado del panel marca **"falta en FRONTEND_URL"** en cada origen que no esté en la env,
para que el paso 2 olvidado se vea ahí en vez de descubrirse con un error en consola.

⚠️ **Desactivar una app (`activa: false`) corta las sesiones en curso**, no solo los logins
nuevos: `coincideConOrigen` solo resuelve apps activas.

## Clientes sin navegador

Node no manda `Origin` por su cuenta — no existe "mismo origen" para un cliente HTTP fuera
del navegador, ni siquiera si corre en el mismo contenedor. Hay que fijarlo a mano, y ahora
hace falta en **todas** las peticiones, no solo en el login.

Inventario completo de clientes internos que pegan a esta API por HTTP:

- **`src/scripts/checar-jornada.ts`** (el cron de Dokploy) — pega al loopback
  (`http://127.0.0.1:$PORT`) en vez de salir a internet, y manda ese mismo valor como
  `Origin` desde su helper `post()`, así que cubre tanto el login como
  `POST /asistencia-remota/<action>`. Está registrado como la app `scripts`, sin permiso
  requerido.
- **La colección de Bruno** — `collection.bru` manda `Origin: {{origin}}` para **toda** la
  colección; el valor vive en cada entorno.

Los otros scripts de `src/scripts/` usan el ORM directo, no HTTP. Los `Authorization: Bearer`
de `perfiles.service.ts`, `socialFetch.ts` y `platformLookup.ts` son salientes (Facebook,
TikTok, YouTube), no hacia nosotros.

No afectados, porque tienen su propio controller **sin** `AuthGuard`:

- El checador ZKTeco → `@Controller('iclock')`, sin guard.
- El webhook de OpenWA → `tickets/webhooks/whatsapp`, con su propio `OPENWA_WEBHOOK_SECRET`.

## Al desplegar

1. **`npm run config:init` primero.** Si el gate llega a una base sin apps registradas, nadie
   entra al panel. Es idempotente: se puede correr las veces que haga falta.
2. **Todas las sesiones activas se cortan.** Los tokens emitidos antes del claim `app` ya no
   se toleran, así que cada usuario vuelve a iniciar sesión una vez. En el panel se ve como la
   tarjeta de sesión expirada y un rebote a `/signin`.
