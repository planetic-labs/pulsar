import torch
import io
import soundfile as sf
from speechbrain.inference.speaker import EncoderClassifier
from typing import Dict, List, Any

class EndpointHandler:
    def __init__(self, path=""):
        # Инициализируем модель при старте контейнера
        # Используем GPU если доступен, иначе CPU
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir="/tmp/model_cache",
            run_opts={"device": device}
        )

    def __call__(self, data: Any) -> List[float]:
        """
        Args:
            data (:obj:):
                includes the deserialized audio file as bytes
        Return:
            A :obj:`list` of floats: the speaker embedding.
        """
        # HF Inference Endpoints передает данные в разных форматах в зависимости от запроса
        # Если прислали просто байты (как мы делаем в приложении), они будут в ключе "inputs" 
        # или прямо в data.
        inputs = data.pop("inputs", data)
        
        if isinstance(inputs, dict) and "bytes" in inputs:
            audio_bytes = inputs["bytes"]
        else:
            audio_bytes = inputs

        # Читаем аудио из байтов
        try:
            audio_data, samplerate = sf.read(io.BytesIO(audio_bytes))
        except Exception as e:
            raise ValueError(f"Error reading audio data: {e}")

        # Преобразуем в тензор [batch, frames]
        # SpeechBrain ожидает 16kHz mono. Мы шлем это из приложения,
        # но для надежности берем только первый канал если пришло стерео.
        if len(audio_data.shape) > 1:
            audio_data = audio_data[:, 0]
            
        waveform = torch.from_numpy(audio_data).float().unsqueeze(0)
        
        # Инференс
        with torch.no_grad():
            embeddings = self.model.encode_batch(waveform)
            # Убираем лишние размерности [1, 1, 192] -> [192]
            vector = embeddings.flatten().tolist()
            
        return vector
