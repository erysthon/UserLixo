import os
from datetime import datetime

from hydrogram import Client, filters
from hydrogram.types import Message

import utils
from config import bot
from locales import use_lang


@Client.on_message(filters.command("backup", prefixes=".") & filters.sudoers)
@use_lang()
async def backup(c: Client, m: Message, t):
    wait_msg = await m.edit(t("initiating_backup"))
    d1 = datetime.now()
    arq = await utils.backup_sources()
    await wait_msg.edit(t("uploading_backup"))
    await bot.send_document(
        chat_id=c.me.id,
        document=arq,
        caption=t("backup_caption").format(name=m.from_user.mention, date=d1),
    )
    d2 = datetime.now()
    await wait_msg.edit(t("backup_completed").format(time=(d2 - d1).seconds))
    os.remove(arq)