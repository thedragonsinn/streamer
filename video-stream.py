#!/usr/bin/env python3

from asyncio import CancelledError, Lock, create_task, run, sleep
from collections import defaultdict
from collections.abc import AsyncIterator
from mimetypes import guess_type
from os import getenv

from aiohttp import web
from dotenv import load_dotenv
from pyrogram import Client

load_dotenv("config.env")

telegram_client = Client(
    name="Streamer",
    api_id=getenv("API_ID"),
    api_hash=getenv("API_HASH"),
    bot_token=getenv("BOT_TOKEN"),
    no_updates=True,
    max_concurrent_transmissions=69,
)


chunk_cache: dict[str, AsyncIterator | None] = defaultdict(lambda: None)

chunk_lock = Lock()

stream_tasks = []


async def get_chunk(link: str, message):
    if chunk_cache[link] is None:
        chunk_cache[link] = aiter(telegram_client.stream_media(message=message))

    try:
        return await anext(chunk_cache[link])
    except StopAsyncIteration:
        del chunk_cache[link]
        return None


async def _video_stream(stream_request) -> web.StreamResponse | web.Response:
    message_link = stream_request.query.get("link")

    print(f"{stream_request} - {message_link}")

    if not message_link:
        return web.Response(status=400, text="Parameter link missing from URL.")

    try:
        message = await telegram_client.get_messages(link=message_link)
    except Exception as e:
        return web.Response(
            status=403, text=f"An error occurred while fetching {message_link} :\n{e}"
        )

    media = message.video or message.document
    media_size = media.file_size or 0

    if not media:
        return web.Response(status=404, text="Link Doesn't Contain a Video.")

    response_headers = {
        "Keep-Alive": "timeout=10",
        "Content-Type": getattr(media, "mime_type", guess_type(media.file_name)[0]),
        "Content-Length": str(media_size),
        "Content-Disposition": f'''inline; filename="{media.file_name or "v.mkv"}"''',
    }

    stream_response = web.StreamResponse(
        status=206 if stream_request.http_range.start else 200,
        reason="OK",
        headers=response_headers,
    )

    await stream_response.prepare(stream_request)

    async with chunk_lock:
        while chunk := await get_chunk(message_link, message):
            await stream_response.write(chunk)
            await sleep(0)

    return stream_response


async def video_stream(stream_request) -> None | web.StreamResponse | web.Response:
    try:
        task = create_task(_video_stream(stream_request))
        stream_tasks.append(task)
        return await task
    except (ConnectionResetError, ConnectionError, CancelledError, KeyboardInterrupt):
        return None


async def init_local_site() -> web.AppRunner:
    app = web.Application()
    app.router.add_get(path="/", handler=video_stream)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner=runner,
        host=getenv("STREAM_URL", "0.0.0.0"),
        port=int(getenv("STREAM_PORT", 8888)),
    )
    await site.start()

    print(f"Usage:\n\n    http://{site._host}:{site._port}/?link=")

    return runner


async def idle():
    try:
        while 1:
            await sleep(300)
    except (CancelledError, KeyboardInterrupt):
        return


async def main():
    await telegram_client.start()
    print("\nTelegram Client Started.\n")
    site_runner = await init_local_site()
    await idle()
    print("\nClosing Site.\n")
    [task.cancel() for task in stream_tasks]
    await site_runner.cleanup()


if __name__ == "__main__":
    run(main(), loop_factory=lambda: telegram_client.loop)
