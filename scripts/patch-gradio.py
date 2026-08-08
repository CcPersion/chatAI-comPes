"""Patch Gradio 4.44.1 + gradio_client 1.3.0 schema parsing for Python 3.12.

The bug: _json_schema_to_python_type receives schema=True (bool),
and get_type returns "Any", but the if-elif chain doesn't handle "Any"/"unknown",
so it falls through to raise APIInfoParseError.

Also: recursive calls inside the function may pass non-dict schemas.
"""
import sys
utils_file = sys.argv[1] if len(sys.argv) > 1 else "/root/setup/venv/lib/python3.12/site-packages/gradio_client/utils.py"

with open(utils_file, "r") as f:
    content = f.read()

patches = 0

# Fix 1: get_type — guard for non-dict
old1 = '''def get_type(schema: dict):
    if "const" in schema:'''
new1 = '''def get_type(schema: dict):
    if not isinstance(schema, dict):
        return "Any"
    if "const" in schema:'''
if old1 in content:
    content = content.replace(old1, new1)
    patches += 1
    print("1. get_type: bool guard ✓")

# Fix 2: get_type — return str instead of dict
old2 = '''    elif "type" not in schema:
        return {}
    else:'''
new2 = '''    elif "type" not in schema:
        return "unknown"
    else:'''
if old2 in content:
    content = content.replace(old2, new2)
    patches += 1
    print("2. get_type: dict→str ✓")

# Fix 3: json_schema_to_python_type entry guard
old3 = '''def json_schema_to_python_type(schema: Any) -> str:
    type_ = _json_schema_to_python_type(schema, schema.get("$defs"))'''
new3 = '''def json_schema_to_python_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "Any"
    type_ = _json_schema_to_python_type(schema, schema.get("$defs"))'''
if old3 in content:
    content = content.replace(old3, new3)
    patches += 1
    print("3. json_schema_to_python_type: dict guard ✓")

# Fix 4: _json_schema_to_python_type guard + handle "Any"/"unknown" types
old4 = '''def _json_schema_to_python_type(schema: Any, defs) -> str:
    """Convert the json schema into a python type hint"""
    if schema == {}:
        return "Any"
    type_ = get_type(schema)
    if type_ == {}:'''
new4 = '''def _json_schema_to_python_type(schema: Any, defs) -> str:
    """Convert the json schema into a python type hint"""
    if not isinstance(schema, dict):
        return "Any"
    if schema == {}:
        return "Any"
    type_ = get_type(schema)
    if type_ in ("Any", "unknown"):
        return "Any"
    if type_ == {}:'''
if old4 in content:
    content = content.replace(old4, new4)
    patches += 1
    print("4. _json_schema_to_python_type: guard + Any/unknown handler ✓")

with open(utils_file, "w") as f:
    f.write(content)
print(f"\nTotal patches: {patches}/4")
