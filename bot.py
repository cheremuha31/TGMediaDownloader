import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputTextMessageContent,
    Message,
)
from dotenv import load_dotenv
from yt_dlp.utils import DownloadError

URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
SUPPORTED_DOMAINS = ("instagram.com", "instagr.am", "tiktok.com", "youtube.com", "youtu.be")


def load_settings():
    load_dotenv()
    token = (os.getenv("BOT_TOKEN") or "").strip()
    cache_chat_id = int((os.getenv("CACHE_CHAT_ID") or "").strip())
    max_size_bytes = int((os.getenv("MAX_FILE_SIZE_MB") or "49").strip()) * 1024 * 1024
    cookies_file = (os.getenv("COOKIES_FILE") or "").strip() or None
    if not token:
        raise ValueError("BOT_TOKEN is required")
    return token, cache_chat_id, max_size_bytes, cookies_file


def extract_url(text: str) -> str | None:
    m = URL_PATTERN.search(text or "")
    if not m:
        return None
    url = m.group(0)
    while url and url[-1] in ").,":
        url = url[:-1]
    return url


async def handle_chosen(
    bot: Bot,
    logger: logging.Logger,
    url: str,
    inline_message_id: str,
    cache_chat_id: int,
    max_size_bytes: int,
    cookies_file: str | None,
) -> None:
    try:
        await bot.edit_message_text(inline_message_id=inline_message_id, text="Скачиваю...")

        with tempfile.TemporaryDirectory(prefix="tgmedia_") as tmp_dir:
            tmp_path = Path(tmp_dir)

            options = {
                "outtmpl": str(tmp_path / "media.%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "max_filesize": max_size_bytes,
            }
            if shutil.which("ffmpeg"):
                options["merge_output_format"] = "mp4"
            if cookies_file:
                options["cookiefile"] = cookies_file

            def download_sync():
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if isinstance(info, dict) and info.get("entries"):
                        info = next((x for x in info["entries"] if isinstance(x, dict)), {})
                    if not isinstance(info, dict):
                        info = {}

                    path = None
                    req = info.get("requested_downloads")
                    if isinstance(req, list) and req and isinstance(req[0], dict):
                        path = req[0].get("filepath")
                    if not path:
                        path = ydl.prepare_filename(info)

                    file_path = Path(path)
                    if not file_path.is_file():
                        raise RuntimeError("Downloaded file not found")
                    return file_path

            file_path = await asyncio.to_thread(download_sync)
            ext = file_path.suffix.lower()

            if ext in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
                sent = await bot.send_photo(cache_chat_id, FSInputFile(file_path))
                media = InputMediaPhoto(media=sent.photo[-1].file_id, caption=f"Source: {url}"[:1024])
            elif ext in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".gif"}:
                sent = await bot.send_video(
                    cache_chat_id,
                    FSInputFile(file_path),
                    supports_streaming=True,
                )
                media = InputMediaVideo(media=sent.video.file_id, caption=f"Source: {url}"[:1024])
            else:
                sent = await bot.send_document(cache_chat_id, FSInputFile(file_path))
                media = InputMediaDocument(media=sent.document.file_id, caption=f"Source: {url}"[:1024])

            await bot.edit_message_media(inline_message_id=inline_message_id, media=media)

    except DownloadError:
        logger.exception("download failed url=%s", url)
        await bot.edit_message_text(inline_message_id=inline_message_id, text="Не удалось скачать видео.")
    except TelegramBadRequest:
        logger.exception("telegram bad request url=%s", url)
        await bot.edit_message_text(
            inline_message_id=inline_message_id,
            text="Ошибка Telegram, попробуйте еще раз.",
        )
    except RuntimeError:
        logger.exception("runtime error url=%s", url)
        await bot.edit_message_text(inline_message_id=inline_message_id, text="Файл не найден после загрузки.")


async def main() -> None:
    token, cache_chat_id, max_size_bytes, cookies_file = load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("tgmedia")

    bot = Bot(token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer("Используйте inline: @username <ссылка>")

    @dp.callback_query(F.data == "noop")
    async def on_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @dp.inline_query()
    async def on_inline_query(inline_query: InlineQuery) -> None:
        url = extract_url(inline_query.query or "")

        if not url:
            await inline_query.answer(
                results=[
                    InlineQueryResultArticle(
                        id=uuid.uuid4().hex,
                        title="Ссылка не найдена",
                        description="Вставьте URL",
                        input_message_content=InputTextMessageContent(
                            message_text="Пример: @bot https://youtu.be/..."
                        ),
                    )
                ],
                is_personal=True,
                cache_time=0,
            )
            return

        host = (urlparse(url).hostname or "").lower()
        if not any(host == d or host.endswith(f".{d}") for d in SUPPORTED_DOMAINS):
            await inline_query.answer(
                results=[
                    InlineQueryResultArticle(
                        id=uuid.uuid4().hex,
                        title="Сайт не поддерживается",
                        description="Только Instagram/TikTok/YouTube",
                        input_message_content=InputTextMessageContent(
                            message_text="Эта ссылка не поддерживается."
                        ),
                    )
                ],
                is_personal=True,
                cache_time=0,
            )
            return

        await inline_query.answer(
            results=[
                InlineQueryResultArticle(
                    id=uuid.uuid4().hex,
                    title="Скачать медиа",
                    description=url,
                    input_message_content=InputTextMessageContent(message_text="Скачиваю..."),
                    # Telegram дает inline_message_id только когда есть reply_markup.
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="...", callback_data="noop")]]
                    ),
                )
            ],
            is_personal=True,
            cache_time=0,
        )

    @dp.chosen_inline_result()
    async def on_chosen(chosen: ChosenInlineResult) -> None:
        url = extract_url(chosen.query or "")
        inline_message_id = chosen.inline_message_id
        if not url or not inline_message_id:
            return

        asyncio.create_task(
            handle_chosen(
                bot=bot,
                logger=logger,
                url=url,
                inline_message_id=inline_message_id,
                cache_chat_id=cache_chat_id,
                max_size_bytes=max_size_bytes,
                cookies_file=cookies_file,
            )
        )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
