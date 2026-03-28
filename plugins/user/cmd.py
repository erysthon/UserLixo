import asyncio
import io
import os
import docker
from docker.errors import ContainerError, APIError
from hydrogram import Client, filters
from hydrogram.types import Message

from locales import use_lang

# --- VARIÁVEIS DE AMBIENTE ---
DOCKER_SOCKET_PATH = "/var/run/docker.sock"

def run_host_command(command: str) -> str:
    """
    Executa um comando Shell arbitrário no HOST (com acesso a binários e serviços)
    usando um contêiner sidecar temporário com mapeamento do Root do Host.
    """
    try:
        client = docker.from_env()
    except Exception as e:
        raise Exception(f"docker_connection_error:{e}")

    try:
        shell_command = f"chroot /host sh -c '{command}'"
        container = client.containers.run(
            image="alpine:latest",
            command=["sh", "-c", shell_command],
            volumes={
                "/": {'bind': '/host', 'mode': 'ro'},
                DOCKER_SOCKET_PATH: {'bind': DOCKER_SOCKET_PATH, 'mode': 'rw'}
            },
            privileged=True,
            network_mode='host',
            remove=True,
            detach=False
        )
        return container.decode('utf-8').strip()
    except ContainerError as e:
        output = (e.stderr or b'').decode('utf-8').strip()
        if not output:
            output = (e.stdout or b'').decode('utf-8').strip()
        if not output:
            output = f"Código de saída: {e.exit_status}"
        raise Exception(f"container_error:{output}")
    except APIError as e:
        raise Exception(f"docker_api_error:{e}")
    except Exception as e:
        raise Exception(f"docker_unexpected_error:{e}")

@Client.on_message(filters.command("cmd", prefixes=".") & filters.sudoers)
@use_lang()
async def cmd(_, m: Message, t):
    # 1. Parsing do Comando
    text = m.text[5:].strip()
    if not text:
        await m.edit(t("cmd_usage"))
        return

    # 2. Detectar flag -l (apenas -l)
    parts = text.split()
    local_exec = False
    if parts and parts[0] == "-l":
        local_exec = True
        text = " ".join(parts[1:]).strip()
        if not text:
            await m.edit(t("cmd_usage"))
            return

    # 3. Detecção do ambiente
    is_docker_env = os.path.exists("/.dockerenv")
    can_access_docker_daemon = os.path.exists(DOCKER_SOCKET_PATH)

    res = ""
    try:
        # 4. Execução
        if local_exec:
            # Executa no container atual
            proc = await asyncio.create_subprocess_shell(
                text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            # Combina stdout e stderr, priorizando stderr se houver
            output = (stderr or stdout).decode('utf-8').strip()
            if not output:
                output = stdout.decode('utf-8').strip()
            res = output
            # Se houve erro de comando não encontrado, formata com mensagem traduzida
            if proc.returncode != 0 and ("not found" in res.lower() or "command not found" in res.lower()):
                res = t("cmd_command_not_found").format(command=text)
            elif proc.returncode != 0 and res:
                # Outro erro qualquer, mantém a saída
                pass
        else:
            # Lógica original
            if is_docker_env and can_access_docker_daemon:
                res = await asyncio.to_thread(run_host_command, text)
            else:
                proc = await asyncio.create_subprocess_shell(
                    text,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                ex = await proc.communicate()
                res = ex[0].decode().rstrip()
    except Exception as e:
        # Trata exceções vindas de qualquer execução
        error_msg = str(e)
        if error_msg.startswith("docker_connection_error:"):
            original = error_msg.split(":", 1)[1]
            res = t("cmd_docker_connection_error").format(error=original)
        elif error_msg.startswith("docker_api_error:"):
            original = error_msg.split(":", 1)[1]
            res = t("cmd_docker_api_error").format(error=original)
        elif error_msg.startswith("docker_unexpected_error:"):
            original = error_msg.split(":", 1)[1]
            res = t("cmd_docker_unexpected_error").format(error=original)
        elif error_msg.startswith("container_error:"):
            original = error_msg.split(":", 1)[1]
            if "not found" in original.lower():
                res = t("cmd_command_not_found").format(command=text)
            else:
                res = t("cmd_execution_error").format(error=original)
        else:
            res = t("cmd_execution_error").format(error=error_msg)

    # 5. Formatação e Resposta
    if not res:
        final_response = t("cmd_no_output")
    else:
        final_response = res

    if len(final_response) > 4096:
        with io.BytesIO(str.encode(final_response)) as out_file:
            out_file.name = "cmd.txt"
            await m.reply_document(out_file)
    else:
        await m.edit(final_response)