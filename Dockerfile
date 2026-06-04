FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Установка системных зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgomp1 \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Установка uv для быстрого управления пакетами
RUN pip install --no-cache-dir uv

# Добавляем аргументы для UID и GID (по умолчанию 1000)
ARG USER_ID=1000
ARG GROUP_ID=1000

# Создаем группу и пользователя, если они не существуют
RUN groupadd -g ${GROUP_ID} appuser || true && \
    useradd -l -u ${USER_ID} -g ${GROUP_ID} -m appuser || true

# Создаем папки для монтирования заранее, чтобы у пользователя были права
RUN mkdir -p /app/downloads /app/data /app/storage /app/models_cache && \
    chown -R ${USER_ID}:${GROUP_ID} /app

USER appuser

COPY pyproject.toml README.md ./
# Синхронизация зависимостей
RUN uv sync

COPY app ./app
COPY scripts ./scripts
COPY cron ./cron
COPY templates ./templates
COPY static ./static

# FastAPI port
EXPOSE 8000

