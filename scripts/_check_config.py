import sys, json, re

html = sys.stdin.read()
m = re.search(r'window\.gradio_config = ({.*?});</script>', html, re.DOTALL)
if m:
    config = json.loads(m.group(1))
    for c in config['components']:
        t = c.get('type','')
        props = c.get('props',{})
        if t in ('audio','textbox','html'):
            label = props.get('label','')
            visible = props.get('visible', True)
            classes = props.get('elem_classes', [])
            value = props.get('value', '')[:80] if props.get('value') else '-'
            print(f"id={c['id']:3d} type={t:8s} label={str(label):20s} visible={visible} classes={classes} value={value}")
else:
    print("No config found")
