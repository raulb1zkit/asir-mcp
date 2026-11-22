'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../graph/app.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function snapshot() {
  return {
    corpus_id: 'synthetic-corpus', captured_at: '2026-11-14T00:00:00Z',
    counts: {chunk: 2, source: 2, subject: 2},
    nodes: [
      {id: 'subject:ASO', kind: 'subject', label: 'ASO', subjects: ['ASO']},
      {id: 'subject:SRI', kind: 'subject', label: 'SRI', subjects: ['SRI']},
      {id: 'source:ASO:a', kind: 'source', label: 'Manual de procesos', subjects: ['ASO']},
      {id: 'source:SRI:b', kind: 'source', label: 'Manual de DNS', subjects: ['SRI']},
      {id: 'chunk:a', kind: 'chunk', label: 'Contenido compartido', text: 'Este contenido pertenece a dos materias.', subjects: ['ASO', 'SRI'], sources: ['source:ASO:a', 'source:SRI:b'], review: false},
      {id: 'chunk:b', kind: 'chunk', label: 'Resolución de nombres', text: 'Una caché mantiene respuestas.', subjects: ['SRI'], sources: ['source:SRI:b'], review: false},
    ],
    links: [
      {source: 'subject:ASO', target: 'source:ASO:a', kind: 'contains'},
      {source: 'subject:SRI', target: 'source:SRI:b', kind: 'contains'},
      {source: 'source:ASO:a', target: 'chunk:a', kind: 'contains'},
      {source: 'source:SRI:b', target: 'chunk:a', kind: 'contains'},
      {source: 'source:SRI:b', target: 'chunk:b', kind: 'contains'},
      {source: 'chunk:a', target: 'chunk:b', kind: 'adjacent'},
    ],
  };
}

async function page(data = snapshot(), {ok = true, reducedMotion = true} = {}) {
  function element() {
    const e = {value: '', textContent: '', hidden: false, children: [],
      append(child) { this.children.push(child); },
      replaceChildren(...children) { this.children = children; },
      addEventListener(type, callback) { this['on' + type] = callback; },
    };
    // Every DOM node rejects HTML sinks. Untrusted text must stay text.
    Object.defineProperty(e, 'innerHTML', {set() { throw Error('HTML sink used'); }});
    return e;
  }
  const names = ['graph', 'subject', 'search', 'limit', 'visible', 'stats', 'results',
    'details', 'kind', 'title', 'meta', 'content', 'neighbors', 'reset', 'close', 'version'];
  const elements = Object.fromEntries(names.map(name => [name, element()]));
  elements.limit.value = '300';
  const context2d = Object.fromEntries(['setTransform', 'clearRect', 'save', 'translate', 'scale',
    'beginPath', 'moveTo', 'lineTo', 'stroke', 'arc', 'fill', 'fillText', 'restore'].map(name => [name, () => {}]));
  Object.assign(elements.graph, {getContext: () => context2d, clientWidth: 900, clientHeight: 700,
    setPointerCapture() {}, getBoundingClientRect: () => ({left: 0, top: 0})});
  const requests = [], animationRequests = [];
  const context = vm.createContext({
    document: {getElementById: id => elements[id], createElement: () => element()},
    window: {}, devicePixelRatio: 1, matchMedia: () => ({matches: reducedMotion}),
    requestAnimationFrame: callback => { animationRequests.push(callback); return animationRequests.length; }, cancelAnimationFrame() {},
    setTimeout(callback) { callback(); return 1; }, clearTimeout() {},
    fetch: async url => { requests.push(url); return {ok, json: async () => data}; },
  });
  vm.runInContext(source, context, {filename: 'graph/app.js'});
  await flush();
  return {elements, requests, animationRequests,
    setHidden: value => { context.document.hidden = value; },
    read: expr => JSON.parse(vm.runInContext(`JSON.stringify(${expr})`, context)),
    run: code => vm.runInContext(code, context),
  };
}

test('shared chunks keep only the selected subject and its document memberships on the graph', async () => {
  const p = await page();
  p.elements.subject.value = 'ASO';
  p.elements.subject.onchange();
  assert.deepEqual(p.read('nodes.map(n => n.id).sort()'), ['chunk:a', 'source:ASO:a', 'subject:ASO']);
  assert.deepEqual(p.read('edges.map(e => [e.source,e.target])'), [
    ['subject:ASO', 'source:ASO:a'], ['source:ASO:a', 'chunk:a'],
  ]);
  assert.match(p.elements.stats.textContent, /Mostrando 1 de 1 chunks/);
});

test('searching a document title shows its child chunks even without title words in their text', async () => {
  const p = await page();
  p.elements.search.value = 'Manual de DNS';
  p.elements.search.oninput();
  assert.deepEqual(p.read("nodes.filter(n=>n.kind==='chunk').map(n=>n.id).sort()"), ['chunk:a', 'chunk:b']);
  assert.ok(p.elements.results.children.some(e => e.textContent === 'Manual de DNS'));
  assert.match(p.elements.stats.textContent, /Mostrando 2 de 2 chunks/);
});

test('title search respects the selected subject', async () => {
  const p = await page();
  p.elements.subject.value = 'ASO';
  p.elements.search.value = 'Manual de DNS';
  p.elements.search.oninput();
  assert.deepEqual(p.read('nodes'), []);
  assert.equal(p.elements.results.children.length, 0);
});

test('empty snapshot renders zero counts without falling into the load error state', async () => {
  const p = await page({corpus_id: 'empty-corpus', captured_at: '2026-11-14T00:00:00Z', counts: {}, nodes: [], links: []});
  assert.match(p.elements.stats.textContent, /0 chunks indexados/);
  assert.match(p.elements.stats.textContent, /0 documentos/);
  assert.equal(p.elements.visible.textContent, '0 nodos · 0 conexiones');
  assert.deepEqual(p.read('nodes'), []);
  assert.deepEqual(p.requests, ['/api/graph']);
});

test('hostile HTML labels and full fragment content are displayed through text nodes', async () => {
  const data = snapshot();
  const hostile = '<img src=x onerror=alert(1)><script>attack()</script>';
  data.nodes.find(n => n.id === 'chunk:a').label = hostile;
  data.nodes.find(n => n.id === 'chunk:a').text = hostile;
  const p = await page(data);
  p.elements.search.value = 'onerror';
  p.elements.search.oninput();
  const result = p.elements.results.children.find(e => e.textContent === hostile);
  assert.ok(result);
  result.onclick();
  assert.equal(p.elements.title.textContent, hostile);
  assert.equal(p.elements.content.textContent, hostile);
  assert.equal(p.elements.details.hidden, false);
  assert.equal(p.elements.neighbors.children.length, 3);
  assert.doesNotMatch(source, /\.innerHTML\s*=|insertAdjacentHTML\s*\(|document\.write\s*\(/);
});

test('failed snapshot request shows a clear error without graph data', async () => {
  const p = await page(snapshot(), {ok: false});
  assert.match(p.elements.stats.textContent, /No se pudo cargar el corpus/);
  assert.deepEqual(p.read('nodes'), []);
});

function manySubjects(size = 80) {
  const subjects = ['ASO', 'Bases de Datos', 'Digitalizacion', 'IAW', 'SAD', 'SRI'];
  const graph = {corpus_id: 'many-subjects', captured_at: '2026-11-14T00:00:00Z',
    nodes: [], links: [], counts: {chunk: size * subjects.length, subject: subjects.length, source: subjects.length}};
  for (const subject of subjects) {
    const document = 'source:' + subject;
    graph.nodes.push({id: 'subject:' + subject, kind: 'subject', label: subject, subjects: [subject]},
      {id: document, kind: 'source', label: subject + ' manual', subjects: [subject]});
    graph.links.push({source: 'subject:' + subject, target: document, kind: 'contains'});
    for (let i = 0; i < size; i++) {
      const id = 'chunk:' + subject + ':' + i;
      graph.nodes.push({id, kind: 'chunk', label: 'Synthetic ' + i, text: 'Synthetic content',
        subjects: [subject], sources: [document], review: false});
      graph.links.push({source: document, target: id, kind: 'contains'});
    }
  }
  return graph;
}

test('default sample balances subjects despite corpus order, and selection is deterministic', async () => {
  const p = await page(manySubjects());
  const groups = p.read("nodes.filter(n=>n.kind==='chunk').reduce((counts,n)=>(counts[n.subjects[0]]=(counts[n.subjects[0]]||0)+1,counts),{})");
  assert.deepEqual(groups, {ASO: 50, 'Bases de Datos': 50, Digitalizacion: 50, IAW: 50, SAD: 50, SRI: 50});
  const ids = p.read("nodes.filter(n=>n.kind==='chunk').map(n=>n.id)");
  p.elements.limit.onchange();
  assert.deepEqual(p.read("nodes.filter(n=>n.kind==='chunk').map(n=>n.id)"), ids);
});

test('reduced motion keeps the settled map stationary with no animation loop', async () => {
  for (const reducedMotion of [true]) {
    const p = await page(manySubjects(), {reducedMotion});
    assert.equal(p.animationRequests.length, 0);
    assert.ok(p.read('nodes.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)&&n.vx===0&&n.vy===0)'));
    assert.ok(p.read('ticks<=72'));
    const before = p.read('nodes.map(n=>[n.x,n.y])');
    await flush();
    assert.deepEqual(p.read('nodes.map(n=>[n.x,n.y])'), before);
    p.elements.reset.onclick();
    assert.deepEqual(p.read('nodes.map(n=>[n.x,n.y])'), before);
  }
});

test('fit centers the node bounds and keeps all nodes inside the viewport on reset and resize', async () => {
  const p = await page(manySubjects());
  function checkFit() {
    const points = p.read('nodes.map(n=>({x:width/2+pan.x+n.x*scale,y:height/2+pan.y+n.y*scale}))');
    const xs = points.map(n=>n.x), ys = points.map(n=>n.y);
    assert.ok(Math.min(...xs) >= 69 && Math.max(...xs) <= p.elements.graph.clientWidth - 69);
    assert.ok(Math.min(...ys) >= 69 && Math.max(...ys) <= p.elements.graph.clientHeight - 69);
    assert.ok(Math.abs((Math.min(...xs)+Math.max(...xs))/2 - p.elements.graph.clientWidth/2) < 1e-8);
    assert.ok(Math.abs((Math.min(...ys)+Math.max(...ys))/2 - p.elements.graph.clientHeight/2) < 1e-8);
  }
  checkFit();
  p.run('scale=8;pan={x:999,y:999};');
  p.elements.reset.onclick();
  checkFit();
  p.elements.graph.clientWidth = 400;
  p.elements.graph.clientHeight = 500;
  p.run('window.onresize()');
  checkFit();
});

test('bucket acceleration applies a circular force cutoff and preserves rotational symmetry', async () => {
  const p = await page();
  // Cases span grid cell boundaries, diagonal candidates and out-of-range pairs.
  const points = [{x: 1, y: -2}, {x: 41, y: -2}, {x: 41, y: 38}, {x: -12, y: -31}];
  const angle = Math.PI / 3, c = Math.cos(angle), s = Math.sin(angle);
  const rotate = ({x, y}) => ({x: x*c-y*s, y: x*s+y*c});
  const forces = p.run('repulsion(' + JSON.stringify(points) + ')');
  const rotated = p.run('repulsion(' + JSON.stringify(points.map(rotate)) + ')');
  for (let i=0;i<forces.length;i++) {
    const expected = rotate(forces[i]);
    assert.ok(Math.abs(expected.x-rotated[i].x) < 1e-10);
    assert.ok(Math.abs(expected.y-rotated[i].y) < 1e-10);
  }
  const outside = p.read('repulsion([{x:0,y:0},{x:40,y:40}],56)');
  assert.deepEqual(outside, [{x:0,y:0},{x:0,y:0}]);
});


test('ambient motion is bounded around the settled layout and never restarts the force simulation', async () => {
  const p = await page(manySubjects(), {reducedMotion: false});
  assert.equal(p.animationRequests.length, 1);
  const base = p.read('nodes.map(n=>[n.bx,n.by])');
  p.animationRequests.shift()(0);
  p.animationRequests.shift()(5000);
  assert.ok(p.read('nodes.some(n=>n.x!==n.bx||n.y!==n.by)'));
  for (const seconds of [10,1000,1000000]) {
    p.run(`driftPositions(${seconds})`);
    assert.ok(p.read('nodes.every(n=>Math.abs(n.x-n.bx)*scale<=6.800001&&Math.abs(n.y-n.by)*scale<=6.800001&&n.vx===0&&n.vy===0)'));
  }
  assert.deepEqual(p.read('nodes.map(n=>[n.bx,n.by])'),base);
  const n=p.read('nodes[0].id');
  p.run(`selected=${JSON.stringify(n)};driftPositions(70)`);
  assert.ok(p.read('nodes[0].x===nodes[0].bx&&nodes[0].y===nodes[0].by'));
});

test('ambient frames pause drawing when hidden or dragging and respect the frame rate cap', async () => {
  const p = await page(snapshot(), {reducedMotion:false});
  p.animationRequests.shift()(0);
  const before=p.read('nodes.map(n=>[n.x,n.y])');
  p.animationRequests.shift()(10);
  assert.deepEqual(p.read('nodes.map(n=>[n.x,n.y])'),before);
  p.setHidden(true);
  p.animationRequests.shift()(1000);
  assert.deepEqual(p.read('nodes.map(n=>[n.x,n.y])'),before);
  p.setHidden(false);p.run('drag={x:0,y:0}');
  p.animationRequests.shift()(2000);
  assert.deepEqual(p.read('nodes.map(n=>[n.x,n.y])'),before);
  p.run('drag=null');p.animationRequests.shift()(3000);
  assert.notDeepEqual(p.read('nodes.map(n=>[n.x,n.y])'),before);
});
