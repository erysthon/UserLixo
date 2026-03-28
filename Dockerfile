FROM python:3.11-slim

WORKDIR /app

# Variáveis de ambiente
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

# Instalar dependências do sistema
COPY --from=denoland/deno:bin-2.0.2 /deno /usr/local/bin/deno
RUN apt-get update --fix-missing && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libgl1 \
        libglib2.0-0 \
        tzdata \
        git \
        ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependências
COPY requirements.txt .
# Atualizar o pip antes de instalar os pacotes
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar o restante do código
COPY . .

# Comando para rodar o bot
CMD ["python", "bot.py"]