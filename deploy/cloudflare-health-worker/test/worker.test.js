/**
 * Tests del worker externo de salud. Solo node:test + node:assert
 * (builtins), sin dependencias. Ejecutar: node --test test/
 *
 * Cubre, además del contrato de /healthz y Telegram, los hallazgos de la
 * revisión r2: recovery único por incidencia, intent en KV antes de
 * enviar (fail-closed), ack durable por entrega con semántica
 * at-least-once honesta, FIFO con stop en el primer fallo, parpadeo
 * coalescido, presupuesto de escrituras en KV y migración v1 → v2.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker, { runMonitor, checkHealth, sendTelegram } from '../worker.js';

const HEALTH_URL = 'https://mcp.bztracker.xyz/healthz';
const OK_BODY = { status: 'ok', service: 'asir-mcp', chunks: 13451, windows: 27259 };

function jsonResponse(status, body, contentType = 'application/json') {
  return new Response(typeof body === 'string' ? body : JSON.stringify(body), {
    status,
    headers: { 'content-type': contentType },
  });
}

function fakeKv(store = new Map()) {
  const kv = {
    store,
    puts: 0,
    async get(k) {
      return store.has(k) ? store.get(k) : null;
    },
    async put(k, v) {
      kv.puts += 1;
      store.set(k, v);
    },
    async delete(k) {
      store.delete(k);
    },
  };
  return kv;
}

/** KV cuyo put nº failOn (1-indexado) lanza; los demás escriben. */
function failPutKv(store, failOn) {
  const kv = fakeKv(store);
  let n = 0;
  kv.put = async (k, v) => {
    if (++n === failOn) throw new Error('kv put inyectado falla');
    kv.puts += 1;
    store.set(k, v);
  };
  return kv;
}

/** Reloj determinista: avanza 5 min por llamada (un ciclo por llamada). */
function clock(startMs = Date.parse('2026-11-22T18:00:00.000Z')) {
  let t = startMs;
  return () => new Date((t += 5 * 60 * 1000)).toISOString();
}

/** fetch simulado: enruta por URL (healthz vs api.telegram.org). */
function fetchRouter({ health, telegram }) {
  const calls = [];
  const impl = async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    calls.push({ url: String(url), init: init || null });
    if (url.includes('api.telegram.org')) {
      return telegram(url, init);
    }
    return health(url, init);
  };
  impl.calls = calls;
  impl.telegramCalls = () => calls.filter((c) => c.url.includes('api.telegram.org'));
  impl.telegramTexts = () =>
    impl.telegramCalls().map((c) => JSON.parse(c.init.body).text);
  return impl;
}

const healthOk = () => jsonResponse(200, OK_BODY);
const healthFail = () => jsonResponse(404, '<html>404</html>', 'text/html');
const telegramOk = () => jsonResponse(200, { ok: true, result: {} });
const telegramFail = () => jsonResponse(200, { ok: false, error_code: 502 });

function env(overrides = {}) {
  return {
    STATE: overrides.kv || fakeKv(),
    TELEGRAM_TOKEN: 'test-token',
    TELEGRAM_CHAT_ID: '6196526911',
    HEALTH_URL,
    ...overrides,
  };
}

/** Lee el estado persistido (ya migrado) del KV simulado. */
async function storedState(kv) {
  return JSON.parse(await kv.store.get('state'));
}

// ── Contrato de /healthz ────────────────────────────────────────────────

test('healthz sano (200 JSON contrato real) → ok', async () => {
  const r = await checkHealth(HEALTH_URL, async () => healthOk());
  assert.equal(r.ok, true);
});

test('503 degradado → fallo reason=degraded', async () => {
  const r = await checkHealth(HEALTH_URL, async () =>
    jsonResponse(503, { status: 'degraded', service: 'asir-mcp' }),
  );
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'degraded');
});

test('200 con HTML → fallo reason=not_json (falso 200)', async () => {
  const r = await checkHealth(HEALTH_URL, async () =>
    jsonResponse(200, '<html>ok</html>', 'text/html'),
  );
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'not_json');
});

test('200 JSON sin service asir-mcp → fallo contract_mismatch', async () => {
  const r = await checkHealth(HEALTH_URL, async () => jsonResponse(200, { status: 'ok' }));
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'contract_mismatch');
});

test('timeout → fallo reason=timeout', async () => {
  const err = new Error('timed out');
  err.name = 'TimeoutError';
  const r = await checkHealth(HEALTH_URL, async () => {
    throw err;
  });
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'timeout');
});

test('red rota → fallo reason=network_error', async () => {
  const r = await checkHealth(HEALTH_URL, async () => {
    throw new Error('connect ECONNRESET');
  });
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'network_error');
});

test('redirect manual → fallo (sin falsos 200 por redirección)', async () => {
  const r = await checkHealth(HEALTH_URL, async () =>
    new Response(null, { status: 302, headers: { location: 'https://otro.example/' } }),
  );
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'redirect');
});

test('el healthcheck usa nonce aleatorio y no-cache (bypass de caché del edge)', async () => {
  const seen = [];
  const impl = fetchRouter({
    health: (url) => {
      seen.push(url);
      return healthOk();
    },
    telegram: () => telegramOk(),
  });
  await runMonitor(env(), { fetch: impl });
  await runMonitor(env(), { fetch: impl });
  assert.equal(seen.length, 2);
  const nonces = seen.map((u) => new URL(u).searchParams.get('nonce'));
  assert.ok(nonces[0]);
  assert.notEqual(nonces[0], nonces[1]);
  const init = impl.calls[0].init;
  assert.equal(init.headers['Cache-Control'], 'no-cache');
  assert.equal(init.redirect, 'manual');
});

// ── Máquina de estados básica ───────────────────────────────────────────

test('sano: sin alertas ni llamadas a Telegram', async () => {
  const impl = fetchRouter({ health: healthOk, telegram: () => { throw new Error('no debería llamar'); } });
  const r = await runMonitor(env(), { fetch: impl });
  assert.equal(r.status, 'healthy');
  assert.equal(r.incident, null);
  assert.equal(impl.calls.length, 1);
});

test('un fallo aislado no alerta', async () => {
  const impl = fetchRouter({ health: healthFail, telegram: telegramOk });
  const r = await runMonitor(env(), { fetch: impl });
  assert.equal(r.status, 'down');
  assert.equal(r.failCount, 1);
  assert.equal(r.incident, null);
  assert.equal(impl.telegramCalls().length, 0);
});

test('dos fallos consecutivos → 1 aviso down, sin repetir en la misma incidencia', async () => {
  const kv = fakeKv();
  const impl = fetchRouter({ health: healthFail, telegram: telegramOk });
  const r1 = await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(r1.incident, null); // 1er fallo: aún sin incidencia
  const r2 = await runMonitor(env({ kv }), { fetch: impl });
  assert.ok(r2.incident);
  assert.equal(r2.queueCount, 0); // entregado con ok:true
  assert.equal(impl.telegramCalls().length, 1);
  const r3 = await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(r3.status, 'down');
  assert.equal(r3.queueCount, 0);
  assert.equal(impl.telegramCalls().length, 1); // sin alerta repetida
});

test('recuperación tras incidencia → 1 aviso y cierre de incidencia', async () => {
  const kv = fakeKv();
  let checkNum = 0;
  const impl = fetchRouter({
    health: () => (++checkNum <= 2 ? healthFail() : healthOk()),
    telegram: telegramOk,
  });
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 1
  const r2 = await runMonitor(env({ kv }), { fetch: impl }); // fallo 2 → down
  assert.ok(r2.incident);
  const r3 = await runMonitor(env({ kv }), { fetch: impl }); // recuperación
  assert.equal(r3.status, 'healthy');
  assert.equal(r3.incident, null);
  assert.equal(impl.telegramCalls().length, 2); // down + recovery
  assert.match(JSON.parse(impl.telegramCalls()[1].init.body).text, /accesible de nuevo/);
});

// ── Hallazgo 1: recovery único por incidencia ───────────────────────────

test('Telegram caído 3 ciclos sanos: recovery NO se acumula; al volver, exactamente 1', async () => {
  const kv = fakeKv();
  let checkNum = 0;
  let tgOk = true;
  const impl = fetchRouter({
    health: () => (++checkNum <= 2 ? healthFail() : healthOk()),
    telegram: () => (tgOk ? telegramOk() : telegramFail()),
  });
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 1
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 2 → down entregado
  tgOk = false;
  for (let i = 0; i < 3; i++) {
    await runMonitor(env({ kv }), { fetch: impl }); // 3 ciclos sanos, TG caído
  }
  let s = await storedState(kv);
  assert.equal(s.queue.length, 1); // un único recovery pendiente, sin acumular
  assert.equal(s.incident.recoveryMsgId, s.queue[0].id);
  tgOk = true;
  const before = impl.telegramCalls().length;
  const r = await runMonitor(env({ kv }), { fetch: impl }); // TG vuelve
  assert.equal(r.incident, null); // incidencia cerrada
  assert.equal(r.queueCount, 0);
  const recoveries = impl.telegramTexts().slice(before).filter((t) => t.includes('accesible'));
  assert.equal(recoveries.length, 1); // exactamente 1 recovery aceptado
  s = await storedState(kv);
  assert.equal(s.queue.length, 0);
  assert.equal(s.incident, null);
});

test('recovery pendiente no se re-encola en ciclos sanos repetidos (ids estables)', async () => {
  const kv = fakeKv();
  let checkNum = 0;
  let tgFail = false;
  const impl = fetchRouter({
    health: () => (++checkNum <= 2 ? healthFail() : healthOk()),
    telegram: () => (tgFail ? telegramFail() : telegramOk()),
  });
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 1
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 2 → down entregado
  tgFail = true;
  const r3 = await runMonitor(env({ kv }), { fetch: impl }); // encola recovery, TG caído
  assert.equal(r3.queueCount, 1);
  const idRecovery = (await storedState(kv)).queue[0].id;
  for (let i = 0; i < 4; i++) {
    const r = await runMonitor(env({ kv }), { fetch: impl });
    assert.equal(r.queueCount, 1); // nunca crece
  }
  const s = await storedState(kv);
  assert.equal(s.queue[0].id, idRecovery); // mismo id estable
  assert.equal(new Set(s.queue.map((m) => m.id)).size, s.queue.length);
});

// ── Hallazgo 2: intent antes de enviar + ack durable (at-least-once) ────

test('KV put falla ANTES de enviar → 0 llamadas a Telegram y error kvWrite', async () => {
  const kv = failPutKv(new Map(), 1); // falla el intent
  const impl = fetchRouter({ health: healthFail, telegram: telegramOk });
  await assert.rejects(
    () => runMonitor(env({ kv }), { fetch: impl }),
    (e) => e.kvWrite === true,
  );
  assert.equal(impl.telegramCalls().length, 0); // fail-closed
  assert.equal(kv.store.has('state'), false); // nada escrito
});

test('KV put falla en el ack tras ok:true → pendiente durable y reintento honesto', async () => {
  const kv = failPutKv(new Map(), 3); // puts 1-2 ok (intents), put 3 = ack del down falla
  let checkNum = 0;
  const impl = fetchRouter({
    health: () => (++checkNum <= 3 ? healthFail() : healthOk()),
    telegram: telegramOk,
  });
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 1 (1 put)
  await assert.rejects(() => runMonitor(env({ kv }), { fetch: impl }), (e) => e.kvWrite === true);
  assert.equal(impl.telegramCalls().length, 1); // Telegram aceptó el down
  // El estado en KV sigue siendo el del intent: down pendiente, incidencia abierta.
  let s = await storedState(kv);
  assert.equal(s.incident.recoveryMsgId, null);
  assert.equal(s.queue.length, 1);
  const incidentId = s.incident.id;
  // Ciclo siguiente: reintento re-entrega el mismo down (at-least-once) y no
  // abre una incidencia nueva ni repite un aviso nuevo.
  const before = impl.telegramTexts().length;
  const r = await runMonitor(env({ kv }), { fetch: impl }); // fallo 3, TG ok
  assert.equal(r.incident, incidentId); // misma incidencia, sin duplicar
  const downs = impl.telegramTexts().slice(before).filter((t) => t.includes('inaccesible'));
  assert.equal(downs.length, 1); // un único texto de down en el reintento
  s = await storedState(kv);
  assert.equal(s.incident.id, incidentId);
  assert.equal(s.queue.length, 0); // ack durable tras el reintento
});

// ── Hallazgo 3: FIFO, stop en el primer fallo, parpadeo coalescido ──────

test('FIFO: un recovery no adelanta a un down fallido; orden preservado al drenar', async () => {
  const kv = fakeKv();
  let checkNum = 0;
  let tgFail = true;
  const impl = fetchRouter({
    health: () => (++checkNum <= 2 ? healthFail() : healthOk()),
    telegram: () => (tgFail ? telegramFail() : telegramOk()),
  });
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 1
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 2 → down en cola, TG caído
  // TG sigue caído en el ciclo sano: el recovery se encola DETRÁS del down.
  const before = impl.telegramCalls().length;
  await runMonitor(env({ kv }), { fetch: impl }); // solo intenta el down y falla
  assert.equal(impl.telegramCalls().length, before + 1);
  let s = await storedState(kv);
  assert.deepEqual(s.queue.map((m) => m.kind), ['down', 'recovery']);
  assert.ok(s.incident); // la incidencia NO se cierra con el down sin entregar
  // TG responde ok a todo: drena en FIFO down → recovery y cierra.
  tgFail = false;
  const r = await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(r.incident, null);
  assert.equal(r.queueCount, 0);
  const kinds = impl.telegramTexts().slice(before + 1).map((t) => (t.includes('inaccesible') ? 'down' : 'recovery'));
  assert.deepEqual(kinds, ['down', 'recovery']); // orden preservado, 1 cada uno
});

test('parpadeo: recovery pendiente se retira si la salud vuelve a caer (incidencia abierta)', async () => {
  const kv = fakeKv();
  let checkNum = 0;
  let tgFail = false;
  const impl = fetchRouter({
    health: () => (++checkNum <= 2 ? healthFail() : healthOk()),
    telegram: () => (tgFail ? telegramFail() : telegramOk()),
  });
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 1
  await runMonitor(env({ kv }), { fetch: impl }); // fallo 2 → down entregado
  tgFail = true;
  await runMonitor(env({ kv }), { fetch: impl }); // sano + TG caído → recovery pendiente
  let s = await storedState(kv);
  const incidentId = s.incident.id;
  checkNum = 1; // fuerza un nuevo fallo de salud en el siguiente ciclo
  const beforeTg = impl.telegramCalls().length;
  const r = await runMonitor(env({ kv }), { fetch: impl }); // cae de nuevo
  assert.equal(r.incident, incidentId); // misma incidencia abierta
  s = await storedState(kv);
  assert.equal(s.incident.recoveryMsgId, null); // recovery retirado
  assert.deepEqual(s.queue, []); // nada falso en cola
  assert.equal(impl.telegramCalls().length, beforeTg); // sin llamada nueva
  tgFail = false;
  const beforeR = impl.telegramCalls().length;
  const r2 = await runMonitor(env({ kv }), { fetch: impl }); // sano de verdad
  assert.equal(r2.incident, null);
  assert.equal(impl.telegramCalls().length - beforeR, 1); // solo el recovery pendiente
  const downs = impl.telegramTexts().filter((t) => t.includes('inaccesible'));
  assert.equal(downs.length, 1); // nunca un segundo down
});

test('5 ciclos con Telegram caído y salud en parpadeo: sin ráfagas, duplicados ni pérdida', async () => {
  const kv = fakeKv();
  const healthSeq = ['fail', 'fail', 'ok', 'fail', 'ok', 'ok']; // 2 ciclos extra de drenaje
  let tick = -1;
  let tgFail = true;
  const impl = fetchRouter({
    health: () => jsonResponse(200, healthSeq[++tick] === 'ok' ? OK_BODY : {}),
    telegram: () => (tgFail ? telegramFail() : telegramOk()), // TG caído TODO el rato
  });
  for (let i = 0; i < 5; i++) {
    await runMonitor(env({ kv }), { fetch: impl });
  }
  let s = await storedState(kv);
  assert.ok(s.incident); // sigue abierta (su recovery no quedó entregado)
  assert.ok(s.queue.length <= 3); // acotada por construcción (test+down+recovery)
  assert.equal(new Set(s.queue.map((m) => m.id)).size, s.queue.length);
  assert.equal(s.queue.filter((m) => m.kind === 'down').length <= 1, true);
  assert.equal(s.queue.filter((m) => m.kind === 'recovery').length <= 1, true);
  // Telegram vuelve: drena la cola en FIFO, un envío por mensaje, sin ráfaga.
  // (sin tick++ manual: el propio callback de salud avanza el índice)
  tgFail = false;
  const before = impl.telegramCalls().length;
  const r = await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(impl.telegramCalls().length - before, s.queue.length);
  assert.equal(r.queueCount, 0);
  if (r.incident === null) {
    const drained = impl.telegramTexts().slice(before);
    assert.deepEqual(
      drained.map((t) => (t.includes('inaccesible') ? 'down' : 'recovery')),
      ['down', 'recovery'],
    );
  }
});

// ── Hallazgo 4: presupuesto de escrituras y timestamps ──────────────────

test('régimen sano: exactamente 1 escritura KV por ciclo (~288 writes/día)', async () => {
  const kv = fakeKv();
  const impl = fetchRouter({ health: healthOk, telegram: () => { throw new Error('no'); } });
  await runMonitor(env({ kv }), { fetch: impl });
  await runMonitor(env({ kv }), { fetch: impl });
  await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(kv.puts, 3);
});

test('lastCheckAt se actualiza en cada ciclo (evidencia de cron auténtico)', async () => {
  const kv = fakeKv();
  const now = clock();
  const impl = fetchRouter({ health: healthOk, telegram: () => { throw new Error('no'); } });
  await runMonitor(env({ kv }), { fetch: impl, now });
  const s1 = await storedState(kv);
  await runMonitor(env({ kv }), { fetch: impl, now });
  const s2 = await storedState(kv);
  assert.ok(s2.health.lastCheckAt > s1.health.lastCheckAt);
  assert.equal(s2.health.lastHealthyAt, s2.health.lastCheckAt);
});

// ── Migración v1 → v2 ───────────────────────────────────────────────────

test('migración v1: deduplica recoveries acumulados y asigna ids estables sin perder avisos', async () => {
  const kv = fakeKv(new Map([['state', JSON.stringify({
    status: 'down', failCount: 4,
    incident: { id: '20261122T170000Z', startedMs: Date.parse('2026-11-22T17:00:00Z') },
    pending: [
      { kind: 'down', text: '[ASIR] down', createdAt: '2026-11-22T17:00:00Z' },
      { kind: 'recovery', text: '[ASIR] rec A', createdAt: '2026-11-22T17:05:00Z' },
      { kind: 'recovery', text: '[ASIR] rec B', createdAt: '2026-11-22T17:10:00Z' },
      { kind: 'recovery', text: '[ASIR] rec C', createdAt: '2026-11-22T17:15:00Z' },
    ],
    testPending: false, lastCheckAt: '2026-11-22T17:15:00Z', lastHealthyAt: null, lastError: 'http_503',
  })]]));
  let tgFail = true;
  const impl = fetchRouter({
    health: healthOk,
    telegram: () => (tgFail ? telegramFail() : telegramOk()),
  });
  await runMonitor(env({ kv }), { fetch: impl }); // migra pero no entrega (TG caído)
  // Cola migrada: 1 down + 1 recovery (dedup), en FIFO, sin recortes.
  assert.deepEqual((await storedState(kv)).queue.map((m) => m.kind), ['down', 'recovery']);
  // Drena: entrega el down pendiente y el recovery, y cierra la incidencia.
  const before = impl.telegramCalls().length;
  tgFail = false;
  const r = await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(r.incident, null);
  const drained = impl.telegramTexts().slice(before);
  assert.deepEqual(drained, ['[ASIR] down', '[ASIR] rec A']); // FIFO, 1 cada uno
});

test('migración v1: una cola legacy larga se preserva entera (sin recorte silencioso)', async () => {
  const legacyPending = Array.from({ length: 5 }, (_, i) => ({
    kind: 'down', text: `[ASIR] legacy ${i}`, createdAt: `2026-11-22T17:0${i}:00Z`,
  }));
  const kv = fakeKv(new Map([['state', JSON.stringify({
    status: 'healthy', failCount: 0, incident: null, pending: legacyPending,
    testPending: false, lastCheckAt: null, lastHealthyAt: null, lastError: null,
  })]]));
  const impl = fetchRouter({ health: healthOk, telegram: telegramOk });
  await runMonitor(env({ kv }), { fetch: impl });
  const texts = impl.telegramTexts();
  assert.equal(texts.length, 5); // 5/5 entregados, ninguno descartado
  assert.deepEqual(texts, legacyPending.map((m) => m.text)); // FIFO exacto
  assert.equal((await storedState(kv)).queue.length, 0);
});

test('migración v1: incidencia abierta legacy no re-encola otro down', async () => {
  const kv = fakeKv(new Map([['state', JSON.stringify({
    status: 'down', failCount: 3,
    incident: { id: '20261122T170000Z', startedMs: Date.parse('2026-11-22T17:00:00Z') },
    pending: [], testPending: false, lastCheckAt: null, lastHealthyAt: null, lastError: 'timeout',
  })]]));
  const impl = fetchRouter({ health: healthFail, telegram: telegramOk });
  const r = await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(impl.telegramCalls().length, 0); // el down ya se notificó en v1
  assert.equal(r.incident, '20261122T170000Z'); // incidencia preservada
  assert.equal((await storedState(kv)).queue.length, 0);
});

// ── Mensajes, superficie pública y primitivas ───────────────────────────

test('KV ilegible → skip fail-safe sin llamar a Telegram', async () => {
  const brokenKv = { get: async () => { throw new Error('kv down'); }, put: async () => {} };
  const impl = fetchRouter({ health: healthOk, telegram: telegramOk });
  const r = await runMonitor(env({ kv: brokenKv }), { fetch: impl });
  assert.equal(r.skipped, 'kv_read_failed');
  assert.equal(impl.calls.length, 0);
});

test('mensaje TEST one-off: se envía una sola vez y queda consumido', async () => {
  const kv = fakeKv();
  const impl = fetchRouter({ health: healthOk, telegram: telegramOk });
  await kv.store.set('state', JSON.stringify({
    schema: 2,
    health: { status: 'unknown', failCount: 0, lastCheckAt: null, lastHealthyAt: null, lastError: null },
    incident: null, queue: [], testPending: true,
  }));
  const r1 = await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(impl.telegramCalls().length, 1);
  assert.match(JSON.parse(impl.telegramCalls()[0].init.body).text, /^\[TEST\]/);
  const raw1 = JSON.parse(await kv.store.get('state'));
  assert.equal(raw1.testPending, false);
  await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(impl.telegramCalls().length, 1); // no se repite
});

test('los mensajes no filtran token, cuerpos HTTP ni métricas en bruto', async () => {
  const kv = fakeKv();
  const impl = fetchRouter({ health: healthFail, telegram: telegramOk });
  await runMonitor(env({ kv }), { fetch: impl });
  await runMonitor(env({ kv }), { fetch: impl });
  assert.equal(impl.telegramCalls().length, 1);
  const text = JSON.parse(impl.telegramCalls()[0].init.body).text;
  assert.ok(!text.includes('test-token'));
  assert.ok(!text.includes('chunks'));
  assert.ok(!text.includes('windows'));
  assert.ok(!text.includes('<html'));
  assert.match(text, /^\[ASIR\]/);
});

test('fetch handler público → 404 siempre (sin rutas de depuración)', async () => {
  const noop = { waitUntil() {} };
  const res = await worker.fetch(new Request('https://worker.example/health'), env(), noop);
  assert.equal(res.status, 404);
  const res2 = await worker.fetch(new Request('https://worker.example/'), env(), noop);
  assert.equal(res2.status, 404);
});

test('sendTelegram: solo ok:true marca entregado', async () => {
  const okCase = await sendTelegram('t', 'c', 'hola', async () => jsonResponse(200, { ok: true }));
  assert.equal(okCase.delivered, true);
  const badCase = await sendTelegram('t', 'c', 'hola', async () => jsonResponse(400, { ok: false, error_code: 400 }));
  assert.equal(badCase.delivered, false);
  assert.equal(badCase.reason, 'tg_400');
});
