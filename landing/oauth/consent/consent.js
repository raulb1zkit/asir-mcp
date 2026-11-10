"use strict";

const SUPABASE_URL = "https://kwvezxkhrjouaidzoglj.supabase.co";
const PENDING_KEY = "asir_oauth_authorization_id";
const statusNode = document.getElementById("status");
const signIn = document.getElementById("sign-in");
const consent = document.getElementById("consent");
const google = document.getElementById("google");
const approve = document.getElementById("approve");
const deny = document.getElementById("deny");
const switchAccount = document.getElementById("switch-account");

function showError(message) {
  statusNode.textContent = message;
  signIn.hidden = true;
  consent.hidden = true;
}

function finish(redirectUrl) {
  if (typeof redirectUrl !== "string" || !redirectUrl) throw new Error("missing_redirect");
  sessionStorage.removeItem(PENDING_KEY);
  window.location.assign(redirectUrl);
}

async function main() {
  if (!window.supabase) return showError("No se ha podido cargar el acceso. Vuelve a intentarlo.");
  if (!/^sb_publishable_[A-Za-z0-9_-]+$/.test(window.ASIR_AUTH_CONFIG?.publishableKey || "")) return showError("El acceso todavía no está configurado.");
  const query = new URLSearchParams(window.location.search);
  let authorizationId = query.get("authorization_id") || sessionStorage.getItem(PENDING_KEY);
  if (!authorizationId || authorizationId.length > 512) return showError("Falta la solicitud de conexión. Iníciala desde tu cliente de IA.");
  sessionStorage.setItem(PENDING_KEY, authorizationId);
  // Retira la solicitud de la barra y del historial antes de cargar otras páginas.
  if (query.has("authorization_id")) history.replaceState(null, "", window.location.pathname);
  const client = window.supabase.createClient(SUPABASE_URL, window.ASIR_AUTH_CONFIG.publishableKey, {
    auth: {flowType: "pkce", detectSessionInUrl: true, persistSession: true, autoRefreshToken: true, storage: sessionStorage}
  });
  async function startGoogle() {
    google.disabled = true;
    const redirectTo = window.location.origin + "/oauth/consent/";
    const {error} = await client.auth.signInWithOAuth({provider: "google", options: {redirectTo, queryParams: {prompt: "select_account"}}});
    if (error) { google.disabled = false; showError("No se ha podido iniciar sesión con Google. Vuelve a intentarlo."); }
  }
  const {data: userData, error: userError} = await client.auth.getUser();
  if (userError || !userData.user) {
    statusNode.textContent = "Para continuar, identifica tu cuenta.";
    signIn.hidden = false;
    google.addEventListener("click", startGoogle);
    return;
  }
  const {data, error} = await client.auth.oauth.getAuthorizationDetails(authorizationId);
  if (error || !data) return showError("La solicitud ha caducado o no es válida. Vuelve a conectarte desde tu cliente.");
  if (!("authorization_id" in data)) return finish(data.redirect_url);
  document.getElementById("client").textContent = data.client?.name || "Cliente de IA";
  document.getElementById("identity").textContent = userData.user.email || "Tu cuenta";
  const labels = {openid: "Identificar tu cuenta", email: "Ver tu correo", profile: "Ver tu perfil", offline_access: "Mantener la conexión", phone: "Ver tu teléfono"};
  const scopes = (data.scope || "").split(/\s+/).filter(Boolean);
  document.getElementById("scopes").textContent = scopes.map(scope => labels[scope] || scope).join(", ") || "Acceso autorizado al MCP";
  document.getElementById("redirect").textContent = data.redirect_uri || "Tu cliente de IA";
  statusNode.textContent = "Revisa la solicitud antes de continuar.";
  consent.hidden = false;
  switchAccount.addEventListener("click", async () => {
    switchAccount.disabled = true;
    const {error: signOutError} = await client.auth.signOut({scope: "local"});
    if (signOutError) return showError("No se ha podido cambiar de cuenta. Vuelve a intentarlo.");
    await startGoogle();
  });
  async function decide(allow) {
    approve.disabled = deny.disabled = true;
    try {
      const options = {skipBrowserRedirect: true};
      const result = allow ? await client.auth.oauth.approveAuthorization(authorizationId, options) : await client.auth.oauth.denyAuthorization(authorizationId, options);
      if (result.error || !result.data) throw new Error("decision_failed");
      finish(result.data.redirect_url);
    } catch (_) {
      showError("No se ha podido completar la solicitud. Vuelve a intentarlo desde tu cliente.");
    }
  }
  approve.addEventListener("click", () => decide(true));
  deny.addEventListener("click", () => decide(false));
}

main().catch(() => showError("No se ha podido completar la conexión. Vuelve a intentarlo."));
