import asyncio
import os
import sys
from pathlib import Path

from hydrogram import Client, filters
from hydrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from db import Config
from locales import use_lang
from version import version as bot_version

# Tentativa de importar a biblioteca docker
try:
    import docker
    from docker.errors import DockerException, NotFound, APIError, ImageNotFound
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False


# --- Funções de Detecção de Ambiente (mesmas de antes) ---
def is_docker_env() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if any(x in content for x in ['docker', 'lxc', 'crio', 'containerd', 'systemd/docker']):
                return True
    except (FileNotFoundError, PermissionError):
        pass
    container_env = os.environ.get('container')
    if container_env and container_env.lower() in ('docker', 'lxc', 'runc', 'containerd'):
        return True
    return False


def has_docker_socket() -> bool:
    socket_path = "/var/run/docker.sock"
    return os.path.exists(socket_path) and os.access(socket_path, os.R_OK | os.W_OK)


def _detect_container_without_socket() -> dict:
    info = {'type': 'unknown', 'reason': 'No docker socket and no fallback info'}
    try:
        with open('/proc/self/cgroup', 'r') as f:
            for line in f:
                if 'docker' in line:
                    parts = line.strip().split('/')
                    if len(parts) > 2 and parts[1] == 'docker':
                        info['type'] = 'docker'
                        info['container_id'] = parts[2]
                        return info
    except (FileNotFoundError, PermissionError):
        pass
    hostname = os.environ.get('HOSTNAME')
    if hostname:
        info['type'] = 'docker'
        info['container_id'] = hostname
        info['reason'] = 'Inferred from HOSTNAME'
    return info


def detect_docker_environment() -> tuple[str, dict]:
    if not DOCKER_AVAILABLE:
        return ('no_docker_socket', {'reason': 'docker library not installed'})

    if is_docker_env() and not has_docker_socket():
        return ('docker_no_socket', {'reason': 'Docker environment detected but no socket access'})

    if not has_docker_socket():
        fallback = _detect_container_without_socket()
        if fallback['type'] == 'docker':
            return ('docker_no_socket', {'reason': f'No socket access. {fallback.get("reason", "")}'})
        return ('no_docker_socket', {'reason': 'Docker socket not accessible'})

    try:
        client = docker.from_env()
        container_id = os.environ.get('HOSTNAME')
        if not container_id:
            return ('unknown', {'reason': 'HOSTNAME env var not found'})

        container = client.containers.get(container_id)
        image = container.image
        repotags = image.attrs.get('RepoTags', [])

        is_remote = any('ghcr.io' in tag for tag in repotags) or \
                    any('/' in tag and '.' in tag.split('/')[0] for tag in repotags)

        has_code_mount = any(
            mount.get('Destination') == '/app' and mount.get('Type') == 'bind'
            for mount in container.attrs.get('Mounts', [])
        )

        if is_remote:
            return ('remote_image', {'repotags': repotags, 'container': container})
        elif has_code_mount:
            return ('local_build', {'repotags': repotags, 'container': container})
        else:
            return ('unknown', {'repotags': repotags, 'reason': 'could not classify'})
    except (DockerException, NotFound, APIError) as e:
        return ('unknown', {'reason': str(e)})
    except Exception as e:
        return ('unknown', {'reason': str(e)})


# --- Verificação de atualização para imagem remota ---
async def check_remote_image_update(image_tag: str) -> tuple[bool, str]:
    try:
        client = docker.from_env()
        try:
            local_image = client.images.get(image_tag)
            local_digest = local_image.attrs.get('RepoDigests', [None])[0]
        except ImageNotFound:
            local_digest = None

        def pull_image():
            return client.images.pull(image_tag)
        remote_image = await asyncio.to_thread(pull_image)
        remote_digest = remote_image.attrs.get('RepoDigests', [None])[0]

        if local_digest == remote_digest and local_digest is not None:
            return False, "up_to_date"
        else:
            return True, "update_available"
    except Exception as e:
        return False, f"error: {str(e)}"


# --- Atualização remota usando container helper ---
async def perform_remote_update_with_helper(image_tag: str, chat_id: int, message_id: int, strings):
    try:
        client = docker.from_env()
        compose_dir = "/app"
        cmd = f"cd {compose_dir} && docker-compose pull && docker-compose up -d"
        volumes = {
            '/var/run/docker.sock': {'bind': '/var/run/docker.sock', 'mode': 'rw'},
            compose_dir: {'bind': '/work', 'mode': 'ro'}
        }
        helper_image = "docker/compose:latest"
        await asyncio.to_thread(client.images.pull, helper_image)
        helper_container = client.containers.create(
            image=helper_image,
            command=["sh", "-c", cmd],
            volumes=volumes,
            working_dir="/work",
            auto_remove=True,
            detach=True
        )
        helper_container.start()
        await Config.update_or_create(
            id="upgrade",
            defaults={"valuej": {"chat_id": chat_id, "message_id": message_id}}
        )
        sys.exit(0)
    except Exception as e:
        print(f"[UPGRADE] Erro ao criar helper: {e}")
        sys.exit(1)


# --- Comando upgrade principal ---
@Client.on_message(filters.command("upgrade", prefixes=".") & filters.me)
@use_lang()
async def upgrade(c: Client, m: Message, strings):
    # Helper para obter string com fallback
    def get_str(key: str, **kwargs) -> str:
        val = strings(key)
        if val is None or val == key:
            # fallback para evitar None
            return f"[{key}]"
        if kwargs:
            try:
                return val.format(**kwargs)
            except KeyError:
                return val
        return val

    # 1. Identificar ambiente
    docker_mode = is_docker_env()
    if not docker_mode:
        env_type = "local"
        env_info = {}
    else:
        env_type, env_info = detect_docker_environment()

    # 2. Mapeamento do texto do modo
    mode_map = {
        "local": "upgrade_mode_local_detected",
        "remote_image": "upgrade_mode_remote_image_detected",
        "local_build": "upgrade_mode_local_build_detected",
        "docker_no_socket": "upgrade_mode_docker_no_socket_detected",
        "unknown": "upgrade_mode_unknown_detected"
    }
    mode_key = mode_map.get(env_type, "upgrade_mode_unknown_detected")
    mode_text = get_str(mode_key)

    header = get_str("upgrade_header")
    searching = get_str("upgrade_searching_updates")

    # Mensagem inicial (buscando)
    initial_msg = f"{header}\n\n{mode_text}\n\n🔍 {searching}"
    await m.edit(initial_msg)

    # 3. Verificar atualizações
    update_available = False
    update_info = {}
    check_message = None
    image_tag = None

    if env_type == "local":
        try:
            proc = await asyncio.create_subprocess_shell(
                "git log -1 --format=%cd --date=short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            local_date = stdout.decode().strip() or "desconhecida"
            update_info['local_date'] = local_date

            proc = await asyncio.create_subprocess_shell(
                "git fetch --dry-run",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                check_message = get_str("upgrade_git_fetch_error", error=stderr.decode())
            else:
                if stdout:
                    update_available = True
                    proc = await asyncio.create_subprocess_shell(
                        "git log -1 --format=%cd --date=short origin/$(git branch --show-current)",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await proc.communicate()
                    remote_date = stdout.decode().strip() or "desconhecida"
                    update_info['remote_date'] = remote_date
                else:
                    update_available = False
        except Exception as e:
            check_message = get_str("upgrade_git_error", error=str(e))

    elif env_type == "remote_image":
        repotags = env_info.get('repotags', [])
        if not repotags:
            check_message = get_str("upgrade_no_image_tag")
        else:
            image_tag = repotags[0]
            available, result = await check_remote_image_update(image_tag)
            if available:
                update_available = True
                try:
                    client = docker.from_env()
                    remote_image = client.images.get(image_tag)
                    created = remote_image.attrs.get('Created')
                    if created:
                        update_info['remote_date'] = created.split('T')[0]
                    version = remote_image.attrs.get('Config', {}).get('Labels', {}).get('version', 'desconhecida')
                    update_info['version'] = version
                except:
                    pass
            else:
                if result == "up_to_date":
                    update_available = False
                else:
                    check_message = get_str("upgrade_remote_check_error", error=result)

    elif env_type == "local_build":
        update_available = False
        check_message = get_str("upgrade_local_build_no_auto_check")
    elif env_type == "docker_no_socket":
        update_available = False
        check_message = get_str("upgrade_docker_no_socket_check")
    else:
        update_available = False
        check_message = get_str("upgrade_unknown_env")

    # 4. Montar mensagem final
    if update_available:
        extra = ""
        if env_type == "local":
            local_date = update_info.get('local_date', get_str('upgrade_unknown_value'))
            remote_date = update_info.get('remote_date', get_str('upgrade_unknown_value'))
            extra = get_str("upgrade_git_update_details", local_date=local_date, remote_date=remote_date)
        elif env_type == "remote_image":
            version_remote = update_info.get('version', get_str('upgrade_unknown_value'))
            date = update_info.get('remote_date', get_str('upgrade_unknown_value'))
            extra = get_str("upgrade_remote_update_details", version=version_remote, date=date)
        final_text = f"{header}\n\n{mode_text}\n{get_str('upgrade_updates_found')}\n{extra}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(get_str("upgrade_button_update"), callback_data=f"upgrade_confirm_{env_type}_{image_tag or 'none'}")]
        ])
        await m.edit(final_text, reply_markup=keyboard)
    else:
        if check_message:
            final_text = f"{header}\n\n{mode_text}\n{check_message}"
        else:
            extra = ""
            if env_type == "local":
                local_date = update_info.get('local_date', get_str('upgrade_unknown_value'))
                extra = get_str("upgrade_git_up_to_date_details", date=local_date)
            elif env_type == "remote_image" and image_tag:
                try:
                    client = docker.from_env()
                    local_image = client.images.get(image_tag)
                    created = local_image.attrs.get('Created')
                    local_date = created.split('T')[0] if created else get_str('upgrade_unknown_value')
                    # Tenta obter a versão da label da imagem, se não existir usa a versão do bot
                    version_local = local_image.attrs.get('Config', {}).get('Labels', {}).get('version')
                    if not version_local:
                        version_local = bot_version
                    extra = get_str("upgrade_remote_up_to_date_details", version=version_local, date=local_date)
                except Exception as e:
                    # Em caso de erro, usa a versão do bot e data desconhecida
                    extra = get_str("upgrade_remote_up_to_date_details", version=bot_version, date=get_str('upgrade_unknown_value'))
            elif env_type == "local_build":
                extra = ""  # já tem mensagem específica em check_message
            elif env_type == "docker_no_socket":
                extra = ""  # já tem mensagem específica em check_message
            else:
                extra = ""
            # Se extra não foi definido (casos sem extra), apenas pula a linha extra
            if extra:
                final_text = f"{header}\n\n{mode_text}\n{get_str('upgrade_no_updates')}\n{extra}"
            else:
                final_text = f"{header}\n\n{mode_text}\n{get_str('upgrade_no_updates')}"
        await m.edit(final_text)


# --- Callback do botão de confirmação ---
@Client.on_callback_query(filters.regex(r"^upgrade_confirm_"))
@use_lang()
async def upgrade_confirm_callback(c: Client, q: CallbackQuery, strings):
    parts = q.data.split("_")
    if len(parts) < 3:
        await q.answer("Invalid callback data", show_alert=True)
        return
    env_type = parts[2]
    image_tag = parts[3] if len(parts) > 3 and parts[3] != 'none' else None

    await q.answer()
    original_message = q.message
    chat_id = original_message.chat.id
    message_id = original_message.id

    # Helper para string
    def get_str(key: str, **kwargs) -> str:
        val = strings(key)
        if val is None or val == key:
            return f"[{key}]"
        if kwargs:
            try:
                return val.format(**kwargs)
            except KeyError:
                return val
        return val

    await original_message.edit_text(get_str("upgrade_starting_update"))

    if env_type == "local":
        try:
            with open(os.path.join(".git", "HEAD")) as f:
                branch = f.read().split("/")[-1].rstrip()
        except FileNotFoundError:
            await original_message.edit_text(get_str("upgrade_not_git_repo"))
            return

        proc = await asyncio.create_subprocess_shell(
            f"git pull --no-edit origin {branch}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        if proc.returncode != 0:
            await original_message.edit_text(get_str("upgrade_failed", branch=branch, returncode=proc.returncode, decode=output))
            return
        if "Already up to date." in output:
            await original_message.edit_text(get_str("upgrade_already_up_to_date", branch=branch))
            return

        await original_message.edit_text(get_str("updating_requirements"))
        proc = await asyncio.create_subprocess_shell(
            "pip install -r requirements.txt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
        if proc.returncode != 0:
            await original_message.edit_text(get_str("upgrade_requirements_update_failed", decode=output))
            return

        await original_message.edit_text(get_str("restarting"))
        await Config.update_or_create(
            id="upgrade",
            defaults={"valuej": {"chat_id": chat_id, "message_id": message_id}},
        )
        os.execl(sys.executable, sys.executable, *sys.argv)

    elif env_type == "remote_image":
        if not image_tag:
            await original_message.edit_text(get_str("upgrade_no_image_tag"))
            return
        asyncio.create_task(perform_remote_update_with_helper(image_tag, chat_id, message_id, strings))
        await asyncio.sleep(1)

    elif env_type == "local_build":
        await original_message.edit_text(get_str("upgrade_local_build_manual_instructions"))
    elif env_type == "docker_no_socket":
        await original_message.edit_text(get_str("upgrade_docker_no_socket_instructions"))
    else:
        await original_message.edit_text(get_str("upgrade_unknown_error"))