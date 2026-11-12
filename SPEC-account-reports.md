# Especificación: reportar el estado de cuentas desde el navegador

Estado: aprobada por el usuario para implementación el 2026-11-04.

## Objetivo

El operador marca el estado de una cuenta desde una extensión incluida en todos
los perfiles de Chrome y Camoufox. El reporte aparece en el panel de FoxProfile
para que el departamento de Cuentas pueda revisarlo.

Es una sola capacidad: reportar desde el navegador y revisar en el panel.
La detección de la red facilita el formulario; el usuario confirma el estado.

## Experiencia y criterios de aceptación

1. La extensión se carga automáticamente al abrir cualquier perfil, tanto local
   como desde un agente conectado al hub, aunque el perfil no tenga número.
2. Al abrir su botón, reconoce la red de la pestaña activa: Facebook, Instagram,
   TikTok o X. Gmail y Outlook también se admiten por estar en el catálogo actual.
   Solo acepta dominios oficiales y sus subdominios previstos; una coincidencia
   dentro de otro dominio no basta. En otros sitios informa que no se admiten.
3. El formulario muestra el perfil y la red. Si hay varias cuentas guardadas de
   esa red, obliga a elegir una. Si no hay ninguna, permite reportar el perfil y
   la red sin crear credenciales ni afirmar qué usuario está conectado.
4. El operador elige «Inhabilitado», «Suspendido», «Videoselfie» o «Código de
   teléfono/SMS/WhatsApp» y puede agregar
   una nota de hasta 1.000 caracteres. Enviar requiere una acción explícita.
5. Solo se muestra «Reporte enviado» después de confirmar su persistencia. Si
   falla la conexión o el guardado, mantiene el formulario y permite reintentar.
6. Se conserva el último reporte por perfil y cuenta; sin cuenta guardada, por
   perfil y red. Incluye estado, nota, fecha del servidor y operador autenticado
   cuando el lanzamiento tiene identidad. Un nuevo reporte queda sin revisar.
7. El panel muestra los reportes y permite filtrar los pendientes de revisión.
   Todos los estados quedan destacados para Cuentas. La actualización entra
   en el refresco normal del panel, actualmente cada cuatro segundos si está visible.
8. Cuentas puede marcar el último reporte como revisado; se registra quién y cuándo.
   Revisar no cambia el estado que informó el operador. Si llega un reporte nuevo
   mientras se revisa uno anterior, el nuevo sigue pendiente.
9. La visibilidad sigue los permisos actuales de perfiles y grupos. El equipo de
   Cuentas necesita acceso a los grupos correspondientes; no se crea un rol global.
10. Los reportes persisten al reiniciar y siguen la identidad del perfil al renombrarlo.
    Eliminar un perfil elimina sus reportes. No se modifican sus sesiones ni sus
    credenciales. El cambio anterior para abrir Facebook se conserva.

## Stack y alcance del código

- Python 3.10+, FastAPI/Pydantic y persistencia local siguiendo los stores existentes.
- WebExtensions: manifiesto compatible con Chrome y con Firefox/Camoufox.
- Panel HTML/CSS/JavaScript existente; conservar sus componentes y traducciones
  español, inglés y vietnamita. Formularios con etiquetas, foco y errores accesibles.
- Reutilizar la carga de extensiones y el patrón del badge en
  `src/services/browser/label.py`; el reporte debe funcionar sin número de perfil.
- Revisar los dos runners, `src/services/browser/process.py`, el launcher,
  `src/agent.py`, los comandos del hub y el empaquetado para cubrir cada lanzamiento.
- El almacenamiento y la API se integran con los perfiles existentes; la interfaz
  se incorpora a `src/web/index.html`. Las pruebas viven en `tests/` y
  `chrome_engine/tests/`; no se agregan dependencias.

## Contrato y seguridad

El reporte contiene `platform`, `account_id` opcional, `status` y `note`.
Los únicos estados admitidos son `inhabilitado`, `suspendido`, `videoselfie` y `codigo`
(mismas claves que el estado de perfil del panel Army).
El servidor determina perfil autorizado, identidad y fechas; no confía en esos
valores si llegan desde el formulario.

La extensión solo puede reportar para su perfil de lanzamiento. La comunicación
con el agente/hub debe usar una autorización limitada al reporte, nunca entregar
la clave general del panel o del agente a la extensión o a una página web.
No se leen contraseñas, cookies, códigos 2FA ni contenido privado de las redes.
Las notas se muestran como texto y se valida su longitud en el servidor.
Reintentar el mismo envío no debe crear reportes ni tareas duplicadas. Los recibos
de reintento se conservan durante siete días, sin guardar un historial de notas.

## Estilo

Seguir los módulos y stores actuales, nombres Python en snake_case, Ruff con
líneas de 100 caracteres y los helpers de DOM del panel. Ejemplo del estilo existente:

```python
class AccountResponse(BaseModel):
    id: str
    platform: str
    username: str
    has_password: bool
    has_totp: bool
```

## Comandos y verificación

- Pruebas: `rtk proxy .venv/bin/pytest -q`.
- Pruebas existentes relacionadas:
  `rtk proxy .venv/bin/pytest -q tests/test_accounts.py tests/test_extensions.py tests/test_label.py tests/test_launcher.py tests/test_hub.py tests/test_agent_auth.py`.
- Lint: `rtk proxy .venv/bin/ruff check src tests chrome_engine`.
- Formato: `rtk proxy .venv/bin/ruff format --check src tests chrome_engine`.
- Agregar pruebas enfocadas de persistencia, validación, permisos, aislamiento de
  perfiles y revisión concurrente. Probar la extensión con ambos motores y el
  flujo agente → hub → panel, incluyendo desconexión y reintento.
- Verificar que no se guardan secretos en reportes y que no se debilitan las
  restricciones actuales de las rutas de credenciales. Comparar cualquier fallo
  de la suite con la base existente antes de atribuirlo al cambio.

## Límites

- Siempre: validar entradas, preservar permisos, confirmar el guardado antes del
  éxito y conservar los cambios de trabajo anteriores.
- Consultar antes: agregar otras redes, detección automática de sesiones,
  permisos nuevos para Cuentas o un historial completo de incidencias.
- Nunca: intentar iniciar sesión, cambiar contraseñas, resolver bloqueos o mandar
  notificaciones externas como parte de este reporte.

## Decisiones propuestas para aprobación

- La marca de estado es manual; no se deduce automáticamente de la página.
- Se conserva el último reporte, con pendiente/revisado, sin un historial completo.
- El departamento de Cuentas usa los permisos actuales de grupos y perfiles.
- No se incorpora una cola sin conexión: si no se pudo guardar, el usuario recibe
  un error y puede reintentar desde el formulario.
