from PIL import Image
import os
d = "/root/setup/LiveTalking/data/avatars/wav2lip256_avatar1/face_imgs"
for f in sorted(os.listdir(d))[:1]:
    img = Image.open(os.path.join(d, f))
    print(f"{f}: {img.size}")
