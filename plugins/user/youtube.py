import asyncio
import datetime
import os
import re
import tempfile
import time
import random
from pathlib import Path
from typing import List, Optional

from hydrogram import Client, filters
from hydrogram.helpers import ikb
from hydrogram.types import CallbackQuery, Message
from yt_dlp import YoutubeDL

from config import bot, user
from locales import use_lang
from utils import aiowrap, pretty_size, loop
from db import CookieKey

# --- CONFIGURAÇÃO ---
DATA_DIR = Path("data")
COOKIES_PREFIX = "ytdl-cookies"
MAX_FILESIZE = 2000 * 1024 * 1024  # 2GB
COOKIE_BLOCK_MINUTES = 75
MAX_FAILURES = 3

YOUTUBE_REGEX = re.compile(
    r"(?m)http(?:s?):\/\/(?:www\.)?(?:music\.)?youtu(?:be\.com\/(watch\?v=|shorts/)|\.be\/|)([\w\-\_]*)(&(amp;)?[\w\?=]*)?"
)

YDL_OPTIONS = {
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "force_ipv4": True,
    
    # Pausas aleatórias (comportamento humano)
    "sleep_requests": random.randint(1, 3),
    "sleep_interval": random.randint(5, 10),
    "max_sleep_interval": random.randint(30, 60),

    # Extração com clientes alternativos
    "extractor_args": {
        "youtube": {
            "player_client": ["web_safari", "android", "web_embedded"],
            "skip": ["dash", "hls"]
        }
    },
    
    # Headers realistas
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Mode": "navigate"
    },
    
    # JS Runtime (necessário para extração moderna)
    "js_runtimes": {"deno": {"path": "/usr/local/bin/deno"}},
    "remote_components": ["ejs:github"],
}


def is_rate_limit_error(error_str: str) -> bool:
    """Verifica se a mensagem de erro indica rate limit do YouTube"""
    error_lower = error_str.lower()
    return "rate-limited" in error_lower or "try again later" in error_lower


@aiowrap
def extract_info(instance: YoutubeDL, url: str, download=True):
    return instance.extract_info(url, download)


def estimate_sizes(formats):
    """Retorna (audio_size, video_size) em bytes, baseado nos melhores formatos"""
    best_audio = None
    best_video = None
    best_combined = None

    for f in formats:
        vcodec = f.get('vcodec', 'none')
        acodec = f.get('acodec', 'none')
        if vcodec != 'none' and acodec != 'none':
            if best_combined is None or f.get('tbr', 0) > best_combined.get('tbr', 0):
                best_combined = f
        elif acodec != 'none' and vcodec == 'none':
            if best_audio is None or f.get('abr', 0) > best_audio.get('abr', 0):
                best_audio = f
        elif vcodec != 'none':
            if best_video is None or f.get('vbr', 0) > best_video.get('vbr', 0):
                best_video = f

    def get_size(fmt):
        if fmt:
            return fmt.get('filesize') or fmt.get('filesize_approx') or 0
        return 0

    audio_size = 0
    video_size = 0

    if best_combined:
        video_size = get_size(best_combined)
    else:
        if best_video:
            video_size = get_size(best_video)
        if best_audio:
            audio_size = get_size(best_audio)

    return audio_size, video_size


async def get_available_cookies() -> List[Path]:
    """
    Retorna lista de arquivos de cookies válidos (não bloqueados e com falhas < MAX_FAILURES).
    Também remove arquivos que excederam falhas.
    """
    all_files = list(DATA_DIR.glob(f"{COOKIES_PREFIX}*.txt"))
    if not all_files:
        return []

    all_files.sort(key=lambda p: (p.name != f"{COOKIES_PREFIX}.txt", p.name))

    available = []
    for file_path in all_files:
        filename = file_path.name
        record = await CookieKey.get_or_none(filename=filename)
        
        if record:
            # Se está bloqueado e ainda não expirou, ignorar
            if record.blocked_until and record.blocked_until > datetime.datetime.now():
                continue
            # Se falhas >= MAX_FAILURES, deletar arquivo e remover registro
            if record.failures >= MAX_FAILURES:
                try:
                    file_path.unlink()
                except Exception:
                    pass
                await record.delete()
                continue
            # Se expirou o bloqueio, limpar o bloqueio
            if record.blocked_until and record.blocked_until <= datetime.datetime.now():
                record.blocked_until = None
                await record.save()
        else:
            # Novo cookie, criar registro inicial
            record = await CookieKey.create(
                filename=filename,
                failures=0,
                blocked_until=None
            )
        available.append(file_path)
    
    return available


async def mark_cookie_failure(filename: str):
    """Registra uma falha para o cookie e aplica bloqueio."""
    record = await CookieKey.get_or_none(filename=filename)
    if not record:
        record = await CookieKey.create(filename=filename)
    
    record.failures += 1
    record.last_fail_time = datetime.datetime.now()
    record.blocked_until = datetime.datetime.now() + datetime.timedelta(minutes=COOKIE_BLOCK_MINUTES)
    await record.save()
    
    # Se atingiu o limite, deletar o arquivo agora (será removido na próxima listagem)
    if record.failures >= MAX_FAILURES:
        file_path = DATA_DIR / filename
        try:
            file_path.unlink()
        except Exception:
            pass
        await record.delete()


async def try_cookie_operation(url: str, download: bool = False, opts: dict = None) -> tuple:
    """
    Tenta executar uma operação yt-dlp (extract_info ou download) usando cookies disponíveis.
    Retorna (resultado, ydl_instance, cookie_filename) ou levanta exceção se todos falharem.
    """
    cookies = await get_available_cookies()
    if not cookies:
        raise Exception("Nenhum cookie disponível para uso.")
    
    last_error = None
    for cookie_path in cookies:
        cookie_filename = cookie_path.name
        opts_copy = (opts or YDL_OPTIONS).copy()
        opts_copy["cookiefile"] = str(cookie_path)
        # Adiciona rate limit aleatório entre 3 e 5 MB/s
        opts_copy["ratelimit"] = random.randint(3 * 1024 * 1024, 5 * 1024 * 1024)
        
        try:
            with YoutubeDL(opts_copy) as ydl:
                if download:
                    info = await extract_info(ydl, url, download=True)
                    return (info, ydl, cookie_filename)
                else:
                    res = await extract_info(ydl, url, download=False)
                    return (res, ydl, cookie_filename)
        except Exception as e:
            err_msg = str(e)
            if is_rate_limit_error(err_msg):
                await mark_cookie_failure(cookie_filename)
                last_error = e
                continue
            else:
                # Outros erros não são tratados como falha de cookie
                raise e
    
    raise last_error or Exception("Todos os cookies falharam (rate limit).")


@Client.on_message(filters.command("ytdl", prefixes=".") & filters.sudoers)
@use_lang()
async def ytdlcmd(c: Client, m: Message, strings):
    # Extrai o alvo
    if m.reply_to_message and (m.reply_to_message.text or m.reply_to_message.caption):
        target = m.reply_to_message.text or m.reply_to_message.caption
    elif len(m.command) > 1:
        target = m.text.split(None, 1)[1]
    else:
        return await m.reply(strings("ytdl_missing_argument"))

    match = YOUTUBE_REGEX.search(target)
    url = match.group() if match else f"ytsearch1:{target}"

    try:
        res, ydl, used_cookie = await try_cookie_operation(url, download=False)

        if "entries" in res:
            if not res["entries"]:
                return await m.reply(strings("ytdl_not_found"))
            yt = res["entries"][0]
        else:
            yt = res

        formats = yt.get("formats", [])
        audio_size, video_size = estimate_sizes(formats)

    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = str(e) or "Erro desconhecido na extração."
        clean_err = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', err_msg)
        if is_rate_limit_error(clean_err):
            return await m.reply(strings("ytdl_timeout"))
        return await m.reply(strings("ytdl_search_error").format(error=clean_err[:200]))

    size_parts = []
    if audio_size > 0:
        size_parts.append(f"{pretty_size(audio_size)}")
    if video_size > 0:
        size_parts.append(f"{pretty_size(video_size)}")

    duration = datetime.timedelta(seconds=yt.get('duration', 0))

    text = f"🎧 <b>{yt.get('uploader', 'YouTube')}</b> - <i>{yt.get('title', 'Video')}</i>\n"
    if size_parts:
        text += "💾 " + " / ".join(size_parts) + "\n"
    text += f"⏳ <code>{duration}</code>"

    keyboard = [[
        (strings("ytdl_audio_button"), f'_aud|{yt["id"]}|{audio_size}|{m.chat.id}|{m.id}'),
        (strings("ytdl_video_button"), f'_vid|{yt["id"]}|{video_size}|{m.chat.id}|{m.id}'),
    ]]

    await m.reply(text, reply_markup=ikb(keyboard))


@bot.on_callback_query(filters.regex("^(_(vid|aud))") & filters.sudoers)
@use_lang()
async def cli_ytdl(c: Client, cq: CallbackQuery, strings):
    try:
        kind, vid_id, fsize, cid, mid = cq.data.split("|")
        fsize, cid, mid = int(fsize), int(cid), int(mid)
    except Exception:
        return await cq.answer(strings("ytdl_video_error"), show_alert=True)

    if fsize > MAX_FILESIZE:
        return await cq.answer(
            strings("ytdl_file_too_big").format(size=pretty_size(MAX_FILESIZE)),
            show_alert=True
        )

    await cq.edit_message_text(strings("ytdl_downloading"))

    with tempfile.TemporaryDirectory() as tempdir:
        path = Path(tempdir)
        opts = YDL_OPTIONS.copy()
        opts.update({
            "outtmpl": f"{path}/%(title)s.%(ext)s",
        })

        if kind == "_vid":
            opts.update({
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "postprocessors": [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }],
            })
        else:
            opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })

        # Controle de progresso
        last_update = 0
        start_time = time.time()

        def progress_hook(d):
            nonlocal last_update
            if d['status'] == 'downloading':
                now = time.time()
                if now - last_update >= 1:
                    last_update = now
                    total = d.get('total_bytes') or d.get('total_bytes_estimate')
                    downloaded = d.get('downloaded_bytes', 0)
                    if total:
                        asyncio.run_coroutine_threadsafe(
                            update_progress(cq, strings, downloaded, total, now - start_time),
                            loop
                        )

        opts["progress_hooks"] = [progress_hook]

        try:
            info, ydl, used_cookie = await try_cookie_operation(
                f"https://www.youtube.com/watch?v={vid_id}",
                download=True,
                opts=opts
            )

            base_filename = ydl.prepare_filename(info)

            # Localiza o arquivo final após conversão
            final_file = None
            possible_exts = ['.mp4', '.mp3', '.m4a', '.webm', '.mkv', '.opus']
            base, _ = os.path.splitext(base_filename)
            for ext in possible_exts:
                test_path = base + ext
                if os.path.exists(test_path):
                    final_file = test_path
                    break
            if final_file is None:
                final_file = base_filename

            await cq.edit_message_text(strings("ytdl_sending"))

            if kind == "_vid":
                await user.send_video(
                    chat_id=cid,
                    video=final_file,
                    caption=info.get("title"),
                    duration=info.get("duration", 0),
                    reply_to_message_id=mid
                )
            else:
                await user.send_audio(
                    chat_id=cid,
                    audio=final_file,
                    title=info.get("title"),
                    performer=info.get("uploader"),
                    duration=info.get("duration", 0),
                    reply_to_message_id=mid
                )
            await cq.edit_message_text(strings("ytdl_sent"))
        except Exception as e:
            import traceback
            traceback.print_exc()
            err_msg = str(e) or "Erro no download/processamento."
            clean_err = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', err_msg)
            if is_rate_limit_error(clean_err):
                await cq.edit_message_text(strings("ytdl_timeout"))
            else:
                await cq.edit_message_text(strings("ytdl_send_error").format(e=clean_err[:400]))


async def update_progress(cq: CallbackQuery, strings, current, total, elapsed):
    percent = current / total * 100
    bar_length = 10
    filled = int(bar_length * current // total)
    bar = '▰' * filled + '▱' * (bar_length - filled)
    speed = current / elapsed if elapsed > 0 else 0
    text = strings("ytdl_progress_downloading").format(
        bar=bar,
        percent=percent,
        current=pretty_size(current),
        total=pretty_size(total),
        speed=pretty_size(speed)
    )
    try:
        await cq.edit_message_text(text)
    except Exception:
        pass