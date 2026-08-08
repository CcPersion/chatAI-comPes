"""
=============================================================================
app_minimal.py — 最简文字聊天版本
=============================================================================
只保留: 聊天记录 + 文字输入 + 发送 + 状态显示 + 连接检查
=============================================================================
"""
import os, sys, json, time, threading, logging, traceback, re, uuid
from pathlib import Path
from typing import Generator, List

import yaml
import httpx
import gradio as gr

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s [%(name)s] %(message)s')
logger = logging.getLogger("chat")

# ---- 配置加载 ----
def find_config():
    for p in [
        os.path.expanduser("~/setup/voice.yaml"),
        os.path.join(os.path.dirname(__file__), "..", "..", "config", "voice.yaml"),
    ]:
        if os.path.isfile(os.path.expanduser(p)):
            return os.path.expanduser(p)
    raise RuntimeError("找不到 voice.yaml")

cfg_path = find_config()
with open(cfg_path, "r") as f:
    config = yaml.safe_load(f)
logger.info(f"配置: {cfg_path}")

LLM_BASE_URL = str(config.get("LLM_BASE_URL", "http://localhost:11434")).rstrip("/")
LLM_MODEL = str(config.get("LLM_MODEL", "qwen2.5:7b-instruct"))
LLM_MAX_TOKENS = int(config.get("LLM_MAX_TOKENS", 1024))
LLM_TEMPERATURE = float(config.get("LLM_TEMPERATURE", 0.7))

# ---- LLM 客户端 ----
class LLMClient:
    def __init__(self):
        self.base_url = LLM_BASE_URL
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, connect=5.0))

    def stream_chat(self, messages: list, cancel_event: threading.Event) -> Generator[str, None, None]:
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": LLM_MODEL, "messages": messages, "stream": True,
            "temperature": LLM_TEMPERATURE, "max_tokens": LLM_MAX_TOKENS,
        }
        try:
            with self.client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"LLM 服务异常 HTTP {resp.status_code}")
                for line in resp.iter_lines():
                    if cancel_event.is_set():
                        break
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue
        except httpx.ConnectError:
            raise RuntimeError("无法连接 Ollama，请确认 ollama serve 正在运行")
        except Exception as e:
            raise RuntimeError(f"LLM 异常: {e}")

llm = LLMClient()

# ---- 对话管线 ----
class ChatPipeline:
    def __init__(self):
        self.history = [{"role": "system", "content": "你是一个友善、体贴的AI语音助手，说话温柔自然，用中文回复。"}]
        self._cancel = threading.Event()

    def chat(self, text: str):
        self._cancel.clear()
        self.history.append({"role": "user", "content": text})
        full_reply = ""
        try:
            for token in llm.stream_chat(self.history, self._cancel):
                if self._cancel.is_set():
                    break
                full_reply += token
                yield full_reply
        except Exception as e:
            yield f"错误: {e}"
            return
        if not self._cancel.is_set() and full_reply.strip():
            self.history.append({"role": "assistant", "content": full_reply})
            if len(self.history) > 41:
                self.history = [self.history[0]] + self.history[-40:]

    def stop(self):
        self._cancel.set()

    def clear(self):
        self.history = self.history[:1]

pipeline = ChatPipeline()

# ---- 服务连接状态 ----
def get_status():
    import requests
    status = {}
    try:
        r = requests.get(f"{LLM_BASE_URL}/api/tags", timeout=3)
        status["大模型 (Ollama)"] = "connected" if r.status_code == 200 else "degraded"
    except:
        status["大模型 (Ollama)"] = "disconnected"
    return status

# ---- Gradio UI ----
def create_ui():
    css = """
    footer { display: none !important; }
    .chat-area { height: 500px !important; }
    """
    with gr.Blocks(title="AI 语音伴侣 - 文字聊天", css=css, analytics_enabled=False) as demo:
        gr.Markdown("# AI 语音伴侣 💬")
        gr.Markdown("输入文字，与 AI 对话")

        chatbot = gr.Chatbot(label="对话", height=500)
        status_bar = gr.Textbox(value="待机", label="状态", interactive=False)

        with gr.Row():
            text_input = gr.Textbox(placeholder="输入消息...", show_label=False, scale=5)
            send_btn = gr.Button("发送", variant="primary", scale=1)

        with gr.Row():
            stop_btn = gr.Button("停止", variant="stop")
            clear_btn = gr.Button("清空对话", variant="secondary")
            refresh_btn = gr.Button("刷新连接", variant="secondary")

        conn_html = gr.HTML("检测中...")

        # ---- 事件 ----
        def handle_send(text, history):
            if not text or not text.strip():
                return "", history, "待机"
            history = history or []
            new_hist = list(history)
            for reply in pipeline.chat(text.strip()):
                if new_hist:
                    new_hist[-1] = [text, reply]
                else:
                    new_hist.append([text, reply])
                yield "", new_hist, "回复中..."
            # 最终状态
            if new_hist and new_hist[-1][1] and new_hist[-1][1].startswith("错误"):
                yield "", new_hist, "错误"
            else:
                yield "", new_hist, "待机"

        def handle_stop():
            pipeline.stop()
            return "已停止"

        def handle_clear():
            pipeline.clear()
            return [], "已清空"

        def refresh_status():
            svc = get_status()
            html = '<div style="font-size:13px;line-height:1.8;">'
            for name, state in svc.items():
                color = "#4ade80" if state in ("connected",) else "#f87171"
                dot = f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:{color};margin-right:4px;"></span>'
                html += f"{dot} {name}: {state}<br>"
            html += "</div>"
            return html

        send_btn.click(handle_send, [text_input, chatbot], [text_input, chatbot, status_bar])
        text_input.submit(handle_send, [text_input, chatbot], [text_input, chatbot, status_bar])
        stop_btn.click(handle_stop, [], [status_bar])
        clear_btn.click(handle_clear, [], [chatbot, status_bar])
        refresh_btn.click(refresh_status, [], [conn_html])
        demo.load(refresh_status, [], [conn_html])

    return demo

if __name__ == "__main__":
    logger.info(f"启动文字聊天, LLM: {LLM_BASE_URL}/v1/chat/completions, model: {LLM_MODEL}")
    demo = create_ui()
    demo.queue(default_concurrency_limit=1).launch(
        server_name="127.0.0.1", server_port=7860,
        share=False, inbrowser=False,
    )
