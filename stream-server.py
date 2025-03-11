#!/usr/bin/env python3

from asyncio import CancelledError, Lock, Task, create_task, sleep
from collections import defaultdict
from mimetypes import guess_type
from os import getenv, path

from aiohttp.web import (
    Application,
    AppRunner,
    BaseRequest,
    Response,
    StreamResponse,
    TCPSite,
)
from ub_core import BOT, Config, Message, bot
from ub_core.utils import aio, get_tg_media_details


class Chunk:
    def __init__(self):
        self.lock = Lock()
        self.pending_chunks = None


STREAM_URL = getenv("STREAM_URL")
STREAM_PORT = int(getenv("STREAM_PORT", 0))

STREAM_TASKS: list[Task] = []

CACHE_CHANNEL = int(getenv("CACHE_CHANNEL", 0))

CHUNK_CACHE: dict[str, Chunk] = defaultdict(lambda: Chunk())


async def setup_website():
    app = Application()
    app.router.add_get(path="/", handler=aio.handle_request)
    app.router.add_get(path="/getfile", handler=handle_file_request)
    runner = AppRunner(app)
    await runner.setup()
    site = TCPSite(
        runner=runner,
        host="0.0.0.0",
        port=STREAM_PORT,
        reuse_address=True,
        reuse_port=True,
    )
    await site.start()
    Config.EXIT_TASKS.append(runner.cleanup)
    bot.log.info("Website Started")


async def handle_file_request(stream_request) -> None | StreamResponse | Response:
    try:
        task = create_task(_stream_file(stream_request))
        STREAM_TASKS.append(task)
        return await task
    except (ConnectionResetError, ConnectionError, CancelledError, KeyboardInterrupt):
        return None



async def _stream_file(stream_request: BaseRequest) -> StreamResponse | Response:
    message_link = stream_request.query.get("link")
    requester_ip = stream_request.headers.get("X-Forwarded-For") or stream_request.remote

    await bot.log_text(
        text=f"{stream_request} {requester_ip} - {message_link}", type="info"
    )

    if not message_link:
        return Response(status=400, text="Parameter link missing from URL.")

    try:
        message = await bot.get_messages(link=message_link)
    except Exception as e:
        return Response(
            status=403, text=f"An error occurred while fetching {message_link} :\n{e}"
        )

    media = get_tg_media_details(message=message)

    if not (media or message.story):
        return Response(status=404, text="Link Doesn't Contain a file.")

    media_name = getattr(media, "file_name", None)
    media_size = getattr(media, "file_size", 0)
    media_type = getattr(media, "mime_type", guess_type(media_name)[0])

    response_headers = {
        "Keep-Alive": "timeout=20",
        "Content-Type": media_type,
        "Content-Length": str(media_size),
        "Content-Disposition": f'''attachment; filename="{media_name or "file"}"''',
    }

    stream_response = StreamResponse(
        status=206 if stream_request.http_range.start else 200,
        reason="OK",
        headers=response_headers,
    )

    await stream_response.prepare(stream_request)

    key = f"{requester_ip}-{message_link}"

    async with CHUNK_CACHE[key].lock:
        while chunk := await get_chunk(key=key, message=message):
            await stream_response.write(chunk)
            await sleep(0)

    return stream_response


async def get_chunk(key: str, message: Message):
    cached_chunk = CHUNK_CACHE[key]

    if cached_chunk.pending_chunks is None:
        cached_chunk.pending_chunks = aiter(bot.stream_media(message=message))

    try:
        return await anext(cached_chunk.pending_chunks)
    except StopAsyncIteration:
        del CHUNK_CACHE[key]
        return None


@BOT.add_cmd(cmd="link")
async def get_stream_link(bot: BOT, message: Message):
    replied = message.replied

    if not replied or not replied.media:
        await message.reply(text="Reply to a media file.")
        return

    cached_media = await replied.forward(chat_id=CACHE_CHANNEL)
    stream_url = path.join(STREAM_URL, f"getfile?link={cached_media.link}")

    await message.reply(text=stream_url)


async def clean_tasks():
    [task.cancel() for task in STREAM_TASKS]


async def main():
    if not (CACHE_CHANNEL and STREAM_URL and STREAM_PORT):
        bot.log.error("CACHE_CHANNEL or STREAM_URL or STREAM_PORT var empty.")
        return

    await setup_website()

    Config.EXIT_TASKS.append(clean_tasks)

    await bot.boot()


if __name__ == "__main__":
    bot.run(main())
