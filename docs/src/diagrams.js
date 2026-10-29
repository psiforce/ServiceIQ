// Generates the ServiceIQ process-flow and architecture diagrams as SVG.
// Usage: node diagrams.js <outDir>
const fs = require('fs'), path = require('path');
const OUT = process.argv[2] || path.join(__dirname, '..', 'diagrams', 'svg');
fs.mkdirSync(OUT, { recursive: true });

const FONT = 'Segoe UI, Arial, sans-serif';
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const ST = {
  task:   { f: '#E8F0FB', s: '#2F5B9A', t: '#14284B' },
  sys:    { f: '#E7F6EE', s: '#2E8B57', t: '#123D27' },
  human:  { f: '#FFF3E0', s: '#C77700', t: '#4A2C00' },
  dec:    { f: '#FFF8D6', s: '#B08900', t: '#3D3000' },
  term:   { f: '#1F3A5F', s: '#1F3A5F', t: '#FFFFFF' },
  alert:  { f: '#FDECEC', s: '#C0392B', t: '#5C1010' },
  ext:    { f: '#F1ECFA', s: '#6A4BA3', t: '#2E1A55' },
  store:  { f: '#EEF2F5', s: '#5D6D7E', t: '#1C2833' },
  future: { f: '#FFFFFF', s: '#7F8C8D', t: '#34495E', dash: true },
};

function text(x, y, str, o = {}) {
  const size = o.size || 14, lines = String(str).split('\n'), lh = size * 1.25;
  const y0 = y - (lines.length - 1) * lh / 2;
  return `<text x="${x}" y="${y0}" font-family="${FONT}" font-size="${size}" font-weight="${o.weight || 400}" fill="${o.color || '#1C2833'}" text-anchor="${o.anchor || 'middle'}" dominant-baseline="central"${o.italic ? ' font-style="italic"' : ''}>` +
    lines.map((l, i) => `<tspan x="${x}" dy="${i ? lh : 0}">${esc(l)}</tspan>`).join('') + '</text>';
}
function label(x, y, t) {
  const lines = t.split('\n');
  const w = Math.max(...lines.map(l => l.length)) * 6.4 + 12, h = lines.length * 15 + 5;
  return `<rect x="${x - w / 2}" y="${y - h / 2}" width="${w}" height="${h}" rx="3" fill="#FFFFFF" stroke="#D5DBDB" stroke-width="0.8"/>` +
    text(x, y, t, { size: 12, color: '#34495E', italic: true });
}
function svg(w, h, body) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">` +
    `<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#34495E"/></marker>` +
    `<marker id="ahb" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#7F8C8D"/></marker></defs>` +
    `<rect width="100%" height="100%" fill="#FFFFFF"/>${body}</svg>`;
}
function title(W, t, sub) {
  return text(W / 2, 22, t, { size: 19, weight: 700, color: '#1F3A5F' }) + (sub ? text(W / 2, 44, sub, { size: 12.5, color: '#5D6D7E' }) : '');
}
function shape(n) {
  const s = ST[n.style || 'task'], x = n.cx - n.w / 2, y = n.cy - n.h / 2;
  const dash = (s.dash || n.dash) ? ' stroke-dasharray="6 4"' : '';
  let el, ty = n.cy;
  if (n.shape === 'dec') el = `<polygon points="${n.cx},${y} ${x + n.w},${n.cy} ${n.cx},${y + n.h} ${x},${n.cy}" fill="${s.f}" stroke="${s.s}" stroke-width="1.6"/>`;
  else if (n.shape === 'term') el = `<rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="${Math.min(n.h / 2, 26)}" fill="${s.f}" stroke="${s.s}" stroke-width="1.6"/>`;
  else if (n.shape === 'db') {
    const ry = 9;
    el = `<path d="M${x},${y + ry} A${n.w / 2},${ry} 0 0 0 ${x + n.w},${y + ry} V${y + n.h - ry} A${n.w / 2},${ry} 0 0 1 ${x},${y + n.h - ry} Z" fill="${s.f}" stroke="${s.s}" stroke-width="1.6"${dash}/>` +
      `<ellipse cx="${n.cx}" cy="${y + ry}" rx="${n.w / 2}" ry="${ry}" fill="${s.f}" stroke="${s.s}" stroke-width="1.6"/>`;
    ty += ry / 2;
  } else el = `<rect x="${x}" y="${y}" width="${n.w}" height="${n.h}" rx="8" fill="${s.f}" stroke="${s.s}" stroke-width="1.6"${dash}/>`;
  const lines = String(n.text).split('\n');
  if (n.bold1 && lines.length > 1) {
    const size = n.size || 13.5, lh = size * 1.25, top = ty - (lines.length - 1) * lh / 2;
    return el + text(n.cx, top, lines[0], { size: size + 0.5, weight: 700, color: s.t }) +
      text(n.cx, top + lh * lines.length / 2, lines.slice(1).join('\n'), { size: size - 0.5, color: s.t });
  }
  return el + text(n.cx, ty, n.text, { size: n.size || 13.5, color: s.t, weight: (n.shape === 'term' || n.bold) ? 600 : 400 });
}
function edge(pts, o = {}) {
  const d = 'M' + pts.map(p => p.join(',')).join(' L');
  const p = `<path d="${d}" fill="none" stroke="${o.color || '#34495E'}" stroke-width="${o.sw || 1.5}"${o.dash ? ' stroke-dasharray="6 4"' : ''} marker-end="url(#ah)"${o.both ? ' marker-start="url(#ah)"' : ''}/>`;
  let lab = '';
  if (o.label) {
    const i = o.li ?? 0, a = pts[i], b = pts[i + 1];
    lab = label((a[0] + b[0]) / 2 + (o.lx || 0), (a[1] + b[1]) / 2 + (o.ly || 0), o.label);
  }
  return { p, lab };
}
const box = n => ({ l: n.cx - n.w / 2, r: n.cx + n.w / 2, t: n.cy - n.h / 2, b: n.cy + n.h / 2 });

// ---------- Swimlane (lanes are columns, flow runs top to bottom) ----------
function swimlane({ file, title: t, sub, lanes, nodes, edges, LW = 230, RH = 100, footer }) {
  const TOP = 62, HDR = 52;
  const nr = Math.max(...nodes.map(n => n.row)) + 1;
  const W = lanes.length * LW + 20, H = TOP + HDR + nr * RH + (footer ? 46 : 14);
  const m = {};
  nodes.forEach(n => {
    n.cx = 10 + n.lane * LW + LW / 2 + (n.dx || 0);
    n.cy = TOP + HDR + n.row * RH + RH / 2;
    const nl = String(n.text).split('\n').length;
    n.w = n.w || (n.shape === 'dec' ? LW - 40 : LW - 40);
    n.h = n.h || (n.shape === 'dec' ? 82 : n.shape === 'term' ? Math.max(44, nl * 17 + 14) : Math.max(58, nl * 17 + 12));
    n.style = n.style || (n.shape === 'dec' ? 'dec' : n.shape === 'term' ? 'term' : lanes[n.lane].style || 'task');
    m[n.id] = n;
  });
  let bg = '';
  lanes.forEach((ln, i) => {
    const x = 10 + i * LW;
    bg += `<rect x="${x}" y="${TOP}" width="${LW}" height="${HDR + nr * RH}" fill="${i % 2 ? '#FFFFFF' : '#F6F8FB'}" stroke="#C9D3DE" stroke-width="1"/>`;
    bg += `<rect x="${x}" y="${TOP}" width="${LW}" height="${HDR}" fill="#1F3A5F" stroke="#C9D3DE" stroke-width="1"/>`;
    bg += text(x + LW / 2, TOP + HDR / 2, ln.name, { size: 13.5, weight: 600, color: '#FFFFFF' });
  });
  let ps = '', labs = '';
  edges.forEach(([f, to, lab, o = {}]) => {
    const a = m[f], b = m[to], A = box(a), B = box(b);
    let pts, r = o.r || 'v';
    const sameLane = Math.abs(a.cx - b.cx) < 1, sameRow = Math.abs(a.cy - b.cy) < 1;
    if (r === 'v' && !sameLane && b.cy < a.cy) r = 'h';
    if (r === 'back') { const vx = o.x || Math.max(A.r, B.r) + 16; pts = [[A.r, a.cy], [vx, a.cy], [vx, b.cy], [B.r, b.cy]]; }
    else if (r === 'backL') { const vx = o.x || Math.min(A.l, B.l) - 16; pts = [[A.l, a.cy], [vx, a.cy], [vx, b.cy], [B.l, b.cy]]; }
    else if (sameRow) pts = b.cx > a.cx ? [[A.r, a.cy], [B.l, b.cy]] : [[A.l, a.cy], [B.r, b.cy]];
    else if (r === 'h') { const sx = b.cx > a.cx ? A.r : A.l; pts = [[sx, a.cy], [b.cx, a.cy], [b.cx, b.cy > a.cy ? B.t : B.b]]; }
    else if (sameLane) pts = b.cy > a.cy ? [[a.cx, A.b], [b.cx, B.t]] : [[a.cx, A.t], [b.cx, B.b]];
    else { const my = o.my || (A.b + B.t) / 2; pts = [[a.cx, A.b], [a.cx, my], [b.cx, my], [b.cx, B.t]]; }
    const blx = r === 'backL' ? 46 : r === 'back' ? -30 : 0;
    const e = edge(pts, { label: lab, li: o.li ?? (pts.length === 4 && r === 'v' ? 1 : 0), dash: o.dash, both: o.both, lx: o.lx ?? blx, ly: o.ly });
    ps += e.p; labs += e.lab;
  });
  const body = title(W, t, sub) + bg + ps + nodes.map(shape).join('') + labs +
    (footer ? text(W / 2, H - 24, footer, { size: 12.5, color: '#34495E', italic: true }) : '');
  fs.writeFileSync(path.join(OUT, file + '.svg'), svg(W, H, body));
}

// ---------- Free-layout architecture diagram ----------
function arch({ file, W, H, title: t, sub, groups = [], boxes = [], links = [], extra = '' }) {
  const m = {};
  boxes.forEach(b => { b.cx = b.x + b.w / 2; b.cy = b.y + b.h / 2; m[b.id] = b; });
  const side = (b, s, off = 0) => s === 'l' ? [b.x, b.cy + off] : s === 'r' ? [b.x + b.w, b.cy + off] : s === 't' ? [b.cx + off, b.y] : [b.cx + off, b.y + b.h];
  let g = '';
  groups.forEach(gr => {
    g += `<rect x="${gr.x}" y="${gr.y}" width="${gr.w}" height="${gr.h}" rx="10" fill="${gr.fill || '#F6F8FB'}" stroke="${gr.stroke || '#AEB6BF'}" stroke-width="1.3"${gr.dash ? ' stroke-dasharray="7 5"' : ''}/>`;
    g += gr.ta === 'end'
      ? text(gr.x + gr.w - 14, gr.y + 17, gr.title, { size: 13.5, weight: 700, color: gr.tc || '#1F3A5F', anchor: 'end' })
      : text(gr.x + 14, gr.y + 17, gr.title, { size: 13.5, weight: 700, color: gr.tc || '#1F3A5F', anchor: 'start' });
  });
  let ps = '', labs = '';
  links.forEach(([f, fs_, to, ts, lab, o = {}]) => {
    let pts;
    if (o.pts) pts = o.pts;
    else {
      const p1 = side(m[f], fs_, o.fo || 0), p2 = side(m[to], ts, o.to || 0);
      const h1 = 'lr'.includes(fs_), h2 = 'lr'.includes(ts);
      if (h1 && h2) { if (Math.abs(p1[1] - p2[1]) < 1) pts = [p1, p2]; else { const mx = o.mx || (p1[0] + p2[0]) / 2; pts = [p1, [mx, p1[1]], [mx, p2[1]], p2]; } }
      else if (!h1 && !h2) { if (Math.abs(p1[0] - p2[0]) < 1) pts = [p1, p2]; else { const my = o.my || (p1[1] + p2[1]) / 2; pts = [p1, [p1[0], my], [p2[0], my], p2]; } }
      else if (h1) pts = [p1, [p2[0], p1[1]], p2];
      else pts = [p1, [p1[0], p2[1]], p2];
    }
    const e = edge(pts, { label: lab, li: o.li ?? Math.floor((pts.length - 1) / 2), dash: o.dash, both: o.both, lx: o.lx, ly: o.ly, sw: o.sw });
    ps += e.p; labs += e.lab;
  });
  const body = title(W, t, sub) + g + ps + boxes.map(b => shape({ ...b, shape: b.shape || 'rect' })).join('') + labs + extra;
  fs.writeFileSync(path.join(OUT, file + '.svg'), svg(W, H, body));
}

// ---------- Sequence diagram ----------
function sequence({ file, title: t, sub, parts, steps, W = 1200 }) {
  const TOP = 62, PH = 56, colW = (W - 40) / parts.length, RH = 44;
  const px = {}; parts.forEach((p, i) => { px[p.id] = 20 + colW * i + colW / 2; });
  let y = TOP + PH + 30, body = '', n = 1;
  const rows = [];
  steps.forEach(s => {
    if (s.sep) {
      rows.push(`<rect x="24" y="${y - 14}" width="${W - 48}" height="28" rx="4" fill="#FFF8D6" stroke="#B08900" stroke-width="0.8"/>` + text(40, y, s.sep, { size: 12.5, color: '#3D3000', anchor: 'start', italic: true }));
      y += 38; return;
    }
    const lines = s.text.split('\n').length, x1 = px[s.f], x2 = px[s.t];
    const ly = y, ay = y + 8 + (lines - 1) * 8;
    const msg = `${n++}. ${s.text}`;
    if (s.f === s.t) {
      rows.push(`<path d="M${x1},${ay - 6} H${x1 + 34} V${ay + 16} H${x1 + 4}" fill="none" stroke="#34495E" stroke-width="1.5" marker-end="url(#ah)"/>`);
      rows.push(`<rect x="${x1 + 40}" y="${ay - 6 - lines * 8}" width="${Math.max(...msg.split('\n').map(l => l.length)) * 6.9 + 8}" height="${lines * 16 + 4}" fill="#FFFFFF" opacity="0.92"/>`);
      rows.push(text(x1 + 44, ay + 4, msg, { size: 12.5, anchor: 'start' }));
      y += RH + lines * 10 + 8;
    } else {
      rows.push(`<path d="M${x1},${ay} H${x2 + (x2 > x1 ? -2 : 2)}" fill="none" stroke="${s.dash ? '#5D6D7E' : '#34495E'}" stroke-width="1.5"${s.dash ? ' stroke-dasharray="6 4"' : ''} marker-end="url(#ah)"/>`);
      const mx = (x1 + x2) / 2, tw = Math.max(...msg.split('\n').map(l => l.length)) * 6.9 + 10;
      rows.push(`<rect x="${mx - tw / 2}" y="${ay - 8 - lines * 16}" width="${tw}" height="${lines * 16 + 2}" fill="#FFFFFF" opacity="0.92"/>`);
      rows.push(text(mx, ay - 8 - lines * 8, msg, { size: 12.5 }));
      y += RH + (lines - 1) * 14;
    }
  });
  const H = y + 10;
  parts.forEach(p => {
    const x = px[p.id], s = ST[p.style || 'task'];
    body += `<line x1="${x}" y1="${TOP + PH}" x2="${x}" y2="${H - 10}" stroke="#AEB6BF" stroke-width="1.2" stroke-dasharray="5 4"/>`;
    body += `<rect x="${x - colW / 2 + 8}" y="${TOP}" width="${colW - 16}" height="${PH}" rx="7" fill="${s.f}" stroke="${s.s}" stroke-width="1.6"/>` + text(x, TOP + PH / 2, p.label, { size: 13, weight: 600, color: s.t });
  });
  fs.writeFileSync(path.join(OUT, file + '.svg'), svg(W, H, title(W, t, sub) + body + rows.join('')));
}

// ---------- Entity diagram ----------
function entities({ file, W, H, title: t, sub, ents, rels }) {
  const m = {}; let body = '';
  ents.forEach(e => {
    e.h = 30 + e.fields.length * 17 + 10; e.cx = e.x + e.w / 2; e.cy = e.y + e.h / 2; m[e.id] = e;
    const s = ST[e.style || 'task'];
    body += `<rect x="${e.x}" y="${e.y}" width="${e.w}" height="${e.h}" rx="6" fill="#FFFFFF" stroke="${s.s}" stroke-width="1.5"${e.style === 'future' ? ' stroke-dasharray="6 4"' : ''}/>`;
    body += `<path d="M${e.x},${e.y + 6} a6,6 0 0 1 6,-6 h${e.w - 12} a6,6 0 0 1 6,6 v24 h-${e.w} z" fill="${s.f}" stroke="${s.s}" stroke-width="1.5"/>`;
    body += text(e.cx, e.y + 15, e.name, { size: 13.5, weight: 700, color: s.t });
    e.fields.forEach((f, i) => { body += text(e.x + 10, e.y + 40 + i * 17, f, { size: 12, anchor: 'start', color: f.includes(' PK') ? '#14284B' : '#34495E', weight: f.includes(' PK') ? 600 : 400 }); });
  });
  let ps = '', labs = '';
  const side = (b, s, off = 0) => s === 'l' ? [b.x, b.cy + off] : s === 'r' ? [b.x + b.w, b.cy + off] : s === 't' ? [b.cx + off, b.y] : [b.cx + off, b.y + b.h];
  rels.forEach(([f, fs_, to, ts, lab, o = {}]) => {
    let pts = o.pts;
    if (!pts) {
      const p1 = side(m[f], fs_, o.fo || 0), p2 = side(m[to], ts, o.to || 0);
      if ('lr'.includes(fs_) && 'lr'.includes(ts)) { const mx = o.mx || (p1[0] + p2[0]) / 2; pts = Math.abs(p1[1] - p2[1]) < 1 ? [p1, p2] : [p1, [mx, p1[1]], [mx, p2[1]], p2]; }
      else { const my = o.my || (p1[1] + p2[1]) / 2; pts = Math.abs(p1[0] - p2[0]) < 1 ? [p1, p2] : [p1, [p1[0], my], [p2[0], my], p2]; }
    }
    const e = edge(pts, { label: lab, li: o.li ?? Math.floor((pts.length - 1) / 2), dash: o.dash });
    ps += e.p; labs += e.lab;
  });
  fs.writeFileSync(path.join(OUT, file + '.svg'), svg(W, H, title(W, t, sub) + ps + body + labs));
}

// =====================================================================
// BP-1 Tenant onboarding
swimlane({
  file: 'bp1_tenant_onboarding', LW: 270,
  title: 'BP-1  Tenant Onboarding', sub: 'From signed pilot to a live, monitored chat widget',
  lanes: [{ name: 'HVAC Company\n(Tenant Owner / Admin)', style: 'human' }, { name: 'ServiceIQ\nPlatform Ops', style: 'task' }, { name: 'ServiceIQ Platform\n(automated)', style: 'sys' }],
  nodes: [
    { id: 's', lane: 0, row: 0, shape: 'term', text: 'Request pilot / sign up' },
    { id: 'a1', lane: 1, row: 1, text: 'Verify business; sign\nagreement & data-processing terms' },
    { id: 'a2', lane: 2, row: 2, text: 'Provision tenant: tenant_id,\nPinecone namespace, widget key,\nadmin account' },
    { id: 'a3', lane: 0, row: 3, text: 'Configure profile: service area,\nhours, brand, emergency &\nescalation contacts' },
    { id: 'a4', lane: 0, row: 4, text: 'Invite users & assign roles\n(Knowledge Admin, CSR, Viewer)' },
    { id: 'a5', lane: 0, row: 5, text: 'Upload & approve initial\nknowledge corpus (BP-2)' },
    { id: 'a6', lane: 2, row: 6, text: 'Bulk ingest; run baseline\nevaluation suite for tenant' },
    { id: 'd1', lane: 1, row: 7, shape: 'dec', text: 'Meets acceptance\nthresholds?' },
    { id: 'a7', lane: 0, row: 8, text: 'Embed chat-widget script on\nwebsite (allow-listed domain)' },
    { id: 'e', lane: 2, row: 9, shape: 'term', text: 'Tenant live;\nmonitoring active' },
  ],
  edges: [['s', 'a1'], ['a1', 'a2'], ['a2', 'a3'], ['a3', 'a4'], ['a4', 'a5'], ['a5', 'a6'], ['a6', 'd1'],
    ['d1', 'a7', 'Yes'], ['d1', 'a5', 'No: close content\ngaps / tune config', { r: 'backL', li: 1 }], ['a7', 'e']],
});

// BP-2 Knowledge lifecycle
swimlane({
  file: 'bp2_knowledge_lifecycle',
  title: 'BP-2  Knowledge Lifecycle & Freshness', sub: 'Author → approve → ingest → serve → retire',
  lanes: [{ name: 'Knowledge Admin', style: 'human' }, { name: 'Admin Console /\nDocument Registry', style: 'task' }, { name: 'Ingestion Pipeline', style: 'sys' }, { name: 'Scheduler &\nMonitoring', style: 'task' }],
  nodes: [
    { id: 's', lane: 0, row: 0, shape: 'term', text: 'New / changed content\n(price, policy, FAQ)' },
    { id: 'a1', lane: 0, row: 1, text: 'Upload file or register URL;\nset type, authority, effective\n& expiry dates' },
    { id: 'a2', lane: 1, row: 2, text: 'Store immutable draft\nversion; status = pending' },
    { id: 'd1', lane: 0, row: 3, shape: 'dec', text: 'Approve as\nauthoritative?' },
    { id: 'sch', lane: 3, row: 3, style: 'sys', text: 'Daily website crawl;\nhourly expiry sweep' },
    { id: 'a3', lane: 1, row: 4, text: 'Publish version; emit\ndocument.approved event' },
    { id: 'a4', lane: 2, row: 5, text: 'Load → clean → structure-\naware chunk (~500 tokens,\n75 overlap)' },
    { id: 'a5', lane: 2, row: 6, text: 'Embed (text-embedding-\n3-small); upsert to tenant\nnamespace with metadata' },
    { id: 'a6', lane: 2, row: 7, text: 'Retire superseded / expired\nvectors; update registry' },
    { id: 'd2', lane: 2, row: 8, shape: 'dec', text: 'Succeeded within\nfreshness SLA?' },
    { id: 'a7', lane: 3, row: 9, style: 'alert', text: 'Alert admin & ops;\nretry with backoff;\nquarantine bad file' },
    { id: 'e', lane: 1, row: 9, shape: 'term', text: 'Version live\nfor retrieval' },
  ],
  edges: [['s', 'a1'], ['a1', 'a2'], ['a2', 'd1'], ['d1', 'a1', 'No: revise', { r: 'backL', li: 1 }], ['d1', 'a3', 'Yes'],
    ['sch', 'a4', 'scheduled\nchanges'], ['a3', 'a4'], ['a4', 'a5'], ['a5', 'a6'], ['a6', 'd2'],
    ['d2', 'e', 'Yes', { r: 'h' }], ['d2', 'a7', 'No', { r: 'h' }], ['a7', 'a4', 'retry', { r: 'back', li: 1 }]],
  footer: 'Freshness SLA: pricing, policy & warranty changes ≤ 15 min after approval; general knowledge ≤ 24 h.',
});

// BP-3 Customer Q&A
swimlane({
  file: 'bp3_customer_qa',
  title: 'BP-3  Customer Question Answering (MVP)', sub: 'Every answer is grounded and cited, or the system abstains / escalates',
  lanes: [{ name: 'Customer', style: 'human' }, { name: 'Chat Widget', style: 'task' }, { name: 'ServiceIQ RAG Service', style: 'sys' }, { name: 'Human Team (CSR)', style: 'human' }],
  nodes: [
    { id: 's', lane: 0, row: 0, shape: 'term', text: 'Asks a question in\nnatural language' },
    { id: 'a1', lane: 1, row: 1, text: 'Send message with\nsession & widget key' },
    { id: 'a2', lane: 2, row: 2, text: 'Resolve tenant; input\nguardrails (PII, abuse,\nprompt injection)' },
    { id: 'd1', lane: 2, row: 3, shape: 'dec', text: 'Emergency?\n(gas, CO, fire)' },
    { id: 'em', lane: 1, row: 3, style: 'alert', text: 'Safety guidance first;\nnotify on-call (BP-4)' },
    { id: 'a3', lane: 2, row: 4, text: 'Retrieve top_k = 10 with\ntenant, approval & date\nfilters' },
    { id: 'a4', lane: 2, row: 5, text: 'Rerank → top 5; resolve\nconflicts by authority &\neffective date' },
    { id: 'd2', lane: 2, row: 6, shape: 'dec', text: 'Sufficient &\nconsistent evidence?' },
    { id: 'esc', lane: 3, row: 6, text: 'Escalate with context\n(BP-4); customer told\nhow & when' },
    { id: 'a5', lane: 2, row: 7, text: 'Generate concise answer\nwith citations' },
    { id: 'd3', lane: 2, row: 8, shape: 'dec', text: 'Grounding &\ncitation check pass?' },
    { id: 'fb', lane: 1, row: 8, style: 'alert', text: 'Safe fallback: cannot\nconfirm + offer human\ncontact' },
    { id: 'a6', lane: 1, row: 9, text: 'Display answer / message\nwith source citations' },
    { id: 'a7', lane: 0, row: 10, text: 'Reads answer, opens\ncitations, rates it' },
    { id: 'e', lane: 0, row: 11, shape: 'term', text: 'Resolved or\nnext question' },
  ],
  edges: [['s', 'a1'], ['a1', 'a2'], ['a2', 'd1'], ['d1', 'em', 'Yes'], ['d1', 'a3', 'No'], ['a3', 'a4'], ['a4', 'd2'],
    ['d2', 'esc', 'No'], ['d2', 'a5', 'Yes'], ['a5', 'd3'], ['d3', 'fb', 'No'], ['d3', 'a6', 'Yes'], ['fb', 'a6'],
    ['a6', 'a7'], ['a7', 'e']],
});

// BP-4 Human escalation
swimlane({
  file: 'bp4_escalation',
  title: 'BP-4  Human Escalation & Handoff', sub: 'Triggers: emergency · insufficient or conflicting evidence · binding quote · licensed-technician need · customer request',
  lanes: [{ name: 'Customer', style: 'human' }, { name: 'ServiceIQ', style: 'sys' }, { name: 'CSR / Dispatcher', style: 'human' }, { name: 'Technician /\nService Manager', style: 'human' }],
  nodes: [
    { id: 's', lane: 1, row: 0, shape: 'term', text: 'Escalation trigger\ndetected' },
    { id: 'a1', lane: 1, row: 1, text: 'Classify trigger &\nassign priority (P1–P3)' },
    { id: 'd1', lane: 1, row: 2, shape: 'dec', text: 'Emergency (P1)?' },
    { id: 'saf', lane: 0, row: 2, style: 'alert', text: 'Sees safety steps &\nemergency numbers' },
    { id: 'pg', lane: 2, row: 2, style: 'alert', text: 'On-call paged\n(SMS / phone)' },
    { id: 'a2', lane: 1, row: 3, text: 'Build handoff packet:\ntranscript, intent,\ncitations, gaps' },
    { id: 'a3', lane: 0, row: 4, text: 'Consents; gives name,\nphone, email, address' },
    { id: 'a4', lane: 1, row: 5, text: 'Create handoff ticket;\nnotify CSR queue;\ngive customer reference' },
    { id: 'a5', lane: 2, row: 6, text: 'Review packet; contact\ncustomer within\nresponse SLA' },
    { id: 'd2', lane: 2, row: 7, shape: 'dec', text: 'Needs technician\nor manager?' },
    { id: 't1', lane: 3, row: 7, text: 'Diagnose; issue binding\nquote or warranty\ndecision' },
    { id: 'a6', lane: 2, row: 8, text: 'Resolve: answer,\nbook visit manually' },
    { id: 'a7', lane: 1, row: 9, text: 'Record outcome; flag\nknowledge gap (BP-5)' },
    { id: 'e', lane: 0, row: 10, shape: 'term', text: 'Customer issue\nresolved' },
  ],
  edges: [['s', 'a1'], ['a1', 'd1'], ['d1', 'saf', 'Yes'], ['d1', 'pg', 'Yes'], ['d1', 'a2', 'No'], ['saf', 'a3'], ['a2', 'a3'], ['a3', 'a4'],
    ['pg', 'a5'], ['a4', 'a5'], ['a5', 'd2'], ['d2', 't1', 'Yes'], ['d2', 'a6', 'No'], ['t1', 'a6'], ['a6', 'a7'], ['a7', 'e']],
});

// BP-5 Feedback & evaluation
swimlane({
  file: 'bp5_feedback_eval',
  title: 'BP-5  Feedback & Continuous Evaluation', sub: 'Production signals feed the evaluation set; release gates block regressions',
  lanes: [{ name: 'Customer / CSR /\nKnowledge Admin', style: 'human' }, { name: 'Observability\n(LangSmith)', style: 'sys' }, { name: 'AI / Evaluation\nEngineer', style: 'human' }, { name: 'CI/CD & Eval Harness\n(Ragas)', style: 'sys' }],
  nodes: [
    { id: 's', lane: 1, row: 0, shape: 'term', text: 'Every conversation traced\n(PII-redacted)' },
    { id: 'a0', lane: 0, row: 1, text: 'Rate answer (up/down);\nCSR flags bad answers' },
    { id: 'a1', lane: 1, row: 2, text: 'Auto-flag: negative rating,\nlow grounding, fallback,\nlatency > 5 s' },
    { id: 'a2', lane: 2, row: 3, text: 'Triage flagged\nconversations (weekly)' },
    { id: 'd1', lane: 2, row: 4, shape: 'dec', text: 'Root cause?' },
    { id: 'kg', lane: 0, row: 4, text: 'Add / fix content\nvia BP-2' },
    { id: 'a3', lane: 2, row: 5, text: 'Add case to eval set:\nquestion, reference answer,\nrelevant source IDs' },
    { id: 'a4', lane: 2, row: 6, text: 'Change chunking, top_k,\nreranker, prompt or model' },
    { id: 'a5', lane: 3, row: 7, text: 'Run regression suite\n(≥100 questions): Ragas +\npricing/policy checks' },
    { id: 'd2', lane: 3, row: 8, shape: 'dec', text: 'All release\ngates met?' },
    { id: 'a6', lane: 3, row: 9, text: 'Deploy staging → prod;\nrecord new baseline' },
    { id: 'e', lane: 1, row: 10, shape: 'term', text: 'Dashboards & KPI\nreport updated' },
  ],
  edges: [['s', 'a1'], ['a0', 'a1'], ['a1', 'a2'], ['a2', 'd1'], ['d1', 'kg', 'Knowledge gap'], ['d1', 'a3', 'Retrieval /\ngeneration defect'],
    ['a3', 'a4'], ['a4', 'a5'], ['kg', 'a5', 're-test', { my: 814, li: 1, lx: -250 }], ['a5', 'd2'], ['d2', 'a4', 'No', { r: 'backL', li: 1 }], ['d2', 'a6', 'Yes'], ['a6', 'e']],
  footer: 'Gates: faithfulness ≥95% · Recall@5 ≥90% · citation accuracy ≥95% · unsupported-answer handling ≥95% · 0 critical pricing/policy errors · P95 ≤5 s',
});

// BP-6 Service intake (Phase 2)
swimlane({
  file: 'bp6_service_intake',
  title: 'BP-6  AI Service Intake (Phase 2)', sub: 'Intent → qualification → structured service request → human review',
  lanes: [{ name: 'Customer', style: 'human' }, { name: 'Intake Agent\n(LangGraph)', style: 'sys' }, { name: 'RAG Core\n(read-only tool)', style: 'sys' }, { name: 'CSR / Dispatcher', style: 'human' }],
  nodes: [
    { id: 's', lane: 0, row: 0, shape: 'term', text: 'Describes a problem or\nasks for service' },
    { id: 'a1', lane: 1, row: 1, text: 'Detect intent: question,\nservice request,\nemergency, other' },
    { id: 'd1', lane: 1, row: 2, shape: 'dec', text: 'Intent?' },
    { id: 'q', lane: 2, row: 2, text: 'Answer via BP-3\n(grounded + cited)' },
    { id: 'em', lane: 0, row: 2, style: 'alert', text: 'Safety script +\nP1 escalation (BP-4)' },
    { id: 'a2', lane: 1, row: 3, text: 'Check service area &\neligibility (ZIP, services,\nhours)' },
    { id: 'd2', lane: 1, row: 4, shape: 'dec', text: 'Eligible?' },
    { id: 'no', lane: 0, row: 4, text: 'Polite decline +\ncallback option' },
    { id: 'a3', lane: 1, row: 5, text: 'Collect: equipment brand,\nmodel & age, symptoms,\nurgency, address, contact' },
    { id: 'c1', lane: 0, row: 5, text: 'Answers follow-up\nquestions; adds photos' },
    { id: 'a4', lane: 1, row: 6, text: 'Summarize details &\nask for confirmation' },
    { id: 'd3', lane: 0, row: 7, shape: 'dec', text: 'Details\nconfirmed?' },
    { id: 'a5', lane: 1, row: 8, text: 'Create structured service\nrequest (JSON schema);\npriority score' },
    { id: 'a6', lane: 3, row: 9, text: 'Review request; contact\ncustomer; book in\nscheduling tool' },
    { id: 'e', lane: 0, row: 10, shape: 'term', text: 'Confirmation\nreceived' },
  ],
  edges: [['s', 'a1'], ['a1', 'd1'], ['d1', 'q', 'Question'], ['d1', 'em', 'Emergency'], ['d1', 'a2', 'Service request'],
    ['a2', 'q', 'policy lookup', { r: 'h', dash: true }], ['a2', 'd2'], ['d2', 'no', 'No'], ['d2', 'a3', 'Yes'], ['a3', 'c1', 'Q&A turns', { both: true }],
    ['a3', 'a4'], ['a4', 'd3'], ['d3', 'a3', 'Edit', { r: 'back', li: 1 }], ['d3', 'a5', 'Yes'], ['a5', 'a6'], ['a6', 'e']],
});

// BP-7 Agentic operations (Phase 3)
swimlane({
  file: 'bp7_agentic_ops',
  title: 'BP-7  Agentic Service Operations (Phase 3)', sub: 'Agent orchestrates CRM, scheduling & work orders under human-approval policies',
  lanes: [{ name: 'Customer', style: 'human' }, { name: 'Ops Agent\n(LangGraph)', style: 'sys' }, { name: 'CRM / Scheduling /\nWork-Order Systems', style: 'ext' }, { name: 'Dispatcher /\nService Manager', style: 'human' }],
  nodes: [
    { id: 's', lane: 1, row: 0, shape: 'term', text: 'Service request\nfrom BP-6' },
    { id: 'a1', lane: 2, row: 1, text: 'CRM lookup: customer,\nequipment history,\nwarranty, plan' },
    { id: 'a2', lane: 1, row: 2, text: 'Enrich & prioritize:\nurgency, SLA, plan,\nwarranty status' },
    { id: 'a3', lane: 2, row: 3, text: 'Technician availability\nby skill & territory' },
    { id: 'a4', lane: 1, row: 4, text: 'Propose 2–3 slots; show\nprice range from approved\nprice book' },
    { id: 'd1', lane: 1, row: 5, shape: 'dec', text: 'Within auto-\napproval policy?' },
    { id: 'ap', lane: 3, row: 5, text: 'Approve / adjust\nproposal' },
    { id: 'a5', lane: 0, row: 6, text: 'Selects slot;\naccepts terms' },
    { id: 'a6', lane: 2, row: 7, text: 'Book appointment; create\nwork order (idempotent\nrequest key)' },
    { id: 'mon', lane: 3, row: 7, text: 'Monitor agent actions;\noverride / reassign' },
    { id: 'a7', lane: 2, row: 8, text: 'Send confirmation &\nreminders; sync CRM' },
    { id: 'a8', lane: 0, row: 9, text: 'Gets updates; can\nreschedule / cancel\nin chat' },
    { id: 'a9', lane: 1, row: 10, text: 'Post-visit: survey,\nclose loop in CRM' },
    { id: 'e', lane: 3, row: 11, shape: 'term', text: 'KPIs: conversion,\ntime-to-book, CSAT' },
  ],
  edges: [['s', 'a1'], ['a1', 'a2'], ['a2', 'a3'], ['a3', 'a4'], ['a4', 'd1'], ['d1', 'ap', 'No: binding quote,\nafter-hours, exception', { r: 'h' }], ['d1', 'a5', 'Yes'],
    ['ap', 'a5', '', { my: 720 }], ['a5', 'a6'], ['a6', 'mon', 'audit log', { dash: true }], ['a6', 'a7'], ['a7', 'a8'], ['a8', 'a9'], ['a9', 'e']],
});

// ---------- Context diagram ----------
arch({
  file: 'arch1_context', W: 1180, H: 640,
  title: 'System Context (C4 Level 1)', sub: 'ServiceIQ multi-tenant platform, its users and external dependencies',
  boxes: [
    { id: 'web', x: 430, y: 70, w: 300, h: 66, style: 'ext', text: 'HVAC Company Website\nhosts the chat widget', bold1: true },
    { id: 'siq', x: 430, y: 235, w: 300, h: 150, style: 'term', text: 'ServiceIQ Platform\nMulti-tenant AI knowledge\nassistant & service-intake\n(SaaS, containerized)', bold1: true },
    { id: 'ops', x: 430, y: 500, w: 300, h: 66, style: 'human', text: 'ServiceIQ Platform Ops &\nAI / Evaluation Engineers' },
    { id: 'cust', x: 30, y: 70, w: 230, h: 66, style: 'human', text: 'Customer\nhomeowner / business', bold1: true },
    { id: 'adm', x: 30, y: 250, w: 230, h: 66, style: 'human', text: 'Knowledge Admin\ntenant staff', bold1: true },
    { id: 'csr', x: 30, y: 400, w: 230, h: 66, style: 'human', text: 'CSR / Dispatcher /\nTechnician', bold1: true },
    { id: 'oai', x: 910, y: 70, w: 240, h: 62, style: 'ext', text: 'OpenAI API\nembeddings + LLM', bold1: true },
    { id: 'pc', x: 910, y: 160, w: 240, h: 62, style: 'ext', text: 'Pinecone\nvector database', bold1: true },
    { id: 'ls', x: 910, y: 250, w: 240, h: 62, style: 'ext', text: 'LangSmith\ntracing & evaluation', bold1: true },
    { id: 'nt', x: 910, y: 340, w: 240, h: 62, style: 'ext', text: 'Email / SMS / Slack\nnotification providers', bold1: true },
    { id: 'crm', x: 910, y: 450, w: 240, h: 74, style: 'future', text: 'CRM · Scheduling ·\nWork orders\n(Phase 3)', bold1: true },
  ],
  links: [
    ['cust', 'r', 'web', 'l', 'asks questions'],
    ['web', 'b', 'siq', 't', 'HTTPS chat API'],
    ['adm', 'r', 'siq', 'l', 'upload & approve\nknowledge', { to: -35 }],
    ['siq', 'l', 'csr', 'r', 'handoff packets,\nalerts', { fo: 40, mx: 345 }],
    ['ops', 't', 'siq', 'b', 'operate, deploy, evaluate'],
    ['siq', 'r', 'oai', 'l', 'embed / generate', { fo: -55, mx: 770, li: 2 }],
    ['siq', 'r', 'pc', 'l', 'vector search', { fo: -28, mx: 800, li: 2 }],
    ['siq', 'r', 'ls', 'l', 'traces, evals', { fo: 0, li: 0, lx: 0 }],
    ['siq', 'r', 'nt', 'l', 'notifications', { fo: 28, mx: 800, li: 2 }],
    ['siq', 'r', 'crm', 'l', 'tool calls', { fo: 55, mx: 770, li: 2, dash: true }],
  ],
});

// ---------- Container diagram ----------
const AW = 270, AH = 86;
arch({
  file: 'arch2_containers', W: 1200, H: 830,
  title: 'Logical / Container Architecture (C4 Level 2)', sub: 'Cloud-agnostic containers; tenant isolation is enforced in the API layer before any retrieval',
  groups: [
    { x: 20, y: 60, w: 900, h: 110, title: 'Channels' },
    { x: 20, y: 192, w: 900, h: 88, title: 'Edge' },
    { x: 20, y: 302, w: 900, h: 350, title: 'Application services (Python, FastAPI, LangChain / LangGraph)' },
    { x: 20, y: 674, w: 900, h: 140, title: 'Data stores' },
    { x: 945, y: 60, w: 235, h: 754, title: 'External services', fill: '#FAF8FD', stroke: '#B9A7D9' },
  ],
  boxes: [
    { id: 'w', x: 40, y: 92, w: 200, h: 62, text: 'Chat Widget\n(JS embed per tenant)', bold1: true },
    { id: 'st', x: 260, y: 92, w: 200, h: 62, text: 'Streamlit UI\n(MVP demo / pilot)', bold1: true },
    { id: 'ac', x: 480, y: 92, w: 200, h: 62, text: 'Admin Console\n(knowledge & users)', bold1: true },
    { id: 'ci', x: 700, y: 92, w: 200, h: 62, text: 'CSR Handoff Inbox\n(+ email / Slack)', bold1: true },
    { id: 'gw', x: 40, y: 218, w: 860, h: 50, text: 'API Gateway / Reverse Proxy (Traefik or NGINX): TLS · widget key & JWT auth → tenant_id · rate limits · CORS allow-list', size: 13 },
    { id: 'chat', x: 40, y: 334, w: AW, h: AH, style: 'sys', text: 'Chat / RAG Service\nguardrails · retrieve · rerank ·\ngenerate · cite · fallback', bold1: true },
    { id: 'esc', x: 335, y: 334, w: AW, h: AH, style: 'sys', text: 'Escalation & Notification\nhandoff tickets · paging ·\nemail / SMS / Slack', bold1: true },
    { id: 'adm', x: 630, y: 334, w: AW, h: AH, style: 'sys', text: 'Admin & Knowledge API\ndocuments · versions ·\napprovals · users · tenants', bold1: true },
    { id: 'ev', x: 40, y: 440, w: AW, h: AH, style: 'sys', text: 'Evaluation Runner\nRagas + LangSmith datasets;\nCI release gates', bold1: true },
    { id: 'ing', x: 335, y: 440, w: AW, h: AH, style: 'sys', text: 'Ingestion Workers\nload · clean · chunk ·\nembed · upsert · retire', bold1: true },
    { id: 'q', x: 630, y: 440, w: AW, h: AH, style: 'sys', text: 'Job Queue & Scheduler\nevents, retries, daily crawl,\nexpiry sweep', bold1: true },
    { id: 'ag', x: 40, y: 546, w: AW, h: AH, style: 'future', text: 'Agent Service (Phase 2–3)\nLangGraph intake & ops;\ncalls RAG core as a tool', bold1: true },
    { id: 'fa', x: 335, y: 546, w: AW, h: AH, style: 'sys', text: 'Feedback & Analytics\nratings · flags · KPIs ·\nconversation logs', bold1: true },
    { id: 'tn', x: 630, y: 546, w: AW, h: AH, style: 'sys', text: 'Tenant & Identity\ntenants · roles (RBAC) ·\nAPI keys · SSO (later)', bold1: true },
    { id: 'pc', x: 40, y: 704, w: 200, h: 96, shape: 'db', style: 'store', text: 'Pinecone\nnamespace per tenant;\nvectors + metadata', bold1: true },
    { id: 'pg', x: 260, y: 704, w: 200, h: 96, shape: 'db', style: 'store', text: 'PostgreSQL\nregistry, conversations,\ntickets, eval results', bold1: true },
    { id: 'ob', x: 480, y: 704, w: 200, h: 96, shape: 'db', style: 'store', text: 'Object Storage\n(S3-compatible / MinIO)\nraw versioned files', bold1: true },
    { id: 'rd', x: 700, y: 704, w: 200, h: 96, shape: 'db', style: 'store', text: 'Redis\ncache, sessions,\nqueue broker', bold1: true },
    { id: 'x1', x: 960, y: 100, w: 205, h: 74, style: 'ext', text: 'OpenAI API\ntext-embedding-3-small;\nchat model', bold1: true },
    { id: 'x2', x: 960, y: 220, w: 205, h: 74, style: 'ext', text: 'LangSmith\ntraces, datasets,\nexperiments', bold1: true },
    { id: 'x3', x: 960, y: 340, w: 205, h: 74, style: 'ext', text: 'Email / SMS / Slack\nproviders', bold1: true },
    { id: 'x4', x: 960, y: 460, w: 205, h: 74, style: 'ext', text: 'Tenant websites\n(crawl sources)', bold1: true },
    { id: 'x5', x: 960, y: 580, w: 205, h: 74, style: 'future', text: 'CRM · Scheduling ·\nWork orders (Phase 3)', bold1: true },
    { id: 'x6', x: 960, y: 700, w: 205, h: 74, style: 'ext', text: 'Secrets manager\n(Vault or cloud KMS)', bold1: true },
  ],
  links: [
    ['chat', 'r', 'esc', 'l', 'handoff'],
    ['adm', 'b', 'q', 't', 'doc.approved'],
    ['q', 'l', 'ing', 'r', 'jobs'],
    ['ev', 't', 'chat', 'b', 'replay eval set'],
  ],
  extra: [[690, 154, 690, 216], [617, 268, 617, 300], [617, 652, 617, 672], [920, 480, 943, 480]]
    .map(([x1, y1, x2, y2]) => `<path d="M${x1},${y1} L${x2},${y2}" stroke="#7F8C8D" stroke-width="5" marker-end="url(#ahb)"/>`).join(''),
});

// ---------- RAG query sequence ----------
sequence({
  file: 'arch3_rag_sequence', W: 1200,
  title: 'RAG Query Pipeline (runtime sequence)', sub: 'Budget: P95 ≤ 5 s end-to-end; answer streams to the widget',
  parts: [
    { id: 'w', label: 'Chat Widget', style: 'human' }, { id: 'gw', label: 'API Gateway' }, { id: 'r', label: 'RAG Service', style: 'sys' },
    { id: 'g', label: 'Guardrails', style: 'sys' }, { id: 'o', label: 'OpenAI\n(embed / LLM)', style: 'ext' }, { id: 'p', label: 'Pinecone', style: 'ext' },
    { id: 'rr', label: 'Reranker', style: 'sys' }, { id: 'db', label: 'Postgres /\nLangSmith', style: 'store' },
  ],
  steps: [
    { f: 'w', t: 'gw', text: 'POST /v1/chat (widget key, session)' },
    { f: 'gw', t: 'r', text: 'authenticated request + tenant_id' },
    { f: 'r', t: 'g', text: 'input checks: PII, injection, emergency' },
    { sep: 'alt  emergency detected → return safety script, page on-call (BP-4), stop' },
    { f: 'r', t: 'o', text: 'embed query (text-embedding-3-small)' },
    { f: 'r', t: 'p', text: 'query top_k=10 in tenant namespace;\nfilter: approved, effective ≤ now < expiry, access_scope' },
    { f: 'p', t: 'r', text: '10 candidate chunks + metadata', dash: true },
    { f: 'r', t: 'rr', text: 'rerank candidates' },
    { f: 'rr', t: 'r', text: 'top 5 evidence chunks', dash: true },
    { f: 'r', t: 'r', text: 'resolve conflicts (authority, effective date); sufficiency check' },
    { sep: 'alt  insufficient or conflicting evidence → fallback message + escalation offer (BP-4)' },
    { f: 'r', t: 'o', text: 'generate: rules + evidence (as untrusted data) + question' },
    { f: 'o', t: 'r', text: 'answer with [n] citation markers', dash: true },
    { f: 'r', t: 'g', text: 'output check: each claim cited; no unsupported price/warranty' },
    { f: 'r', t: 'db', text: 'log trace, citations, tokens, latency' },
    { f: 'r', t: 'w', text: 'answer + citations + status (answered | fallback | escalated)', dash: true },
  ],
});

// ---------- Ingestion pipeline ----------
const SX = [250, 475, 700, 925], SW = 200;
arch({
  file: 'arch4_ingestion', W: 1150, H: 720,
  title: 'Knowledge Ingestion Pipeline', sub: 'Event-driven (approval) and scheduled (daily crawl, expiry sweep); idempotent by document:version:chunk',
  groups: [{ x: 10, y: 60, w: 215, h: 420, title: 'Sources & triggers', fill: '#FFF8EE', stroke: '#E0B97A' }],
  boxes: [
    { id: 's1', x: 25, y: 92, w: 185, h: 70, style: 'human', text: 'Admin uploads\nPDF · DOCX · CSV', bold1: true },
    { id: 's2', x: 25, y: 182, w: 185, h: 70, style: 'human', text: 'Website pages\n(daily crawl)', bold1: true },
    { id: 's3', x: 25, y: 272, w: 185, h: 70, style: 'human', text: 'FAQs & price lists\n(HTML / CSV)', bold1: true },
    { id: 's4', x: 25, y: 362, w: 185, h: 96, style: 'task', text: 'Triggers\ndocument.approved ·\nschedule · expiry ·\nmanual reindex', bold1: true },
    { id: 'p1', x: SX[0], y: 110, w: SW, h: 120, style: 'sys', text: '1  Load\nLangChain loaders:\nweb, PDF, DOCX, CSV;\nfetch raw from storage', bold1: true },
    { id: 'p2', x: SX[1], y: 110, w: SW, h: 120, style: 'sys', text: '2  Clean\nstrip nav & boilerplate;\ndedupe (content hash);\nkeep headings & tables', bold1: true },
    { id: 'p3', x: SX[2], y: 110, w: SW, h: 120, style: 'sys', text: '3  Enrich metadata\ntenant, doc, type, version,\neffective / expiry,\napproval, access_scope', bold1: true },
    { id: 'p4', x: SX[3], y: 110, w: SW, h: 120, style: 'sys', text: '4  Chunk\nstructure-aware ~500 tok,\n75 overlap; whole FAQs\n& policy clauses', bold1: true },
    { id: 'p5', x: SX[0], y: 330, w: SW, h: 120, style: 'sys', text: '5  Embed\ntext-embedding-3-small;\nbatched, retried,\ncost metered', bold1: true },
    { id: 'p6', x: SX[1], y: 330, w: SW, h: 120, style: 'sys', text: '6  Upsert\nnamespace = tenant;\nid = doc:version:chunk', bold1: true },
    { id: 'p7', x: SX[2], y: 330, w: SW, h: 120, style: 'sys', text: '7  Activate & retire\nswitch current version;\ndelete superseded\nvectors', bold1: true },
    { id: 'p8', x: SX[3], y: 330, w: SW, h: 120, style: 'sys', text: '8  Validate\nchunk counts; smoke\nretrieval test;\nSLA timer stop', bold1: true },
    { id: 'd1', x: SX[0], y: 540, w: SW, h: 80, style: 'ext', text: 'OpenAI\nEmbeddings API', bold1: true },
    { id: 'd2', x: SX[1], y: 530, w: SW, h: 96, shape: 'db', style: 'store', text: 'Pinecone\nvectors + metadata', bold1: true },
    { id: 'd3', x: SX[2], y: 530, w: SW, h: 96, shape: 'db', style: 'store', text: 'PostgreSQL\ndocument registry,\nversions, job log', bold1: true },
    { id: 'd4', x: SX[3], y: 540, w: SW, h: 80, style: 'alert', text: 'Alerts\nfailures, SLA breach,\nquarantine', bold1: true },
  ],
  links: [
    ['p1', 'r', 'p2', 'l'], ['p2', 'r', 'p3', 'l'], ['p3', 'r', 'p4', 'l'],
    ['p4', 'b', 'p5', 't', '', { pts: [[1025, 230], [1025, 285], [350, 285], [350, 328]] }],
    ['p5', 'r', 'p6', 'l'], ['p6', 'r', 'p7', 'l'], ['p7', 'r', 'p8', 'l'],
    ['p5', 'b', 'd1', 't', 'embed batch', { both: true }], ['p6', 'b', 'd2', 't', 'upsert'], ['p7', 'b', 'd3', 't', 'version state'], ['p8', 'b', 'd4', 't', 'on failure', { dash: true }],
  ],
  extra: `<path d="M225,170 L248,170" stroke="#34495E" stroke-width="1.5" marker-end="url(#ah)"/>` +
    text(575, 678, 'Freshness SLA: pricing / policy / warranty ≤ 15 min after approval · general knowledge ≤ 24 h · new version is activated only after all its vectors are upserted (no retrieval gap)', { size: 12.5, italic: true, color: '#34495E' }),
});

// ---------- Data model ----------
entities({
  file: 'arch5_data_model', W: 1200, H: 790,
  title: 'Logical Data Model', sub: 'Every tenant-owned record carries tenant_id; Chunk lives in Pinecone, all others in PostgreSQL',
  ents: [
    { id: 'tn', name: 'Tenant', x: 20, y: 70, w: 260, fields: ['tenant_id PK', 'name, plan, status', 'service_area (ZIPs)', 'business_hours', 'escalation_contacts', 'widget_key, allowed_domains', 'pinecone_namespace'] },
    { id: 'us', name: 'User', x: 20, y: 280, w: 260, fields: ['user_id PK', 'tenant_id FK', 'role: owner | knowledge_admin |', '  csr | viewer', 'email, mfa_enabled'] },
    { id: 'ec', name: 'EvalCase', x: 20, y: 440, w: 260, fields: ['case_id PK', 'tenant_id FK', 'question, reference_answer', 'relevant_document_ids[]', 'category (price, warranty, …)'] },
    { id: 'er', name: 'EvalRun', x: 20, y: 600, w: 260, fields: ['run_id PK', 'git_sha, config snapshot', 'faithfulness, recall_at_5', 'citation_accuracy, p95_latency', 'passed_gates (bool)'] },
    { id: 'dc', name: 'Document', x: 320, y: 70, w: 260, fields: ['document_id PK', 'tenant_id FK', 'source (URL / file)', 'document_type', 'authority_rank', 'access_scope', 'current_version'] },
    { id: 'dv', name: 'DocumentVersion', x: 320, y: 270, w: 260, fields: ['version_id PK', 'document_id FK, version', 'approval_status, approved_by', 'effective_date, expiry_date', 'content_hash, raw_uri', 'ingest_status, ingested_at'] },
    { id: 'ch', name: 'Chunk (Pinecone vector)', x: 320, y: 480, w: 260, style: 'ext', fields: ['id = doc:version:chunk_no', 'values: 1536-dim embedding', 'tenant_id, document_id, version', 'document_type, approval_status', 'effective_date, expiry_date', 'access_scope, section_path', 'text, source_url'] },
    { id: 'cv', name: 'Conversation', x: 620, y: 70, w: 260, fields: ['conversation_id PK', 'tenant_id FK, session_id', 'channel, started_at', 'outcome: resolved | fallback |', '  escalated | intake'] },
    { id: 'ms', name: 'Message', x: 620, y: 240, w: 260, fields: ['message_id PK', 'conversation_id FK, role', 'content (PII-redacted)', 'intent, status', 'latency_ms, tokens, trace_id'] },
    { id: 'ct', name: 'Citation', x: 620, y: 410, w: 260, fields: ['message_id FK, ordinal', 'chunk_id, document_id', 'version, supporting_span'] },
    { id: 'fb', name: 'Feedback', x: 620, y: 545, w: 260, fields: ['feedback_id PK', 'message_id FK', 'rating, reason, flagged_by'] },
    { id: 'es', name: 'Escalation', x: 920, y: 70, w: 260, fields: ['escalation_id PK', 'conversation_id FK', 'trigger, priority (P1–P3)', 'handoff_packet (JSON)', 'assignee, status, resolution'] },
    { id: 'sr', name: 'ServiceRequest (Phase 2)', x: 920, y: 260, w: 260, style: 'future', fields: ['request_id PK', 'tenant_id, conversation_id FK', 'contact, address', 'equipment {brand, model, age}', 'symptoms, urgency, priority', 'status'] },
    { id: 'wo', name: 'WorkOrder (Phase 3)', x: 920, y: 480, w: 260, style: 'future', fields: ['work_order_id PK', 'request_id FK', 'crm_ref, scheduled_slot', 'technician_ref, status', 'idempotency_key'] },
  ],
  rels: [
    ['tn', 'b', 'us', 't', '1 : N'], ['tn', 'r', 'dc', 'l', '1 : N'], ['dc', 'b', 'dv', 't', '1 : N'], ['dv', 'b', 'ch', 't', '1 : N'],
    ['cv', 'b', 'ms', 't', '1 : N'], ['ms', 'b', 'ct', 't', '1 : N'], ['ct', 'l', 'ch', 'r', 'cites', { to: -104 }],
    ['ms', 'l', 'fb', 'l', '1 : N', { pts: [[620, 315], [600, 315], [600, 590], [620, 590]], li: 1 }],
    ['cv', 'r', 'es', 'l', '1 : N', { fo: -10, to: -5 }], ['cv', 'r', 'sr', 'l', '1 : 0..1', { fo: 30, mx: 900 }], ['sr', 'b', 'wo', 't', '1 : N'],
    ['ec', 'b', 'er', 't', 'scored in'],
  ],
});

// ---------- Agent architecture (Phase 2/3) ----------
arch({
  file: 'arch6_agents', W: 1160, H: 720,
  title: 'Agent Architecture (Phase 2 Intake · Phase 3 Operations)', sub: 'LangGraph state machines call the RAG core and enterprise systems only through policy-checked tools',
  groups: [
    { x: 225, y: 60, w: 570, h: 640, title: 'Agent Service (LangGraph)' },
    { x: 240, y: 385, w: 540, h: 215, title: 'Phase 3: operations subgraph', dash: true, fill: '#FFFFFF', ta: 'end' },
    { x: 815, y: 60, w: 330, h: 640, title: 'Tool layer', fill: '#FAF8FD', stroke: '#B9A7D9' },
  ],
  boxes: [
    { id: 'ch', x: 20, y: 260, w: 180, h: 70, style: 'human', text: 'Chat widget\n(SMS / voice later)', bold1: true },
    { id: 'csr', x: 20, y: 470, w: 180, h: 70, style: 'human', text: 'CSR / Dispatcher\nconsole', bold1: true },
    { id: 'rt', x: 420, y: 95, w: 180, h: 56, style: 'sys', text: 'Intent router' },
    { id: 'qa', x: 245, y: 195, w: 165, h: 58, style: 'sys', text: 'Answer question\n(RAG tool)' },
    { id: 'in', x: 428, y: 195, w: 165, h: 58, style: 'sys', text: 'Intake subgraph\n(slot filling, validation)' },
    { id: 'em', x: 611, y: 195, w: 165, h: 58, style: 'alert', text: 'Emergency\nhandler' },
    { id: 'cf', x: 428, y: 292, w: 165, h: 58, style: 'sys', text: 'Confirm & create\nservice request' },
    { id: 'ho', x: 611, y: 292, w: 165, h: 58, style: 'human', text: 'Human handoff\n(BP-4)' },
    { id: 'c1', x: 258, y: 425, w: 155, h: 56, style: 'sys', text: 'CRM lookup &\nenrichment' },
    { id: 'c2', x: 433, y: 425, w: 155, h: 56, style: 'sys', text: 'Availability\nsearch' },
    { id: 'c3', x: 608, y: 425, w: 155, h: 56, style: 'sys', text: 'Slot & price-range\nproposal' },
    { id: 'c4', x: 608, y: 520, w: 155, h: 60, style: 'human', text: 'Approval gate\n(human-in-the-loop)' },
    { id: 'c5', x: 433, y: 520, w: 155, h: 60, style: 'sys', text: 'Book + create\nwork order' },
    { id: 'c6', x: 258, y: 520, w: 155, h: 60, style: 'sys', text: 'Notify & sync\nCRM' },
    { id: 'cp', x: 245, y: 625, w: 530, h: 50, style: 'store', text: 'Checkpointer (PostgreSQL): durable, resumable conversation state per thread' },
    { id: 't1', x: 835, y: 95, w: 290, h: 62, style: 'sys', text: 'RAG Core API (read-only)\nsame guardrails & citations', bold1: true },
    { id: 't2', x: 835, y: 175, w: 290, h: 56, style: 'ext', text: 'CRM adapter', bold1: true },
    { id: 't3', x: 835, y: 245, w: 290, h: 56, style: 'ext', text: 'Scheduling adapter', bold1: true },
    { id: 't4', x: 835, y: 315, w: 290, h: 56, style: 'ext', text: 'Work-order adapter', bold1: true },
    { id: 't5', x: 835, y: 385, w: 290, h: 56, style: 'ext', text: 'Notification adapter\nemail / SMS', bold1: true },
    { id: 't6', x: 835, y: 460, w: 290, h: 110, style: 'task', text: 'Policy & guardrail layer\nallow-listed tools per tenant ·\napproval thresholds · scoped\ncredentials · idempotency keys ·\nrate limits', bold1: true },
    { id: 't7', x: 835, y: 595, w: 290, h: 80, style: 'store', text: 'Audit log\nevery tool call: who, what,\ninputs, result, approver', bold1: true },
  ],
  links: [
    ['ch', 'r', 'rt', 'l', 'messages', { both: true, li: 2, pts: [[202, 295], [216, 295], [216, 123], [418, 123]] }],
    ['rt', 'b', 'qa', 't'], ['rt', 'b', 'in', 't'], ['rt', 'b', 'em', 't'],
    ['in', 'b', 'cf', 't'], ['em', 'b', 'ho', 't'],
    ['cf', 'b', 'c1', 't', '', { pts: [[510, 350], [510, 400], [335, 400], [335, 423]] }],
    ['c1', 'r', 'c2', 'l'], ['c2', 'r', 'c3', 'l'], ['c3', 'b', 'c4', 't'], ['c4', 'l', 'c5', 'r'], ['c5', 'l', 'c6', 'r'],
    ['c4', 'l', 'csr', 'r', '', { pts: [[685, 580], [685, 608], [215, 608], [215, 505], [202, 505]], dash: true }],
  ],
  extra: `<path d="M795,360 L833,360" stroke="#7F8C8D" stroke-width="5" marker-end="url(#ahb)" marker-start="url(#ahb)"/>`,
});

// ---------- Deployment ----------
arch({
  file: 'arch7_deployment', W: 1180, H: 700,
  title: 'Deployment View (cloud-agnostic, Docker)', sub: 'Same images run on Docker Compose (dev / pilot) and on any container platform or Kubernetes (production)',
  groups: [
    { x: 20, y: 60, w: 240, h: 380, title: 'GitHub', fill: '#F6F8FB' },
    { x: 285, y: 60, w: 610, h: 620, title: 'Container platform (per environment: dev · staging · prod)', fill: '#F3FAF6', stroke: '#8BC3A3' },
    { x: 920, y: 60, w: 240, h: 620, title: 'Managed SaaS dependencies', fill: '#FAF8FD', stroke: '#B9A7D9' },
  ],
  boxes: [
    { id: 'repo', x: 40, y: 95, w: 200, h: 80, text: 'Repository\ncode · prompts · configs ·\neval datasets · IaC', bold1: true },
    { id: 'gha', x: 40, y: 200, w: 200, h: 110, text: 'GitHub Actions\nlint · unit tests · Ragas\nregression gate · build &\nscan images', bold1: true },
    { id: 'reg', x: 40, y: 335, w: 200, h: 80, style: 'store', text: 'Container registry\n(GHCR) signed images', bold1: true },
    { id: 'px', x: 305, y: 95, w: 570, h: 50, text: 'Reverse proxy: Traefik / NGINX · TLS (Let’s Encrypt) · rate limiting', size: 13 },
    { id: 'api', x: 305, y: 175, w: 180, h: 76, style: 'sys', text: 'api\nFastAPI chat / RAG\n(N replicas)', bold1: true },
    { id: 'adm', x: 500, y: 175, w: 180, h: 76, style: 'sys', text: 'admin-api\nknowledge, tenants,\nescalations', bold1: true },
    { id: 'ui', x: 695, y: 175, w: 180, h: 76, style: 'sys', text: 'web\nwidget JS, admin UI,\nStreamlit (MVP)', bold1: true },
    { id: 'wk', x: 305, y: 270, w: 180, h: 76, style: 'sys', text: 'worker\ningestion &\nnotifications (Celery)', bold1: true },
    { id: 'sc', x: 500, y: 270, w: 180, h: 76, style: 'sys', text: 'scheduler\ncrawl, expiry sweep,\nnightly evals', bold1: true },
    { id: 'ag', x: 695, y: 270, w: 180, h: 76, style: 'future', text: 'agent-svc\nLangGraph\n(Phase 2+)', bold1: true },
    { id: 'pg', x: 305, y: 380, w: 180, h: 90, shape: 'db', style: 'store', text: 'postgres\n(managed in prod)', bold1: true },
    { id: 'rd', x: 500, y: 380, w: 180, h: 90, shape: 'db', style: 'store', text: 'redis\ncache + broker', bold1: true },
    { id: 'ob', x: 695, y: 380, w: 180, h: 90, shape: 'db', style: 'store', text: 'minio / S3\nraw documents', bold1: true },
    { id: 'obs', x: 305, y: 505, w: 570, h: 72, style: 'task', text: 'Observability: OpenTelemetry collector → Prometheus / Grafana (or any APM) · structured logs · uptime & SLA alerts', size: 13 },
    { id: 'sec', x: 305, y: 595, w: 570, h: 62, style: 'task', text: 'Secrets via Vault / cloud KMS injected at runtime · network policies · non-root containers · backups', size: 13 },
    { id: 'x1', x: 940, y: 100, w: 200, h: 70, style: 'ext', text: 'OpenAI API\nper-env keys & quotas', bold1: true },
    { id: 'x2', x: 940, y: 195, w: 200, h: 70, style: 'ext', text: 'Pinecone\nindex per env', bold1: true },
    { id: 'x3', x: 940, y: 290, w: 200, h: 70, style: 'ext', text: 'LangSmith\nproject per env', bold1: true },
    { id: 'x4', x: 940, y: 385, w: 200, h: 70, style: 'ext', text: 'Email / SMS / Slack', bold1: true },
    { id: 'x5', x: 940, y: 480, w: 200, h: 70, style: 'future', text: 'CRM / scheduling\n(Phase 3)', bold1: true },
  ],
  links: [
    ['repo', 'b', 'gha', 't'], ['gha', 'b', 'reg', 't'],
    ['reg', 'r', 'api', 'l', 'deploy', { pts: [[240, 375], [270, 375], [270, 213], [303, 213]], li: 1 }],
  ],
  extra: `<path d="M895,360 L938,360" stroke="#7F8C8D" stroke-width="5" marker-end="url(#ahb)"/>` + text(140, 470, 'dev → staging → prod promotion;\nprod deploy needs passing\neval gates + approval', { size: 12.5, italic: true, color: '#34495E' }),
});

// =====================================================================
// Phase 4: payment & review
arch({
  file: 'bp0_end_to_end', W: 1190, H: 470,
  title: 'End-to-End Service Flow: Appointment → Field Service → Payment → Review', sub: 'One agent owns the job across its whole life; events wake it up when the customer is not chatting',
  groups: [{ x: 600, y: 62, w: 575, h: 225, title: 'Phase 4 (this document)', fill: '#F3FAF6', stroke: '#2E8B57', tc: '#123D27' }],
  boxes: [
    { id: 'b6', x: 20, y: 92, w: 170, h: 84, style: 'sys', text: 'BP-6\nAI service intake\n(Phase 2, live)', bold1: true },
    { id: 'b7', x: 215, y: 92, w: 170, h: 84, style: 'sys', text: 'BP-7\nBooking under policy\n(Phase 3, live)', bold1: true },
    { id: 'fs', x: 410, y: 92, w: 170, h: 84, style: 'human', text: 'Field service\nTechnician does the job\n(scheduling service)', bold1: true },
    { id: 'b8', x: 615, y: 92, w: 170, h: 84, style: 'sys', text: 'BP-8\nCompletion & invoicing', bold1: true },
    { id: 'b9', x: 805, y: 92, w: 170, h: 84, style: 'sys', text: 'BP-9\nPayment collection', bold1: true },
    { id: 'b10', x: 995, y: 92, w: 170, h: 84, style: 'sys', text: 'BP-10\nReview & recovery', bold1: true },
    { id: 't6', x: 20, y: 200, w: 170, h: 72, style: 'store', size: 12, text: 'Trigger: chat message\nstage: collecting →\nconfirming' },
    { id: 't7', x: 215, y: 200, w: 170, h: 72, style: 'store', size: 12, text: 'Trigger: customer confirms\nstage: choosing_slot →\nbooked | awaiting_approval' },
    { id: 'tf', x: 410, y: 200, w: 170, h: 72, style: 'store', size: 12, text: 'Trigger: technician sets\nstatus = completed\n+ final price' },
    { id: 't8', x: 615, y: 200, w: 170, h: 72, style: 'store', size: 12, text: 'Trigger: job.completed\nwebhook (sweep backup)\nstage: awaiting_payment' },
    { id: 't9', x: 805, y: 200, w: 170, h: 72, style: 'store', size: 12, text: 'Trigger: Stripe webhook\nor admin “mark paid”\nstage: paid' },
    { id: 't10', x: 995, y: 200, w: 170, h: 72, style: 'store', size: 12, text: 'Trigger: rating submitted\nstage: reviewed |\nrecovery' },
    { id: 'ag', x: 20, y: 310, w: 1145, h: 56, style: 'task', text: 'One ServiceIQ agent (LangGraph): same conversation thread · one policy layer (policy.py) · one audit log (ToolCall) · Postgres checkpointer', size: 13.5 },
    { id: 'hm', x: 20, y: 384, w: 1145, h: 62, style: 'human', size: 13, text: 'Humans in the loop: admin approves quotes, warranty, after-hours visits and out-of-range invoices · technician enters the final price\nadmin records Zelle / check / cash payments · service manager follows up on low ratings' },
  ],
  links: [
    ['b6', 'r', 'b7', 'l'], ['b7', 'r', 'fs', 'l'], ['fs', 'r', 'b8', 'l'], ['b8', 'r', 'b9', 'l'], ['b9', 'r', 'b10', 'l'],
    ['b6', 'b', 't6', 't', '', { dash: true }], ['b7', 'b', 't7', 't', '', { dash: true }], ['fs', 'b', 'tf', 't', '', { dash: true }],
    ['b8', 'b', 't8', 't', '', { dash: true }], ['b9', 'b', 't9', 't', '', { dash: true }], ['b10', 'b', 't10', 't', '', { dash: true }],
  ],
});

swimlane({
  file: 'bp8_completion_invoicing', LW: 220,
  title: 'BP-8  Job Completion & Invoicing (Phase 4)', sub: 'Technician completes the job → policy-checked invoice → secure payment link',
  lanes: [{ name: 'Technician', style: 'human' }, { name: 'Scheduling Service\n(fieldservice)', style: 'ext' }, { name: 'ServiceIQ Agent', style: 'sys' }, { name: 'Admin / Approver', style: 'human' }, { name: 'Customer', style: 'human' }],
  nodes: [
    { id: 's', lane: 0, row: 0, shape: 'term', text: 'Finishes the job\non site' },
    { id: 'a1', lane: 0, row: 1, text: 'Sets status = completed;\nenters final price\nand notes' },
    { id: 'a2', lane: 1, row: 2, text: 'Saves job; sends signed\njob.completed webhook\n(HMAC, retried)' },
    { id: 'sw', lane: 1, row: 3, style: 'store', text: 'Hourly sweep finds\ncompleted jobs with\nno event (backup)' },
    { id: 'a3', lane: 2, row: 3, text: 'on_job_completed:\nverify, find request;\nkey = job:{id}' },
    { id: 'd1', lane: 2, row: 4, shape: 'dec', text: 'Price within\npublished range?' },
    { id: 'ap', lane: 3, row: 4, text: 'Review invoice:\napprove, adjust\nor decline' },
    { id: 'dc', lane: 3, row: 5, w: 165, dx: -10, style: 'alert', text: 'Declined: void\ninvoice; ticket\nfor a callback' },
    { id: 'a4', lane: 2, row: 6, text: 'Create invoice + Stripe\nCheckout link (methods\nfrom tenant profile)' },
    { id: 'a5', lane: 4, row: 7, text: 'Receives invoice email\nwith secure payment\nlink (+ Zelle details)' },
    { id: 'e', lane: 2, row: 8, shape: 'term', text: 'stage = awaiting_payment\n→ BP-9' },
  ],
  edges: [['s', 'a1'], ['a1', 'a2'], ['a2', 'sw', '', { dash: true }], ['a2', 'a3'], ['sw', 'a3', 'backup', { dash: true }], ['a3', 'd1'],
    ['d1', 'ap', 'No'], ['d1', 'a4', 'Yes'], ['ap', 'dc', 'Declined'], ['ap', 'a4', 'Approved', { r: 'back', x: 884, li: 1, lx: 0, ly: 62 }],
    ['a4', 'a5', 'email'], ['a4', 'e']],
  footer: 'Every step is an audited tool call (ToolCall). A repeated webhook reuses key job:{id} and changes nothing.',
});

swimlane({
  file: 'bp9_payment', LW: 240,
  title: 'BP-9  Payment Collection (Phase 4)', sub: 'Hosted Stripe Checkout for online methods; admin records off-line payments; one shared “paid” path',
  lanes: [{ name: 'Customer', style: 'human' }, { name: 'Stripe Checkout\n(hosted)', style: 'ext' }, { name: 'ServiceIQ Agent', style: 'sys' }, { name: 'Admin', style: 'human' }],
  nodes: [
    { id: 's', lane: 0, row: 0, shape: 'term', text: 'Opens invoice email' },
    { id: 'd0', lane: 0, row: 1, shape: 'dec', text: 'Pay online\nor off-line?' },
    { id: 'a1', lane: 1, row: 2, text: 'Pays: card, Apple /\nGoogle Pay, ACH bank,\nCash App Pay' },
    { id: 'm1', lane: 3, row: 2, text: 'Records Zelle / check /\ncash: “Mark paid”\n(audited, recorded_by)' },
    { id: 'a2', lane: 1, row: 3, text: 'Signed webhook: session\ncompleted / async\nsucceeded / failed' },
    { id: 'a3', lane: 2, row: 4, text: 'Verify signature; set\ninvoice processing |\npaid | failed (idempotent)' },
    { id: 'd2', lane: 2, row: 5, shape: 'dec', text: 'Paid?' },
    { id: 'f1', lane: 3, row: 5, style: 'alert', text: 'Failed or unpaid N days:\none reminder + new link,\nthen ticket' },
    { id: 'a4', lane: 2, row: 6, text: 'on_paid: invoice and\nrequest = paid;\nsend receipt' },
    { id: 'a5', lane: 0, row: 7, text: 'Receives receipt' },
    { id: 'e', lane: 2, row: 8, shape: 'term', text: 'stage = paid\n→ BP-10' },
  ],
  edges: [['s', 'd0'], ['d0', 'a1', 'Online'], ['d0', 'm1', 'Zelle, check, cash', { r: 'h' }], ['a1', 'a2'], ['a2', 'a3'], ['a3', 'd2'],
    ['d2', 'f1', 'No'], ['d2', 'a4', 'Yes'], ['m1', 'a4', 'same path', { r: 'back', li: 1 }], ['a4', 'a5', 'receipt'], ['a4', 'e']],
  footer: 'Card data never reaches ServiceIQ (hosted Checkout). ACH settles in days, so the review request waits for “paid”.',
});

swimlane({
  file: 'bp10_review_recovery', LW: 280,
  title: 'BP-10  Review & Service Recovery (Phase 4)', sub: 'Every customer is asked and every reviewer sees the public link; low ratings start a recovery',
  lanes: [{ name: 'Customer', style: 'human' }, { name: 'ServiceIQ Agent', style: 'sys' }, { name: 'Admin / Service Manager', style: 'human' }],
  nodes: [
    { id: 's', lane: 1, row: 0, shape: 'term', text: 'Invoice paid (BP-9)' },
    { id: 'a1', lane: 1, row: 1, text: 'request_review: email a link\nback to the same conversation\n(signed token)' },
    { id: 'a2', lane: 0, row: 2, text: 'Opens chat; gives a\n1–5 rating and a comment' },
    { id: 'n1', lane: 2, row: 2, style: 'store', text: 'No reply in 7 days:\none reminder, then close\nwithout a review' },
    { id: 'a3', lane: 1, row: 3, text: 'record_review: guard checks\nthe comment, PII redacted,\nReview stored' },
    { id: 'a4', lane: 1, row: 4, text: 'Thank the customer; show the\npublic review link to every\nreviewer (no rating gating)' },
    { id: 'd1', lane: 1, row: 5, shape: 'dec', text: 'Rating ≤ 3?' },
    { id: 't1', lane: 2, row: 5, style: 'alert', text: 'Recovery ticket (P2) +\nadmin email with the\njob and comment' },
    { id: 'e1', lane: 1, row: 6, shape: 'term', text: 'stage = reviewed' },
    { id: 'a5', lane: 2, row: 6, text: 'Calls the customer: fix,\nrevisit or goodwill credit\n(credit needs approval)' },
    { id: 'e2', lane: 2, row: 7, shape: 'term', text: 'stage = recovery → resolved;\noutcome logged' },
  ],
  edges: [['s', 'a1'], ['a1', 'a2'], ['a1', 'n1', 'no reply', { dash: true }], ['a2', 'a3'], ['a3', 'a4'], ['a4', 'd1'],
    ['d1', 't1', 'Yes'], ['d1', 'e1', 'No'], ['t1', 'a5'], ['a5', 'e2']],
  footer: 'Asking everyone and showing the public link to all avoids review gating (Google review policy; FTC consumer-review rule).',
});

arch({
  file: 'bp11_eval_loop', W: 1170, H: 520,
  title: 'Phase 4 Agent Evaluation Loop', sub: 'Offline gates before release, online monitoring after, and every new failure becomes a labeled test case',
  boxes: [
    { id: 'gd', x: 20, y: 90, w: 200, h: 100, style: 'store', text: 'Golden dataset\nserviceiq-agent-golden\n50 cases, version-tagged\n(50 / 30 / 14 / 6 %)', bold1: true },
    { id: 'ex', x: 250, y: 90, w: 200, h: 100, style: 'ext', text: 'LangSmith experiment\nevaluate(): one trace\nper case · code, trajectory\n& LLM-judge evaluators', bold1: true },
    { id: 'cm', x: 480, y: 90, w: 200, h: 100, style: 'task', text: 'Compare runs\nbaseline vs candidate;\nper-case regressions', bold1: true },
    { id: 'cg', x: 710, y: 90, w: 200, h: 100, style: 'dec', text: 'Release gates\nsafety & integrity = 0\nquality ≥ pass bar', bold1: true },
    { id: 'pr', x: 940, y: 90, w: 200, h: 100, style: 'sys', text: 'Production\nAGENT_PAYMENTS_ENABLED\nper-tenant rollout', bold1: true },
    { id: 'mo', x: 940, y: 300, w: 200, h: 100, style: 'sys', text: 'Monitoring\nLangSmith online evals ·\nToolCall · Stripe · sweeps', bold1: true },
    { id: 'tr', x: 710, y: 300, w: 200, h: 100, style: 'human', text: 'Weekly triage\nlabel failed traces;\ntop failure modes', bold1: true },
    { id: 'im', x: 480, y: 300, w: 200, h: 100, style: 'task', text: 'Improvement playbook\none lever per experiment', bold1: true },
    { id: 'ru', x: 20, y: 300, w: 400, h: 100, style: 'store', size: 12.5, text: 'Rules\n• same dataset version and evaluators for both runs\n• change one lever per experiment\n• any safety or integrity regression blocks release', bold1: true },
  ],
  links: [
    ['gd', 'r', 'ex', 'l'], ['ex', 'r', 'cm', 'l'], ['cm', 'r', 'cg', 'l'], ['cg', 'r', 'pr', 'l', 'pass'],
    ['pr', 'b', 'mo', 't'], ['mo', 'l', 'tr', 'r', 'alerts'], ['tr', 'l', 'im', 'r', 'fix'],
    ['cg', 'b', 'im', 't', 'fail', { pts: [[810, 190], [810, 245], [640, 245], [640, 298]], li: 0 }],
    ['im', 't', 'ex', 'b', 're-run', { pts: [[540, 298], [540, 262], [350, 262], [350, 192]], li: 1 }],
    ['tr', 'b', 'gd', 'b', 'add labeled cases → new dataset version', { pts: [[810, 400], [810, 470], [120, 470], [120, 192]], li: 1 }],
  ],
});

arch({
  file: 'arch8_phase4_containers', W: 1200, H: 720,
  title: 'Phase 4 Container View', sub: 'New parts plug into the existing API, agent, policy layer and audit log; card data stays with Stripe',
  groups: [
    { x: 225, y: 60, w: 600, h: 490, title: 'ServiceIQ (Cloud Run)' },
    { x: 225, y: 570, w: 600, h: 130, title: 'Field-service platform (private Cloud Run)', fill: '#FAF8FD', stroke: '#B9A7D9' },
    { x: 850, y: 60, w: 330, h: 370, title: 'External services', fill: '#FAF8FD', stroke: '#B9A7D9' },
    { x: 850, y: 450, w: 330, h: 250, title: 'Later (not Phase 4)', dash: true, fill: '#FFFFFF' },
  ],
  boxes: [
    { id: 'cu', x: 20, y: 100, w: 180, h: 70, style: 'human', text: 'Customer\nchat widget', bold1: true },
    { id: 'em', x: 20, y: 200, w: 180, h: 70, style: 'human', text: 'Customer inbox\ninvoice · receipt ·\nreview links', bold1: true },
    { id: 'ad', x: 20, y: 385, w: 180, h: 70, style: 'human', text: 'Admin\napprove · mark paid ·\nrecovery', bold1: true },
    { id: 'te', x: 20, y: 590, w: 180, h: 70, style: 'human', text: 'Technician\ncomplete job +\nfinal price', bold1: true },
    { id: 'api', x: 245, y: 95, w: 270, h: 80, style: 'sys', size: 12.5, text: 'serviceiq-api (FastAPI)\n/v1/chat · /v1/reviews\n/v1/events/fieldservice\n/v1/payments/stripe', bold1: true },
    { id: 'ag', x: 535, y: 95, w: 270, h: 80, style: 'sys', text: 'Agent (one LangGraph graph)\nintake → booking → invoice\n→ payment → review', bold1: true },
    { id: 'po', x: 245, y: 200, w: 270, h: 74, style: 'task', size: 12.5, text: 'Policy layer (policy.py)\ncheck_booking · check_invoice\napprovals · rate limits', bold1: true },
    { id: 'to', x: 535, y: 200, w: 270, h: 74, style: 'task', size: 12.5, text: 'Audited tools (tools.py)\ncreate_invoice · send_payment_link\nrequest_review · record_review', bold1: true },
    { id: 'fc', x: 245, y: 300, w: 270, h: 66, style: 'sys', size: 12.5, text: 'fieldservice_client.py\nHttp | Memory (+ get_job)', bold1: true },
    { id: 'pa', x: 535, y: 300, w: 270, h: 66, style: 'sys', size: 12.5, text: 'payments.py (new)\nPaymentProvider: Stripe | Memory', bold1: true },
    { id: 'sw', x: 535, y: 390, w: 270, h: 60, style: 'sys', size: 12.5, text: 'sweep job (hourly)\napprovals · completions · payments', bold1: true },
    { id: 'aa', x: 245, y: 390, w: 270, h: 60, style: 'sys', size: 12.5, text: 'admin app (Streamlit)\nSchedule · Approvals · Requests', bold1: true },
    { id: 'db', x: 245, y: 470, w: 560, h: 62, shape: 'db', style: 'store', size: 12.5, text: 'PostgreSQL (Cloud SQL)\nServiceRequest · Approval · ToolCall · Invoice (new) · Review (new) · checkpoints', bold1: true },
    { id: 'fs', x: 245, y: 605, w: 560, h: 75, style: 'sys', size: 12.5, text: 'serviceiq-fsp (scheduling service)\njobs · technicians · availability · PUT /api/jobs → completed\nsends signed job.completed webhook to /v1/events/fieldservice', bold1: true },
    { id: 'st', x: 870, y: 95, w: 290, h: 74, style: 'ext', size: 12.5, text: 'Stripe (test mode)\nhosted Checkout + signed webhooks\ncard · wallets · ACH · Cash App', bold1: true },
    { id: 'sm', x: 870, y: 185, w: 290, h: 66, style: 'ext', size: 12.5, text: 'SMTP / outbox\ninvoice · receipt · review emails', bold1: true },
    { id: 'oa', x: 870, y: 267, w: 290, h: 66, style: 'ext', size: 12.5, text: 'OpenAI\nextraction · answers · LLM judge', bold1: true },
    { id: 'ls', x: 870, y: 349, w: 290, h: 66, style: 'ext', size: 12.5, text: 'LangSmith\nredacted traces · datasets · experiments', bold1: true },
    { id: 'cr', x: 870, y: 485, w: 290, h: 60, style: 'future', size: 12.5, text: 'CRM / FSP adapters\nServiceTitan · Jobber · Housecall Pro', bold1: true },
    { id: 'mc', x: 870, y: 557, w: 290, h: 60, style: 'future', size: 12.5, text: 'ServiceIQ MCP server\nowner Q&A + approvals from AI clients', bold1: true },
    { id: 've', x: 870, y: 629, w: 290, h: 60, style: 'future', size: 12.5, text: 'Venmo (PayPal / Braintree)\nSMS notifications', bold1: true },
  ],
  links: [
    ['cu', 'r', 'api', 'l', '', { both: true, to: 0 }],
    ['em', 't', 'cu', 'b', 'links reopen chat', { dash: true }],
    ['ad', 'r', 'aa', 'l'],
    ['te', 'r', 'fs', 'l', '', { pts: [[200, 625], [243, 625]] }],
    ['api', 'r', 'ag', 'l', '', { both: true }],
    ['ag', 'b', 'to', 't'],
    ['to', 'l', 'po', 'r', '', { both: true }],
    ['to', 'b', 'pa', 't'],
    ['to', 'b', 'fc', 't', '', { pts: [[580, 274], [580, 287], [380, 287], [380, 298]] }],
    ['pa', 'r', 'st', 'l', '', { both: true, pts: [[807, 333], [838, 333], [838, 132], [868, 132]] }],
    ['to', 'r', 'sm', 'l', '', { pts: [[805, 218], [868, 218]] }],
    ['fc', 'b', 'fs', 'r', '', { both: true, pts: [[380, 366], [380, 379], [815, 379], [815, 642], [807, 642]] }],
  ],
});

sequence({
  file: 'arch9_postservice_sequence', W: 1200,
  title: 'Post-Service Event Sequence (Phase 4)', sub: 'job.completed → invoice → payment → review; every write is idempotent and audited',
  parts: [
    { id: 'te', label: 'Technician /\nadmin app', style: 'human' }, { id: 'fs', label: 'Scheduling\nservice', style: 'sys' }, { id: 'api', label: 'ServiceIQ API', style: 'sys' },
    { id: 'ag', label: 'Agent', style: 'sys' }, { id: 'po', label: 'Policy +\naudit log', style: 'task' }, { id: 'st', label: 'Stripe', style: 'ext' },
    { id: 'ml', label: 'Email (SMTP)', style: 'ext' }, { id: 'cu', label: 'Customer', style: 'human' },
  ],
  steps: [
    { f: 'te', t: 'fs', text: 'PUT /api/jobs/{id} status=completed, price' },
    { f: 'fs', t: 'api', text: 'POST /v1/events/fieldservice (HMAC-signed)' },
    { f: 'api', t: 'ag', text: 'on_job_completed(request, price)' },
    { f: 'ag', t: 'po', text: 'check_invoice: price vs range' },
    { sep: 'alt  outside the published range → Approval row + admin email; continues after Agent.decide(approve)' },
    { f: 'ag', t: 'st', text: 'create Checkout Session\n(idempotency key = invoice id)' },
    { f: 'st', t: 'ag', text: 'session id + payment URL', dash: true },
    { f: 'ag', t: 'po', text: 'audit: create_invoice, send_payment_link' },
    { f: 'ag', t: 'ml', text: 'invoice email with payment link' },
    { f: 'ml', t: 'cu', text: 'invoice + link', dash: true },
    { f: 'cu', t: 'st', text: 'pays: card / wallet / ACH / Cash App' },
    { f: 'st', t: 'api', text: 'signed webhook: checkout.session.completed\nor async_payment_succeeded' },
    { f: 'api', t: 'ag', text: 'on_paid(event) — idempotent' },
    { f: 'ag', t: 'ml', text: 'receipt + review request link' },
    { f: 'cu', t: 'api', text: 'POST /v1/reviews (rating, comment)' },
    { f: 'api', t: 'ag', text: 'record_review: redact; ≤ 3 → ticket' },
    { f: 'ag', t: 'cu', text: 'thank-you + public review link (every rating)', dash: true },
    { sep: 'alt  off-line payment: admin “Mark paid (Zelle / check / cash)” → record_manual_payment → same on_paid path' },
  ],
});

arch({
  file: 'arch10_agent_lifecycle', W: 1180, H: 560,
  title: 'Agent Conversation Lifecycle (stages)', sub: 'Phase 4 adds post-service stages to the same checkpointed thread (thread_id = conversation_id)',
  groups: [
    { x: 10, y: 60, w: 1160, h: 180, title: 'Phase 2–3 (live)' },
    { x: 10, y: 255, w: 1160, h: 290, title: 'Phase 4 (this document)', fill: '#F3FAF6', stroke: '#2E8B57', tc: '#123D27' },
  ],
  boxes: [
    { id: 'co', x: 20, y: 95, w: 160, h: 50, style: 'sys', text: 'collecting' },
    { id: 'cf', x: 210, y: 95, w: 160, h: 50, style: 'sys', text: 'confirming' },
    { id: 'cs', x: 400, y: 95, w: 160, h: 50, style: 'sys', text: 'choosing_slot' },
    { id: 'aw', x: 400, y: 172, w: 160, h: 50, style: 'human', text: 'awaiting_approval' },
    { id: 'bk', x: 590, y: 95, w: 170, h: 50, shape: 'term', style: 'term', text: 'booked' },
    { id: 'ox', x: 790, y: 95, w: 370, h: 60, style: 'store', size: 12.5, text: 'Other exits: emergency · not_eligible · escalated ·\nintake_cancelled · answered (Q&A only)' },
    { id: 'ap', x: 590, y: 300, w: 170, h: 60, style: 'sys', size: 12.5, text: 'awaiting_payment\ninvoice sent or pending\napproval', bold1: true },
    { id: 'pd', x: 800, y: 300, w: 160, h: 60, style: 'sys', size: 12.5, text: 'paid\nreceipt sent', bold1: true },
    { id: 'rr', x: 1000, y: 300, w: 160, h: 60, style: 'sys', size: 12.5, text: 'review_requested\nlink emailed', bold1: true },
    { id: 'rv', x: 1000, y: 450, w: 160, h: 50, shape: 'term', style: 'term', text: 'reviewed' },
    { id: 'rc', x: 800, y: 440, w: 160, h: 70, style: 'alert', size: 12.5, text: 'recovery\nticket open\n(rating ≤ 3)', bold1: true },
    { id: 'vd', x: 590, y: 440, w: 170, h: 70, style: 'alert', size: 12.5, text: 'invoice void\ndeclined by approver\n→ callback ticket', bold1: true },
    { id: 'mp', x: 20, y: 290, w: 520, h: 225, style: 'store', size: 12.5, text: 'Status fields that move with the stages\n\nServiceRequest.status\nscheduled → completed → invoiced → paid → reviewed\n\nInvoice.status\npending_approval → sent → processing → paid | failed | void\n\nfailed: a new link is sent once; after that a ticket is opened', bold1: true },
  ],
  links: [
    ['co', 'r', 'cf', 'l'], ['cf', 'r', 'cs', 'l'], ['cs', 'r', 'bk', 'l'],
    ['cf', 'b', 'aw', 'l', 'needs approval', { li: 0, lx: -48 }], ['aw', 'r', 'bk', 'b', 'approved'],
    ['bk', 'b', 'ap', 't', 'job.completed', { pts: [[675, 145], [675, 298]], ly: 40 }],
    ['ap', 'r', 'pd', 'l', 'paid'], ['pd', 'r', 'rr', 'l'],
    ['rr', 'b', 'rv', 't', 'rating ≥ 4'], ['rr', 'b', 'rc', 't', 'rating ≤ 3', { pts: [[1030, 360], [1030, 400], [880, 400], [880, 438]], li: 1 }],
    ['rc', 'r', 'rv', 'l', 'resolved', { dash: true, to: 0, pts: [[960, 475], [998, 475]] }],
    ['ap', 'b', 'vd', 't', 'declined'],
  ],
});

entities({
  file: 'arch11_phase4_data', W: 1100, H: 640,
  title: 'Phase 4 Data Model Additions', sub: 'New tables in green; existing tables (grey) gain new values. New columns on existing tables go in NEW_COLUMNS (db.py)',
  ents: [
    { id: 'sr', name: 'ServiceRequest (existing)', x: 20, y: 70, w: 320, style: 'store', fields: ['request_id PK', 'tenant_id FK, conversation_id', 'payload (ServiceRequestV1)', 'job_ref (scheduling job id)', 'status: … scheduled → completed |', '  invoiced | paid | reviewed', 'String(16): every value fits'] },
    { id: 'ap', name: 'Approval (existing)', x: 20, y: 300, w: 320, style: 'store', fields: ['approval_id PK', 'request_id FK, condition', '+ invoice_out_of_range', 'proposal JSON (+ kind: invoice)', 'status, decided_by, escalated_at'] },
    { id: 'in', name: 'Invoice (new)', x: 390, y: 70, w: 330, style: 'sys', fields: ['invoice_id PK', 'tenant_id FK, request_id FK, job_ref', 'amount_cents, currency', 'status: pending_approval | sent |', '  processing | paid | failed | void', 'method: card | ach | cashapp |', '  zelle | check | cash', 'provider_session_id, payment_url', 'sent_at, paid_at, reminded_at', 'recorded_by (manual payments)'] },
    { id: 'rv', name: 'Review (new)', x: 390, y: 380, w: 330, style: 'sys', fields: ['review_id PK', 'tenant_id FK, request_id FK', 'rating (1–5)', 'comment (PII-redacted)', 'public_link_shown (always true)', 'recovery_escalation_id FK', 'created_at'] },
    { id: 'tc', name: 'ToolCall (existing audit log)', x: 760, y: 70, w: 320, style: 'store', fields: ['call_id PK, tenant_id', 'tool: + create_invoice, send_payment_link,', '  request_review, record_review,', '  record_manual_payment', 'idempotency_key: job:{id}, invoice:{id}', 'inputs (redacted), result, approver'] },
    { id: 'es', name: 'Escalation (existing)', x: 760, y: 300, w: 320, style: 'store', fields: ['escalation_id PK', 'conversation_id FK', 'trigger: + payment_failed, low_rating', 'priority P1–P3, status'] },
    { id: 'pf', name: 'profile.json (per tenant config)', x: 760, y: 460, w: 320, style: 'future', fields: ['payment_methods [card, us_bank_account,', '  cashapp]', 'zelle_handle', 'review_url (public review page)', 'payment_reminder_days'] },
  ],
  rels: [
    ['sr', 'r', 'in', 'l', '1 : N', { fo: -20, to: -40 }],
    ['sr', 'b', 'ap', 't', '1 : N'],
    ['sr', 'r', 'rv', 'l', '1 : 0..1', { fo: 40, to: 0, mx: 365 }],
    ['in', 'r', 'tc', 'l', 'audited', { dash: true, to: -20, fo: -60 }],
    ['rv', 'r', 'es', 'l', '≤ 3 → recovery', { dash: true }],
  ],
});

console.log('SVGs written to', OUT);
