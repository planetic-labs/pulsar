FROM python:3.14-slim

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

# Установка uv для быстрого управления пакетами (рекомендованный способ Astral)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Настройка виртуального окружения в PATH
ENV PATH="/app/.venv/bin:$PATH"

# Добавляем аргументы для UID и GID (по умолчанию 1000)
ARG USER_ID=1000
ARG GROUP_ID=1000

# Создаем группу и пользователя, если они не существуют
RUN groupadd -g ${GROUP_ID} appuser || true && \
    useradd -l -u ${USER_ID} -g ${GROUP_ID} -m appuser || true

# Создаем папки для монтирования заранее, чтобы у пользователя были права
RUN mkdir -p /app/downloads /app/data /app/storage && \
    chown -R ${USER_ID}:${GROUP_ID} /app

USER appuser

COPY pyproject.toml uv.lock README.md ./
# Синхронизация зависимостей
RUN uv sync --frozen

COPY app ./app
COPY scripts ./scripts
COPY cron ./cron
COPY templates ./templates
COPY static ./static

# FastAPI port
EXPOSE 8351

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8351"]
