"""Small local-only VoxCPM TTS worker.

The worker deliberately keeps the HTTP protocol independent from FastAPI and
from the optional VoxCPM dependency so that protocol checks can run anywhere.
It loads exactly one local checkpoint and never downloads a model.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import numpy as np

PROTOCOL_VERSION = "tts.v2"
SERVICE_VERSION = "voxcpm-worker.v1"
PROFILE_SPECS = {
    "balanced-v2": {"model_id": "VoxCPM2", "sample_rate": 48000},
    "safe-v15": {"model_id": "VoxCPM1.5", "sample_rate": 44100},
}
EXPECTED_SAMPLE_RATES = {"VoxCPM2": 48000, "VoxCPM1.5": 44100}
log = logging.getLogger("voxcpm-worker")


def now_ms() -> int:
    return int(time.time() * 1000)


def _package_version(package: str) -> Optional[str]:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def json_line(value: Dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def profile_spec(profile: str, model_id: str) -> Dict[str, Any]:
    if profile not in PROFILE_SPECS:
        raise ValueError(f"unsupported VOXCPM_PROFILE: {profile}")
    if model_id not in EXPECTED_SAMPLE_RATES:
        raise ValueError(f"unsupported VOXCPM_MODEL_ID: {model_id}")
    spec = dict(PROFILE_SPECS[profile])
    if spec["model_id"] != model_id:
        raise ValueError(f"profile {profile} requires model_id {spec['model_id']}")
    return spec


class GenerationRegistry:
    """Tracks the active generation and gives cancellation a generation key."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: Optional[str] = None
        self._events: Dict[str, threading.Event] = {}

    def start(self, generation_id: str) -> threading.Event:
        with self._lock:
            if self._active is not None:
                raise RuntimeError("TTS_QUEUE_001")
            event = threading.Event()
            self._active = generation_id
            self._events[generation_id] = event
            return event

    def cancel(self, generation_id: str) -> str:
        with self._lock:
            event = self._events.get(generation_id)
            if event is None:
                return "already_finished"
            event.set()
            return "cancelling"

    def finish(self, generation_id: str) -> None:
        with self._lock:
            self._events.pop(generation_id, None)
            if self._active == generation_id:
                self._active = None

    @property
    def active_generation_id(self) -> Optional[str]:
        with self._lock:
            return self._active


class VoxCPMRuntime:
    def __init__(self, model_path: str, model_id: str, profile: str, ref_wav: str = "", ref_text: str = "", configured_sample_rate: int = 0, reference_root: str = "", inference_timesteps: int = 4) -> None:
        self.model_path = str(Path(model_path).expanduser().resolve())
        self.model_id = model_id
        self.profile = profile
        self.ref_wav = str(Path(ref_wav).expanduser().resolve()) if ref_wav else ""
        self.reference_root = Path(reference_root).expanduser().resolve() if reference_root else None
        self.ref_text = ref_text
        self.configured_sample_rate = int(configured_sample_rate or EXPECTED_SAMPLE_RATES[model_id])
        self.inference_timesteps = max(1, int(inference_timesteps))
        self.model = None
        self.actual_sample_rate: Optional[int] = None
        self.error: Optional[str] = None
        self.registry = GenerationRegistry()

    def load(self) -> None:
        """Load only an existing local model, on CUDA, with no fallback."""
        try:
            import torch
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA is unavailable; refusing CPU fallback")
            if not os.path.isdir(self.model_path):
                raise RuntimeError(f"local model directory not found: {self.model_path}")
            from voxcpm import VoxCPM
            # Keep device selection explicit: a missing CUDA setup must not
            # silently fall back to CPU and break the latency/VRAM contract.
            self.model = VoxCPM.from_pretrained(
                self.model_path,
                load_denoiser=False,
                local_files_only=True,
                device="cuda",
            )
            self.actual_sample_rate = int(self.model.tts_model.sample_rate)
            expected = EXPECTED_SAMPLE_RATES[self.model_id]
            if self.actual_sample_rate != expected:
                raise RuntimeError(f"model {self.model_id} reported {self.actual_sample_rate}Hz, expected {expected}Hz")
            if self.configured_sample_rate != self.actual_sample_rate:
                raise RuntimeError(f"configured sample rate {self.configured_sample_rate}Hz does not match {self.actual_sample_rate}Hz")
            log.info("loaded local %s model=%s sample_rate=%s", self.profile, self.model_id, self.actual_sample_rate)
        except Exception as exc:
            self.error = str(exc)
            self.model = None
            log.error("VoxCPM worker is not ready: %s", exc)

    @property
    def ready(self) -> bool:
        return self.model is not None and self.actual_sample_rate is not None and self.error is None

    def health(self) -> Dict[str, Any]:
        spec = profile_spec(self.profile, self.model_id)
        active = self.registry.active_generation_id
        gpu = {"peak_gib": None, "budget_gib": 15.0}
        if self.ready:
            try:
                import torch
                gpu["peak_gib"] = round(torch.cuda.max_memory_allocated() / (1024 ** 3), 3)
            except Exception:
                pass
        return {
            "status": "busy" if active and self.ready else ("ready" if self.ready else "error"),
            "service": "voxcpm-worker",
            "service_version": SERVICE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "ready": self.ready,
            "model": {"id": self.model_id, "role": "default" if self.profile == "balanced-v2" else "standby", "profile": self.profile, "sample_rate": self.actual_sample_rate or spec["sample_rate"]},
            "reference_id": self.reference_id(),
            "streaming": True,
            "audio_format": "pcm_s16le_mono",
            "device": "cuda" if self.ready else "cuda_required",
            "queue": {"active": bool(active), "depth": 1 if active else 0, "max_depth": 1},
            "gpu": gpu,
            "active_generation_id": active,
            "timestamp_ms": now_ms(),
            "error": self.error,
        }

    def model_revision(self) -> Optional[str]:
        manifest = Path(self.model_path) / "config.json"
        if not manifest.is_file():
            return None
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()[:16]
        return f"local-manifest-sha256:{digest}"

    def version(self) -> Dict[str, Any]:
        spec = profile_spec(self.profile, self.model_id)
        return {
            "service": "voxcpm-worker",
            "service_version": SERVICE_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "model_id": self.model_id,
            "profile": self.profile,
            "sample_rate": self.actual_sample_rate or spec["sample_rate"],
            "audio_format": "pcm_s16le_mono",
            "model_revision": self.model_revision(),
            "voxcpm_package_version": _package_version("voxcpm"),
            "implementation": "from voxcpm import VoxCPM; generate_streaming",
            "local_files_only": True,
            "features": {
                "streaming": True,
                "reference_audio": True,
                "reference_text": True,
                "isolated_reference": self.model_id == "VoxCPM2",
                "controllable_cloning": self.model_id == "VoxCPM2",
                "voice_design": self.model_id == "VoxCPM2",
            },
        }

    def reference_id(self) -> Optional[str]:
        if not self.ref_wav:
            return None
        return uuid.uuid5(uuid.NAMESPACE_URL, self.ref_wav).hex[:12]

    def update_reference(self, path: str, prompt_text: str = "") -> None:
        candidate = str(Path(path).expanduser().resolve())
        if not os.path.isfile(candidate):
            raise FileNotFoundError("reference wav not found")
        if self.reference_root is not None:
            try:
                Path(candidate).relative_to(self.reference_root)
            except ValueError as exc:
                raise PermissionError("reference wav is outside the configured upload directory") from exc
        self.ref_wav = candidate
        if prompt_text.strip():
            self.ref_text = prompt_text.strip()

    def synthesize(self, text: str, request_id: str, conversation_id: str, generation_id: str, cancel: threading.Event) -> Iterator[Dict[str, Any]]:
        if not self.ready:
            yield {"type": "error", "request_id": request_id, "generation_id": generation_id, "error": {"code": "TTS_MODEL_001", "message": self.error or "model not ready", "retryable": True}}
            return
        kwargs: Dict[str, Any] = {
            "text": text,
            "inference_timesteps": self.inference_timesteps,
        }
        if self.ref_wav:
            if not os.path.isfile(self.ref_wav):
                yield {"type": "error", "request_id": request_id, "generation_id": generation_id, "error": {"code": "TTS_REF_001", "message": "reference wav not found"}}
                return
            if self.model_id == "VoxCPM2":
                # VoxCPM2 reference-only cloning avoids the continuation seam
                # created when the same clip is also supplied as prompt audio.
                # It still supports native parenthesized speed/style control.
                kwargs["reference_wav_path"] = self.ref_wav
            else:
                # VoxCPM1.5 has no isolated reference input and therefore keeps
                # the legacy prompt-audio continuation contract.
                kwargs["prompt_wav_path"] = self.ref_wav
                kwargs["prompt_text"] = self.ref_text
        started = now_ms()
        yield {"type": "generation.started", "request_id": request_id, "conversation_id": conversation_id, "generation_id": generation_id, "model_id": self.model_id, "sample_rate": self.actual_sample_rate, "audio_format": "pcm_s16le_mono", "reference_id": self.reference_id(), "started_at_ms": started}
        sequence = 0
        total_samples = 0
        try:
            pending: Optional[np.ndarray] = None

            def emit_chunk(audio: np.ndarray, is_last: bool) -> Dict[str, Any]:
                nonlocal sequence, total_samples
                pcm = np.clip(audio, -1.0, 1.0)
                pcm16 = (pcm * 32767.0).astype("<i2", copy=False)
                total_samples += int(pcm16.size)
                event = {"type": "audio.chunk", "request_id": request_id, "conversation_id": conversation_id, "generation_id": generation_id, "sequence": sequence, "sample_rate": self.actual_sample_rate, "audio_format": "pcm_s16le_mono", "duration_ms": int(round(pcm16.size * 1000 / self.actual_sample_rate)), "is_first": sequence == 0, "is_last": is_last, "audio_base64": base64.b64encode(pcm16.tobytes()).decode("ascii")}
                sequence += 1
                return event

            for raw in self.model.generate_streaming(**kwargs):
                if cancel.is_set():
                    yield {"type": "generation.cancelled", "request_id": request_id, "generation_id": generation_id, "reason": "user_interrupt", "last_sequence": sequence - 1, "cancelled_at_ms": now_ms()}
                    return
                audio = np.asarray(raw, dtype=np.float32).reshape(-1)
                if not audio.size:
                    continue
                if pending is not None:
                    yield emit_chunk(pending, False)
                pending = audio

            if pending is not None:
                yield emit_chunk(pending, True)
            yield {"type": "generation.completed", "request_id": request_id, "conversation_id": conversation_id, "generation_id": generation_id, "model_id": self.model_id, "sample_rate": self.actual_sample_rate, "audio_format": "pcm_s16le_mono", "chunks": sequence, "audio_duration_ms": int(round(total_samples * 1000 / self.actual_sample_rate)), "first_audio_latency_ms": None, "completed_at_ms": now_ms()}
        except Exception as exc:
            log.exception("generation failed")
            yield {"type": "error", "request_id": request_id, "generation_id": generation_id, "error": {"code": "TTS_STREAM_001", "message": str(exc), "retryable": True}}


class Handler(BaseHTTPRequestHandler):
    runtime: VoxCPMRuntime
    server_version = "voxcpm-worker"

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        if self.path == "/api/tts/health":
            health = self.runtime.health()
            self._send_json(health, 200 if health["ready"] else 503)
        elif self.path == "/api/tts/version":
            self._send_json(self.runtime.version())
        else:
            self._send_json({"error": {"code": "NOT_FOUND", "message": "unknown endpoint"}}, 404)

    def do_POST(self) -> None:
        if self.path == "/api/tts/reference-audio":
            try:
                body = self._body()
                self.runtime.update_reference(
                    str(body.get("path", "")),
                    str(body.get("prompt_text", "")),
                )
                self._send_json({"ok": True, "reference_id": self.runtime.reference_id()})
            except Exception as exc:
                self._send_json({"error": {"code": "TTS_REF_001", "message": str(exc)}}, 400)
            return
        if self.path.startswith("/api/tts/generations/") and self.path.endswith("/cancel"):
            generation_id = self.path.split("/")[-2]
            state = self.runtime.registry.cancel(generation_id)
            self._send_json({"protocol_version": PROTOCOL_VERSION, "generation_id": generation_id, "state": state})
            return
        if self.path != "/api/tts/synthesize":
            self._send_json({"error": {"code": "NOT_FOUND", "message": "unknown endpoint"}}, 404)
            return
        try:
            body = self._body()
            text = str(body.get("text", "")).strip()
            if not text:
                raise ValueError("text is required")
            request_id = str(body.get("request_id") or "req_" + uuid.uuid4().hex)
            conversation_id = str(body.get("conversation_id") or "conv_" + uuid.uuid4().hex)
            generation_id = str(body.get("generation_id") or "gen_" + uuid.uuid4().hex)
            cancel = self.runtime.registry.start(generation_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for event in self.runtime.synthesize(text, request_id, conversation_id, generation_id, cancel):
                self.wfile.write(json_line(event))
                self.wfile.flush()
        except RuntimeError as exc:
            self._send_json({"error": {"code": str(exc), "message": "another generation is active"}}, 409)
        except Exception as exc:
            self._send_json({"error": {"code": "TTS_REQ_001", "message": str(exc)}}, 400)
        finally:
            if 'generation_id' in locals():
                self.runtime.registry.finish(generation_id)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


def build_runtime(args: argparse.Namespace) -> VoxCPMRuntime:
    model_id = args.model_id
    profile_spec(args.profile, model_id)
    return VoxCPMRuntime(
        args.model_path,
        model_id,
        args.profile,
        args.ref_wav,
        args.ref_text,
        args.sample_rate,
        args.reference_root,
        args.inference_timesteps,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-only VoxCPM streaming TTS worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--model-id", choices=sorted(EXPECTED_SAMPLE_RATES), default="VoxCPM2")
    parser.add_argument("--profile", choices=sorted(PROFILE_SPECS), default="balanced-v2")
    parser.add_argument("--ref-wav", default="")
    parser.add_argument("--ref-text", default="")
    parser.add_argument("--reference-root", default="")
    parser.add_argument("--sample-rate", type=int, default=0)
    parser.add_argument("--inference-timesteps", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.inference_timesteps <= 100:
        parser.error("--inference-timesteps must be between 1 and 100")
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    runtime = build_runtime(args)
    runtime.load()
    if not runtime.ready:
        raise SystemExit("VoxCPM worker not started: " + (runtime.error or "model unavailable"))
    Handler.runtime = runtime
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
