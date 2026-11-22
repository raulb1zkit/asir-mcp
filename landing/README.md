# Landing del Proyecto MCP-ASIR

Sitio web estático de presentación, documentación pedagógica e instrucciones de conexión para el alumnado del proyecto **MCP-ASIR**.

## Características técnicas

- **Sitio estático autónomo:** Un único archivo `index.html` con CSS y JavaScript nativo integrado, sin dependencias de frameworks ni pasos de compilación.
- **Tipografía y diseño:** Tipografías Bricolage Grotesque y JetBrains Mono, modo claro/oscuro automático según el sistema (`prefers-color-scheme`) y soporte para reducción de movimiento (`prefers-reduced-motion`).
- **Diagrama de flujo interactivo:** Visualización animada en CSS del flujo entre el cliente de IA, el protocolo MCP y el servidor.
- **Instrucciones directas:** Guía paso a paso para configurar Cursor, Claude Code y clientes estándar con archivo `mcp.json`.

---

## Cómo servir localmente

Al tratarse de un sitio puramente estático, no requiere compilación previa. Se puede probar con cualquier servidor web HTTP local:

### Opción 1: Python 3 (Recomendado)

Desde la raíz del repositorio:

```bash
python3 -m http.server 8080 --directory landing
```

O entrando directamente en el directorio `landing/`:

```bash
cd landing
python3 -m http.server 8080
```

Abre en tu navegador: [http://localhost:8080](http://localhost:8080).

### Opción 2: Node.js (`npx serve`)

```bash
npx serve landing
```

### Opción 3: Caddy / Nginx local

```bash
caddy file-server --listen :8080 --root landing
```

---

## Cómo publicar como sitio estático

El contenido completo reside en el directorio `landing/`. Para su publicación en producción no se requiere ningún paso de `build`:

### 1. Cloudflare Pages

1. Conecta el repositorio o sube la carpeta mediante `wrangler`:
   ```bash
   npx wrangler pages deploy landing --project-name mcp-asir-landing
   ```
2. Si se conecta mediante Git en el panel de Cloudflare Pages:
   - **Framework preset:** `None`
   - **Build command:** *(vacío)*
   - **Build output directory:** `landing`

### 2. GitHub Pages

- Configura GitHub Pages desde la rama `main` indicando la carpeta `/landing` o mediante una GitHub Action que copie el directorio `landing/` a la rama `gh-pages`.

### 3. Servidor Web Tradicional (Nginx / Apache en VPS)

Copia los archivos a la raíz de tu servidor web:

```nginx
# Ejemplo de bloque de servidor Nginx:
server {
    listen 80;
    server_name asir.example.org;

    root /var/www/mcp-asir/landing;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # Cabeceras de seguridad recomendadas
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
}
```

---

## Estructura de archivos

```text
landing/
├── index.html   # Sitio estático auto-contenido (HTML, CSS y JS integrados).
└── README.md    # Guía de ejecución local y publicación estática.
```

## Acceso OAuth (pantalla de consentimiento)

`landing/oauth/consent/` sirve la pantalla de consentimiento de Google. La
configuración vive en `landing/oauth/consent/config.js` (fuera de Git; plantilla
en `config.example.js`) con dos valores publicables: la clave anon del proyecto
(`publishableKey`) y la URL del proyecto (`supabaseUrl`, marcador
`TU-PROJECT-REF`). Al publicar, actualiza también el CSP de
`landing/oauth/consent/index.html` (`connect-src`) con la URL real del
proyecto. La aprobación de acceso se gestiona por una lista privada; esta
página no concede permisos por sí sola.
