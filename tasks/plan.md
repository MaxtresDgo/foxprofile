# Reportes de cuentas

Especificación aprobada: [SPEC-account-reports.md](../SPEC-account-reports.md).

1. Persistencia por UID de perfil y cuenta, API con permisos y revisión concurrente.
2. Puente local limitado por lanzamiento, reutilizando el badge de ambos motores;
   envío local o a través del agente autenticado al hub.
3. Formulario de extensión y reportes en el panel, con traducciones y documentación.
4. Pruebas de permisos, persistencia, reintento, recorrido del agente y ambos motores;
   comparar los fallos de regresión contra HEAD sin alterar el árbol de trabajo.

Se preservan los cambios previos para abrir Facebook. No se agregan dependencias,
detección automática de sesiones, notificaciones ni una cola sin conexión.
