FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0

WORKDIR /srv/search-ui

# Установка системных зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgomp1 \
    curl \
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
RUN mkdir -p /srv/search-ui/downloads /srv/search-ui/data /srv/search-ui/storage /srv/search-ui/models_cache && \
    chown -R ${USER_ID}:${GROUP_ID} /srv/search-ui

USER appuser

COPY pyproject.toml README.md ./
# Синхронизация зависимостей
RUN uv sync

COPY app ./app
COPY scripts ./scripts

# По умолчанию ничего не запускаем, команда будет в docker-compose
EXPOSE 8000
EXPOSE 8501
