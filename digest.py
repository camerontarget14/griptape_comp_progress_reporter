# Griptape Nodes "Execute Python" node.
# Wired input: nk_script_path  (injected as a bare variable in scope) later will wire this directly when published with GT nuke library.
# Assigns: result = {
#     "digest": str, "script_name": str, "shot_code": str,
#     "node_count": int, "comp_version": str, "has_write": bool,
#     "write_path_set": bool,
# }

import os
import re
from collections import Counter

nk_path = (nk_script_path or "").strip()
if not nk_path:
    raise ValueError("nk_script_path is required")
if not os.path.isfile(nk_path):
    raise FileNotFoundError(f"No file at: {nk_path}")

with open(nk_path, encoding="utf-8", errors="replace") as f:
    text = f.read()

script_name = os.path.basename(nk_path)

NODE_RE = re.compile(r"^([A-Za-z_]\w*)\s*\{", re.MULTILINE)


def split_blocks(s):
    blocks = []
    for m in NODE_RE.finditer(s):
        cls = m.group(1)
        depth, i = 1, m.end()
        while i < len(s) and depth:
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
            i += 1
        blocks.append((cls, s[m.end() : i - 1]))
    return blocks


def knob(body, name):
    m = re.search(rf'^\s*{name}\s+("[^"]*"|\S+)', body, re.MULTILINE)
    return m.group(1).strip('"') if m else ""


PRESENCE_ONLY = {
    "Roto",
    "RotoPaint",
    "Tracker4",
    "CameraTracker",
    "Keyer",
    "Primatte3",
    "IBKGizmoV3",
    "DeepRead",
    "SmartVector",
    "GridWarp3",
}
# Structural / non-operation / non-node classes excluded from histogram + counts.
STRUCTURAL = {"Root", "BackdropNode", "StickyNote", "define_window_layout_xml"}
SKIP_OPS = STRUCTURAL | {"Viewer", "Input", "Output"}

blocks = split_blocks(text)

# --- Root metadata ------------------------------------------------------
root_body = next((b for c, b in blocks if c == "Root"), "")
root_last = knob(root_body, "last_frame") or "?"
root_first = knob(root_body, "first_frame") or "1"
root_format = knob(root_body, "format")
root_name = knob(root_body, "name")  # full path to the .nk

# Shot code from the path: .../sequences/<SEQ>/<SHOT>/...
shot_code = ""
seq_code = ""
mseq = re.search(r"/sequences/([^/]+)/([^/]+)/", root_name)
if mseq:
    seq_code, shot_code = mseq.group(1), mseq.group(2)

lines = [f"Script: {script_name}"]
if shot_code:
    lines.append(f"Shot: {shot_code}  (sequence {seq_code})")
if root_format:
    lines.append(f"Format: {root_format}")
lines.append(f"Working range: {root_first}-{root_last}")
lines.append("")

reads, writes = [], []
write_path_set = False
comp_version = ""
disabled_count = 0

for cls, body in blocks:
    if cls == "define_window_layout_xml":
        continue

    disabled = bool(re.search(r"^\s*disable\s+true\s*$", body, re.MULTILINE))
    if disabled:
        disabled_count += 1
    tag = " [DISABLED]" if disabled else ""

    if cls == "Read":
        f_ = knob(body, "file")
        first = knob(body, "first") or "1"  # absent -> default 1
        last = knob(body, "last") or knob(body, "origlast") or "?"
        reads.append(f_)
        lines.append(f"Read: {f_} ({first}-{last}){tag}")
    elif cls.startswith("Write"):
        f_ = knob(body, "file")
        writes.append(f_)
        if f_:
            write_path_set = True
            lines.append(f"Write: {f_}{tag}")
            vm = re.search(r"[._]v(\d{2,4})\b", f_)
            if vm and not comp_version:
                comp_version = f"v{vm.group(1)}"
        else:
            lines.append(f"Write: present but no output path set{tag}")
    elif cls == "BackdropNode":
        lbl = knob(body, "label").strip()
        if lbl:
            lines.append(f"Backdrop: {lbl}")
    elif cls == "StickyNote":
        lbl = knob(body, "label").strip()
        if lbl:
            lines.append(f"StickyNote: {lbl}")
    elif cls == "Group":
        lines.append(f"Group: {knob(body, 'name')}{tag}")
    elif cls in PRESENCE_ONLY:
        lines.append(f"{cls} node: {knob(body, 'name')}{tag}")

# Fallback: pull comp version from the script filename (e.g. scene.v001.nk)
if not comp_version:
    vm = re.search(r"[._]v(\d{2,4})\b", script_name)
    if vm:
        comp_version = f"v{vm.group(1)}"

# Operation histogram (top-level classes only; groups counted as 1 each)
class_counts = Counter(c for c, _ in blocks if c not in SKIP_OPS)
op_summary = ", ".join(f"{n}x {c}" for c, n in class_counts.most_common())

# "real" node count: exclude Root and pure annotation / UI blocks
real_count = sum(1 for c, _ in blocks if c not in STRUCTURAL)

lines.append("")
lines.append(f"Node breakdown: {op_summary}")
lines.append(f"Comp nodes: {real_count}  |  Disabled: {disabled_count}")
if not writes:
    lines.append("No Write node configured (no render output set yet).")
elif not write_path_set:
    lines.append("Write node present but output path not yet set.")

digest = "\n".join(lines)

result = {
    "digest": digest,
    "script_name": script_name,
    "shot_code": shot_code,
    "node_count": real_count,
    "comp_version": comp_version,
    "has_write": bool(writes),
    "write_path_set": write_path_set,
}
