with open('/root/setup/LiveTalking/server/rtc_manager.py', 'r') as f:
    content = f.read()
content = content.replace('params.get(sessionid)', 'params.get("sessionid")')
with open('/root/setup/LiveTalking/server/rtc_manager.py', 'w') as f:
    f.write(content)
print('done')
