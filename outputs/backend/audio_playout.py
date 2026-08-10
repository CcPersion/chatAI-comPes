"""Clocked, persistent PCM playout for the LiveTalking avatar.

VoxCPM produces audio in bursts, while LiveTalking consumes one 20 ms frame at
a time.  This module decouples those two clocks with a small jitter buffer and
a persistent HTTP request.  Resampling is stateful for the whole utterance, so
model chunk boundaries cannot reset the resampler and create clicks.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import numpy as np
import requests
import soxr


logger = logging.getLogger("voice-pipeline.playout")
_STOP = object()


@dataclass(frozen=True)
class PlayoutStats:
    utterances: int
    frames_sent: int
    underflows: int
    max_buffered_frames: int


class LiveTalkingAudioPlayout:
    """Keep one LiveTalking stream open and feed it at a stable audio clock."""

    def __init__(
        self,
        base_url: str,
        session_id: str = "0",
        *,
        output_rate: int = 16000,
        frame_ms: int = 20,
        prebuffer_ms: int = 1200,
        rebuffer_ms: int = 400,
        max_buffer_ms: int = 6000,
        gain: float = 1.0,
        fade_in_ms: int = 30,
        lead_in_ms: int = 0,
        post: Callable = requests.post,
    ) -> None:
        if output_rate <= 0 or frame_ms <= 0:
            raise ValueError("output_rate and frame_ms must be positive")
        if 1000 % frame_ms:
            raise ValueError("frame_ms must divide 1000 exactly")

        self.url = f"{base_url.rstrip('/')}/humanaudio/stream"
        self.session_id = str(session_id)
        self.output_rate = int(output_rate)
        self.frame_ms = int(frame_ms)
        self.frame_samples = self.output_rate * self.frame_ms // 1000
        self.prebuffer_frames = max(1, int(round(prebuffer_ms / frame_ms)))
        self.rebuffer_frames = max(1, int(round(rebuffer_ms / frame_ms)))
        self.max_buffer_frames = max(
            self.prebuffer_frames + 1,
            int(round(max_buffer_ms / frame_ms)),
        )
        self.gain = max(0.0, float(gain))
        self.fade_in_samples = max(
            0, int(round(self.output_rate * fade_in_ms / 1000))
        )
        self.lead_in_frames = max(0, int(round(lead_in_ms / frame_ms)))
        self._post = post

        # The logical size limit is enforced with the condition below.  Using
        # Queue(maxsize=...) while holding that condition could deadlock before
        # the playout thread gets a chance to consume the initial prebuffer.
        self._frames: queue.Queue[np.ndarray] = queue.Queue()
        # Keep only a few already-clocked frames between the playout thread and
        # requests.  A large wire queue would recreate bursty delivery.
        self._wire: queue.Queue[object] = queue.Queue(maxsize=4)
        self._condition = threading.Condition()
        self._closed = threading.Event()
        self._failed = threading.Event()
        self._playing = threading.Event()
        self._accepting = False
        self._producer_done = False
        self._resampler: Optional[soxr.ResampleStream] = None
        self._input_rate: Optional[int] = None
        self._pending = np.empty(0, dtype=np.float32)
        self._fade_remaining = 0

        self._utterances = 0
        self._frames_sent = 0
        self._underflows = 0
        self._max_buffered_frames = 0

        self._http_thread = threading.Thread(
            target=self._run_http,
            name="livetalking-http-stream",
            daemon=True,
        )
        self._playout_thread = threading.Thread(
            target=self._run_playout,
            name="livetalking-playout-clock",
            daemon=True,
        )
        self._http_thread.start()
        self._playout_thread.start()

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def stats(self) -> PlayoutStats:
        return PlayoutStats(
            utterances=self._utterances,
            frames_sent=self._frames_sent,
            underflows=self._underflows,
            max_buffered_frames=self._max_buffered_frames,
        )

    def _body(self) -> Iterator[bytes]:
        while True:
            payload = self._wire.get()
            if payload is _STOP:
                return
            yield payload  # type: ignore[misc]

    def _run_http(self) -> None:
        try:
            response = self._post(
                self.url,
                params={"sessionid": self.session_id},
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Audio-Sample-Rate": str(self.output_rate),
                    "X-Audio-Format": "pcm_s16le_mono",
                    "X-Audio-Frame-Ms": str(self.frame_ms),
                },
                data=self._body(),
                timeout=(5.0, None),
            )
            try:
                result = response.json()
            except (AttributeError, ValueError):
                result = {
                    "code": getattr(response, "status_code", 500),
                    "msg": getattr(response, "text", "")[:200],
                }
            status_code = int(getattr(response, "status_code", 500))
            if status_code != 200 or result.get("code", 0) != 0:
                self._failed.set()
                logger.warning(
                    "LiveTalking persistent stream failed HTTP %s: %s",
                    status_code,
                    result,
                )
        except requests.ConnectionError:
            self._failed.set()
            logger.warning("LiveTalking persistent stream is unreachable")
        except Exception:
            self._failed.set()
            logger.exception("LiveTalking persistent stream failed")
        finally:
            # A normal close also ends requests.post; only mark it as a failure
            # when the connection disappeared while this object was live.
            if not self._closed.is_set():
                self._failed.set()
            with self._condition:
                self._condition.notify_all()

    def _start_utterance_locked(self, input_rate: int) -> None:
        if self._accepting:
            if self._input_rate != input_rate:
                raise ValueError("sample rate changed within one utterance")
            return
        self._accepting = True
        self._producer_done = False
        self._input_rate = int(input_rate)
        self._resampler = soxr.ResampleStream(
            self._input_rate,
            self.output_rate,
            1,
            dtype="float32",
            quality="HQ",
        )
        self._pending = np.empty(0, dtype=np.float32)
        self._fade_remaining = self.fade_in_samples
        self._utterances += 1
        # Give WebRTC/LiveTalking a short run of clocked silence before speech.
        # This primes the downstream audio/video path without changing pitch or
        # inserting discontinuities into the first synthesized samples.
        for _ in range(self.lead_in_frames):
            self._put_frame_locked(
                np.zeros(self.frame_samples, dtype=np.float32)
            )

    def _apply_fade_locked(self, audio: np.ndarray) -> np.ndarray:
        if not audio.size or self._fade_remaining <= 0:
            return audio
        count = min(audio.size, self._fade_remaining)
        completed = self.fade_in_samples - self._fade_remaining
        ramp = (np.arange(count, dtype=np.float32) + completed + 1) / max(
            self.fade_in_samples, 1
        )
        output = audio.copy()
        output[:count] *= ramp
        self._fade_remaining -= count
        return output

    def _enqueue_resampled_locked(self, audio: np.ndarray) -> None:
        if not audio.size:
            return
        audio = self._apply_fade_locked(np.asarray(audio, dtype=np.float32))
        if self._pending.size:
            audio = np.concatenate((self._pending, audio))
        offset = 0
        while audio.size - offset >= self.frame_samples:
            frame = audio[offset : offset + self.frame_samples].copy()
            self._put_frame_locked(frame)
            offset += self.frame_samples
        self._pending = audio[offset:].copy()

    def _put_frame_locked(self, frame: np.ndarray) -> None:
        # Synthesis is allowed to run ahead, but only by a bounded amount.  The
        # condition wait releases the lock so the clock thread can drain data.
        deadline = time.monotonic() + 5.0
        while self._frames.qsize() >= self.max_buffer_frames:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Full
            self._condition.wait(timeout=min(0.1, remaining))
            if self.closed or self.failed:
                raise RuntimeError("playout stream closed while buffering")
        self._frames.put_nowait(frame)
        buffered = self._frames.qsize()
        self._max_buffered_frames = max(self._max_buffered_frames, buffered)
        self._condition.notify_all()

    def push(self, audio_data: np.ndarray, sample_rate: int) -> bool:
        if self.closed or self.failed:
            return False
        raw = np.asarray(audio_data).reshape(-1)
        if not raw.size:
            return True
        if np.issubdtype(raw.dtype, np.integer):
            audio = raw.astype(np.float32) / 32768.0
        else:
            audio = raw.astype(np.float32)
        # A stable utterance-level gain avoids the per-model-chunk pumping that
        # was especially audible during the first second of every reply.
        audio = np.clip(audio * self.gain, -0.95, 0.95)

        try:
            with self._condition:
                self._start_utterance_locked(int(sample_rate))
                assert self._resampler is not None
                converted = self._resampler.resample_chunk(audio, last=False)
                self._enqueue_resampled_locked(converted)
                self._condition.notify_all()
            return True
        except queue.Full:
            logger.warning("Avatar playout buffer remained full for 5 seconds")
        except Exception:
            logger.exception("Could not queue avatar audio")
        return False

    def finish_utterance(self) -> None:
        """Flush the resampler; the persistent HTTP connection remains open."""
        with self._condition:
            if not self._accepting:
                return
            if self._resampler is not None:
                tail = self._resampler.resample_chunk(
                    np.empty(0, dtype=np.float32), last=True
                )
                self._enqueue_resampled_locked(tail)
            if self._pending.size:
                frame = np.zeros(self.frame_samples, dtype=np.float32)
                frame[: self._pending.size] = self._pending
                # A short tail fade prevents the padded final frame from ending
                # on a non-zero sample and clicking at the utterance boundary.
                fade_count = min(self._pending.size, self.output_rate // 100)
                if fade_count:
                    frame[self._pending.size - fade_count : self._pending.size] *= (
                        np.linspace(1.0, 0.0, fade_count, dtype=np.float32)
                    )
                self._put_frame_locked(frame)
                self._pending = np.empty(0, dtype=np.float32)
            self._accepting = False
            self._producer_done = True
            self._resampler = None
            self._input_rate = None
            self._condition.notify_all()

    @staticmethod
    def _clear_queue(target: queue.Queue) -> None:
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                return

    def interrupt(self) -> None:
        """Drop queued speech immediately without tearing down the connection."""
        with self._condition:
            self._clear_queue(self._frames)
            self._clear_queue(self._wire)
            self._pending = np.empty(0, dtype=np.float32)
            self._resampler = None
            self._input_rate = None
            self._accepting = False
            self._producer_done = True
            self._playing.clear()
            # One zero frame gives the downstream mouth driver a clean stop.
            try:
                self._wire.put_nowait(
                    np.zeros(self.frame_samples, dtype="<i2").tobytes()
                )
            except queue.Full:
                pass
            self._condition.notify_all()

    def _wire_frame(self, frame: np.ndarray) -> bool:
        pcm = (np.clip(frame, -0.95, 0.95) * 32767.0).astype("<i2")
        while not self.closed and not self.failed:
            try:
                self._wire.put(pcm.tobytes(), timeout=0.1)
                self._frames_sent += 1
                return True
            except queue.Full:
                continue
        return False

    def _run_playout(self) -> None:
        frame_seconds = self.frame_ms / 1000.0
        while not self.closed:
            with self._condition:
                ready = self._condition.wait_for(
                    lambda: self.closed
                    or self.failed
                    or (
                        (self._frames.qsize() >= self.prebuffer_frames)
                        or (self._producer_done and not self._frames.empty())
                    ),
                    timeout=0.5,
                )
            if self.closed or self.failed:
                return
            if not ready:
                continue
            if self._frames.empty():
                continue

            self._playing.set()
            next_deadline = time.monotonic()
            utterance_underflows = 0
            fade_after_rebuffer = False
            while self._playing.is_set() and not self.closed and not self.failed:
                try:
                    frame = self._frames.get_nowait()
                    with self._condition:
                        self._condition.notify_all()
                except queue.Empty:
                    with self._condition:
                        done = self._producer_done
                    if done:
                        break
                    # Do not emit a train of isolated silence frames while the
                    # model is late: that is perceived as repeated stuttering.
                    # Pause once, rebuild a small safety margin, then resume on
                    # a fresh clock with a short fade-in.
                    with self._condition:
                        self._condition.wait_for(
                            lambda: self.closed
                            or self.failed
                            or self._producer_done
                            or self._frames.qsize() >= self.rebuffer_frames,
                            timeout=2.0,
                        )
                        done = self._producer_done
                        buffered = self._frames.qsize()
                    # The producer often marks completion immediately after the
                    # clock consumes the final frame. Do not report that normal
                    # tail race as a real buffer underrun.
                    if done and buffered == 0:
                        break
                    self._underflows += 1
                    utterance_underflows += 1
                    next_deadline = time.monotonic()
                    fade_after_rebuffer = True
                    continue

                if fade_after_rebuffer:
                    frame = frame.copy()
                    frame *= np.linspace(
                        0.0, 1.0, self.frame_samples, dtype=np.float32
                    )
                    fade_after_rebuffer = False

                if not self._wire_frame(frame):
                    return
                next_deadline += frame_seconds
                delay = next_deadline - time.monotonic()
                if delay > 0:
                    self._closed.wait(delay)
                elif delay < -frame_seconds:
                    # Do not attempt a bursty catch-up after scheduler stalls.
                    next_deadline = time.monotonic()

            self._playing.clear()
            if utterance_underflows:
                logger.warning(
                    "Avatar playout completed with %d buffer underflows",
                    utterance_underflows,
                )
            else:
                logger.info(
                    "Avatar playout completed without underflow; max_buffer=%dms",
                    self._max_buffered_frames * self.frame_ms,
                )

    def wait_until_idle(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (
                not self._accepting
                and self._producer_done
                and self._frames.empty()
                and not self._playing.is_set()
            ):
                return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        if self._closed.is_set():
            return
        self.interrupt()
        self._closed.set()
        with self._condition:
            self._condition.notify_all()
        self._clear_queue(self._wire)
        try:
            self._wire.put_nowait(_STOP)
        except queue.Full:
            pass
        self._playout_thread.join(timeout=2.0)
        self._http_thread.join(timeout=5.0)
