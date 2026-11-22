# Registro de cursos y exclusiones

## Formato

`config/courses.json` es el registro validado de cursos que consume
`scripts/course_registry.py` (y la sincronización nocturna). El esquema:

```json
{
  "version": 1,
  "courses": [
    {
      "slug": "identificador-curso",
      "subject": "SIGLAS",
      "course_url": "https://moodle.example.edu/course/view.php?id=1001",
      "exclusions": [
        {"url": "https://moodle.example.edu/mod/assign/view.php?id=24601",
         "reason": "Motivo breve de la exclusión"}
      ]
    }
  ]
}
```

El cargador falla cerrado ante duplicados de `slug`/`subject`/URL, orígenes no
HTTPS, fragmentos, rutas no permitidas o ficheros fuera de `config/`. Las
exclusiones evitan descargar recursos concretos (entregas, listas de notas,
recursos que devuelven errores) y su `reason` queda registrado.

## Datos sintéticos

El fichero incluido en este repositorio es una **plantilla sintética** con
cursos de ejemplo en `moodle.example.edu` e identificadores obviamente falsos.
No es el registro real de producción: cada despliegue debe preparar su propio
registro autorizado y mantenerlo fuera del control de versiones si contiene
datos institucionales.

## Orígenes configurables

El origen del Moodle y los hosts de manuales aceptados son configurables por
entorno, con valores de ejemplo por defecto:

- `ASIR_MOODLE_ORIGIN` (por defecto `https://moodle.example.edu`)
- `ASIR_MOODLE_ORIGIN_HOST` (por defecto `moodle.example.edu`)
- `ASIR_MOODLE_COURSE_URL` (por defecto `https://moodle.example.edu/course/view.php?id=1001`)
- `ASIR_MANUAL_HOST`, `ASIR_MANUAL_PAGES_HOST` (hosts de manuales de ejemplo)

Cualquier URL fuera del origen configurado se rechaza antes de descargar.
