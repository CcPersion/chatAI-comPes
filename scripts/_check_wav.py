import soundfile as sf
data, sr = sf.read("/root/setup/logs/last_tts.wav")
print(f"sr={sr}, shape={data.shape}, dtype={data.dtype}, min={data.min():.3f}, max={data.max():.3f}")
print(f"duration={len(data)/sr:.1f}s, ok={data.max() > 0.01}")
