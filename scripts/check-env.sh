#!/bin/bash
echo "===== GPU ====="
/usr/lib/wsl/lib/nvidia-smi 2>&1 | head -1

echo ""
echo "===== Ollama ====="
CODE=$(curl -s -o /dev/null -w "%{http_code}" http://172.19.80.1:11434/api/tags 2>/dev/null)
echo "HTTP $CODE"

echo ""
echo "===== voice.yaml ====="
test -f /root/setup/voice.yaml && echo "EXISTS" || echo "MISSING"

echo ""
echo "===== venv ====="
test -f /root/setup/venv/bin/activate && echo "EXISTS" || echo "MISSING"

echo ""
echo "===== LiveTalking ====="
if [ -f /root/setup/LiveTalking/requirements.txt ]; then
  echo "EXISTS"
  ls /root/setup/LiveTalking/*.py 2>/dev/null | head -5
else
  echo "MISSING"
fi

echo ""
echo "===== 资产 ====="
for f in wav2lip256.pth idle.mp4 ref.wav; do
  test -f /root/setup/$f && echo "$f: OK" || echo "$f: MISSING"
done

echo ""
echo "===== Node.js ====="
node --version 2>/dev/null || echo "MISSING"

echo ""
echo "===== 端口 ====="
ss -tlnp 2>/dev/null | grep -E "7860|8010|8011" || echo "无占用"

echo ""
echo "===== app.py ====="
test -f /home/chatAI-comPes/outputs/backend/app.py && echo "EXISTS" || echo "MISSING"

echo ""
echo "===== avatar-sync.js ====="
test -f /home/chatAI-comPes/scripts/avatar-sync.js && echo "EXISTS" || echo "MISSING"
