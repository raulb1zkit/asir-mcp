"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("landing/oauth/consent/consent.js", "utf8");

function page({search = "?authorization_id=test-request", user = {id: "synthetic"}, data, key = "sb_publishable_fake"} = {}) {
  const names = ["status", "sign-in", "consent", "google", "approve", "deny", "client", "scopes", "redirect", "identity", "switch-account"];
  const elements = Object.fromEntries(names.map(name => [name, {hidden: true, disabled: false, textContent: "", events: {}, addEventListener(event, fn) { this.events[event] = fn; }}]));
  const storage = new Map(), navigations = [], calls = [];
  const client = {auth: {
    async getUser() { return {data: {user}, error: null}; },
    async signOut() { calls.push({signOut: true}); return {error: null}; },
    async signInWithOAuth(args) { calls.push(args); return {error: null}; },
    oauth: {
      async getAuthorizationDetails(id) { calls.push({details: id}); return {data: data || {authorization_id: id, client: {name: "<not html>"}, scope: "openid", redirect_uri: "http://localhost:1234/callback"}, error: null}; },
      async approveAuthorization(id, options) { calls.push({approve: id, options}); return {data: {redirect_url: "http://localhost:1234/callback?code=fake"}, error: null}; },
      async denyAuthorization(id) { calls.push({deny: id}); return {data: {redirect_url: "http://localhost:1234/callback?error=denied"}, error: null}; }
    }
  }};
  const context = {
    URLSearchParams,
    document: {getElementById: id => elements[id]},
    sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
    history: {replaceState: (...args) => calls.push({replaceState: args})},
    window: {location: {search, pathname: "/oauth/consent/", origin: "https://asir.example", assign: url => navigations.push(url)}, supabase: {createClient: (url, publishable, options) => { calls.push({options}); return client; }}, ASIR_AUTH_CONFIG: {publishableKey: key}}
  };
  vm.runInNewContext(source, context);
  return {elements, storage, calls, navigations};
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test("requires an authorization request", async () => {
  const p = page({search: ""}); await flush();
  assert.match(p.elements.status.textContent, /Falta la solicitud/);
  assert.equal(p.elements.consent.hidden, true);
});

test("displays untrusted client name as text and requires approval", async () => {
  const p = page(); await flush();
  assert.equal(p.elements.client.textContent, "<not html>");
  assert.equal(p.elements.consent.hidden, false);
  assert.equal(p.calls.find(call => call.options).options.auth.flowType, "pkce");
  assert.equal(p.navigations.length, 0);
  await p.elements.approve.events.click(); await flush();
  assert.deepEqual(p.navigations, ["http://localhost:1234/callback?code=fake"]);
  assert.equal(p.calls.find(call => call.approve).options.skipBrowserRedirect, true);
  assert.equal(p.storage.size, 0);
});

test("starts Google login while preserving the pending request", async () => {
  const p = page({user: null}); await flush();
  assert.equal(p.elements["sign-in"].hidden, false);
  await p.elements.google.events.click();
  assert.equal(p.calls.at(-1).provider, "google");
  assert.equal(p.calls.at(-1).options.redirectTo, "https://asir.example/oauth/consent/");
  assert.equal(p.storage.get("asir_oauth_authorization_id"), "test-request");
});

test("rejects a secret key in the browser configuration", async () => {
  const p = page({key: "sb_secret_not_allowed"}); await flush();
  assert.match(p.elements.status.textContent, /no está configurado/);
  assert.equal(p.elements.consent.hidden, true);
});

test("switches accounts without losing the pending authorization", async () => {
  const p = page(); await flush();
  await p.elements["switch-account"].events.click();
  assert.equal(p.calls.some(call => call.signOut), true);
  assert.equal(p.calls.at(-1).options.queryParams.prompt, "select_account");
  assert.equal(p.storage.get("asir_oauth_authorization_id"), "test-request");
});
