import os
import re
import shutil
import asyncio
from pathlib import Path
from hydrogram import Client, filters
from hydrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from config import user, bot
from db import Message as DBMessage
from locales import use_lang

MIN_INTERVAL = 3  # segundos mínimos entre envios


# ---------- utilitários ----------

def parse_interval(text: str) -> int:
    pattern = r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?'
    match = re.fullmatch(pattern, text.strip())
    if not match:
        raise ValueError
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    total = h * 3600 + m * 60 + s
    if total < MIN_INTERVAL:
        raise ValueError
    return total


def format_seconds(seconds: int) -> str:
    if seconds == 0:
        return "0 segundos"
    parts = []
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        parts.append(f"{hours} hora{'s' if hours > 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minuto{'s' if minutes > 1 else ''}")
    if secs:
        parts.append(f"{secs} segundo{'s' if secs > 1 else ''}")
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " e " + parts[-1]
    return parts[0]


def natural_sort_key(name: str):
    # Quebra o nome em pedaços de dígitos e não-dígitos, convertendo os
    # pedaços numéricos para int. Assim "arquivo2" < "arquivo10"
    # (comparação por valor numérico), em vez de "arquivo10" < "arquivo2"
    # (comparação lexicográfica, onde '1' < '2' caractere a caractere).
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r'(\d+)', name)
    ]


def list_files(folder_path: str):
    entries = sorted(os.listdir(folder_path), key=natural_sort_key)
    return [f for f in entries if (Path(folder_path) / f).is_file()]


# ---------- edição de progresso (funciona para mensagem normal OU inline) ----------

async def edit_progress(state: dict, text: str, reply_markup=None):
    """
    Edita a mensagem de progresso. Como o teclado inicial foi enviado pela
    conta `user` (via mecanismo de inline bot result do reload.py), a
    mensagem resultante normalmente só existe como "inline message" - ou
    seja, só temos um inline_message_id, não um chat_id/message_id normal.
    """
    inline_message_id = state.get("progress_inline_message_id")
    if inline_message_id:
        return await bot.edit_inline_text(
            inline_message_id, text, reply_markup=reply_markup
        )
    # fallback, caso um dia a mensagem seja uma mensagem "normal" do bot
    chat_id = state.get("progress_chat_id")
    message_id = state.get("progress_message_id")
    if chat_id and message_id:
        return await bot.edit_message_text(
            chat_id, message_id, text, reply_markup=reply_markup
        )
    raise RuntimeError("Nenhuma referência de mensagem de progresso encontrada no state.")


def capture_progress_ref(state: dict, cb: CallbackQuery):
    """Guarda no state a referência (inline ou normal) da mensagem do callback atual."""
    if cb.inline_message_id:
        state["progress_inline_message_id"] = cb.inline_message_id
    elif cb.message:
        state["progress_chat_id"] = cb.message.chat.id
        state["progress_message_id"] = cb.message.id


# ---------- envio final ----------

async def send_files_state(state: dict, t):
    folder = Path(state["folder_path"])
    files = state["files"]
    interval = state["interval"]
    delete_folder = state["delete_folder"]
    reply_to_orig = state.get("reply_to_orig", False)
    chat_id = state["chat_id"]
    orig_msg_id = state.get("orig_msg_id")
    total = len(files)

    for i, file_name in enumerate(files, 1):
        file_path = folder / file_name
        send_kwargs = {}
        if reply_to_orig and orig_msg_id:
            send_kwargs["reply_to_message_id"] = orig_msg_id
        try:
            await user.send_document(chat_id, file_path, **send_kwargs)
        except Exception as e:
            await edit_progress(
                state, t("doc_err_send").format(file=file_name, error=str(e))
            )
            return

        percent = (i / total) * 100
        remaining = total - i
        remaining_time = remaining * interval
        progress_text = t("doc_progress").format(
            current=i, total=total, percent=percent,
            remaining=format_seconds(remaining_time)
        )
        await edit_progress(state, progress_text)

        if i < total and interval > 0:
            await asyncio.sleep(interval)

    final_text = t("doc_finished")
    if delete_folder:
        try:
            shutil.rmtree(folder)
            final_text += "\n" + t("doc_folder_deleted").format(folder=str(folder))
        except Exception as e:
            final_text += "\n" + t("doc_delete_error").format(error=str(e))
    await edit_progress(state, final_text)


# ---------- perguntar intervalo (usado em callback e em comando direto) ----------

async def ask_interval(responder, listener_client: Client, chat_id: int, user_id: int, t):
    """
    `responder` precisa ter um método `.edit()` - pode ser tanto um
    `Message` (fluxo .doc -all direto) quanto um `CallbackQuery`
    (fluxo .doc <pasta>, onde a mensagem original é inline e só o
    callback consegue editá-la corretamente).
    """
    await responder.edit(t("doc_all_ask_interval"))
    try:
        resp = await listener_client.listen(
            chat_id=chat_id,
            user_id=user_id,
            timeout=60,
            filters=filters.text
        )
    except asyncio.TimeoutError:
        await responder.edit(t("doc_all_timeout"))
        return None
    text = resp.text.strip()
    await resp.delete()
    try:
        interval = parse_interval(text)
    except ValueError:
        await responder.edit(t("doc_all_invalid_interval").format(min=MIN_INTERVAL))
        return None
    return interval


# ---------- callbacks ----------

@bot.on_callback_query(filters.regex(r"^doc_confirm_dir\|") & filters.sudoers)
@use_lang()
async def doc_confirm_dir(c: Client, cb: CallbackQuery, t):
    # Responde imediatamente ao callback para evitar timeout
    await cb.answer()
    parts = cb.data.split("|")
    if len(parts) != 3:
        return await cb.edit(t("doc_all_err_format"))
    try:
        key = int(parts[1])
        choice = parts[2]
    except ValueError:
        return await cb.edit(t("doc_all_err_format"))

    db_msg = await DBMessage.get_or_none(key=key)
    if not db_msg:
        return await cb.edit(t("old_msg"))
    state = db_msg.keyboard
    if not isinstance(state, dict) or state.get("type") != "doc_dir_confirm":
        return await cb.edit(t("doc_all_err_state"))
    await db_msg.delete()

    if choice != "yes":
        return await cb.edit(t("canceled"))

    # Se só um arquivo, pula pergunta de intervalo
    if len(state["files"]) == 1:
        state["interval"] = 0
        state["type"] = "doc_batch"
        db_msg_batch = await DBMessage.create(text="doc_batch_state", keyboard=state)
        buttons = [
            [InlineKeyboardButton(t("yes"), callback_data=f"doc_reply|{db_msg_batch.key}|yes"),
             InlineKeyboardButton(t("no"), callback_data=f"doc_reply|{db_msg_batch.key}|no")]
        ]
        return await cb.edit(t("doc_all_ask_reply"), reply_markup=InlineKeyboardMarkup(buttons))

    # Múltiplos arquivos: pergunta o intervalo
    # IMPORTANTE: passamos `cb`, não `cb.message` (que é None para mensagens
    # enviadas via inline bot result) - cb.edit() sabe editar via inline_message_id.
    interval = await ask_interval(cb, user, state["chat_id"], state["user_id"], t)
    if interval is None:
        return
    state["interval"] = interval
    state["type"] = "doc_batch"
    db_msg_batch = await DBMessage.create(text="doc_batch_state", keyboard=state)
    buttons = [
        [InlineKeyboardButton(t("yes"), callback_data=f"doc_reply|{db_msg_batch.key}|yes"),
         InlineKeyboardButton(t("no"), callback_data=f"doc_reply|{db_msg_batch.key}|no")]
    ]
    await cb.edit(t("doc_all_ask_reply"), reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex(r"^doc_reply\|") & filters.sudoers)
@use_lang()
async def doc_reply_callback(c: Client, cb: CallbackQuery, t):
    parts = cb.data.split("|")
    if len(parts) != 3:
        return await cb.edit(t("doc_all_err_format"))
    try:
        key = int(parts[1])
        choice = parts[2]
    except ValueError:
        return await cb.edit(t("doc_all_err_format"))

    db_msg = await DBMessage.get_or_none(key=key)
    if not db_msg:
        return await cb.edit(t("old_msg"))
    state = db_msg.keyboard
    if not isinstance(state, dict) or state.get("type") != "doc_batch":
        return await cb.edit(t("doc_all_err_state"))

    state["reply_to_orig"] = (choice == "yes")

    # Este é o primeiro ponto em que temos certeza de qual é a mensagem
    # (inline ou não) que deve virar a mensagem de progresso mais tarde,
    # já que ela é a mesma em todos os callbacks seguintes (doc_reply -> doc_del).
    capture_progress_ref(state, cb)

    db_msg.keyboard = state
    await db_msg.save()

    buttons = [
        [InlineKeyboardButton(t("yes"), callback_data=f"doc_del|{db_msg.key}|yes"),
         InlineKeyboardButton(t("no"), callback_data=f"doc_del|{db_msg.key}|no")]
    ]
    await cb.edit(t("doc_all_ask_delete"), reply_markup=InlineKeyboardMarkup(buttons))


@bot.on_callback_query(filters.regex(r"^doc_del\|") & filters.sudoers)
@use_lang()
async def doc_del_callback(c: Client, cb: CallbackQuery, t):
    # Responde imediatamente antes do envio demorado
    await cb.answer()
    parts = cb.data.split("|")
    if len(parts) != 3:
        return await cb.edit(t("doc_all_err_format"))
    try:
        key = int(parts[1])
        choice = parts[2]
    except ValueError:
        return await cb.edit(t("doc_all_err_format"))

    db_msg = await DBMessage.get_or_none(key=key)
    if not db_msg:
        return await cb.edit(t("old_msg"))
    state = db_msg.keyboard
    if not isinstance(state, dict) or state.get("type") != "doc_batch":
        return await cb.edit(t("doc_all_err_state"))

    state["delete_folder"] = (choice == "yes")

    # Reforça a captura da referência de progresso (defensivo, caso o passo
    # anterior não tenha rodado por algum motivo).
    if not state.get("progress_inline_message_id") and not state.get("progress_chat_id"):
        capture_progress_ref(state, cb)

    await db_msg.delete()
    await cb.edit(t("doc_starting"))

    await send_files_state(state, t)


# ---------- comando principal ----------

@Client.on_message(filters.command("doc", prefixes=".") & filters.sudoers)
@use_lang()
async def doc(c: Client, m: Message, t):
    # --- Modo -all ---
    if len(m.command) >= 2 and m.command[1] == "-all":
        await m.edit(t("doc_all_ask_folder"))
        try:
            resp = await c.listen(
                chat_id=m.chat.id,
                user_id=m.from_user.id,
                timeout=60,
                filters=filters.text
            )
        except asyncio.TimeoutError:
            return await m.edit(t("doc_all_timeout"))
        folder_path = resp.text.strip()
        await resp.delete()

        if not Path(folder_path).is_dir():
            return await m.edit(t("doc_all_folder_not_found").format(path=folder_path))
        try:
            files = list_files(folder_path)
        except Exception:
            return await m.edit(t("doc_all_folder_read_error"))
        if not files:
            return await m.edit(t("doc_all_no_files"))

        if len(files) == 1:
            interval = 0
            await m.edit(t("doc_all_folder_found").format(count=1))
        else:
            await m.edit(t("doc_all_folder_found").format(count=len(files)))
            interval = await ask_interval(m, user, m.chat.id, m.from_user.id, t)
            if interval is None:
                return

        state = {
            "type": "doc_batch",
            "chat_id": m.chat.id,
            "orig_msg_id": m.id,
            "folder_path": folder_path,
            "files": files,
            "interval": interval,
            "user_id": m.from_user.id,
        }
        db_msg = await DBMessage.create(text="doc_batch_state", keyboard=state)
        buttons = [
            [InlineKeyboardButton(t("yes"), callback_data=f"doc_reply|{db_msg.key}|yes"),
             InlineKeyboardButton(t("no"), callback_data=f"doc_reply|{db_msg.key}|no")]
        ]
        await m.reply(t("doc_all_ask_reply"), reply_markup=InlineKeyboardMarkup(buttons))
        return

    # --- Comportamento normal ---
    if len(m.command) < 2:
        return await m.edit(t("no_file"))
    path = m.text.split(" ", 1)[1]
    if not Path(path).exists():
        return await m.edit(t("no_file"))

    # Diretório
    if Path(path).is_dir():
        try:
            files = list_files(path)
        except Exception:
            return await m.edit(t("doc_all_folder_read_error"))
        if not files:
            return await m.edit(t("doc_all_no_files"))
        state = {
            "type": "doc_dir_confirm",
            "chat_id": m.chat.id,
            "orig_msg_id": m.id,
            "folder_path": path,
            "files": files,
            "user_id": m.from_user.id,
        }
        db_msg = await DBMessage.create(text="doc_dir_confirm_state", keyboard=state)
        buttons = [
            [InlineKeyboardButton(t("yes"), callback_data=f"doc_confirm_dir|{db_msg.key}|yes"),
             InlineKeyboardButton(t("no"), callback_data=f"doc_confirm_dir|{db_msg.key}|no")]
        ]
        await m.reply(t("doc_dir_confirm").format(path=path), reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Arquivo único
    await m.reply_document(path)
