"""Project documents in the existing loopback/authenticated dashboard."""

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import subprocess
from types import SimpleNamespace
from urllib.parse import unquote, urlencode, urlsplit

from flask import Blueprint, Response, abort, jsonify, render_template, request, send_from_directory, url_for
from markdown_it import MarkdownIt

from chert import file_access
from chert.hosts import load_hosts
from chert.paths import ROOT, runtime_path
from chert.projects import ProjectStore

files = Blueprint("files", __name__, url_prefix="/files")
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".gif": "image/gif", ".webp": "image/webp"}
MARKDOWN = {".md", ".markdown", ".mdown"}


def projects():
    path = runtime_path(os.environ.get("PROJECT_STATE_FILE"), "private/projects.json")
    return ProjectStore(path).projects


def read_project(project, path, action):
    params = {"root": project.directory, "path": path, "action": action}
    try:
        if project.host == "local":
            return file_access.access(**params)
        hosts_path = os.environ.get("CHERT_HOSTS_FILE")
        hosts = load_hosts(runtime_path(hosts_path, "") if hosts_path else None,
                           SimpleNamespace(project_root=Path.home() / "projects", live_socket=None))
        host = hosts.get(project.host)
        if host is None:
            abort(503, description="This project's host is not configured.")
        command = shlex.join([host.python, "-c", Path(file_access.__file__).read_text()])
        result = subprocess.run([*host.ssh_command(), command], input=json.dumps(params),
                                text=True, capture_output=True, timeout=20)
        if result.returncode:
            abort(503, description="The project host is unavailable. Try again shortly.")
        payload = json.loads(result.stdout)
        if "error" in payload:
            status = {"FileNotFoundError": 404, "NotADirectoryError": 404,
                      "PermissionError": 403}.get(payload.get("type"), 400)
            abort(status, description="The file is unavailable, private, or exceeds the 8 MB limit.")
        return payload["result"]
    except (FileNotFoundError, NotADirectoryError):
        abort(404, description="This file or folder no longer exists.")
    except PermissionError:
        abort(403, description="This path is private or outside the project.")
    except ValueError:
        abort(400, description="This file cannot be opened (8 MB limit).")
    except (OSError, subprocess.TimeoutExpired):
        abort(503, description="The project files are temporarily unavailable.")


def project_or_404(name):
    project = projects().get(name)
    if project is None:
        abort(404, description="Project not found.")
    return project


def document_url(project, path, raw=False):
    endpoint = "files.raw" if raw else "files.index"
    return url_for(endpoint, **({"name": project} if raw else {})) + "?" + urlencode(
        {"path": path, **({} if raw else {"project": project})})


def render_markdown(text, project, path):
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    tokens = md.parse(text)
    headings = {}
    for index, block in enumerate(tokens):
        if block.type == "heading_open":
            title = tokens[index + 1].content
            slug = re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")
            count = headings.get(slug, 0)
            headings[slug] = count + 1
            block.attrSet("id", "doc-" + slug + (f"-{count}" if count else ""))
        for token in block.children or []:
            attribute = "src" if token.type == "image" else "href" if token.type == "link_open" else None
            if not attribute:
                continue
            value = token.attrGet(attribute) or ""
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc:
                continue
            if not parsed.path:
                if parsed.fragment:
                    token.attrSet(attribute, "#doc-" + parsed.fragment)
                continue
            # Resolve document links within the selected project, never as dashboard routes.
            relative = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(parsed.path)))
            if relative.startswith("/") or relative == ".." or relative.startswith("../"):
                token.attrSet(attribute, "#")
                continue
            rewritten = document_url(project, relative, raw=attribute == "src")
            token.attrSet(attribute, rewritten + ("#doc-" + parsed.fragment if parsed.fragment else ""))
    return md.renderer.render(tokens, md.options, {})


@files.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'self'; form-action 'none'")
    return response


@files.errorhandler(400)
@files.errorhandler(403)
@files.errorhandler(404)
@files.errorhandler(503)
def file_error(error):
    return jsonify(error=error.description), error.code


@files.route("/")
def index():
    return render_template("files.html")


@files.route("/static/<path:name>")
def static_asset(name):
    return send_from_directory(ROOT / "static", name)


@files.route("/api/projects")
def project_list():
    return jsonify(projects=[{"name": p.name, "host": p.host, "archived": p.archived}
                            for p in sorted(projects().values(), key=lambda p: (p.archived, p.name))])


@files.route("/api/<name>/list")
def listing(name):
    return jsonify(read_project(project_or_404(name), request.args.get("path", ""), "list"))


@files.route("/api/<name>/document")
def document(name):
    path = request.args.get("path", "")
    result = read_project(project_or_404(name), path, "read")
    data = base64.b64decode(result.pop("data"))
    version = hashlib.sha256(data).hexdigest()
    if request.args.get("version") == version:
        return jsonify(unchanged=True, version=version)
    suffix = PurePosixPath(path).suffix.lower()
    kind = "image" if suffix in IMAGES else "download"
    result.update(kind=kind, version=version)
    if kind != "image" and len(data) <= 2 * 1024 * 1024 and b"\x00" not in data:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
        else:
            result.update(kind="markdown" if suffix in MARKDOWN else "text", text=text)
            if suffix in MARKDOWN:
                result["html"] = render_markdown(text, name, path)
    return jsonify(result)


@files.route("/raw/<name>")
def raw(name):
    path = request.args.get("path", "")
    result = read_project(project_or_404(name), path, "read")
    suffix = PurePosixPath(path).suffix.lower()
    response = Response(base64.b64decode(result["data"]), mimetype=IMAGES.get(suffix, "application/octet-stream"))
    disposition = "inline" if suffix in IMAGES and "download" not in request.args else "attachment"
    response.headers.set("Content-Disposition", disposition, filename=PurePosixPath(path).name)
    return response
