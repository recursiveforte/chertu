"""Current browser links for agent-created files, shared by both harnesses."""

import json
import os
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urlsplit

from chert.file_access import visible
from chert.paths import runtime_path
from chert.projects import ProjectStore


def viewer_url_file():
    return runtime_path(os.environ.get("CHERT_FILES_URL_FILE"), "private/file-viewer-url")


def viewer_url():
    value = os.environ.get("CHERT_FILES_URL", "").strip()
    if not value:
        try:
            value = viewer_url_file().read_text().strip()
        except OSError:
            return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return ""
    except ValueError:
        return ""
    return value.rstrip("/") + "/"


def viewer_context(cwd, host="local"):
    base = viewer_url()
    if not base or not cwd:
        return ""
    try:
        store = ProjectStore(runtime_path(os.environ.get("PROJECT_STATE_FILE"), "private/projects.json"))
        directory = Path(cwd).resolve() if host == "local" else PurePosixPath(cwd)
        matches = [p for p in store.projects.values()
                   if p.host == host and directory.is_relative_to(PurePosixPath(p.directory))
                   and visible(directory.relative_to(PurePosixPath(p.directory)))]
        if not matches:
            return ""
        project = max(matches, key=lambda p: len(PurePosixPath(p.directory).parts))
    except (OSError, ValueError, TypeError, KeyError):
        return ""
    example = base + "?" + urlencode({"project": project.name, "path": "docs/report.md"})
    return (
        "[Chert file viewer context: When you create or update user-facing documents, "
        "reports, source files, or images, automatically include clickable viewer links "
        "in your reply without waiting to be asked. Use ordinary Markdown links, e.g. "
        f"[Open report]({example}). The viewer project is {json.dumps(project.name)}, "
        f"root {json.dumps(project.directory)}, on host {json.dumps(host)}. "
        f"The current public viewer base is {base}. Build each URL with query parameters "
        "project (the project name) and path (the file path relative to that root); "
        "URL-encode both values, including spaces, #, &, and Unicode. Use the actual "
        "file you produced, not the example path. Prefer these browser links to bare "
        "filesystem paths or localhost links. The viewer handles Markdown, text, and "
        "images, with downloads up to 8 MB. Hidden/private paths and files outside "
        "the registered root are unavailable; never claim a link works for those. "
        "Never publish credentials. Keep explicit image attachments when requested. "
        "Use this current base instead of older tunnel addresses in the conversation.]"
    )
