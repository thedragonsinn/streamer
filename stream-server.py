#!/usr/bin/env python3

from asyncio import CancelledError
from functools import wraps
from mimetypes import guess_type
from os import getenv, path
from urllib.parse import quote, unquote

from aiohttp.web import BaseRequest, Response, StreamResponse
from pyrogram.enums import ParseMode
from pyrogram.errors import FileReferenceExpired
from pyrogram.utils import FileId
from ub_core import BOT, Message, bot
from ub_core.utils import aio, get_filename_from_mime, get_tg_media_details

STREAM_URL = getenv("STREAM_URL")
STREAM_PORT = int(getenv("API_PORT", 0))
CACHE_CHANNEL = int(getenv("CACHE_CHANNEL", 0))


def handle_errors(function):

    @wraps(function)
    async def inner(stream_request):

        try:
            return await function(stream_request)
        except (ConnectionResetError, ConnectionError, CancelledError, KeyboardInterrupt):
            return None
        except Exception as e:
            bot.log.exception(e)
            return Response(status=403, text=f"An error occurred:\n{e}")

    return inner


async def _stream_file(
    stream_request: BaseRequest,
    media_file_id_object: FileId,
    media_type: str,
    media_size: int,
    media_name: str,
):

    response_headers = {
        "Keep-Alive": "timeout=20",
        "Content-Type": media_type,
        "Content-Length": str(media_size),
        "Content-Disposition": f'attachment; filename="{media_name or "file"}"',
    }

    offset = 0

    if start_range := stream_request.http_range.start:
        response_headers["Accept-Ranges"] = "bytes"
        response_headers["Content-Range"] = f"bytes {start_range}-{media_size-1}/{media_size}"
        offset += start_range // (1024 * 1024)

    stream_response = StreamResponse(
        status=206 if start_range else 200, reason="OK", headers=response_headers
    )

    await stream_response.prepare(stream_request)

    async for chunk in bot.get_file(
        file_id=media_file_id_object, file_size=media_size, offset=offset
    ):
        await stream_response.write(chunk)

    return stream_response


@handle_errors
async def stream_file_via_link(stream_request: BaseRequest) -> StreamResponse | Response:
    message_link = stream_request.query.get("link")
    requester_ip = stream_request.headers.get("X-Forwarded-For") or stream_request.remote

    await bot.log_text(
        text=f"{stream_request} {requester_ip} - {message_link}",
        type="info",
        parse_mode=ParseMode.DISABLED,
    )

    if not message_link:
        return Response(status=400, text="Parameter link missing from URL.")

    try:
        message = await bot.get_messages(link=message_link)
    except Exception as e:
        return Response(status=403, text=f"An error occurred while fetching {message_link} :\n{e}")

    media = get_tg_media_details(message)

    if not media:
        return Response(status=404, text="Link Doesn't Contain a media.")

    media_file_id_object = FileId.decode(media.file_id)
    media_name = media.file_name
    media_size = getattr(media, "file_size", 0)
    media_type = getattr(media, "mime_type", None) or guess_type(media_name)[0]

    return await _stream_file(
        stream_request, media_file_id_object, media_type, media_size, media_name
    )


@handle_errors
async def stream_file_via_file_id(stream_request: BaseRequest):
    params = stream_request.query

    file_id = unquote(params.get("file_id"))
    media_name = unquote(params.get("media_name"))
    media_size = int(params.get("media_size"))
    media_type = unquote(params.get("media_type", guess_type(media_name)[0]))

    requester_ip = stream_request.headers.get("X-Forwarded-For") or stream_request.remote

    await bot.log_text(
        text=f"{stream_request}\n\nIP: {requester_ip}\n\nFile ID: {file_id}\n\nFile Name: {media_name}\n\nFile Size: {media_size}\n\nFile Type: {media_type}\n",
        type="info",
        parse_mode=ParseMode.DISABLED,
    )

    media_file_id_object = FileId.decode(file_id)

    return await _stream_file(
        stream_request, media_file_id_object, media_type, media_size, media_name
    )


@BOT.add_cmd(cmd="link")
async def get_stream_link(bot: BOT, message: Message):
    """
    CMD: link
    INFO: Get a Direct download link for a telegram media
    FLAGS:
        -f: forward the media to channel
    """
    replied = message.replied

    if not replied or not replied.media:
        await message.reply(text="Reply to a media file.")
        return

    if "-f" in message.flags:
        cached_media = await replied.forward(chat_id=CACHE_CHANNEL)
        query = f"link={cached_media.link}"
        endpoint = "getvialink"
    else:
        endpoint = "getviaid"
        media = get_tg_media_details(replied)
        file_id = quote(media.file_id)
        media_size = getattr(media, "file_size", 0)
        media_type = quote(getattr(media, "mime_type", "") or guess_type(media_name)[0], safe="")
        media_name = quote(media.file_name)
        query = f"file_id={file_id}&media_size={media_size}&media_type={media_type}&media_name={media_name}"

    await message.reply(text=path.join(STREAM_URL, f"{endpoint}?{query}"))


async def main():
    if not (CACHE_CHANNEL and STREAM_URL and STREAM_PORT):
        bot.log.error("CACHE_CHANNEL or STREAM_URL or STREAM_PORT var empty.")
        return

    aio.server.add_route(
        method="GET", path="/getvialink", handler=stream_file_via_link, name="GetFileLinkHandler"
    )
    aio.server.add_route(
        method="GET", path="/getviaid", handler=stream_file_via_file_id, name="GetFileIDHandler"
    )

    await bot.boot()


if __name__ == "__main__":
    bot.run(main())
