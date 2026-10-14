import base64
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

import test_backend_parity as fixtures
from chert.backends.codex.images import reply_parts
from chert.vendor import bridge as upstream


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9uoAAAAASUVORK5CYII="
)


class ImageParsingTests(unittest.TestCase):
    def test_paths_and_web_links(self):
        text = ('Before ![first](sandbox:/tmp/a%20b.png) between '
                '![second](</tmp/pic (2).jpg>) and ![web](https://example.com/p.png) after')
        parts = list(reply_parts(text))
        self.assertEqual([v for k, v in parts if k == "image"],
                         [("first", "/tmp/a b.png"), ("second", "/tmp/pic (2).jpg")])
        self.assertIn(("url", ("web", "https://example.com/p.png")), parts)

    def test_code_examples_ordinary_links_and_non_images_are_not_uploaded(self):
        for source in ('`![x](/tmp/x.png)`', '```md\n![x](/tmp/x.png)\n```',
                       '~~~\n![x](/tmp/x.png)\n~~~', '```\n![x](/tmp/x.png)',
                       '    ![x](/tmp/x.png)', r'\![x](/tmp/x.png)',
                       '[x](/tmp/x.png)', '![x](/tmp/secrets.txt)',
                       '![x](file://another-host/tmp/x.png)'):
            with self.subTest(source=source):
                self.assertEqual(list(reply_parts(source)), [("text", source)])


class ImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.BackendParityTests.asyncSetUp
    asyncTearDown = fixtures.BackendParityTests.asyncTearDown

    async def reply(self, text, identifier="reply"):
        await self.adapter.events.handle_live_event({"method": "item/completed", "params": {
            "threadId": self.session.codex_thread, "turnId": "turn",
            "item": {"id": identifier, "type": "agentMessage", "text": text},
        }})

    async def test_local_reply_uploads_actual_bytes_and_deduplicates_replayed_events(self):
        path = Path(self.tmp.name) / "picture.png"
        path.write_bytes(PNG)
        sent = []

        async def upload(caption, **kwargs):
            file = kwargs["file"]
            sent.append((caption, file.filename, file.fp.read(), file))

        self.channel.send.side_effect = upload
        text = f"Here it is.\n![Requested picture]({path})"
        await self.reply(text)
        await self.reply(text)
        self.assertEqual([(c, n, d) for c, n, d, _ in sent], [("Requested picture", "picture.png", PNG)])
        self.assertTrue(sent[0][3].fp.closed)
        self.adapter.say.assert_awaited_once_with(self.channel, "Here it is.\n")

    async def test_remote_reply_fetches_only_from_session_host(self):
        self.adapter.remote = SimpleNamespace(run=AsyncMock(return_value={
            "name": "picture.png", "data": base64.b64encode(PNG).decode(),
        }), close=AsyncMock())
        captured = []

        async def upload(*args, **kwargs):
            captured.append(kwargs["file"].fp.read())

        self.channel.send.side_effect = upload
        original_open = Path.open

        def checked_open(path, *args, **kwargs):
            self.assertNotEqual(str(path), "/tmp/picture.png", "Read image from wrong host")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", checked_open):
            await self.reply("![Picture](/tmp/picture.png)")
        self.adapter.remote.run.assert_awaited_once_with(
            "download", path="/tmp/picture.png", cwd=self.session.cwd)
        self.assertEqual(captured, [PNG])

    async def test_missing_invalid_and_oversized_files_report_failure(self):
        path = Path(self.tmp.name) / "bad.png"
        for index, contents in enumerate((None, b"not an image", PNG)):
            if contents is not None:
                path.write_bytes(contents)
            with patch.object(upstream, "FILE_LIMIT_BYTES", 10):
                await self.reply(f"![Picture]({path})", str(index))
        self.channel.send.assert_not_called()
        self.assertEqual(self.adapter.say.await_count, 3)
        self.assertTrue(all("Could not send image" in c.args[1] for c in self.adapter.say.call_args_list))

    async def test_discord_retry_does_not_repeat_already_sent_text_or_images(self):
        path = Path(self.tmp.name) / "picture.png"
        path.write_bytes(PNG)
        error = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "retry")
        self.channel.send.side_effect = [None, error, None]
        text = f"Intro\n![first]({path})\n![second]({path})"
        with self.assertRaises(discord.HTTPException):
            await self.reply(text)
        await self.reply(text)
        self.adapter.say.assert_awaited_once_with(self.channel, "Intro\n")
        self.assertEqual([c.args[0] for c in self.channel.send.call_args_list], ["first", "second", "second"])

    async def test_muted_reply_does_not_upload(self):
        self.session.muted = True
        await self.reply("![Picture](/tmp/picture.png)")
        self.channel.send.assert_not_called()

    async def test_public_image_url_gets_an_explicit_discord_embed(self):
        await self.reply("![Product](https://example.com/product.jpg)")
        self.channel.send.assert_awaited_once()
        embed = self.channel.send.call_args.kwargs["embed"]
        self.assertEqual(embed.image.url, "https://example.com/product.jpg")
        self.assertEqual(embed.description, "Product")

    async def test_incidental_mcp_screenshots_are_not_uploaded(self):
        await self.adapter.events.handle_live_event({"method": "item/completed", "params": {
            "threadId": self.session.codex_thread, "turnId": "turn",
            "item": {"id": "tool", "type": "mcpToolCall", "tool": "screenshot", "result": {
                "content": [{"type": "image", "mimeType": "image/png",
                             "data": base64.b64encode(PNG).decode()}],
            }},
        }})
        self.channel.send.assert_not_called()
