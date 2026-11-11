"""SpeechBrain ECAPA-TDNN: извлечение embedding голоса.

Возвращает np.array (192,) float32. Работает на CPU, ~1 сек на 10-сек кусок.

ВАЖНО (Windows):
- HF_HOME на тот же диск, что savedir (E:), чтобы не заполнять C:.
- local_strategy=LocalStrategy.COPY (enum, не строка!) — всегда копирует,
  не пытается symlink (который требует прав на Windows).
"""

import os

_PROJECT_DIR = os.getcwd()
_HF_HOME = os.path.join(_PROJECT_DIR, ".hf_cache")
os.makedirs(_HF_HOME, exist_ok=True)
os.environ["HF_HOME"] = _HF_HOME
os.environ["HUGGINGFACE_HUB_CACHE"] = os.path.join(_HF_HOME, "hub")
os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

import numpy as np
import torch

# Определяем стратегию копирования (enum, не строка!)
_LOCAL_STRATEGY = None
try:
    from speechbrain.utils.fetching import LocalStrategy
    for _name in ("COPY", "COPY_SKIP_CACHE_OVERWRITE_IF_LINK_FAILS",
                  "COPY_SKIP_CACHE", "COPY_SKIP_CACHE_OVERWRITE"):
        if hasattr(LocalStrategy, _name):
            _LOCAL_STRATEGY = getattr(LocalStrategy, _name)
            break
except ImportError:
    pass

_speechbrain_model = None
SPEECHBRAIN_AVAILABLE = False


def _load_model():
    """Ленивая загрузка SpeechBrain ECAPA (один раз)."""
    global _speechbrain_model, SPEECHBRAIN_AVAILABLE
    if _speechbrain_model is not None:
        return _speechbrain_model
    try:
        from speechbrain.inference.speaker import EncoderClassifier
        print("[DL] Загружаю SpeechBrain ECAPA (может занять 30–60 секунд)...")
        model_dir = os.path.join(_PROJECT_DIR, "pretrained_models", "spkrec-ecapa-voxceleb")
        os.makedirs(model_dir, exist_ok=True)
        kwargs = {
            "source": "speechbrain/spkrec-ecapa-voxceleb",
            "savedir": model_dir,
            "run_opts": {"device": "cpu"},
        }
        if _LOCAL_STRATEGY is not None:
            kwargs["local_strategy"] = _LOCAL_STRATEGY
            print(f"   [LIST] local_strategy = {_LOCAL_STRATEGY}")
        _speechbrain_model = EncoderClassifier.from_hparams(**kwargs)
        SPEECHBRAIN_AVAILABLE = True
        print("[OK] SpeechBrain ECAPA OK")
        return _speechbrain_model
    except Exception as e:
        print(f"[ERR] SpeechBrain не загрузился: {e}")
        SPEECHBRAIN_AVAILABLE = False
        return None


def _load_waveform(audio_path: str, start_sec: float = None, end_sec: float = None,
                   target_sr: int = 16000) -> torch.Tensor | None:
    """Загружает аудио через soundfile (обход torchcodec).

    Возвращает torch.Tensor [1, N] на target_sr.
    """
    try:
        import soundfile as sf
        info = sf.info(audio_path)
        sr = info.samplerate
        start_frame = int((start_sec or 0) * sr)
        end_frame = int(end_sec * sr) if end_sec else None
        data, _ = sf.read(audio_path, start=start_frame, stop=end_frame,
                          dtype="float32", always_2d=True)
        # data: [N, channels] → [channels, N]
        waveform = torch.from_numpy(data.T)
        # Моно
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        # Ресемплинг до 16000
        if sr != target_sr:
            import torchaudio.functional as F
            waveform = F.resample(waveform, orig_freq=sr, new_freq=target_sr)
        return waveform
    except Exception as e:
        print(f"[WARN]  _load_waveform: {e}")
        return None


def get_embedding(audio_path: str, start_sec: float = None,
                  end_sec: float = None) -> np.ndarray | None:
    """Извлекает embedding голоса из куска аудио [start_sec, end_sec].

    Возвращает np.array (192,) float32 или None при ошибке.
    """
    model = _load_model()
    if model is None:
        return None
    waveform = _load_waveform(audio_path, start_sec, end_sec, target_sr=16000)
    if waveform is None or waveform.shape[1] < 16000 * 1:
        # слишком короткий (< 1 сек)
        return None
    try:
        with torch.no_grad():
            emb = model.encode_batch(waveform)  # [1, 1, 192]
        emb_np = emb.squeeze().cpu().numpy().astype(np.float32)
        if emb_np.ndim == 0 or len(emb_np) == 0:
            return None
        return emb_np
    except Exception as e:
        print(f"[WARN]  get_embedding: {e}")
        return None


def cosine_similarity(e1: np.ndarray, e2: np.ndarray) -> float:
    """Косинусное сходство двух embedding'ов."""
    if e1 is None or e2 is None:
        return 0.0
    n1 = e1 / (np.linalg.norm(e1) + 1e-8)
    n2 = e2 / (np.linalg.norm(e2) + 1e-8)
    return float(np.dot(n1, n2))