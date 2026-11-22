/**
 * mcp-asir-external-health — Worker externo (Cloudflare Workers, plan Free)
 *
 * Detecta desde fuera del VPS que el MCP público (mcp.bztracker.xyz) es
 * inaccesible y avisa por Telegram. Solo usa el handler `scheduled`
 * (Cron Trigger cada 5 min); cualquier petición HTTP pública recibe 404.
 *
 * Contrato real de /healthz (scripts/mcp_http.py del repo principal):
 *   - 200 + JSON {"status":"ok","service":"asir-mcp",...}  → sano
 *   - 503 + JSON {"status":"degraded","service":"asir-mcp"} → degradado
 * Se exige status HTTP 200 Y JSON con status==="ok" y service==="asir-mcp".
 * Un 200 con HTML u otro JSON se cuenta como fallo (fail-closed).
 *
 * Diseño de alertas (revisión r2, documentado en docs/51):
 *   - Estado físico (`health`) separado de la notificación (`incident`):
 *     la incidencia se abre con el 2.º fallo consecutivo, con id estable,
 *     y solo se cierra cuando su aviso de recuperación queda entregado
 *     y acreditado en KV (nunca por una comprobación sana a secas).
 *   - Un aviso por tipo e incidencia con ids estables (`<inc>#down`,
 *     `<inc>#recovery`): no se re-encola un aviso ya encolado, y si la
 *     salud vuelve a caer con la recuperación pendiente, esta se retira
 *     (parpadeo coalescido: la incidencia sigue abierta, sin ráfagas).
 *   - Cola FIFO acotada por construcción (test + 1 down + 1 recovery);
 *     nunca se recorta ni descarta en silencio: la entrega se detiene en
 *     el primer envío fallido y el resto conserva su orden.
 *   - Persistencia: el intent (estado+cola) se persiste en KV ANTES de
 *     llamar a Telegram (fail-closed si falla la escritura) y cada
 *     entrega se acredita con un put durable antes de la siguiente.
 *     Semántica at-least-once: si Telegram acepta y el ack falla, el
 *     reintento reenvía (ventana mínima de duplicado; Telegram no ofrece
 *     idempotencia, así que "exactamente una vez" no se promete).
 *
 * Secretos: bindings TELEGRAM_TOKEN y TELEGRAM_CHAT_ID (secret_text).
 * No hay rutas de depuración y los mensajes no incluyen tokens, cuerpos
 * HTTP ni métricas en bruto.
 */

const DEFAULT_HEALTH_URL = 'https://mcp.bztracker.xyz/healthz';
const KV_KEY = 'state';
const SCHEMA = 2;
const HEALTH_TIMEOUT_MS = 10000;
const TELEGRAM_TIMEOUT_MS = 10000;
const FAILS_BEFORE_ALERT = 2;

const MESSAGES = {
  down:
    (reason, incidentId) =>
      `[ASIR] MCP inaccesible desde fuera (${FAILS_BEFORE_ALERT} comprobaciones consecutivas). ` +
      `Motivo: ${reason}. Incidencia: ${incidentId}.`,
  recovery:
    (incidentId, startedMs, nowMs) =>
      `[ASIR] MCP accesible de nuevo. Incidencia ${incidentId} cerrada ` +
      `tras ~${Math.max(1, Math.round((nowMs - startedMs) / 60000))} min.`,
  test:
    () =>
      '[TEST] Verificación del worker externo de salud (mcp-asir-external-health). ' +
      'Mensaje de prueba planificado; no es una alerta real.',
};

function compactNow(dateIso) {
  return new Date(dateIso).toISOString().replace(/[-:]/g, '').replace(/\..+/, 'Z');
}

function messageId(incidentId, kind) {
  return `${incidentId}#${kind}`;
}

function initialState() {
  return {
    schema: SCHEMA,
    health: { status: 'unknown', failCount: 0, lastCheckAt: null, lastHealthyAt: null, lastError: null },
    incident: null, // {id, startedMs, downMsgId, recoveryMsgId}
    queue: [], // [{id, kind, incidentId, text, createdAt, attempts, lastError}] FIFO
    testPending: false,
  };
}

/**
 * Migración suave v1 → v2: convierte el estado antiguo sin perder datos.
 * Los avisos pendientes conservan su texto y orden; los de una incidencia
 * legacy reciben ids estables y se deduplican por (incidencia, tipo) — el
 * código v1 acumulaba un "recovery" por ciclo sano, se colapsan a uno.
 */
function migrate(raw) {
  if (!raw) return initialState();
  let old;
  try {
    old = JSON.parse(raw);
  } catch {
    return initialState();
  }
  if (!old || typeof old !== 'object') return initialState();
  if (old.schema === SCHEMA) {
    const base = initialState();
    const state = { ...base, ...old };
    state.health = { ...base.health, ...(old.health || {}) };
    state.incident = old.incident || null;
    state.queue = Array.isArray(old.queue) ? old.queue : [];
    return state;
  }
  const legacyIncident =
    old.incident && old.incident.id
      ? { id: String(old.incident.id), startedMs: Number(old.incident.startedMs) || 0 }
      : null;
  const incident = legacyIncident
    ? {
        id: legacyIncident.id,
        startedMs: legacyIncident.startedMs,
        // El down de una incidencia legacy ya se notificó (o está en cola):
        // el id sintético evita re-encolar otro aviso "down".
        downMsgId: messageId(legacyIncident.id, 'down'),
        recoveryMsgId: null,
      }
    : null;
  const queue = [];
  const seen = new Set();
  const legacyPending = Array.isArray(old.pending) ? old.pending : [];
  let legacyCounter = 0;
  for (const m of legacyPending) {
    if (!m || typeof m !== 'object') continue;
    if (m.kind !== 'down' && m.kind !== 'recovery' && m.kind !== 'test') continue;
    let id;
    if (legacyIncident && (m.kind === 'down' || m.kind === 'recovery')) {
      id = messageId(legacyIncident.id, m.kind);
      if (seen.has(id)) continue; // deduplica (p. ej. recoveries acumulados en v1)
      if (m.kind === 'recovery') incident.recoveryMsgId = id;
    } else {
      id = `legacy-${m.kind}-${legacyCounter++}`;
    }
    seen.add(id);
    queue.push({
      id,
      kind: m.kind,
      incidentId: legacyIncident ? legacyIncident.id : null,
      text: String(m.text || ''),
      createdAt: String(m.createdAt || ''),
      attempts: 0,
      lastError: null,
    });
  }
  return {
    schema: SCHEMA,
    health: {
      status: old.status || 'unknown',
      failCount: Number(old.failCount) || 0,
      lastCheckAt: old.lastCheckAt || null,
      lastHealthyAt: old.lastHealthyAt || null,
      lastError: old.lastError || null,
    },
    incident,
    queue,
    testPending: Boolean(old.testPending),
  };
}

/** Comprueba /healthz evitando el caché del edge (nonce + no-cache) y con timeout. */
export async function checkHealth(healthUrl, fetchImpl) {
  const url = new URL(healthUrl);
  // Nonce aleatorio: URL única que, según el comportamiento documentado de
  // la caché, evita servir un 200 cacheado del edge que simule salud con el
  // origen caído. Es mitigación (normas de caché), no garantía absoluta.
  url.searchParams.set('nonce', crypto.randomUUID());
  let res;
  try {
    res = await fetchImpl(url, {
      method: 'GET',
      redirect: 'manual', // una redirección no es un 200 válido: fail-closed
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
      headers: { 'Cache-Control': 'no-cache', Pragma: 'no-cache' },
      // Directiva documentada de Workers: no usar caché del edge.
      cf: { cacheTtl: 0, cacheEverything: false },
    });
  } catch (err) {
    const name = err && err.name;
    if (name === 'TimeoutError' || name === 'AbortError') {
      return { ok: false, reason: 'timeout' };
    }
    return { ok: false, reason: 'network_error' };
  }
  if (res.status >= 300 && res.status < 400) {
    return { ok: false, reason: 'redirect' };
  }
  let body = null;
  const contentType = res.headers.get('content-type') || '';
  try {
    body = await res.json();
  } catch {
    return { ok: false, reason: res.status === 200 ? (contentType.includes('json') ? 'bad_json' : 'not_json') : `http_${res.status}` };
  }
  if (res.status !== 200) {
    // El propio MCP anuncia degradado (503 + JSON del contrato real).
    if (res.status === 503 && body && body.service === 'asir-mcp' && body.status === 'degraded') {
      return { ok: false, reason: 'degraded' };
    }
    return { ok: false, reason: `http_${res.status}` };
  }
  if (!body || typeof body !== 'object') {
    return { ok: false, reason: 'bad_json' };
  }
  if (body.service !== 'asir-mcp') {
    return { ok: false, reason: 'contract_mismatch' };
  }
  if (body.status !== 'ok') {
    return { ok: false, reason: 'degraded' };
  }
  return { ok: true };
}

/** Envía un mensaje a Telegram. Solo {ok:true} cuenta como entregado. */
export async function sendTelegram(token, chatId, text, fetchImpl) {
  let res;
  try {
    res = await fetchImpl(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'POST',
      signal: AbortSignal.timeout(TELEGRAM_TIMEOUT_MS),
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        disable_web_page_preview: true,
      }),
    });
  } catch {
    return { delivered: false, reason: 'network_error' };
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    return { delivered: false, reason: `http_${res.status}` };
  }
  if (res.status === 200 && data && data.ok === true) {
    return { delivered: true };
  }
  // Solo códigos/cortos: nunca el cuerpo completo (podría ecoar entrada).
  const code = data && data.error_code ? `tg_${data.error_code}` : `http_${res.status}`;
  return { delivered: false, reason: code };
}

/**
 * Máquina de estados e intentos de entrega inyectables para tests
 * (env.STATE = binding KV, overrides.now = reloj determinista).
 */
export async function runMonitor(env, overrides = {}) {
  const fetchImpl = overrides.fetch || ((u, i) => fetch(u, i));
  const healthUrl = env.HEALTH_URL || DEFAULT_HEALTH_URL;
  const nowIso = overrides.now ? overrides.now() : new Date().toISOString();
  const kv = env.STATE;

  let state;
  try {
    state = migrate(await kv.get(KV_KEY));
  } catch (err) {
    console.log(JSON.stringify({ event: 'kv_read_failed', type: err && err.name }));
    // Fail-safe: sin estado fiable no se envían alertas (evita tormentas).
    return { skipped: 'kv_read_failed' };
  }

  // 1) Mensaje de ensayo one-off (sembrado por el despliegue): el intent
  //    (bandera consumida + mensaje en cola) se persiste antes de enviar.
  let sent = 0;
  if (state.testPending) {
    state.testPending = false;
    state.queue.push({
      id: `test-${compactNow(nowIso)}`,
      kind: 'test',
      incidentId: null,
      text: MESSAGES.test(),
      createdAt: nowIso,
      attempts: 0,
      lastError: null,
    });
  }

  // 2) Comprobación de salud y transiciones (solo mutación de estado).
  const result = await checkHealth(healthUrl, fetchImpl);
  state.health.lastCheckAt = nowIso;

  if (result.ok) {
    state.health.status = 'healthy';
    state.health.failCount = 0;
    state.health.lastError = null;
    state.health.lastHealthyAt = nowIso;
    // Un único recovery por incidencia: si ya está encolado, no se repite
    // (v1 encolaba uno por cada ciclo sano hasta agotar MAX_PENDING).
    if (state.incident && !state.incident.recoveryMsgId) {
      const inc = state.incident;
      const id = messageId(inc.id, 'recovery');
      inc.recoveryMsgId = id;
      state.queue.push({
        id,
        kind: 'recovery',
        incidentId: inc.id,
        text: MESSAGES.recovery(inc.id, inc.startedMs, Date.parse(nowIso)),
        createdAt: nowIso,
        attempts: 0,
        lastError: null,
      });
    }
  } else {
    state.health.status = 'down';
    state.health.failCount += 1;
    state.health.lastError = result.reason;
    if (state.incident && state.incident.recoveryMsgId) {
      // Recovery no entregado: la salud volvió a caer, el aviso ya no es
      // verdad → se retira; la incidencia sigue abierta (mismo id y
      // duración original). El siguiente ciclo sano lo re-encolará.
      const rid = state.incident.recoveryMsgId;
      state.queue = state.queue.filter((m) => m.id !== rid);
      state.incident.recoveryMsgId = null;
    }
    if (state.health.failCount >= FAILS_BEFORE_ALERT && !state.incident) {
      const inc = {
        id: compactNow(nowIso),
        startedMs: Date.parse(nowIso),
        downMsgId: messageId(compactNow(nowIso), 'down'),
        recoveryMsgId: null,
      };
      state.incident = inc;
      state.queue.push({
        id: inc.downMsgId,
        kind: 'down',
        incidentId: inc.id,
        text: MESSAGES.down(result.reason, inc.id),
        createdAt: nowIso,
        attempts: 0,
        lastError: null,
      });
    }
  }

  // 3) Intent ANTES de cualquier envío: si esta escritura falla, no se
  //    llama a Telegram (fail-closed) y Cloudflare reintenta el ciclo.
  const persist = async () => {
    try {
      await kv.put(KV_KEY, JSON.stringify(state));
    } catch (err) {
      const wrapped = new Error(`kv_put_failed:${err && err.name}`);
      wrapped.kvWrite = true;
      throw wrapped;
    }
  };
  await persist();

  // 4) Entrega FIFO: se detiene en el primer fallo (nada adelanta a un
  //    envío fallido) y cada entrega se acredita con un put durable antes
  //    de intentar la siguiente. Los no enviados conservan orden y texto.
  let stopped = false;
  for (const msg of [...state.queue]) {
    const outcome = await sendTelegram(env.TELEGRAM_TOKEN, env.TELEGRAM_CHAT_ID, msg.text, fetchImpl);
    if (!outcome.delivered) {
      msg.attempts += 1;
      msg.lastError = outcome.reason;
      stopped = true;
      break;
    }
    state.queue = state.queue.filter((m) => m.id !== msg.id);
    sent += 1;
    // El cierre va en el MISMO put que el ack: incidencia cerrada solo
    // cuando la recuperación quedó entregada y acreditada.
    if (
      state.incident &&
      state.incident.recoveryMsgId &&
      !state.queue.some((m) => m.id === state.incident.recoveryMsgId)
    ) {
      state.incident = null;
    }
    await persist();
  }
  if (stopped) {
    await persist(); // conserva intentos/errores y los mensajes no enviados
  }

  // Red de seguridad: cierre pendiente de acreditar si el item de recovery
  // ya no está en cola (p. ej. ventana de reintento tras un ack fallido).
  if (
    state.incident &&
    state.incident.recoveryMsgId &&
    !state.queue.some((m) => m.id === state.incident.recoveryMsgId)
  ) {
    state.incident = null;
    await persist();
  }

  return {
    status: state.health.status,
    failCount: state.health.failCount,
    incident: state.incident ? state.incident.id : null,
    queueCount: state.queue.length,
    sent,
    lastError: state.health.lastError,
  };
}

export default {
  /** Solo Cron Triggers; sin endpoint público ni rutas de depuración. */
  async fetch() {
    return new Response('Not Found', { status: 404 });
  },
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      runMonitor(env).then(
        (r) => console.log(JSON.stringify({ event: 'monitor_run', ...r })),
        (err) => {
          // Re-lanzar solo fallos de escritura en KV para que Cloudflare
          // reintente el ciclo; el resto termina limpio (sin duplicar envíos).
          if (err && err.kvWrite) throw err;
          console.log(JSON.stringify({ event: 'monitor_error', type: err && err.name }));
        },
      ),
    );
  },
};
