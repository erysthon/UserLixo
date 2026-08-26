import base64
import io
import json
import asyncio
import os
import time
from tempfile import TemporaryDirectory
from datetime import datetime
from typing import Dict, List, Optional, Any

import httpx
import wikipedia
from hydrogram import Client, filters
from hydrogram.types import InputMediaPhoto, Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from hydrogram.enums import ChatType
from hydrogram.errors import FloodWait, ListenerTimeout
from locales import use_lang
from config import bot

# ------------------------------------------------------------
# Gerenciamento de memória (melhoria 2)
# ------------------------------------------------------------
gpt_instances: Dict[int, Dict] = {}
MAX_CONVERSATIONS = 50
CONVERSATION_TTL = 3600       # 1 hora
MAX_HISTORY_MESSAGES = 25

async def cleanup_old_conversations():
    now = time.time()
    to_delete = [mid for mid, data in gpt_instances.items() if now - data["timestamp"] > CONVERSATION_TTL]
    for mid in to_delete:
        del gpt_instances[mid]
    if len(gpt_instances) > MAX_CONVERSATIONS:
        sorted_items = sorted(gpt_instances.items(), key=lambda x: x[1]["timestamp"])
        for mid, _ in sorted_items[:len(gpt_instances) - MAX_CONVERSATIONS]:
            del gpt_instances[mid]

def limit_conversation_history(form: List[Dict]) -> List[Dict]:
    if form and form[0].get("role") == "system":
        system = form[0]
        rest = form[1:]
    else:
        system = None
        rest = form[:]
    if len(rest) > MAX_HISTORY_MESSAGES:
        rest = rest[-MAX_HISTORY_MESSAGES:]
    return [system] + rest if system else rest

# ------------------------------------------------------------
# Filtro de continuação de conversa
# ------------------------------------------------------------
async def filter_gpt_logic(flt, client: Client, message: Message):
    if message:
        if message.reply_to_message_id and message.reply_to_message_id in gpt_instances:
            return True
    return False

filter_gpt = filters.create(filter_gpt_logic)

# ------------------------------------------------------------
# Comando principal .gpt
# ------------------------------------------------------------
@Client.on_message((filters.command("gpt", prefixes=".") | filter_gpt) & filters.sudoers)
@use_lang()
async def gpt(c: Client, m: Message, t):
    await cleanup_old_conversations()
    
    # Lock por chat (evita edições concorrentes)
    if not hasattr(gpt, "_locks"):
        gpt._locks = {}
    chat_lock = gpt._locks.get(m.chat.id)
    if chat_lock is None:
        chat_lock = asyncio.Lock()
        gpt._locks[m.chat.id] = chat_lock
    
    async with chat_lock:
        # CAPTURA A MENSAGEM DE ESPERA
        wait_msg = await m.edit(t("wait"))
        
        # Recupera ou cria o histórico
        form = []
        if m.reply_to_message_id and m.reply_to_message_id in gpt_instances:
            conv_data = gpt_instances[m.reply_to_message_id]
            form = conv_data["form"]
            conv_data["timestamp"] = time.time()
            # Adiciona a mensagem do usuário atual
            form.append({"role": "user", "content": m.text, "name": m.from_user.first_name})
        else:
            # Nova conversa: processa o comando e contexto
            mtext = m.text.split(" ", maxsplit=1)
            
            # Se houver foto anexada
            if m.photo:
                content = []
                try:
                    with TemporaryDirectory() as tempdir:
                        photo = await c.download_media(m, file_name=tempdir, in_memory=True)
                        encoded_string = base64.b64encode(photo.getvalue()).decode("utf-8")
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded_string}", "detail": "high"}
                    })
                    if len(mtext) >= 2:
                        content.append({"type": "text", "text": mtext[1]})
                    elif m.caption:
                        content.append({"type": "text", "text": m.caption})
                    else:
                        content.append({"type": "text", "text": t("ai_default_image_question")})
                    form = [{"role": "user", "content": content, "name": m.from_user.first_name}]
                except Exception as e:
                    form = [{"role": "user", "content": t("ai_error").format(error=str(e)), "name": m.from_user.first_name}]
            elif len(mtext) >= 2:
                form = [{"role": "user", "content": mtext[1], "name": m.from_user.first_name}]
            else:
                # Sem texto: tenta usar reply ou histórico do chat
                if m.reply_to_message and m.reply_to_message.text:
                    form = [{"role": "user", "content": m.reply_to_message.text, "name": m.from_user.first_name}]
                else:
                    # Busca histórico do chat (últimas mensagens) para contexto
                    start_msg = None
                    try:
                        history = await c.get_chat_history(m.chat.id, limit=2)
                        for hist_msg in history:
                            if hist_msg.id != m.id:
                                start_msg = hist_msg
                                break
                    except FloodWait as e:
                        await asyncio.sleep(min(e.value, 10))
                        try:
                            history = await c.get_chat_history(m.chat.id, limit=2)
                            for hist_msg in history:
                                if hist_msg.id != m.id:
                                    start_msg = hist_msg
                                    break
                        except:
                            pass
                    
                    if start_msg and start_msg.text:
                        form = [{"role": "user", "content": start_msg.text, "name": m.from_user.first_name}]
                    else:
                        return await wait_msg.edit(t("ai_no_text"))
            
            # Coleta de contexto: mensagens anteriores em cadeia (reply_to_message)
            start_msg = m.reply_to_message if m.reply_to_message else None
            if not start_msg and m.chat.type != ChatType.PRIVATE:
                try:
                    history = await c.get_chat_history(m.chat.id, limit=2)
                    for hist_msg in history:
                        if hist_msg.id != m.id:
                            start_msg = hist_msg
                            break
                except:
                    pass
            
            collected = []
            current = start_msg
            iterations = 0
            while current and iterations < 30:
                collected.insert(0, current)  # mantém ordem cronológica
                if current.reply_to_message_id:
                    try:
                        current = await c.get_messages(m.chat.id, current.reply_to_message_id)
                    except FloodWait as e:
                        await asyncio.sleep(min(e.value, 10))
                        try:
                            current = await c.get_messages(m.chat.id, current.reply_to_message_id)
                        except:
                            break
                    except:
                        break
                else:
                    break
                iterations += 1
                await asyncio.sleep(0)
            
            for mes in collected:
                msg_content = await convert_telegram_message_to_openai(c, mes, t)
                if msg_content:
                    form.insert(0, {"role": "user", "content": msg_content, "name": mes.from_user.first_name if mes.from_user else "User"})
        
        # Adiciona system prompt (traduzido)
        system_prompt = t("ai_system_prompt").format(
            bot_name=c.me.first_name,
            chat_info=await get_chat_info(c, m, t),
            current_time=datetime.now().strftime("%d/%m/%Y às %H:%M:%S"),
            user_name=m.from_user.first_name,
            user_username=f" (@{m.from_user.username})" if m.from_user and m.from_user.username else ""
        )
        if form and form[0].get("role") == "system":
            form[0]["content"] = system_prompt
        else:
            form.insert(0, {"role": "system", "content": system_prompt})
        
        # --------------------------------------------------------
        # Loop de chamadas de função (comportamento original)
        # --------------------------------------------------------
        user_keys = await get_ai_keys(m.from_user.id)
        if not user_keys:
           return await wait_msg.edit(t("ai_no_key_error"))

        functions = [
            {
                "name": "create_ai_art",
                "description": "Return this only if the user wants to create a photo or art. Depending on what the user wants to create, generate a prompt for them by describing the image in great detail. Ensure conciseness by breaking down every detail with commas. Prompt word is never longer than 25 words.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"description": "The prompt to create art", "type": "string"},
                        "negative_prompt": {"description": "The negative prompt to create art", "type": "string"},
                        "width": {"description": "The width of the image, minimum 512", "type": "integer"},
                        "height": {"description": "The height of the image, minimum 512", "type": "integer"},
                        "amount": {"description": "The amount of images to generate, maximum 8", "type": "integer"},
                    },
                },
            },
            {
                "name": "get_wikipedia_page",
                "description": "Use esta função para obter o conteúdo de uma página da wikipedia a partir do título",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"description": "O título do artigo da wikipedia", "type": "string"},
                        "language": {"description": "Idioma da página da wikipedia", "type": "string"},
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "search_wikipedia",
                "description": "Use esta função para pesquisar por artigos na wikipedia, use ela primeiro para obter o título correto do artigo, depois use a função get_wikipedia_page para obter o conteúdo do artigo",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"description": "A query para pesquisar na wikipedia", "type": "string"},
                        "language": {"description": "Idioma da pesquisa na wikipedia", "type": "string"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "web_search",
                "description": "Busca informações na internet em tempo real usando o DuckDuckGo. Use esta função quando precisar de informações atualizadas, notícias, eventos recentes, ou qualquer coisa que não esteja no seu conhecimento de treinamento. Retorna resumos de múltiplas fontes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"description": "A consulta de pesquisa para buscar na internet", "type": "string"},
                        "max_results": {"description": "Número máximo de resultados a retornar (padrão: 5, máximo: 10)", "type": "integer"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fetch_webpage",
                "description": "Busca e extrai o conteúdo completo de uma página web específica. Use quando o usuário quiser ler uma URL específica ou quando precisar de conteúdo detalhado de um site.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"description": "A URL completa da página web a ser buscada", "type": "string"},
                    },
                    "required": ["url"],
                },
            },
        ]
        
        iteration = 0
        max_iter = 5
        page_url = None
        
        while iteration < max_iter:
            payload = {"model": "gpt-4o-mini", "messages": form, "functions": functions}
            response, error_text = await call_openai_chat(m.from_user.id, payload)

            if response is None:
                return await wait_msg.edit(t("ai_api_error").format(error=error_text or "?"))
            
            res = response.json()["choices"][0]["message"]
            
            if not res.get("function_call"):
                break
            
            fn_name = res["function_call"]["name"]
            fn_args = json.loads(res["function_call"]["arguments"])
            
            # Adiciona a chamada da função ao histórico
            form.append(res)
            
            # ----------------------------------------------------
            # create_ai_art (comportamento original)
            # ----------------------------------------------------
            if fn_name == "create_ai_art":
                amount = fn_args.get("amount", 1)
                prompt = fn_args["prompt"]
                negative_prompt = fn_args.get("negative_prompt", "")
                width = fn_args.get("width", 1024)
                height = fn_args.get("height", 1024)
                width = max(512, width)
                height = max(512, height)
                await m.edit(
                    t("ai_generating_images").format(
                        amount=amount,
                        prompt=prompt,
                        negative_prompt=negative_prompt,
                        width=width,
                        height=height
                    )
                )
                # Obter token da Vulcan Labs
                async with httpx.AsyncClient() as http_client:
                    r = await http_client.post(
                        "https://api.vulcanlabs.co/smith-auth/api/v1/token",
                        json={"device_id": str(c.me.id)},
                        headers={"content-type": "application/json; charset=utf-8"},
                    )
                    img_token = r.json()["AccessToken"]
                
                img_headers = {
                    "User-Agent": "Chat Smith Android, Version 3.9.8(691)",
                    "Accept": "application/json",
                    "authorization": f"Bearer {img_token}",
                    "content-type": "application/json; charset=utf-8",
                }
                
                photos = []
                for i in range(amount):
                    payload_img = {
                        "model": "stable-diffusion-xl-v1-0",
                        "negative_prompt": negative_prompt,
                        "width": width,
                        "height": height,
                        "prompt": prompt,
                        "steps": 20,
                        "guidance": 7.5,
                        "output_format": "jpeg",
                        "scheduler": "euler",
                    }
                    async with httpx.AsyncClient() as http_client:
                        resp_img = await http_client.post(
                            "https://api.vulcanlabs.co/smith-v2/api/v1/text2image",
                            json=payload_img,
                            headers=img_headers,
                            timeout=340,
                        )
                    if (
                        not resp_img
                        or not resp_img.json().get("data")
                        or not resp_img.json().get("data").get("image")
                    ):
                        result = t("ai_image_generation_failed")
                        break
                    img = resp_img.json()["data"]["image"]
                    photos.append(
                        InputMediaPhoto(
                            io.BytesIO(base64.b64decode(img)),
                            caption=t("ai_image_caption").format(
                                prompt=prompt,
                                negative_prompt=negative_prompt,
                                width=width,
                                height=height
                            ) if i == 0 else "",
                        )
                    )
                if photos:
                    await m.delete()
                    await c.send_media_group(m.chat.id, photos)
                    result = t("ai_images_generated_successfully").format(count=amount)
                else:
                    result = t("ai_image_generation_failed")
                
                form.append({"role": "function", "name": fn_name, "content": result})
                # Não precisa continuar o loop, pois a resposta final será do assistente
                # Mas como a função já enviou as imagens, podemos sair do loop
                break
            
            # ----------------------------------------------------
            # get_wikipedia_page (comportamento original com status)
            # ----------------------------------------------------
            elif fn_name == "get_wikipedia_page":
                title = fn_args["title"]
                language = fn_args.get("language", "en")
                await m.edit(t("ai_wikipedia_fetching").format(title=title, language=language))
                try:
                    wikipedia.set_lang(language)
                    page = wikipedia.page(title)
                    content = page.content
                    page_url = page.url
                except wikipedia.exceptions.DisambiguationError as e:
                    pages = e.options
                    content = t("ai_wikipedia_ambiguous").format(
                        title=title,
                        pages="\n".join([f"- {p}" for p in pages])
                    )
                except wikipedia.exceptions.PageError:
                    content = t("ai_wikipedia_no_results").format(query=title)
                except Exception as e:
                    content = t("ai_wikipedia_error").format(error=str(e))
                
                form.append({"role": "function", "name": fn_name, "content": content})
            
            # ----------------------------------------------------
            # search_wikipedia (com status)
            # ----------------------------------------------------
            elif fn_name == "search_wikipedia":
                query = fn_args["query"]
                language = fn_args.get("language", "en")
                await m.edit(t("ai_wikipedia_searching").format(query=query, language=language))
                try:
                    wikipedia.set_lang(language)
                    pages = wikipedia.search(query)
                    if not pages:
                        content = t("ai_wikipedia_no_results").format(query=query)
                    else:
                        content = t("ai_wikipedia_results_header").format(query=query) + "\n\n"
                        content += "\n".join([f"- {p}" for p in pages])
                except Exception as e:
                    content = t("ai_wikipedia_error").format(error=str(e))
                
                form.append({"role": "function", "name": fn_name, "content": content})
            
            # ----------------------------------------------------
            # web_search (com status)
            # ----------------------------------------------------
            elif fn_name == "web_search":
                query = fn_args["query"]
                max_results = fn_args.get("max_results", 5)
                max_results = min(max_results, 10)
                try:
                    await m.edit(t("ai_web_searching").format(query=query))
                except Exception:
                    pass
                
                content = ""
                try:
                    from ddgs import DDGS
                    with DDGS() as ddgs:
                        results = list(ddgs.text(query, max_results=max_results, region="pt-BR"))
                        for i, result in enumerate(results):
                            content += t("ai_web_result_item").format(
                                index=i+1,
                                title=result['title'],
                                url=result['href'],
                                summary=result['body']
                            ) + "\n\n"
                    if not content:
                        content = t("ai_web_no_results").format(query=query)
                except ImportError:
                    content = t("ai_error").format(error=t("ai_dependency_ddgs"))
                except Exception as e:
                    content = t("ai_web_error").format(error=str(e))
                
                form.append({"role": "function", "name": fn_name, "content": content})
            
            # ----------------------------------------------------
            # fetch_webpage (com status)
            # ----------------------------------------------------
            elif fn_name == "fetch_webpage":
                url = fn_args["url"]
                await m.edit(t("ai_fetching_page").format(url=url))
                try:
                    from bs4 import BeautifulSoup
                    async with httpx.AsyncClient(follow_redirects=True) as http_client:
                        resp = await http_client.get(url, timeout=30)
                        resp.raise_for_status()
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for script in soup(["script", "style", "nav", "footer", "header"]):
                        script.decompose()
                    title = soup.find("title")
                    title_text = title.get_text().strip() if title else t("ai_web_no_title")
                    text = soup.get_text()
                    lines = (line.strip() for line in text.splitlines())
                    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                    text = "\n".join(chunk for chunk in chunks if chunk)
                    max_chars = 4000
                    if len(text) > max_chars:
                        text = text[:max_chars] + "\n\n" + t("ai_web_truncated")
                    content = t("ai_webpage_content").format(title=title_text, url=url, content=text)
                except ImportError:
                    content = t("ai_error").format(error=t("ai_dependency_bs4"))
                except Exception as e:
                    content = t("ai_error").format(error=t("ai_web_fetch_error").format(error=str(e)))
                
                form.append({"role": "function", "name": fn_name, "content": content})
            
            iteration += 1
        
        # Resposta final
        final_content = res.get("content") or t("ai_empty_response")
        # Determina o texto do usuário para exibir no blockquote
        user_query = ""
        if form:
            # Procura a última mensagem do usuário
            for msg_rev in reversed(form):
                if msg_rev.get("role") == "user":
                    user_query = msg_rev.get("content", "")
                    if isinstance(user_query, list):
                        for part in user_query:
                            if part.get("type") == "text":
                                user_query = part["text"]
                                break
                        else:
                            user_query = t("ai_image_sent_with_command")
                    break
        if not user_query:
            user_query = t("ai_image_sent_with_command")
        
        if page_url:
            text = f"<blockquote>{user_query}</blockquote>\n\n{final_content}\n\n" + t("ai_wikipedia_read_more").format(url=page_url)
        else:
            text = f"<blockquote>{user_query}</blockquote>\n\n{final_content}"
        await m.edit(text)
        
        # Salva o histórico da conversa
        final_form = limit_conversation_history(form)
        gpt_instances[m.id] = {"form": final_form, "timestamp": time.time()}

# ------------------------------------------------------------
# Funções auxiliares
# ------------------------------------------------------------
async def convert_telegram_message_to_openai(client: Client, msg: Message, t):
    """Converte uma mensagem do Telegram para o formato da OpenAI (texto ou multimodal)"""
    if msg.photo or msg.sticker:
        try:
            with TemporaryDirectory() as tempdir:
                media = await client.download_media(msg, file_name=tempdir, in_memory=True)
                encoded = base64.b64encode(media.getvalue()).decode("utf-8")
            content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "high"}}]
            if msg.caption:
                content.append({"type": "text", "text": t("ai_image_caption_label").format(caption=msg.caption)})
            return content
        except Exception:
            return t("ai_error").format(error="Falha ao processar mídia")
    elif msg.video or msg.video_note or msg.animation:
        media_type = t("ai_media_video") if msg.video else (t("ai_media_video_note") if msg.video_note else t("ai_media_gif"))
        caption = msg.caption if msg.caption else t("ai_media_no_caption")
        duration = getattr(msg.video or msg.video_note or msg.animation, "duration", "desconhecida")
        return t("ai_media_content").format(media_type=media_type, duration=duration, caption=caption)
    elif msg.audio or msg.voice:
        media_type = t("ai_media_audio") if msg.audio else t("ai_media_voice")
        duration = getattr(msg.audio or msg.voice, "duration", "desconhecida")
        caption = msg.caption if msg.caption else t("ai_media_no_caption")
        return t("ai_media_content").format(media_type=media_type, duration=duration, caption=caption)
    elif msg.document:
        file_name = msg.document.file_name or t("ai_media_unnamed")
        file_size = msg.document.file_size or 0
        caption = msg.caption if msg.caption else t("ai_media_no_caption")
        return t("ai_media_document_content").format(file_name=file_name, file_size=file_size, caption=caption)
    elif msg.location:
        return t("ai_media_location").format(lat=msg.location.latitude, lon=msg.location.longitude)
    elif msg.contact:
        return t("ai_media_contact").format(first_name=msg.contact.first_name, last_name=msg.contact.last_name or "", phone=msg.contact.phone_number)
    elif msg.poll:
        options = ", ".join([opt.text for opt in msg.poll.options])
        return t("ai_media_poll").format(question=msg.poll.question, options=options)
    elif msg.text:
        return msg.text
    else:
        return t("ai_media_unsupported")

async def get_chat_info(client: Client, msg: Message, t) -> str:
    if msg.chat.type == ChatType.PRIVATE:
        return t("ai_chat_private").format(name=msg.chat.first_name)
    else:
        info = t("ai_chat_group").format(title=msg.chat.title)
        if msg.chat.username:
            info += f" (@{msg.chat.username})"
        try:
            member_count = await client.get_chat_members_count(msg.chat.id)
            info += t("ai_chat_members").format(count=member_count)
        except:
            pass
        return info

# ------------------------------------------------------------
# Gerenciamento de chaves API (múltiplas por usuário, com base_url)
# ------------------------------------------------------------
def mask_ai_key(api_key: str) -> str:
    return api_key[:6] + "****" + api_key[-4:] if len(api_key) > 10 else "****"


async def get_ai_keys(user_id: int):
    """Todas as chaves do usuário, a ativa primeiro."""
    from db import AIApiKey
    return await AIApiKey.filter(user_id=user_id).order_by("-is_active", "id")


async def get_active_ai_key(user_id: int):
    from db import AIApiKey
    return await AIApiKey.get_or_none(user_id=user_id, is_active=True)


async def add_ai_key(user_id: int, base_url: str, api_key: str):
    """Adiciona uma nova chave e a torna a ativa (desativando as demais)."""
    from db import AIApiKey
    await AIApiKey.filter(user_id=user_id).update(is_active=False)
    return await AIApiKey.create(
        user_id=user_id,
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        is_active=True,
    )


async def set_active_ai_key(user_id: int, key_id: int) -> bool:
    from db import AIApiKey
    key_record = await AIApiKey.get_or_none(id=key_id, user_id=user_id)
    if not key_record:
        return False
    await AIApiKey.filter(user_id=user_id).update(is_active=False)
    key_record.is_active = True
    await key_record.save()
    return True


async def remove_ai_key(user_id: int, key_id: int) -> bool:
    from db import AIApiKey
    key_record = await AIApiKey.get_or_none(id=key_id, user_id=user_id)
    if not key_record:
        return False
    was_active = key_record.is_active
    await key_record.delete()
    if was_active:
        # promove a próxima chave cadastrada a ativa, se houver
        remaining = await AIApiKey.filter(user_id=user_id).order_by("id").first()
        if remaining:
            remaining.is_active = True
            await remaining.save()
    return True


async def validate_ai_key(base_url: str, api_key: str) -> bool:
    """Valida uma chave fazendo uma requisição de teste em {base_url}/models."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/models",
                headers=headers,
                timeout=10
            )
            return response.status_code == 200
    except Exception:
        return False


async def call_openai_chat(user_id: int, payload: dict):
    """
    Faz a chamada de chat completions tentando a chave ativa do usuário
    primeiro e, se ela falhar, as demais chaves cadastradas (nessa ordem).
    A primeira chave que funcionar é marcada como ativa. Retorna
    (response, error_text) - `response` é None se todas as chaves falharem,
    e nesse caso `error_text` traz o erro da última tentativa.
    """
    keys = await get_ai_keys(user_id)
    if not keys:
        return None, None

    last_error = None
    for key in keys:
        headers = {
            "Authorization": f"Bearer {key.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{key.base_url.rstrip('/')}/chat/completions"
        try:
            async with httpx.AsyncClient() as http_client:
                response = await http_client.post(
                    url, json=payload, headers=headers, timeout=340
                )
        except Exception as e:
            last_error = str(e)
            continue

        if response.status_code == 200:
            if not key.is_active:
                await set_active_ai_key(user_id, key.id)
            return response, None

        last_error = response.text

    return None, last_error

# ------------------------------------------------------------
# Callbacks de configuração da chave AI
# ------------------------------------------------------------
@bot.on_callback_query(filters.regex(r"\bconfig_plugin_ai\b"))
@use_lang()
async def config_ai(c: Client, cq: CallbackQuery, t):
    """Menu principal de configuração das chaves AI."""
    user_id = cq.from_user.id
    active_key = await get_active_ai_key(user_id)

    if active_key:
        key_status = t("ai_has_key").format(
            masked=f"{active_key.base_url} — {mask_ai_key(active_key.api_key)}"
        )
    else:
        key_status = t("ai_no_key")

    await cq.edit_message_text(
        f"{t('ai_settings_title')}\n\n{key_status}",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    text=t("ai_add_key"),
                    callback_data="config_plugin_ai_key"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("ai_select_key"),
                    callback_data="config_plugin_ai_select"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("ai_remove_key"),
                    callback_data="config_plugin_ai_remove"
                )
            ],
            [InlineKeyboardButton(text=t("back"), callback_data="config_plugins")]
        ])
    )


def build_key_list_markup(keys, action: str, t, back_callback: str = "config_plugin_ai"):
    """Monta os botões de lista de chaves para os menus de seleção/remoção."""
    buttons = []
    for key in keys:
        label = f"{'✅ ' if key.is_active else ''}{key.base_url} — {mask_ai_key(key.api_key)}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"{action}|{key.id}")])
    buttons.append([InlineKeyboardButton(text=t("back"), callback_data=back_callback)])
    return InlineKeyboardMarkup(buttons)

@bot.on_callback_query(filters.regex(r"^config_plugin_ai_key$"))
@use_lang()
async def config_ai_key(c: Client, cq: CallbackQuery, t):
    """Fluxo de adicionar chave: pede a URL base e, em seguida, a chave."""
    user_id = cq.from_user.id

    # Passo 1: pede a URL base da API
    try:
        await cq.edit_message_text(
            t("ai_enter_baseurl_instructions"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("cancel"), callback_data="config_plugin_ai")]
            ])
        )
    except Exception as e:
        print(t("ai_error_editing_message").format(error=e))
        return

    try:
        url_msg = await cq.message.chat.listen(
            filters.text & filters.user(user_id),
            timeout=60
        )
    except ListenerTimeout:
        try:
            await cq.edit_message_text(
                t("ai_timeout"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_ai")]
                ])
            )
        except:
            pass
        return

    base_url = url_msg.text.strip()
    await url_msg.delete()

    if base_url.startswith(("/", ".")):
        try:
            await cq.edit_message_text(
                t("canceled"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_ai")]
                ])
            )
        except:
            pass
        return

    if not base_url.startswith(("http://", "https://")):
        try:
            await cq.edit_message_text(
                t("ai_invalid_baseurl"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(text=t("try_again"), callback_data="config_plugin_ai_key")]
                ])
            )
        except:
            pass
        return

    # Passo 2: pede a chave da API
    try:
        await cq.edit_message_text(
            t("ai_enter_key_instructions"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("cancel"), callback_data="config_plugin_ai")]
            ])
        )
    except:
        pass

    try:
        key_msg = await cq.message.chat.listen(
            filters.text & filters.user(user_id),
            timeout=60
        )
    except ListenerTimeout:
        try:
            await cq.edit_message_text(
                t("ai_timeout"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_ai")]
                ])
            )
        except:
            pass
        return

    new_key = key_msg.text.strip()

    if new_key.startswith(("/", ".")):
        try:
            await cq.edit_message_text(
                t("canceled"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_ai")]
                ])
            )
        except:
            pass
        return

    try:
        await cq.edit_message_text(t("ai_validating_key"))
    except:
        pass

    is_valid = await validate_ai_key(base_url, new_key)

    if not is_valid:
        try:
            await cq.edit_message_text(
                t("ai_invalid_key"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(text=t("try_again"), callback_data="config_plugin_ai_key")]
                ])
            )
        except:
            pass
        # Não apaga a mensagem do usuário (para que possa tentar novamente)
        return

    # Chave válida: apaga a mensagem do usuário e salva (já como ativa)
    await add_ai_key(user_id, base_url, new_key)

    try:
        await cq.edit_message_text(
            t("ai_key_set"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_ai")]
            ])
        )
        await key_msg.delete()
    except:
        pass


@bot.on_callback_query(filters.regex(r"^config_plugin_ai_select$"))
@use_lang()
async def config_ai_select_list(c: Client, cq: CallbackQuery, t):
    """Lista as chaves cadastradas para o usuário escolher qual ativar."""
    user_id = cq.from_user.id
    keys = await get_ai_keys(user_id)

    if not keys:
        return await cq.edit_message_text(
            t("ai_key_list_empty"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_ai")]
            ])
        )

    await cq.edit_message_text(
        t("ai_select_key_title"),
        reply_markup=build_key_list_markup(keys, "config_plugin_ai_setactive", t)
    )


@bot.on_callback_query(filters.regex(r"^config_plugin_ai_setactive\|"))
@use_lang()
async def config_ai_set_active(c: Client, cq: CallbackQuery, t):
    user_id = cq.from_user.id
    try:
        key_id = int(cq.data.split("|")[1])
    except (IndexError, ValueError):
        return await cq.answer(t("ai_key_list_empty"), show_alert=True)

    ok = await set_active_ai_key(user_id, key_id)
    message = t("ai_key_activated") if ok else t("ai_key_list_empty")

    await cq.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_ai")]
        ])
    )


@bot.on_callback_query(filters.regex(r"^config_plugin_ai_remove$"))
@use_lang()
async def config_ai_remove(c: Client, cq: CallbackQuery, t):
    """Lista as chaves cadastradas para o usuário escolher qual remover."""
    user_id = cq.from_user.id
    keys = await get_ai_keys(user_id)

    if not keys:
        return await cq.edit_message_text(
            t("ai_no_key_to_remove"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_ai")]
            ])
        )

    await cq.edit_message_text(
        t("ai_remove_key_title"),
        reply_markup=build_key_list_markup(keys, "config_plugin_ai_delkey", t)
    )


@bot.on_callback_query(filters.regex(r"^config_plugin_ai_delkey\|"))
@use_lang()
async def config_ai_remove_confirm(c: Client, cq: CallbackQuery, t):
    user_id = cq.from_user.id
    try:
        key_id = int(cq.data.split("|")[1])
    except (IndexError, ValueError):
        return await cq.answer(t("ai_key_list_empty"), show_alert=True)

    removed = await remove_ai_key(user_id, key_id)
    message = t("ai_key_removed") if removed else t("ai_no_key_to_remove")

    await cq.edit_message_text(
        message,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_ai")]
        ])
    )

# Adicionar à lista de plugins
from config import plugins
plugins.append("ai")