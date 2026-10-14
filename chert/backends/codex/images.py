"""Deliver images explicitly included in assistant replies to the session's Discord thread."""

import base64
import io
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

import discord

from chert.vendor import bridge as upstream


DELIVERY_CONTEXT = (
    "[Chert delivery context: The user is reading this conversation in Discord. "
    "To send a picture, save it to a file on this session's host and include "
    "![description](/absolute/path/to/image.png) in your reply, outside code blocks. "
    "Chert uploads that file to this Discord thread, including from remote hosts. "
    "PNG, JPEG, GIF and WebP are supported, up to 25 MB each. Public https image "
    "URLs also work. Merely viewing an image, emitting a tool screenshot, or "
    "leaving a browser tab open does not send it to Discord. For a requested "
    "screenshot, save the screenshot and include its image link in your reply.]"
)

# Skip code and escaped syntax; accept angle-bracket paths and balanced parentheses.
_PART = r"(?:[^\s()\\]|\\.|\([^\s()]*\))"
_TOKENS = re.compile(
    r"(?P<code>^ {0,3}(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^ {0,3}(?P=fence)[ \t]*(?:\n|$)"
    r"|^ {0,3}(?:`{3,}|~{3,})[^\n]*\n.*\Z|^(?: {4}|\t)[^\n]*|`+[^`\n]*`+)"
    r"|(?P<image>(?<![\\!])!\[(?P<alt>(?:\\.|[^\]\\\n])*)\]"
    r"\(\s*(?P<path><[^>\n]+>|" + _PART + r"+)"
    r"(?:\s+\"[^\"\n]*\")?\s*\))",
    re.MULTILINE | re.DOTALL,
)


def reply_parts(text):
    """Yield text and explicit image links, preserving order and code examples."""
    start = 0
    for match in _TOKENS.finditer(text):
        if not match.group("image"):
            continue
        path = match["path"].removeprefix("<").removesuffix(">")
        path = re.sub(r"\\([\\() ])", r"\1", path)
        try:
            parsed = urlsplit(path)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.netloc and len(path) <= 2048:
            yield "text", text[start:match.start()]
            yield "url", (match["alt"], path)
        elif parsed.scheme in {"", "sandbox", "file"} and not parsed.netloc:
            local = unquote(parsed.path)
            if Path(local).suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                continue
            yield "text", text[start:match.start()]
            yield "image", (match["alt"], local)
        else:
            continue
        start = match.end()
    yield "text", text[start:]


async def image_file(backend, session, path):
    limit = min(upstream.FILE_LIMIT_BYTES, 25 * 1024 * 1024)
    if backend.remote:
        result = await backend.remote.run("download", path=path, cwd=session.cwd)
        data = base64.b64decode(result["data"], validate=True)
        name = result["name"]
    else:
        source = Path(path).expanduser()
        if not source.is_absolute():
            source = Path(session.cwd) / source
        with source.open("rb") as stream:
            data = stream.read(limit + 1)
        name = source.name
    if len(data) > limit:
        raise ValueError("Image exceeds the Discord upload limit (25 MB).")
    if not (data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
            or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")):
        raise ValueError("The file is not a supported image (PNG, JPEG, GIF or WebP).")
    return discord.File(io.BytesIO(data), filename=name)


async def send_reply(backend, session, key, text):
    channel = await backend.live_channel(session)
    for index, (kind, value) in enumerate(reply_parts(text)):
        checkpoint = f"{key}:part:{index}"
        if checkpoint in session.seen_live_items:
            continue
        if kind == "text":
            if value.strip():
                await backend.say(channel, value)
        elif kind == "url":
            caption, url = value
            embed = discord.Embed(description=caption[:4096] or None)
            embed.set_image(url=url)
            await channel.send(embed=embed, allowed_mentions=upstream.NO_PING)
        else:
            caption, path = value
            try:
                file = await image_file(backend, session, path)
            except (OSError, ValueError) as exc:
                await backend.say(channel, f"⚠️ Could not send image `{Path(path).name}`: {exc}")
            else:
                try:
                    await channel.send(caption[:2000] or None, file=file,
                                       allowed_mentions=upstream.NO_PING)
                finally:
                    file.close()
                    file.fp.close()
        session.seen_live_items = (session.seen_live_items + [checkpoint])[-256:]
        backend.store.save()
