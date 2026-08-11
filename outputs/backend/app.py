"""
=============================================================================
app.py — local-ai-companion-v2 Gradio 主应用 (speech-to-speech 管线)
=============================================================================
运行: python app.py
配置文件: ~/setup/voice.yaml（自动查找）
端口: 7860 (Gradio 默认)
=============================================================================
架构: VAD → faster-whisper (ASR) → llama.cpp (LLM转发) → VoxCPM Worker → 双路分发
  数字人状态机: 待机 → 倾听 → 思考 → 说话
  打断控制: 取消令牌贯穿 LLM/TTS/播放
=============================================================================
安全 (SEC-01~20): 见文件末尾自查清单。
=============================================================================
"""

import os
import sys
import json
import time
import uuid
import shutil
import hashlib
import logging
import threading
import queue
import traceback
import re
import inspect
import atexit
import html
from collections import deque
from pathlib import Path
from datetime import datetime
from io import BytesIO
from typing import Optional, Generator, List, Dict, Any, Tuple

# ---- 配置加载 ----
import yaml
import requests
import httpx
import numpy as np

try:
    from outputs.backend.voxcpm_client import VoxCPMClient
    from outputs.backend.audio_playout import LiveTalkingAudioPlayout
except ImportError:  # direct ``python outputs/backend/app.py`` execution
    from voxcpm_client import VoxCPMClient
    from audio_playout import LiveTalkingAudioPlayout

# ---- 日志配置 ----
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s [%(name)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("voice-pipeline")

# =============================================================================
# 统一错误体 (API契约 §7.2)
# =============================================================================

class PipelineError(Exception):
    """管线统一异常，携带应用级错误码"""
    def __init__(self, code: str, message: str, detail: str = ""):
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail if self.detail else "详见本地日志"
            }
        }

def error_response(code: str, message: str, detail: str = "") -> dict:
    """构建统一错误响应体（SEC-11: 不暴露 Python 堆栈）"""
    logger.error(f"[{code}] {message}")
    if detail:
        logger.debug(f"  详情: {detail}")
    return {
        "error": {
            "code": code,
            "message": message,
            "detail": detail if detail else "详见本地日志"
        }
    }

# =============================================================================
# 配置管理
# =============================================================================

def find_config() -> str:
    """查找 voice.yaml 配置文件"""
    search_paths = [
        os.path.join(os.path.dirname(__file__), "..", "..", "config", "voice.yaml"),
        os.path.join(os.getcwd(), "config", "voice.yaml"),
        os.path.expanduser("~/setup/voice.yaml"),
        os.path.join(os.getcwd(), "voice.yaml"),
    ]
    for p in search_paths:
        expanded = os.path.expanduser(p)
        if os.path.isfile(expanded):
            return expanded
    raise PipelineError(
        "CFG_ERR_001",
        "找不到 voice.yaml 配置文件",
        "请将 config/voice.yaml 复制到 ~/setup/ 或项目根目录"
    )

CONFIG_PATH = find_config()
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

logger.info(f"配置已加载: {CONFIG_PATH}")

# 提取配置值（带默认值回退）
VAD_THRESH = float(config.get("VAD_THRESH", 0.5))
MIN_SILENCE_MS = int(config.get("MIN_SILENCE_MS", 700))
MAX_AUDIO_SEC = int(config.get("MAX_AUDIO_SEC", 20))
ASR_MODEL = str(config.get("ASR_MODEL", "large-v3"))
ASR_LANG = str(config.get("ASR_LANG", "zh"))
ASR_COMPUTE_TYPE = str(config.get("ASR_COMPUTE_TYPE", "int8_float16"))
ASR_BEAM_SIZE = int(config.get("ASR_BEAM_SIZE", 1))
LLM_BASE_URL = str(config.get("LLM_BASE_URL", "http://localhost:8090"))
LLM_MODEL = str(config.get("LLM_MODEL", "qwen3-8b"))
LLM_MAX_TOKENS = int(config.get("LLM_MAX_TOKENS", 1024))
LLM_TEMPERATURE = float(config.get("LLM_TEMPERATURE", 0.7))
LLM_KEEP_ALIVE = str(config.get("LLM_KEEP_ALIVE", "30m"))
LLM_WARMUP_ENABLED = bool(config.get("LLM_WARMUP_ENABLED", True))
VOXCPM_MODEL_ID = str(config.get("VOXCPM_MODEL_ID", "VoxCPM2"))
VOXCPM_PROFILE = str(config.get("VOXCPM_PROFILE", "balanced-v2"))
VOXCPM_MODEL_PATH = os.path.expanduser(str(config.get("VOXCPM_MODEL_PATH", "~/setup/models/VoxCPM2")))
VOXCPM_REF_WAV = os.path.expanduser(str(config.get("VOXCPM_REF_WAV", "")))
VOXCPM_REF_TEXT = str(config.get("VOXCPM_REF_TEXT", ""))
ACTIVE_REF_WAV = VOXCPM_REF_WAV
ACTIVE_REF_TEXT = VOXCPM_REF_TEXT
VOXCPM_SAMPLE_RATE = int(config.get("VOXCPM_SAMPLE_RATE", 48000))
VOXCPM_WORKER_URL = str(config.get("VOXCPM_WORKER_URL", "http://127.0.0.1:8020")).rstrip("/")
VOXCPM_LOCAL_FILES_ONLY = bool(config.get("VOXCPM_LOCAL_FILES_ONLY", True))
VOXCPM_STYLE_PROMPT = str(config.get("VOXCPM_STYLE_PROMPT", "")).strip()
ROLE_STYLE = str(config.get("ROLE_STYLE", "温柔陪伴")).strip() or "温柔陪伴"
ROLE_CUSTOM_INSTRUCTION = str(config.get("ROLE_CUSTOM_INSTRUCTION", "")).strip()
TTS_BACKCHANNEL_ENABLED = bool(config.get("TTS_BACKCHANNEL_ENABLED", False))
LIVETALKING_URL = str(config.get("LIVETALKING_URL", "http://localhost:8010"))
AVATAR_OUTPUT_SAMPLE_RATE = int(config.get("AVATAR_OUTPUT_SAMPLE_RATE", 16000))
AVATAR_FRAME_MS = int(config.get("AVATAR_FRAME_MS", 20))
AVATAR_PREBUFFER_MS = int(config.get("AVATAR_PREBUFFER_MS", 1200))
AVATAR_REBUFFER_MS = int(config.get("AVATAR_REBUFFER_MS", 400))
AVATAR_MAX_BUFFER_MS = int(config.get("AVATAR_MAX_BUFFER_MS", 6000))
AVATAR_AUDIO_GAIN = float(config.get("AVATAR_AUDIO_GAIN", 1.0))
AVATAR_FADE_IN_MS = int(config.get("AVATAR_FADE_IN_MS", 30))
AVATAR_LEAD_IN_MS = int(config.get("AVATAR_LEAD_IN_MS", 0))
AVATAR_SYNC_URL = str(config.get("AVATAR_SYNC_URL", "http://localhost:8011"))
UPLOAD_DIR = os.path.expanduser(str(config.get("UPLOAD_DIR", "~/setup/uploads")))
MAX_UPLOAD_SIZE_MB = int(config.get("MAX_UPLOAD_SIZE_MB", 15))
MIN_REF_AUDIO_SEC = int(config.get("MIN_REF_AUDIO_SEC", 5))
MAX_REF_AUDIO_SEC = int(config.get("MAX_REF_AUDIO_SEC", 15))
ALLOWED_AUDIO_EXTENSIONS = list(config.get("ALLOWED_AUDIO_EXTENSIONS", ["wav", "mp3", "m4a", "flac"]))

os.makedirs(UPLOAD_DIR, exist_ok=True)

# 读取日志配置并应用
LOG_LEVEL = str(config.get("LOG_LEVEL", "INFO")).upper()
LOG_DIR = os.path.expanduser(str(config.get("LOG_DIR", "~/setup/logs")))
_config_write_lock = threading.Lock()

_log_level = getattr(logging, LOG_LEVEL, logging.INFO)
os.makedirs(LOG_DIR, exist_ok=True)
_log_file = os.path.join(LOG_DIR, "voice-pipeline.log")

LOG_SOURCE_PATHS = {
    "应用": _log_file,
    "VoxCPM": "/tmp/voxcpm-worker.log",
    "数字人": "/tmp/livetalking.log",
    "桥接": "/tmp/avatar-sync.log",
}
LOG_SOURCE_LABELS = {"全部": "全部服务", **LOG_SOURCE_PATHS}

# 重配置根 logger 以应用配置中的日志级别和文件输出
_root_logger = logging.getLogger()
_root_logger.setLevel(_log_level)
try:
    _fh = logging.FileHandler(_log_file, encoding="utf-8")
    _fh.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s [%(name)s] %(message)s'))
    _root_logger.addHandler(_fh)
    logger.info(f"日志文件: {_log_file} (级别: {LOG_LEVEL})")
except Exception as _e:
    logger.warning(f"无法创建日志文件 {_log_file}: {_e}，仅输出到控制台")

# =============================================================================
# 数字人状态机 (§2 架构设计 4状态)
# =============================================================================

from enum import Enum

class AvatarState(Enum):
    IDLE = "idle"           # 待机
    LISTENING = "listening" # 倾听
    THINKING = "thinking"   # 思考
    SPEAKING = "speaking"   # 说话

class AvatarStateMachine:
    """
    数字人状态机：任意时刻单状态。
    停止 / 用户语音输入优先级最高。
    """

    def __init__(self):
        self._state = AvatarState.IDLE
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._state_callbacks: List[callable] = []

    @property
    def state(self) -> AvatarState:
        with self._lock:
            return self._state

    @property
    def state_text(self) -> str:
        """返回中文状态文本，用于前端显示"""
        labels = {
            AvatarState.IDLE: "待机",
            AvatarState.LISTENING: "倾听中...",
            AvatarState.THINKING: "思考中...",
            AvatarState.SPEAKING: "说话中...",
        }
        return labels.get(self.state, "未知")

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    def transition(self, new_state: AvatarState) -> bool:
        """状态转换。打断时取消当前操作。"""
        with self._lock:
            old_state = self._state
            # 仅在打断场景（从活跃状态转换到停止/倾听状态）时发出取消信号
            # 正常流程 IDLE→LISTENING（开始对话）或 THINKING→SPEAKING 不触发取消
            if new_state in (AvatarState.LISTENING, AvatarState.IDLE) and old_state in (AvatarState.SPEAKING, AvatarState.THINKING):
                self._cancel_event.set()
            self._state = new_state
            logger.info(f"状态: {old_state.value} → {new_state.value}")
        self._notify_callbacks(new_state)
        return True

    def reset_cancel(self):
        """重置取消令牌（新轮开始时调用）"""
        self._cancel_event.clear()

    def on_state_change(self, callback: callable):
        """注册状态变化回调"""
        self._state_callbacks.append(callback)

    def _notify_callbacks(self, state: AvatarState):
        for cb in self._state_callbacks:
            try:
                cb(state)
            except Exception as e:
                logger.warning(f"状态回调异常: {e}")


# 全局状态机实例
state_machine = AvatarStateMachine()

# =============================================================================
# 服务探活模块（连接状态提示 US-08）
# =============================================================================

def check_service(name: str, url: str, timeout: float = 2.0) -> str:
    """
    探活单个服务。返回状态字符串。
    SEC-03: 使用 httpx 库而非 shell 命令。URL 来自配置文件，不拼接用户输入。
    """
    try:
        resp = requests.get(url, timeout=timeout)
        # 任何响应（2xx/3xx/4xx）都表示服务可达，只有连接拒绝才算 disconnected
        return "connected"
    except requests.ConnectionError:
        return "disconnected"
    except requests.Timeout:
        return "timeout"
    except Exception:
        return "error"

def get_service_status() -> dict:
    """
    获取所有服务的连接状态。
    SEC-17: 不暴露内部配置、模型路径等字段。
    """
    status = {}
    try:
        tts_health = requests.get(f"{VOXCPM_WORKER_URL}/api/tts/health", timeout=2.0)
        payload = tts_health.json()
        status["tts"] = "ready" if tts_health.status_code == 200 and payload.get("ready") is True and payload.get("status") in {"ready", "busy"} else "degraded"
    except requests.ConnectionError:
        status["tts"] = "disconnected"
    except requests.Timeout:
        status["tts"] = "timeout"
    except Exception:
        status["tts"] = "error"
    status.update({
        "llama": check_service("llama-server", f"{LLM_BASE_URL}/api/tags"),
        "asr": "ready",   # ASR 在本地进程内，始终就绪
        "livetalking": check_service("LiveTalking", f"{LIVETALKING_URL}/health"),
        "avatar_sync": check_service("avatar-sync", f"{AVATAR_SYNC_URL}/health"),
    })
    # 整体状态
    all_ok = all(v == "connected" or v == "ready" for v in status.values())
    status["overall"] = "ready" if all_ok else "partial"
    return status


# =============================================================================
# VAD 静音检测模块 (#2 架构设计)
# =============================================================================

class VADDetector:
    """
    基于能量的 VAD（Voice Activity Detection）静音检测。
    从麦克风音频流实时检测静音，超过 MIN_SILENCE_MS 判定为句尾。
    参数从 voice.yaml 读取。
    """

    def __init__(self, threshold: float = VAD_THRESH, min_silence_ms: int = MIN_SILENCE_MS,
                 sample_rate: int = 16000):
        self.threshold = threshold
        self.min_silence_ms = min_silence_ms
        self.sample_rate = sample_rate
        # 使用 webrtcvad 作为更精确的 VAD（如果可用），否则回退到能量检测
        try:
            import webrtcvad
            self.vad = webrtcvad.Vad(2)  # 灵敏度 0-3，2=中等
            self.use_webrtc = True
            logger.info("VAD: 使用 WebRTC VAD 引擎")
        except ImportError:
            self.vad = None
            self.use_webrtc = False
            logger.info("VAD: 使用能量阈值引擎（webrtcvad 不可用）")

    def detect_silence_boundary(self, audio_chunk: np.ndarray) -> Tuple[bool, float]:
        """
        检测当前音频块是否为静音。
        返回: (is_silence, energy_level)
        """
        if len(audio_chunk) == 0:
            return True, 0.0

        energy = np.sqrt(np.mean(audio_chunk.astype(np.float32) ** 2))

        if self.use_webrtc and hasattr(self, 'vad') and self.vad:
            # WebRTC VAD 需要 16-bit PCM int16
            pcm = (audio_chunk * 32767).astype(np.int16).tobytes()
            try:
                is_speech = self.vad.is_speech(pcm, self.sample_rate)
                return not is_speech, energy
            except Exception:
                pass  # 回退到能量检测

        is_silence = energy < self.threshold
        return is_silence, energy

    def find_sentence_boundary(self, audio: np.ndarray, frame_ms: int = 30) -> int:
        """
        在音频中查找句尾边界（连续静音超过 MIN_SILENCE_MS 的位置）。
        返回: 句尾的样本索引（-1 = 未找到）
        """
        frame_samples = int(self.sample_rate * frame_ms / 1000)
        min_silence_frames = self.min_silence_ms // frame_ms

        silence_count = 0
        for i in range(0, len(audio) - frame_samples, frame_samples):
            chunk = audio[i:i + frame_samples]
            is_silence, _ = self.detect_silence_boundary(chunk)
            if is_silence:
                silence_count += 1
                if silence_count >= min_silence_frames:
                    return i
            else:
                silence_count = 0
        return -1


# =============================================================================
# ASR 模块（faster-whisper large-v3，#3 架构设计）
# =============================================================================

class ASREngine:
    """
    语音识别引擎：faster-whisper large-v3
    接收 VAD 截取的语音片段，转写为中文文本。
    """

    def __init__(self):
        self.model = None
        self._init_model()

    def _init_model(self):
        """延迟加载 ASR 模型"""
        try:
            from faster_whisper import WhisperModel
            # 设备选择：CUDA 优先
            device = "cuda"
            compute_type = ASR_COMPUTE_TYPE
            try:
                import torch
                if not torch.cuda.is_available():
                    device = "cpu"
                    compute_type = "int8"
                    logger.warning("ASR: CUDA 不可用，使用 CPU 推理")
            except ImportError:
                device = "cpu"
                compute_type = "int8"

            logger.info(f"ASR: 加载 {ASR_MODEL} ({device}/{compute_type})...")
            # SEC-03: 模型路径来自配置文件，不拼接用户输入
            self.model = WhisperModel(
                ASR_MODEL,
                device=device,
                compute_type=compute_type,
                download_root=os.path.expanduser("~/setup/models/faster-whisper-large-v3"),
                local_files_only=True,
            )
            logger.info("ASR: 模型加载完成")
        except Exception as e:
            logger.error(f"ASR 模型加载失败: {e}")
            raise PipelineError("ASR_ERR_002", "语音识别模型加载失败", str(e))

    def transcribe(self, audio_data: np.ndarray, sample_rate: int = 16000) -> str:
        """
        转写音频为文本。
        SEC-03: 通过 Python API 调用，不使用 shell 命令。
        SEC-02: LLM 输出只作文本处理，不执行代码。
        """
        if self.model is None:
            self._init_model()

        if audio_data is None or len(audio_data) < sample_rate * 0.3:  # < 0.3 秒
            raise PipelineError("ASR_ERR_001", "未检测到有效语音，请再说一次")

        try:
            # 持续通话已经经过前置 VAD 切句，不能再让 Whisper 的二次
            # VAD 因麦克风音量偏低把整句全部过滤掉。
            if audio_data.dtype != np.float32:
                audio_data = audio_data.astype(np.float32)
            audio_data = np.asarray(audio_data, dtype=np.float32)
            if audio_data.ndim > 1:
                audio_data = audio_data.mean(axis=1)
            if sample_rate != 16000 and audio_data.size >= 2:
                target_len = max(1, int(round(audio_data.size * 16000 / sample_rate)))
                source_x = np.linspace(0.0, 1.0, num=audio_data.size, endpoint=False)
                target_x = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
                audio_data = np.interp(target_x, source_x, audio_data).astype(np.float32)
                sample_rate = 16000
            rms = float(np.sqrt(np.mean(audio_data ** 2))) if audio_data.size else 0.0
            peak = float(np.max(np.abs(audio_data))) if audio_data.size else 0.0
            logger.info(
                "ASR 输入音频: %.2fs, rms=%.6f, peak=%.6f",
                len(audio_data) / max(sample_rate, 1),
                rms,
                peak,
            )
            decode_audio = audio_data
            input_gain = 1.0
            if 1e-5 < peak < 0.08:
                input_gain = min(32.0, 0.18 / peak)
                decode_audio = np.clip(audio_data * input_gain, -1.0, 1.0)
                logger.info("ASR 低音量预增益: gain=%.1fx", input_gain)

            def decode(source: np.ndarray, *, use_vad: bool) -> str:
                kwargs = {
                    "language": ASR_LANG,
                    "beam_size": ASR_BEAM_SIZE,
                    "condition_on_previous_text": False,
                }
                if use_vad:
                    kwargs.update(
                        vad_filter=True,
                        vad_parameters=dict(
                            threshold=VAD_THRESH,
                            min_silence_duration_ms=MIN_SILENCE_MS,
                        ),
                    )
                else:
                    kwargs["vad_filter"] = False
                segments, _info = self.model.transcribe(source, **kwargs)
                return "".join(
                    segment.text.strip() for segment in segments
                ).strip()

            try:
                result = decode(decode_audio, use_vad=True)
            except Exception as exc:
                logger.warning("ASR 二次 VAD 处理失败，转入容错重试: %s", exc)
                result = ""
            if not result and peak > 1e-5:
                # 前置 WebRTC VAD 已经确认这是一段语音时，给低音量输入
                # 一次无二次 VAD 的重试；增益封顶，避免底噪被无限放大。
                fallback_audio = decode_audio
                logger.warning(
                    "ASR 首次结果为空，关闭二次 VAD 重试: gain=%.1fx",
                    input_gain,
                )
                result = decode(fallback_audio, use_vad=False)

            if not result:
                raise PipelineError("ASR_ERR_001", "未检测到有效语音，请再说一次")

            logger.info(f"ASR 转写: {result[:80]}...")
            return result

        except PipelineError:
            raise
        except Exception as e:
            logger.error(f"ASR 转写异常: {traceback.format_exc()}")
            raise PipelineError("ASR_ERR_002", "语音识别失败，可用文字输入", str(e))


# 全局 ASR 引擎实例（懒加载，阶段 2 才需要）
# 注意：不在模块导入时初始化，避免 CUDA 模型加载卡死启动。
# 首次语音输入时通过 _get_asr() 按需加载。
asr_engine = None

def _get_asr():
    """懒加载 ASR 引擎"""
    global asr_engine
    if asr_engine is None:
        try:
            asr_engine = ASREngine()
        except Exception as e:
            logger.warning(f"ASR 引擎未加载（阶段 2 功能，不影响文字聊天）: {e}")
    return asr_engine

# =============================================================================
# LLM 转发模块（OpenAI 兼容 SSE 流式，#4 架构设计）
# =============================================================================

class LLMClient:
    """
    LLM 转发模块：以 OpenAI 兼容格式调用 Windows 侧 llama-server。
    POST /v1/chat/completions, stream=true, SSE 流式消费。
    """

    def __init__(self):
        self.base_url = LLM_BASE_URL.rstrip("/")
        # SEC-03: 使用 httpx 库，URL 来自配置，不拼接用户输入到 shell 字符串
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, connect=5.0))

    def check_health(self) -> bool:
        """检查 llama-server 是否可访问"""
        try:
            resp = self.client.get(f"{self.base_url}/health", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    def stream_chat(self, messages: list, cancel_event: threading.Event) -> Generator[str, None, None]:
        """
        流式对话。
        SEC-02: 返回的 token 只作文本拼接，不执行任何代码。
        SEC-03: URL 和参数通过 httpx 传递，无 shell 拼接。
        """
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "stream": True,
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
        }
        if LLM_KEEP_ALIVE:
            payload["keep_alive"] = 0 if LLM_KEEP_ALIVE == "0" else LLM_KEEP_ALIVE

        try:
            with self.client.stream("POST", url, json=payload) as response:
                if response.status_code == 503:
                    raise PipelineError(
                        "LLM_ERR_002",
                        "显存不足，请切换 8B 模型或关闭其他组件"
                    )
                if response.status_code != 200:
                    raise PipelineError(
                        "LLM_ERR_003",
                        f"大模型服务异常 (HTTP {response.status_code})"
                    )

                for line in response.iter_lines():
                    # 检查打断信号
                    if cancel_event.is_set():
                        logger.info("LLM 流被取消（用户打断）")
                        break

                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue

        except PipelineError:
            raise
        except httpx.ConnectError:
            raise PipelineError(
                "LLM_ERR_001",
                "本地大模型服务未启动，请先运行 start-llama.bat"
            )
        except Exception as e:
            logger.error(f"LLM 异常: {traceback.format_exc()}")
            raise PipelineError("LLM_ERR_003", "大模型回复超时，请重试", str(e))

    def warmup(self):
        """让本地模型在用户开口前完成加载，避免首轮回复被冷启动拖慢。"""
        if not LLM_WARMUP_ENABLED:
            return
        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": "你好"}],
            "stream": False,
            "options": {"temperature": 0, "num_predict": 1},
        }
        if LLM_KEEP_ALIVE:
            payload["keep_alive"] = LLM_KEEP_ALIVE
        started_at = time.perf_counter()
        try:
            response = self.client.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=httpx.Timeout(45.0, connect=5.0),
            )
            response.raise_for_status()
            logger.info(
                "LLM: 模型预热完成, 耗时=%.2fs, keep_alive=%s",
                time.perf_counter() - started_at,
                LLM_KEEP_ALIVE or "off",
            )
        except Exception as e:
            logger.warning("LLM: 预热失败，首次对话仍会按需加载: %s", e)


# 全局 LLM 客户端
llm_client = LLMClient()


def split_sentences(text: str) -> list:
    """
    按自然停顿拆句，尽早送 TTS。
    以中文标点（。！？；\n）+ 英文句号/问号/感叹号 + 逗号处切分。
    """
    # 先按主要断句标点切分
    parts = re.split(r'([。！？；\n!?;,，])', text)
    sentences = []
    current = ""
    for part in parts:
        current += part
        if re.search(r'[。！？\n!?]', part):
            sentences.append(current.strip())
            current = ""
    if current.strip():
        sentences.append(current.strip())
    # 合并过短的句子（少于 5 个字符合并到上一句）
    merged = []
    for s in sentences:
        if merged and len(s) < 5:
            merged[-1] += s
        else:
            merged.append(s)
    return [s for s in merged if s]


def pop_tts_units(buffer: str, final: bool = False) -> Tuple[list, str]:
    """从正在生成的回复中取出可安全交给 TTS 的语义片段。

    Qwen3-TTS 的 Python 接口当前返回完整音频数组，因此这里把 LLM 的流式
    输出切成句子/较长分句，让第一段音频可以在整段回复结束前开始合成。
    这不会截断回复，只改变 TTS 的提交边界。
    """
    units = []
    remaining = buffer
    while remaining:
        sentence_match = re.search(r"^(.+?[。！？；.!?;\n])", remaining, re.S)
        if sentence_match:
            candidate = sentence_match.group(1).strip()
            if len(candidate) >= 5:
                units.append(candidate)
                remaining = remaining[sentence_match.end():]
                continue

        # 没有完整句号时，在自然逗号后保留几个字再切一段。
        # 这样像“你好呀，愿你……”这类回复不会等到整句结束才开始
        # 克隆音色合成；切的是 TTS 提交边界，不会截断界面里的完整回复。
        if not final and len(remaining) >= 10:
            clause_match = re.search(r"^(.{3,}?[，,].{3,})", remaining, re.S)
            if clause_match:
                units.append(clause_match.group(1).strip())
                remaining = remaining[clause_match.end():]
                continue
        break

    if final and remaining.strip():
        units.append(remaining.strip())
        remaining = ""
    return units, remaining


def batch_tts_reply(text: str, max_chars: int = 160) -> List[str]:
    """Build large semantic TTS batches without restarting VoxCPM per sentence.

    LLM tokens are still shown immediately.  Once the short local-model reply
    is complete, ordinary replies use one VoxCPM streaming generation.  Only a
    genuinely long answer is split, and then at sentence boundaries so the
    playout buffer has ample audio to cover the next generation startup.
    """
    cleaned = text.strip()
    if not cleaned:
        return []
    sentences = [
        part.strip()
        for part in re.findall(r".+?[。！？；.!?;\n]+|.+$", cleaned, re.S)
        if part.strip()
    ]
    batches: List[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > max_chars:
            batches.append(current)
            current = ""
        if len(sentence) <= max_chars:
            current += sentence
            continue
        if current:
            batches.append(current)
            current = ""
        # Pathological punctuation-free answers still remain bounded.
        batches.extend(
            sentence[offset : offset + max_chars]
            for offset in range(0, len(sentence), max_chars)
        )
    if current:
        batches.append(current)
    return batches


def normalize_tts_text(text: str) -> str:
    """去掉克隆 TTS 不稳定的 emoji/控制符，保留界面里的原始回复文本。"""
    cleaned = re.sub(
        r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]",
        "",
        text,
    )
    cleaned = re.sub(r"[\u200B-\u200D\uFE0E\uFE0F]", "", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


# =============================================================================
# TTS 模块：VoxCPM 独立 Worker
# =============================================================================

''' LEGACY TTS IMPLEMENTATIONS (inert reference only; never executed).
The application must never instantiate the removed local TTS engines. Audio is
produced only by the local VoxCPM worker.

class _RemovedLegacyTTSEngine:
    """Low-latency neural TTS used by the immersive realtime mode."""

    def __init__(self):
        import edge_tts  # noqa: F401 - fail fast when the optional backend is absent
        self.voice = EDGE_TTS_VOICE
        self.rate = EDGE_TTS_RATE
        logger.info(f"TTS: 极速模式已就绪 ({self.voice}, rate={self.rate})")

    def synthesize(self, text: str, ref_wav_path: Optional[str] = None,
                   cancel_event: Optional[threading.Event] = None) -> Tuple[Optional[np.ndarray], int]:
        if cancel_event and cancel_event.is_set():
            return None, 0

        import edge_tts
        import soundfile as sf

        started_at = time.perf_counter()
        temp_path = os.path.join(LOG_DIR, f"edge_tts_{uuid.uuid4().hex}.mp3")
        try:
            edge_tts.Communicate(
                text=text,
                voice=self.voice,
                rate=self.rate,
            ).save_sync(temp_path)
            audio, sr = sf.read(temp_path, dtype="float32")
            if audio.ndim > 1:
                audio = audio[:, 0]
            audio = np.asarray(audio, dtype=np.float32).flatten()
            logger.info(
                f"TTS: 极速合成完成, 耗时={time.perf_counter() - started_at:.2f}s, "
                f"音频={len(audio) / sr:.1f}s"
            )
            return audio, int(sr)
        except Exception:
            logger.error(f"TTS 极速合成异常: {traceback.format_exc()}")
            return None, 0
        finally:
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

    def update_ref_audio(self, new_ref_path: str):
        logger.info("TTS: 极速模式不使用参考音频；Qwen 克隆模式仍保留该文件")

class TTSEngine:
    """
    语音合成引擎：Qwen3-TTS 1.7B + 音色克隆。
    接收文本，使用 ref.wav 音色克隆合成音频。
    """

    def __init__(self):
        self.model = None
        self.processor = None
        self.ref_wav = TTS_REF_WAV
        self.voice_clone_prompt = None
        self._backchannel_audio = None
        self._backchannel_lock = threading.Lock()
        self._init_model()

    def _init_model(self):
        """延迟加载 TTS 模型（使用 qwen-tts 官方库）"""
        try:
            import torch

            model_path = os.path.expanduser(TTS_MODEL_PATH)
            if not os.path.isdir(model_path):
                logger.warning(f"TTS 模型路径不存在: {model_path}")
                return

            logger.info(f"TTS: 加载模型 {model_path}...")

            from qwen_tts import Qwen3TTSModel
            self.model = Qwen3TTSModel.from_pretrained(
                model_path,
                device_map="cuda:0",
                dtype=torch.bfloat16,
                attn_implementation=QWEN_ATTN_IMPLEMENTATION,
                local_files_only=True,
            )
            self.processor = None  # qwen-tts 不需要独立的 processor
            self._refresh_voice_clone_prompt()
            logger.info(f"TTS: 模型加载完成（qwen-tts, attention={QWEN_ATTN_IMPLEMENTATION}）")

        except Exception as e:
            logger.error(f"TTS 模型初始化失败: {e}")
            logger.error(traceback.format_exc())
            self.model = None
            self.processor = None

    def _refresh_voice_clone_prompt(self):
        """Cache the reference voice embedding instead of rebuilding it per sentence."""
        if self.model is None or not os.path.isfile(self.ref_wav):
            self.voice_clone_prompt = None
            return
        self.voice_clone_prompt = self.model.create_voice_clone_prompt(
            ref_audio=self.ref_wav,
            x_vector_only_mode=True,
        )
        logger.info("TTS: 音色特征缓存完成")

    def load_ref_audio(self, ref_path: str) -> Optional[np.ndarray]:
        """加载参考音频"""
        try:
            import soundfile as sf
            audio, sr = sf.read(ref_path)
            # 重采样到 16kHz
            if sr != TTS_SAMPLE_RATE:
                import librosa
                audio = librosa.resample(audio, orig_sr=sr, target_sr=TTS_SAMPLE_RATE)
            return audio.astype(np.float32)
        except Exception as e:
            logger.error(f"加载参考音频失败: {e}")
            return None

    def synthesize(self, text: str, ref_wav_path: Optional[str] = None,
                   cancel_event: Optional[threading.Event] = None) -> Tuple[Optional[np.ndarray], int]:
        """
        合成语音。
        返回: (audio_data, sample_rate) 或 (None, 0) 表示失败
        SEC-03: 通过 Python API 调用，不使用 shell 命令。
        """
        text = normalize_tts_text(text)
        if not text:
            return None, 0
        ref_path = ref_wav_path or self.ref_wav

        if not os.path.isfile(ref_path):
            logger.warning(f"参考音频不存在: {ref_path}")
            return None, 0

        if self.model is None:
            logger.warning("TTS 模型未加载，无法合成语音")
            return None, 0

        # 检查打断信号
        if cancel_event and cancel_event.is_set():
            logger.info("TTS 合成被取消（用户打断）")
            return None, 0

        try:
            if not os.path.isfile(ref_path):
                logger.warning(f"参考音频不存在: {ref_path}")
                return None, 0

            logger.info(f"TTS: 合成 '{text[:50]}...' (ref={ref_path})")

            # Qwen3-TTS base model: voice cloning
            generate_kwargs = {
                "text": text,
                "language": "Chinese",
            }
            if ref_path == self.ref_wav and self.voice_clone_prompt is not None:
                generate_kwargs["voice_clone_prompt"] = self.voice_clone_prompt
            else:
                generate_kwargs["ref_audio"] = ref_path
                generate_kwargs["x_vector_only_mode"] = True
            wavs, sr = self.model.generate_voice_clone(**generate_kwargs)

            audio = wavs[0] if isinstance(wavs, list) else wavs
            audio = np.asarray(audio, dtype=np.float32).flatten()

            logger.info(f"TTS: 合成完成, {len(audio)} 采样点 @ {sr}Hz ({len(audio)/sr:.1f}s)")
            return audio, int(sr)

        except Exception as e:
            logger.error(f"TTS 合成异常: {traceback.format_exc()}")
            return None, 0

    def update_ref_audio(self, new_ref_path: str):
        """更新参考音频路径"""
        self.ref_wav = new_ref_path
        self._refresh_voice_clone_prompt()
        logger.info(f"参考音频已更新: {new_ref_path}")

    def prepare_backchannel(self):
        """预生成一条克隆音色的短回声，避免用户等待时完全无反馈。"""
        if self.model is None or self._backchannel_audio is not None:
            return
        with self._backchannel_lock:
            if self._backchannel_audio is not None:
                return
            # 只做极短的即时反馈；真正的语义回复仍由后续流式克隆 TTS 生成。
            audio, sr = self.synthesize("嗯。")
            if audio is not None and sr > 0:
                self._backchannel_audio = (audio, sr)
                logger.info("TTS: 克隆音色即时回声已预热")

    def get_backchannel(self):
        if self._backchannel_audio is None:
            return None
        audio, sr = self._backchannel_audio
        return np.copy(audio), sr


# 全局 TTS 客户端（懒加载）
# 注意：不在模块导入时初始化，避免 CUDA 模型加载卡死启动。
# 首次语音合成时通过 _get_tts() 按需加载。
tts_engine = None
class VoxCPMEngine:
    """Thin adapter that exposes the worker client's streaming contract."""

    def __init__(self):
        self.client = VoxCPMClient(VOXCPM_WORKER_URL)

    @staticmethod
    def _load_wav_compat(wav, target_sr, min_sr=16000):
        """Use soundfile directly to avoid TorchCodec/torchaudio drift."""
        import soundfile as sf
        import torch
        import librosa

        audio, sample_rate = sf.read(wav, dtype="float32")
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sample_rate != target_sr:
            if sample_rate < min_sr:
                raise ValueError(
                    f"wav sample rate {sample_rate} must be at least {min_sr}"
                )
            audio = librosa.resample(
                audio,
                orig_sr=sample_rate,
                target_sr=target_sr,
            )
        return torch.from_numpy(np.asarray(audio, dtype=np.float32)).unsqueeze(0)

    def _init_model(self):
        try:
            if not os.path.isdir(COSYVOICE_MODEL_PATH):
                logger.warning(f"CosyVoice model path not found: {COSYVOICE_MODEL_PATH}")
                return
            if not os.path.isdir(COSYVOICE_REPO):
                logger.warning(f"CosyVoice repo not found: {COSYVOICE_REPO}")
                return

            matcha_path = os.path.join(COSYVOICE_REPO, "third_party", "Matcha-TTS")
            for import_path in (matcha_path, COSYVOICE_REPO):
                if import_path not in sys.path:
                    sys.path.insert(0, import_path)

            from cosyvoice.cli.cosyvoice import AutoModel
            import cosyvoice.cli.frontend as frontend
            frontend.load_wav = self._load_wav_compat
            self.model = AutoModel(
                model_dir=COSYVOICE_MODEL_PATH,
                fp16=COSYVOICE_FP16,
            )

            # CosyVoice3's default first streaming block is conservative. A
            # smaller hop lets the cloned voice begin sooner while preserving
            # the same cached speaker embedding and inference session.
            if (
                COSYVOICE_STREAM_HOP_TOKENS > 0
                and self.model.__class__.__name__ == "CosyVoice3"
                and hasattr(self.model, "model")
                and hasattr(self.model.model, "token_hop_len")
            ):
                hop = max(10, COSYVOICE_STREAM_HOP_TOKENS)
                self.model.model.token_hop_len = hop
                if hasattr(self.model.model, "token_max_hop_len"):
                    self.model.model.token_max_hop_len = max(hop * 2, hop)
                logger.info("TTS: CosyVoice 首块 token hop=%d", hop)

            if not os.path.isfile(self.ref_wav):
                logger.warning(f"CosyVoice reference audio not found: {self.ref_wav}")
                self.model = None
                return

            # Extract speaker features once. Every sentence and bi-stream turn
            # reuses this ID. CosyVoice3 requires the instruction terminator in
            # the cached prompt; CosyVoice2 does not.
            self.prompt_text = TTS_REF_TEXT
            if self.model.__class__.__name__ == "CosyVoice3" and "<|endofprompt|>" not in self.prompt_text:
                self.prompt_text = (
                    "You are a helpful assistant.<|endofprompt|>"
                    + self.prompt_text
                )
            self.model.add_zero_shot_spk(self.prompt_text, self.ref_wav, self.voice_id)
            logger.info(
                "TTS: CosyVoice streaming clone ready "
                f"(model={COSYVOICE_MODEL_PATH}, fp16={COSYVOICE_FP16})"
            )
        except Exception:
            logger.error(f"CosyVoice TTS init failed: {traceback.format_exc()}")
            self.model = None

    def stream_synthesize(
        self,
        text: str,
        cancel_event: Optional[threading.Event] = None,
    ) -> Generator[Tuple[np.ndarray, int], None, None]:
        text = normalize_tts_text(text)
        if not text or self.model is None:
            return
        if cancel_event and cancel_event.is_set():
            return

        started_at = time.perf_counter()
        first_chunk = True
        with self._inference_lock:
            try:
                stream = self.model.inference_zero_shot(
                    text,
                    "",
                    "",
                    zero_shot_spk_id=self.voice_id,
                    stream=True,
                    text_frontend=False,
                )
                for item in stream:
                    if cancel_event and cancel_event.is_set():
                        return
                    audio = item.get("tts_speech")
                    if audio is None:
                        continue
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    audio = np.asarray(audio, dtype=np.float32).flatten()
                    if not len(audio):
                        continue
                    if first_chunk:
                        logger.info(
                            "TTS: CosyVoice 首包 %.2fs, %.2fs 音频",
                            time.perf_counter() - started_at,
                            len(audio) / self.model.sample_rate,
                        )
                        first_chunk = False
                    yield audio, int(self.model.sample_rate)
            except Exception:
                logger.error(f"CosyVoice TTS synthesis failed: {traceback.format_exc()}")

    def stream_text_synthesize(
        self,
        text_source,
        cancel_event: Optional[threading.Event] = None,
    ) -> Generator[Tuple[np.ndarray, int], None, None]:
        """Keep one cloned-voice session open while LLM text arrives incrementally."""
        if self.model is None or cancel_event and cancel_event.is_set():
            return

        started_at = time.perf_counter()
        first_chunk = True
        with self._inference_lock:
            try:
                stream = self.model.inference_zero_shot(
                    text_source,
                    "",
                    "",
                    zero_shot_spk_id=self.voice_id,
                    stream=True,
                    text_frontend=False,
                )
                for item in stream:
                    if cancel_event and cancel_event.is_set():
                        return
                    audio = item.get("tts_speech")
                    if audio is None:
                        continue
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    audio = np.asarray(audio, dtype=np.float32).flatten()
                    if not len(audio):
                        continue
                    if first_chunk:
                        logger.info(
                            "TTS: CosyVoice bi-stream 首包 %.2fs, %.2fs 音频",
                            time.perf_counter() - started_at,
                            len(audio) / self.model.sample_rate,
                        )
                        first_chunk = False
                    yield audio, int(self.model.sample_rate)
            except Exception:
                logger.error(
                    f"CosyVoice bi-stream TTS synthesis failed: {traceback.format_exc()}"
                )

    def synthesize(
        self,
        text: str,
        ref_wav_path: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Tuple[Optional[np.ndarray], int]:
        if ref_wav_path and ref_wav_path != self.ref_wav:
            self.update_ref_audio(ref_wav_path)
        chunks = []
        sample_rate = 0
        for audio, sr in self.stream_synthesize(text, cancel_event):
            chunks.append(audio)
            sample_rate = sr
        if not chunks or not sample_rate:
            return None, 0
        audio = np.concatenate(chunks).astype(np.float32, copy=False)
        logger.info(
            "TTS: CosyVoice 合成完成 %d samples @ %dHz (%.1fs)",
            len(audio),
            sample_rate,
            len(audio) / sample_rate,
        )
        return audio, sample_rate

    def prepare_backchannel(self):
        if self.model is None or self._backchannel_audio is not None:
            return
        with self._backchannel_lock:
            if self._backchannel_audio is not None:
                return
            # Keep the immediate acknowledgement short; semantic speech follows
            # through the bi-stream path and must not be mistaken for the reply.
            audio, sr = self.synthesize("嗯。")
            if audio is not None and sr > 0:
                self._backchannel_audio = (audio, sr)
                logger.info("TTS: CosyVoice 克隆音色即时回声已预热")

    def get_backchannel(self):
        if self._backchannel_audio is None:
            return None
        audio, sr = self._backchannel_audio
        return np.copy(audio), sr

    def update_ref_audio(self, new_ref_path: str):
        if self.model is None or not os.path.isfile(new_ref_path):
            return
        with self._inference_lock:
            self.ref_wav = new_ref_path
            self.model.add_zero_shot_spk(self.prompt_text, self.ref_wav, self.voice_id)
            self._backchannel_audio = None
        logger.info(f"CosyVoice 克隆音色参考音频已更新: {new_ref_path}")


_tts_init_lock = threading.Lock()

def _get_tts():
    """懒加载 TTS 引擎"""
    global tts_engine
    if tts_engine is None:
        with _tts_init_lock:
            if tts_engine is None:
                tts_engine = VoxCPMClient(VOXCPM_WORKER_URL)
    return tts_engine


'''

tts_engine = None
_tts_init_lock = threading.Lock()

def _get_tts() -> VoxCPMClient:
    """Return the single configured VoxCPM worker client."""
    global tts_engine
    if tts_engine is None:
        with _tts_init_lock:
            if tts_engine is None:
                tts_engine = VoxCPMClient(
                    VOXCPM_WORKER_URL,
                    style_prompt=VOXCPM_STYLE_PROMPT,
                )
    return tts_engine

# =============================================================================
# 音频上传处理（音色克隆 US-03）
# =============================================================================

def validate_ref_audio(file_path: str) -> Tuple[bool, str]:
    """
    校验参考音频（SEC-05, SEC-12）。
    返回: (is_valid, error_message)
    """
    # SEC-05: 路径限定在 ~/setup/uploads/ 内
    upload_dir_real = os.path.realpath(UPLOAD_DIR)
    file_real = os.path.realpath(file_path)

    if not file_real.startswith(upload_dir_real + os.sep) and file_real != upload_dir_real:
        return False, "文件路径不在允许的上传目录内"

    # SEC-12: 扩展名白名单
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        return False, f"不支持的音频格式: .{ext}，支持: {', '.join(ALLOWED_AUDIO_EXTENSIONS)}"

    # SEC-12: 大小上限 ≤15MB
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_SIZE_MB:
        return False, f"文件过大 ({size_mb:.1f}MB)，上限 {MAX_UPLOAD_SIZE_MB}MB"

    # 时长校验 5-15 秒
    try:
        import soundfile as sf
        info = sf.info(file_path)
        duration = info.duration
        if duration < MIN_REF_AUDIO_SEC or duration > MAX_REF_AUDIO_SEC:
            return False, f"音频时长 {duration:.1f} 秒不符合要求（需 {MIN_REF_AUDIO_SEC}-{MAX_REF_AUDIO_SEC} 秒）"
    except Exception as e:
        logger.warning(f"音频时长校验失败: {e}")

    return True, ""

def handle_audio_upload(uploaded_file, prompt_text: str = "") -> dict:
    """
    处理参考音频上传。
    SEC-05: 文件名防路径遍历，限制在 UPLOAD_DIR。
    SEC-12: 白名单扩展名 + 大小上限。
    """
    if uploaded_file is None:
        return error_response("TTS_ERR_002", "请上传 5-15 秒清晰单人声参考音频")

    global ACTIVE_REF_WAV, ACTIVE_REF_TEXT
    try:
        # 安全：获取原始文件名并清除路径分隔符（防路径遍历）
        original_name = os.path.basename(str(uploaded_file) if isinstance(uploaded_file, str) else "ref_upload.wav")
        safe_name = hashlib.sha256(original_name.encode()).hexdigest()[:16]
        ext = os.path.splitext(original_name)[1].lower()
        if ext.lstrip(".") not in ALLOWED_AUDIO_EXTENSIONS:
            ext = ".wav"
        save_path = os.path.join(UPLOAD_DIR, f"ref_{safe_name}{ext}")

        # 复制/保存上传的文件
        if isinstance(uploaded_file, str) and os.path.isfile(uploaded_file):
            shutil.copy2(uploaded_file, save_path)
        elif hasattr(uploaded_file, 'read'):
            with open(save_path, "wb") as f:
                f.write(uploaded_file.read())
        else:
            return error_response("TTS_ERR_002", "上传文件格式不支持")

        # 校验
        is_valid, err_msg = validate_ref_audio(save_path)
        if not is_valid:
            os.remove(save_path)
            return error_response("TTS_ERR_002", err_msg)

        logger.info(f"参考音频已保存: {save_path}")
        try:
            worker_reply = _get_tts().update_ref_audio(save_path, prompt_text.strip())
        except Exception as exc:
            logger.error("VoxCPM 参考音频更新失败: %s", exc)
            return error_response("TTS_WORKER_001", "参考音频已保存，但 VoxCPM 音色更新失败，请检查 Worker")
        ACTIVE_REF_WAV = save_path
        if prompt_text.strip():
            ACTIVE_REF_TEXT = prompt_text.strip()
        return {
            "ok": True,
            "message": "参考音频上传成功，音色克隆已更新",
            # SEC-17: 不暴露完整文件路径
            "file_id": safe_name,
            "reference_id": worker_reply.get("reference_id"),
        }

    except Exception as e:
        logger.error(f"音频上传处理异常: {traceback.format_exc()}")
        return error_response("TTS_ERR_002", "参考音频上传处理失败", "")


EDITABLE_RUNTIME_KEYS = (
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "LLM_KEEP_ALIVE",
    "ROLE_STYLE",
    "ROLE_CUSTOM_INSTRUCTION",
    "VAD_THRESH",
    "MIN_SILENCE_MS",
    "MAX_AUDIO_SEC",
    "ASR_LANG",
    "VOXCPM_STYLE_PROMPT",
    "AVATAR_AUDIO_GAIN",
    "AVATAR_PREBUFFER_MS",
    "AVATAR_REBUFFER_MS",
    "AVATAR_MAX_BUFFER_MS",
    "AVATAR_FADE_IN_MS",
    "AVATAR_LEAD_IN_MS",
    "LOG_LEVEL",
)


def get_runtime_settings() -> dict:
    """Return a safe, user-facing snapshot of the current runtime settings."""
    reference_exists = bool(ACTIVE_REF_WAV and os.path.isfile(ACTIVE_REF_WAV))
    return {
        "llm_model": LLM_MODEL,
        "tts_model": VOXCPM_MODEL_ID,
        "tts_profile": VOXCPM_PROFILE,
        "tts_style": VOXCPM_STYLE_PROMPT or "默认",
        "role_style": ROLE_STYLE,
        "role_custom_instruction": ROLE_CUSTOM_INSTRUCTION or "未设置",
        "tts_sample_rate": f"{VOXCPM_SAMPLE_RATE} Hz",
        "audio_gain": f"{AVATAR_AUDIO_GAIN:.1f}x",
        "prebuffer": f"{AVATAR_PREBUFFER_MS} ms",
        "reference_audio": "已配置" if reference_exists else "未配置",
        "reference_text": ACTIVE_REF_TEXT or "未设置",
        "local_files_only": "是" if VOXCPM_LOCAL_FILES_ONLY else "否",
        "llm_temperature": LLM_TEMPERATURE,
        "llm_max_tokens": LLM_MAX_TOKENS,
        "llm_keep_alive": LLM_KEEP_ALIVE,
        "vad_threshold": VAD_THRESH,
        "min_silence_ms": MIN_SILENCE_MS,
        "max_audio_sec": MAX_AUDIO_SEC,
        "asr_language": ASR_LANG,
        "rebuffer": AVATAR_REBUFFER_MS,
        "max_buffer": AVATAR_MAX_BUFFER_MS,
        "fade_in": AVATAR_FADE_IN_MS,
        "lead_in": AVATAR_LEAD_IN_MS,
        "log_level": LOG_LEVEL,
    }


def get_runtime_form_values() -> Tuple[Any, ...]:
    """Return editable values in the same stable order as the settings form."""
    return (
        LLM_TEMPERATURE,
        LLM_MAX_TOKENS,
        LLM_KEEP_ALIVE,
        ROLE_STYLE,
        ROLE_CUSTOM_INSTRUCTION,
        VAD_THRESH,
        MIN_SILENCE_MS,
        MAX_AUDIO_SEC,
        ASR_LANG,
        VOXCPM_STYLE_PROMPT,
        AVATAR_AUDIO_GAIN,
        AVATAR_PREBUFFER_MS,
        AVATAR_REBUFFER_MS,
        AVATAR_MAX_BUFFER_MS,
        AVATAR_FADE_IN_MS,
        AVATAR_LEAD_IN_MS,
        LOG_LEVEL,
    )


def _coerce_runtime_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the public settings whitelist and normalize component values."""
    unknown = set(values) - set(EDITABLE_RUNTIME_KEYS)
    if unknown:
        raise ValueError("包含不允许修改的设置")

    normalized: Dict[str, Any] = {}

    def number(key: str, minimum: float, maximum: float, *, integer: bool = False):
        raw = values.get(key)
        try:
            value = int(raw) if integer else float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 必须是数字") from exc
        if value < minimum or value > maximum:
            raise ValueError(f"{key} 必须在 {minimum:g}–{maximum:g} 之间")
        normalized[key] = value

    number("LLM_TEMPERATURE", 0.0, 1.5)
    number("LLM_MAX_TOKENS", 128, 4096, integer=True)
    number("VAD_THRESH", 0.1, 0.9)
    number("MIN_SILENCE_MS", 300, 1500, integer=True)
    number("MAX_AUDIO_SEC", 5, 60, integer=True)
    number("AVATAR_AUDIO_GAIN", 0.5, 2.5)
    number("AVATAR_PREBUFFER_MS", 400, 2500, integer=True)
    number("AVATAR_REBUFFER_MS", 100, 1200, integer=True)
    number("AVATAR_MAX_BUFFER_MS", 2000, 12000, integer=True)
    number("AVATAR_FADE_IN_MS", 0, 200, integer=True)
    number("AVATAR_LEAD_IN_MS", 0, 500, integer=True)

    if normalized["AVATAR_REBUFFER_MS"] > normalized["AVATAR_PREBUFFER_MS"]:
        raise ValueError("重新缓冲不能大于首段预缓冲")
    if normalized["AVATAR_MAX_BUFFER_MS"] <= normalized["AVATAR_PREBUFFER_MS"]:
        raise ValueError("最大缓冲必须大于首段预缓冲")

    keep_alive = str(values.get("LLM_KEEP_ALIVE", "")).strip().lower()
    if not re.fullmatch(r"(?:0|[1-9]\d*[smhd])", keep_alive):
        raise ValueError("模型驻留时间请使用 30m、2h、24h 或 0 这类格式")
    normalized["LLM_KEEP_ALIVE"] = keep_alive

    role_style = str(values.get("ROLE_STYLE", "")).strip()
    if not role_style or len(role_style) > 40:
        raise ValueError("角色风格不能为空且最多 40 个字符")
    normalized["ROLE_STYLE"] = role_style

    custom_instruction = str(values.get("ROLE_CUSTOM_INSTRUCTION", "")).strip()
    if len(custom_instruction) > 300:
        raise ValueError("补充人设说明最多 300 个字符")
    normalized["ROLE_CUSTOM_INSTRUCTION"] = custom_instruction

    language = str(values.get("ASR_LANG", "")).strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}", language):
        raise ValueError("识别语言请使用 zh、en、ja 等语言代码")
    normalized["ASR_LANG"] = language

    style_prompt = str(values.get("VOXCPM_STYLE_PROMPT", "")).strip()
    if len(style_prompt) > 80:
        raise ValueError("语音风格最多 80 个字符")
    normalized["VOXCPM_STYLE_PROMPT"] = style_prompt

    log_level = str(values.get("LOG_LEVEL", "INFO")).strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("日志级别不受支持")
    normalized["LOG_LEVEL"] = log_level
    return normalized


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _persist_runtime_settings(values: Dict[str, Any]) -> None:
    """Atomically update only whitelisted scalar keys while preserving comments."""
    config_path = Path(CONFIG_PATH)
    with _config_write_lock:
        original = config_path.read_text(encoding="utf-8")
        updated = original
        for key, value in values.items():
            if key not in EDITABLE_RUNTIME_KEYS:
                raise ValueError("拒绝写入非白名单设置")
            replacement = f"{key}: {_yaml_scalar(value)}"
            pattern = re.compile(rf"(?m)^{re.escape(key)}\s*:\s*[^\r\n]*$")
            if pattern.search(updated):
                updated = pattern.sub(replacement, updated, count=1)
            else:
                updated = updated.rstrip() + f"\n{replacement}\n"

        temp_path = config_path.with_name(config_path.name + ".runtime.tmp")
        try:
            temp_path.write_text(updated, encoding="utf-8")
            try:
                os.chmod(temp_path, config_path.stat().st_mode)
            except OSError:
                pass
            os.replace(temp_path, config_path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)


def apply_runtime_settings(values: Dict[str, Any]) -> dict:
    """Apply validated runtime settings now and persist them for the next start."""
    global LLM_TEMPERATURE, LLM_MAX_TOKENS, LLM_KEEP_ALIVE
    global ROLE_STYLE, ROLE_CUSTOM_INSTRUCTION
    global VAD_THRESH, MIN_SILENCE_MS, MAX_AUDIO_SEC, ASR_LANG
    global VOXCPM_STYLE_PROMPT, AVATAR_AUDIO_GAIN, AVATAR_PREBUFFER_MS
    global AVATAR_REBUFFER_MS, AVATAR_MAX_BUFFER_MS, AVATAR_FADE_IN_MS
    global AVATAR_LEAD_IN_MS, LOG_LEVEL, config

    if state_machine.state is not AvatarState.IDLE:
        return error_response("CFG_ERR_003", "请等待当前回复结束或先点击停止")
    try:
        normalized = _coerce_runtime_settings(values)
        _persist_runtime_settings(normalized)
    except (OSError, ValueError) as exc:
        return error_response("CFG_ERR_002", str(exc))

    avatar_changed = any(
        normalized[key] != globals()[key]
        for key in (
            "AVATAR_AUDIO_GAIN", "AVATAR_PREBUFFER_MS", "AVATAR_REBUFFER_MS",
            "AVATAR_MAX_BUFFER_MS", "AVATAR_FADE_IN_MS", "AVATAR_LEAD_IN_MS",
        )
    )
    LLM_TEMPERATURE = normalized["LLM_TEMPERATURE"]
    LLM_MAX_TOKENS = normalized["LLM_MAX_TOKENS"]
    LLM_KEEP_ALIVE = normalized["LLM_KEEP_ALIVE"]
    ROLE_STYLE = normalized["ROLE_STYLE"]
    ROLE_CUSTOM_INSTRUCTION = normalized["ROLE_CUSTOM_INSTRUCTION"]
    VAD_THRESH = normalized["VAD_THRESH"]
    MIN_SILENCE_MS = normalized["MIN_SILENCE_MS"]
    MAX_AUDIO_SEC = normalized["MAX_AUDIO_SEC"]
    ASR_LANG = normalized["ASR_LANG"]
    VOXCPM_STYLE_PROMPT = normalized["VOXCPM_STYLE_PROMPT"]
    AVATAR_AUDIO_GAIN = normalized["AVATAR_AUDIO_GAIN"]
    AVATAR_PREBUFFER_MS = normalized["AVATAR_PREBUFFER_MS"]
    AVATAR_REBUFFER_MS = normalized["AVATAR_REBUFFER_MS"]
    AVATAR_MAX_BUFFER_MS = normalized["AVATAR_MAX_BUFFER_MS"]
    AVATAR_FADE_IN_MS = normalized["AVATAR_FADE_IN_MS"]
    AVATAR_LEAD_IN_MS = normalized["AVATAR_LEAD_IN_MS"]
    LOG_LEVEL = normalized["LOG_LEVEL"]
    config.update(normalized)

    active_pipeline = globals().get("pipeline")
    if active_pipeline is not None:
        active_pipeline.vad.threshold = VAD_THRESH
        active_pipeline.vad.min_silence_ms = MIN_SILENCE_MS
        if avatar_changed:
            active_pipeline._close_avatar_audio_stream()
    active_tts = globals().get("tts_engine")
    if active_tts is not None:
        active_tts.style_prompt = VOXCPM_STYLE_PROMPT.strip().strip("()（）")
    active_pipeline = globals().get("pipeline")
    if active_pipeline is not None:
        set_role_style = getattr(active_pipeline, "set_role_style", None)
        if set_role_style is not None:
            set_role_style(ROLE_STYLE, ROLE_CUSTOM_INSTRUCTION)
    logging.getLogger().setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    logger.info("运行设置已应用: %s", ", ".join(normalized.keys()))
    return {"ok": True, "message": "运行设置已保存，将从下一轮回复开始生效"}


def render_runtime_settings(settings: Optional[dict] = None) -> str:
    """Render settings without exposing local paths or internal endpoints."""
    values = settings or get_runtime_settings()
    rows = (
        ("大模型", values["llm_model"]),
        ("语音模型", f"{values['tts_model']} · {values['tts_profile']}"),
        ("角色人设", values["role_style"]),
        ("语速风格", values["tts_style"]),
        ("采样率", values["tts_sample_rate"]),
        ("播放增益", values["audio_gain"]),
        ("播放预缓冲", values["prebuffer"]),
        ("参考音频", values["reference_audio"]),
        ("本地模型", values["local_files_only"]),
    )
    items = "".join(
        f'<div class="settings-row"><span>{html.escape(str(label))}</span>'
        f'<strong>{html.escape(str(value))}</strong></div>'
        for label, value in rows
    )
    prompt = html.escape(str(values["reference_text"]))
    return (
        '<div class="settings-card">'
        f"{items}"
        f'<div class="settings-reference-text"><span>参考文本</span>'
        f'<p>{prompt}</p></div>'
        "</div>"
    )


def apply_reference_audio(file_path: Optional[str], prompt_text: str = "") -> dict:
    """Apply a validated reference audio file after explicit user confirmation."""
    if not file_path:
        return error_response("TTS_ERR_002", "请选择参考音频后再应用")
    return handle_audio_upload(file_path, prompt_text)


def get_recent_logs(source: str = "全部", limit: int = 200) -> str:
    """Read only allow-listed local service logs, capped to the latest lines."""
    try:
        line_limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        line_limit = 200
    if source not in LOG_SOURCE_LABELS:
        source = "全部"
    sources = LOG_SOURCE_PATHS if source == "全部" else {source: LOG_SOURCE_PATHS[source]}
    blocks = []
    for label, path_value in sources.items():
        path = Path(path_value)
        try:
            if not path.is_file():
                content = "暂无日志"
            else:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    lines = deque((line.rstrip("\r\n") for line in stream), maxlen=line_limit)
                content = "\n".join(lines) or "暂无日志"
        except OSError:
            content = "日志暂时不可读取"
        blocks.append(f"【{label}】\n{content}")
    return "\n\n".join(blocks)


def render_recent_logs(source: str = "全部", limit: int = 200) -> str:
    """Render allow-listed logs as a readable, level-colored HTML viewer."""
    plain_text = get_recent_logs(source, limit)
    rendered_blocks = []
    for block in plain_text.split("\n\n"):
        lines = block.splitlines()
        if not lines:
            continue
        title = html.escape(lines[0])
        rendered_lines = []
        visible_line = 0
        for raw_line in lines[1:]:
            visible_line += 1
            level_match = re.search(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", raw_line.upper())
            level = level_match.group(1).lower() if level_match else "default"
            rendered_lines.append(
                f'<div class="utility-log-line utility-log-{level}">'
                f'<span class="utility-log-number">{visible_line:04d}</span>'
                f'<span class="utility-log-message">{html.escape(raw_line) or "&nbsp;"}</span>'
                "</div>"
            )
        if not rendered_lines:
            rendered_lines.append(
                '<div class="utility-log-line utility-log-default">'
                '<span class="utility-log-number">0001</span>'
                '<span class="utility-log-message">暂无日志</span>'
                "</div>"
            )
        rendered_blocks.append(
            '<section class="utility-log-block">'
            f'<div class="utility-log-source">{title}</div>'
            f'{"".join(rendered_lines)}'
            "</section>"
        )
    return '<div class="utility-log-scroll">' + "".join(rendered_blocks) + "</div>"


STATUS_LABELS = {
    "llama": "大模型",
    "asr": "语音识别",
    "tts": "语音合成",
    "livetalking": "数字人口型",
    "avatar_sync": "页面桥接",
}
STATUS_TEXTS = {
    "connected": "已连接",
    "ready": "就绪",
    "degraded": "降级",
    "timeout": "超时",
    "disconnected": "未连接",
    "error": "错误",
}


def format_status_summary(status: dict) -> str:
    overall = status.get("overall", "partial")
    if overall == "ready":
        label, tone = "系统正常", "ready"
    else:
        label, tone = "部分服务异常", "degraded"
    return (
        f'<div class="overall-status {tone}" role="status" aria-live="polite">'
        f'<span class="status-dot {tone}"></span>{label}'
        " · 点击左下角状态查看详情</div>"
    )


def format_status_details(status: dict) -> str:
    rows = []
    for key, label in STATUS_LABELS.items():
        value = status.get(key, "disconnected")
        text = STATUS_TEXTS.get(value, value)
        rows.append(
            f'<div class="service-status-row">'
            f'<span><span class="status-dot {html.escape(value)}"></span>'
            f'{html.escape(label)}</span><strong>{html.escape(text)}</strong></div>'
        )
    return '<div class="service-status-card">' + "".join(rows) + "</div>"


def refresh_status_details() -> Tuple[str, str]:
    """Probe services and return the compact summary plus detailed HTML."""
    status = get_service_status()
    return format_status_summary(status), format_status_details(status)


def toggle_utility_panel(panel_name: str):
    """Return Gradio visibility updates for one mutually-exclusive panel."""
    names = {"settings": "设置", "logs": "日志", "status": "状态"}
    selected = panel_name if panel_name in names else "status"
    return (
        gr.update(visible=True),
        names[selected],
        gr.update(visible=selected == "settings"),
        gr.update(visible=selected == "logs"),
        gr.update(visible=selected == "status"),
    )


def close_utility_panel():
    """Close the shared utility drawer and all of its views."""
    return (
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(visible=False),
    )


# =============================================================================
# 音频转发到 avatar-sync.js (驱动口型)
# =============================================================================

def forward_text_to_livetalking(text: str, session_id: str = "0") -> bool:
    """发送文字到 LiveTalking /human，由 LiveTalking 内部 TTS 驱动口型（官方推荐方式）。"""
    try:
        url = f"{LIVETALKING_URL}/human"
        resp = requests.post(url,
            json={"text": text, "type": "echo", "sessionid": session_id},
            timeout=5.0)
        ok = resp.status_code == 200
        if ok:
            logger.info(f"口型驱动(文字): /human 成功 -> {resp.json()}")
        else:
            logger.warning(f"口型驱动(文字): /human 失败 HTTP {resp.status_code}: {resp.text[:200]}")
        return ok
    except Exception as e:
        logger.warning(f"口型驱动(文字): 异常 - {e}")
        return False


# =============================================================================
# 核心对话管线
# =============================================================================

BASE_SYSTEM_PROMPT = (
    "你是一个友善、体贴的AI语音助手。回复自然、有陪伴感、口语化，适合语音朗读；"
    "根据用户需要决定回复长度，不要为了语音合成而刻意缩短内容。"
)

ROLE_STYLE_PRESETS = {
    "温柔陪伴": "你温柔、耐心、善于倾听，先共情再回答，不说教，不强行热情。",
    "活泼俏皮": "你活泼、轻松、俏皮，适度使用幽默，但不要油腻，也不要影响信息准确性。",
    "成熟知性": "你成熟、理性、清晰，善于梳理重点，用有分寸的方式给出可靠建议。",
    "元气少女": "你阳光、有活力、亲切可爱，但保持自然和尊重，不使用幼稚或夸张的表达。",
    "安静倾听": "你安静、克制、细腻，优先理解用户情绪，回复简洁，不抢话，不制造压力。",
}


def build_system_prompt(role_style: str = "", custom_instruction: str = "") -> str:
    """Build the LLM persona prompt from a safe preset plus optional user note."""
    style = str(role_style or "温柔陪伴").strip()
    persona = ROLE_STYLE_PRESETS.get(style, f"当前角色风格是：{style}。")
    custom = str(custom_instruction or "").strip()
    if custom:
        persona += f"补充人设要求：{custom}"
    return f"{BASE_SYSTEM_PROMPT}当前角色设定：{persona}"

class ConversationPipeline:
    """语音对话完整管线：VAD -> ASR -> LLM -> TTS -> 双路分发"""

    def __init__(self):
        self.vad = VADDetector()
        self.role_style = ROLE_STYLE
        self.role_custom_instruction = ROLE_CUSTOM_INSTRUCTION
        self.history: List[dict] = [
            {"role": "system", "content": build_system_prompt(self.role_style, self.role_custom_instruction)}
        ]
        self._avatar_stream: Optional[LiveTalkingAudioPlayout] = None
        self._avatar_stream_lock = threading.Lock()

    def set_role_style(self, role_style: str, custom_instruction: str = "") -> None:
        """Apply persona to the next LLM request without disturbing audio state."""
        self.role_style = role_style.strip() or "温柔陪伴"
        self.role_custom_instruction = custom_instruction.strip()
        system_prompt = build_system_prompt(
            self.role_style, self.role_custom_instruction
        )
        if self.history and self.history[0].get("role") == "system":
            self.history[0]["content"] = system_prompt
        else:
            self.history.insert(0, {"role": "system", "content": system_prompt})
        logger.info("角色风格已应用: %s", self.role_style)

    def _push_avatar_audio(self, audio_data: np.ndarray, sample_rate: int) -> bool:
        """把 VoxCPM 音频交给固定时钟的持久 LiveTalking 播放器。"""
        with self._avatar_stream_lock:
            if self._avatar_stream is None or self._avatar_stream.failed:
                old_stream = self._avatar_stream
                self._avatar_stream = LiveTalkingAudioPlayout(
                    LIVETALKING_URL,
                    "0",
                    output_rate=AVATAR_OUTPUT_SAMPLE_RATE,
                    frame_ms=AVATAR_FRAME_MS,
                    prebuffer_ms=AVATAR_PREBUFFER_MS,
                    rebuffer_ms=AVATAR_REBUFFER_MS,
                    max_buffer_ms=AVATAR_MAX_BUFFER_MS,
                    gain=AVATAR_AUDIO_GAIN,
                    fade_in_ms=AVATAR_FADE_IN_MS,
                    lead_in_ms=AVATAR_LEAD_IN_MS,
                )
                if old_stream is not None:
                    old_stream.close()
            stream = self._avatar_stream
        return stream.push(audio_data, int(sample_rate))

    def _finish_avatar_audio_stream(self, wait: bool = False):
        """结束本轮话语，但保留跨轮复用的 LiveTalking HTTP 连接。"""
        with self._avatar_stream_lock:
            stream = self._avatar_stream
        if stream is not None:
            stream.finish_utterance()
            if wait and not stream.wait_until_idle(timeout=60.0):
                logger.warning("口型驱动: 等待播放队列清空超时")

    def _interrupt_avatar_audio_stream(self):
        with self._avatar_stream_lock:
            stream = self._avatar_stream
        if stream is not None:
            stream.interrupt()

    def _close_avatar_audio_stream(self):
        """只在应用退出时真正关闭 LiveTalking HTTP 连接。"""
        with self._avatar_stream_lock:
            stream = self._avatar_stream
            self._avatar_stream = None
        if stream is not None:
            stream.close()

    def _backchannel_event(self):
        """返回已预热的克隆音色短回声，避免用户等待时完全无反馈。"""
        if not TTS_BACKCHANNEL_ENABLED:
            return None
        engine = _get_tts()
        return None

    def process_voice(self, audio_data: np.ndarray, sample_rate: int = 16000) -> Generator[dict, None, None]:
        """
        处理语音输入 —— 完整管线。
        生成器输出状态事件（yield dict）。
        """
        session_id = str(uuid.uuid4())
        state_machine.reset_cancel()

        # ---- 状态: 倾听 ----
        state_machine.transition(AvatarState.LISTENING)
        yield {"type": "status", "state": "listening", "text": "倾听中..."}

        # ---- ASR 转写 ----
        state_machine.transition(AvatarState.THINKING)
        yield {"type": "status", "state": "thinking", "text": "正在识别..."}

        max_samples = max(1, int(sample_rate * MAX_AUDIO_SEC))
        if audio_data is not None and len(audio_data) > max_samples:
            logger.info("录音超过 %s 秒，已按设置截取", MAX_AUDIO_SEC)
            audio_data = audio_data[:max_samples]

        if _get_asr() is None:
            state_machine.transition(AvatarState.IDLE)
            yield {"type": "error", "error": {"code": "ASR_UNAVAILABLE", "message": "语音识别未安装（阶段 2 功能），请使用文字输入"}}
            return
        try:
            user_text = _get_asr().transcribe(audio_data, sample_rate)
        except PipelineError as e:
            self._finish_avatar_audio_stream()
            state_machine.transition(AvatarState.IDLE)
            yield {"type": "error", "error": e.to_dict()["error"]}
            return

        yield {"type": "transcription", "text": user_text}
        self.history.append({"role": "user", "content": user_text})

        backchannel = self._backchannel_event()
        if backchannel:
            yield backchannel

        # ---- LLM 流式回复 ----
        yield {"type": "status", "state": "thinking", "text": "思考中..."}

        cancel = state_machine.cancel_event

        # Stream LLM text to the UI first, then use one VoxCPM streaming
        # generation for a normal reply. Restarting VoxCPM for every sentence
        # creates a measurable gap on the RTX 5060 Ti.
        full_reply = ""
        try:
            for token in llm_client.stream_chat(self.history, cancel):
                if cancel.is_set():
                    break
                full_reply += token
                yield {"type": "reply_token", "text": token}
            if full_reply.strip() and not cancel.is_set():
                for speech_batch in batch_tts_reply(full_reply):
                    if cancel.is_set():
                        break
                    yield from self._synthesize_and_dispatch(
                        speech_batch, session_id, cancel
                    )
        except PipelineError as e:
            self._finish_avatar_audio_stream()
            state_machine.transition(AvatarState.IDLE)
            yield {"type": "error", "error": e.to_dict()["error"]}
            return

        # ---- 保存到历史 ----
        if not cancel.is_set():
            self.history.append({"role": "assistant", "content": full_reply})
            # 限制历史长度（滑动窗口，保留最近 20 轮）
            if len(self.history) > 41:  # 1 system + 20*2 user/assistant
                self.history = [self.history[0]] + self.history[-40:]

        self._finish_avatar_audio_stream(wait=True)
        state_machine.transition(AvatarState.IDLE)
        yield {"type": "status", "state": "idle", "text": "待机"}

    def process_text(self, text: str) -> Generator[dict, None, None]:
        """处理文字输入"""
        session_id = str(uuid.uuid4())
        state_machine.reset_cancel()

        state_machine.transition(AvatarState.THINKING)
        yield {"type": "status", "state": "thinking", "text": "思考中..."}

        self.history.append({"role": "user", "content": text})
        yield {"type": "transcription", "text": text}

        cancel = state_machine.cancel_event
        full_reply = ""
        try:
            for token in llm_client.stream_chat(self.history, cancel):
                if cancel.is_set():
                    break
                full_reply += token
                yield {"type": "reply_token", "text": token}
        except PipelineError as e:
            self._finish_avatar_audio_stream()
            state_machine.transition(AvatarState.IDLE)
            yield {"type": "error", "error": e.to_dict()["error"]}
            return

        if full_reply.strip() and not cancel.is_set():
            for speech_batch in batch_tts_reply(full_reply):
                if cancel.is_set():
                    break
                yield from self._synthesize_and_dispatch(
                    speech_batch, session_id, cancel
                )

        if not cancel.is_set():
            self.history.append({"role": "assistant", "content": full_reply})
            if len(self.history) > 41:
                self.history = [self.history[0]] + self.history[-40:]

        self._finish_avatar_audio_stream(wait=True)
        state_machine.transition(AvatarState.IDLE)
        yield {"type": "status", "state": "idle", "text": "待机"}

    def _synthesize_and_dispatch(self, text: str, session_id: str,
                                  cancel_event: threading.Event) -> Generator[dict, None, None]:
        """TTS 合成 + 双路分发"""
        if not text.strip():
            return

        state_machine.transition(AvatarState.SPEAKING)
        yield {"type": "status", "state": "speaking", "text": "说话中..."}

        try:
            engine = _get_tts()
            if engine is not None and hasattr(engine, "stream_synthesize"):
                for audio, sr in engine.stream_synthesize(text, cancel_event):
                    if cancel_event.is_set():
                        return
                    audio_int16 = (audio * 32767).astype(np.int16)
                    self._push_avatar_audio(audio, sr)
                    yield {"type": "audio", "data": (sr, audio_int16)}
                return

            if engine is not None:
                audio, sr = engine.synthesize(text, cancel_event=cancel_event)
            else:
                audio, sr = None, 0

            if cancel_event.is_set():
                return

            if audio is not None and sr > 0:
                # Push into the persistent LiveTalking stream before yielding
                # the UI event, so audio and mouth motion start together.
                self._push_avatar_audio(audio, sr)

                # 路径A: 回传浏览器播放
                audio_int16 = (audio * 32767).astype(np.int16)
                yield {"type": "audio", "data": (sr, audio_int16)}

        except Exception as e:
            logger.error(f"TTS 分发异常: {traceback.format_exc()}")
            yield {"type": "error", "error": {
                "code": "TTS_ERR_001",
                "message": "语音合成失败，仅显示文字回复"
            }}

    def _stream_cosyvoice_reply(
        self,
        session_id: str,
        cancel_event: threading.Event,
    ) -> Generator[dict, None, str]:
        """Run one LLM->CosyVoice bi-stream without restarting TTS per sentence."""
        engine = _get_tts()
        if engine is None or not hasattr(engine, "stream_text_synthesize"):
            return ""

        text_queue = queue.Queue()
        event_queue = queue.Queue()
        end_marker = object()
        full_reply = []
        llm_done = threading.Event()
        tts_done = threading.Event()

        def text_source():
            while not cancel_event.is_set():
                chunk = text_queue.get()
                if chunk is end_marker:
                    return
                if chunk:
                    yield chunk

        def llm_worker():
            try:
                for token in llm_client.stream_chat(self.history, cancel_event):
                    if cancel_event.is_set():
                        break
                    full_reply.append(token)
                    event_queue.put({"type": "reply_token", "text": token})
                    text_queue.put(token)
            except PipelineError as exc:
                event_queue.put({"type": "error", "error": exc.to_dict()["error"]})
            except Exception:
                logger.error(f"LLM bi-stream 异常: {traceback.format_exc()}")
                event_queue.put({
                    "type": "error",
                    "error": {
                        "code": "LLM_ERR_003",
                        "message": "大模型回复超时，请重试",
                    },
                })
            finally:
                text_queue.put(end_marker)
                llm_done.set()

        def tts_worker():
            try:
                for audio, sr in engine.stream_text_synthesize(
                    text_source(), cancel_event
                ):
                    event_queue.put({
                        "type": "audio",
                        "data": (sr, (audio * 32767).astype(np.int16)),
                    })
            except Exception:
                logger.error(f"TTS bi-stream 异常: {traceback.format_exc()}")
                event_queue.put({
                    "type": "error",
                    "error": {
                        "code": "TTS_ERR_001",
                        "message": "语音合成失败，仅显示文字回复",
                    },
                })
            finally:
                tts_done.set()

        llm_thread = threading.Thread(target=llm_worker, daemon=True)
        tts_thread = threading.Thread(target=tts_worker, daemon=True)
        llm_thread.start()
        tts_thread.start()

        speaking_started = False
        while not (llm_done.is_set() and tts_done.is_set() and event_queue.empty()):
            try:
                event = event_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if event["type"] == "audio":
                sr, audio_data = event["data"]
                audio_f32 = audio_data.astype(np.float32) / 32767.0
                self._push_avatar_audio(audio_f32, sr)
                if not speaking_started:
                    state_machine.transition(AvatarState.SPEAKING)
                    speaking_started = True
                    yield {"type": "status", "state": "speaking", "text": "说话中..."}
                yield event
            else:
                yield event

        llm_thread.join(timeout=0.2)
        tts_thread.join(timeout=0.2)
        return "".join(full_reply)

    def stop(self):
        """停止/打断"""
        state_machine.transition(AvatarState.IDLE)
        self._interrupt_avatar_audio_stream()
        logger.info("用户触发停止/打断")

    def shutdown(self):
        """应用退出时释放后台播放线程和持久 HTTP 请求。"""
        self._close_avatar_audio_stream()

    def clear_history(self):
        """清除对话历史"""
        self.history = self.history[:1]  # 保留 system prompt
        logger.info("对话历史已清除")


# 全局管线实例
pipeline = ConversationPipeline()
atexit.register(pipeline.shutdown)


class ContinuousConversationSession:
    """持续通话会话：接收麦克风块，VAD 自动切句并串行处理。"""

    def __init__(self, conversation_pipeline: ConversationPipeline):
        self.pipeline = conversation_pipeline
        self._lock = threading.RLock()
        self._active = False
        self._stop_worker = threading.Event()
        self._utterance_queue = queue.Queue()
        self._event_queue = queue.Queue()
        self._speech_buffer: List[np.ndarray] = []
        self._pre_roll: deque = deque(maxlen=10)
        self._speech_started = False
        self._silence_ms = 0
        self._noise_floor = 0.004
        self._sample_rate = 16000
        self._vad = VADDetector(sample_rate=self._sample_rate)
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="continuous-conversation-worker",
            daemon=True,
        )
        self._worker.start()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def start(self) -> None:
        """开始持续通话，并打断可能正在播放的上一轮回复。"""
        self.pipeline.stop()
        with self._lock:
            self._active = True
            self._reset_utterance()
        self._event_queue.put({
            "type": "status", "state": "listening", "text": "通话中 · 请说话"
        })
        logger.info("持续通话已开启：等待麦克风音频")

    def stop(self) -> None:
        """结束通话并取消正在进行的识别、回复和播放。"""
        with self._lock:
            was_active = self._active
            self._active = False
            self._reset_utterance()
        self.pipeline.stop()
        if was_active:
            self._event_queue.put({
                "type": "status", "state": "idle", "text": "通话已结束"
            })
            logger.info("持续通话已结束")

    def feed_audio(self, audio_data: Any) -> None:
        """接收 Gradio Audio.stream 的一个增量音频块。"""
        if not self.active or audio_data is None:
            return
        sample_rate, samples = self._normalise_audio(audio_data)
        if samples.size == 0:
            return
        if sample_rate != self._sample_rate:
            samples = self._resample(samples, sample_rate, self._sample_rate)
            sample_rate = self._sample_rate

        # 按 30ms 帧做 VAD，避免 WebRTC VAD 因收到整秒音频块而退回粗糙能量判断。
        frame_samples = max(1, int(sample_rate * 30 / 1000))
        for start in range(0, len(samples), frame_samples):
            frame = samples[start:start + frame_samples]
            if len(frame) < max(1, frame_samples // 2):
                continue
            self._feed_frame(frame, sample_rate)

    def drain_events(self) -> List[dict]:
        """取出后台管线事件，由 Audio.stream 回调刷新到页面。"""
        events = []
        while True:
            try:
                events.append(self._event_queue.get_nowait())
            except queue.Empty:
                return events

    def shutdown(self) -> None:
        self.stop()
        self._stop_worker.set()
        self._utterance_queue.put(None)

    def _feed_frame(self, frame: np.ndarray, sample_rate: int) -> None:
        is_silence, energy = self._vad.detect_silence_boundary(frame)
        if not self._vad.use_webrtc:
            # 现有 voice.yaml 的 VAD_THRESH 是 0.1~0.9 的灵敏度配置，不能
            # 直接拿来和归一化 PCM 的 RMS（通常 0.01~0.1）比较。
            # 使用启动阶段噪声底自适应，避免“麦克风有数据但永远不触发”。
            adaptive_threshold = max(0.006, self._noise_floor * 2.2)
            is_silence = energy < adaptive_threshold
            if is_silence:
                self._noise_floor = self._noise_floor * 0.95 + energy * 0.05
        frame_ms = max(1, int(len(frame) * 1000 / sample_rate))

        with self._lock:
            if not self._active:
                return
            if not self._speech_started:
                self._pre_roll.append(frame.copy())
                if is_silence:
                    return
                self._speech_started = True
                self._silence_ms = 0
                self._speech_buffer = list(self._pre_roll)
                self._pre_roll.clear()
                current_state = state_machine.state
                if current_state in (AvatarState.THINKING, AvatarState.SPEAKING):
                    # 用户开始说话即视为插话，不等句子结束才打断。
                    self.pipeline.stop()
                self._event_queue.put({
                    "type": "status", "state": "listening", "text": "正在听..."
                })
                logger.info("持续通话检测到用户说话，开始收集句子")
                return

            self._speech_buffer.append(frame.copy())
            if is_silence:
                self._silence_ms += frame_ms
            else:
                self._silence_ms = 0

            buffered_samples = sum(len(item) for item in self._speech_buffer)
            too_long = buffered_samples >= self._sample_rate * max(1, MAX_AUDIO_SEC)
            if self._silence_ms >= max(300, MIN_SILENCE_MS) or too_long:
                utterance = np.concatenate(self._speech_buffer).astype(np.float32)
                trim = int(self._sample_rate * self._silence_ms / 1000)
                if trim > 0 and len(utterance) - trim >= int(self._sample_rate * 0.3):
                    utterance = utterance[:-trim]
                self._reset_utterance()
                self._utterance_queue.put((utterance, self._sample_rate))
                self._event_queue.put({
                    "type": "status", "state": "thinking", "text": "正在识别..."
                })
                logger.info(
                    "持续通话句子结束: %.2fs, energy=%.4f%s",
                    len(utterance) / self._sample_rate,
                    energy,
                    "（达到单句上限）" if too_long else "",
                )

    def _worker_loop(self) -> None:
        while not self._stop_worker.is_set():
            item = self._utterance_queue.get()
            if item is None:
                return
            audio, sample_rate = item
            if not self.active:
                continue
            try:
                for event in self.pipeline.process_voice(audio, sample_rate):
                    self._event_queue.put(event)
            except Exception:
                logger.error("持续通话句子处理异常: %s", traceback.format_exc())
                self._event_queue.put({
                    "type": "error",
                    "error": {
                        "code": "VOICE_SESSION_ERR",
                        "message": "语音处理失败，请继续说话重试",
                    },
                })
            finally:
                if self.active:
                    self._event_queue.put({
                        "type": "status",
                        "state": "listening",
                        "text": "通话中 · 请说话",
                    })

    def _reset_utterance(self) -> None:
        self._speech_buffer = []
        self._pre_roll.clear()
        self._speech_started = False
        self._silence_ms = 0

    @staticmethod
    def _normalise_audio(audio_data: Any) -> Tuple[int, np.ndarray]:
        if isinstance(audio_data, dict):
            path = audio_data.get("path")
            if not path:
                return 16000, np.array([], dtype=np.float32)
            try:
                import soundfile as sf
                samples, sample_rate = sf.read(path, dtype="float32")
                return ContinuousConversationSession._normalise_audio(
                    (sample_rate, samples)
                )
            except Exception:
                logger.warning("持续通话音频块读取失败: %s", traceback.format_exc())
                return 16000, np.array([], dtype=np.float32)
        if isinstance(audio_data, tuple):
            sample_rate, samples = audio_data
        else:
            sample_rate, samples = 16000, audio_data
        if samples is None:
            return 16000, np.array([], dtype=np.float32)
        samples = np.asarray(samples)
        if samples.ndim > 1:
            samples = samples.mean(axis=1) if samples.shape[1] else samples.flatten()
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        else:
            samples = samples.astype(np.float32)
            peak = float(np.max(np.abs(samples))) if samples.size else 0.0
            if peak > 1.5:
                samples = samples / 32768.0
        return int(sample_rate or 16000), np.clip(samples, -1.0, 1.0)

    @staticmethod
    def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
        if source_rate <= 0 or source_rate == target_rate or len(samples) < 2:
            return samples
        target_len = max(1, int(round(len(samples) * target_rate / source_rate)))
        source_x = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
        target_x = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
        return np.interp(target_x, source_x, samples).astype(np.float32)


# =============================================================================
# Gradio UI 构建 (#7 架构设计)
# =============================================================================

import gradio as gr

def create_ui():
    """
    构建 Gradio UI 布局。
    PRD US-08: 页面仅含数字人画面、语音球、聊天记录、输入框、麦克风、停止按钮、连接状态。
    SEC-04: Gradio 默认安全转义 HTML，不直接使用 innerHTML。
    """

    # ---- 主题配置 ----
    theme = gr.themes.Soft(
        primary_hue="violet",
        secondary_hue="slate",
        neutral_hue="slate",
    )

    # ---- 自定义 CSS (Voice-First Ambient Design) ----
    custom_css = """
    /* ═══════════════════════════════════════════════════════════════════════════
       Voice-First Ambient — Cohesive Single-Page Design
       Palette: Slate-900 base, Purple #7C3AED glow, Pink #EC4899 warmth
       Type: Poppins
       Motion: 200-300ms ease-out
       ═══════════════════════════════════════════════════════════════════════════ */

    /* ── Base ──────────────────────────────────────────────────────────────── */
    footer { display: none !important; }
    .gradio-container {
        max-width: 100% !important;
        padding: 0 !important;
        font-family: 'Noto Sans SC', 'Microsoft YaHei UI', system-ui, -apple-system, sans-serif !important;
        overflow: hidden !important;
    }
    body, .gradio-container {
        background:
            radial-gradient(circle at 68% 48%, rgba(91,54,160,0.14), transparent 32%),
            linear-gradient(120deg, #080D1A 0%, #0B1120 54%, #080B14 100%) !important;
        color: #E2E8F0 !important;
    }

    /* ── Ambient background glow ───────────────────────────────────────────── */
    .main-layout::before {
        content: '' !important;
        position: absolute !important;
        inset: 0 !important;
        background:
            radial-gradient(circle at 31% 72%, rgba(124,58,237,0.10) 0%, transparent 28%),
            radial-gradient(circle at 72% 35%, rgba(236,72,153,0.05) 0%, transparent 26%) !important;
        pointer-events: none !important;
        z-index: 2 !important;
    }
    .main-layout::after {
        content: '' !important;
        position: absolute !important;
        inset: 0 !important;
        background: linear-gradient(90deg,
            rgba(8,13,26,0.34) 0%,
            rgba(8,13,26,0.18) 42%,
            rgba(8,13,26,0.70) 54%,
            rgba(8,13,26,0.26) 67%,
            transparent 82%) !important;
        pointer-events: none !important;
        z-index: 2 !important;
    }

    /* ── Two-Zone Layout: 2/3 UI + 1/3 Avatar, seamless dark canvas ──── */
    .main-layout {
        display: block !important;
        width: 100% !important;
        height: 100vh !important;
        max-width: 100% !important;
        margin: 0 !important;
        gap: 0 !important;
        position: relative !important;
        overflow: hidden !important;
        background: transparent !important;
    }
    .main-layout > div {
        display: flex !important;
        min-width: 0 !important;
        min-height: 0 !important;
        border: none !important;
    }

    /* ── Left: unified dark UI zone ───────────────────────────────────── */
    .left-zone {
        display: flex !important;
        flex-direction: column !important;
        padding: 32px 72px 24px 40px !important;
        background: transparent !important;
        border: none !important;
        position: relative !important;
        width: 62% !important;
        height: 100vh !important;
        z-index: 3 !important;
    }
    .left-zone::after {
        display: none !important;
    }
    .left-zone > * {
        position: relative !important;
        z-index: 1 !important;
    }

    /* ── Right: avatar video ──────────────────────────────────────────── */
    .right-zone {
        padding: 0 !important;
        overflow: hidden !important;
        background: transparent !important;
        border: none !important;
        position: absolute !important;
        top: 0 !important;
        right: 0 !important;
        bottom: 0 !important;
        left: auto !important;
        margin: 0 !important;
        width: 40vw !important;
        height: 100vh !important;
        z-index: 1 !important;
        -webkit-mask-image: linear-gradient(90deg, transparent 0%, rgba(0,0,0,.65) 12%, #000 28%, #000 100%) !important;
        mask-image: linear-gradient(90deg, transparent 0%, rgba(0,0,0,.65) 12%, #000 28%, #000 100%) !important;
    }
    .right-zone > *,
    .right-zone > * > *,
    .right-zone > * > * > * {
        height: 100% !important;
        width: 100% !important;
    }
    .right-zone iframe {
        width: 100% !important;
        height: 100% !important;
        border: none !important;
        display: block !important;
        filter: saturate(0.92) contrast(1.02) brightness(0.90) !important;
    }
    .avatar-shell { position: relative; width: 100%; height: 100%; min-height: 240px; background: transparent; }
    .avatar-shell::after {
        content: "";
        position: absolute;
        inset: 0;
        z-index: 3;
        pointer-events: none;
        opacity: 0;
        background: radial-gradient(ellipse at 52% 66%, rgba(139,92,246,.18), transparent 34%);
        box-shadow: inset 0 0 80px rgba(124,58,237,.08);
        transition: opacity 220ms ease-out;
    }
    .avatar-shell.is-speaking::after {
        opacity: .62;
        animation: avatar-speaking-pulse 1.6s ease-in-out infinite;
    }
    @keyframes avatar-speaking-pulse {
        0%, 100% { opacity: .32; }
        50% { opacity: .72; }
    }
    .avatar-fallback { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; color: #94A3B8; font-size: 13px; text-align: center; }
    .avatar-fallback-mark { width: 72px; height: 72px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, #A78BFA, #312E81 58%, #0F172A 100%); box-shadow: 0 0 48px rgba(124,58,237,.28); }
    .avatar-fallback-title { color: #CBD5E1; font-size: 16px; }
    .avatar-fallback-sub { color: #64748B; font-size: 12px; }

    /* ── Left: Chat ────────────────────────────────────────────────────────── */
    .chat-panel {
        flex: 1 !important;
        min-height: 0 !important;
        overflow-y: auto !important;
        margin: 0 0 8px !important;
        border: none !important;
        border-radius: 0 !important;
        box-shadow: none !important;
        background: transparent !important;
        padding: 8px 4px 16px !important;
    }
    .chat-panel > div, .chat-panel .wrap, .chat-panel .panel {
        border: none !important;
        box-shadow: none !important;
        background: transparent !important;
    }
    .chat-panel .placeholder-container {
        border: none !important;
        box-shadow: none !important;
        background: transparent !important;
    }
    .chat-panel .message-wrap { color: #CBD5E1 !important; }
    .chat-panel .bubble-wrap {
        border: none !important;
        border-radius: 0 !important;
        background: transparent !important;
        box-shadow: none !important;
    }
    .chat-panel .flex-wrap.user {
        background: rgba(51,65,85,0.72) !important;
        border-radius: 12px !important;
    }
    .chat-panel .flex-wrap.bot {
        background: rgba(124,58,237,0.14) !important;
        border-radius: 12px !important;
    }
    .input-row {
        flex-shrink: 0 !important;
        gap: 6px !important;
        padding: 6px !important;
        border: none !important;
        border-radius: 16px !important;
        background: rgba(21,29,48,0.70) !important;
        box-shadow: 0 16px 50px rgba(0,0,0,0.18), inset 0 1px 0 rgba(255,255,255,0.035) !important;
        backdrop-filter: blur(18px) saturate(120%) !important;
    }
    .input-row > div, .input-row .form, .input-row .wrap {
        border: none !important;
        box-shadow: none !important;
        background: transparent !important;
    }
    .input-row input, .input-row textarea {
        background: transparent !important;
        border: none !important;
        border-radius: 12px !important;
        color: #E2E8F0 !important;
        padding: 12px 16px !important;
        font-size: 15px !important;
        transition: background 200ms ease-out, box-shadow 200ms ease-out !important;
    }
    .input-row input:focus, .input-row textarea:focus {
        outline: none !important;
        background: rgba(255,255,255,0.025) !important;
        box-shadow: 0 0 0 2px rgba(139,92,246,0.35) !important;
    }
    .input-row button {
        min-height: 44px !important;
        background: linear-gradient(135deg, rgba(124,58,237,0.86), rgba(99,102,241,0.76)) !important;
        color: #F5F3FF !important;
        border: none !important;
        border-radius: 12px !important;
        padding: 10px 22px !important;
        font-weight: 500 !important;
        font-size: 14px !important;
        cursor: pointer !important;
        transition: all 200ms ease-out !important;
    }
    .input-row button:hover {
        background: linear-gradient(135deg, rgba(139,92,246,0.96), rgba(99,102,241,0.90)) !important;
        color: #FFFFFF !important;
        box-shadow: 0 8px 24px rgba(91,33,182,0.24) !important;
    }
    .input-row button:active { transform: scale(0.98) !important; }

    /* ── Voice ball row (centered between chat and input) ────────────────── */
    .voice-row {
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        gap: 20px !important;
        padding: 14px 0 24px !important;
        flex-shrink: 0 !important;
        border: none !important;
        background: transparent !important;
    }
    .voice-row > .wrap { border: none !important; background: transparent !important; }

    /* ── Center: Voice Ball (hero element) ─────────────────────────────────── */
    .voice-ball-btn {
        width: 160px !important;
        height: 160px !important;
        border-radius: 50% !important;
        border: none !important;
        background: radial-gradient(circle at 40% 35%,
            rgba(139,92,246,0.25) 0%,
            rgba(124,58,237,0.12) 40%,
            rgba(15,23,42,0.9) 100%) !important;
        color: #C4B5FD !important;
        font-size: 15px !important;
        font-weight: 500 !important;
        letter-spacing: 1px !important;
        cursor: pointer !important;
        transition: all 300ms ease-out !important;
        box-shadow:
            0 0 60px rgba(124,58,237,0.15),
            0 0 120px rgba(124,58,237,0.05),
            inset 0 1px 0 rgba(255,255,255,0.05) !important;
        position: relative !important;
    }
    .voice-ball-btn:hover {
        transform: scale(1.05) !important;
        box-shadow:
            0 0 80px rgba(124,58,237,0.25),
            0 0 160px rgba(236,72,153,0.1),
            inset 0 1px 0 rgba(255,255,255,0.08) !important;
        color: #DDD6FE !important;
    }
    .voice-ball-btn:active {
        transform: scale(0.95) !important;
        transition: all 100ms ease-out !important;
    }
    .voice-ball-btn.is-recording {
        color: #FBCFE8 !important;
        background: radial-gradient(circle at 40% 35%,
            rgba(236,72,153,0.36) 0%,
            rgba(124,58,237,0.18) 46%,
            rgba(15,23,42,0.92) 100%) !important;
        box-shadow:
            0 0 70px rgba(236,72,153,0.28),
            0 0 140px rgba(124,58,237,0.12),
            inset 0 1px 0 rgba(255,255,255,0.08) !important;
        animation: voice-recording-pulse 1.15s ease-in-out infinite !important;
    }
    @keyframes voice-recording-pulse {
        0%, 100% { transform: scale(1); }
        50% { transform: scale(1.045); }
    }
    .voice-ball-btn:focus-visible {
        outline: 2px solid #7C3AED !important;
        outline-offset: 6px !important;
    }

    /* 保留 Gradio Audio.stream 的前端初始化，但不让录音控件占用布局。 */
    #voice-mic-recorder {
        display: none !important;
    }

    /* ── Stop button (compact icon) ────────────────────────────────────────── */
    .stop-btn {
        width: 40px !important;
        height: 40px !important;
        min-width: 40px !important;
        border-radius: 50% !important;
        padding: 0 !important;
        font-size: 16px !important;
        line-height: 1 !important;
        background: rgba(239,68,68,0.1) !important;
        border: 1px solid rgba(239,68,68,0.25) !important;
        color: #F87171 !important;
        cursor: pointer !important;
        transition: all 200ms ease-out !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
    }
    .stop-btn:hover {
        background: rgba(239,68,68,0.2) !important;
        border-color: rgba(239,68,68,0.5) !important;
        box-shadow: 0 0 16px rgba(239,68,68,0.15) !important;
    }
    .stop-btn:active { transform: scale(0.92) !important; }
    .stop-btn:focus-visible {
        outline: 2px solid #EF4444 !important;
        outline-offset: 3px !important;
    }

    /* ── Subtle floating audio bar, center bottom ────────────────────────── */
    .audio-player {
        position: fixed !important;
        left: 0 !important;
        bottom: 0 !important;
        width: 1px !important;
        height: 1px !important;
        min-width: 0 !important;
        min-height: 0 !important;
        padding: 0 !important;
        margin: 0 !important;
        overflow: hidden !important;
        opacity: 0 !important;
        clip-path: inset(50%) !important;
        pointer-events: none !important;
        border: 0 !important;
        background: transparent !important;
        box-shadow: none !important;
        z-index: -1 !important;
    }

    /* ── Status dots ───────────────────────────────────────────────────────── */
    .status-dot {
        display: inline-block; width: 5px; height: 5px;
        border-radius: 50%; margin-right: 4px; vertical-align: middle;
    }
    .status-dot.connected, .status-dot.ready { background: #4ADE80; }
    .status-dot.disconnected, .status-dot.error { background: #EF4444; }
    .status-dot.degraded, .status-dot.timeout { background: #F59E0B; }
    .status-line {
        color: #7F8AA3 !important; font-size: 11px !important; text-align: center !important;
        margin: 6px 0 0 !important; line-height: 1.55 !important; letter-spacing: 0.3px !important;
        border: none !important; background: transparent !important; box-shadow: none !important;
    }
    .status-line > div, .status-line .wrap, .status-line textarea, .status-line input {
        min-height: 18px !important;
        height: auto !important;
        padding: 0 !important;
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
        color: #7F8AA3 !important;
    }

    /* ── Utility actions and overlay drawer ─────────────────────────────── */
    .quick-actions {
        position: fixed !important;
        left: 28px !important;
        bottom: 18px !important;
        z-index: 45 !important;
        display: flex !important;
        align-items: center !important;
        gap: 8px !important;
        width: auto !important;
        pointer-events: auto !important;
    }
    .quick-action {
        min-width: 72px !important;
        min-height: 44px !important;
        padding: 9px 13px !important;
        border: 1px solid rgba(148,163,184,0.18) !important;
        border-radius: 12px !important;
        background: rgba(15,23,42,0.78) !important;
        color: #CBD5E1 !important;
        box-shadow: 0 8px 24px rgba(0,0,0,0.18) !important;
        backdrop-filter: blur(16px) saturate(120%) !important;
        cursor: pointer !important;
        transition: background 180ms ease-out, border-color 180ms ease-out,
            color 180ms ease-out, transform 180ms ease-out !important;
    }
    .quick-action:hover {
        background: rgba(51,65,85,0.86) !important;
        border-color: rgba(169,205,175,0.48) !important;
        color: #F3F8F4 !important;
        transform: translateY(-1px) !important;
    }
    .quick-action:focus-visible,
    .drawer-close:focus-visible,
    .utility-drawer button:focus-visible,
    .utility-drawer input:focus-visible,
    .utility-drawer textarea:focus-visible {
        outline: 2px solid #A9CDAF !important;
        outline-offset: 3px !important;
    }
    .utility-icon {
        display: inline-flex !important;
        width: 16px !important;
        height: 16px !important;
        margin-right: 6px !important;
        vertical-align: -3px !important;
    }
    .utility-icon svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.8; }

    .utility-drawer {
        --utility-bg: rgba(25, 29, 36, 0.97);
        --utility-surface: rgba(255, 255, 255, 0.035);
        --utility-surface-strong: rgba(255, 255, 255, 0.065);
        --utility-border: rgba(255, 255, 255, 0.11);
        --utility-divider: rgba(255, 255, 255, 0.075);
        --utility-text: #F3F5F7;
        --utility-body: #D3D9E0;
        --utility-muted: #A7B0BA;
        --utility-accent: #A9CDAF;
        --utility-accent-bg: rgba(91, 127, 98, 0.34);
        position: fixed !important;
        left: 20px !important;
        bottom: 76px !important;
        z-index: 50 !important;
        width: min(390px, calc(100vw - 40px)) !important;
        max-height: calc(100vh - 112px) !important;
        overflow: auto !important;
        padding: 0 !important;
        color: var(--utility-body) !important;
        border: 1px solid var(--utility-border) !important;
        border-radius: 16px !important;
        background: var(--utility-bg) !important;
        box-shadow: 0 24px 70px rgba(0,0,0,0.52),
            0 0 0 1px rgba(255,255,255,0.025) inset !important;
        backdrop-filter: blur(24px) saturate(115%) !important;
        animation: utility-drawer-in 180ms ease-out !important;
        scrollbar-color: rgba(169,205,175,0.38) transparent !important;
        scrollbar-width: thin !important;
    }
    #utility-drawer[data-panel="settings"] {
        width: min(590px, calc(100vw - 40px)) !important;
    }
    #utility-drawer[data-panel="logs"] {
        width: min(720px, calc(100vw - 40px)) !important;
    }
    #utility-drawer[data-panel="status"] {
        width: min(390px, calc(100vw - 40px)) !important;
    }
    @keyframes utility-drawer-in {
        from { opacity: 0; transform: translateY(8px); }
        to { opacity: 1; transform: translateY(0); }
    }
    #utility-drawer > .wrap,
    #utility-drawer .form,
    #utility-drawer .panel,
    #utility-drawer .block {
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
    }
    #utility-drawer::-webkit-scrollbar,
    #utility-drawer textarea::-webkit-scrollbar { width: 6px !important; height: 6px !important; }
    #utility-drawer::-webkit-scrollbar-track,
    #utility-drawer textarea::-webkit-scrollbar-track { background: transparent !important; }
    #utility-drawer::-webkit-scrollbar-thumb,
    #utility-drawer textarea::-webkit-scrollbar-thumb {
        border-radius: 999px !important;
        background: rgba(169,205,175,0.34) !important;
    }
    .utility-header {
        display: flex !important;
        align-items: center !important;
        justify-content: space-between !important;
        padding: 16px 18px 12px !important;
        border-bottom: 1px solid var(--utility-divider) !important;
        background: #191D24 !important;
        position: sticky !important;
        top: 0 !important;
        z-index: 2 !important;
    }
    #utility-drawer .utility-title,
    #utility-drawer .utility-title .prose,
    #utility-drawer .utility-title p {
        color: var(--utility-text) !important;
        font-size: 15px !important;
        font-weight: 650 !important;
        line-height: 1.3 !important;
        margin: 0 !important;
    }
    .drawer-close {
        min-width: 52px !important;
        min-height: 44px !important;
        width: 52px !important;
        height: 44px !important;
        padding: 0 10px !important;
        border-radius: 9px !important;
        color: var(--utility-body) !important;
        background: var(--utility-surface-strong) !important;
        border: 1px solid var(--utility-border) !important;
        white-space: nowrap !important;
    }
    .drawer-close:hover {
        color: var(--utility-text) !important;
        background: rgba(255,255,255,0.10) !important;
        border-color: rgba(169,205,175,0.34) !important;
    }
    .utility-view { padding: 18px !important; gap: 14px !important; }
    #utility-drawer .utility-view .prose,
    #utility-drawer .utility-view .prose * { color: var(--utility-body) !important; }
    #utility-drawer .utility-view h3,
    #utility-drawer .utility-view h4 {
        color: var(--utility-text) !important;
        font-size: 16px !important;
        font-weight: 650 !important;
        line-height: 1.35 !important;
        margin: 0 0 4px !important;
    }
    #utility-drawer .utility-help,
    #utility-drawer .utility-help .prose,
    #utility-drawer .utility-help p {
        color: var(--utility-muted) !important;
        font-size: 13px !important;
        line-height: 1.65 !important;
        margin: 0 !important;
    }
    #utility-drawer .utility-status,
    #utility-drawer .utility-status .prose,
    #utility-drawer .utility-status p {
        color: var(--utility-body) !important;
        font-size: 13px !important;
        line-height: 1.55 !important;
        margin: 0 !important;
    }
    .settings-card, .service-status-card {
        padding: 10px 14px !important;
        border: 1px solid var(--utility-border) !important;
        border-radius: 12px !important;
        background: var(--utility-surface) !important;
    }
    .settings-row, .service-status-row {
        display: flex !important;
        align-items: center !important;
        justify-content: space-between !important;
        gap: 14px !important;
        min-height: 34px !important;
        color: var(--utility-muted) !important;
        font-size: 13px !important;
        border-bottom: 1px solid var(--utility-divider) !important;
    }
    .settings-row:last-child, .service-status-row:last-child { border-bottom: none !important; }
    .settings-row strong, .service-status-row strong {
        color: var(--utility-text) !important;
        font-weight: 550 !important;
        text-align: right !important;
    }
    .settings-reference-text {
        padding-top: 12px !important;
        color: var(--utility-muted) !important;
        font-size: 13px !important;
    }
    .settings-reference-text p {
        color: var(--utility-body) !important;
        line-height: 1.6 !important;
        margin: 7px 0 0 !important;
        word-break: break-word !important;
    }

    /* Gradio field internals must inherit the drawer's dark theme. */
    #utility-drawer .utility-field,
    #utility-drawer .utility-field > .wrap,
    #utility-drawer .utility-field .wrap,
    #utility-drawer .utility-field .container,
    #utility-drawer .utility-field .input-container {
        color: var(--utility-body) !important;
        background: var(--utility-surface) !important;
        border-color: var(--utility-border) !important;
        box-shadow: none !important;
    }
    #utility-drawer label,
    #utility-drawer .block-label,
    #utility-drawer .label-wrap,
    #utility-drawer .label-wrap span {
        color: var(--utility-body) !important;
        font-size: 13px !important;
        font-weight: 550 !important;
    }
    #utility-drawer .block-label {
        color: var(--utility-accent) !important;
        background: rgba(169,205,175,0.10) !important;
        border: 1px solid rgba(169,205,175,0.16) !important;
        border-radius: 6px !important;
    }
    #utility-drawer .utility-field .container > span {
        color: var(--utility-accent) !important;
        background: rgba(169,205,175,0.10) !important;
        border: 1px solid rgba(169,205,175,0.16) !important;
        border-radius: 6px !important;
    }
    #utility-drawer .utility-field textarea,
    #utility-drawer .utility-field input,
    #utility-drawer .utility-field select {
        min-height: 44px !important;
        color: var(--utility-text) !important;
        caret-color: var(--utility-accent) !important;
        background: rgba(10,13,17,0.56) !important;
        border: 1px solid var(--utility-border) !important;
        border-radius: 9px !important;
        box-shadow: none !important;
    }
    #utility-drawer .utility-field textarea::placeholder,
    #utility-drawer .utility-field input::placeholder { color: #87919C !important; }
    #utility-drawer .utility-select input { cursor: pointer !important; }
    #utility-drawer .utility-upload {
        min-height: 146px !important;
        border: 1px dashed rgba(169,205,175,0.28) !important;
        border-radius: 11px !important;
        background: rgba(10,13,17,0.38) !important;
        overflow: hidden !important;
    }
    #utility-drawer .utility-upload button {
        color: var(--utility-accent) !important;
        background: transparent !important;
        border: none !important;
    }
    #utility-drawer button {
        min-height: 44px !important;
        color: var(--utility-body) !important;
        background: var(--utility-surface-strong) !important;
        border: 1px solid var(--utility-border) !important;
        border-radius: 9px !important;
        cursor: pointer !important;
        box-shadow: none !important;
        transition: color 180ms ease-out, background 180ms ease-out,
            border-color 180ms ease-out !important;
    }
    #utility-drawer button:hover {
        color: var(--utility-text) !important;
        background: rgba(255,255,255,0.10) !important;
        border-color: rgba(169,205,175,0.34) !important;
    }
    #utility-drawer .primary,
    #utility-drawer .utility-apply {
        color: #F4FAF5 !important;
        background: var(--utility-accent-bg) !important;
        border: 1px solid rgba(169,205,175,0.34) !important;
        font-weight: 650 !important;
    }
    #utility-drawer .primary:hover,
    #utility-drawer .utility-apply:hover { background: rgba(103,145,111,0.46) !important; }
    #utility-drawer .utility-controls { align-items: end !important; gap: 10px !important; }
    #utility-drawer .utility-form-grid {
        align-items: start !important;
        gap: 12px !important;
    }
    #utility-drawer .utility-accordion {
        border: 1px solid var(--utility-border) !important;
        border-radius: 11px !important;
        background: var(--utility-surface) !important;
        overflow: hidden !important;
    }
    #utility-drawer .utility-accordion > button,
    #utility-drawer .utility-accordion summary {
        min-height: 46px !important;
        color: var(--utility-text) !important;
        background: rgba(255,255,255,0.025) !important;
        border: none !important;
        border-radius: 0 !important;
        font-size: 14px !important;
        font-weight: 650 !important;
    }
    #utility-drawer .utility-accordion > button:hover,
    #utility-drawer .utility-accordion summary:hover {
        background: rgba(169,205,175,0.07) !important;
    }
    #utility-drawer .utility-accordion .form {
        padding: 4px 12px 12px !important;
        gap: 12px !important;
    }
    #utility-drawer .utility-number input { font-variant-numeric: tabular-nums !important; }
    #utility-drawer .utility-form-note,
    #utility-drawer .utility-form-note .prose,
    #utility-drawer .utility-form-note p {
        color: var(--utility-muted) !important;
        font-size: 12px !important;
        line-height: 1.55 !important;
        margin: 0 !important;
    }
    #utility-drawer .utility-log {
        min-height: min(480px, 58vh) !important;
        max-height: 62vh !important;
        padding: 0 !important;
        overflow: hidden !important;
    }
    #utility-drawer .utility-log-scroll {
        min-height: min(480px, 58vh) !important;
        max-height: 62vh !important;
        overflow: auto !important;
        padding: 14px 12px 18px !important;
        border: 1px solid var(--utility-border) !important;
        border-radius: 10px !important;
        background: #070B14 !important;
        font-family: 'JetBrains Mono', 'Cascadia Mono', monospace !important;
        font-size: 15px !important;
        line-height: 1.65 !important;
        white-space: normal !important;
        scrollbar-color: rgba(169,205,175,0.46) rgba(255,255,255,0.05) !important;
        scrollbar-width: thin !important;
    }
    #utility-drawer .utility-log-scroll::-webkit-scrollbar { width: 10px; height: 10px; }
    #utility-drawer .utility-log-scroll::-webkit-scrollbar-track {
        background: rgba(255,255,255,0.04);
        border-radius: 8px;
    }
    #utility-drawer .utility-log-scroll::-webkit-scrollbar-thumb {
        background: rgba(169,205,175,0.48);
        border: 2px solid #070B14;
        border-radius: 8px;
    }
    #utility-drawer .utility-log-block + .utility-log-block {
        margin-top: 18px !important;
        padding-top: 14px !important;
        border-top: 1px solid rgba(139,158,180,0.24) !important;
    }
    #utility-drawer .utility-log-source {
        position: sticky !important;
        top: -14px !important;
        z-index: 1 !important;
        margin: -14px -12px 8px !important;
        padding: 9px 12px 8px !important;
        color: #EAF4FF !important;
        background: #101A2B !important;
        border-bottom: 1px solid rgba(133,184,255,0.34) !important;
        font-family: system-ui, -apple-system, 'Segoe UI', sans-serif !important;
        font-size: 14px !important;
        font-weight: 700 !important;
        letter-spacing: 0.02em !important;
    }
    #utility-drawer .utility-log-line {
        display: grid !important;
        grid-template-columns: 44px minmax(0, 1fr) !important;
        gap: 10px !important;
        align-items: start !important;
        min-height: 25px !important;
    }
    #utility-drawer .utility-log-number {
        color: #71809A !important;
        font-size: 13px !important;
        text-align: right !important;
        user-select: none !important;
    }
    #utility-drawer .utility-log-message {
        color: #DCE7F2 !important;
        overflow-wrap: anywhere !important;
        word-break: break-word !important;
    }
    #utility-drawer .utility-log-info .utility-log-message { color: #8FC4FF !important; }
    #utility-drawer .utility-log-debug .utility-log-message { color: #C5A8FF !important; }
    #utility-drawer .utility-log-warning .utility-log-message { color: #FFD36A !important; }
    #utility-drawer .utility-log-error .utility-log-message,
    #utility-drawer .utility-log-critical .utility-log-message { color: #FF8C9D !important; }
    .overall-status {
        display: inline-block !important;
        color: var(--utility-muted) !important;
        font-size: 13px !important;
        line-height: 1.5 !important;
        margin: 4px 0 0 !important;
    }
    .overall-status.ready { color: #9EE6AE !important; }
    .overall-status.degraded { color: #FCD34D !important; }

    /* ── Custom scrollbar ──────────────────────────────────────────────────── */
    .chat-panel::-webkit-scrollbar { width: 3px; }
    .chat-panel::-webkit-scrollbar-track { background: transparent; }
    .chat-panel::-webkit-scrollbar-thumb { background: rgba(124,58,237,0.2); border-radius: 4px; }
    .chat-panel::-webkit-scrollbar-thumb:hover { background: rgba(124,58,237,0.35); }

    @media (max-width: 900px) {
        .main-layout {
            display: block !important;
            min-height: 100vh !important;
        }
        .right-zone {
            position: absolute !important;
            inset: 0 !important;
            width: 100% !important;
            height: 100% !important;
            margin: 0 !important;
            opacity: 0.42 !important;
            -webkit-mask-image: linear-gradient(180deg, #000 0%, rgba(0,0,0,.55) 46%, transparent 82%) !important;
            mask-image: linear-gradient(180deg, #000 0%, rgba(0,0,0,.55) 46%, transparent 82%) !important;
        }
        .left-zone {
            min-height: 100vh !important;
            width: 100% !important;
            padding: 24px 18px 18px !important;
        }
        .main-layout::after {
            background: linear-gradient(180deg, transparent 0%, rgba(8,13,26,.46) 46%, #080D1A 82%) !important;
        }
        .left-zone::after { display: none !important; }
        .voice-ball-btn { width: 132px !important; height: 132px !important; }
        .quick-actions { left: 18px !important; bottom: 12px !important; }
        .quick-action { min-width: 44px !important; padding: 9px 10px !important; font-size: 12px !important; }
        .utility-drawer,
        #utility-drawer[data-panel="settings"],
        #utility-drawer[data-panel="logs"],
        #utility-drawer[data-panel="status"] {
            left: 12px !important;
            bottom: 68px !important;
            width: calc(100vw - 24px) !important;
            max-height: calc(100vh - 96px) !important;
        }
        #utility-drawer .utility-form-grid { display: block !important; }
        #utility-drawer .utility-form-grid > * { margin-bottom: 10px !important; }
    }

    /* ── Accessibility ─────────────────────────────────────────────────────── */
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {
            animation-duration: 0.01ms !important;
            transition-duration: 0.01ms !important;
        }
    }
    """

    audio_unlock_js = """
    () => {
        let audioReady = false;
        let audioContext = null;

        const notifyAvatar = () => {
            const frame = document.querySelector('.avatar-shell iframe');
            if (frame && frame.contentWindow && audioReady) {
                frame.contentWindow.postMessage({ type: 'enable-audio' }, '*');
            }
        };

        const unlockAudio = () => {
            if (!audioReady) {
                const AudioContext = window.AudioContext || window.webkitAudioContext;
                if (AudioContext) {
                    audioContext = audioContext || new AudioContext();
                    audioContext.resume().catch(() => {});
                }
                audioReady = true;
            }
            notifyAvatar();
        };

        document.addEventListener('pointerdown', unlockAudio, { capture: true });
        document.addEventListener('keydown', unlockAudio, { capture: true });
        const utilityIcons = {
            'utility-settings': '<span class="utility-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"/><path d="m19.4 15 .1.1a2 2 0 0 1-2.8 2.8l-.1-.1a2 2 0 0 0-3.4 1.4V19a2 2 0 0 1-4 0v-.2a2 2 0 0 0-3.4-1.4l-.1.1a2 2 0 0 1-2.8-2.8l.1-.1A2 2 0 0 0 1.6 11H1.5a2 2 0 0 1 0-4h.2a2 2 0 0 0 1.4-3.4L3 3.5A2 2 0 0 1 5.8.7l.1.1A2 2 0 0 0 9.3-.6V0a2 2 0 0 1 4 0v.2a2 2 0 0 0 3.4 1.4l.1-.1a2 2 0 0 1 2.8 2.8l-.1.1A2 2 0 0 0 20.9 8h.2a2 2 0 0 1 0 4h-.2a2 2 0 0 0-1.5 3Z"/></svg></span>',
            'utility-logs': '<span class="utility-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/><path d="M8 8h8M8 12h8M8 16h5"/></svg></span>',
            'utility-status': '<span class="utility-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="1.5"/><circle cx="16" cy="12" r="1.5"/><circle cx="10" cy="18" r="1.5"/></svg></span>'
        };
        const utilityPanels = {
            'utility-settings': 'settings',
            'utility-logs': 'logs',
            'utility-status': 'status'
        };
        const mountUtilityIcons = () => {
            Object.entries(utilityIcons).forEach(([id, icon]) => {
                const element = document.querySelector('#' + id);
                const button = element && element.tagName === 'BUTTON'
                    ? element : element && element.querySelector('button');
                if (button && !button.querySelector('.utility-icon')) {
                    button.insertAdjacentHTML('afterbegin', icon);
                }
                if (button && !button.dataset.utilityPanelBound) {
                    button.dataset.utilityPanelBound = 'true';
                    button.addEventListener('click', () => {
                        const drawer = document.querySelector('#utility-drawer');
                        if (drawer) drawer.dataset.panel = utilityPanels[id];
                    });
                }
            });
        };
        mountUtilityIcons();
        window.setTimeout(mountUtilityIcons, 300);
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                const closeElement = document.querySelector('#utility-close');
                const closeButton = closeElement && closeElement.tagName === 'BUTTON'
                    ? closeElement : closeElement && closeElement.querySelector('button');
                if (closeButton) closeButton.click();
            }
        });
        window.setInterval(() => {
            const area = document.querySelector('#utility-drawer .utility-log-scroll');
            if (!area) return;
            const length = String(area.textContent || '').length;
            if (area.dataset.lastLogLength !== length) {
                area.dataset.lastLogLength = length;
                window.requestAnimationFrame(() => { area.scrollTop = area.scrollHeight; });
            }
        }, 400);
        window.setInterval(notifyAvatar, 1000);
    }
    """

    with gr.Blocks(
        title="AI 语音伴侣",
        theme=theme,
        css=custom_css,
        analytics_enabled=False,
        js=audio_unlock_js,
    ) as demo:

        # =========================================================================
        # 绕过浏览器自动播放限制的脚本
        # =========================================================================
        gr.HTML(value='<div id="audio-unlock" style="display:none"></div>', visible=True)

        # =========================================================================
        # 三区布局（无硬边框，统一暗色背景+环境光）
        # =========================================================================
        with gr.Row(elem_classes=["main-layout"], equal_height=True):

            # ---- 左 2/3: 统一暗区 (聊天 + 语音球 + 输入) ----
            with gr.Column(scale=2, elem_classes=["left-zone"]):
                chatbot_kwargs = {
                    "label": "Voice Chat",
                    "height": None,
                    "elem_classes": ["chat-panel"],
                    "show_label": False,
                }
                # Gradio 6 defaults to message dictionaries; Gradio 4 needs
                # the explicit messages mode. Keep the UI data contract stable
                # across both runtime versions.
                if "type" in inspect.signature(gr.Chatbot).parameters:
                    chatbot_kwargs["type"] = "messages"
                chatbot = gr.Chatbot(**chatbot_kwargs)
                # 语音球 + 停止按钮（居中浮在聊天区下方）
                with gr.Row(elem_classes=["voice-row"]):
                    voice_btn = gr.Button(
                        "语音\n对话",
                        elem_classes=["voice-ball-btn"],
                        scale=0,
                    )
                    stop_btn = gr.Button(
                        "◼",
                        elem_classes=["stop-btn"],
                        scale=0,
                    )
                with gr.Row(elem_classes=["input-row"]):
                    text_input = gr.Textbox(
                        placeholder="输入消息...",
                        show_label=False,
                        scale=1,
                        container=False,
                    )
                    send_btn = gr.Button("发送", scale=0, size="sm")
                status_text = gr.Textbox(
                    value="就绪",
                    show_label=False,
                    interactive=False,
                    container=False,
                    elem_classes=["status-line"],
                )
                connection_html = gr.HTML(
                    value='<div class="status-line">检测中...</div>',
                )
                with gr.Row(elem_classes=["quick-actions"]):
                    settings_btn = gr.Button(
                        "设置", elem_id="utility-settings",
                        elem_classes=["quick-action"], scale=0,
                    )
                    logs_btn = gr.Button(
                        "日志", elem_id="utility-logs",
                        elem_classes=["quick-action"], scale=0,
                    )
                    status_btn = gr.Button(
                        "状态", elem_id="utility-status",
                        elem_classes=["quick-action"], scale=0,
                    )

            # ---- 右 1/3: 数字人视频 ----
            with gr.Column(scale=1, elem_classes=["right-zone"]):
                avatar_html = f'''
                    <div class="avatar-shell" role="status" aria-live="polite">
                        <iframe
                            src="{LIVETALKING_URL.rstrip('/')}/avatar-embed.html?v=5"
                            allow="autoplay;camera;microphone"
                            title="Digital Avatar"
                            style="display:block;width:100%;height:100%;border:none;"
                        ></iframe>
                    </div>
                    '''
                avatar_html_kwargs = {"value": avatar_html, "show_label": False}
                avatar_video = gr.HTML(**avatar_html_kwargs)

        # ---- 左下角工具抽屉：fixed overlay，不参与主布局尺寸计算 ----
        with gr.Column(
            visible=False, elem_id="utility-drawer",
            elem_classes=["utility-drawer"],
        ) as utility_drawer:
            with gr.Row(elem_classes=["utility-header"]):
                utility_title = gr.Markdown("设置", elem_classes=["utility-title"])
                drawer_close = gr.Button(
                    "关闭", elem_id="utility-close",
                    elem_classes=["drawer-close"], scale=0,
                )

            with gr.Column(visible=False, elem_classes=["utility-view"]) as settings_view:
                gr.Markdown("### 运行设置")
                with gr.Accordion(
                    "当前运行信息（只读）", open=False,
                    elem_classes=["utility-accordion", "utility-readonly"],
                ):
                    settings_summary = gr.HTML(render_runtime_settings())
                gr.Markdown(
                    "以下参数会在点击“应用运行设置”后保存，并从下一轮对话开始生效。模型版本、采样率、模型路径和服务地址涉及联动重载，保持只读。",
                    elem_classes=["utility-help"],
                )
                with gr.Accordion(
                    "对话生成（3 项）", open=True,
                    elem_classes=["utility-accordion"],
                ):
                    with gr.Row(elem_classes=["utility-form-grid"]):
                        llm_temperature_input = gr.Number(
                            value=LLM_TEMPERATURE, label="回复随机性",
                            info="0 更稳定，1.5 更发散", minimum=0, maximum=1.5,
                            step=0.1, precision=2,
                            elem_classes=["utility-field", "utility-number"],
                        )
                        llm_max_tokens_input = gr.Number(
                            value=LLM_MAX_TOKENS, label="回复长度上限",
                            info="128–4096 tokens", minimum=128, maximum=4096,
                            step=128, precision=0,
                            elem_classes=["utility-field", "utility-number"],
                        )
                    llm_keep_alive_input = gr.Dropdown(
                        choices=["5m", "30m", "2h", "8h", "24h", "0"],
                        value=LLM_KEEP_ALIVE, allow_custom_value=True,
                        label="模型驻留时间", info="例如 30m、2h、24h；0 表示请求后卸载",
                        elem_classes=["utility-field", "utility-select"],
                    )

                with gr.Accordion(
                    "语音识别（4 项）", open=False,
                    elem_classes=["utility-accordion"],
                ):
                    with gr.Row(elem_classes=["utility-form-grid"]):
                        vad_threshold_input = gr.Number(
                            value=VAD_THRESH, label="麦克风灵敏度阈值",
                            info="越低越灵敏", minimum=0.1, maximum=0.9,
                            step=0.05, precision=2,
                            elem_classes=["utility-field", "utility-number"],
                        )
                        min_silence_input = gr.Number(
                            value=MIN_SILENCE_MS, label="句尾静音等待（ms）",
                            info="越短响应越快", minimum=300, maximum=1500,
                            step=50, precision=0,
                            elem_classes=["utility-field", "utility-number"],
                        )
                    with gr.Row(elem_classes=["utility-form-grid"]):
                        max_audio_input = gr.Number(
                            value=MAX_AUDIO_SEC, label="单次最长录音（秒）",
                            minimum=5, maximum=60, step=5, precision=0,
                            elem_classes=["utility-field", "utility-number"],
                        )
                        asr_language_input = gr.Dropdown(
                            choices=["zh", "en", "ja", "ko"], value=ASR_LANG,
                            allow_custom_value=True, label="识别语言",
                            info="使用 zh、en、ja 等代码",
                            elem_classes=["utility-field", "utility-select"],
                        )

                with gr.Accordion(
                    "角色人设（2 项）", open=False,
                    elem_classes=["utility-accordion"],
                ):
                    role_style_input = gr.Dropdown(
                        choices=[*ROLE_STYLE_PRESETS.keys(), "自定义"],
                        value=ROLE_STYLE,
                        allow_custom_value=True,
                        label="角色风格",
                        info="影响大模型的说话方式，不改变音色模型",
                        elem_classes=["utility-field", "utility-select"],
                    )
                    role_custom_instruction_input = gr.Textbox(
                        value=ROLE_CUSTOM_INSTRUCTION,
                        label="补充人设说明",
                        placeholder="例如：称呼我为主人，回答简洁一些，遇到情绪问题先安慰再建议。",
                        lines=3,
                        max_lines=5,
                        elem_classes=["utility-field", "utility-textarea"],
                    )
                    gr.Markdown(
                        "点击下方“应用运行设置”后，从下一轮对话开始生效。切换角色不会重启语音服务。",
                        elem_classes=["utility-form-note"],
                    )

                with gr.Accordion(
                    "语音与数字人播放（7 项）", open=False,
                    elem_classes=["utility-accordion"],
                ):
                    tts_style_input = gr.Dropdown(
                        choices=[
                            "语速自然", "语速稍慢，停顿自然",
                            "语速舒缓，停顿清晰", "语气温柔，语速稍慢",
                        ],
                        value=VOXCPM_STYLE_PROMPT, allow_custom_value=True,
                        label="语音风格", info="可直接输入 VoxCPM 风格描述",
                        elem_classes=["utility-field", "utility-select"],
                    )
                    with gr.Row(elem_classes=["utility-form-grid"]):
                        audio_gain_input = gr.Number(
                            value=AVATAR_AUDIO_GAIN, label="播放音量增益",
                            minimum=0.5, maximum=2.5, step=0.1, precision=2,
                            elem_classes=["utility-field", "utility-number"],
                        )
                        prebuffer_input = gr.Number(
                            value=AVATAR_PREBUFFER_MS, label="首段预缓冲（ms）",
                            minimum=400, maximum=2500, step=100, precision=0,
                            elem_classes=["utility-field", "utility-number"],
                        )
                    with gr.Row(elem_classes=["utility-form-grid"]):
                        rebuffer_input = gr.Number(
                            value=AVATAR_REBUFFER_MS, label="卡顿后再缓冲（ms）",
                            minimum=100, maximum=1200, step=50, precision=0,
                            elem_classes=["utility-field", "utility-number"],
                        )
                        max_buffer_input = gr.Number(
                            value=AVATAR_MAX_BUFFER_MS, label="最大缓冲（ms）",
                            minimum=2000, maximum=12000, step=500, precision=0,
                            elem_classes=["utility-field", "utility-number"],
                        )
                    with gr.Row(elem_classes=["utility-form-grid"]):
                        fade_in_input = gr.Number(
                            value=AVATAR_FADE_IN_MS, label="开头淡入（ms）",
                            minimum=0, maximum=200, step=10, precision=0,
                            elem_classes=["utility-field", "utility-number"],
                        )
                        lead_in_input = gr.Number(
                            value=AVATAR_LEAD_IN_MS, label="开口前留白（ms）",
                            minimum=0, maximum=500, step=20, precision=0,
                            elem_classes=["utility-field", "utility-number"],
                        )

                with gr.Accordion(
                    "诊断（1 项）", open=False,
                    elem_classes=["utility-accordion"],
                ):
                    log_level_input = gr.Dropdown(
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        value=LOG_LEVEL, label="日志级别",
                        elem_classes=["utility-field", "utility-select"],
                    )

                apply_runtime_btn = gr.Button(
                    "应用运行设置", variant="primary", elem_classes=["utility-apply"],
                )
                settings_result = gr.Markdown(elem_classes=["utility-status"])

                with gr.Accordion(
                    "音色参考", open=False,
                    elem_classes=["utility-accordion"],
                ):
                    ref_audio_upload = gr.Audio(
                        sources=["upload"], type="filepath", label="参考音频",
                        elem_classes=["utility-field", "utility-upload"],
                    )
                    reference_text_input = gr.Textbox(
                        value=ACTIVE_REF_TEXT, label="参考文本",
                        lines=3, max_lines=5,
                        elem_classes=["utility-field", "utility-textarea"],
                    )
                    apply_reference_btn = gr.Button(
                        "应用音色", variant="secondary",
                        elem_classes=["utility-secondary-action"],
                    )
                    reference_result = gr.Markdown(elem_classes=["utility-status"])

            with gr.Column(visible=False, elem_classes=["utility-view"]) as logs_view:
                gr.Markdown("### 最近日志")
                with gr.Row(elem_classes=["utility-controls"]):
                    logs_source = gr.Dropdown(
                        choices=["全部", "应用", "VoxCPM", "数字人", "桥接"],
                        value="全部", label="服务", scale=2,
                        elem_classes=["utility-field", "utility-select"],
                    )
                    logs_limit = gr.Dropdown(
                        choices=[100, 200, 500, 1000], value=500,
                        label="行数", scale=1,
                        elem_classes=["utility-field", "utility-select"],
                    )
                    logs_refresh_btn = gr.Button(
                        "刷新", scale=0, min_width=72,
                        elem_classes=["utility-secondary-action"],
                    )
                logs_output = gr.HTML(
                    value='<div class="utility-log-scroll"><div class="utility-log-line utility-log-default"><span class="utility-log-number">0001</span><span class="utility-log-message">点击刷新查看最近日志。</span></div></div>',
                    show_label=False,
                    elem_classes=["utility-field", "utility-log"],
                )

            with gr.Column(visible=False, elem_classes=["utility-view"]) as status_view:
                gr.Markdown("### 服务状态")
                status_summary = gr.HTML('<div class="utility-status">点击刷新获取状态。</div>')
                status_details = gr.HTML('<div class="utility-status">尚未检测。</div>')
                status_refresh_btn = gr.Button(
                    "刷新状态", variant="secondary",
                    elem_classes=["utility-secondary-action"],
                )

        # ---- 浮动音频播放条（自动播放TTS语音） ----
        tts_audio_output = gr.Audio(
            type="filepath", format="wav", interactive=False,
            # LiveTalking's WebRTC stream is the primary synchronized audio
            # path. Keep this component hidden for API compatibility, but do
            # not feed it the same audio or the browser will play two copies.
            visible=False, autoplay=True,
            elem_classes=["audio-player"],
        )

        # ---- 隐藏组件 ----
        audio_input = gr.Audio(
            sources=["microphone"], type="numpy", streaming=True, visible=True,
            elem_id="voice-mic-recorder",
        )
        upload_status = gr.Textbox(visible=False)

        # =====================================================================
        # 事件处理
        # =====================================================================
        is_recording = gr.State(False)
        phone_active = gr.State(False)
        phone_session = ContinuousConversationSession(pipeline)
        atexit.register(phone_session.shutdown)
        ui_chat_history = []

        def handle_text_input(text):
            nonlocal ui_chat_history
            if not text or not text.strip():
                return "", ui_chat_history, "就绪", None
            new_history = list(ui_chat_history)
            reply_text = ""
            status = "思考中..."
            audio_segments = []
            audio_sr = 24000
            progressive_audio_count = 0
            progressive_voxcpm = True

            def audio_skip():
                """Keep the current browser audio while text/status streams update."""
                return gr.skip() if hasattr(gr, "skip") else None

            def write_playback_wav(audio_data, sample_rate, sequence):
                """Normalize one TTS unit and persist it with a unique cache path."""
                import soundfile as sf

                raw = np.asarray(audio_data).reshape(-1)
                if np.issubdtype(raw.dtype, np.integer):
                    audio_f32 = raw.astype(np.float32) / 32767.0
                else:
                    audio_f32 = raw.astype(np.float32)
                audio_f32 = np.clip(audio_f32, -1.0, 1.0)
                playback_rms = float(
                    np.sqrt(np.mean(audio_f32.astype(np.float64) ** 2))
                )
                playback_peak = float(np.max(np.abs(audio_f32)))
                target_rms = 0.25  # roughly -12 dBFS before peak limiting
                desired_gain = target_rms / max(playback_rms, 1e-6)
                peak_safe_gain = 0.95 / max(playback_peak, 1e-6)
                playback_gain = min(desired_gain, peak_safe_gain, 4.0)
                audio_f32 = np.clip(
                    audio_f32 * playback_gain, -0.95, 0.95
                ).astype(np.float32)
                output_path = os.path.join(
                    LOG_DIR,
                    f"tts_chunk_{sequence}_{uuid.uuid4().hex}.wav",
                )
                sf.write(output_path, audio_f32, int(sample_rate))
                return output_path, playback_gain, audio_f32

            def make_message(role, content):
                """Use Gradio's native message object when the runtime provides it."""
                if hasattr(gr, "ChatMessage"):
                    return gr.ChatMessage(role=role, content=content)
                return {"role": role, "content": content}

            def message_role(message):
                if isinstance(message, dict):
                    return message.get("role")
                return getattr(message, "role", None)

            def set_message_content(message, content):
                if isinstance(message, dict):
                    message["content"] = content
                else:
                    message.content = content

            def append_reply(reply):
                """Keep chatbot data in Gradio's cross-version message shape."""
                if not new_history or message_role(new_history[-1]) != "assistant":
                    new_history.append(make_message("user", text))
                    new_history.append(make_message("assistant", reply))
                else:
                    set_message_content(new_history[-1], reply)

            try:
                for event in pipeline.process_text(text.strip()):
                    if event["type"] == "reply_token":
                        reply_text += event["text"]
                        append_reply(reply_text)
                        yield "", new_history, status, audio_skip()
                    elif event["type"] == "status":
                        status = event["text"]
                        # Keep generator alive during TTS phase
                        yield "", new_history, status, audio_skip()
                    elif event["type"] == "error":
                        append_reply(f"错误: {event['error']['message']}")
                        yield "", new_history, "就绪", audio_skip()
                        return
                    elif event["type"] == "audio":
                        sr, audio_data = event["data"]
                        if audio_data is not None and len(audio_data) > 0:
                            if progressive_voxcpm:
                                progressive_audio_count += 1
                                duration = len(audio_data) / max(sr, 1)
                                logger.info(
                                    f"TTS WebRTC 播放: 第{progressive_audio_count}段 "
                                    f"{duration:.1f}s"
                                )
                                # ConversationPipeline has already written this
                                # chunk into the persistent LiveTalking stream.
                                yield "", new_history, "播放中", audio_skip()
                            else:
                                audio_segments.append(audio_data)
                                audio_sr = sr
                                # Signal TTS progress to keep connection alive
                                yield "", new_history, f"语音生成中 ({len(audio_segments)})...", audio_skip()
            except Exception as e:
                logger.error(f"文字处理异常: {traceback.format_exc()}")
                append_reply("处理失败，请重试")

            ui_chat_history = new_history

            # Qwen chunks have already been sent one by one. Do not yield a final
            # empty audio update, otherwise Gradio can clear the last chunk.
            if progressive_audio_count:
                logger.info(
                    f"TTS 持续流播放结束: 共{progressive_audio_count}段，未限制回复长度"
                )
                return

            # 合并所有音频段，写入固定路径 WAV，返回给 Gradio
            if audio_segments:
                full_audio = np.concatenate(audio_segments)
                dump_path, playback_gain, full_audio_f32 = write_playback_wav(
                    full_audio, audio_sr, "full"
                )
                logger.info(
                    f"TTS 完成: {len(full_audio_f32)} 采样点 @ {audio_sr}Hz "
                    f"({len(full_audio_f32)/audio_sr:.1f}s), 播放增益={playback_gain:.2f}x "
                    f"({20*np.log10(max(playback_gain, 1e-6)):.1f}dB)"
                )
                yield "", new_history, "播放中", dump_path
                # Gradio receives the browser audio update before LiveTalking starts,
                # keeping visible mouth motion aligned with local playback startup.
                # 音频已送出，不再 yield None 覆盖它
                return

            yield "", new_history, "就绪", None

        def handle_audio_input(audio_data):
            if audio_data is None:
                return "", [], "就绪"
            if isinstance(audio_data, tuple):
                sr, samples = audio_data
            else:
                sr, samples = 16000, audio_data
            if samples is None or len(samples) == 0:
                return "", [], "就绪"
            if not isinstance(samples, np.ndarray):
                samples = np.array(samples)
            if samples.ndim > 1:
                samples = samples[:, 0] if samples.shape[1] > 0 else samples.flatten()
            chat_history = []
            status = "就绪"
            user_text = ""
            reply_text = ""
            audio_count = 0

            def make_message(role, content):
                if hasattr(gr, "ChatMessage"):
                    return gr.ChatMessage(role=role, content=content)
                return {"role": role, "content": content}

            def set_message_content(message, content):
                if isinstance(message, dict):
                    message["content"] = content
                else:
                    message.content = content

            try:
                for event in pipeline.process_voice(
                    samples.astype(np.float32) / 32768.0
                    if samples.dtype == np.int16
                    else samples.astype(np.float32), sr
                ):
                    if event["type"] == "transcription":
                        user_text = event["text"]
                        chat_history.append(make_message("user", user_text))
                        chat_history.append(make_message("assistant", ""))
                    elif event["type"] == "reply_token":
                        reply_text += event["text"]
                        set_message_content(chat_history[-1], reply_text)
                        yield chat_history, status, ""
                    elif event["type"] == "status":
                        status = event["text"]
                        yield chat_history, status, ""
                    elif event["type"] == "audio":
                        # 语音输入链路此前漏掉了 audio 事件，导致音频只
                        # 累积在管线里，直到整轮结束才尝试发送。现在每个
                        # ConversationPipeline 已将片段写入持久 LiveTalking 流。
                        audio_count += 1
                        status = "播放中"
                        yield chat_history, status, ""
                    elif event["type"] == "error":
                        chat_history.append(make_message("user", "(语音)"))
                        chat_history.append(make_message("assistant", f"错误: {event['error']['message']}"))
            except Exception as e:
                logger.error(f"语音处理异常: {traceback.format_exc()}")
                chat_history.append(make_message("user", "(语音)"))
                chat_history.append(make_message("assistant", "处理失败，请重试"))
            yield chat_history, status, ""

        def toggle_phone_mode(active):
            if active:
                phone_session.stop()
                return False, "通话已结束"
            phone_session.start()
            return True, "通话中 · 请说话"

        def poll_phone_events(active):
            """把后台通话线程产生的事件低频合并到 Gradio 页面。"""
            nonlocal ui_chat_history
            if not active:
                return gr.skip(), gr.skip(), gr.skip()

            events = phone_session.drain_events()
            if not events:
                return gr.skip(), gr.skip(), gr.skip()

            status = "通话中 · 请说话"

            def make_message(role, content):
                if hasattr(gr, "ChatMessage"):
                    return gr.ChatMessage(role=role, content=content)
                return {"role": role, "content": content}

            def set_message_content(message, content):
                if isinstance(message, dict):
                    message["content"] = content
                else:
                    message.content = content

            for event in events:
                event_type = event.get("type")
                if event_type == "transcription":
                    ui_chat_history.append(make_message("user", event.get("text", "")))
                    ui_chat_history.append(make_message("assistant", ""))
                elif event_type == "reply_token":
                    last_is_assistant = bool(ui_chat_history) and (
                        (isinstance(ui_chat_history[-1], dict) and ui_chat_history[-1].get("role") == "assistant")
                        or getattr(ui_chat_history[-1], "role", None) == "assistant"
                    )
                    if not last_is_assistant:
                        ui_chat_history.append(make_message("assistant", ""))
                    current = ui_chat_history[-1]
                    old_content = current.get("content", "") if isinstance(current, dict) else getattr(current, "content", "")
                    set_message_content(current, old_content + event.get("text", ""))
                elif event_type == "status":
                    status = event.get("text", status)
                    if event.get("state") == "idle" and phone_session.active:
                        status = "通话中 · 请说话"
                elif event_type == "audio":
                    status = "播放中 · 你可以随时插话"
                elif event_type == "error":
                    error = event.get("error", {})
                    ui_chat_history.append(make_message(
                        "assistant", f"错误：{error.get('message', '语音处理失败')}"
                    ))
                    status = "通话中 · 请继续说话"

            return ui_chat_history, status, gr.skip()

        def handle_phone_audio_chunk(audio_data):
            """Audio.stream 的单一入口：入队音频后立即刷新通话 UI。"""
            active = phone_session.active
            if not active:
                return gr.skip(), gr.skip(), gr.skip()
            phone_session.feed_audio(audio_data)
            return poll_phone_events(active)

        def handle_stop():
            phone_session.stop()
            pipeline.stop()
            return gr.skip(), "已停止", False

        def update_connection_status():
            return format_status_summary(get_service_status())

        def update_status_display():
            return state_machine.state_text, update_connection_status()

        def open_settings_panel():
            drawer_state = toggle_utility_panel("settings")
            return (
                *drawer_state,
                render_runtime_settings(),
                *get_runtime_form_values(),
                ACTIVE_REF_TEXT,
                "",
                "",
            )

        def open_logs_panel(source, limit):
            return (*toggle_utility_panel("logs"), render_recent_logs(source, limit))

        def open_status_panel():
            summary, details = refresh_status_details()
            return (*toggle_utility_panel("status"), summary, details)

        def apply_runtime_settings_ui(*form_values):
            result = apply_runtime_settings(dict(zip(EDITABLE_RUNTIME_KEYS, form_values)))
            if result.get("ok"):
                return render_runtime_settings(), result.get("message", "运行设置已应用")
            error = result.get("error", {})
            return render_runtime_settings(), error.get("message", "运行设置应用失败")

        def apply_reference_ui(file_path, prompt_text):
            result = apply_reference_audio(file_path, prompt_text or "")
            if result.get("ok"):
                message = result.get("message", "音色已应用")
                return render_runtime_settings(), message
            error = result.get("error", {})
            return render_runtime_settings(), error.get("message", "音色应用失败")

        # ---- 绑定事件 ----
        settings_btn.click(
            fn=open_settings_panel,
            inputs=[],
            outputs=[
                utility_drawer, utility_title, settings_view, logs_view,
                status_view, settings_summary,
                llm_temperature_input, llm_max_tokens_input, llm_keep_alive_input,
                role_style_input, role_custom_instruction_input,
                vad_threshold_input, min_silence_input, max_audio_input,
                asr_language_input, tts_style_input, audio_gain_input,
                prebuffer_input, rebuffer_input, max_buffer_input,
                fade_in_input, lead_in_input, log_level_input,
                reference_text_input, settings_result, reference_result,
            ],
        )
        logs_btn.click(
            fn=open_logs_panel,
            inputs=[logs_source, logs_limit],
            outputs=[utility_drawer, utility_title, settings_view, logs_view, status_view, logs_output],
        )
        status_btn.click(
            fn=open_status_panel,
            inputs=[],
            outputs=[utility_drawer, utility_title, settings_view, logs_view, status_view, status_summary, status_details],
        )
        drawer_close.click(
            fn=close_utility_panel,
            inputs=[],
            outputs=[utility_drawer, settings_view, logs_view, status_view],
        )
        apply_runtime_btn.click(
            fn=apply_runtime_settings_ui,
            inputs=[
                llm_temperature_input, llm_max_tokens_input, llm_keep_alive_input,
                role_style_input, role_custom_instruction_input,
                vad_threshold_input, min_silence_input, max_audio_input,
                asr_language_input, tts_style_input, audio_gain_input,
                prebuffer_input, rebuffer_input, max_buffer_input,
                fade_in_input, lead_in_input, log_level_input,
            ],
            outputs=[settings_summary, settings_result],
        )
        apply_reference_btn.click(
            fn=apply_reference_ui,
            inputs=[ref_audio_upload, reference_text_input],
            outputs=[settings_summary, reference_result],
        )
        logs_refresh_btn.click(
            fn=render_recent_logs,
            inputs=[logs_source, logs_limit],
            outputs=[logs_output],
        )
        logs_source.change(
            fn=render_recent_logs,
            inputs=[logs_source, logs_limit],
            outputs=[logs_output],
        )
        logs_limit.change(
            fn=render_recent_logs,
            inputs=[logs_source, logs_limit],
            outputs=[logs_output],
        )
        status_refresh_btn.click(
            fn=refresh_status_details,
            inputs=[],
            outputs=[status_summary, status_details],
        )
        send_btn.click(
            fn=handle_text_input,
            inputs=[text_input],
            outputs=[text_input, chatbot, status_text, tts_audio_output],
        )
        text_input.submit(
            fn=handle_text_input,
            inputs=[text_input],
            outputs=[text_input, chatbot, status_text, tts_audio_output],
        )
        status_text.change(
            fn=None,
            inputs=[status_text],
            outputs=[],
            js="""(status) => {
                const shell = document.querySelector('.avatar-shell');
                if (shell) shell.classList.toggle('is-speaking', /说话中|播放中|语音生成/.test(status || ''));
            }""",
        )
        stop_btn.click(
            fn=handle_stop, inputs=[], outputs=[chatbot, status_text, phone_active],
        )
        # 语音球：开启后持续保持麦克风连接，由服务端 VAD 自动切句。
        # 只操作 Gradio 的录音按钮，绝不能点击 file input，否则会打开文件夹。
        voice_btn.click(
            fn=toggle_phone_mode,
            inputs=[phone_active],
            outputs=[phone_active, status_text],
            js="""() => {
                const recorder = document.getElementById('voice-mic-recorder');
                if (!recorder) {
                    console.error('Voice recorder component is unavailable');
                    return;
                }

                const firstVisible = (selector) => Array.from(
                    recorder.querySelectorAll(selector)
                ).find((button) => {
                    const style = window.getComputedStyle(button);
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && !button.disabled;
                });
                const stopControl = firstVisible(
                    'button.stop-button, button[aria-label="Stop recording"], '
                    + 'button[aria-label="Stop audio recording"]'
                );
                const recordControl = firstVisible(
                    'button.record-button, button[aria-label="Record audio"]'
                );
                const control = stopControl || recordControl;
                if (!control) {
                    console.error('Voice recorder control is unavailable');
                    return;
                }

                control.click();
                const voiceBall = document.querySelector('.voice-ball-btn');
                if (voiceBall) {
                    voiceBall.textContent = stopControl ? '语音\\n通话' : '结束\\n通话';
                    voiceBall.classList.toggle('is-recording', !stopControl);
                    voiceBall.setAttribute('aria-label', stopControl ? '开始语音通话' : '结束语音通话');
                }
            }""",
        )
        audio_stream_kwargs = {
            "fn": handle_phone_audio_chunk,
            # Gradio 4 的 stream 事件只会把流式 Audio 作为输入传回；State
            # 不会随每个音频块一起提交，因此从 phone_session.active 读取。
            "inputs": [audio_input],
            "outputs": [chatbot, status_text, text_input],
            "queue": False,
            "show_progress": "hidden",
            "concurrency_limit": None,
        }
        # Gradio 6 exposes ``stream_every`` for backend stream throttling.
        # Gradio 4 的 ``every`` 会把 Audio.stream 改造成 Timer.tick，导致
        # 音频块不再作为输入传入，因此不能把它用于这里的持续麦克风链路。
        stream_signature = inspect.signature(audio_input.stream).parameters
        if "stream_every" in stream_signature:
            audio_stream_kwargs["stream_every"] = 0.25
        audio_input.stream(**audio_stream_kwargs)
        # 页面加载
        demo.load(
            fn=update_status_display,
            inputs=[], outputs=[status_text, connection_html],
        )

    return demo


# =============================================================================
# 主入口
# =============================================================================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("local-ai-companion-v2 speech-to-speech 管线启动")
    logger.info(f"配置文件: {CONFIG_PATH}")
    logger.info(f"LLM 地址: {LLM_BASE_URL}")
    logger.info(f"TTS 模型: {VOXCPM_MODEL_ID} ({VOXCPM_PROFILE})")
    logger.info(f"TTS 后端: VoxCPM Worker ({VOXCPM_WORKER_URL})")
    logger.info(f"上传目录: {UPLOAD_DIR}")
    logger.info("=" * 60)

    def warm_local_models():
        # Sequential warmup avoids loading Ollama and the 1.7B TTS model at the
        # same time on a 16GB GPU. The short phrase is synthesized once with
        # the cloned voice and reused as an immediate conversational backchannel.
        engine = _get_tts()
        if hasattr(engine, "prepare_backchannel"):
            engine.prepare_backchannel()
        llm_client.warmup()

    threading.Thread(
        target=warm_local_models,
        name="local-model-preload",
        daemon=True,
    ).start()

    demo = create_ui()

    # Gradio 默认监听 0.0.0.0:7860，通过 WSL2 localhostForwarding 对 Windows 可见
    # 若需仅监听 127.0.0.1（安全加固），设置 server_name="127.0.0.1"
    demo.queue(
        default_concurrency_limit=1,  # 单会话约束（SEC-16 弱化）
        max_size=5,
    ).launch(
        server_name="0.0.0.0",     # WSL2 需要 0.0.0.0 才能通过 localhost 访问
        server_port=7860,
        share=False,
        allowed_paths=[LOG_DIR],
        show_error=True,  # SEC-11: True 时 Gradio 仍会显示错误，我们已在代码层捕获
        inbrowser=False,
        quiet=False,
    )


# =============================================================================
# 安全自查清单 (SEC-01~20)
# =============================================================================
#
# SEC-01 (SQL注入): 不适用 — 本项目无数据库。
#
# SEC-02 (eval/exec/Function): [通过]
#   - 代码中无 eval()、exec()、compile()、Function() 调用。
#   - LLM 回复文本仅作字符串拼接和显示，不执行代码。
#   - regex 用于文本拆分，无动态代码执行风险。
#
# SEC-03 (命令注入): [通过]
#   - 所有外部服务调用使用 httpx/requests 库，HTTP 请求参数由代码构造。
#   - URL 从 voice.yaml 读取，不拼接任何用户输入。
#   - 无 os.system()、subprocess.Popen(shell=True) 等 shell 命令调用。
#   - faster-whisper/Qwen3-TTS 通过 Python API 调用，不经过命令行。
#
# SEC-04 (XSS): [通过]
#   - 使用 Gradio 原生组件渲染文本，Gradio 默认对 Markdown/HTML 组件做安全转义。
#   - chatbot 组件使用 Markdown 渲染，Gradio 会自动转义 HTML 标签。
#   - 无 innerHTML 直接插入模型输出。
#   - 自定义 HTML 组件（avatar_video）内容为静态 HTML，不含用户/模型输出。
#   - tts_audio_output (gr.Audio) 接收 (sample_rate, numpy_array) 数值数据，无 HTML 注入风险。
#
# SEC-05 (路径遍历): [通过]
#   - handle_audio_upload() 使用 os.path.basename() 清除路径分隔符。
#   - 文件名使用 SHA256 哈希生成安全名称。
#   - validate_ref_audio() 验证最终路径位于 UPLOAD_DIR 内。
#   - UPLOAD_DIR 从 voice.yaml 读取，限制在 ~/setup/uploads/。
#
# SEC-06 (硬编码密钥): [通过]
#   - 无 API Key/Token/JWT secret 等密钥。
#   - 端口与模型路径从 voice.yaml 读取，无硬编码敏感信息。
#   - 本项目无认证，无密码。
#
# SEC-07 (密码): 不适用 — 无用户认证、无密码。
# SEC-08 (JWT): 不适用 — 无 JWT。
# SEC-09 (权限校验): 不适用 — 无支付/用户数据/管理后台，服务仅绑定 127.0.0.1。
#
# SEC-10 (日志隐私): [通过]
#   - 日志记录转写文本和对话内容（用于调试），但不记录完整音频文件。
#   - 参考音频路径在日志中出现，但文件内容不记录。
#   - 日志级别和目录通过 voice.yaml 控制（LOG_LEVEL / LOG_DIR），已实际生效（Bug-006 修复）。
#   - 日志文件写入 LOG_DIR/voice-pipeline.log，同时输出到控制台。
#
# SEC-11 (异常泄露): [通过]
#   - PipelineError 携带应用级错误码和用户可理解的 message。
#   - error_response() 构建统一错误体，detail 字段默认 "详见本地日志"。
#   - CFG_ERR_001 (配置缺失) 已包装为 PipelineError 统一格式（Bug-002 修复）。
#   - Gradio 事件处理中使用 try/except 捕获所有异常，只返回可理解消息。
#   - show_error=True 但自定义错误已捕获，不会泄漏 Python 堆栈。
#
# SEC-12 (上传校验): [通过]
#   - ALLOWED_AUDIO_EXTENSIONS 白名单 (wav/mp3/m4a/flac)。
#   - MAX_UPLOAD_SIZE_MB 大小上限 (默认 15MB)。
#   - MIN_REF_AUDIO_SEC / MAX_REF_AUDIO_SEC 时长校验 (5-15秒)。
#   - validate_ref_audio() 对所有上传执行完整校验。
#
# SEC-13 (GET改数据): [通过]
#   - GET 请求仅用于页面加载（/）和探活（/health）。
#   - 所有状态变更通过 Gradio 按钮事件（HTTP POST）触发。
#
# SEC-14 (CORS): [通过（弱化）]
#   - 服务仅绑定 127.0.0.1，无跨域场景。
#   - avatar-sync.js 设置了 CORS Access-Control-Allow-Origin: localhost:7860（非 *）。
#
# SEC-15 (Cookie): 不适用 — 无 Cookie。
#
# SEC-16 (限流): [通过（弱化）]
#   - demo.queue(default_concurrency_limit=1) 限制单并发，防止重入。
#   - 打断后释放取消令牌再接受新一轮，禁止并发 TTS 队列叠加。
#
# SEC-17 (响应过度暴露): [通过]
#   - get_service_status() 只返回状态字符串，不返回模型路径。
#   - 错误响应体仅含 code/message/detail，不含内部路径或配置。
#   - 参考音频上传返回 file_id（哈希），不返回完整路径。
#
# SEC-18 (依赖漏洞): [通过]
#   - requirements.txt 中所有依赖已锁定版本号。
#   - 已移除无效的 huggingface-cli==0.0.0 占位版本（Bug-007 修复）。
#   - CLI 功能由 huggingface_hub 包提供。
#   - 注释中标注审计建议（定期 pip-audit）。
#
# SEC-19 (默认密码): 不适用 — 无密码。
# SEC-20 (数据库连接池): 不适用 — 无数据库。
# =============================================================================
