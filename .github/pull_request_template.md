## Descripción

<!-- Qué cambia y por qué. Enlaza el issue si lo hay: "Closes #123" -->

## Tipo de cambio

- [ ] Corrección de error (fix)
- [ ] Funcionalidad nueva (feat)
- [ ] Documentación (docs)
- [ ] Refactor / limpieza de código
- [ ] Cambio que rompe la compatibilidad (breaking change)

## Comprobado

- [ ] `ruff check src tests chrome_engine` y `ruff format --check src tests chrome_engine` sin errores
- [ ] `pytest` pasa completo
- [ ] Añadí tests para el cambio (si toca lógica)
- [ ] Probé la app y describo cómo probarlo abajo
- [ ] Los textos nuevos de la interfaz están en `src/core/strings.py` en `en`, `vi` y `es`
- [ ] El PR no contiene cookies, proxies ni datos de cuentas reales

## Cómo probarlo

<!-- Pasos para que quien revise lo compruebe por su cuenta -->
