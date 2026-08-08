"""Test gradio chat endpoint."""
from gradio_client import Client

client = Client("http://127.0.0.1:7860")
print("Connected OK")

# Test text chat
result = client.predict("你好，用一句话介绍自己", [], api_name="/handle_send")
# result is (text, chatbot_history, status)
print(f"Status: {result[2]}")
if result[1]:
    last = result[1][-1]
    print(f"User: {last[0]}")
    print(f"AI: {last[1][:200]}")
else:
    print("No reply")
print("DONE")
