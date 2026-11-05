import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chert import file_access
from chert.projects import Project
from chert.web import files
from chert.web.dashboard import app


class FileViewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = Project("demo", str(self.root), 123)
        self.patch = patch.object(files, "projects", return_value={"demo": self.project})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = app.test_client()

    def get(self, route, path="", **query):
        return self.client.get(f"/files/api/demo/{route}", query_string={"path": path, **query})

    def test_listing_and_private_boundaries(self):
        (self.root / "notes").mkdir()
        (self.root / "readme.md").write_text("hello")
        (self.root / ".env").write_text("secret")
        (self.root / "private").mkdir()
        (self.root / "private" / "state.json").write_text("secret")
        (self.root / "alias.txt").symlink_to(self.root / ".env")
        (self.root / "outside").symlink_to("/etc")
        result = self.get("list")
        self.assertEqual([item["name"] for item in result.json["entries"]], ["notes", "readme.md"])
        for path in [".env", "private/state.json", "alias.txt", "../escape", "/etc/passwd", "outside/passwd"]:
            with self.subTest(path=path):
                self.assertEqual(self.get("document", path).status_code, 403)
                self.assertEqual(self.client.get("/files/raw/demo", query_string={"path": path}).status_code, 403)
        self.assertEqual(self.get("document", "missing.md").status_code, 404)

    def test_markdown_is_safe_and_relative_links_are_local(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "note.md").write_text(
            '# Plan\n\n| Task | State |\n| --- | --- |\n| Ship | Done |\n\n'
            '```python\nprint("hello")\n```\n\n'
            '<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n'
            '![figure](../plot.png)\n\n[next](other.md#details)\n\n[jump](#plan)\n\n'
            '[escape](../../outside)'
        )
        response = self.get("document", "docs/note.md")
        self.assertEqual(response.status_code, 200)
        html = response.json["html"]
        self.assertIn('<h1 id="doc-plan">Plan</h1>', html)
        self.assertIn("<table>", html)
        self.assertIn('class="language-python"', html)
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn('/files/raw/demo?path=plot.png', html)
        self.assertIn('path=docs%2Fother.md&amp;project=demo#doc-details', html)
        self.assertIn('href="#doc-plan"', html)
        self.assertIn('href="#"', html)
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_refresh_text_images_and_safe_downloads(self):
        note = self.root / "note.md"
        note.write_text("first")
        version = self.get("document", "note.md").json["version"]
        self.assertTrue(self.get("document", "note.md", version=version).json["unchanged"])
        note.write_text("second")
        self.assertEqual(self.get("document", "note.md", version=version).json["text"], "second")
        (self.root / "page.html").write_text("<script>bad()</script>")
        self.assertEqual(self.get("document", "page.html").json["kind"], "text")
        raw = self.client.get('/files/raw/demo?path=page.html')
        self.assertEqual(raw.mimetype, "application/octet-stream")
        self.assertTrue(raw.headers["Content-Disposition"].startswith("attachment"))
        (self.root / "image.png").write_bytes(b"\x89PNG\x00test")
        self.assertEqual(self.get("document", "image.png").json["kind"], "image")
        self.assertEqual(self.client.get('/files/raw/demo?path=image.png').mimetype, "image/png")
        (self.root / "binary.pdf").write_bytes(b"\x00\xff")
        self.assertEqual(self.get("document", "binary.pdf").json["kind"], "download")
        with patch.object(file_access, "MAX_BYTES", 3):
            self.assertEqual(self.get("document", "note.md").status_code, 400)

    def test_unknown_project_and_no_write_routes(self):
        self.assertEqual(self.client.get('/files/api/unknown/list').status_code, 404)
        self.assertEqual(self.client.post('/files/api/demo/document', json={"text": "changed"}).status_code, 405)
        for path in ['/files/', '/files/static/files.js', '/files/static/files.css']:
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)

    def test_remote_access_and_offline_host_never_fall_back(self):
        self.project.host = "macbook"
        host = SimpleNamespace(python="python3", ssh_command=lambda: ["ssh", "macbook"])
        result = SimpleNamespace(returncode=0, stdout=json.dumps({"result": {
            "data": base64.b64encode(b"# Remote").decode(), "size": 8, "modified": 1}}))
        with patch.object(files, "load_hosts", return_value={"macbook": host}), patch.object(files.subprocess, "run", return_value=result) as run:
            response = self.get("document", "remote.md")
            self.assertEqual(response.json["text"], "# Remote")
            payload = json.loads(run.call_args.kwargs["input"])
            self.assertEqual(payload["root"], str(self.root))
            self.assertEqual(run.call_args.args[0][:2], ["ssh", "macbook"])
            run.return_value = SimpleNamespace(returncode=255)
            self.assertEqual(self.get("document", "remote.md").status_code, 503)

    def test_helper_runs_independently_for_remote_hosts(self):
        import subprocess
        import sys
        (self.root / "note.md").write_text("# Remote")
        result = subprocess.run([sys.executable, str(Path(file_access.__file__))],
                                input=json.dumps({"root": str(self.root), "path": "note.md", "action": "read"}),
                                text=True, capture_output=True, check=True)
        self.assertEqual(base64.b64decode(json.loads(result.stdout)["result"]["data"]), b"# Remote")
