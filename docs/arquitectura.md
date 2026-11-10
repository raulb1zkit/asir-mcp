# Arquitectura y operación

## Flujo de datos

1. La ingesta local obtiene documentos autorizados y guarda originales fuera de Git.
2. La extracción y el OCR generan texto con localizadores; la fragmentación
   conserva citas y advertencias de calidad.
3. Los índices léxico y semántico se preparan localmente y el corpus se importa
   a PostgreSQL/pgvector. El modelo de embeddings también debe estar preparado
   en el VPS. La búsqueda híbrida fusiona resultados por ranking.
4. Un cliente MCP solicita búsquedas y fragmentos. El servidor comprueba la
   credencial, limita las asignaturas disponibles y consulta la base de datos
   con un rol de lectura. El cliente decide si entrega los pasajes a su modelo.

El despliegue usa Cloudflare delante del VPS, Supabase para la base de datos y
Supabase Auth con Google para identidad. El VPS ejecuta el MCP, calcula embeddings
de consulta y sirve la landing. El servicio no necesita acceso a la sesión de
Moodle. Los archivos privados se mantienen fuera del repositorio.

## Acceso y administración

OAuth autentica al usuario, pero el servidor autoriza cada petición consultando
una lista privada de UUID y asignaturas. La pantalla de consentimiento no es una
lista de acceso. Se admiten también tokens personales independientes; cada uno
requiere su propia gestión de permisos, caducidad y revocación.

`scripts/manage_oauth_users.py` gestiona la lista desde terminal.
`scripts/admin_oauth.py` ofrece un panel temporal en `127.0.0.1`, con contraseña
generada por ejecución, comprobación de origen y protección CSRF. Debe usarse
mediante túnel SSH desde un equipo de confianza, **sin publicarlo en un proxy**.
El UUID se comprueba en Supabase Auth tras el primer inicio de sesión de la
persona; nunca se autoriza basándose en metadatos editables por ella.

En el despliegue existente, el administrador puede abrirlo así:

```bash
ssh -L 8765:127.0.0.1:8765 rag-asir 'sudo -n -u asir /opt/asir/venv/bin/python /opt/asir/app/scripts/admin_oauth.py'
```

Después abre `http://localhost:8765/` en su equipo y usa las credenciales
temporales de la terminal. Ctrl+C cierra el panel y el túnel. El alias y las
rutas son específicos de ese despliegue; no configuran automáticamente uno nuevo.

## Evaluación y límites

La evaluación de recuperación compara resultados contra referencias fijadas
para una versión concreta del corpus. Las referencias y el conjunto ampliado
derivado de los materiales permanecen privados. Algunas herramientas de
evaluación esperan esos archivos privados y no se pueden ejecutar íntegramente
solo con este repositorio. Para reproducir el experimento hay que preparar un
corpus propio autorizado, sus referencias y el modelo correspondiente.

La migración entre motores de búsqueda exige repetir la evaluación: PostgreSQL
y los índices locales pueden ordenar resultados de manera diferente. Las
medidas de recuperación no demuestran que una respuesta sea correcta ni que
exista cobertura completa. El OCR y las referencias requieren revisión humana.

## Seguridad y privacidad

- Mantener claves, cookies, tokens, originales, documentos derivados y datos de
  evaluación fuera de Git. No introducir credenciales en argumentos o registros.
- Usar TLS verificado y un rol PostgreSQL de mínimo privilegio.
- Conservar `ASIR_OAUTH_OPEN_ACCESS=0` para exigir autorización explícita y
  revisar también los tokens personales, que no usan la lista OAuth.
- Revisar los permisos de distribución y datos personales antes de indexar o
  compartir material. El acceso a un repositorio público no concede derechos
  sobre un corpus privado.
- Las métricas agregan peticiones por fecha y clase de respuesta; no equivalen
  a consultas ni usuarios y no pretenden registrar preguntas ni credenciales.
