import statistics
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1] / "outputs" / "backend"))
from audio_playout import LiveTalkingAudioPlayout


class _Response:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {"code": 0}


class _RecordingPost:
    def __init__(self):
        self.calls = 0
        self.frames = []
        self.times = []
        self.started = threading.Event()

    def __call__(self, _url, **kwargs):
        self.calls += 1
        self.started.set()
        for payload in kwargs["data"]:
            self.frames.append(payload)
            self.times.append(time.monotonic())
        return _Response()


def _tone(sample_rate=48000, seconds=0.28):
    timeline = np.arange(round(sample_rate * seconds), dtype=np.float32) / sample_rate
    return (0.2 * np.sin(2 * np.pi * 440 * timeline)).astype(np.float32)


def _send_in_model_chunks(playout, audio, sample_rate=48000):
    for offset in range(0, audio.size, 4093):
        assert playout.push(audio[offset : offset + 4093], sample_rate)
    playout.finish_utterance()
    assert playout.wait_until_idle(timeout=3.0)


def test_clocked_playout_reuses_one_http_stream_across_utterances():
    post = _RecordingPost()
    playout = LiveTalkingAudioPlayout(
        "http://livetalking.test",
        prebuffer_ms=100,
        max_buffer_ms=1000,
        post=post,
    )
    assert post.started.wait(1.0)

    _send_in_model_chunks(playout, _tone())
    first_count = len(post.frames)
    _send_in_model_chunks(playout, _tone())
    playout.close()

    assert post.calls == 1
    assert first_count >= 13
    assert len(post.frames) >= 26
    assert all(len(frame) == 320 * 2 for frame in post.frames)
    assert playout.stats().utterances == 2
    assert playout.stats().underflows == 0


def test_playout_uses_twenty_millisecond_clock_and_fades_first_frame():
    post = _RecordingPost()
    playout = LiveTalkingAudioPlayout(
        "http://livetalking.test",
        prebuffer_ms=100,
        max_buffer_ms=1000,
        fade_in_ms=30,
        post=post,
    )
    _send_in_model_chunks(playout, _tone(seconds=0.32))
    playout.close()

    intervals = [right - left for left, right in zip(post.times, post.times[1:])]
    assert 0.015 <= statistics.median(intervals) <= 0.030

    peaks = [
        int(np.max(np.abs(np.frombuffer(frame, dtype="<i2").astype(np.int32))))
        for frame in post.frames
    ]
    assert peaks[0] < max(peaks[2:6])


def test_playout_does_not_start_before_prebuffer_threshold():
    post = _RecordingPost()
    playout = LiveTalkingAudioPlayout(
        "http://livetalking.test",
        prebuffer_ms=200,
        max_buffer_ms=1000,
        post=post,
    )
    # Less than the 200 ms threshold must remain buffered even after the
    # scheduler's periodic 500 ms wake-up.
    assert playout.push(_tone(seconds=0.12), 48000)
    time.sleep(0.65)
    assert post.frames == []

    playout.finish_utterance()
    assert playout.wait_until_idle(timeout=2.0)
    playout.close()
    assert len(post.frames) >= 5


def test_playout_primes_webrtc_with_silence_before_faded_speech():
    post = _RecordingPost()
    playout = LiveTalkingAudioPlayout(
        "http://livetalking.test",
        prebuffer_ms=100,
        max_buffer_ms=1000,
        fade_in_ms=80,
        lead_in_ms=60,
        post=post,
    )
    _send_in_model_chunks(playout, _tone(seconds=0.32))
    playout.close()

    first_frames = [np.frombuffer(frame, dtype="<i2") for frame in post.frames[:3]]
    assert len(first_frames) == 3
    assert all(np.count_nonzero(frame) == 0 for frame in first_frames)
    assert any(
        np.count_nonzero(np.frombuffer(frame, dtype="<i2")) > 0
        for frame in post.frames[3:]
    )
