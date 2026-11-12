# Reportes de cuentas

- [x] Persistencia, validación de cuentas y API protegida.
- [x] Puente temporal y extensión en Chrome y Camoufox.
- [x] Envío por agente al hub y atribución al operador.
- [x] Reportes pendientes/revisados, traducciones y documentación.
- [x] Cerrar verificación de regresión y revisión final.

Verificación: 687 pruebas pasan, 11 se omiten por sus condiciones existentes y
2 fallan también en una copia limpia de HEAD: la prueba antigua de carga de
extensiones de Chrome llama `_extension_args()` sin argumento y la prueba de
cierre solo cierra una de las pestañas abiertas. Pruebas nuevas de Chrome y
Camoufox pasan también con un proxy inaccesible: los reportes locales llegan.
Ruff y verificación del formato pasan.
