"""Local HTTP NDJSON client for the VoxCPM worker."""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Iterator, Optional

import numpy as np


class VoxCPMClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        style_prompt: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.style_prompt = style_prompt.strip().strip("()（）")
        self.last_event: Optional[Dict[str, Any]] = None

    def prepare_text(self, text: str) -> str:
        """Apply VoxCPM2's native parenthesized speaking-style control."""
        cleaned = text.strip()
        if not cleaned or not self.style_prompt:
            return cleaned
        return f"({self.style_prompt}){cleaned}"

    def _get(self, path: str) -> Dict[str, Any]:
        with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def health(self) -> Dict[str, Any]:
        return self._get("/api/tts/health")

    def version(self) -> Dict[str, Any]:
        return self._get("/api/tts/version")

    def stream_synthesize(self, text: str, cancel_event: Optional[threading.Event] = None, *, request_id: Optional[str] = None, generation_id: Optional[str] = None, conversation_id: Optional[str] = None) -> Iterator[tuple[np.ndarray, int]]:
        """Yield ``(float32 mono PCM, actual sample_rate)`` and retain event metadata."""
        request_id = request_id or "req_" + uuid.uuid4().hex
        generation_id = generation_id or "gen_" + uuid.uuid4().hex
        conversation_id = conversation_id or "conv_" + uuid.uuid4().hex
        payload = json.dumps({"text": self.prepare_text(text), "request_id": request_id, "generation_id": generation_id, "conversation_id": conversation_id}).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/api/tts/synthesize", data=payload, headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"}, method="POST")
        expected_sequence = 0
        expected_sample_rate: Optional[int] = None
        self.last_event = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for line in response:
                    if cancel_event is not None and cancel_event.is_set():
                        self.cancel(generation_id)
                        return
                    event = json.loads(line.decode("utf-8"))
                    self.last_event = event
                    if event.get("generation_id") not in {None, generation_id}:
                        raise RuntimeError("TTS_STREAM_002: generation mismatch")
                    if event.get("type") == "audio.chunk":
                        if int(event.get("sequence", -1)) != expected_sequence:
                            raise RuntimeError("TTS_STREAM_002: audio sequence is not monotonic")
                        sample_rate = int(event["sample_rate"])
                        if expected_sample_rate is None:
                            expected_sample_rate = sample_rate
                        elif sample_rate != expected_sample_rate:
                            raise RuntimeError("TTS_STREAM_002: sample rate changed within generation")
                        audio = np.frombuffer(base64.b64decode(event["audio_base64"]), dtype="<i2").astype(np.float32) / 32767.0
                        expected_sequence += 1
                        yield audio, sample_rate
                    elif event.get("type") == "error":
                        raise RuntimeError(event.get("error", {}).get("message", "VoxCPM synthesis failed"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"VoxCPM worker HTTP {exc.code}: {detail}") from exc

    def synthesize(self, text: str, cancel_event: Optional[threading.Event] = None, **ids: str) -> Iterator[tuple[np.ndarray, int]]:
        return self.stream_synthesize(text, cancel_event, **ids)

    def cancel(self, generation_id: str) -> Dict[str, Any]:
        request = urllib.request.Request(self.base_url + f"/api/tts/generations/{generation_id}/cancel", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def cancel_generation(self, generation_id: str) -> Dict[str, Any]:
        return self.cancel(generation_id)

    def update_ref_audio(self, path: str, prompt_text: str = "") -> Dict[str, Any]:
        payload = json.dumps({"path": path, "prompt_text": prompt_text}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/tts/reference-audio",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
