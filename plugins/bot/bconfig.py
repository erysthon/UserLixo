from functools import partial
from typing import Union
import re

from hydrogram import Client, filters
from hydrogram.helpers import ikb
from hydrogram.types import CallbackQuery, Message
from hydrogram.errors import ListenerTimeout
from hydrogram.enums import MessageEntityType

from config import plugins
from db import Config, Sudoer
from locales import get_locale_string, langdict, use_lang


@Client.on_message(filters.command("config") & filters.sudoers)
@Client.on_callback_query(filters.regex(r"\bconfig\b") & filters.sudoers)
@use_lang()
async def config(c: Client, m: Union[Message, CallbackQuery], t):
    keyb = [
        [(t("lang"), "config_lang")],
        [(t("plugins_settings_button"), "config_plugins")],
        [(t("sudoers_manage"), "config_sudoers")],
    ]

    if isinstance(m, Message):
        await m.reply(t("config_choose"), reply_markup=ikb(keyb))
    elif isinstance(m, CallbackQuery):
        await m.edit(t("config_choose"), reply_markup=ikb(keyb))


@Client.on_callback_query(filters.regex(r"^config_lang") & filters.sudoers)
@use_lang()
async def config_lang(c: Client, m: CallbackQuery, t):
    langs = list(langdict)
    keyb = [
        [
            (
                f"{langdict[lang]['FLAG']} {langdict[lang]['NAME']}",
                f"config_setlang_{langdict[lang]['LANGUAGE_CODE']}",
            )
        ]
        for lang in langs
    ]
    keyb.append([(t("back"), "config")])
    await m.edit(t("choose_lang"), reply_markup=ikb(keyb))


@Client.on_callback_query(filters.regex(r"^config_setlang_") & filters.sudoers)
@use_lang()
async def config_lang_cq(c: Client, m: CallbackQuery, t):
    lang = m.data.split("_", 2)[2]
    await Config.get(id="lang").update(value=lang)
    lfunc = partial(get_locale_string, lang)
    await m.edit(
        lfunc("lang_set"), reply_markup=ikb([[(lfunc("back"), "config_lang")]])
    )


@Client.on_callback_query(filters.regex(r"^config_plugins") & filters.sudoers)
@use_lang()
async def config_plugins(c: Client, m: CallbackQuery, t):
    table = []
    row = []
    for i, plugin in enumerate(plugins):
        if i % 2 == 0 and i != 0:
            table.append(row)
            row = []
        row.append((t(f"{plugin}_config_button"), f"config_plugin_{plugin}"))
    table.append(row)

    table.append([(t("back"), "config")])

    await m.edit(t("plugins_settings"), reply_markup=ikb(table))


# -------------------- SUDOERS MANAGEMENT --------------------

@Client.on_callback_query(filters.regex(r"^config_sudoers$") & filters.sudoers)
@use_lang()
async def config_sudoers(c: Client, cq: CallbackQuery, t):
    from config import user
    host_id = user.me.id
    host_username = user.me.username or str(host_id)

    sudoers_config = await Config.get(id="sudoers")
    sudoers_ids = sudoers_config.valuej
    sudoers_ids = [uid for uid in sudoers_ids if uid != host_id]

    if not sudoers_ids:
        text = f"{t('sudoers_list_title')}\n{t('sudoers_no_sudoers')}\n\n{t('sudoers_host_label').format(username=host_username)}"
        buttons = [
            [(t("sudoers_add_button"), "config_sudoers_add")],
            [(t("back"), "config")]
        ]
        await cq.edit(text, reply_markup=ikb(buttons))
        return

    sudoers_data = []
    for uid in sudoers_ids:
        sudoer_entry = await Sudoer.get_or_none(user_id=uid)
        added_at = sudoer_entry.added_at if sudoer_entry else None
        try:
            user_obj = await c.get_users(uid)
            username = user_obj.username or str(uid)
        except:
            username = str(uid)
        sudoers_data.append((uid, username, added_at))

    sudoers_data.sort(key=lambda x: x[2] or 0, reverse=True)

    lines = [t('sudoers_list_title')]
    for idx, (uid, username, added_at) in enumerate(sudoers_data, start=1):
        date_str = added_at.strftime("%d/%m/%y") if added_at else "??/??/??"
        lines.append(t('sudoers_entry').format(index=idx, username=username, date=date_str))
    lines.append(f"\n{t('sudoers_host_label').format(username=host_username)}")
    text = "\n".join(lines)

    buttons = [
        [(t("sudoers_add_button"), "config_sudoers_add")],
        [(t("sudoers_remove_button"), "config_sudoers_remove")],
        [(t("back"), "config")]
    ]
    await cq.edit(text, reply_markup=ikb(buttons))


@Client.on_callback_query(filters.regex(r"^config_sudoers_add$") & filters.sudoers)
@use_lang()
async def config_sudoers_add(c: Client, cq: CallbackQuery, t):
    from config import user
    if cq.from_user.id != user.me.id:
        await cq.answer(t("sudoers_only_owner_alert"), show_alert=True)
        return

    cancel_buttons = [[(t("cancel"), "config_sudoers")]]
    await cq.edit(t("sudoers_add_instructions"), reply_markup=ikb(cancel_buttons))

    try:
        msg = await cq.message.chat.listen(
            filters.text & filters.user(user.me.id),
            timeout=60
        )
    except ListenerTimeout:
        await cq.edit(t("sudoers_add_timeout"), reply_markup=ikb([[(t("back"), "config_sudoers")]]))
        return

    identifier = msg.text.strip()

    if identifier.startswith(('/', '.')):
        await cq.edit(t("canceled"), reply_markup=ikb([[(t("back"), "config_sudoers")]]))
        return

    try:
        if identifier.startswith('@'):
            user_obj = await c.get_users(identifier)
        elif identifier.isdigit():
            user_obj = await c.get_users(int(identifier))
        else:
            raise ValueError
    except Exception:
        await msg.delete()
        await cq.edit(t("sudoers_add_user_not_found"), reply_markup=ikb([[(t("try_again"), "config_sudoers_add")]]))
        return

    if user_obj.id == user.me.id:
        await msg.delete()
        await cq.edit(t("sudoers_add_self"), reply_markup=ikb([[(t("back"), "config_sudoers")]]))
        return

    sudoers_config = await Config.get(id="sudoers")
    sudoers_ids = sudoers_config.valuej
    if user_obj.id in sudoers_ids:
        await msg.delete()
        await cq.edit(t("sudoers_add_already_sudoer"), reply_markup=ikb([[(t("back"), "config_sudoers")]]))
        return

    sudoers_ids.append(user_obj.id)
    sudoers_config.valuej = sudoers_ids
    await sudoers_config.save()
    await Sudoer.create(user_id=user_obj.id)

    await msg.delete()
    username = user_obj.username or str(user_obj.id)
    await cq.edit(t("sudoers_add_success").format(username=username), reply_markup=ikb([[(t("back"), "config_sudoers")]]))


@Client.on_callback_query(filters.regex(r"^config_sudoers_remove(_page_(\d+))?$") & filters.sudoers)
@use_lang()
async def config_sudoers_remove(c: Client, cq: CallbackQuery, t):
    from config import user
    if cq.from_user.id != user.me.id:
        await cq.answer(t("sudoers_only_owner_alert"), show_alert=True)
        return

    match = re.match(r"config_sudoers_remove(?:_page_(\d+))?$", cq.data)
    page = int(match.group(1)) if match.group(1) else 1

    sudoers_config = await Config.get(id="sudoers")
    sudoers_ids = [uid for uid in sudoers_config.valuej if uid != user.me.id]

    if not sudoers_ids:
        await cq.answer(t("sudoers_no_sudoers"), show_alert=True)
        await cq.edit(t("sudoers_list_title"), reply_markup=ikb([[(t("back"), "config_sudoers")]]))
        return

    PER_PAGE = 8
    total = len(sudoers_ids)
    max_page = (total + PER_PAGE - 1) // PER_PAGE
    start = (page - 1) * PER_PAGE
    end = start + PER_PAGE
    page_sudoers = sudoers_ids[start:end]

    buttons = []
    for uid in page_sudoers:
        try:
            user_obj = await c.get_users(uid)
            username = user_obj.username or str(uid)
        except:
            username = str(uid)
        buttons.append([(f"@{username}", f"config_sudoers_remove_confirm_{uid}")])

    nav_buttons = []
    if page > 1:
        nav_buttons.append((t("prev"), f"config_sudoers_remove_page_{page-1}"))
    if page < max_page:
        nav_buttons.append((t("next"), f"config_sudoers_remove_page_{page+1}"))
    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([(t("back"), "config_sudoers")])

    await cq.edit(t("sudoers_remove_title"), reply_markup=ikb(buttons))


@Client.on_callback_query(filters.regex(r"^config_sudoers_remove_confirm_(\d+)$") & filters.sudoers)
@use_lang()
async def config_sudoers_remove_confirm(c: Client, cq: CallbackQuery, t):
    from config import user
    if cq.from_user.id != user.me.id:
        await cq.answer(t("sudoers_only_owner_alert"), show_alert=True)
        return

    uid = int(cq.data.split("_")[-1])
    if uid == user.me.id:
        await cq.answer(t("sudoers_remove_self_alert"), show_alert=True)
        return

    try:
        user_obj = await c.get_users(uid)
        username = user_obj.username or str(uid)
    except:
        username = str(uid)

    confirm_buttons = [
        [(t("yes"), f"config_sudoers_remove_execute_{uid}")],
        [(t("no"), "config_sudoers_remove")]
    ]
    await cq.edit(t("sudoers_remove_confirm").format(username=username), reply_markup=ikb(confirm_buttons))


@Client.on_callback_query(filters.regex(r"^config_sudoers_remove_execute_(\d+)$") & filters.sudoers)
@use_lang()
async def config_sudoers_remove_execute(c: Client, cq: CallbackQuery, t):
    from config import user
    if cq.from_user.id != user.me.id:
        await cq.answer(t("sudoers_only_owner_alert"), show_alert=True)
        return

    uid = int(cq.data.split("_")[-1])
    if uid == user.me.id:
        await cq.answer(t("sudoers_remove_self_alert"), show_alert=True)
        return

    sudoers_config = await Config.get(id="sudoers")
    sudoers_ids = sudoers_config.valuej
    if uid in sudoers_ids:
        sudoers_ids.remove(uid)
        sudoers_config.valuej = sudoers_ids
        await sudoers_config.save()

    await Sudoer.filter(user_id=uid).delete()

    try:
        user_obj = await c.get_users(uid)
        username = user_obj.username or str(uid)
    except:
        username = str(uid)

    await cq.edit(t("sudoers_remove_success").format(username=username), reply_markup=ikb([[(t("back"), "config_sudoers")]]))