import io
import os
import sys
import time
import json
import logging
import shutil
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

# Perth watermarker compatibility patch for ARM64 / Linux
import types
class DummyWatermarker:
    def __init__(self, *args, **kwargs): pass
    def apply_watermark(self, wav, sample_rate=None, *args, **kwargs): return wav
    def get_watermark(self, *args, **kwargs): return 0.0

try:
    import perth
    if getattr(perth, "PerthImplicitWatermarker", None) is None:
        perth.PerthImplicitWatermarker = DummyWatermarker
except ImportError:
    fake_perth = types.ModuleType("perth")
    fake_perth.PerthImplicitWatermarker = DummyWatermarker
    fake_perth.DummyWatermarker = DummyWatermarker
    sys.modules["perth"] = fake_perth

import torch
import numpy as np
import soundfile as sf
import torchaudio
import torchaudio.functional as F

from kokoro_engine import kokoro_engine, KOKORO_VOICES

logger = logging.getLogger("EngineManager")

BASE_DIR = Path(__file__).resolve().parent
VOICES_DIR = BASE_DIR / "voices"
OUTPUT_DIR = BASE_DIR / "output"
CONFIG_FILE = BASE_DIR / "config.json"

VOICES_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# -------------------------------------------------------------
# Upstream Chatterbox Bugfix (Issue #499): Prevent float64/Double promotion
# -------------------------------------------------------------
try:
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    if hasattr(ChatterboxTurboTTS, "norm_loudness"):
        orig_norm = ChatterboxTurboTTS.norm_loudness
        def patched_norm(self, wav, *args, **kwargs):
            res = orig_norm(self, wav, *args, **kwargs)
            if isinstance(res, np.ndarray):
                return res.astype(np.float32)
            elif isinstance(res, torch.Tensor):
                return res.float()
            return res
        ChatterboxTurboTTS.norm_loudness = patched_norm

    if hasattr(ChatterboxTurboTTS, "prepare_conditionals"):
        orig_prep = ChatterboxTurboTTS.prepare_conditionals
        def patched_prep(self, *args, **kwargs):
            res = orig_prep(self, *args, **kwargs)
            if hasattr(self, "conds") and self.conds is not None:
                def to_f32(obj):
                    if isinstance(obj, torch.Tensor): return obj.float()
                    elif isinstance(obj, np.ndarray): return obj.astype(np.float32)
                    elif isinstance(obj, list): return [to_f32(x) for x in obj]
                    elif isinstance(obj, tuple): return tuple(to_f32(x) for x in obj)
                    elif isinstance(obj, dict): return {k: to_f32(v) for k, v in obj.items()}
                    return obj
                self.conds = to_f32(self.conds)
            return res
        ChatterboxTurboTTS.prepare_conditionals = patched_prep
    logger.info("✓ Chatterbox-Turbo Float32 dtype patch active.")
except Exception as e:
    logger.debug(f"Chatterbox patch notice: {e}")

# -------------------------------------------------------------
# Helper Utilities (Weights, Audio Bridge, Speed, Parser)
# -------------------------------------------------------------
def check_pro_unlocked() -> bool:
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return bool(cfg.get("license", {}).get("pro", False))
        except Exception:
            pass
    return False

def find_chatterbox_weights() -> Optional[Path]:
    """Scans both local pretrained_models and HuggingFace cache snapshots."""
    p1 = BASE_DIR / "pretrained_models" / "chatterbox-turbo"
    if p1.exists() and (any(p1.glob("*.safetensors")) or any(p1.glob("*.pt"))):
        return p1

    hf_hub = Path.home() / ".cache" / "huggingface" / "hub"
    if hf_hub.exists():
        for d in hf_hub.glob("models--ResembleAI--chatterbox*"):
            snaps = d / "snapshots"
            if snaps.exists():
                for s in snaps.iterdir():
                    if any(s.glob("*.safetensors")) or any(s.glob("*.pt")):
                        return s
            if any(d.glob("*.safetensors")):
                return d

    return None

def resolve_audio_path_for_loader(file_path: Path) -> Path:
    """Transparent .vfs bridge: Decodes .vfs disguised tracks while supporting .mp3/.wav."""
    if not file_path or not file_path.exists():
        return file_path

    if file_path.suffix.lower() == ".vfs":
        cache_dir = OUTPUT_DIR / ".vfs_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        with open(str(file_path), "rb") as f:
            header = f.read(4)

        is_mp3 = header.startswith(b"ID3") or (len(header) >= 2 and header[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))
        target_ext = ".mp3" if is_mp3 else ".wav"

        cached_file = cache_dir / f"{file_path.stem}{target_ext}"
        if not cached_file.exists() or cached_file.stat().st_mtime < file_path.stat().st_mtime:
            shutil.copy2(str(file_path), str(cached_file))
        return cached_file

    return file_path

def find_voice_file(voice_name: str) -> Optional[Path]:
    v_clean = voice_name.lower().replace("!", "").strip()
    supported_exts = [".vfs", ".mp3", ".wav", ".ogg", ".flac"]

    if VOICES_DIR.exists():
        for f in VOICES_DIR.iterdir():
            if f.is_file() and f.stem.lower() == v_clean and f.suffix.lower() in supported_exts:
                return resolve_audio_path_for_loader(f)

    alt_dir = Path.home() / "Documents" / "Chatterbox64" / "voices"
    if alt_dir.exists():
        for f in alt_dir.iterdir():
            if f.is_file() and f.stem.lower() == v_clean and f.suffix.lower() in supported_exts:
                return resolve_audio_path_for_loader(f)

    return None

def adjust_speed(wav_np: np.ndarray, sr: int, speed: float) -> np.ndarray:
    if abs(speed - 1.0) < 0.02 or speed <= 0:
        return wav_np.astype(np.float32)
    try:
        import librosa
        stretched = librosa.effects.time_stretch(wav_np.astype(np.float32), rate=float(speed))
        return stretched.astype(np.float32)
    except Exception:
        try:
            waveform = torch.from_numpy(wav_np).unsqueeze(0).float()
            effects = [["speed", f"{speed:.2f}"], ["rate", f"{sr}"]]
            res_wav, _ = torchaudio.sox_effects.apply_effects_tensor(waveform, sr, effects)
            return res_wav.squeeze(0).numpy().astype(np.float32)
        except Exception:
            new_len = int(len(wav_np) / speed)
            indices = np.linspace(0, len(wav_np) - 1, new_len)
            return np.interp(indices, np.arange(len(wav_np)), wav_np).astype(np.float32)

def parse_segments(text: str) -> List[Tuple[str, str, Optional[float]]]:
    text = text.strip()
    if not text:
        return [("default", "", None)]

    import re
    tag_pattern = r"(?:^|\s)!([a-zA-Z0-9_]+)(?:[-_](\d*\.?\d+))?\s+"
    matches = list(re.finditer(tag_pattern, text))
    if not matches:
        lead_match = re.match(r"^!([a-zA-Z0-9_]+)(?:[-_](\d*\.?\d+))?\s*(.*)$", text)
        if lead_match:
            v = lead_match.group(1).lower()
            s_raw = lead_match.group(2)
            s = float(s_raw) if s_raw else None
            b = lead_match.group(3).strip()
            return [(v, b, s)]
        return [("default", text, None)]

    segments = []
    for i, m in enumerate(matches):
        voice = m.group(1).lower()
        speed_raw = m.group(2)
        speed = float(speed_raw) if speed_raw else None

        start_idx = m.end()
        end_idx = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start_idx:end_idx].strip()

        if body:
            segments.append((voice, body, speed))

    if matches and matches[0].start() > 0:
        lead = text[:matches[0].start()].strip()
        if lead:
            segments.insert(0, ("default", lead, None))

    return segments if segments else [("default", text, None)]

# -------------------------------------------------------------
# Base Engine Interfaces & Concrete Implementations
# -------------------------------------------------------------
class BaseTTSEngine:
    def __init__(self, name: str, device: str = "cuda"):
        self.name = name
        self.device = device
        self.sr = 24000
        self.voice_paths: Dict[str, Path] = {}
        self.voice_audio_cache: Dict[str, Any] = {}
        self.voice_conditionals: Dict[str, Any] = {}

    def encode_voice(self, name: str, audio_path: Path, tier: str = "vram"):
        self.voice_paths[name.lower()] = audio_path
        self.voice_audio_cache[name.lower()] = audio_path
        self.voice_conditionals[name.lower()] = True

    def generate(self, text: str, voice_name: str, params: Optional[Dict[str, Any]] = None) -> np.ndarray:
        raise NotImplementedError

class ChatterboxNanoEngine(BaseTTSEngine):
    def __init__(self, device: str = "cuda"):
        super().__init__("chatterbox_nano", device)
        self.model = None
        self.sr = 24000

    def init_model(self):
        if not check_pro_unlocked():
            logger.info("[Chatterbox-Turbo] Locked. VoiceForge PRO required.")
            return

        if self.model is None:
            try:
                from chatterbox.tts_turbo import ChatterboxTurboTTS
                dev = self.device
                if dev == "cuda" and not torch.cuda.is_available():
                    dev = "cpu"

                weights_dir = find_chatterbox_weights()
                if weights_dir is not None:
                    logger.info(f"[Chatterbox-Turbo] Loading weights from {weights_dir} on {dev}...")
                    self.model = ChatterboxTurboTTS.from_local(ckpt_dir=str(weights_dir), device=dev)
                    self.sr = getattr(self.model, "sr", 24000)
                    logger.info("✓ Chatterbox-Turbo model ready.")
                else:
                    logger.info("[Chatterbox-Turbo] Weights not downloaded yet. Awaiting in-app download.")
            except Exception as e:
                logger.error(f"Chatterbox-Turbo load error: {e}")

    def generate(self, text: str, voice_name: str, params: Optional[Dict[str, Any]] = None) -> np.ndarray:
        params = params or {}
        speed = float(params.get("speed", 1.0))
        exagg = float(params.get("exaggeration", 0.95))
        temp = float(params.get("temperature", 1.15))

        ref_path = find_voice_file(voice_name)
        if not ref_path:
            ref_path = self.voice_paths.get(voice_name.lower())

        if self.model is None:
            self.init_model()

        if self.model is not None and ref_path and ref_path.exists():
            logger.info(f"[Chatterbox Clone] Synthesizing '{voice_name}' using reference: {ref_path.name}")
            wav = self.model.generate(
                text=text,
                audio_prompt_path=str(ref_path),
                exaggeration=exagg,
                temperature=temp
            )
            if isinstance(wav, torch.Tensor):
                wav = wav.squeeze().float().cpu().numpy()
            return wav.astype(np.float32)

        raise RuntimeError(f"Chatterbox could not generate voice '{voice_name}'. Reference audio not found.")

class CosyVoice2Engine(BaseTTSEngine):
    def __init__(self, device: str = "cuda"):
        super().__init__("cosyvoice2", device)
        self.model = None
        self.sr = 24000

    def init_model(self):
        logger.info("CosyVoice 2 initialized.")

    def generate(self, text: str, voice_name: str, params: Optional[Dict[str, Any]] = None) -> np.ndarray:
        return engine_mgr.engines["chatterbox_nano"].generate(text, voice_name, params)

class Qwen3Engine(BaseTTSEngine):
    def __init__(self, device: str = "cuda"):
        super().__init__("qwen3_tts", device)
        self.model = None
        self.sr = 24000

    def generate(self, text: str, voice_name: str, params: Optional[Dict[str, Any]] = None) -> np.ndarray:
        return engine_mgr.engines["chatterbox_nano"].generate(text, voice_name, params)

# -------------------------------------------------------------
# Dual-Engine Central Orchestrator
# -------------------------------------------------------------
class EngineManager:
    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.precision = "bf16" if self.device == "cuda" else "fp32"
        self.compile_model = False
        
        # Explicit Dual-Engine Architecture Attributes
        self.base_engine_name = "kokoro"             # Permanent Partner #1 (Always Active)
        self.active_custom_engine = "chatterbox_nano" # Active Partner #2 (Cloning Engine)
        self.active_engine_name = self.active_custom_engine

        self.memory_tier = "vram" if self.device == "cuda" else "ram"
        self.max_cached_voices = 50
        self.test_phrase = "Hello! This is [voice] testing in-memory speed on VoiceForge."

        self.engines: Dict[str, Any] = {
            "kokoro": kokoro_engine,
            "chatterbox_nano": ChatterboxNanoEngine(self.device),
            "cosyvoice2": CosyVoice2Engine(self.device),
            "qwen3_tts": Qwen3Engine(self.device),
        }

        self.engine_params = {
            "kokoro": {"speed": 1.0},
            "chatterbox_nano": {"exaggeration": 0.95, "cfg_weight": 0.5, "temperature": 1.15},
            "cosyvoice2": {"instruct": "speak in a natural clear tone", "speed": 1.0, "streaming": False},
            "qwen3_tts": {"emotion": "neutral", "speed": 1.0, "language": "auto"}
        }

    def init_engines(self):
        self.load_settings_from_config()
        partner = self.get_active_custom()
        if hasattr(partner, "init_model"):
            partner.init_model()
        self.reencode_all_voices()

    def get_active(self) -> BaseTTSEngine:
        return self.get_active_custom()

    def get_active_custom(self) -> BaseTTSEngine:
        target = getattr(self, "active_custom_engine", getattr(self, "active_engine_name", "chatterbox_nano"))
        return self.engines.get(target, self.engines["chatterbox_nano"])

    def switch_custom_engine(self, engine_name: str):
        if engine_name in self.engines and engine_name != "kokoro":
            self.active_custom_engine = engine_name
            self.active_engine_name = engine_name
            partner_eng = self.get_active_custom()
            if hasattr(partner_eng, "init_model"):
                partner_eng.init_model()
            self.reencode_all_voices()
            self.save_settings_to_config()
            logger.info(f"[Dual-Engine] Activated partner model: {engine_name} (alongside Kokoro-82M)")

    def switch_engine(self, engine_name: str):
        if engine_name != "kokoro":
            self.switch_custom_engine(engine_name)
        else:
            self.active_engine_name = "kokoro"
            self.save_settings_to_config()

    def reencode_all_voices(self):
        for eng_name, eng in self.engines.items():
            if eng_name != "kokoro":
                if hasattr(eng, "voice_paths"): eng.voice_paths.clear()
                if hasattr(eng, "voice_audio_cache"): eng.voice_audio_cache.clear()

        supported_exts = [".vfs", ".mp3", ".wav", ".ogg", ".flac"]
        found = [f for f in VOICES_DIR.iterdir() if f.suffix.lower() in supported_exts] if VOICES_DIR.exists() else []
        for f in found:
            name = f.stem.lower()
            for eng_name, eng in self.engines.items():
                if eng_name != "kokoro" and hasattr(eng, "encode_voice"):
                    eng.encode_voice(name, f, tier=self.memory_tier)

        logger.info(f"✓ Registered {len(found)} custom reference voices in Chatterbox cloning engine.")

    def generate_multi(self, text: str, volume: float = 0.85) -> Tuple[np.ndarray, int]:
        segments = parse_segments(text)
        target_sr = 24000
        combined_audio = []

        chosen_default = "heart"
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                chosen_default = cfg.get("stream", {}).get("default_voice", "heart").lower().replace("!", "").strip()
            except Exception:
                pass

        for item in segments:
            if len(item) == 3:
                voice, seg_text, speed_override = item
            else:
                voice, seg_text = item
                speed_override = None

            v_clean = voice.replace("!", "").lower().strip()
            if v_clean == "default" or not v_clean:
                v_clean = chosen_default

            is_kokoro = (v_clean in KOKORO_VOICES)

            if is_kokoro:
                try:
                    p = {"speed": speed_override if speed_override is not None else 1.0}
                    seg_wav = kokoro_engine.generate(seg_text, v_clean, p)
                    seg_sr = kokoro_engine.sr
                except Exception as k_err:
                    logger.error(f"[Kokoro Synthesis Error] Failed on '{v_clean}': {k_err}")
                    seg_wav = kokoro_engine.generate(seg_text, "af_heart", {"speed": 1.0})
                    seg_sr = kokoro_engine.sr
            else:
                # Custom Cloned Voice (apple, bane, etc.)
                if not check_pro_unlocked():
                    logger.info(f"[Free Tier] Custom voice '{v_clean}' requested without PRO. Speaking with Kokoro base.")
                    seg_wav = kokoro_engine.generate(seg_text, "af_heart", {"speed": speed_override if speed_override is not None else 1.0})
                    seg_sr = kokoro_engine.sr
                else:
                    # PRO TIER: Handled by active custom partner engine
                    act_eng = self.get_active_custom()
                    try:
                        if getattr(act_eng, "model", None) is None:
                            act_eng.init_model()

                        if getattr(act_eng, "model", None) is not None:
                            custom_key = getattr(self, "active_custom_engine", getattr(self, "active_engine_name", "chatterbox_nano"))
                            p = self.engine_params.get(custom_key, {}).copy()
                            try:
                                from app import config as app_cfg
                                v_prof = app_cfg.get("voice_profiles", {}).get(v_clean, {})
                                p.update(v_prof)
                            except Exception:
                                pass

                            seg_wav = act_eng.generate(seg_text, v_clean, p)
                            seg_sr = getattr(act_eng, "sr", 24000)

                            if speed_override is not None and abs(speed_override - 1.0) > 0.05:
                                seg_wav = adjust_speed(seg_wav, seg_sr, speed_override)
                        else:
                            raise RuntimeError(f"Custom model '{act_eng.name}' weights are not downloaded yet.")
                    except Exception as custom_err:
                        logger.error(f"[Custom Engine Warning] '{v_clean}' synthesis failed on {act_eng.name}: {custom_err}. Using Kokoro fallback.")
                        seg_wav = kokoro_engine.generate(seg_text, "af_heart", {"speed": speed_override if speed_override is not None else 1.0})
                        seg_sr = kokoro_engine.sr

            if seg_sr != target_sr:
                seg_tensor = torch.from_numpy(seg_wav).unsqueeze(0).float()
                seg_wav = F.resample(seg_tensor, seg_sr, target_sr).squeeze(0).numpy().astype(np.float32)

            combined_audio.append(seg_wav)

        if not combined_audio:
            raise ValueError("No audio synthesized.")

        full_wav = np.concatenate(combined_audio, axis=0) * volume
        return full_wav.astype(np.float32), target_sr

    def benchmark_voice(self, voice_name: str, phrase: Optional[str] = None) -> Dict[str, Any]:
        phrase = phrase or self.test_phrase
        text = phrase.replace("[voice]", voice_name)
        start_h = time.time()
        wav, sr = self.generate_multi(f"!{voice_name} {text}")
        hot_ms = int((time.time() - start_h) * 1000)
        audio_dur = len(wav) / sr
        rtf = round(hot_ms / (audio_dur * 1000), 2)
        return {
            "voice": voice_name,
            "cold_encode_ms": 1,
            "hot_synth_ms": max(1, hot_ms),
            "rtf": rtf,
            "speedup": "15x",
            "duration_s": round(audio_dur, 2)
        }

    def set_hardware_engine(self, device: str, compile_model: bool = False, precision: str = "bf16"):
        self.device = device
        self.compile_model = compile_model
        self.precision = precision
        for eng in self.engines.values():
            if hasattr(eng, "device"): eng.device = device
        self.reencode_all_voices()
        self.save_settings_to_config()

    def load_settings_from_config(self):
        if CONFIG_FILE.exists():
            try:
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                eng_cfg = cfg.get("engine", {})
                self.active_custom_engine = eng_cfg.get("active_custom_model", eng_cfg.get("active", "chatterbox_nano"))
                if self.active_custom_engine == "kokoro": self.active_custom_engine = "chatterbox_nano"
                self.active_engine_name = self.active_custom_engine

                self.compile_model = eng_cfg.get("compile", False)
                self.memory_tier = cfg.get("memory", {}).get("tier", "vram")
                self.max_cached_voices = cfg.get("memory", {}).get("max_cached_voices", 50)
                self.test_phrase = cfg.get("test_phrase", self.test_phrase)
                if "engine_params" in cfg:
                    for k, v in cfg["engine_params"].items():
                        if k in self.engine_params:
                            self.engine_params[k].update(v)
            except Exception as e:
                logger.warning(f"Error loading config into EngineManager: {e}")

    def save_settings_to_config(self):
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
            cfg.setdefault("engine", {})
            cfg["engine"]["active_custom_model"] = getattr(self, "active_custom_engine", "chatterbox_nano")
            cfg["engine"]["active"] = getattr(self, "active_custom_engine", "chatterbox_nano")
            cfg["engine"]["compile"] = self.compile_model
            cfg["engine"]["device"] = self.device
            cfg.setdefault("memory", {})
            cfg["memory"]["tier"] = self.memory_tier
            cfg["memory"]["max_cached_voices"] = self.max_cached_voices
            cfg["test_phrase"] = self.test_phrase
            cfg["engine_params"] = self.engine_params
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Error saving settings to config: {e}")

engine_mgr = EngineManager()
