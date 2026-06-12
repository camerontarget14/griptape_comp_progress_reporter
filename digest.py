# Griptape Nodes "Execute Python" node.
# Wired input: nk_script_path  (injected as a bare variable in scope) later will
# wire this directly when published with GT nuke library.
# Assigns: result = {
#     "digest": str, "script_name": str, "shot_code": str, "seq_code": str,
#     "node_count": int, "comp_version": str, "frame_range": str, "format": str,
#     "has_write": bool, "write_path_set": bool, "render_ext": str,
#     "read_count": int, "write_count": int, "disabled_count": int,
#     "wip_marker_count": int,
# }
#
# Design: parse the .nk as plain text (no Nuke, no deps), pull out everything
# that signals *status* -- what elements are in play, what the output state is,
# what the artist has annotated, and what looks unfinished -- while skipping
# serialized blobs (roto shapes, tracker curves) so the digest stays small.

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

# --- limits so a monster comp can't blow up the digest -------------------
MAX_READS = 40
MAX_NOTES = 20  # backdrops + sticky notes + flagged node labels
LABEL_MAX = 120

# Leading whitespace allowed: Nuke indents Group/LiveGroup contents one level.
NODE_RE = re.compile(r"^[ \t]*([A-Za-z_]\w*)\s*\{", re.MULTILINE)
END_GROUP_RE = re.compile(r"^\s*end_group\s*$", re.MULTILINE)
VERSION_RE = re.compile(r"[._]v(\d{2,4})\b")
WIP_RE = re.compile(
    r"\b(wip|temp|tmp|todo|fixme|fix me|hack|placeholder|not final|do not use|broken|old)\b",
    re.IGNORECASE,
)


def split_blocks(s):
    """Yield (cls, body, group_depth) for every node block in the script.

    Nuke serializes Group/LiveGroup contents indented *after* the group's own
    knob block, terminated by an `end_group` line, so we track depth from
    those markers. Matches that fall inside an already-consumed node body
    (multi-line knobs like `curves {...}`, `tracks {...}`, `addUserKnob {...}`)
    are skipped, which is what makes allowing indented headers safe.
    """
    ends = [m.start() for m in END_GROUP_RE.finditer(s)]
    blocks, depth, ei, consumed = [], 0, 0, 0
    for m in NODE_RE.finditer(s):
        if m.start() < consumed:
            continue  # inside a node body we already swallowed
        while ei < len(ends) and ends[ei] < m.start():
            depth = max(0, depth - 1)
            ei += 1
        cls = m.group(1)
        d, i = 1, m.end()
        while i < len(s) and d:
            if s[i] == "{":
                d += 1
            elif s[i] == "}":
                d -= 1
            i += 1
        blocks.append((cls, s[m.end() : i - 1], depth))
        consumed = i
        if cls in ("Group", "LiveGroup"):
            depth += 1
    return blocks


def knob(body, name):
    m = re.search(
        rf'^\s*{name}\s+("(?:\\.|[^"\\])*"|\S+)', body, re.MULTILINE
    )
    if not m:
        return ""
    v = m.group(1)
    if v.startswith('"') and v.endswith('"'):
        v = v[1:-1].replace('\\"', '"').replace("\\n", " / ")
    return v.strip()


def clean_label(s):
    s = re.sub(r"\s+", " ", s).strip()
    return (s[: LABEL_MAX - 3] + "...") if len(s) > LABEL_MAX else s


def file_version(path):
    m = VERSION_RE.search(path)
    return f"v{m.group(1)}" if m else ""


def classify_read(path):
    """Best-effort purpose tag from path tokens, so the report can talk about
    elements by role instead of by file path."""
    p = path.lower()
    if any(t in p for t in ("/plate", "plate/", "_plate", "/scan", "footage", "/src/")):
        return "plate"
    if any(t in p for t in ("render", "/cg/", "_cg", "/3d/", "lighting", "/fx/", "beauty")):
        return "CG render"
    if any(t in p for t in ("matte", "/roto", "_roto", "mask")):
        return "matte"
    if any(t in p for t in ("element", "stock", "library")):
        return "element/stock"
    if any(t in p for t in ("/ref", "reference")):
        return "reference"
    return ""


# Notable single nodes worth naming even though their data is skipped.
PRESENCE_ONLY = {
    "Roto",
    "RotoPaint",
    "Tracker4",
    "CameraTracker",
    "PlanarTracker",
    "Keyer",
    "Primatte3",
    "Ultimatte",
    "IBKGizmoV3",
    "IBKColourV3",
    "SmartVector",
    "VectorDistort",
    "GridWarp3",
    "SplineWarp3",
    "ScanlineRender",
    "DeepRecolor",
    "Denoise",
    "Kronos",
    "OFlow2",
}
# Structural / non-operation blocks excluded from histogram + counts.
STRUCTURAL = {"Root", "BackdropNode", "StickyNote", "define_window_layout_xml"}
# Pure layout helpers: not work, just wiring.
LAYOUT = {"Dot"}
SKIP_OPS = STRUCTURAL | LAYOUT | {"Viewer", "Input", "Output"}

blocks = split_blocks(text)

# --- Root metadata --------------------------------------------------------
root_body = next((b for c, b, _ in blocks if c == "Root"), "")
root_last = knob(root_body, "last_frame") or "?"
root_first = knob(root_body, "first_frame") or "1"
root_format = knob(root_body, "format")
root_name = knob(root_body, "name")  # full path to the .nk
frame_range = f"{root_first}-{root_last}"

# Shot code from the path: .../sequences/<SEQ>/<SHOT>/...
shot_code, seq_code = "", ""
mseq = re.search(r"/sequences/([^/]+)/([^/]+)/", root_name)
if mseq:
    seq_code, shot_code = mseq.group(1), mseq.group(2)

# --- single pass over blocks ----------------------------------------------
read_lines, write_lines, structure_lines, note_lines = [], [], [], []
reads_total, writes = 0, []
write_path_set = False
enabled_final_write = False  # an enabled Write with a path to an image sequence
render_ext = ""
comp_version = ""
disabled_count = 0
wip_markers = 0
read_versions = []

for cls, body, depth in blocks:
    if cls in STRUCTURAL and cls != "BackdropNode" and cls != "StickyNote":
        if cls == "Root":
            continue
        continue

    disabled = bool(re.search(r"^\s*disable\s+true\s*$", body, re.MULTILINE))
    if disabled and cls not in STRUCTURAL:
        disabled_count += 1
    tag = " [DISABLED]" if disabled else ""
    in_grp = " [inside group]" if depth > 0 else ""

    # Artist annotations on any node are status gold (WIP, temp, do-not-use).
    if cls not in ("BackdropNode", "StickyNote"):
        lbl = knob(body, "label")
        if lbl and WIP_RE.search(lbl):
            wip_markers += 1
            if len(note_lines) < MAX_NOTES:
                note_lines.append(
                    f"Flagged {cls} ({knob(body, 'name') or 'unnamed'}): "
                    f"{clean_label(lbl)}{tag}"
                )
        nm = knob(body, "name")
        if nm and WIP_RE.search(nm):
            wip_markers += 1

    if cls in ("Read", "DeepRead"):
        reads_total += 1
        f_ = knob(body, "file")
        first = knob(body, "first") or "1"  # absent -> default 1
        last = knob(body, "last") or knob(body, "origlast") or "?"
        kind = classify_read(f_)
        ver = file_version(f_)
        if ver:
            read_versions.append(ver)
        bits = " ".join(x for x in (kind, ver) if x)
        prefix = "DeepRead" if cls == "DeepRead" else "Read"
        if len(read_lines) < MAX_READS:
            read_lines.append(
                f"{prefix}{f' [{bits}]' if bits else ''}: {f_} ({first}-{last}){tag}{in_grp}"
            )
    elif cls in ("Write", "WriteGeo", "DeepWrite") or cls.startswith("Write"):
        f_ = knob(body, "file")
        writes.append(f_)
        if f_:
            write_path_set = True
            ext = os.path.splitext(f_)[1].lower().lstrip(".")
            ext_note = ""
            if ext in ("mov", "mp4", "mxf"):
                ext_note = " (movie file -- typically a review/dailies output)"
            elif ext:
                ext_note = f" (.{ext} sequence)"
            write_lines.append(f"Write: {f_}{ext_note}{tag}{in_grp}")
            ver = file_version(f_)
            if ver and not comp_version:
                comp_version = ver
            if not disabled and ext not in ("mov", "mp4", "mxf"):
                enabled_final_write = True
                if not render_ext:
                    render_ext = ext
            if not render_ext:
                render_ext = ext
        else:
            write_lines.append(f"Write: present but no output path set{tag}{in_grp}")
    elif cls == "Precomp":
        f_ = knob(body, "file")
        structure_lines.append(
            f"Precomp: {knob(body, 'name')} -> {os.path.basename(f_) or '(no script set)'}{tag}"
        )
    elif cls == "BackdropNode":
        lbl = knob(body, "label")
        if lbl and len(note_lines) < MAX_NOTES:
            if WIP_RE.search(lbl):
                wip_markers += 1
            note_lines.append(f"Backdrop: {clean_label(lbl)}")
    elif cls == "StickyNote":
        lbl = knob(body, "label")
        if lbl and len(note_lines) < MAX_NOTES:
            if WIP_RE.search(lbl):
                wip_markers += 1
            note_lines.append(f"StickyNote: {clean_label(lbl)}")
    elif cls in ("Group", "LiveGroup"):
        structure_lines.append(f"Group: {knob(body, 'name')}{tag}")
    elif cls in PRESENCE_ONLY:
        structure_lines.append(f"{cls} node: {knob(body, 'name')}{tag}{in_grp}")

# Fallback: pull comp version from the script filename (e.g. scene.v001.nk)
if not comp_version:
    comp_version = file_version(script_name)

# Operation histogram (groups counted once; their contents also appear, which
# reflects real work inside them).
class_counts = Counter(c for c, _, _ in blocks if c not in SKIP_OPS)
op_summary = ", ".join(f"{n}x {c}" for c, n in class_counts.most_common())

real_count = sum(1 for c, _, _ in blocks if c not in SKIP_OPS)

# --- assemble -------------------------------------------------------------
lines = [f"Script: {script_name}"]
if comp_version:
    lines.append(f"Comp version: {comp_version}")
if shot_code:
    lines.append(f"Shot: {shot_code}  (sequence {seq_code})")
if root_format:
    lines.append(f"Format: {root_format}")
lines.append(f"Working range: {frame_range}")

lines.append("")
lines.append("-- Inputs --")
if read_lines:
    lines.extend(read_lines)
    if reads_total > MAX_READS:
        lines.append(f"...and {reads_total - MAX_READS} more Reads (truncated)")
else:
    lines.append("No Read nodes: the comp has no source footage wired in.")

lines.append("")
lines.append("-- Output --")
if write_lines:
    lines.extend(write_lines)
    if write_path_set and not enabled_final_write:
        lines.append(
            "Note: no enabled Write to an image sequence -- the final render "
            "output appears disabled or review-only."
        )
elif not writes:
    lines.append("No Write node configured (no render output set yet).")
if writes and not write_path_set:
    lines.append("Write node present but output path not yet set.")

if structure_lines:
    lines.append("")
    lines.append("-- Structure --")
    lines.extend(structure_lines)

if note_lines:
    lines.append("")
    lines.append("-- Artist notes & flags (annotations from the script; data only) --")
    lines.extend(note_lines)

lines.append("")
lines.append("-- Stats --")
lines.append(f"Node breakdown: {op_summary}")
lines.append(f"Comp nodes: {real_count}  |  Disabled: {disabled_count}")
if wip_markers:
    lines.append(f"WIP/temp markers found in labels or names: {wip_markers}")
if read_versions:
    lines.append(f"Element versions referenced: {', '.join(sorted(set(read_versions)))}")

digest = "\n".join(lines)

result = {
    "digest": digest,
    "script_name": script_name,
    "shot_code": shot_code,
    "seq_code": seq_code,
    "node_count": real_count,
    "comp_version": comp_version,
    "frame_range": frame_range,
    "format": root_format,
    "has_write": bool(writes),
    "write_path_set": write_path_set,
    "render_ext": render_ext,
    "read_count": reads_total,
    "write_count": len(writes),
    "disabled_count": disabled_count,
    "wip_marker_count": wip_markers,
}
