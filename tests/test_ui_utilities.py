import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from outputs.backend import app


def test_recent_logs_are_allowlisted_and_capped(tmp_path, monkeypatch):
    log_path = tmp_path / "app.log"
    log_path.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    monkeypatch.setitem(app.LOG_SOURCE_PATHS, "应用", str(log_path))

    output = app.get_recent_logs("应用", limit=2)

    assert "【应用】" in output
    assert "line-1" not in output
    assert "line-2" in output and "line-3" in output


def test_recent_logs_rejects_unknown_source_without_reading_arbitrary_path():
    output = app.get_recent_logs("C:/should-not-be-read", limit=10)

    assert "【应用】" in output
    assert "should-not-be-read" not in output


def test_runtime_settings_do_not_expose_model_or_service_paths():
    rendered = app.render_runtime_settings(app.get_runtime_settings())

    assert "模型路径" not in rendered
    assert "127.0.0.1:8020" not in rendered
    assert "voice.yaml" not in rendered


def test_status_summary_and_details_include_non_color_state_text():
    status = {
        "overall": "partial",
        "llama": "connected",
        "asr": "ready",
        "tts": "timeout",
        "livetalking": "disconnected",
        "avatar_sync": "ready",
    }

    summary = app.format_status_summary(status)
    details = app.format_status_details(status)

    assert "部分服务异常" in summary
    assert "语音合成" in details and "超时" in details
    assert "数字人口型" in details and "未连接" in details


def test_utility_panel_updates_are_mutually_exclusive():
    drawer, title, settings, logs, status = app.toggle_utility_panel("logs")

    assert drawer["visible"] is True
    assert title == "日志"
    assert settings["visible"] is False
    assert logs["visible"] is True
    assert status["visible"] is False

    closed = app.close_utility_panel()
    assert all(update["visible"] is False for update in closed)


def test_apply_reference_audio_delegates_explicit_application(monkeypatch):
    captured = {}

    def fake_upload(file_path, prompt_text):
        captured.update(file_path=file_path, prompt_text=prompt_text)
        return {"ok": True, "message": "updated"}

    monkeypatch.setattr(app, "handle_audio_upload", fake_upload)

    result = app.apply_reference_audio("/tmp/reference.wav", "spoken reference")

    assert result["ok"] is True
    assert captured == {
        "file_path": "/tmp/reference.wav",
        "prompt_text": "spoken reference",
    }


def test_apply_reference_audio_requires_a_file():
    result = app.apply_reference_audio(None, "")

    assert result["error"]["code"] == "TTS_ERR_002"
    assert "参考音频" in result["error"]["message"]
