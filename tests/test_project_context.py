import shlex
import unittest
from unittest.mock import patch

from chert.backends.codex.project_context import project_context
from chert.vendor import bridge as upstream


class ProjectContextTests(unittest.TestCase):
    def test_local_helper_honors_gateway_configuration_without_secret_contents(self):
        with patch.object(upstream, "HOOK_PORT", 9123), patch.object(
            upstream, "HOOK_SECRET_FILE", "/tmp/private dir/hook.secret"
        ):
            context = project_context("local")
        command = context.split("`", 2)[1]
        argv = shlex.split(command)
        self.assertEqual(argv[-2:], ["create", "NAME"])
        self.assertEqual(argv[argv.index("--port") + 1], "9123")
        self.assertEqual(argv[argv.index("--secret-file") + 1], "/tmp/private dir/hook.secret")
        self.assertTrue(argv[1].endswith("/bin/chert-project"))

    def test_remote_context_does_not_offer_gateway_local_paths(self):
        context = project_context("macbook")
        self.assertIn("macbook", context)
        self.assertIn("/project name:NAME", context)
        self.assertNotIn("/bin/chert-project", context)
