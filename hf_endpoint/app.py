import os
import shutil
from pathlib import Path

import torch
import torch.nn.functional as functional
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from speechbrain.inference.speaker import EncoderClassifier

app = FastAPI()
security = HTTPBearer()

# Секретный токен берется из переменной окружения
API_TOKEN = os.getenv("API_TOKEN", "my-secret-token")

# Папка для моделей
SAVE_DIR = "pretrained_models/spkrec-ecapa-voxceleb"
os.makedirs(SAVE_DIR, exist_ok=True)

# Загружаем модель
classifier = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=SAVE_DIR)


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


@app.post("/embed", dependencies=[Depends(verify_token)])
async def get_embedding(file: UploadFile = File(...)):
    temp_path = Path(f"temp_{file.filename}")
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        waveform = classifier.load_audio(str(temp_path))
        with torch.no_grad():
            embeddings = classifier.encode_batch(waveform)
            # ВАЖНО: Применяем L2-нормализацию
            # embeddings имеет форму [batch, 1, 192]
            normalized_embeddings = functional.normalize(embeddings, p=2, dim=2)
            vector = normalized_embeddings.flatten().tolist()
        return {"embedding": vector}
    finally:
        if temp_path.exists():
            temp_path.unlink()


@app.get("/")
def read_root():
    return {"message": "SpeechBrain ECAPA-TDNN Security Active!"}
