import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import io
import os
import sys
import time
import json
import types
import asyncio
import subprocess
import logging
import shutil
import webbrowser
from pathlib import Path
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager

# --- ARM64 / Windows Compatibility Watermarker Patch ---
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
import soundfile as sf
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from engine_manager import engine_mgr, VOICES_DIR, OUTPUT_DIR, parse_segments, adjust_speed
from audio_filter import audio_filter
from scheduler import scheduler
from chat_sources import ChatSourceManager

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
logger = logging.getLogger("VoiceForge")

# -------------------------------------------------------------
# Real-Time WebSocket Log Streamer for In-App Live Terminal Bar
# -------------------------------------------------------------
class LiveWebSocketLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            if active_websockets:
                payload = {"type": "log_event", "text": msg, "level": record.levelname}
                for ws in list(active_websockets):
                    try: asyncio.create_task(ws.send_json(payload))
                    except Exception: pass
        except Exception:
            pass

ws_log_handler = LiveWebSocketLogHandler()
ws_log_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(ws_log_handler)


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "stream_reading": True,
    "stream": {"default_voice": "swifty", "read_mode": "all"},
    "engine": {"active": "cosyvoice2", "device": "cuda" if torch.cuda.is_available() else "cpu", "compile": False},
    "memory": {"tier": "vram" if torch.cuda.is_available() else "ram", "max_cached_voices": 50},
    "test_phrase": "Hello! This is [voice] testing in-memory speed on VoiceForge.",
    "engine_params": {
        "chatterbox_nano": {"exaggeration": 0.95, "cfg_weight": 0.5, "temperature": 1.15},
        "cosyvoice2": {"instruct": "speak in a natural clear tone", "speed": 1.0, "streaming": False},
        "qwen3_tts": {"emotion": "neutral", "speed": 1.0, "language": "auto"},
        "kokoro": {"speed": 1.0}
    },
    "kokoro_languages": {
        "en_us": True,
        "en_gb": True,
        "es": False,
        "fr": False,
        "it": False,
        "ja": False,
        "pt": False,
        "hi": False,
        "zh": False
    },
    "audio": {"volume": 85, "default_speed": 1.0, "output_dir": "output/", "muted": False},
    "reader": {"color": "#00FF00", "font": "Impact", "size": 32, "text_color": "#FFFFFF"},
    "commands": [
        {"id": "c1", "name": "!hello", "voice": "ron", "response": "Hey everyone, welcome to the live stream!", "cooldown": 10, "enabled": True}
    ],
    "sources": [],
    "voice_profiles": {}
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
        except Exception:
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

config = load_config()

is_stream_reading_active: bool = config.get("stream_reading", True)
current_tts_text: str = "Idle"

tts_queue: asyncio.Queue = asyncio.Queue()
active_websockets: List[WebSocket] = []
reader_websockets: List[WebSocket] = []
source_popout_ws: Dict[str, List[WebSocket]] = {}
chat_history: List[Dict[str, Any]] = []

async def broadcast_ws(event: dict):
    for ws in list(active_websockets):
        try: await ws.send_json(event)
        except Exception: active_websockets.remove(ws)

async def broadcast_reader(event: dict):
    for ws in list(reader_websockets):
        try: await ws.send_json(event)
        except Exception: reader_websockets.remove(ws)

async def auto_open_browser():
    await asyncio.sleep(1.2)
    try:
        import subprocess
        logger.info("[Auto-Launch] Launching VoiceForge Desktop App Window...")
        edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

        if os.path.exists(edge_path):
            subprocess.Popen([edge_path, "--app=http://localhost:8080", "--window-size=1400,900"])
        elif os.path.exists(chrome_path):
            subprocess.Popen([chrome_path, "--app=http://localhost:8080", "--window-size=1400,900"])
        else:
            webbrowser.open("http://localhost:8080")
    except Exception as e:
        webbrowser.open("http://localhost:8080")

async def handle_unified_chat(platform: str, channel: str, sender: str, message: str, color: str = None, source_id: str = "main"):
    ts = time.strftime("%H:%M:%S")
    clean_spoken = audio_filter.clean_for_tts(message) if hasattr(audio_filter, "clean_for_tts") else message
    if not clean_spoken or not clean_spoken.strip():
        clean_spoken = message.strip()

    chat_item = {
        "id": int(time.time() * 1000),
        "time": ts,
        "platform": platform,
        "channel": channel,
        "sender": sender,
        "color": color or "#38bdf8",
        "raw_message": message,
        "clean_text": clean_spoken,
        "source_id": source_id
    }

    chat_history.append(chat_item)
    if len(chat_history) > 150: chat_history.pop(0)

    await broadcast_ws({"type": "chat_message", "data": chat_item})
    await broadcast_reader({
        "type": "chat",
        "time": ts,
        "source": platform.lower(),
        "sender": sender,
        "text": f"{sender}: {clean_spoken}",
        "reading_active": is_stream_reading_active
    })

    if source_id in source_popout_ws:
        for ws in list(source_popout_ws[source_id]):
            try: await ws.send_json({"type": "chat", "data": chat_item})
            except Exception: source_popout_ws[source_id].remove(ws)

    if not is_stream_reading_active or not clean_spoken:
        return

    # Moderation Check
    blocked, reason = audio_filter.should_block({"sender": sender, "message": message, "raw_message": message, "platform": platform})
    if blocked:
        audio_filter.log_filtered(chat_item, reason)
        await broadcast_ws({"type": "filter_event", "data": {"time": ts, "source": platform, "sender": sender, "message": message, "reason": reason}})
        return

    vol = 0.0 if config["audio"].get("muted", False) else (config["audio"].get("volume", 85) / 100.0)

    # 1. Custom Viewer Commands (!hello, etc.)
    first_word = message.strip().split()[0].lower() if message.strip() else ""
    matched_cmd = next((c for c in config.get("commands", []) if c["name"].lower() == first_word and c.get("enabled", True)), None)
    if matched_cmd:
        cmd_voice = matched_cmd.get('voice', 'swifty').lstrip('!')
        await tts_queue.put({"text": f"!{cmd_voice} {matched_cmd['response']}", "sender": sender, "platform": platform, "volume": vol})
        return

    # 2. Voice Tag Resolution & Speed Modifiers
    from kokoro_engine import KOKORO_VOICES
    primary_voices = list(engine_mgr.get_active().voice_audio_cache.keys())
    all_available = set([v.lower() for v in primary_voices] + list(KOKORO_VOICES.keys()))

    raw_default = config.get("stream", {}).get("default_voice", "swifty").lstrip('!')
    active_default = raw_default if raw_default.lower() in all_available else "swifty"

    read_mode = config.get("stream", {}).get("read_mode", "all")
    matched_voice = None
    speed_tag = ""
    speech_text = ""

    if message.startswith("!"):
        import re
        tag_match = re.match(r"^!([a-zA-Z0-9_\-]+)(?:[-–_\s]+(\d*\.?\d+))?\s*(.*)$", message)
        if tag_match:
            candidate = tag_match.group(1).lower()
            speed_val = tag_match.group(2)
            if candidate in all_available:
                matched_voice = candidate
                speed_tag = f"-{speed_val}" if speed_val else ""
                speech_text = tag_match.group(3).strip()
            else:
                cleaned = re.sub(r"^!+", "", message)
                speech_text = cleaned.strip()
    else:
        speech_text = clean_spoken.strip()

    if not speech_text:
        return

    # 3. Queue Speech
    if matched_voice:
        await tts_queue.put({"text": f"!{matched_voice}{speed_tag} {speech_text}", "sender": sender, "platform": platform, "volume": vol})
    elif read_mode == "all":
        await tts_queue.put({"text": f"!{active_default} {speech_text}", "sender": sender, "platform": platform, "volume": vol})

async def scheduled_fire_callback(msg_text: str, source_tag: str):
    scheduler.log_activity(msg_text, source_tag)
    if scheduler.activity_log:
        await broadcast_ws({"type": "activity_event", "data": scheduler.activity_log[0]})
    if is_stream_reading_active:
        vol = 0.0 if config["audio"].get("muted", False) else (config["audio"].get("volume", 85) / 100.0)
        await tts_queue.put({"text": msg_text, "sender": source_tag, "platform": "Scheduled", "volume": vol})

scheduler.fire_callback = scheduled_fire_callback

async def tts_worker():
    global current_tts_text
    while True:
        job = await tts_queue.get()
        try:
            current_tts_text = f"{job.get('sender', 'Stream')}: {job.get('text', '')}"
            start_t = time.time()
            wav_np, sr = await asyncio.to_thread(engine_mgr.generate_multi, text=job["text"], volume=job.get("volume", 0.85))
            gen_dur = time.time() - start_t

            if wav_np is None or len(wav_np) == 0:
                logger.warning(f"Synthesis returned empty audio for: {job.get('text')}")
                continue

            now_ms = int(time.time() * 1000)
            plat_tag = job.get('platform', 'chat').lower().replace('.', '_')
            filename = f"tts_{now_ms}_{plat_tag}.wav"
            out_path = OUTPUT_DIR / filename
            sf.write(str(out_path), wav_np, sr)

            audio_payload = {
                "type": "audio_ready",
                "audio_url": f"/output/{filename}?t={now_ms}",
                "filename": filename,
                "text": job["text"],
                "sender": job.get("sender", "Stream"),
                "platform": job.get("platform", "Chat"),
                "gen_time": f"{gen_dur:.2f}s"
            }
            await broadcast_ws(audio_payload)
            await broadcast_reader(audio_payload)
        except Exception as e:
            logger.error(f"TTS Synthesis notice: {e}")
        finally:
            current_tts_text = "Idle"
            tts_queue.task_done()

async def handle_source_status_event(source_id: str, platform: str, target: str, status: str, log_msg: str):
    scheduler.log_activity(log_msg, platform)
    await broadcast_ws({
        "type": "source_status",
        "source_id": source_id,
        "platform": platform,
        "target": target,
        "status": status,
        "log": log_msg,
        "time": time.strftime("%H:%M:%S")
    })

chat_sources = ChatSourceManager(message_callback=handle_unified_chat, status_callback=handle_source_status_event)

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine_mgr.init_engines()
    asyncio.create_task(tts_worker())
    scheduler.start()
    for src in config.get("sources", []):
        plat = chat_sources.detect_platform_name(src["target"])
        if plat == "Stream":
            plat = src.get("platform", "Rumble")
        await chat_sources.add_source(src["id"], plat, src["target"], src.get("token", ""))
    
    asyncio.create_task(auto_open_browser())
    yield
    scheduler.stop()

class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/output"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


# -------------------------------------------------------------
# Clean Desktop Application Lifecycle & Auto-Shutdown
# -------------------------------------------------------------
shutdown_timer_task = None

async def delayed_shutdown_check():
    """Shuts down server automatically if all UI windows have been closed."""
    await asyncio.sleep(8)
    if len(active_websockets) == 0 and len(reader_websockets) == 0:
        logger.info("[Auto-Shutdown] All application windows closed. Shutting down VoiceForge...")
        await execute_clean_exit()

async def execute_clean_exit():
    try:
        scheduler.stop()
        if chat_sources and chat_sources.context:
            await chat_sources.context.close()
        if chat_sources and chat_sources.playwright:
            await chat_sources.playwright.stop()
    except Exception:
        pass
    logger.info("[Shutdown] Complete. Releasing port 8080 and exiting.")
    os._exit(0)



app = FastAPI(title="VoiceForge Studio Pro", version="1.3.4", lifespan=lifespan)
app.add_middleware(NoCacheMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")

@app.post("/api/shutdown")
async def manual_shutdown_endpoint():
    logger.info("[Shutdown] Received manual exit signal from UI.")
    asyncio.create_task(execute_clean_exit())
    return {"status": "shutting_down"}



from fastapi.responses import FileResponse

@app.get("/favicon.ico", include_in_schema=False)
async def serve_favicon():
    fav = BASE_DIR / "icon.ico"
    if fav.exists():
        return FileResponse(fav)
    return Response(status_code=204)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return HTMLResponse(content=(BASE_DIR / "index.html").read_text(encoding="utf-8"))

@app.get("/reader", response_class=HTMLResponse)
async def serve_reader():
    return HTMLResponse(content=(BASE_DIR / "reader.html").read_text(encoding="utf-8"))

@app.get("/api/sources")
async def get_all_sources():
    return {"status": "success", "sources": chat_sources.get_sources()}

@app.post("/api/sources/add")
async def add_chat_source(data: dict):
    target = data.get("target", "").strip()
    token = data.get("token", "").strip()
    platform = chat_sources.detect_platform_name(target)
    if platform == "Stream":
        platform = data.get("platform", "rumble")

    src_id = f"{platform.lower().replace('.', '_')}_{int(time.time()*1000)}"
    src = await chat_sources.add_source(
        source_id=src_id,
        platform=platform,
        target=target,
        token=token
    )
    config["sources"] = chat_sources.get_sources()
    save_config(config)
    return {"status": "success", "source": src, "sources": chat_sources.get_sources()}

@app.delete("/api/sources/{source_id}")
async def remove_chat_source(source_id: str):
    await chat_sources.remove_source(source_id)
    config["sources"] = chat_sources.get_sources()
    save_config(config)
    return {"status": "deleted", "sources": chat_sources.get_sources()}

@app.post("/api/settings/browser-visibility")
async def toggle_browser_window(data: dict):
    visible = bool(data.get("visible", False))
    await chat_sources.toggle_browser_visibility(visible)
    return {"status": "success", "visible": visible}

@app.get("/api/settings/kokoro-languages")
async def get_kokoro_languages():
    from kokoro_engine import kokoro_engine, KOKORO_LANGUAGES
    return {
        "status": "success",
        "languages": KOKORO_LANGUAGES,
        "enabled": kokoro_engine.enabled_languages
    }

@app.post("/api/settings/kokoro-languages")
async def set_kokoro_languages(data: dict):
    from kokoro_engine import kokoro_engine
    new_langs = data.get("languages", {})
    kokoro_engine.save_language_config(new_langs)
    return {
        "status": "success",
        "enabled": kokoro_engine.enabled_languages
    }

@app.get("/api/settings/volume")
async def get_volume_settings():
    return {"status": "success", "audio": config.get("audio", {})}

@app.post("/api/settings/volume")
async def set_volume_endpoint(data: dict):
    config.setdefault("audio", {})
    if "volume" in data:
        vol = max(0, min(150, int(data["volume"])))
        config["audio"]["volume"] = vol
        config["volume"] = vol
    if "muted" in data:
        config["audio"]["muted"] = bool(data["muted"])
        config["muted"] = bool(data["muted"])
    save_config(config)
    return {"status": "success", "audio": config["audio"]}

@app.get("/api/settings/stream-voice")
async def get_stream_voice_settings():
    return {"status": "success", "stream": config.get("stream", {})}

@app.post("/api/settings/stream-voice")
async def set_stream_voice_settings(data: dict):
    config.setdefault("stream", {})
    if "default_voice" in data:
        config["stream"]["default_voice"] = data["default_voice"].lstrip('!')
    if "read_mode" in data:
        config["stream"]["read_mode"] = data["read_mode"]
    save_config(config)
    return {"status": "success", "stream": config["stream"]}

@app.post("/api/stream/start")
async def start_stream_reading():
    global is_stream_reading_active
    is_stream_reading_active = True
    config["stream_reading"] = True
    save_config(config)
    await broadcast_ws({"type": "reading_status", "reading": True})
    await broadcast_reader({"type": "status", "reading": True})
    return {"status": "started", "reading": True}

@app.post("/api/stream/stop")
async def stop_stream_reading():
    global is_stream_reading_active
    is_stream_reading_active = False
    config["stream_reading"] = False
    save_config(config)
    await broadcast_ws({"type": "reading_status", "reading": False})
    await broadcast_reader({"type": "status", "reading": False})
    return {"status": "stopped", "reading": False}

@app.get("/api/stream/status")
async def get_stream_status():
    return {
        "reading": is_stream_reading_active,
        "queue_depth": tts_queue.qsize(),
        "current": current_tts_text,
        "engine": engine_mgr.active_engine_name,
        "voices_count": len(engine_mgr.get_active().voice_audio_cache),
        "memory_tier": engine_mgr.memory_tier.upper(),
        "max_cached": engine_mgr.max_cached_voices
    }

@app.get("/api/engine")
async def get_engine_state():
    return {
        "active": engine_mgr.active_engine_name,
        "device": engine_mgr.device,
        "precision": engine_mgr.precision,
        "params": engine_mgr.engine_params,
        "memory_tier": engine_mgr.memory_tier,
        "max_cached_voices": engine_mgr.max_cached_voices,
        "test_phrase": engine_mgr.test_phrase,
        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "None",
        "available_engines": ["chatterbox_nano", "cosyvoice2", "qwen3_tts", "kokoro"]
    }

@app.post("/api/engine/switch")
async def switch_engine(data: dict):
    eng = data.get("engine", "cosyvoice2")
    engine_mgr.switch_engine(eng)
    return {"status": "success", "active": eng}

@app.post("/api/engine/params")
async def update_engine_params(data: dict):
    eng = data.get("engine", engine_mgr.active_engine_name)
    params = data.get("params", {})
    if eng in engine_mgr.engine_params:
        engine_mgr.engine_params[eng].update(params)
    engine_mgr.save_settings_to_config()
    return {"status": "success", "params": engine_mgr.engine_params}

@app.post("/api/settings/engine")
async def set_hardware_engine_endpoint(data: dict):
    device_choice = data.get("device", "cuda")
    compile_model = bool(data.get("compile", False))
    precision = data.get("precision", "bf16" if device_choice == "cuda" else "fp32")
    await asyncio.to_thread(engine_mgr.set_hardware_engine, device_choice, compile_model, precision)
    vram_mb = (torch.cuda.memory_allocated(0) // (1024 * 1024)) if torch.cuda.is_available() else 0
    return {
        "status": "success",
        "device": engine_mgr.device,
        "precision": engine_mgr.precision,
        "compile": engine_mgr.compile_model,
        "vram_used_mb": vram_mb,
        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "None"
    }

@app.post("/api/settings/memory")
async def update_memory_settings(data: dict):
    if "tier" in data: engine_mgr.memory_tier = data["tier"]
    if "max_cached_voices" in data: engine_mgr.max_cached_voices = max(1, min(100, int(data["max_cached_voices"])))
    if "test_phrase" in data: engine_mgr.test_phrase = data["test_phrase"]
    engine_mgr.save_settings_to_config()
    engine_mgr.reencode_all_voices()
    return {
        "status": "success",
        "tier": engine_mgr.memory_tier,
        "max_cached_voices": engine_mgr.max_cached_voices,
        "voices_in_memory": len(engine_mgr.get_active().voice_conditionals)
    }

@app.get("/api/voices/{name}/profile")
async def get_voice_profile_endpoint(name: str):
    profiles = config.setdefault("voice_profiles", {})
    return {"voice": name, "profile": profiles.get(name.lower(), {})}

@app.post("/api/voices/{name}/profile")
async def save_voice_profile_endpoint(name: str, payload: dict):
    profiles = config.setdefault("voice_profiles", {})
    profiles[name.lower()] = payload.get("profile", {})
    save_config(config)
    return {"status": "success", "voice": name, "profile": profiles[name.lower()]}

@app.get("/api/voices-profiles")
async def get_all_voice_profiles_endpoint():
    return {"profiles": config.get("voice_profiles", {})}

@app.get("/api/voices")
async def get_voices():
    from kokoro_engine import KOKORO_VOICES
    active_eng = engine_mgr.get_active()
    cloned_voices = sorted(list(active_eng.voice_audio_cache.keys()))
    kokoro_voices = sorted(list(set(KOKORO_VOICES.keys())))

    all_voices = cloned_voices + [v for v in kokoro_voices if v not in cloned_voices]
    return {
        "voices": all_voices,
        "cloned_voices": cloned_voices,
        "kokoro_voices": kokoro_voices,
        "count": len(all_voices),
        "active_engine": engine_mgr.active_engine_name
    }

@app.get("/api/voices/{name}/preview")
async def preview_voice(name: str, phrase: Optional[str] = None):
    raw_phrase = phrase if phrase and phrase.strip() else engine_mgr.test_phrase
    text = raw_phrase.replace("[voice]", name)
    wav_np, sr = await asyncio.to_thread(engine_mgr.generate_multi, f"!{name} {text}", 1.0)
    buf = io.BytesIO()
    sf.write(buf, wav_np, sr, format="WAV")
    buf.seek(0)
    return StreamingResponse(buf, media_type="audio/wav", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.post("/api/voices/{name}/benchmark")
async def benchmark_voice_speed(name: str, payload: dict = None):
    test_text = (payload or {}).get("test_phrase")
    try:
        stats = engine_mgr.benchmark_voice(name, test_text)
        return stats
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/voices/encode-all")
async def encode_all_voices():
    engine_mgr.reencode_all_voices()
    return {"status": "encoded", "count": len(engine_mgr.get_active().voice_conditionals)}

@app.get("/api/fonts")
async def get_system_fonts():
    fonts = set()
    try:
        proc = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=2)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                family = line.split(",")[0].strip()
                if family and not family.startswith("@"):
                    fonts.add(family)
    except Exception:
        pass
    fallback = ["Impact", "Arial", "Roboto", "Inter", "Segoe UI", "Montserrat", "Courier New", "Georgia", "Ubuntu"]
    fonts.update(fallback)
    return {"fonts": sorted(list(fonts))}

@app.get("/api/timed-messages")
async def get_timed_messages():
    return {"messages": scheduler.timed_messages, "events": scheduler.event_triggers, "activity": list(scheduler.activity_log)}

@app.post("/api/timed-messages")
async def add_timed_message(data: dict):
    item = {
        "id": f"t_{int(time.time()*1000)}",
        "type": data.get("type", "interval"),
        "interval_min": int(data.get("interval_min", 10)),
        "time": data.get("time", "12:00"),
        "voice": data.get("voice", "ron"),
        "message": data.get("message", ""),
        "enabled": True,
        "next_fire": time.time() + (int(data.get("interval_min", 10)) * 60),
        "last_run": "Never"
    }
    scheduler.timed_messages.append(item)
    scheduler.save()
    return item

@app.delete("/api/timed-messages/{item_id}")
async def delete_timed_message(item_id: str):
    scheduler.timed_messages = [m for m in scheduler.timed_messages if m["id"] != item_id]
    scheduler.save()
    return {"status": "deleted"}

@app.get("/api/filters")
async def get_filter_rules():
    return {
        "blocked_users": getattr(audio_filter, "blocked_users", []),
        "keywords": getattr(audio_filter, "block_keywords", []),
        "allow_list": getattr(audio_filter, "allow_list", []),
        "min_length": getattr(audio_filter, "min_length", 1),
        "max_length": getattr(audio_filter, "max_length", 500),
        "block_bots": getattr(audio_filter, "block_bots", False),
        "block_keywords_active": getattr(audio_filter, "block_keywords_active", True),
        "log": getattr(audio_filter, "filtered_log", [])
    }

@app.post("/api/filters/update")
async def update_filter_rules(data: dict):
    if "keywords" in data and hasattr(audio_filter, "block_keywords"):
        audio_filter.block_keywords = [k.strip() for k in data["keywords"] if k.strip()]
    if "allow_list" in data and hasattr(audio_filter, "allow_list"):
        audio_filter.allow_list = [u.strip().lower() for u in data["allow_list"] if u.strip()]
    if "block_bots" in data and hasattr(audio_filter, "block_bots"):
        audio_filter.block_bots = bool(data["block_bots"])
    if "block_keywords_active" in data and hasattr(audio_filter, "block_keywords_active"):
        audio_filter.block_keywords_active = bool(data["block_keywords_active"])
    if hasattr(audio_filter, "save"):
        try: audio_filter.save()
        except Exception: pass
    return {"status": "success", "rules": await get_filter_rules()}

@app.post("/api/filters/whitelist-user")
async def whitelist_user_endpoint(data: dict):
    user = data.get("username", "").strip().lower()
    if user and hasattr(audio_filter, "allow_list"):
        if user not in audio_filter.allow_list:
            audio_filter.allow_list.append(user)
            if hasattr(audio_filter, "save"):
                try: audio_filter.save()
                except Exception: pass
    return {"status": "whitelisted", "username": user}

@app.post("/api/filters/block-user")
async def block_chatter(data: dict):
    user = data.get("username", "").strip()
    if hasattr(audio_filter, "block_user"):
        audio_filter.block_user(user, data.get("source", "all"), data.get("reason", "Manual block"), int(data.get("duration", 0)))
    return {"status": "blocked", "user": user}

@app.post("/api/filters/unblock-user")
async def unblock_chatter(data: dict):
    user = data.get("username", "").strip()
    if hasattr(audio_filter, "unblock_user"):
        audio_filter.unblock_user(user)
    return {"status": "unblocked", "user": user}

@app.post("/api/filters/restore/{item_id}")
async def restore_filtered_message(item_id: str):
    log_items = getattr(audio_filter, "filtered_log", [])
    item = next((f for f in log_items if f.get("id") == item_id), None)
    if item:
        vol = 0.0 if config["audio"].get("muted", False) else (config["audio"].get("volume", 85) / 100.0)
        await tts_queue.put({"text": item["message"], "sender": item["sender"], "platform": item["source"], "volume": vol})
        return {"status": "restored", "whitelisted": item["sender"]}
    raise HTTPException(status_code=404, detail="Item not found")

@app.get("/api/commands")
async def get_commands():
    return config.get("commands", [])

@app.post("/api/commands")
async def save_command(data: dict):
    cmd_name = data.get("name", "").strip().lower()
    if not cmd_name.startswith("!"): cmd_name = f"!{cmd_name}"
    cmd = {
        "id": data.get("id", f"c_{int(time.time()*1000)}"),
        "name": cmd_name,
        "voice": data.get("voice", "ron"),
        "response": data.get("response", ""),
        "cooldown": int(data.get("cooldown", 10)),
        "enabled": data.get("enabled", True)
    }
    cmds = [c for c in config.get("commands", []) if c["id"] != cmd["id"]]
    cmds.append(cmd)
    config["commands"] = cmds
    save_config(config)
    return cmd

@app.delete("/api/commands/{item_id}")
async def delete_command(item_id: str):
    config["commands"] = [c for c in config.get("commands", []) if c["id"] != item_id]
    save_config(config)
    return {"status": "deleted"}

# -------------------------------------------------------------
# Model Manager API (Status, Download, Delete)
# -------------------------------------------------------------
MODELS_CATALOG = {
    "kokoro": {
        "name": "Kokoro-82M",
        "description": "Ultra-lightweight style TTS (54 voices, 8 languages)",
        "type": "huggingface",
        "repo_id": "hexgrad/Kokoro-82M",
        "manual_url": "https://huggingface.co/hexgrad/Kokoro-82M/tree/main",
        "folder": "Kokoro-82M",
        "default_size": "~350 MB"
    },
    "chatterbox_nano": {
        "name": "Chatterbox-Turbo",
        "description": "Zero-shot fast voice cloning for custom voices",
        "type": "huggingface",
        "repo_id": "ResembleAI/chatterbox-turbo",
        "manual_url": "https://huggingface.co/ResembleAI/chatterbox-turbo/tree/main",
        "folder": "chatterbox-turbo",
        "default_size": "~1.2 GB"
    },
    "cosyvoice2": {
        "name": "CosyVoice 2 (0.5B)",
        "description": "Instruction-guided expressive flow-matching model",
        "type": "local_or_hf",
        "repo_id": "FunAudioLLM/CosyVoice2-0.5B",
        "manual_url": "https://huggingface.co/FunAudioLLM/CosyVoice2-0.5B/tree/main",
        "folder": "CosyVoice2-0.5B",
        "default_size": "~3.8 GB"
    },
    "qwen3_tts": {
        "name": "Qwen3-TTS",
        "description": "Multi-emotion neural voice engine",
        "type": "huggingface",
        "repo_id": "Qwen/Qwen2.5-0.5B",
        "manual_url": "https://huggingface.co/Qwen/Qwen2.5-0.5B/tree/main",
        "folder": "Qwen2.5-0.5B",
        "default_size": "~2.1 GB"
    }
}

model_download_status = {}

def get_dir_size_mb(path: Path) -> float:
    if not path or not path.exists():
        return 0.0
    if path.is_file():
        return round(path.stat().st_size / (1024 * 1024), 1)
    total = sum(f.stat().st_size for f in path.glob("**/*") if f.is_file())
    return round(total / (1024 * 1024), 1)

def find_model_path(model_id: str) -> Optional[Path]:
    info = MODELS_CATALOG.get(model_id, {})
    folder_name = info.get("folder", model_id).lower()
    mid_low = model_id.lower()
    
    # 1. Check local pretrained_models folder (Exact + Fuzzy Match)
    pretrained_dir = BASE_DIR / "pretrained_models"
    if pretrained_dir.exists():
        for p in pretrained_dir.iterdir():
            if p.is_dir():
                p_low = p.name.lower()
                if p_low == folder_name or p_low == mid_low:
                    if any(p.iterdir()): return p
                if "cosy" in mid_low and "cosy" in p_low:
                    if any(p.iterdir()): return p
                if "qwen" in mid_low and "qwen" in p_low:
                    if any(p.iterdir()): return p
                if "chatterbox" in mid_low and "chatterbox" in p_low:
                    if any(p.iterdir()): return p
                if "kokoro" in mid_low and "kokoro" in p_low:
                    if any(p.iterdir()): return p

    # 2. Check HuggingFace Hub Cache (Exact + Fuzzy Match)
    hf_hub = Path.home() / ".cache" / "huggingface" / "hub"
    if hf_hub.exists():
        for p in hf_hub.iterdir():
            if p.is_dir():
                p_low = p.name.lower()
                if "cosy" in mid_low and "cosy" in p_low:
                    return p
                if "qwen" in mid_low and "qwen" in p_low:
                    return p
                if "chatterbox" in mid_low and "chatterbox" in p_low:
                    return p
                if "kokoro" in mid_low and "kokoro" in p_low:
                    return p

    return None

@app.get("/api/models/status")
async def get_models_status():
    status = {}
    for m_id, info in MODELS_CATALOG.items():
        m_path = find_model_path(m_id)
        is_installed = m_path is not None and m_path.exists()
        size_mb = get_dir_size_mb(m_path) if is_installed else 0.0
        dl_state = model_download_status.get(m_id, "idle")

        status[m_id] = {
            "id": m_id,
            "name": info["name"],
            "description": info["description"],
            "installed": is_installed,
            "size_mb": size_mb,
            "approx_size": info["default_size"],
            "folder_path": str((BASE_DIR / "pretrained_models" / info.get("folder", m_id)).resolve()),
            "manual_url": info.get("manual_url", ""),
            "download_state": dl_state,
            "active": (m_id == engine_mgr.active_engine_name) or (m_id == "kokoro")
        }
    return {"status": "success", "models": status}

@app.get("/api/models/{model_id}/instructions")
async def get_model_instructions(model_id: str):
    info = MODELS_CATALOG.get(model_id)
    if not info:
        raise HTTPException(status_code=404, detail="Model not found.")
    target_dir = BASE_DIR / "pretrained_models" / info.get("folder", model_id)
    return {
        "id": model_id,
        "name": info["name"],
        "folder_path": str(target_dir.resolve()),
        "manual_url": info.get("manual_url", ""),
        "approx_size": info["default_size"],
        "installed": target_dir.exists() and any(target_dir.iterdir())
    }

@app.post("/api/models/{model_id}/verify")
async def verify_model_installation(model_id: str):
    info = MODELS_CATALOG.get(model_id)
    if not info:
        raise HTTPException(status_code=404, detail="Model not found.")
    m_path = find_model_path(model_id)
    is_installed = m_path is not None and m_path.exists()
    size_mb = get_dir_size_mb(m_path) if is_installed else 0.0
    
    if is_installed and hasattr(engine_mgr, "engines") and "chatterbox_nano" in engine_mgr.engines:
        engine_mgr.engines["chatterbox_nano"].init_model()
            
    return {
        "status": "success",
        "installed": is_installed,
        "size_mb": size_mb,
        "path": str(m_path.resolve()) if m_path else None
    }

@app.delete("/api/models/{model_id}")
async def delete_model_endpoint(model_id: str):
    if model_id == engine_mgr.active_engine_name:
        raise HTTPException(status_code=400, detail="Cannot delete currently active engine. Switch engine first.")
    
    m_path = find_model_path(model_id)
    if not m_path or not m_path.exists():
        return {"status": "not_found", "message": "Model cache not found or already deleted."}

    try:
        if m_path.is_dir():
            shutil.rmtree(m_path)
        else:
            m_path.unlink()
        model_download_status[model_id] = "idle"
        return {"status": "deleted", "model_id": model_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete model: {e}")

@app.post("/api/models/{model_id}/download")
async def download_model_endpoint(model_id: str):
    info = MODELS_CATALOG.get(model_id)
    if not info:
        raise HTTPException(status_code=404, detail="Unknown model.")

    if model_download_status.get(model_id) == "downloading":
        return {"status": "already_downloading", "model_id": model_id}

    async def do_download():
        model_download_status[model_id] = "downloading"
        try:
            logger.info(f"[Model Manager] Starting download for {info[name]}...")
            target_dir = BASE_DIR / "pretrained_models" / info.get("folder", model_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            
            if model_id == "kokoro":
                from kokoro import KPipeline
                KPipeline(lang_code="a", device="cpu")
            elif model_id == "chatterbox_nano":
                from huggingface_hub import snapshot_download
                snapshot_download(repo_id="ResembleAI/chatterbox-turbo", local_dir=str(target_dir), token=False)
                engine_mgr.engines["chatterbox_nano"].init_model()
            elif model_id == "cosyvoice2":
                from huggingface_hub import snapshot_download
                snapshot_download(repo_id="FunAudioLLM/CosyVoice2-0.5B", local_dir=str(target_dir), token=False)
            elif model_id == "qwen3_tts":
                from huggingface_hub import snapshot_download
                snapshot_download(repo_id="Qwen/Qwen2.5-0.5B", local_dir=str(target_dir), token=False)
                
            model_download_status[model_id] = "installed"
            logger.info(f"[Model Manager] ✓ Successfully installed {info[name]}.")
        except Exception as err:
            model_download_status[model_id] = "error"
            logger.error(f"[Model Manager] Download error: {err}")

    asyncio.create_task(do_download())
    return {"status": "downloading", "model_id": model_id, "message": f"Downloading {info[name]} in the background."}

@app.post("/api/generate")
async def manual_generate(payload: dict):
    text = payload.get("text", "").strip()
    if not text: raise HTTPException(status_code=400, detail="Empty text")
    vol = 0.0 if config["audio"].get("muted", False) else (config["audio"].get("volume", 85) / 100.0)
    start_t = time.time()
    wav_np, sr = await asyncio.to_thread(engine_mgr.generate_multi, text=text, volume=vol)
    gen_dur = time.time() - start_t
    buf = io.BytesIO()
    sf.write(buf, wav_np, sr, format="WAV")
    buf.seek(0)
    now_ms = int(time.time() * 1000)
    filename = f"out_{now_ms}.wav"
    sf.write(str(OUTPUT_DIR / filename), wav_np, sr)
    headers = {"X-Filename": filename, "X-Gen-Time": f"{gen_dur:.2f}s", "Access-Control-Expose-Headers": "X-Filename, X-Gen-Time"}
    return StreamingResponse(buf, media_type="audio/wav", headers=headers)

@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        await websocket.send_json({
            "type": "init",
            "reading": is_stream_reading_active,
            "engine": engine_mgr.active_engine_name,
            "voices": list(engine_mgr.get_active().voice_audio_cache.keys())
        })
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_websockets: active_websockets.remove(websocket)
        asyncio.create_task(delayed_shutdown_check())

@app.websocket("/ws/reader")
async def ws_reader(websocket: WebSocket):
    await websocket.accept()
    reader_websockets.append(websocket)
    try:
        await websocket.send_json({
            "type": "init",
            "history": chat_history,
            "reader": config.get("reader"),
            "reading": is_stream_reading_active
        })
        while True:
            msg = await websocket.receive_json()
            if msg.get("cmd") == "set_color": config.setdefault("reader", {})["color"] = msg.get("value")
            elif msg.get("cmd") == "set_size": config.setdefault("reader", {})["size"] = int(msg.get("value"))
            elif msg.get("cmd") == "set_font": config.setdefault("reader", {})["font"] = msg.get("value")
            save_config(config)
            await broadcast_reader({"type": "config", "reader": config["reader"]})
    except WebSocketDisconnect:
        if websocket in reader_websockets: reader_websockets.remove(websocket)

@app.websocket("/ws/chat/{source_id}")
async def ws_source_chat(websocket: WebSocket, source_id: str):
    await websocket.accept()
    source_popout_ws.setdefault(source_id, []).append(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        if source_id in source_popout_ws and websocket in source_popout_ws[source_id]:
            source_popout_ws[source_id].remove(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False, log_level="info")
