import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
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
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".gif"}


@dataclass
class UserSettings:
    video_quality: str = "best"
    add_link: bool = True
    send_as_file: bool = False


def load_settings():
    load_dotenv()
    token = (os.getenv("BOT_TOKEN") or "").strip()
    raw_cache_chat_id = (os.getenv("CACHE_CHAT_ID") or "").strip()
    max_size_bytes = int((os.getenv("MAX_FILE_SIZE_MB") or "49").strip()) * 1024 * 1024
    cookies_file = (os.getenv("COOKIES_FILE") or "").strip() or None
    if not token:
        raise ValueError("BOT_TOKEN is required")
    if not raw_cache_chat_id:
        raise ValueError("CACHE_CHAT_ID is required (example: -1001234567890)")
    try:
        cache_chat_id = int(raw_cache_chat_id)
    except ValueError as e:
        raise ValueError("CACHE_CHAT_ID must be an integer (example: -1001234567890)") from e
    return token, cache_chat_id, max_size_bytes, cookies_file


def extract_url(text: str) -> str | None:
    m = URL_PATTERN.search(text or "")
    if not m:
        return None
    url = m.group(0)
    while url and url[-1] in ").,":
        url = url[:-1]
    return url


def is_supported_source(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in SUPPORTED_DOMAINS)


def get_user_settings(user_settings: dict[int, UserSettings], user_id: int) -> UserSettings:
    settings = user_settings.get(user_id)
    if settings is None:
        settings = UserSettings()
        user_settings[user_id] = settings
    return settings


def build_settings_keyboard(settings: UserSettings) -> InlineKeyboardMarkup:
    quality = settings.video_quality
    add_link = settings.add_link
    send_as_file = settings.send_as_file

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'✅ ' if quality == 'best' else ''}Лучшее",
                    callback_data="qbest",
                ),
                InlineKeyboardButton(
                    text=f"{'✅ ' if quality == '720' else ''}720p",
                    callback_data="q720",
                ),
                InlineKeyboardButton(
                    text=f"{'✅ ' if quality == '480' else ''}480p",
                    callback_data="q480",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✅ ' if add_link else ''}Ссылка: вкл",
                    callback_data="link1",
                ),
                InlineKeyboardButton(
                    text=f"{'✅ ' if not add_link else ''}Ссылка: выкл",
                    callback_data="link0",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✅ ' if not send_as_file else ''}Отправка: видео",
                    callback_data="file0",
                ),
                InlineKeyboardButton(
                    text=f"{'✅ ' if send_as_file else ''}Отправка: файл",
                    callback_data="file1",
                ),
            ],
        ]
    )


def build_settings_text(settings: UserSettings) -> str:
    quality_label = "Лучшее" if settings.video_quality == "best" else f"{settings.video_quality}p"
    return (
        "Текущие настройки:\n"
        f"Качество видео: {quality_label}\n"
        f"Ссылка в подписи: {'вкл' if settings.add_link else 'выкл'}\n"
        f"Отправка видео: {'файлом' if settings.send_as_file else 'как видео'}"
    )


def build_download_options(
    tmp_path: Path,
    max_size_bytes: int,
    cookies_file: str | None,
    video_quality: str,
) -> dict:
    options = {
        "outtmpl": str(tmp_path / "media.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": max_size_bytes,
    }
    ffmpeg_installed = bool(shutil.which("ffmpeg"))
    if ffmpeg_installed:
        if video_quality == "720":
            options["format"] = "bv*[height<=720]+ba/b[height<=720]/best[height<=720]"
        elif video_quality == "480":
            options["format"] = "bv*[height<=480]+ba/b[height<=480]/best[height<=480]"
        else:
            options["format"] = "bv*+ba/best"
        options["merge_output_format"] = "mp4"
    else:
        # Без ffmpeg нельзя склеивать video+audio, берем только уже готовый single-file формат.
        if video_quality == "720":
            options["format"] = "best[height<=720]/best"
        elif video_quality == "480":
            options["format"] = "best[height<=480]/best"
        else:
            options["format"] = "best"
    if cookies_file:
        options["cookiefile"] = cookies_file
    return options


def download_sync(url: str, options: dict) -> Path:
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


def tg_media_kind(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "document"


async def handle_chosen(
    bot: Bot,
    logger: logging.Logger,
    url: str,
    inline_message_id: str,
    cache_chat_id: int,
    max_size_bytes: int,
    cookies_file: str | None,
    user_settings: UserSettings,
) -> None:
    try:
        await bot.edit_message_text(inline_message_id=inline_message_id, text="Скачиваю...")

        with tempfile.TemporaryDirectory(prefix="tgmedia_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            options = build_download_options(
                tmp_path,
                max_size_bytes,
                cookies_file,
                user_settings.video_quality,
            )
            file_path = await asyncio.to_thread(download_sync, url, options)
            media_kind = tg_media_kind(file_path)
            caption = f"Source: {url}"[:1024] if user_settings.add_link and media_kind == "video" else None

            if media_kind == "image":
                sent = await bot.send_photo(cache_chat_id, FSInputFile(file_path))
                media = InputMediaPhoto(media=sent.photo[-1].file_id, caption=caption)
            elif media_kind == "video" and not user_settings.send_as_file:
                sent = await bot.send_video(
                    cache_chat_id,
                    FSInputFile(file_path),
                    supports_streaming=True,
                )
                media = InputMediaVideo(media=sent.video.file_id, caption=caption)
            else:
                sent = await bot.send_document(
                    cache_chat_id,
                    FSInputFile(file_path),
                    disable_content_type_detection=True,
                )
                media = InputMediaDocument(
                    media=sent.document.file_id,
                    caption=caption,
                    disable_content_type_detection=True,
                )

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


async def handle_text_link(
    bot: Bot,
    logger: logging.Logger,
    message: Message,
    url: str,
    max_size_bytes: int,
    cookies_file: str | None,
    user_settings: UserSettings,
) -> None:
    status = await message.answer("Скачиваю...")

    try:
        with tempfile.TemporaryDirectory(prefix="tgmedia_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            options = build_download_options(
                tmp_path,
                max_size_bytes,
                cookies_file,
                user_settings.video_quality,
            )
            file_path = await asyncio.to_thread(download_sync, url, options)
            media_kind = tg_media_kind(file_path)
            caption = f"Source: {url}"[:1024] if user_settings.add_link and media_kind == "video" else None

            if media_kind == "image":
                media = InputMediaPhoto(media=FSInputFile(file_path), caption=caption)
            elif media_kind == "video" and not user_settings.send_as_file:
                media = InputMediaVideo(media=FSInputFile(file_path), caption=caption, supports_streaming=True)
            else:
                media = InputMediaDocument(
                    media=FSInputFile(file_path),
                    caption=caption,
                    disable_content_type_detection=True,
                )

            await bot.edit_message_media(
                chat_id=status.chat.id,
                message_id=status.message_id,
                media=media,
            )
    except DownloadError:
        logger.exception("download failed direct url=%s", url)
        await status.edit_text("Не удалось скачать видео.")
    except TelegramBadRequest:
        logger.exception("telegram bad request direct url=%s", url)
        await status.edit_text("Ошибка Telegram, попробуйте еще раз.")
    except RuntimeError:
        logger.exception("runtime error direct url=%s", url)
        await status.edit_text("Файл не найден после загрузки.")


async def main() -> None:
    token, cache_chat_id, max_size_bytes, cookies_file = load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("tgmedia")
    per_user_settings: dict[int, UserSettings] = {}

    bot = Bot(token)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer("Привет! Отправь мне ссылку на видео и я его скачаю")

    @dp.message(Command("settings"))
    async def on_settings(message: Message) -> None:
        if message.chat.type != "private" or not message.from_user:
            return

        settings = get_user_settings(per_user_settings, message.from_user.id)
        await message.answer(
            build_settings_text(settings),
            reply_markup=build_settings_keyboard(settings),
        )

    @dp.callback_query(F.data == "noop")
    async def on_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @dp.callback_query(
        F.data.startswith("q") | F.data.startswith("link") | F.data.startswith("file")
    )
    async def on_settings_button(callback: CallbackQuery) -> None:
        if not callback.from_user:
            await callback.answer()
            return

        data = callback.data or ""
        settings = get_user_settings(per_user_settings, callback.from_user.id)
        changed = False

        if data.startswith("q"):
            quality = data[1:]
            if quality not in {"best", "720", "480"}:
                await callback.answer("Некорректная команда", show_alert=True)
                return
            changed = settings.video_quality != quality
            settings.video_quality = quality
        elif data.startswith("link"):
            value = data[4:]
            if value not in {"0", "1"}:
                await callback.answer("Некорректная команда", show_alert=True)
                return
            new_value = value == "1"
            changed = settings.add_link != new_value
            settings.add_link = new_value
        elif data.startswith("file"):
            value = data[4:]
            if value not in {"0", "1"}:
                await callback.answer("Некорректная команда", show_alert=True)
                return
            new_value = value == "1"
            changed = settings.send_as_file != new_value
            settings.send_as_file = new_value
        else:
            await callback.answer("Некорректная команда", show_alert=True)
            return

        if callback.message and changed:
            await callback.message.edit_text(
                build_settings_text(settings),
                reply_markup=build_settings_keyboard(settings),
            )
        await callback.answer("Настройки сохранены" if changed else "Уже выбрано")

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

        if not is_supported_source(url):
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

        settings = replace(get_user_settings(per_user_settings, chosen.from_user.id))

        asyncio.create_task(
            handle_chosen(
                bot=bot,
                logger=logger,
                url=url,
                inline_message_id=inline_message_id,
                cache_chat_id=cache_chat_id,
                max_size_bytes=max_size_bytes,
                cookies_file=cookies_file,
                user_settings=settings,
            )
        )

    @dp.message(F.text)
    async def on_text_message(message: Message) -> None:
        if message.chat.type != "private":
            return

        text = message.text or ""
        if text.startswith("/"):
            return

        url = extract_url(text)
        if not url:
            await message.answer("Отправьте ссылку из Instagram, TikTok или YouTube.")
            return

        if not is_supported_source(url):
            await message.answer("Поддерживаются только Instagram, TikTok и YouTube.")
            return

        if not message.from_user:
            return

        settings = replace(get_user_settings(per_user_settings, message.from_user.id))
        await handle_text_link(
            bot=bot,
            logger=logger,
            message=message,
            url=url,
            max_size_bytes=max_size_bytes,
            cookies_file=cookies_file,
            user_settings=settings,
        )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
