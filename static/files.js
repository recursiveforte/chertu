'use strict';
const $ = id => document.getElementById(id);
let project = '', folder = '', path = '', entries = [], doc = null, mode = 'preview';
let fileGeneration = 0, listGeneration = 0, polling = false;
const query = new URLSearchParams(location.search);
const api = (suffix, params = {}) => `/files/api/${encodeURIComponent(project)}/${suffix}?${new URLSearchParams(params)}`;
const raw = () => `/files/raw/${encodeURIComponent(project)}?${new URLSearchParams({path})}`;
const parent = value => value.split('/').slice(0, -1).join('/');
const join = (a, b) => a ? `${a}/${b}` : b;
const size = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n/1024).toFixed(1)} KB` : `${(n/1048576).toFixed(1)} MB`;
async function fetchJSON(url) {
  const response = await fetch(url, {cache: 'no-store'});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
function saveLocation() {
  history.replaceState(null, '', `/files/?${new URLSearchParams({project, ...(path ? {path} : {folder})})}${location.hash}`);
}
function renderEntries() {
  $('entries').replaceChildren();
  const filter = $('filter').value.toLocaleLowerCase();
  const shown = entries.filter(item => item.name.toLocaleLowerCase().includes(filter));
  for (const item of shown) {
    const button = document.createElement('button');
    button.className = 'entry' + (join(folder, item.name) === path ? ' selected' : '');
    const icon = document.createElement('span');
    icon.className = 'icon';
    icon.textContent = item.directory ? '▸' : /\.(md|mdown|markdown)$/i.test(item.name) ? 'MD' : /\.(png|jpe?g|gif|webp)$/i.test(item.name) ? 'IMG' : 'TXT';
    const name = document.createElement('span');
    name.className = 'name'; name.textContent = item.name;
    button.title = item.name; button.append(icon, name);
    button.onclick = () => item.directory ? openFolder(join(folder, item.name)) : openFile(join(folder, item.name));
    $('entries').append(button);
  }
  if (!shown.length) $('list-status').textContent = entries.length ? 'No matching files.' : 'This folder is empty.';
}
async function openFolder(value) {
  folder = value;
  const generation = ++listGeneration;
  $('folder').textContent = folder || 'Project root'; $('folder').title = folder;
  $('up').disabled = !folder; $('list-status').textContent = 'Loading files…';
  entries = []; renderEntries(); $('list-status').textContent = 'Loading files…';
  try {
    const result = await fetchJSON(api('list', {path: folder}));
    if (generation !== listGeneration) return;
    entries = result.entries;
    $('list-status').textContent = result.truncated ? 'Showing the first 2,000 entries.' : `${entries.length} items`;
    renderEntries();
  } catch (error) {
    if (generation === listGeneration) $('list-status').textContent = error.message;
  }
  if (!path) saveLocation();
}
function renderDocument() {
  if (!doc) return;
  $('metadata').textContent = `${path} · ${size(doc.size)} · Updated ${new Date(doc.modified*1000).toLocaleString()}`;
  $('preview').classList.toggle('active', mode === 'preview');
  $('source').classList.toggle('active', mode === 'source');
  $('preview').setAttribute('aria-pressed', mode === 'preview');
  $('source').setAttribute('aria-pressed', mode === 'source');
  $('source').hidden = !('text' in doc);
  $('content').className = 'document'; $('content').replaceChildren();
  if (mode === 'source' || doc.kind === 'text') {
    $('content').classList.add('source');
    const pre = document.createElement('pre'); const code = document.createElement('code');
    code.textContent = doc.text; pre.append(code); $('content').append(pre);
  } else if (doc.kind === 'markdown') {
    // HTML comes only from the server's Markdown parser with raw HTML disabled.
    $('content').innerHTML = doc.html;
  } else if (doc.kind === 'image') {
    const img = document.createElement('img'); img.src = `${raw()}&v=${doc.version}`;
    img.alt = path.split('/').pop(); $('content').append(img);
  } else {
    $('content').textContent = 'Preview is available for Markdown, UTF-8 text up to 2 MB, and PNG, JPEG, GIF, or WebP images. Download this file to open it in another app.';
  }
}
async function openFile(value, initial = false) {
  if (!initial && location.hash) history.replaceState(null, '', location.pathname + location.search);
  path = value; doc = null; mode = 'preview';
  const generation = ++fileGeneration;
  $('welcome').hidden = true; $('reader').hidden = false;
  $('filename').textContent = path.split('/').pop(); $('metadata').textContent = path;
  $('download').href = `${raw()}&download=1`;
  $('content').className = 'document'; $('content').textContent = 'Opening file…';
  $('status').textContent = ''; saveLocation(); renderEntries();
  try {
    const result = await fetchJSON(api('document', {path}));
    if (generation !== fileGeneration) return;
    doc = result; renderDocument(); $('status').textContent = 'Up to date';
    if (initial && location.hash) document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
  } catch (error) {
    if (generation !== fileGeneration) return;
    $('content').textContent = error.message; $('status').textContent = 'Could not open file';
  }
}
async function refreshFile() {
  if (!path || !doc || polling) return;
  polling = true;
  const generation = fileGeneration;
  try {
    const result = await fetchJSON(api('document', {path, version: doc.version}));
    if (generation !== fileGeneration) return;
    if (!result.unchanged) { doc = result; renderDocument(); }
    $('status').textContent = 'Up to date';
  } catch (error) {
    if (generation === fileGeneration) $('status').textContent = `Refresh failed: ${error.message}`;
  } finally { polling = false; }
}
$('project').onchange = () => {
  project = $('project').value; path = ''; doc = null; ++fileGeneration;
  $('reader').hidden = true; $('welcome').hidden = false; $('filter').value = '';
  openFolder('');
};
$('up').onclick = () => openFolder(parent(folder));
$('reload').onclick = () => { openFolder(folder); refreshFile(); };
$('filter').oninput = () => { $('list-status').textContent = `${entries.length} items`; renderEntries(); };
$('preview').onclick = () => { mode = 'preview'; renderDocument(); };
$('source').onclick = () => { mode = 'source'; renderDocument(); };
$('copy').onclick = async () => {
  try { await navigator.clipboard.writeText(location.href); $('copy').textContent = 'Copied'; }
  catch { window.prompt('Copy this file link:', location.href); }
  setTimeout(() => { $('copy').textContent = 'Copy link'; }, 1800);
};
setInterval(() => { if ($('auto').checked && !document.hidden) refreshFile(); }, 5000);
(async () => {
  try {
    const result = await fetchJSON('/files/api/projects'); $('project').replaceChildren();
    for (const p of result.projects) {
      const option = document.createElement('option'); option.value = p.name;
      option.textContent = `${p.name}${p.host !== 'local' ? ' · ' + p.host : ''}${p.archived ? ' (archived)' : ''}`;
      $('project').append(option);
    }
    if (!result.projects.length) { $('list-status').textContent = 'No projects registered yet.'; return; }
    const requested = query.get('project');
    project = result.projects.some(p => p.name === requested) ? requested : result.projects[0].name;
    $('project').value = project;
    const initial = requested === project ? query.get('path') || '' : '';
    path = initial;
    const initialProject = project;
    await openFolder(initial ? parent(initial) : query.get('folder') || '');
    if (initial && project === initialProject) await openFile(initial, true);
  } catch (error) { $('list-status').textContent = error.message; }
})();
