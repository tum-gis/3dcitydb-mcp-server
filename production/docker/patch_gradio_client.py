import pathlib, re

f = pathlib.Path("/usr/local/lib/python3.12/site-packages/gradio_client/utils.py")
txt = f.read_text()
patched = 0

# Patch 1: get_type() bool guard
old1 = 'def get_type(schema: dict):\n    if "const" in schema:'
new1 = 'def get_type(schema: dict):\n    if isinstance(schema, bool): return "bool"\n    if "const" in schema:'
if old1 in txt and new1 not in txt:
    txt = txt.replace(old1, new1, 1); patched += 1

# Patch 2: _json_schema_to_python_type() bool guard — match any signature
m = re.search(r'(def _json_schema_to_python_type\([^\)]*\)[^\n]*\n)', txt)
if m:
    func_def = m.group(1)
    bool_guard = func_def + '    if isinstance(schema, bool): return "bool"\n'
    if bool_guard not in txt:
        txt = txt.replace(func_def, bool_guard, 1); patched += 1
    print(f"Patch 2 signature: {repr(func_def.strip())}")
else:
    print("ERROR: _json_schema_to_python_type not found")

f.write_text(txt)
print(f"gradio_client patched OK ({patched} fix(es) applied)")
