"""Codex transcript recovery through upstream Ash Twin's private S3 backup."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time

import ash_twin

_cache = (0, {})


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex').resolve()


def backup_index():
    global _cache
    if not ash_twin.BUCKET:
        return {}
    if time.time() - _cache[0] < 600:
        return _cache[1]
    try:
        relative = codex_home().relative_to(ash_twin.HOME.resolve()).as_posix()
    except ValueError:
        return {}
    rows = {}
    client = ash_twin._s3()
    for folder in ('sessions', 'archived_sessions'):
        prefix = f'backup/{ash_twin.HOST}/{relative}/{folder}/'
        token = None
        while True:
            params = {'Bucket': ash_twin.BUCKET, 'Prefix': prefix}
            if token:
                params['ContinuationToken'] = token
            page = client.list_objects_v2(**params)
            for obj in page.get('Contents', []):
                match = re.search(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$', obj['Key'])
                if match:
                    sid = match.group(1)
                    rows[sid] = {'id': sid, 'sid': sid, 'label': f'Codex {sid[:8]} (backup)',
                        'name': '', 'first': '', 'project': 'backup', 'cwd': '', 'live': False,
                        'where': 's3', 'mtime': obj['LastModified'].timestamp(), 'size': obj['Size'],
                        'key': obj['Key'], 'relative': folder + '/' + obj['Key'][len(prefix):]}
            if not page.get('IsTruncated'):
                break
            token = page['NextContinuationToken']
    _cache = (time.time(), rows)
    return rows


def restore(sid):
    row = backup_index().get(sid)
    if row is None:
        return False
    relative = PurePosixPath(row['relative'])
    destination = (codex_home()/relative).resolve()
    if relative.is_absolute() or '..' in relative.parts or not destination.is_relative_to(codex_home()):
        raise ValueError('Invalid backup transcript path')
    if destination.exists():
        return False  # A backup must never overwrite a newer local conversation.
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=destination.parent, prefix='.restore-')
    os.close(fd)
    try:
        ash_twin._s3().download_file(ash_twin.BUCKET, row['key'], filename)
        os.chmod(filename, 0o600)
        with open(filename) as file:
            meta = json.loads(file.readline(4 * 1024 * 1024))
        if meta.get('type') != 'session_meta' or meta.get('payload', {}).get('id') != sid:
            raise ValueError('Backup transcript identity does not match the requested session')
        try:
            os.link(filename, destination)  # Atomic no-overwrite even if another writer races us.
        except FileExistsError:
            return False
        return True
    finally:
        if os.path.exists(filename):
            os.unlink(filename)
