"""The upstream Signalscope templates, with Claude and Codex routes."""
import json
import os
from pathlib import Path
import time
import uuid
import urllib.error
import urllib.request

from dotenv import load_dotenv
load_dotenv(Path(__file__).with_name('.env'))
import app as legacy
from flask import abort, jsonify, redirect, render_template, request, send_from_directory

app = legacy.app
STATE = Path(os.environ.get('CODEX_STATE_FILE') or 'private/codex-state.json')


def codex_sessions():
    try:
        rows = json.loads(STATE.read_text()).get('sessions', [])
    except FileNotFoundError:
        rows = []
    result = []
    for row in rows:
        if not row.get('codex_thread'):
            continue
        result.append({**row, 'sid': row['codex_thread'], 'project': Path(row['cwd']).name,
                       'status': {'running': 'busy'}.get(row['status'], row['status']),
                       'pane': row.get('terminal_pane'), 'can_screen': row['status'] != 'ended',
                       'can_send': row['status'] != 'ended',
                       'updatedAt': (row.get('turn_started') or STATE.stat().st_mtime) * 1000,
                       'snippet': next((r['text'][:280] for r in reversed(row.get('recent', []))
                                        if r['kind'] == 'agentMessage'), '')})
    return result


def find(sid):
    session = next((s for s in codex_sessions() if s['sid'] == sid), None)
    if session:
        return session
    try:
        sid = str(uuid.UUID(sid))
    except ValueError:
        return None
    if (STATE.parent/'codex-transcripts'/f'{sid}.jsonl').exists():
        return {'sid': sid, 'name': sid[:8], 'cwd': '', 'project': '', 'status': 'ended',
                'pane': None, 'can_send': False, 'can_screen': False}
    return None


@app.route('/')
def home():
    return '<a href="/codex/">Codex signalscope</a> · <a href="/claudes/">Claude signalscope</a>'


@app.route('/codex')
def codex_bare():
    return redirect('/codex/')


@app.route('/codex/')
def codex_index():
    return render_template('index.html', prefix='/codex', backend_label='Codex sessions on this box')


@app.route('/codex/static/<path:name>')
def codex_static(name):
    return send_from_directory(Path(__file__).with_name('static'), name)


@app.route('/codex/api/sessions')
def codex_list():
    rows = codex_sessions()
    dead = [{'sid': s['sid'], 'slug': s['cwd'], 'mtime': s['updatedAt']/1000} for s in rows if s['status'] == 'ended']
    return jsonify(sessions=[s for s in rows if s['status'] != 'ended'], dead=dead,
                   now=time.time(), disk=legacy.disk_info())


@app.route('/codex/s/<sid>')
def codex_page(sid):
    session = find(sid)
    if session is None:
        abort(404)
    return render_template('session.html', prefix='/codex', s=session, live=session['can_send'])


@app.route('/codex/api/transcript/<sid>')
def codex_transcript(sid):
    session = find(sid)
    if session is None:
        abort(404)
    path = STATE.parent/'codex-transcripts'/f'{session["sid"]}.jsonl'
    if not path.exists():
        return jsonify(items=[], size=0, status=session['status'])
    after = request.args.get('after', type=int, default=0)
    if after and path.stat().st_size <= after:
        return jsonify(unchanged=True, size=after, status=session['status'])
    items, size = legacy.parse_transcript(path, from_byte=after)
    return jsonify(items=items[-400:], size=size, status=session['status'])


@app.route('/codex/api/screen/<sid>')
def codex_screen(sid):
    if find(sid) is None:
        abort(404)
    return bridge_request('screen', {'sid': sid})


@app.route('/codex/api/send/<sid>', methods=['POST'])
def codex_send(sid):
    if find(sid) is None:
        abort(404)
    body = request.get_json(silent=True) or {}
    return bridge_request('send', {**body, 'sid': sid})


def bridge_request(operation, body):
    secret_path = Path(os.environ.get('HEARTH_HOOK_SECRET') or Path.home()/'.claude/hearth-hook.secret')
    try:
        req = urllib.request.Request(f'http://127.0.0.1:{os.environ.get("HEARTH_HOOK_PORT") or 8897}/codex/{operation}',
            data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'X-Hearth-Secret': secret_path.read_text().strip()})
        with urllib.request.urlopen(req, timeout=30) as response:
            return jsonify(json.load(response))
    except urllib.error.HTTPError as exc:
        return jsonify(json.load(exc)), exc.code
    except OSError as exc:
        return jsonify(error=str(exc)), 503
