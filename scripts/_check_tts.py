import torch
from qwen_tts import Qwen3TTSModel

print("Loading model...")
model = Qwen3TTSModel.from_pretrained(
    "/root/setup/models/Qwen3-TTS-1.7B",
    device_map="cuda:0",
    dtype=torch.bfloat16,
    local_files_only=True,
)
print("Loaded.")

# List all public methods that might generate audio
all_methods = [m for m in dir(model) if not m.startswith('_') and callable(getattr(model, m, None))]
print("Public methods:")
for m in sorted(all_methods):
    print(f"  {m}")

# Also check for generate-related attributes
gen_methods = [m for m in all_methods if any(k in m.lower() for k in ['gen', 'speak', 'synthes', 'infer', 'forward', 'tts'])]
print(f"\nGeneration-related methods: {gen_methods}")
