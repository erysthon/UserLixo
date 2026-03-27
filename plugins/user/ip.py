import ipaddress
from hydrogram import Client, filters
from hydrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import bot
from db import Message as DBMessage
from utils import http
from locales import use_lang


async def get_api_return(ip: str):
    """Consulta a API ipinfo.io para obter detalhes do IP."""
    try:
        r = await http.get(f"https://ipinfo.io/{ip}/json", timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        data.pop("readme", None)
        return data
    except Exception:
        return None


def format_api_return(req: dict, t):
    """Formata o JSON da API em uma mensagem HTML elegante."""
    if req.get("bogon"):
        return t("ip_err_bogon_ip").format(ip=req["ip"])
    
    lines = [f"<b>{k.title()}</b>: <code>{v}</code>" for k, v in req.items()]
    return "\n".join(lines)


async def resolve_hostname(hostname: str) -> list:
    """Resolve um domínio para IPs IPv4 usando Cloudflare DNS (DoH)."""
    ips = []
    try:
        r = await http.get(
            f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A",
            headers={"accept": "application/dns-json"},
            timeout=10
        )
        data = r.json()
        if "Answer" in data:
            ips = [ans["data"] for ans in data["Answer"] if ans["type"] == 1]
    except Exception:
        pass
    return list(dict.fromkeys(ips))  # remove duplicatas


@Client.on_message(filters.command("ip", prefixes=".") & filters.sudoers)
@use_lang()
async def ip_cmd(c: Client, m: Message, t):
    if len(m.command) < 2:
        return await m.edit(t("ip_err_no_ip"))

    query = m.command[1]
    host = query.split("://")[-1].split("/")[0].split(":")[0]

    msg = await m.edit(t("ip_search"))

    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        ips = await resolve_hostname(host)

    if not ips:
        return await msg.edit(t("ip_err_no_ips").format(domain=host))

    # Filtra apenas IPv4
    ips = [ip for ip in ips if ':' not in ip]
    if not ips:
        return await msg.edit(t("ip_err_no_ips").format(domain=host))

    # Caso único: edita a mensagem original
    if len(ips) == 1:
        data = await get_api_return(ips[0])
        if not data:
            return await msg.edit(t("ip_err_search"))
        return await msg.edit(format_api_return(data, t))

    # Múltiplos IPs
    if len(ips) > 10:
        ips = ips[:10]

    # Armazena dados no banco (diretamente como dict, o ORM serializa automaticamente)
    ip_data = {
        "host": host,
        "ips": ips,
        "type": "ip_selector"
    }
    db_msg = await DBMessage.create(
        text=f"IP Selector: {host}",
        keyboard=ip_data
    )

    # Cria botões usando InlineKeyboardButton
    buttons = []
    for i, ip in enumerate(ips):
        buttons.append(InlineKeyboardButton(ip, callback_data=f"ip_info|{i}|{db_msg.key}"))

    # Organiza em duas colunas
    keyb = []
    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            keyb.append([buttons[i], buttons[i+1]])
        else:
            keyb.append([buttons[i]])

    reply_markup = InlineKeyboardMarkup(keyb)

    # Envia mensagem com botões e edita a original para indicar conclusão
    await m.reply(t("ip_select_ip").format(domain=host), reply_markup=reply_markup)
    await msg.edit(t("ip_found"))


@bot.on_callback_query(filters.regex(r"^ip_info\|") & filters.sudoers)
@use_lang()
async def ip_callback(c: Client, cb, t):
    parts = cb.data.split("|")
    if len(parts) != 3:
        return await cb.answer(t("ip_err_format"), show_alert=True)

    try:
        index = int(parts[1])
        message_key = int(parts[2])
    except ValueError:
        return await cb.answer(t("ip_err_format"), show_alert=True)

    db_msg = await DBMessage.get_or_none(key=message_key)
    if not db_msg:
        return await cb.answer(t("old_msg"), show_alert=True)

    ip_data = db_msg.keyboard
    if not isinstance(ip_data, dict) or ip_data.get("type") != "ip_selector":
        return await cb.answer(t("ip_err_data_nf"), show_alert=True)

    try:
        ip = ip_data["ips"][index]
    except (IndexError, KeyError):
        return await cb.answer(t("ip_err_data_nf"), show_alert=True)

    # Remove o pop-up indesejado (não chama cb.answer)
    data = await get_api_return(ip)
    if not data:
        return await cb.answer(t("ip_err_search"), show_alert=True)

    formatted = format_api_return(data, t)

    # Botão Voltar
    back_button = InlineKeyboardButton(t("back"), callback_data=f"ip_back|{message_key}")
    reply_markup = InlineKeyboardMarkup([[back_button]])

    await cb.edit(formatted, reply_markup=reply_markup)


@bot.on_callback_query(filters.regex(r"^ip_back\|") & filters.sudoers)
@use_lang()
async def ip_back_callback(c: Client, cb, t):
    parts = cb.data.split("|")
    if len(parts) != 2:
        return await cb.answer(t("ip_err_format"), show_alert=True)

    try:
        message_key = int(parts[1])
    except ValueError:
        return await cb.answer(t("ip_err_format"), show_alert=True)

    db_msg = await DBMessage.get_or_none(key=message_key)
    if not db_msg:
        return await cb.answer(t("old_msg"), show_alert=True)

    ip_data = db_msg.keyboard
    if not isinstance(ip_data, dict) or ip_data.get("type") != "ip_selector":
        return await cb.answer(t("ip_err_data_nf"), show_alert=True)

    host = ip_data.get("host", "")
    ips = ip_data.get("ips", [])
    if not ips:
        return await cb.answer(t("ip_err_data_nf"), show_alert=True)

    buttons = []
    for i, ip in enumerate(ips):
        buttons.append(InlineKeyboardButton(ip, callback_data=f"ip_info|{i}|{message_key}"))

    keyb = []
    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            keyb.append([buttons[i], buttons[i+1]])
        else:
            keyb.append([buttons[i]])

    reply_markup = InlineKeyboardMarkup(keyb)

    await cb.edit(t("ip_select_ip").format(domain=host), reply_markup=reply_markup)