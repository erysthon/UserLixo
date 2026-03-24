import asyncio
import base64
import io
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, List, Optional, Tuple

import aiofiles
import httpx
from hydrogram import Client, filters
from hydrogram.errors import ListenerTimeout
from hydrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import bot
from db import Config, OpenRouterKey, OpenRouterModel
from locales import use_lang
from utils import http

# Cache para conversas em andamento
# Estrutura: {(user_id, chat_id): {"messages": [...], "model": "model_id", "file_context": {...}}}
conversations = {}

# Configurações padrão - modelos gratuitos
DEFAULT_MODELS = [
    {
        "name": "Mistral 7B",
        "id": "mistralai/mistral-7b-instruct:free",
        "supports_search": False,
        "supports_deep_thought": False,
        "supports_files": False,
        "supports_vision": False,
        "supports_function_calling": False,
        "context_length": 8192,
        "free": True
    },
    {
        "name": "Google Gemma 7B",
        "id": "google/gemma-7b-it:free",
        "supports_search": False,
        "supports_deep_thought": False,
        "supports_files": False,
        "supports_vision": False,
        "supports_function_calling": False,
        "context_length": 8192,
        "free": True
    },
    {
        "name": "Nous Hermes 2 Mixtral",
        "id": "nousresearch/nous-hermes-2-mixtral-8x7b-dpo:free",
        "supports_search": False,
        "supports_deep_thought": False,
        "supports_files": False,
        "supports_vision": False,
        "supports_function_calling": False,
        "context_length": 32768,
        "free": True
    }
]

# Constantes
OPENROUTER_API = "https://openrouter.ai/api/v1"
MAX_MESSAGE_LENGTH = 4096
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB
REQUEST_TIMEOUT = 300  # 5 minutos para respostas longas

async def get_openrouter_key(user_id: int) -> Optional[str]:
    """Obtém a chave API do OpenRouter para um usuário."""
    record = await OpenRouterKey.get_or_none(id=user_id)
    return record.api_key if record else None

async def set_openrouter_key(user_id: int, api_key: str) -> None:
    """Define a chave API do OpenRouter para um usuário."""
    record = await OpenRouterKey.get_or_none(id=user_id)
    if record:
        record.api_key = api_key
        await record.save()
    else:
        await OpenRouterKey.create(id=user_id, api_key=api_key)

async def remove_openrouter_key(user_id: int) -> bool:
    """Remove a chave API do OpenRouter de um usuário."""
    record = await OpenRouterKey.get_or_none(id=user_id)
    if record:
        await record.delete()
        return True
    return False

async def get_user_models(user_id: int) -> List[OpenRouterModel]:
    """Obtém todos os modelos configurados pelo usuário."""
    return await OpenRouterModel.filter(user_id=user_id).all()

async def get_default_model(user_id: int) -> Optional[OpenRouterModel]:
    """Obtém o modelo padrão do usuário."""
    return await OpenRouterModel.get_or_none(user_id=user_id, is_default=True)

async def add_model(
    user_id: int, 
    model_name: str, 
    model_id: str,
    supports_search: bool = False,
    supports_deep_thought: bool = False,
    supports_files: bool = False,
    supports_vision: bool = False,
    supports_function_calling: bool = False,
    context_length: int = 4096,
    is_default: bool = False
) -> OpenRouterModel:
    """Adiciona um novo modelo para o usuário."""
    
    # Se for padrão, remove o padrão atual
    if is_default:
        await OpenRouterModel.filter(user_id=user_id, is_default=True).update(is_default=False)
    
    # Cria o novo modelo
    model = await OpenRouterModel.create(
        user_id=user_id,
        model_name=model_name,
        model_id=model_id,
        supports_search=supports_search,
        supports_deep_thought=supports_deep_thought,
        supports_files=supports_files,
        supports_vision=supports_vision,
        supports_function_calling=supports_function_calling,
        context_length=context_length,
        is_default=is_default
    )
    
    return model

async def remove_model(user_id: int, model_id: int) -> bool:
    """Remove um modelo do usuário."""
    model = await OpenRouterModel.get_or_none(id=model_id, user_id=user_id)
    if model:
        await model.delete()
        return True
    return False

async def set_default_model(user_id: int, model_id: int) -> bool:
    """Define um modelo como padrão."""
    model = await OpenRouterModel.get_or_none(id=model_id, user_id=user_id)
    if not model:
        return False
    
    # Remove o padrão atual
    await OpenRouterModel.filter(user_id=user_id, is_default=True).update(is_default=False)
    
    # Define o novo padrão
    model.is_default = True
    await model.save()
    
    return True

async def validate_openrouter_key(api_key: str) -> Tuple[bool, Optional[str]]:
    """Valida uma chave API do OpenRouter."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Testa a chave com uma requisição simples
            response = await client.get(
                f"{OPENROUTER_API}/auth/key",
                headers=headers
            )
            
            if response.status_code == 200:
                return True, "Key valid"
            elif response.status_code == 401:
                return False, "Invalid API key"
            else:
                return False, f"Error {response.status_code}: {response.text[:100]}"
                
    except Exception as e:
        return False, f"Connection error: {str(e)}"

async def test_model_capabilities(api_key: str, model_id: str) -> Dict:
    """Testa as capacidades de um modelo específico."""
    capabilities = {
        "supports_search": False,
        "supports_deep_thought": False,
        "supports_files": False,
        "supports_vision": False,
        "supports_function_calling": False,
        "context_length": 4096,
        "is_working": False,
        "error": None
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # Teste básico do modelo
    test_payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Hello, respond with just 'OK'."}],
        "max_tokens": 5,
        "temperature": 0.1
    }
    
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Teste 1: Modelo básico
            response = await client.post(
                f"{OPENROUTER_API}/chat/completions",
                headers=headers,
                json=test_payload
            )
            
            if response.status_code != 200:
                capabilities["error"] = f"Model test failed: {response.status_code}"
                return capabilities
            
            capabilities["is_working"] = True
            
            # Teste 2: Função de pesquisa (se suportado)
            if "gpt-4" in model_id.lower() or "claude-3" in model_id.lower():
                search_payload = test_payload.copy()
                search_payload["tools"] = [{
                    "type": "function",
                    "function": {
                        "name": "search_web",
                        "description": "Search the web for information",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"}
                            }
                        }
                    }
                }]
                
                search_response = await client.post(
                    f"{OPENROUTER_API}/chat/completions",
                    headers=headers,
                    json=search_payload,
                    timeout=30
                )
                
                if search_response.status_code == 200:
                    capabilities["supports_search"] = True
                    capabilities["supports_function_calling"] = True
            
            # Teste 3: Obter informações do modelo da API
            models_response = await client.get(
                f"{OPENROUTER_API}/models",
                headers=headers,
                timeout=30
            )
            
            if models_response.status_code == 200:
                models_data = models_response.json().get("data", [])
                for model_info in models_data:
                    if model_info["id"] == model_id:
                        # Extrair informações do modelo
                        if "context_length" in model_info:
                            capabilities["context_length"] = model_info["context_length"]
                        
                        # Verificar capacidades baseadas em tags/metadata
                        if "vision" in str(model_info).lower() or "multimodal" in str(model_info).lower():
                            capabilities["supports_vision"] = True
                        
                        if "file" in str(model_info).lower() or "upload" in str(model_info).lower():
                            capabilities["supports_files"] = True
                        
                        break
            
            # Para deep thought, verificamos se o modelo suporta reasoning
            deep_thought_payload = test_payload.copy()
            deep_thought_payload["messages"] = [{
                "role": "user", 
                "content": "Think step by step about what 2+2 equals, then respond with just '4'."
            }]
            
            dt_response = await client.post(
                f"{OPENROUTER_API}/chat/completions",
                headers=headers,
                json=deep_thought_payload,
                timeout=30
            )
            
            if dt_response.status_code == 200:
                # Verificar se a resposta contém raciocínio
                response_text = dt_response.json()["choices"][0]["message"]["content"]
                if "step" in response_text.lower() or "think" in response_text.lower():
                    capabilities["supports_deep_thought"] = True
            
    except Exception as e:
        capabilities["error"] = f"Test error: {str(e)}"
    
    return capabilities

async def read_file_content(file_path: str, mime_type: str) -> Optional[str]:
    """Lê o conteúdo de um arquivo baseado no tipo."""
    try:
        if mime_type.startswith("text/"):
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                return await f.read()
        elif mime_type in ["application/json", "application/xml"]:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                return await f.read()
        else:
            # Para outros tipos, tentamos ler como texto
            try:
                async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    return await f.read()
            except:
                return None
    except Exception as e:
        print(f"Error reading file: {e}")
        return None

async def extract_text_from_message(message: Message) -> Tuple[str, Optional[str]]:
    """Extrai texto de uma mensagem, incluindo arquivos."""
    text = ""
    file_content = None
    
    # Texto direto
    if message.text:
        text = message.text
    elif message.caption:
        text = message.caption
    
    # Arquivos/documentos
    if message.document:
        try:
            file_path = await message.download(in_memory=False)
            file_size = Path(file_path).stat().st_size
            
            if file_size > MAX_FILE_SIZE:
                # Limpa arquivo temporário
                Path(file_path).unlink(missing_ok=True)
                return text, "FILE_TOO_LARGE"
            
            file_content = await read_file_content(
                file_path, 
                message.document.mime_type or "text/plain"
            )
            # Limpa arquivo temporário
            Path(file_path).unlink(missing_ok=True)
        except Exception as e:
            print(f"Error processing document: {e}")
    
    # Fotos (podemos adicionar OCR futuramente)
    elif message.photo:
        # Por enquanto, apenas menciona que há uma foto
        text += "\n[Photo attached]"
    
    return text, file_content

def format_messages_for_api(
    messages: List[Dict], 
    model: OpenRouterModel, 
    query: str,
    file_content: Optional[str] = None
) -> List[Dict]:
    """Formata as mensagens para a API do OpenRouter."""
    api_messages = []
    
    # Adiciona contexto do arquivo se houver
    if file_content and model.supports_files and file_content != "FILE_TOO_LARGE":
        api_messages.append({
            "role": "user",
            "content": f"File content:\n```\n{file_content[:5000]}\n```\n\nQuestion: {query}"
        })
    elif file_content == "FILE_TOO_LARGE":
        api_messages.append({
            "role": "user",
            "content": f"User attached a large file. Please respond accordingly.\n\nQuestion: {query}"
        })
    else:
        api_messages.append({
            "role": "user",
            "content": query
        })
    
    # Adiciona histórico se houver
    for msg in messages[-10:]:  # Mantém apenas últimos 10 mensagens
        api_messages.append(msg)
    
    return api_messages

async def call_openrouter_api(
    api_key: str,
    model: OpenRouterModel,
    messages: List[Dict],
    use_search: bool = False,
    use_deep_thought: bool = False
) -> Dict:
    """Chama a API do OpenRouter."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/erysthon/userlixo",
        "X-Title": "UserLixo Userbot"
    }
    
    payload = {
        "model": model.model_id,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": min(4000, model.context_length - 500),
    }
    
    # Adiciona configurações especiais se suportado
    extras = {}
    
    if use_search and model.supports_search:
        extras["search"] = True
    
    if use_deep_thought and model.supports_deep_thought:
        extras["reasoning"] = True
    
    if extras:
        payload["extras"] = extras
    
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(
            f"{OPENROUTER_API}/chat/completions",
            headers=headers,
            json=payload
        )
        
        if response.status_code != 200:
            error_msg = response.text
            try:
                error_json = response.json()
                if "error" in error_json:
                    error_msg = error_json["error"].get("message", error_msg)
            except:
                pass
            raise Exception(f"API Error {response.status_code}: {error_msg}")
        
        return response.json()

@Client.on_message(filters.command(["ai", "ia", "openrouter"], prefixes=".") & filters.sudoers)
@use_lang()
async def ai_command(c: Client, m: Message, t):
    """Comando principal de IA usando OpenRouter."""
    user_id = m.from_user.id
    chat_id = m.chat.id
    conversation_key = (user_id, chat_id)
    
    # Verifica se o usuário tem chave API
    api_key = await get_openrouter_key(user_id)
    if not api_key:
        await m.edit(
            t("openrouter_no_api_key"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    text=t("openrouter_configure"),
                    callback_data="config_plugin_openrouter"
                )]
            ])
        )
        return
    
    # Obtém o modelo padrão
    default_model = await get_default_model(user_id)
    if not default_model:
        await m.edit(
            t("openrouter_no_models"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    text=t("openrouter_configure_models"),
                    callback_data="config_plugin_openrouter_models"
                )]
            ])
        )
        return
    
    # Processa os argumentos
    args = m.text.split(" ", 1)
    query = args[1] if len(args) > 1 else ""
    
    # Verifica se é resposta a uma mensagem da IA (continua conversa)
    if m.reply_to_message and m.reply_to_message.from_user.id == c.me.id:
        # Recupera conversa existente
        if conversation_key in conversations:
            conversation = conversations[conversation_key]
            model = await OpenRouterModel.get_or_none(
                user_id=user_id,
                model_id=conversation.get("model", default_model.model_id)
            ) or default_model
        else:
            conversation = {"messages": [], "model": default_model.model_id}
            conversations[conversation_key] = conversation
            model = default_model
    else:
        # Nova conversa
        conversation = {"messages": [], "model": default_model.model_id}
        conversations[conversation_key] = conversation
        model = default_model
    
    # Se não há query mas é reply, usa o texto da mensagem respondida
    if not query and m.reply_to_message:
        query, file_content = await extract_text_from_message(m.reply_to_message)
        if not query and not file_content:
            await m.edit(t("openrouter_no_text"))
            return
    elif not query:
        await m.edit(t("openrouter_no_text"))
        return
    else:
        file_content = None
    
    # Verifica se precisa de recursos especiais
    use_search = False
    use_deep_thought = False
    
    query_lower = query.lower()
    search_keywords = ["pesquisa", "pesquisar", "search", "busca", "buscar", "notícias", "news", "noticias"]
    deep_thought_keywords = ["pensamento profundo", "deep thought", "analise detalhada", "análise detalhada", "raciocínio", "pense passo a passo"]
    
    # Detecção automática de necessidades
    if any(word in query_lower for word in search_keywords):
        if model.supports_search:
            use_search = True
        else:
            await m.edit(t("openrouter_search_not_supported"))
            return
    
    if any(word in query_lower for word in deep_thought_keywords):
        if model.supports_deep_thought:
            use_deep_thought = True
        else:
            await m.edit(t("openrouter_deep_thought_not_supported"))
            return
    
    # Envia mensagem de processamento
    processing_text = t("openrouter_processing")
    if use_search:
        processing_text = t("openrouter_searching")
    elif use_deep_thought:
        processing_text = t("openrouter_deep_thought")
    
    processing_msg = await m.edit(processing_text)
    
    try:
        # Prepara as mensagens
        messages = conversation["messages"]
        formatted_messages = format_messages_for_api(
            messages, 
            model, 
            query, 
            file_content
        )
        
        # Chama a API
        start_time = time.time()
        response_data = await call_openrouter_api(
            api_key,
            model,
            formatted_messages,
            use_search=use_search,
            use_deep_thought=use_deep_thought
        )
        processing_time = time.time() - start_time
        
        # Processa a resposta
        if "choices" not in response_data or not response_data["choices"]:
            await processing_msg.edit(t("openrouter_no_response"))
            return
        
        choice = response_data["choices"][0]
        response_text = choice["message"]["content"]
        
        # Adiciona informações de processamento
        if use_deep_thought:
            response_text = f"⏱️ {t('openrouter_deep_thought_time')}: {processing_time:.1f}s\n\n{response_text}"
        
        # Adiciona informações de pesquisa se usada
        if use_search:
            response_text = f"🔍 {t('openrouter_search_activated')}\n\n{response_text}"
        
        # Atualiza histórico
        conversation["messages"].append({"role": "user", "content": query})
        conversation["messages"].append({"role": "assistant", "content": response_text})
        
        # Limita o histórico
        if len(conversation["messages"]) > 20:
            conversation["messages"] = conversation["messages"][-20:]
        
        # Verifica tamanho da resposta
        if len(response_text) > MAX_MESSAGE_LENGTH:
            # Envia como arquivo
            with io.BytesIO(response_text.encode()) as file:
                file.name = f"ai_response_{int(time.time())}.txt"
                await m.reply_document(
                    file,
                    caption=t("openrouter_response_too_long")
                )
            await processing_msg.delete()
        else:
            await processing_msg.edit(response_text)
        
    except Exception as e:
        error_msg = str(e)
        if len(error_msg) > 200:
            error_msg = error_msg[:200] + "..."
        
        await processing_msg.edit(
            t("openrouter_error").format(error=error_msg)
        )

@Client.on_message(filters.command("aiclear", prefixes=".") & filters.sudoers)
@use_lang()
async def ai_clear(c: Client, m: Message, t):
    """Limpa o histórico de conversa."""
    user_id = m.from_user.id
    chat_id = m.chat.id
    conversation_key = (user_id, chat_id)
    
    if conversation_key in conversations:
        del conversations[conversation_key]
    
    await m.edit(t("openrouter_conversation_cleared"))

@Client.on_message(filters.command("aimodels", prefixes=".") & filters.sudoers)
@use_lang()
async def ai_models(c: Client, m: Message, t):
    """Lista os modelos disponíveis."""
    user_id = m.from_user.id
    models = await get_user_models(user_id)
    
    if not models:
        await m.edit(
            t("openrouter_no_models"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    text=t("openrouter_configure_models"),
                    callback_data="config_plugin_openrouter_models"
                )]
            ])
        )
        return
    
    text = t("openrouter_your_models") + "\n\n"
    for model in models:
        text += f"• **{model.model_name}** (`{model.model_id}`)"
        if model.is_default:
            text += " ⭐"
        
        # Mostra capacidades
        capabilities = []
        if model.supports_search:
            capabilities.append("🔍")
        if model.supports_deep_thought:
            capabilities.append("💭")
        if model.supports_files:
            capabilities.append("📎")
        if model.supports_vision:
            capabilities.append("👁️")
        if model.supports_function_calling:
            capabilities.append("⚙️")
        
        if capabilities:
            text += f" {''.join(capabilities)}"
        
        text += f"\n  └ {t('openrouter_context_length')}: {model.context_length} tokens\n"
    
    await m.edit(text)

# Menu de configuração via bot
@bot.on_callback_query(filters.regex(r"\bconfig_plugin_openrouter\b"))
@use_lang()
async def config_openrouter(c: Client, cq: CallbackQuery, t):
    """Menu principal de configuração do OpenRouter."""
    user_id = cq.from_user.id
    current_key = await get_openrouter_key(user_id)
    
    if current_key:
        key_status = t("openrouter_has_key").format(
            masked=current_key[:4] + "****" + current_key[-4:]
        )
    else:
        key_status = t("openrouter_no_key")
    
    await cq.edit_message_text(
        f"{t('openrouter_settings_title')}\n\n{key_status}",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    text=t("openrouter_set_key"),
                    callback_data="config_plugin_openrouter_key"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("openrouter_manage_models"),
                    callback_data="config_plugin_openrouter_models"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("openrouter_add_default_models"),
                    callback_data="config_plugin_openrouter_default"
                )
            ],
            [InlineKeyboardButton(text=t("back"), callback_data="config_plugins")]
        ])
    )

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_key"))
@use_lang()
async def config_openrouter_key(c: Client, cq: CallbackQuery, t):
    """Configurar chave API do OpenRouter."""
    user_id = cq.from_user.id
    
    await cq.edit_message_text(
        t("openrouter_enter_key"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t("cancel"), callback_data="config_plugin_openrouter")]
        ])
    )
    
    # Aguarda resposta do usuário
    try:
        key_msg = await cq.message.chat.listen(
            filters.text & filters.user(user_id),
            timeout=60
        )
    except ListenerTimeout:
        await cq.edit_message_text(
            t("openrouter_timeout"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter")]
            ])
        )
        return
    
    new_key = key_msg.text.strip()
    
    if new_key.lower() == "/cancel":
        await cq.edit_message_text(
            t("canceled"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter")]
            ])
        )
        return
    
    # Valida a chave
    await cq.edit_message_text(t("openrouter_validating_key"))
    
    is_valid, message = await validate_openrouter_key(new_key)
    
    if not is_valid:
        await cq.edit_message_text(
            t("openrouter_invalid_key").format(error=message),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("try_again"), callback_data="config_plugin_openrouter_key")]
            ])
        )
        return
    
    # Salva a chave
    await set_openrouter_key(user_id, new_key)
    
    await cq.edit_message_text(
        t("openrouter_key_set"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter")]
        ])
    )

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_models"))
@use_lang()
async def config_openrouter_models(c: Client, cq: CallbackQuery, t):
    """Gerenciar modelos do OpenRouter."""
    user_id = cq.from_user.id
    models = await get_user_models(user_id)
    
    if not models:
        text = t("openrouter_no_models_yet")
    else:
        text = t("openrouter_your_models_list") + "\n\n"
        for model in models:
            text += f"• **{model.model_name}** (`{model.model_id}`)"
            if model.is_default:
                text += " ⭐"
            text += f" [ID: {model.id}]\n"
    
    buttons = []
    
    if models:
        buttons.append([
            InlineKeyboardButton(
                text=t("openrouter_set_default"),
                callback_data="config_plugin_openrouter_set_default"
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                text=t("openrouter_remove_model"),
                callback_data="config_plugin_openrouter_remove"
            )
        ])
    
    buttons.append([
        InlineKeyboardButton(
            text=t("openrouter_add_custom_model"),
            callback_data="config_plugin_openrouter_add"
        )
    ])
    buttons.append([
        InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter")
    ])
    
    await cq.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_default"))
@use_lang()
async def config_openrouter_default(c: Client, cq: CallbackQuery, t):
    """Adicionar modelos padrão gratuitos."""
    user_id = cq.from_user.id
    
    text = t("openrouter_default_models_list") + "\n\n"
    
    for i, model in enumerate(DEFAULT_MODELS):
        text += f"{i+1}. **{model['name']}**\n"
        text += f"   ID: `{model['id']}`\n"
        text += f"   📊 {t('openrouter_context_length')}: {model['context_length']} tokens\n"
        
        if model['free']:
            text += f"   💰 {t('openrouter_free_model')}\n"
        
        text += "\n"
    
    buttons = []
    
    for i, model in enumerate(DEFAULT_MODELS):
        buttons.append([
            InlineKeyboardButton(
                text=f"➕ {model['name']}",
                callback_data=f"config_plugin_openrouter_add_default_{i}"
            )
        ])
    
    buttons.append([
        InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter")
    ])
    
    await cq.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_add_default_"))
@use_lang()
async def config_openrouter_add_default(c: Client, cq: CallbackQuery, t):
    """Adiciona um modelo padrão."""
    user_id = cq.from_user.id
    model_index = int(cq.data.split("_")[-1])
    
    if model_index < 0 or model_index >= len(DEFAULT_MODELS):
        await cq.edit_message_text(
            t("openrouter_invalid_model"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_default")]
            ])
        )
        return
    
    model_data = DEFAULT_MODELS[model_index]
    
    # Verifica se já existe
    existing = await OpenRouterModel.get_or_none(
        user_id=user_id,
        model_id=model_data["id"]
    )
    
    if existing:
        await cq.edit_message_text(
            t("openrouter_model_already_exists"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_default")]
            ])
        )
        return
    
    # Verifica se o usuário tem chave API
    api_key = await get_openrouter_key(user_id)
    if not api_key:
        await cq.edit_message_text(
            t("openrouter_no_key_to_add_model"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter")]
            ])
        )
        return
    
    # Testa o modelo antes de adicionar
    await cq.edit_message_text(t("openrouter_testing_model"))
    
    capabilities = await test_model_capabilities(api_key, model_data["id"])
    
    if not capabilities["is_working"]:
        await cq.edit_message_text(
            t("openrouter_model_test_failed").format(
                model=model_data["name"],
                error=capabilities.get("error", t("openrouter_unknown_error"))
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_default")]
            ])
        )
        return
    
    # Adiciona o modelo
    is_default = not await OpenRouterModel.filter(user_id=user_id).exists()
    
    await add_model(
        user_id=user_id,
        model_name=model_data["name"],
        model_id=model_data["id"],
        supports_search=capabilities["supports_search"],
        supports_deep_thought=capabilities["supports_deep_thought"],
        supports_files=capabilities["supports_files"],
        supports_vision=capabilities["supports_vision"],
        supports_function_calling=capabilities["supports_function_calling"],
        context_length=capabilities["context_length"],
        is_default=is_default
    )
    
    await cq.edit_message_text(
        t("openrouter_model_added_success").format(
            model=model_data["name"],
            capabilities=t("openrouter_capabilities") + ": " + ", ".join([
                t("openrouter_cap_search") if capabilities["supports_search"] else None,
                t("openrouter_cap_deep_thought") if capabilities["supports_deep_thought"] else None,
                t("openrouter_cap_files") if capabilities["supports_files"] else None,
                t("openrouter_cap_vision") if capabilities["supports_vision"] else None,
                t("openrouter_cap_function_calling") if capabilities["supports_function_calling"] else None,
            ])
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
        ])
    )

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_add"))
@use_lang()
async def config_openrouter_add(c: Client, cq: CallbackQuery, t):
    """Adicionar um modelo personalizado."""
    user_id = cq.from_user.id
    
    # Verifica se o usuário tem chave API
    api_key = await get_openrouter_key(user_id)
    if not api_key:
        await cq.edit_message_text(
            t("openrouter_no_key_to_add_model"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )
        return
    
    await cq.edit_message_text(
        t("openrouter_add_model_name"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t("cancel"), callback_data="config_plugin_openrouter_models")]
        ])
    )
    
    # Passo 1: Nome do modelo
    try:
        name_msg = await cq.message.chat.listen(
            filters.text & filters.user(user_id),
            timeout=60
        )
    except ListenerTimeout:
        await cq.edit_message_text(
            t("openrouter_timeout"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )
        return
    
    if name_msg.text.lower() == "/cancel":
        await cq.edit_message_text(
            t("canceled"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )
        return
    
    model_name = name_msg.text.strip()
    
    # Passo 2: ID do modelo
    await cq.edit_message_text(
        t("openrouter_add_model_id"),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t("cancel"), callback_data="config_plugin_openrouter_models")]
        ])
    )
    
    try:
        id_msg = await cq.message.chat.listen(
            filters.text & filters.user(user_id),
            timeout=60
        )
    except ListenerTimeout:
        await cq.edit_message_text(
            t("openrouter_timeout"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )
        return
    
    if id_msg.text.lower() == "/cancel":
        await cq.edit_message_text(
            t("canceled"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )
        return
    
    model_id = id_msg.text.strip()
    
    # Testa o modelo
    await cq.edit_message_text(t("openrouter_testing_model_capabilities"))
    
    capabilities = await test_model_capabilities(api_key, model_id)
    
    if not capabilities["is_working"]:
        await cq.edit_message_text(
            t("openrouter_model_test_failed").format(
                model=model_name,
                error=capabilities.get("error", t("openrouter_unknown_error"))
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("try_again"), callback_data="config_plugin_openrouter_add")]
            ])
        )
        return
    
    # Mostra resultados do teste
    text = t("openrouter_model_test_results").format(model=model_name, id=model_id)
    text += f"\n\n{t('openrouter_detected_capabilities')}:\n"
    
    if capabilities["supports_search"]:
        text += f"✅ {t('openrouter_cap_search')}\n"
    else:
        text += f"❌ {t('openrouter_cap_search')}\n"
    
    if capabilities["supports_deep_thought"]:
        text += f"✅ {t('openrouter_cap_deep_thought')}\n"
    else:
        text += f"❌ {t('openrouter_cap_deep_thought')}\n"
    
    if capabilities["supports_files"]:
        text += f"✅ {t('openrouter_cap_files')}\n"
    else:
        text += f"❌ {t('openrouter_cap_files')}\n"
    
    if capabilities["supports_vision"]:
        text += f"✅ {t('openrouter_cap_vision')}\n"
    else:
        text += f"❌ {t('openrouter_cap_vision')}\n"
    
    if capabilities["supports_function_calling"]:
        text += f"✅ {t('openrouter_cap_function_calling')}\n"
    else:
        text += f"❌ {t('openrouter_cap_function_calling')}\n"
    
    text += f"\n{t('openrouter_context_length')}: {capabilities['context_length']} tokens"
    text += f"\n\n{t('openrouter_set_default_question')}"
    
    buttons = [
        [
            InlineKeyboardButton(
                text=t("yes"),
                callback_data=f"config_plugin_openrouter_add_confirm_{model_name}_{model_id}_1"
            ),
            InlineKeyboardButton(
                text=t("no"),
                callback_data=f"config_plugin_openrouter_add_confirm_{model_name}_{model_id}_0"
            )
        ],
        [
            InlineKeyboardButton(
                text=t("cancel"),
                callback_data="config_plugin_openrouter_models"
            )
        ]
    ]
    
    await cq.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_add_confirm_"))
@use_lang()
async def config_openrouter_add_confirm(c: Client, cq: CallbackQuery, t):
    """Confirma a adição do modelo."""
    data_parts = cq.data.split("_")
    model_name = data_parts[5]
    model_id = data_parts[6]
    is_default = bool(int(data_parts[7]))
    
    user_id = cq.from_user.id
    
    # Obtém a chave API para testar novamente
    api_key = await get_openrouter_key(user_id)
    capabilities = await test_model_capabilities(api_key, model_id)
    
    # Adiciona o modelo
    await add_model(
        user_id=user_id,
        model_name=model_name,
        model_id=model_id,
        supports_search=capabilities["supports_search"],
        supports_deep_thought=capabilities["supports_deep_thought"],
        supports_files=capabilities["supports_files"],
        supports_vision=capabilities["supports_vision"],
        supports_function_calling=capabilities["supports_function_calling"],
        context_length=capabilities["context_length"],
        is_default=is_default
    )
    
    await cq.edit_message_text(
        t("openrouter_model_added_success").format(
            model=model_name,
            capabilities=t("openrouter_capabilities") + ": " + ", ".join(filter(None, [
                t("openrouter_cap_search") if capabilities["supports_search"] else None,
                t("openrouter_cap_deep_thought") if capabilities["supports_deep_thought"] else None,
                t("openrouter_cap_files") if capabilities["supports_files"] else None,
                t("openrouter_cap_vision") if capabilities["supports_vision"] else None,
                t("openrouter_cap_function_calling") if capabilities["supports_function_calling"] else None,
            ]))
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
        ])
    )

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_set_default"))
@use_lang()
async def config_openrouter_set_default_select(c: Client, cq: CallbackQuery, t):
    """Seleciona um modelo para definir como padrão."""
    user_id = cq.from_user.id
    models = await get_user_models(user_id)
    
    if not models:
        await cq.edit_message_text(
            t("openrouter_no_models_yet"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )
        return
    
    buttons = []
    
    for model in models:
        if not model.is_default:
            buttons.append([
                InlineKeyboardButton(
                    text=model.model_name,
                    callback_data=f"config_plugin_openrouter_set_default_id_{model.id}"
                )
            ])
    
    buttons.append([
        InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")
    ])
    
    await cq.edit_message_text(
        t("openrouter_select_model_to_default"),
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_set_default_id_"))
@use_lang()
async def config_openrouter_set_default_execute(c: Client, cq: CallbackQuery, t):
    """Define um modelo como padrão."""
    user_id = cq.from_user.id
    model_id = int(cq.data.split("_")[-1])
    
    success = await set_default_model(user_id, model_id)
    
    if success:
        model = await OpenRouterModel.get(id=model_id)
        await cq.edit_message_text(
            t("openrouter_default_set").format(model=model.model_name),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )
    else:
        await cq.edit_message_text(
            t("openrouter_set_default_error"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_remove"))
@use_lang()
async def config_openrouter_remove_select(c: Client, cq: CallbackQuery, t):
    """Seleciona um modelo para remover."""
    user_id = cq.from_user.id
    models = await get_user_models(user_id)
    
    if not models:
        await cq.edit_message_text(
            t("openrouter_no_models_yet"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )
        return
    
    buttons = []
    
    for model in models:
        buttons.append([
            InlineKeyboardButton(
                text=f"🗑️ {model.model_name}",
                callback_data=f"config_plugin_openrouter_remove_id_{model.id}"
            )
        ])
    
    buttons.append([
        InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")
    ])
    
    await cq.edit_message_text(
        t("openrouter_select_model_to_remove"),
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@bot.on_callback_query(filters.regex(r"config_plugin_openrouter_remove_id_"))
@use_lang()
async def config_openrouter_remove_execute(c: Client, cq: CallbackQuery, t):
    """Remove um modelo."""
    user_id = cq.from_user.id
    model_id = int(cq.data.split("_")[-1])
    
    model = await OpenRouterModel.get_or_none(id=model_id, user_id=user_id)
    
    if not model:
        await cq.edit_message_text(
            t("openrouter_model_not_found"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
            ])
        )
        return
    
    model_name = model.model_name
    was_default = model.is_default
    
    await remove_model(user_id, model_id)
    
    # Se era o padrão e ainda há outros modelos, define um novo padrão
    if was_default:
        remaining_models = await get_user_models(user_id)
        if remaining_models:
            await set_default_model(user_id, remaining_models[0].id)
    
    await cq.edit_message_text(
        t("openrouter_model_removed").format(model=model_name),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(text=t("back"), callback_data="config_plugin_openrouter_models")]
        ])
    )

# Adicionar à lista de plugins
from config import plugins
plugins.append("openrouter")