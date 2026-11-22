# Política de seguridad

## Qué contiene este repositorio

Código, pruebas sintéticas, configuración de despliegue genérica y
documentación de arquitectura. **No contiene**:

- Credenciales, tokens, cookies, claves privadas ni secretos de ningún tipo.
  Toda la configuración sensible vive fuera de Git (archivos privados,
  variables de entorno o secretos del sistema).
- El corpus real, documentos originales, materiales del alumnado, calificaciones
  ni datos personales: los originales y los artefactos derivados se guardan
  fuera del control de versiones.
- Valores operativos reales del despliegue (identificadores de proyecto,
  hosts privados, listas de acceso): los identificadores de ejemplo
  (`your-project-ref`, `TU-PROJECT-REF`, `moodle.example.edu`) son marcadores
  que cada despliegue sustituye por su configuración propia mediante variables
  de entorno.

## Cómo se valida

- La suite de pruebas es sintética: no necesita corpus real, credenciales ni
  acceso a servicios externos; las pruebas que requieren datos privados se
  omiten automáticamente si estos no existen.
- La CI construye la imagen y comprueba estáticamente que el contexto Docker
  excluye datos, backups y credenciales, y que el contenedor corre sin
  privilegios.
- El servidor autoriza cada petición contra una lista privada de usuarios y
  asignaturas; la pantalla de consentimiento no es una lista de acceso.

## Reporte de vulnerabilidades

Este es un proyecto educativo sin garantías. Si encuentras un problema de
seguridad, no lo publiques: abre una incidencia describiéndola sin incluir
detalles explotables, y se coordinará la corrección. No incluyas nunca
credenciales propias en la incidencia.

## Datos personales

No envíes a este repositorio material con datos personales ni institucionales
(abre incidencias sin contenido de documentos). El registro de cursos real y
las referencias de evaluación de un despliegue concreto se mantienen privados.
