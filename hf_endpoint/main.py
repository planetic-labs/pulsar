import os
import shutil
from pathlib import Path

import torch
from fastapi import FastAPI, File, UploadFile
from speechbrain.inference.speaker import EncoderClassifier

app = FastAPI()

# Папка для моделей
SAVE_DIR = "pretrained_models/spkrec-ecapa-voxceleb"
os.makedirs(SAVE_DIR, exist_ok=True)

# Загружаем модель (она скачается в кэш при первом запуске)
# EncoderClassifier более прямой способ получения векторов
classifier = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=SAVE_DIR)


@app.post("/embed")
async def get_embedding(file: UploadFile = File(...)):
    # Сохраняем временный файл
    temp_path = Path(f"temp_{file.filename}")
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        # Извлекаем эмбеддинг
        # load_audio возвращает тензор [batch, frames]
        waveform = classifier.load_audio(str(temp_path))

        # На инференсе обычно [1, frames], так что encode_batch вернет [1, 1, 192]
        with torch.no_grad():
            embeddings = classifier.encode_batch(waveform)
            # Убираем лишние размерности [1, 1, 192] -> [192]
            vector = embeddings.flatten().tolist()

        return {"embedding": vector}
    finally:
        # Удаляем временный файл в блоке finally, чтобы он удалился даже при ошибке
        if temp_path.exists():
            temp_path.unlink()


@app.get("/")
def read_root():
    return {"message": "SpeechBrain ECAPA-TDNN is running!"}
