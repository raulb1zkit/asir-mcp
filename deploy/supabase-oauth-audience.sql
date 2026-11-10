-- Preparación para el hook Custom Access Token de Supabase Auth.
-- Antes de activar, comprobar que este proyecto no se use como proveedor
-- OAuth para aplicaciones ajenas a MCP-ASIR. Probar antes de abrir al grupo.
-- Sin client_id la sesión normal de la landing conserva su audiencia.
CREATE OR REPLACE FUNCTION public.asir_mcp_access_token_hook(event jsonb)
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $$
  SELECT CASE
    WHEN NULLIF(event #>> '{claims,client_id}', '') IS NOT NULL
     AND event #>> '{claims,role}' = 'authenticated'
    THEN jsonb_set(event, '{claims,aud}', to_jsonb('https://mcp.bztracker.xyz/mcp'::text), false)
    ELSE event
  END
$$;

REVOKE ALL ON FUNCTION public.asir_mcp_access_token_hook(jsonb) FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA public TO supabase_auth_admin;
GRANT EXECUTE ON FUNCTION public.asir_mcp_access_token_hook(jsonb) TO supabase_auth_admin;
