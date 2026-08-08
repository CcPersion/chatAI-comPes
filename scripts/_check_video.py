import av
c = av.open("/root/setup/idle.mp4")
s = c.streams.video[0]
print(f"{s.width}x{s.height}, fps={s.average_rate}")
