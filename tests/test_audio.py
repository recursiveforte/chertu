import os
from pathlib import Path
import wave
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import discord
from aiohttp import web

from chert.discord.audio import AudioMessage, Transcriber, TranscriptionError, is_audio
from chert.backends.codex.state import Session, SessionStore
from chert.config import Config
from support import make_frontend


def attachment(name="voice-message.ogg", **kwargs):
    return SimpleNamespace(
        filename=name, size=100, content_type=None, duration=None, save=AsyncMock(), **kwargs
    )


class AudioRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = Config("test", 1, 7, set(), root, root / "state.json")
        self.bot = make_frontend(
            config, SimpleNamespace(binary="codex"), SessionStore(config.state_file)
        )
        self.bot._connection.user = SimpleNamespace(id=99)
        self.bot.say = AsyncMock()
        self.bot.transcriber.transcribe = AsyncMock(return_value="Please fix the tests.")
        self.bot.codex.store.sessions[300] = Session(300, str(root), "session")
        self.thread = SimpleNamespace(id=300, parent_id=100, send=AsyncMock())
        self.source = SimpleNamespace(
            id=500,
            content="",
            channel=self.thread,
            author=SimpleNamespace(id=7),
            webhook_id=None,
            attachments=[attachment()],
            add_reaction=AsyncMock(),
            create_thread=AsyncMock(return_value=self.thread),
        )
        self.bot.codex.send_prompt = AsyncMock(return_value="👀")

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    async def test_voice_reply_is_published_before_prompt_and_never_a_command(self):
        self.bot.transcriber.transcribe.return_value = "!kill @everyone"

        async def submit(channel, text, *, source=None):
            self.assertEqual(self.thread.send.await_count, 2)
            self.assertEqual(text, "[Voice transcript 1]\n!kill @everyone")
            return "👀"

        self.bot.codex.send_prompt.side_effect = submit
        await self.bot.on_message(self.source)
        self.bot.codex.send_prompt.assert_awaited_once()
        for call in self.thread.send.call_args_list:
            self.assertEqual(
                call.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict()
            )

    async def test_new_project_audio_echoes_in_created_thread(self):
        self.source.channel = self.bot.project_channels[100]
        self.source.content = "Please also explain it."

        async def launch(prompt, *, source_message, cwd):
            self.assertIn("Please also explain it.", prompt)
            self.assertIn("Please fix the tests.", prompt)
            return await source_message.create_thread(name="test")

        self.bot.codex.start_session = AsyncMock(side_effect=launch)
        await self.bot.on_message(self.source)
        self.bot.codex.start_session.assert_awaited_once()
        self.source.create_thread.assert_awaited_once_with(name="test")
        self.assertEqual(self.thread.send.await_count, 2)

    async def test_mixed_attachments_keep_image_path(self):
        picture = attachment("diagram.png")
        self.source.attachments.append(picture)

        async def saved(msg):
            self.assertEqual(msg.attachments, [picture])
            return "[attachment: diagram.png]"

        self.bot.save_attachments = AsyncMock(side_effect=saved)
        self.source.content = "Context"
        await self.bot.on_message(self.source)
        prompt = self.bot.codex.send_prompt.call_args.args[1]
        self.assertIn("Context", prompt)
        self.assertIn("Please fix the tests.", prompt)
        self.assertIn("[attachment: diagram.png]", prompt)

    async def test_failed_clip_does_not_deliver_partial_prompt(self):
        self.source.attachments.append(attachment("second.mp3"))
        self.bot.transcriber.transcribe.side_effect = ["First", TranscriptionError("Try again")]
        await self.bot.on_message(self.source)
        self.bot.codex.send_prompt.assert_not_called()
        self.thread.send.assert_not_called()
        self.assertIn("Try again", self.bot.say.call_args.args[1])

    async def test_failed_transcript_publication_does_not_submit(self):
        self.thread.send.side_effect = RuntimeError("Discord unavailable")
        await self.bot.on_message(self.source)
        self.bot.codex.send_prompt.assert_not_called()

    async def test_unauthorized_archived_untracked_and_commands_do_not_transcribe(self):
        self.source.author.id = 8
        await self.bot.on_message(self.source)
        self.source.author.id = 7
        project = self.bot.projects.projects["primary"]
        project.archived = True
        await self.bot.on_message(self.source)
        project.archived = False
        self.source.channel = SimpleNamespace(id=301, parent_id=100)
        await self.bot.on_message(self.source)
        self.source.channel = self.thread
        self.source.content = "!screen"
        self.bot.codex.controls.execute = AsyncMock()
        await self.bot.on_message(self.source)
        self.bot.transcriber.transcribe.assert_not_called()

    async def test_multiple_long_transcripts_are_complete(self):
        transcripts = ["a" * 6200, "Second recording."]
        prepared = AudioMessage(self.source, transcripts)
        await prepared.publish(self.thread)
        chunks = [c.args[0] for c in self.thread.send.call_args_list]
        self.assertEqual("".join(chunks[1:5]), transcripts[0])
        self.assertEqual(chunks[-1], transcripts[1])
        self.assertTrue(all(len(chunk) <= 2000 for chunk in chunks))


class TranscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_detection(self):
        for name in ["voice.OGG", "voice.opus", "note.m4a", "speech.flac"]:
            self.assertTrue(is_audio(attachment(name)))
        self.assertFalse(is_audio(attachment("image.png")))
        audio = attachment("unknown")
        audio.content_type = "audio/ogg; codecs=opus"
        self.assertTrue(is_audio(audio))

    async def test_missing_key_and_limits_prevent_download(self):
        transcriber = Transcriber()
        clip = attachment()
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesRegex(TranscriptionError, "OPENAI_API_KEY"):
                await transcriber.transcribe(clip)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            clip.size = 25_000_001
            with self.assertRaisesRegex(TranscriptionError, "25 MB"):
                await transcriber.transcribe(clip)
            clip.size, clip.duration = 100, 601
            with self.assertRaisesRegex(TranscriptionError, "10 minutes"):
                await transcriber.transcribe(clip)
        clip.save.assert_not_called()

    async def test_timeout_cleans_up_temporary_audio(self):
        transcriber = Transcriber()
        paths = []

        async def save(path):
            paths.append(path)
            path.write_bytes(b"audio")

        clip = attachment()
        clip.save.side_effect = save
        transcriber.decode = AsyncMock(side_effect=TimeoutError)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            with self.assertRaisesRegex(TranscriptionError, "timed out"):
                await transcriber.transcribe(clip)
        self.assertFalse(paths[0].parent.exists())

    async def test_multipart_api_success_errors_and_empty_speech(self):
        status, body = 200, {"text": " Real transcript. "}
        captured = {}

        async def handle(request):
            captured["authorization"] = request.headers["Authorization"]
            data = await request.post()
            captured.update(model=data["model"], audio=data["file"].file.read())
            return web.json_response(body, status=status)

        app = web.Application()
        app.router.add_post("/transcriptions", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as folder:
                wav = Path(folder) / "speech.wav"
                wav.write_bytes(b"wave bytes")
                with (
                    patch("chert.discord.audio.API_URL", f"http://127.0.0.1:{port}/transcriptions"),
                    patch.dict(os.environ, {"AUDIO_TRANSCRIPTION_MODEL": ""}),
                ):
                    self.assertEqual(
                        await Transcriber().request(wav, "test-key"), "Real transcript."
                    )
                    self.assertEqual(
                        captured,
                        dict(
                            authorization="Bearer test-key",
                            model="gpt-transcribe",
                            audio=b"wave bytes",
                        ),
                    )
                    for status in [401, 403, 429, 500]:
                        body = {"error": "private provider information"}
                        with self.assertRaises(TranscriptionError) as caught:
                            await Transcriber().request(wav, "test-key")
                        self.assertNotIn("private", str(caught.exception))
                    status, body = 200, {"text": " "}
                    with self.assertRaisesRegex(TranscriptionError, "No speech"):
                        await Transcriber().request(wav, "test-key")
        finally:
            await runner.cleanup()

    async def test_real_discord_ogg_decoding_and_invalid_audio(self):
        import av

        with tempfile.TemporaryDirectory() as folder:
            source, wav = Path(folder) / "voice.ogg", Path(folder) / "speech.wav"
            with av.open(str(source), "w", format="ogg") as container:
                stream = container.add_stream("libopus", rate=48000)
                stream.layout = "mono"
                frame = av.AudioFrame(format="s16", layout="mono", samples=9600)
                frame.sample_rate = 48000
                frame.planes[0].update(bytes(frame.planes[0].buffer_size))
                for packet in stream.encode(frame):
                    container.mux(packet)
                for packet in stream.encode(None):
                    container.mux(packet)
            await Transcriber().decode(source, wav)
            with wave.open(str(wav)) as audio:
                self.assertEqual(audio.getframerate(), 16000)
                self.assertEqual(audio.getnchannels(), 1)
                self.assertGreater(audio.getnframes(), 3000)
            source.write_bytes(b"not audio")
            with self.assertRaisesRegex(TranscriptionError, "Could not read"):
                await Transcriber().decode(source, wav)
