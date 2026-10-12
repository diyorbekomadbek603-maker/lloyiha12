
import os
import re
import asyncio
import logging
import tempfile
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests
import yt_dlp
from dotenv import load_dotenv

import hashlib
import time

# Telefon uchun xavfsiz yuklama chegarasi
DOWNLOAD_LIMIT = asyncio.Semaphore(2)
SEARCH_LIMIT = asyncio.Semaphore(2)

CACHE_DIR = Path("bot_cache")
MP3_CACHE = CACHE_DIR / "mp3"
MP3_CACHE.mkdir(parents=True, exist_ok=True)

SEARCH_CACHE = {}
SEARCH_CACHE_TTL = 600  # 10 daqiqa

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "8876069295:AAHeIjXmXXQg89gKS8ZxT7-Ote5hbEB5Dcc")
AUDD_TOKEN = os.getenv("AUDD_TOKEN", "8876069295:AAHeIjXmXXQg89gKS8ZxT7-Ote5hbEB5Dcc")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("music_bot")

MAX_SIZE = 45 * 1024 * 1024


# ==========================================
# YORDAMCHI FUNKSIYALAR
# ==========================================

def remove_folder(folder):
    if folder:
        shutil.rmtree(folder, ignore_errors=True)


def get_instagram_url(text):
    match = re.search(r"https?://[^\s]+", text.strip())
    if not match:
        return None

    url = match.group(0).rstrip(".,!?)\"]}")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if (
        parsed.scheme in ("http", "https")
        and (
            host == "instagram.com"
            or host.endswith(".instagram.com")
            or host == "instagr.am"
            or host.endswith(".instagr.am")
        )
    ):
        return url

    return None


def duration_text(seconds):
    if not isinstance(seconds, (int, float)):
        return "Noma'lum"

    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


# ==========================================
# YOUTUBE MUSIQA QIDIRISH
# ==========================================


def search_music(query):
    key = query.strip().lower()
    now = time.time()

    cached = SEARCH_CACHE.get(key)
    if cached and now - cached["time"] < SEARCH_CACHE_TTL:
        return cached["songs"]

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": True,
        "socket_timeout": 12,
        "retries": 1,
        "extractor_retries": 1,
        "cachedir": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        result = ydl.extract_info(
            "ytsearch5:" + query,
            download=False,
        )

    songs = []

    for item in result.get("entries", []):
        if not item or not item.get("id"):
            continue

        songs.append({
            "title": item.get("title", "Noma'lum qo'shiq"),
            "url": (
                "https://www.youtube.com/watch?v="
                + item["id"]
            ),
            "duration": item.get("duration"),
        })

    songs = songs[:5]

    SEARCH_CACHE[key] = {
        "time": now,
        "songs": songs,
    }

    if len(SEARCH_CACHE) > 100:
        SEARCH_CACHE.clear()

    return songs

# ==========================================
# MP3 YUKLASH
# ==========================================


def download_mp3(url, folder=None):
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cached_file = MP3_CACHE / f"{key}.mp3"

    # Oldin yuklangan bo'lsa, qayta yuklamaymiz
    if cached_file.is_file() and cached_file.stat().st_size > 0:
        return str(cached_file)

    work_dir = tempfile.mkdtemp(
        prefix="mp3_",
        dir=str(CACHE_DIR),
    )

    try:
        options = {
            "format": "bestaudio/best",
            "outtmpl": str(Path(work_dir) / "track.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 15,
            "retries": 1,
            "extractor_retries": 1,
            "max_filesize": 45 * 1024 * 1024,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        output = Path(work_dir) / "track.mp3"

        if not output.is_file():
            raise RuntimeError("MP3 fayli yaratilmadi")

        if output.stat().st_size > 45 * 1024 * 1024:
            raise RuntimeError("MP3 fayli juda katta")

        temp_cache = MP3_CACHE / f"{key}.tmp"
        shutil.copy2(output, temp_cache)
        os.replace(temp_cache, cached_file)

        return str(cached_file)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ==========================================
# INSTAGRAM VIDEO
# ==========================================


def download_instagram(url, folder):
    options = {
        "outtmpl": str(Path(folder) / "video.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "retries": 1,
        "fragment_retries": 1,
        "concurrent_fragment_downloads": 2,
        "max_filesize": 45 * 1024 * 1024,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = [
        p for p in Path(folder).glob("video.*")
        if p.suffix.lower() in (
            ".mp4", ".mkv", ".webm", ".mov"
        )
    ]

    if not files:
        raise RuntimeError("Instagram videosi topilmadi")

    if files[0].stat().st_size > 45 * 1024 * 1024:
        raise RuntimeError("Video hajmi juda katta")

    return str(files[0])


def extract_video_audio(video, folder):
    output = str(Path(folder) / "video_audio.mp3")

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video,
            "-vn",
            "-codec:a", "libmp3lame",
            "-b:a", "192k",
            output,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=180,
    )

    if not os.path.isfile(output):
        raise RuntimeError("Audio ajratilmadi")

    if os.path.getsize(output) > MAX_SIZE:
        raise RuntimeError("Audio fayli juda katta")

    return output


# ==========================================
# ORIGINAL MUSIQANI ANIQLASH
# ==========================================

def recognize_music(video, folder):
    if not AUDD_TOKEN:
        return None

    clip = str(Path(folder) / "recognition.mp3")

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", video,
            "-t", "20",
            "-vn",
            "-codec:a", "libmp3lame",
            "-b:a", "128k",
            clip,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=60,
    )

    with open(clip, "rb") as audio:
        response = requests.post(
            "https://api.audd.io/",
            data={"api_token": AUDD_TOKEN},
            files={"file": audio},
            timeout=40,
        )

    response.raise_for_status()
    return response.json().get("result")


# ==========================================
# INLINE TUGMALAR
# ==========================================

def songs_keyboard(songs):
    rows = []

    for i, song in enumerate(songs):
        label = f"{i + 1}. {song['title']}"
        if len(label) > 60:
            label = label[:57] + "..."

        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"music:{i}",
            )
        ])

    return InlineKeyboardMarkup(rows)


def instagram_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🎵 Original qo'shiqni aniqlash",
            callback_data="original",
        )],
        [InlineKeyboardButton(
            "🔊 Video ovozini MP3 qilish",
            callback_data="video_audio",
        )],
        [InlineKeyboardButton(
            "❌ Audio kerak emas",
            callback_data="no_audio",
        )],
    ])


# ==========================================
# START
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎶 Salom!\n\n"
        "📥 Instagram havolasini yuboring.\n"
        "🎵 Qo'shiq yoki ijrochi nomini yozing.\n\n"
        "Musiqa qidiruvida 5 ta natija chiqadi. "
        "Raqam yoki tugma orqali tanlang."
    )


# ==========================================
# INSTAGRAM XABARINI QAYTA ISHLASH
# ==========================================

async def handle_instagram(message, url, context):
    status = await message.reply_text("⏳ Instagram videosi yuklanmoqda...")
    folder = tempfile.mkdtemp(prefix="ig_")

    try:
        video = await asyncio.to_thread(
            download_instagram, url, folder
        )

        context.user_data["ig_folder"] = folder
        context.user_data["ig_video"] = video

        with open(video, "rb") as f:
            await message.reply_video(
                video=f,
                caption="📹 Video tayyor. Audio kerakmi?",
                reply_markup=instagram_keyboard(),
                read_timeout=120,
                write_timeout=120,
            )

        await status.delete()

    except Exception:
        log.exception("Instagram xatosi")
        remove_folder(folder)
        await status.edit_text(
            "❌ Video yuklanmadi. Havola ochiq ekanini tekshiring."
        )


# ==========================================
# MUSIQA QIDIRUV XABARI
# ==========================================

async def handle_text(update, context):
    message = update.effective_message
    text = message.text.strip()

    url = get_instagram_url(text)
    if url:
        await handle_instagram(message, url, context)
        return

    if text in ("1", "2", "3", "4", "5"):
        songs = context.user_data.get("songs", [])
        if songs:
            await send_song(message, context, int(text) - 1)
            return

    if len(text) > 150:
        await message.reply_text("Qidiruv matnini qisqartiring.")
        return

    status = await message.reply_text("🔎 5 ta qo'shiq qidirilmoqda...")

    try:
        songs = await asyncio.to_thread(search_music, text)

        if not songs:
            await status.edit_text("❌ Qo'shiq topilmadi.")
            return

        context.user_data["songs"] = songs

        listing = "\n\n".join(
            f"{i + 1}. {song['title']}"
            for i, song in enumerate(songs)
        )

        await status.edit_text(
            "🎵 Topilgan qo'shiqlar:\n\n"
            + listing
            + "\n\nKerakli qo'shiqni tanlang:",
            reply_markup=songs_keyboard(songs),
        )

    except Exception:
        log.exception("Qidiruv xatosi")
        await status.edit_text(
            "❌ Qidiruv ishlamadi. yt-dlp versiyasi va internetni tekshiring."
        )


# ==========================================
# TANLANGAN MUSIQANI MP3 QILISH
# ==========================================


async def send_song(message, context, index):
    songs = context.user_data.get("songs", [])

    if not 0 <= index < len(songs):
        await message.reply_text("❌ Qo'shiq topilmadi.")
        return

    song = songs[index]
    status = await message.reply_text("🎵 MP3 tayyorlanmoqda...")

    try:
        async with DOWNLOAD_LIMIT:
            path = await asyncio.to_thread(
                download_mp3,
                song["url"],
            )

        with open(path, "rb") as audio:
            await message.reply_audio(
                audio=audio,
                filename=f"{Path(path).stem}.mp3",
                title=song["title"][:64],
                read_timeout=120,
                write_timeout=120,
            )

        await status.delete()

    except Exception:
        log.exception("MP3 yuklash xatosi")
        await status.edit_text(
            "❌ MP3 tayyorlanmadi. Internet, FFmpeg "
            "yoki audio manbasini tekshiring."
        )


# ==========================================
# TUGMA BOSILGANDA
# ==========================================

async def callbacks(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("music:"):
        try:
            index = int(data.split(":")[1])
            await send_song(query.message, context, index)
        except Exception:
            log.exception("Musiqa tugmasi xatosi")
            await query.message.reply_text("❌ Xatolik yuz berdi.")
        return

    video = context.user_data.get("ig_video")
    folder = context.user_data.get("ig_folder")

    if data == "no_audio":
        context.user_data.pop("ig_video", None)
        context.user_data.pop("ig_folder", None)
        remove_folder(folder)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("✅ Audio olinmadi.")
        return

    if not video or not folder or not os.path.isfile(video):
        await query.message.reply_text(
            "❌ Video topilmadi. Havolani qayta yuboring."
        )
        return

    await query.edit_message_reply_markup(reply_markup=None)

    if data == "video_audio":
        status = await query.message.reply_text(
            "⏳ Video ovozi MP3 qilinmoqda..."
        )

        try:
            path = await asyncio.to_thread(
                extract_video_audio, video, folder
            )

            with open(path, "rb") as audio:
                await query.message.reply_audio(
                    audio=audio,
                    filename="video_audio.mp3",
                    title="Video ovozi",
                    read_timeout=120,
                    write_timeout=120,
                )

            await status.delete()

        except Exception:
            log.exception("Video audio xatosi")
            await status.edit_text("❌ Video ovozi olinmadi.")

        finally:
            context.user_data.pop("ig_video", None)
            context.user_data.pop("ig_folder", None)
            remove_folder(folder)

    elif data == "original":
        status = await query.message.reply_text(
            "🔎 Original musiqa aniqlanmoqda..."
        )

        try:
            result = await asyncio.to_thread(
                recognize_music, video, folder
            )

            if not result:
                await status.edit_text(
                    "⚠️ AudD_TOKEN sozlanmagan yoki qo'shiq aniqlanmadi."
                )
            else:
                title = result.get("title", "Noma'lum")
                artist = result.get("artist", "Noma'lum")
                link = result.get("song_link", "")

                rows = []

                if link.startswith("https://"):
                    rows.append([
                        InlineKeyboardButton(
                            "🎧 Musiqa sahifasi",
                            url=link,
                        )
                    ])

                rows.append([
                    InlineKeyboardButton(
                        "🔎 Musiqani qidirish",
                        callback_data="search_again",
                    )
                ])

                await status.edit_text(
                    f"🎵 {title}\n👤 {artist}\n\n"
                    "Qo'shiq aniqlandi. To'liq trek mavjudligi "
                    "audio manbasiga bog'liq.",
                    reply_markup=InlineKeyboardMarkup(rows),
                )

        except Exception:
            log.exception("Original musiqa xatosi")
            await status.edit_text("❌ Musiqa aniqlanmadi.")

        finally:
            context.user_data.pop("ig_video", None)
            context.user_data.pop("ig_folder", None)
            remove_folder(folder)


# ==========================================
# QAYTA QIDIRISH
# ==========================================

async def search_again(update, context):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "🔎 Qo'shiq nomi yoki ijrochi ismini yozing."
    )


# ==========================================
# ISHGA TUSHIRISH
# ==========================================

def main():
    if not TOKEN:
        raise RuntimeError(
            ".env faylida BOT_TOKEN ko'rsatilmagan."
        )

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        CallbackQueryHandler(
            search_again,
            pattern=r"^search_again$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callbacks,
            pattern=r"^(music:\d+|original|video_audio|no_audio)$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    print("🎵 Bot ishga tushdi!")
    app.run_polling()


if __name__ == "__main__":
    main()