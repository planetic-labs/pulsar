# Используем базовый образ с поддержкой PyTorch
FROM python:3.12-slim

# Установка системных зависимостей (ffmpeg обязателен для аудио)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Создаем рабочего пользователя (HF рекомендует не использовать root)
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:${PATH}"

WORKDIR /app

# Копируем и устанавливаем зависимости
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальной код
COPY --chown=user . .

# Открываем порт 7860 (стандарт для HF Spaces)
EXPOSE 7860

# Запуск приложения
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
