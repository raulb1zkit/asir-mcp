'use strict';
const $=id=>document.getElementById(id), canvas=$('graph'), ctx=canvas.getContext('2d');
const colors={subject:'#d5b477',source:'#70aeb6',chunk:'#94c8a7'};
let data, nodes=[],edges=[],selected=null, scale=1,pan={x:0,y:0},drag=null,width=0,height=0,ticks=0,motionFrame=null,motionEpoch=null,lastMotionDraw=-Infinity;
const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
function resize(){const d=devicePixelRatio||1;width=canvas.clientWidth;height=canvas.clientHeight;canvas.width=width*d;canvas.height=height*d;ctx.setTransform(d,0,0,d,0,0);fitGraph();draw();}
function rebuild(){
 const subject=$('subject').value,q=$('search').value.trim().toLocaleLowerCase(),limit=Number($('limit').value);
 const matchingSources=new Set(data.nodes.filter(n=>n.kind==='source'&&(!subject||n.subjects.includes(subject))&&n.label.toLocaleLowerCase().includes(q)).map(n=>n.id));
 const matches=data.nodes.filter(n=>(!subject||n.subjects.includes(subject))&&(!q||(n.label+' '+(n.text||'')).toLocaleLowerCase().includes(q)||(n.sources||[]).some(s=>matchingSources.has(s))));
 const chosen=balancedSample(matches.filter(n=>n.kind==='chunk'),limit,subject), ids=new Set(chosen.map(n=>n.id));
 const lookup=new Map(data.nodes.map(n=>[n.id,n]));
 for(const n of chosen){for(const s of n.sources)if(!subject||lookup.get(s).subjects.includes(subject))ids.add(s);for(const s of n.subjects)if(!subject||s===subject)ids.add('subject:'+s);}
 if(!q)for(const n of matches)if(n.kind==='subject')ids.add(n.id);
 nodes=data.nodes.filter(n=>ids.has(n.id)).map(n=>({...n,x:0,y:0,vx:0,vy:0}));
 const index=new Map(nodes.map(n=>[n.id,n]));edges=data.links.filter(e=>ids.has(e.source)&&ids.has(e.target)).map(e=>({...e,a:index.get(e.source),b:index.get(e.target)}));
 $('visible').textContent=`${nodes.length.toLocaleString()} nodos · ${edges.length.toLocaleString()} conexiones`;
 $('stats').textContent=`${(data.counts.chunk||0).toLocaleString()} chunks indexados · ${(data.counts.source||0).toLocaleString()} documentos por asignatura. Mostrando ${chosen.length} de ${matches.filter(n=>n.kind==='chunk').length.toLocaleString()} chunks coincidentes.`;
 $('results').replaceChildren(); if(q)for(const n of matches.slice(0,15)){const b=document.createElement('button');b.textContent=n.label;b.onclick=()=>select(n);$('results').append(b);}
 stopMotion();seedLayout(subject);settleLayout();fitGraph();draw();startMotion();
}
function select(n){selected=n.id;$('details').hidden=false;$('kind').textContent={chunk:'FRAGMENTO INDEXADO',source:'DOCUMENTO',subject:'ASIGNATURA'}[n.kind];$('title').textContent=n.label;$('meta').textContent=n.subjects.join(' / ')+(n.review?' · Requiere revisión OCR':'');$('content').textContent=n.text||'Explora los nodos conectados para leer sus fragmentos.';$('neighbors').replaceChildren();
 const linked=new Set(data.links.filter(e=>e.source===n.id||e.target===n.id).map(e=>e.source===n.id?e.target:e.source));
 for(const other of data.nodes.filter(n=>linked.has(n.id)).slice(0,25)){const b=document.createElement('button');b.textContent=other.label;b.onclick=()=>select(other);$('neighbors').append(b);}draw();}
// Stable hashes make the same corpus/filter produce the same sample and map.
function hash(text){let value=2166136261;for(const c of text){value^=c.charCodeAt(0);value=Math.imul(value,16777619);}return value>>>0;}
function balancedSample(candidates,limit,subject){
 const groups=new Map();
 for(const n of candidates){const key=subject||[...n.subjects].sort()[0]||'';if(!groups.has(key))groups.set(key,[]);groups.get(key).push(n);}
 const queues=[...groups].sort(([a],[b])=>a.localeCompare(b)).map(([,items])=>items.sort((a,b)=>hash(a.id)-hash(b.id)||a.id.localeCompare(b.id)));
 const result=[];let cursor=0;
 while(result.length<limit){let added=false;for(const items of queues)if(cursor<items.length&&result.length<limit){result.push(items[cursor]);added=true;}if(!added)break;cursor++;}
 return result;
}
function seedLayout(subject){
 const index=new Map(nodes.map(n=>[n.id,n])),subjects=nodes.filter(n=>n.kind==='subject').sort((a,b)=>a.id.localeCompare(b.id));
 const radius=subjects.length>1?Math.max(200,subjects.length*58):0;
 for(const [i,n] of subjects.entries()){const angle=2*Math.PI*i/subjects.length-Math.PI/2;n.x=n.ax=Math.cos(angle)*radius;n.y=n.ay=Math.sin(angle)*radius;}
 const sourceGroups=new Map();
 for(const n of nodes.filter(n=>n.kind==='source')){const key=n.subjects[0];if(!sourceGroups.has(key))sourceGroups.set(key,[]);sourceGroups.get(key).push(n);}
 for(const [key,items] of sourceGroups){const anchor=index.get('subject:'+key)||{x:0,y:0};items.sort((a,b)=>hash(a.id)-hash(b.id));for(const [i,n] of items.entries()){const angle=i*2.3999632297,r=58+22*Math.sqrt(i);n.x=n.ax=anchor.x+Math.cos(angle)*r;n.y=n.ay=anchor.y+Math.sin(angle)*r;}}
 const chunksBySource=new Map();
 for(const n of nodes.filter(n=>n.kind==='chunk')){const sources=n.sources.filter(id=>index.has(id)&&(!subject||index.get(id).subjects.includes(subject))).sort();const key=sources[0]||'';if(!chunksBySource.has(key))chunksBySource.set(key,[]);chunksBySource.get(key).push(n);}
 for(const [key,items] of chunksBySource){const anchor=index.get(key)||{x:0,y:0};items.sort((a,b)=>hash(a.id)-hash(b.id));for(const [i,n] of items.entries()){const angle=i*2.3999632297+hash(key)/4294967296*Math.PI*2,r=20+7*Math.sqrt(i);n.x=n.ax=anchor.x+Math.cos(angle)*r;n.y=n.ay=anchor.y+Math.sin(angle)*r;}}
}
function repulsion(points,radius=56){
 // Buckets find candidates only. The circular cutoff makes forces independent
 // of grid direction: a square neighborhood must never define the force range.
 const cells=new Map(),forces=points.map(()=>({x:0,y:0}));
 for(const [i,n] of points.entries()){const key=Math.floor(n.x/radius)+','+Math.floor(n.y/radius);if(!cells.has(key))cells.set(key,[]);cells.get(key).push(i);}
 for(const [i,n] of points.entries()){const cx=Math.floor(n.x/radius),cy=Math.floor(n.y/radius);for(let x=cx-1;x<=cx+1;x++)for(let y=cy-1;y<=cy+1;y++)for(const j of cells.get(x+','+y)||[]){if(i===j)continue;const dx=n.x-points[j].x,dy=n.y-points[j].y,d=Math.hypot(dx,dy);if(d===0||d>=radius)continue;const force=2.8*(1-d/radius)**2;forces[i].x+=dx/d*force;forces[i].y+=dy/d*force;}}
 return forces;
}
function physics(alpha){
 const forces=repulsion(nodes);
 for(const [i,n] of nodes.entries()){n.vx+=(forces[i].x+(n.ax-n.x)*.035)*alpha;n.vy+=(forces[i].y+(n.ay-n.y)*.035)*alpha;}
 for(const e of edges){const dx=e.b.x-e.a.x,dy=e.b.y-e.a.y,d=Math.hypot(dx,dy)||1;const strength=e.kind==='adjacent'?.0004:.0015,f=(d-35)*strength*alpha;e.a.vx+=dx/d*f;e.a.vy+=dy/d*f;e.b.vx-=dx/d*f;e.b.vy-=dy/d*f;}
 for(const n of nodes){n.vx*=.55;n.vy*=.55;n.x+=n.vx;n.y+=n.vy;}
}
function settleLayout(){
 // Bounded computation before display, never an ongoing motion loop. Reduced
 // motion uses the same stable map with a smaller relaxation budget.
 const steps=reduced?48:72;ticks=0;
 for(;ticks<steps;ticks++)physics((1-ticks/steps)**2);
 for(const n of nodes){n.vx=0;n.vy=0;n.bx=n.x;n.by=n.y;}
}
// Ambient drift never feeds back into the force layout: it cannot accumulate
// energy or distort clusters. Its amplitude stays small in screen pixels.
function driftPositions(seconds){
 for(const n of nodes){
  const seed=hash(n.id),phase=seed/4294967296*Math.PI*2;
  const amplitude=(n.kind==='subject'?1.2:n.kind==='source'?2.2:3.4)/scale;
  const speed=.28+(seed%100)/300;
  n.x=n.bx+(n.id===selected?0:amplitude*(Math.sin(seconds*speed+phase)-Math.sin(phase)));
  n.y=n.by+(n.id===selected?0:amplitude*(Math.cos(seconds*speed*.83+phase)-Math.cos(phase)));
 }
}
function stopMotion(){
 if(motionFrame!==null)cancelAnimationFrame(motionFrame);
 motionFrame=null;motionEpoch=null;lastMotionDraw=-Infinity;
}
function animateAmbient(timestamp){
 motionFrame=null;
 if(!document.hidden&&!drag&&timestamp-lastMotionDraw>=1000/30){
  if(motionEpoch===null)motionEpoch=timestamp;
  driftPositions((timestamp-motionEpoch)/1000);draw();lastMotionDraw=timestamp;
 }
 motionFrame=requestAnimationFrame(animateAmbient);
}
function startMotion(){
 if(!reduced&&nodes.length)motionFrame=requestAnimationFrame(animateAmbient);
}
function fitGraph(){
 if(!nodes.length){scale=1;pan={x:0,y:0};return;}
 let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
 for(const n of nodes){minX=Math.min(minX,n.x);maxX=Math.max(maxX,n.x);minY=Math.min(minY,n.y);maxY=Math.max(maxY,n.y);}
 const padding=70;
 scale=Math.max(.001,Math.min(2,Math.max(1,width-2*padding)/Math.max(1,maxX-minX),Math.max(1,height-2*padding)/Math.max(1,maxY-minY)));
 pan={x:-(minX+maxX)/2*scale,y:-(minY+maxY)/2*scale};
}
function draw(){ctx.clearRect(0,0,width,height);ctx.save();ctx.translate(width/2+pan.x,height/2+pan.y);ctx.scale(scale,scale);ctx.lineWidth=.6/scale;
 for(const e of edges){ctx.strokeStyle=e.source===selected||e.target===selected?'#b8d2bc99':e.kind==='adjacent'?'#72947b19':'#8db5a52b';ctx.beginPath();ctx.moveTo(e.a.x,e.a.y);ctx.lineTo(e.b.x,e.b.y);ctx.stroke();}
 for(const n of nodes){const r=n.kind==='subject'?8:n.kind==='source'?4:2.2;ctx.fillStyle=colors[n.kind];ctx.globalAlpha=n.kind==='chunk'?.7:1;ctx.beginPath();ctx.arc(n.x,n.y,n.id===selected?r+3:r,0,Math.PI*2);ctx.fill();ctx.globalAlpha=1;if(n.kind==='subject'||n.id===selected){ctx.fillStyle='#d8e5dc';ctx.font=`${11/scale}px system-ui`;ctx.fillText(n.label.slice(0,55),n.x+12/scale,n.y+4/scale);}}
 ctx.restore();}
function point(e){const r=canvas.getBoundingClientRect();return{x:e.clientX-r.left,y:e.clientY-r.top};}
canvas.onpointerdown=e=>{const p=point(e);drag={...p,start:p};canvas.setPointerCapture(e.pointerId);};
canvas.onpointermove=e=>{if(!drag)return;const p=point(e);pan.x+=p.x-drag.x;pan.y+=p.y-drag.y;drag.x=p.x;drag.y=p.y;draw();};
canvas.onpointerup=e=>{if(!drag)return;const p=point(e);if(Math.hypot(p.x-drag.start.x,p.y-drag.start.y)<5){const x=(p.x-width/2-pan.x)/scale,y=(p.y-height/2-pan.y)/scale;let best=null,dist=14/scale;for(const n of nodes){const d=Math.hypot(n.x-x,n.y-y);if(d<dist){best=n;dist=d;}}if(best)select(best);}drag=null;};
canvas.onpointercancel=()=>{drag=null;};
canvas.addEventListener('wheel',e=>{e.preventDefault();const p=point(e),old=scale;scale=Math.max(.15,Math.min(8,scale*Math.exp(-e.deltaY*.001)));pan.x=p.x-width/2-(p.x-width/2-pan.x)*scale/old;pan.y=p.y-height/2-(p.y-height/2-pan.y)*scale/old;draw();},{passive:false});
$('reset').onclick=()=>{fitGraph();draw();};$('close').onclick=()=>{$('details').hidden=true;};$('subject').onchange=rebuild;$('limit').onchange=rebuild;let debounce;$('search').oninput=()=>{clearTimeout(debounce);debounce=setTimeout(rebuild,180);};window.onresize=resize;
fetch('/api/graph').then(r=>{if(!r.ok)throw Error();return r.json();}).then(d=>{data=d;for(const s of data.nodes.filter(n=>n.kind==='subject').sort((a,b)=>a.label.localeCompare(b.label))){const o=document.createElement('option');o.value=o.textContent=s.label;$('subject').append(o);}$('version').textContent=`Snapshot de Supabase · ${new Date(d.captured_at).toLocaleString()} · corpus ${d.corpus_id.slice(0,12)}`;resize();rebuild();}).catch(()=>{$('stats').textContent='No se pudo cargar el corpus. Reinicia el servidor local y comprueba la conexión.';});
