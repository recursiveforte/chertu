"""Transcribe Discord recordings before delivering them as user prompts."""

import asyncio
import os
from pathlib import Path
import tempfile
import sys
import wave

import aiohttp
import discord

MAX_BYTES = 25_000_000
MAX_SECONDS = 600
AUDIO_SUFFIXES = {
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".m4a",
    ".wav",
    ".webm",
    ".ogg",
    ".oga",
    ".opus",
    ".flac",
    ".aac",
    ".aiff",
    ".aif",
}
API_URL = "https://api.openai.com/v1/audio/transcriptions"


class TranscriptionError(ValueError):
    """An actionable error safe to show in Discord (never an API response body)."""


def is_audio(attachment):
    return (
        (getattr(attachment, "content_type", None) or "").lower().startswith("audio/")
        or Path(attachment.filename).suffix.lower() in AUDIO_SUFFIXES
        or getattr(attachment, "duration", None) is not None
    )


class Transcriber:
    def __init__(self):
        self.slots = asyncio.Semaphore(2)

    async def transcribe(self, attachment):
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise TranscriptionError(
                "Speech transcription needs OPENAI_API_KEY configured on the bot host."
            )
        if attachment.size > MAX_BYTES:
            raise TranscriptionError("Audio files must be no larger than 25 MB.")
        duration = getattr(attachment, "duration", None)
        if duration is not None and duration > MAX_SECONDS:
            raise TranscriptionError("Please split recordings longer than 10 minutes.")
        async with self.slots:
            try:
                async with asyncio.timeout(240):
                    with tempfile.TemporaryDirectory(prefix="chert-audio-") as folder:
                        source = Path(folder) / "source"
                        await attachment.save(source)
                        if source.stat().st_size > MAX_BYTES:
                            raise TranscriptionError("Audio files must be no larger than 25 MB.")
                        wav = Path(folder) / "speech.wav"
                        await self.decode(source, wav)
                        return await self.request(wav, key)
            except TimeoutError:
                raise TranscriptionError(
                    "Audio transcription timed out. Please try again."
                ) from None
            except (aiohttp.ClientError, discord.HTTPException, OSError):
                raise TranscriptionError(
                    "Could not download or transcribe the audio. Please try again."
                ) from None

    async def decode(self, source, wav):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "chert.discord.audio",
            str(source),
            str(wav),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode == 2:
            raise TranscriptionError("Please split recordings longer than 10 minutes.")
        if process.returncode or not wav.exists():
            raise TranscriptionError("Could not read this audio recording. Try another audio file.")
        # PCM is 16 kHz, mono, 16 bit. Allow the small WAV metadata header.
        if wav.stat().st_size > MAX_SECONDS * 32000 + 4096:
            raise TranscriptionError("Please split recordings longer than 10 minutes.")

    async def request(self, wav, key):
        form = aiohttp.FormData()
        form.add_field("model", os.environ.get("AUDIO_TRANSCRIPTION_MODEL") or "gpt-transcribe")
        form.add_field("response_format", "json")
        with wav.open("rb") as audio:
            form.add_field("file", audio, filename="speech.wav", content_type="audio/wav")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as http:
                async with http.post(
                    API_URL,
                    data=form,
                    headers={"Authorization": f"Bearer {key}"},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        if response.status in {401, 403}:
                            detail = "Check the bot's OpenAI API key and model access."
                        elif response.status == 429:
                            detail = (
                                "OpenAI is rate-limiting requests or the API quota is exhausted."
                            )
                        else:
                            detail = "Please try again or check the configured transcription model."
                        raise TranscriptionError(
                            f"Speech transcription failed (HTTP {response.status}). {detail}"
                        )
                    try:
                        result = await response.json()
                    except (ValueError, aiohttp.ContentTypeError):
                        raise TranscriptionError(
                            "The transcription service returned an invalid response."
                        ) from None
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise TranscriptionError("No speech was recognized. Please try a clearer recording.")
        return text.strip()


class AudioMessage:
    """Keep Discord identity/operations while replacing audio with transcript text."""

    def __init__(self, source, transcripts):
        self.source = source
        self.transcripts = transcripts
        self.attachments = [a for a in source.attachments if not is_audio(a)]
        self.transcript_text = "\n\n".join(
            f"[Voice transcript {index}]\n{text}" for index, text in enumerate(transcripts, 1)
        )
        self.content = "\n\n".join(x for x in (source.content, self.transcript_text) if x)

    def __getattr__(self, name):
        return getattr(self.source, name)

    async def publish(self, channel):
        # Direct sends propagate errors: never silently deliver an invisible transcript.
        for index, text in enumerate(self.transcripts, 1):
            await channel.send(
                f"🎙️ **Voice transcript {index}** · <@{self.author.id}>",
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
            )
            # Escape formatting but preserve all recognized text, including long words.
            text = discord.utils.escape_markdown(text)
            for offset in range(0, len(text), 1900):
                await channel.send(
                    text[offset : offset + 1900],
                    allowed_mentions=discord.AllowedMentions.none(),
                    suppress_embeds=True,
                )

    async def create_thread(self, **kwargs):
        thread = await self.source.create_thread(**kwargs)
        await self.publish(thread)
        return thread


async def prepare_audio(message, transcriber):
    audio = [a for a in message.attachments if is_audio(a)]
    if not audio:
        return message
    # Finish all clips before submitting anything; a failure must not send a partial prompt.
    transcripts = [await transcriber.transcribe(a) for a in audio]
    return AudioMessage(message, transcripts)


def decode_recording(source, destination):
    """Run decoding in a killable worker; never block the Discord gateway loop."""
    import av

    try:
        with av.open(
            source,
            options={
                "protocol_whitelist": "file,pipe",
                "format_whitelist": "wav,ogg,mp3,mov,matroska,webm,flac,aac,aiff,mpeg",
            },
        ) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            samples = 0
            with wave.open(destination, "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)

                def write(frames):
                    nonlocal samples
                    for frame in frames:
                        samples += frame.samples
                        if samples > MAX_SECONDS * 16000:
                            raise TranscriptionError("Recording too long")
                        output.writeframes(bytes(frame.planes[0])[: frame.samples * 2])

                for frame in container.decode(audio=0):
                    write(resampler.resample(frame))
                write(resampler.resample(None))
        return 0 if samples else 1
    except TranscriptionError:
        return 2
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(decode_recording(sys.argv[1], sys.argv[2]))
