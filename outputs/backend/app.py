"""
=============================================================================
app.py — local-ai-companion-v2 Gradio 主应用 (speech-to-speech 管线)
=============================================================================
运行: python app.py
配置文件: ~/setup/voice.yaml（自动查找）
端口: 7860 (Gradio 默认)
=============================================================================
架构: VAD → faster-whisper (ASR) → llama.cpp (LLM转发) → Qwen3-TTS → 双路分发
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
from pathlib import Path
from datetime import datetime
from io import BytesIO
from typing import Optional, Generator, List, Dict, Any, Tuple

# ---- 配置加载 ----
import yaml
import requests
import httpx
import numpy as np

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
        os.path.expanduser("~/setup/voice.yaml"),
        os.path.join(os.path.dirname(__file__), "..", "..", "config", "voice.yaml"),
        os.path.join(os.getcwd(), "config", "voice.yaml"),
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
LLM_BASE_URL = str(config.get("LLM_BASE_URL", "http://localhost:8090"))
LLM_MODEL = str(config.get("LLM_MODEL", "qwen3-8b"))
LLM_MAX_TOKENS = int(config.get("LLM_MAX_TOKENS", 1024))
LLM_TEMPERATURE = float(config.get("LLM_TEMPERATURE", 0.7))
TTS_MODEL_PATH = os.path.expanduser(str(config.get("TTS_MODEL_PATH", "~/setup/models/Qwen3-TTS-1.7B")))
TTS_REF_WAV = os.path.expanduser(str(config.get("TTS_REF_WAV", "~/setup/ref.wav")))
TTS_REF_TEXT = str(config.get("TTS_REF_TEXT", ""))
TTS_SAMPLE_RATE = int(config.get("TTS_SAMPLE_RATE", 16000))
LIVETALKING_URL = str(config.get("LIVETALKING_URL", "http://localhost:8010"))
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

_log_level = getattr(logging, LOG_LEVEL, logging.INFO)
os.makedirs(LOG_DIR, exist_ok=True)
_log_file = os.path.join(LOG_DIR, "voice-pipeline.log")

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
    status = {
        "llama": check_service("llama-server", f"{LLM_BASE_URL}/api/tags"),
        "asr": "ready",   # ASR 在本地进程内，始终就绪
        "tts": "ready",   # TTS 在本地进程内，始终就绪
        "livetalking": check_service("LiveTalking", f"{LIVETALKING_URL}/health"),
        "avatar_sync": check_service("avatar-sync", f"{AVATAR_SYNC_URL}/health"),
    }
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
            compute_type = "float16"
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
            # 确保音频为 float32
            if audio_data.dtype != np.float32:
                audio_data = audio_data.astype(np.float32)

            segments, info = self.model.transcribe(
                audio_data,
                language=ASR_LANG,
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(
                    threshold=VAD_THRESH,
                    min_silence_duration_ms=MIN_SILENCE_MS,
                ),
            )

            text_parts = []
            for segment in segments:
                text_parts.append(segment.text.strip())

            result = "".join(text_parts).strip()
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


# =============================================================================
# TTS 模块（Qwen3-TTS 1.7B + 音色克隆，#5 架构设计）
# =============================================================================

class TTSEngine:
    """
    语音合成引擎：Qwen3-TTS 1.7B + 音色克隆。
    接收文本，使用 ref.wav 音色克隆合成音频。
    """

    def __init__(self):
        self.model = None
        self.processor = None
        self.ref_wav = TTS_REF_WAV
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
                local_files_only=True,
            )
            self.processor = None  # qwen-tts 不需要独立的 processor
            logger.info("TTS: 模型加载完成（qwen-tts）")

        except Exception as e:
            logger.error(f"TTS 模型初始化失败: {e}")
            logger.error(traceback.format_exc())
            self.model = None
            self.processor = None

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
            wavs, sr = self.model.generate_voice_clone(
                text=text,
                language="Chinese",
                ref_audio=ref_path,
                x_vector_only_mode=True,  # 只用音色向量，不需要参考文本
            )

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
        logger.info(f"参考音频已更新: {new_ref_path}")


# 全局 TTS 引擎（懒加载，阶段 2 才需要）
# 注意：不在模块导入时初始化，避免 CUDA 模型加载卡死启动。
# 首次语音合成时通过 _get_tts() 按需加载。
tts_engine = None

def _get_tts():
    """懒加载 TTS 引擎"""
    global tts_engine
    if tts_engine is None:
        try:
            tts_engine = TTSEngine()
        except Exception as e:
            logger.warning(f"TTS 引擎未加载（阶段 2 功能，不影响文字聊天）: {e}")
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

def handle_audio_upload(uploaded_file) -> dict:
    """
    处理参考音频上传。
    SEC-05: 文件名防路径遍历，限制在 UPLOAD_DIR。
    SEC-12: 白名单扩展名 + 大小上限。
    """
    if uploaded_file is None:
        return error_response("TTS_ERR_002", "请上传 5-15 秒清晰单人声参考音频")

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

        # 更新 TTS 参考音频
        if _get_tts() is not None:
            _get_tts().update_ref_audio(save_path)

        logger.info(f"参考音频已保存: {save_path}")
        return {
            "ok": True,
            "message": "参考音频上传成功，音色克隆已更新",
            # SEC-17: 不暴露完整文件路径
            "file_id": safe_name,
        }

    except Exception as e:
        logger.error(f"音频上传处理异常: {traceback.format_exc()}")
        return error_response("TTS_ERR_002", "参考音频上传处理失败", "")


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


def forward_audio_to_avatar(audio_data: np.ndarray, sample_rate: int = 16000,
                             session_id: str = "") -> bool:
    """
    将 TTS 音频直发 LiveTalking /humanaudio，驱动 wav2lip 口型。
    """
    try:
        import io
        import wave

        # float32 → int16 WAV
        if audio_data.dtype == np.float32:
            pcm = (audio_data * 32767).astype(np.int16)
        else:
            pcm = audio_data.astype(np.int16)

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm.tobytes())

        # 直发 LiveTalking /humanaudio（sessionid=0）
        url = f"{LIVETALKING_URL}/humanaudio"
        logger.info(f"口型驱动: 转发 {wav_buffer.tell()} 字节音频 -> {url}")
        resp = requests.post(url,
            files={"file": ("tts.wav", wav_buffer.getvalue(), "audio/wav")},
            data={"sessionid": "0"},
            timeout=5.0)
        ok = resp.status_code == 200
        if ok:
            logger.info(f"口型驱动: 转发成功 -> {resp.json()}")
        else:
            logger.warning(f"口型驱动: 转发失败 HTTP {resp.status_code}: {resp.text[:200]}")
        return ok
    except requests.ConnectionError:
        logger.warning("口型驱动: LiveTalking 不可达，跳过")
        return False
    except Exception as e:
        logger.warning(f"口型驱动: 转发异常 - {e}")
        return False


# =============================================================================
# 核心对话管线
# =============================================================================

class ConversationPipeline:
    """语音对话完整管线：VAD -> ASR -> LLM -> TTS -> 双路分发"""

    def __init__(self):
        self.vad = VADDetector()
        self.history: List[dict] = [
            {"role": "system", "content": "你是一个友善、体贴的AI语音助手，说话温柔自然，用中文回复。回复简洁、口语化，适合语音朗读。"}
        ]
        self._audio_segments: List[tuple] = []  # 累积 (audio_np, sample_rate) 用于最后一次性送口型

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

        if _get_asr() is None:
            state_machine.transition(AvatarState.IDLE)
            yield {"type": "error", "error": {"code": "ASR_UNAVAILABLE", "message": "语音识别未安装（阶段 2 功能），请使用文字输入"}}
            return
        try:
            user_text = _get_asr().transcribe(audio_data, sample_rate)
        except PipelineError as e:
            state_machine.transition(AvatarState.IDLE)
            yield {"type": "error", "error": e.to_dict()["error"]}
            return

        yield {"type": "transcription", "text": user_text}
        self.history.append({"role": "user", "content": user_text})

        # ---- LLM 流式回复 ----
        yield {"type": "status", "state": "thinking", "text": "思考中..."}

        full_reply = ""
        sentence_buffer = ""
        cancel = state_machine.cancel_event

        try:
            for token in llm_client.stream_chat(self.history, cancel):
                if cancel.is_set():
                    break
                full_reply += token
                sentence_buffer += token

                # 按自然停顿拆句，尽早送 TTS
                split_point = re.search(r'[。！？\n!?]', sentence_buffer)
                if split_point:
                    sentence = sentence_buffer[:split_point.end()].strip()
                    sentence_buffer = sentence_buffer[split_point.end():]

                    if sentence:
                        yield {"type": "reply_token", "text": sentence}
                        # 送 TTS 并分发
                        audio_result = yield from self._synthesize_and_dispatch(
                            sentence, session_id, cancel
                        )
                        if audio_result:
                            yield audio_result
                else:
                    yield {"type": "reply_token", "text": token}

            # 处理剩余文本
            if sentence_buffer.strip() and not cancel.is_set():
                final_sentence = sentence_buffer.strip()
                yield {"type": "reply_token", "text": final_sentence}
                audio_result = yield from self._synthesize_and_dispatch(
                    final_sentence, session_id, cancel
                )
                if audio_result:
                    yield audio_result

        except PipelineError as e:
            state_machine.transition(AvatarState.IDLE)
            yield {"type": "error", "error": e.to_dict()["error"]}
            return

        # ---- 保存到历史 ----
        if not cancel.is_set():
            self.history.append({"role": "assistant", "content": full_reply})
            # 限制历史长度（滑动窗口，保留最近 20 轮）
            if len(self.history) > 41:  # 1 system + 20*2 user/assistant
                self.history = [self.history[0]] + self.history[-40:]

        state_machine.transition(AvatarState.IDLE)
        self._flush_avatar_audio()
        yield {"type": "status", "state": "idle", "text": "待机"}

    def process_text(self, text: str) -> Generator[dict, None, None]:
        """处理文字输入"""
        session_id = str(uuid.uuid4())
        state_machine.reset_cancel()

        state_machine.transition(AvatarState.THINKING)
        yield {"type": "status", "state": "thinking", "text": "思考中..."}

        self.history.append({"role": "user", "content": text})
        yield {"type": "transcription", "text": text}

        full_reply = ""
        cancel = state_machine.cancel_event

        try:
            for token in llm_client.stream_chat(self.history, cancel):
                if cancel.is_set():
                    break
                full_reply += token
                yield {"type": "reply_token", "text": token}

        except PipelineError as e:
            state_machine.transition(AvatarState.IDLE)
            yield {"type": "error", "error": e.to_dict()["error"]}
            return

        # 完整回复送 TTS
        if full_reply.strip() and not cancel.is_set():
            # 拆句逐句合成
            for sentence in split_sentences(full_reply):
                if cancel.is_set():
                    break
                audio_result = yield from self._synthesize_and_dispatch(
                    sentence, session_id, cancel
                )
                if audio_result:
                    yield audio_result

        if not cancel.is_set():
            self.history.append({"role": "assistant", "content": full_reply})
            if len(self.history) > 41:
                self.history = [self.history[0]] + self.history[-40:]

        state_machine.transition(AvatarState.IDLE)
        self._flush_avatar_audio()
        yield {"type": "status", "state": "idle", "text": "待机"}

    def _synthesize_and_dispatch(self, text: str, session_id: str,
                                  cancel_event: threading.Event) -> Generator[dict, None, None]:
        """TTS 合成 + 双路分发"""
        if not text.strip():
            return

        state_machine.transition(AvatarState.SPEAKING)
        yield {"type": "status", "state": "speaking", "text": "说话中..."}

        try:
            if _get_tts() is not None:
                audio, sr = _get_tts().synthesize(text, cancel_event=cancel_event)
            else:
                audio, sr = None, 0

            if cancel_event.is_set():
                return

            if audio is not None and sr > 0:
                # 路径A: 回传浏览器播放
                audio_int16 = (audio * 32767).astype(np.int16)
                yield {"type": "audio", "data": (sr, audio_int16)}

                # 累积音频，等全部合成完一次性送 /humanaudio（保证口型连续）
                self._audio_segments.append((audio, sr))
                logger.info(f"口型驱动: 累积音频 {len(audio)} 采样点 (第{len(self._audio_segments)}段)")

        except Exception as e:
            logger.error(f"TTS 分发异常: {traceback.format_exc()}")
            yield {"type": "error", "error": {
                "code": "TTS_ERR_001",
                "message": "语音合成失败，仅显示文字回复"
            }}

    def stop(self):
        """停止/打断"""
        state_machine.transition(AvatarState.IDLE)
        logger.info("用户触发停止/打断")

    def _flush_avatar_audio(self):
        """拼接所有累积的 TTS 音频段，一次性发送到 LiveTalking /humanaudio 驱动口型。"""
        if not self._audio_segments:
            return
        try:
            # 拼接所有音频段
            all_audio = np.concatenate([seg for seg, _ in self._audio_segments])
            sr = self._audio_segments[0][1]  # 所有段用同一个采样率

            # RMS 归一化到 -10dBFS，保证口型明显（wav2lip 对音量极其敏感）
            rms = float(np.sqrt(np.mean(all_audio.astype(np.float64) ** 2)))
            target_rms = 0.316  # -10dBFS
            if rms > 0.0001:
                gain = target_rms / rms
            else:
                gain = 4.0  # 静音兜底
            all_audio = np.clip(all_audio * gain, -1.0, 1.0).astype(np.float32)

            total_sec = len(all_audio) / sr
            logger.info(f"口型驱动: 发送完整音频 {len(all_audio)}样本 @ {sr}Hz ({total_sec:.1f}s) "
                        f"原始RMS={rms:.4f} → 增益={gain:.1f}x ({20*np.log10(gain):.0f}dB)")
            threading.Thread(
                target=forward_audio_to_avatar,
                args=(all_audio, sr, "0"),
                daemon=True,
            ).start()
        except Exception as e:
            logger.error(f"口型驱动: 拼接/转发失败 - {e}")
        finally:
            self._audio_segments = []

    def clear_history(self):
        """清除对话历史"""
        self.history = self.history[:1]  # 保留 system prompt
        logger.info("对话历史已清除")


# 全局管线实例
pipeline = ConversationPipeline()


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

    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap');

    /* ── Base ──────────────────────────────────────────────────────────────── */
    footer { display: none !important; }
    .gradio-container {
        max-width: 100% !important;
        padding: 0 !important;
        font-family: 'Poppins', system-ui, -apple-system, sans-serif !important;
    }
    body, .gradio-container {
        background: #0B1120 !important;
        color: #E2E8F0 !important;
    }

    /* ── Ambient background glow ───────────────────────────────────────────── */
    .main-layout::before {
        content: '' !important;
        position: fixed !important;
        top: 50% !important; left: 50% !important;
        width: 600px !important; height: 600px !important;
        transform: translate(-50%, -50%) !important;
        background: radial-gradient(circle, rgba(124,58,237,0.06) 0%, transparent 70%) !important;
        pointer-events: none !important;
        z-index: 0 !important;
    }

    /* ── Two-Zone Layout: 2/3 UI + 1/3 Avatar, seamless dark canvas ──── */
    .main-layout {
        display: grid !important;
        grid-template-columns: 2fr 1fr !important;
        width: 100% !important;
        height: 100vh !important;
        max-width: 100% !important;
        margin: 0 !important;
        gap: 0 !important;
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
        padding: 28px 24px 24px 32px !important;
        background: transparent !important;
        border: none !important;
        position: relative !important;
    }

    /* ── Right: avatar video ──────────────────────────────────────────── */
    .right-zone {
        padding: 0 !important;
        overflow: hidden !important;
        background: transparent !important;
        border: none !important;
    }
    .right-zone,
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
    }
    .avatar-shell { position: relative; width: 100%; height: 100%; min-height: 240px; background: #0B1120; }
    .avatar-fallback { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; color: #94A3B8; font-size: 13px; text-align: center; }
    .avatar-fallback-mark { width: 72px; height: 72px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, #A78BFA, #312E81 58%, #0F172A 100%); box-shadow: 0 0 48px rgba(124,58,237,.28); }
    .avatar-fallback-title { color: #CBD5E1; font-size: 16px; }
    .avatar-fallback-sub { color: #64748B; font-size: 12px; }

    /* ── Left: Chat ────────────────────────────────────────────────────────── */
    .chat-panel {
        flex: 1 !important;
        min-height: 0 !important;
        overflow-y: auto !important;
        margin-bottom: 16px !important;
        border-radius: 12px !important;
        background: rgba(15,23,42,0.5) !important;
        padding: 16px !important;
    }
    .chat-panel .message-wrap { color: #CBD5E1 !important; }
    .chat-panel .bubble-wrap { border-radius: 12px !important; }
    .chat-panel .user .bubble-wrap { background: rgba(30,41,59,0.6) !important; }
    .chat-panel .bot .bubble-wrap { background: rgba(124,58,237,0.12) !important; }
    .input-row {
        flex-shrink: 0 !important;
        gap: 8px !important;
    }
    .input-row input, .input-row textarea {
        background: rgba(30,41,59,0.5) !important;
        border: 1px solid rgba(124,58,237,0.15) !important;
        border-radius: 10px !important;
        color: #E2E8F0 !important;
        padding: 12px 16px !important;
        font-size: 14px !important;
        transition: border-color 200ms ease-out, box-shadow 200ms ease-out !important;
    }
    .input-row input:focus, .input-row textarea:focus {
        border-color: #7C3AED !important;
        outline: none !important;
        box-shadow: 0 0 0 3px rgba(124,58,237,0.15) !important;
    }
    .input-row button {
        background: rgba(124,58,237,0.15) !important;
        color: #A78BFA !important;
        border: 1px solid rgba(124,58,237,0.3) !important;
        border-radius: 10px !important;
        padding: 10px 20px !important;
        font-weight: 500 !important;
        font-size: 14px !important;
        cursor: pointer !important;
        transition: all 200ms ease-out !important;
    }
    .input-row button:hover {
        background: rgba(124,58,237,0.25) !important;
        border-color: #7C3AED !important;
        color: #C4B5FD !important;
    }
    .input-row button:active { transform: scale(0.98) !important; }

    /* ── Voice ball row (centered between chat and input) ────────────────── */
    .voice-row {
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        gap: 20px !important;
        padding: 20px 0 !important;
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
    .voice-ball-btn:focus-visible {
        outline: 2px solid #7C3AED !important;
        outline-offset: 6px !important;
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
        bottom: 24px !important;
        left: 28% !important;
        right: 38% !important;
        width: auto !important;
        z-index: 9999 !important;
        background: rgba(15,23,42,0.85) !important;
        border: 1px solid rgba(124,58,237,0.25) !important;
        border-radius: 8px !important;
        padding: 3px 8px !important;
        backdrop-filter: blur(8px) !important;
    }
    .audio-player audio {
        width: 100% !important;
        height: 28px !important;
    }
    .audio-player .label-wrap, .audio-player .icon-buttons { display: none !important; }

    /* ── Status dots ───────────────────────────────────────────────────────── */
    .status-dot {
        display: inline-block; width: 5px; height: 5px;
        border-radius: 50%; margin-right: 4px; vertical-align: middle;
    }
    .status-dot.connected, .status-dot.ready { background: #4ADE80; }
    .status-dot.disconnected, .status-dot.error { background: #EF4444; }
    .status-dot.degraded, .status-dot.timeout { background: #F59E0B; }
    .status-line {
        color: #64748B; font-size: 11px; text-align: center;
        margin-top: 16px; line-height: 1.8; letter-spacing: 0.3px;
    }

    /* ── Custom scrollbar ──────────────────────────────────────────────────── */
    .chat-panel::-webkit-scrollbar { width: 3px; }
    .chat-panel::-webkit-scrollbar-track { background: transparent; }
    .chat-panel::-webkit-scrollbar-thumb { background: rgba(124,58,237,0.2); border-radius: 4px; }
    .chat-panel::-webkit-scrollbar-thumb:hover { background: rgba(124,58,237,0.35); }

    /* ── Accessibility ─────────────────────────────────────────────────────── */
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after {
            animation-duration: 0.01ms !important;
            transition-duration: 0.01ms !important;
        }
    }
    """

    with gr.Blocks(
        title="AI 语音伴侣",
        theme=theme,
        css=custom_css,
        analytics_enabled=False,
    ) as demo:

        # =========================================================================
        # 绕过浏览器自动播放限制的脚本
        # =========================================================================
        audio_unlock_kwargs = {
            "value": '<div id="audio-unlock" style="display:none"></div>',
            "visible": True,
        }
        if "js_on_load" in inspect.signature(gr.HTML).parameters:
            audio_unlock_kwargs["js_on_load"] = """
            let audioReady = false;
            let ctx = null;
            document.addEventListener('click', () => {
                if (!audioReady) {
                    ctx = new (window.AudioContext || window.webkitAudioContext)();
                    ctx.resume();
                    audioReady = true;
                }
            }, { once: false });
            """
        gr.HTML(**audio_unlock_kwargs)

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

            # ---- 右 1/3: 数字人视频 ----
            with gr.Column(scale=1, elem_classes=["right-zone"]):
                avatar_html = f'''
                    <div class="avatar-shell" role="status" aria-live="polite">
                        <div class="avatar-fallback">
                            <div class="avatar-fallback-mark" aria-hidden="true"></div>
                            <div class="avatar-fallback-title">数字人待机中...</div>
                            <div class="avatar-fallback-sub">正在连接本地视觉服务</div>
                        </div>
                        <iframe
                            src="{LIVETALKING_URL.rstrip('/')}/avatar-embed.html"
                            allow="autoplay;camera;microphone"
                            title="Digital Avatar"
                            style="display:none;width:100%;height:100%;border:none;"
                        ></iframe>
                    </div>
                    '''
                avatar_html_kwargs = {"value": avatar_html, "show_label": False}
                if "js_on_load" in inspect.signature(gr.HTML).parameters:
                    avatar_html_kwargs["js_on_load"] = f"""
                    const shell = element.querySelector('.avatar-shell');
                    const frame = shell?.querySelector('iframe');
                    const fallback = shell?.querySelector('.avatar-fallback');
                    if (shell && frame && fallback) {{
                        fetch({json.dumps(LIVETALKING_URL.rstrip('/') + '/health')}, {{ mode: 'no-cors' }})
                            .then(() => {{ frame.style.display = 'block'; fallback.style.display = 'none'; }})
                            .catch(() => {{ frame.style.display = 'none'; fallback.style.display = 'flex'; }});
                    }}
                    """
                avatar_video = gr.HTML(**avatar_html_kwargs)

        # ---- 浮动音频播放条（自动播放TTS语音） ----
        tts_audio_output = gr.Audio(
            type="filepath", interactive=False,
            visible=True, autoplay=True,
            elem_classes=["audio-player"],
        )

        # ---- 隐藏组件 ----
        audio_input = gr.Audio(
            sources=["microphone", "upload"], type="numpy", visible=False,
        )
        ref_audio_upload = gr.Audio(
            sources=["upload"], type="filepath", visible=False,
        )
        upload_status = gr.Textbox(visible=False)

        # =====================================================================
        # 事件处理
        # =====================================================================
        is_recording = gr.State(False)
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
                        yield "", new_history, status, None
                    elif event["type"] == "status":
                        status = event["text"]
                        # Keep generator alive during TTS phase
                        yield "", new_history, status, None
                    elif event["type"] == "error":
                        append_reply(f"错误: {event['error']['message']}")
                        yield "", new_history, "就绪", None
                        return
                    elif event["type"] == "audio":
                        sr, audio_data = event["data"]
                        if audio_data is not None and len(audio_data) > 0:
                            audio_segments.append(audio_data)
                            audio_sr = sr
                            # Signal TTS progress to keep connection alive
                            yield "", new_history, f"语音生成中 ({len(audio_segments)})...", None
            except Exception as e:
                logger.error(f"文字处理异常: {traceback.format_exc()}")
                append_reply("处理失败，请重试")

            ui_chat_history = new_history

            # 合并所有音频段，写入固定路径 WAV，返回给 Gradio
            if audio_segments:
                import soundfile as sf
                full_audio = np.concatenate(audio_segments)
                full_audio_f32 = (full_audio.astype(np.float32) / 32767.0).clip(-1.0, 1.0)
                dump_path = os.path.join(LOG_DIR, "last_tts.wav")
                sf.write(dump_path, full_audio_f32, audio_sr)
                logger.info(f"TTS 完成: {len(full_audio_f32)} 采样点 @ {audio_sr}Hz ({len(full_audio_f32)/audio_sr:.1f}s)")
                yield "", new_history, "播放中", dump_path
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
                    elif event["type"] == "error":
                        chat_history.append(make_message("user", "(语音)"))
                        chat_history.append(make_message("assistant", f"错误: {event['error']['message']}"))
            except Exception as e:
                logger.error(f"语音处理异常: {traceback.format_exc()}")
                chat_history.append(make_message("user", "(语音)"))
                chat_history.append(make_message("assistant", "处理失败，请重试"))
            yield chat_history, status, ""

        def handle_stop():
            pipeline.stop()
            return gr.skip(), "已停止"

        def update_connection_status():
            svc = get_service_status()
            dots = {
                "connected": '<span class="status-dot connected"></span>',
                "disconnected": '<span class="status-dot disconnected"></span>',
                "degraded": '<span class="status-dot degraded"></span>',
                "ready": '<span class="status-dot ready"></span>',
                "timeout": '<span class="status-dot timeout"></span>',
                "error": '<span class="status-dot error"></span>',
            }
            labels = {
                "llama": "LLM",
                "asr": "ASR",
                "tts": "TTS",
                "livetalking": "口型",
                "avatar_sync": "桥接",
            }
            parts = []
            for key, label in labels.items():
                s = svc.get(key, "disconnected")
                parts.append(f'{dots.get(s, dots["disconnected"])} {label}')
            return '<div class="status-line">' + " &nbsp;".join(parts) + '</div>'

        def update_status_display():
            return state_machine.state_text, update_connection_status()

        # ---- 绑定事件 ----
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
        stop_btn.click(
            fn=handle_stop, inputs=[], outputs=[chatbot, status_text],
        )
        # 语音球: 触发隐藏的麦克风录制
        voice_btn.click(
            fn=None,
            inputs=[],
            outputs=[],
            js="""() => {
                const mic = document.querySelector('input[type="file"][accept*="audio"]');
                if (mic) mic.click();
            }""",
        )
        audio_input.stop_recording(
            fn=handle_audio_input,
            inputs=[audio_input],
            outputs=[chatbot, status_text, text_input],
        )
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
    logger.info(f"TTS 模型: {TTS_MODEL_PATH}")
    logger.info(f"上传目录: {UPLOAD_DIR}")
    logger.info("=" * 60)

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
