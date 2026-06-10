import streamlit as st
import gspread
import io
import json
from datetime import datetime
from PIL import Image
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ─── PAGE CONFIG ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="DME OCT Classifier", page_icon="🔬", layout="centered")

# ─── CSS + ANIMATIONS ────────────────────────────────────────────────────────
st.markdown("""
<style>
@keyframes fadeSlideIn {
    from { opacity: 0; transform: translateY(18px); }
    to   { opacity: 1; transform: translateY(0); }
}
.fade-in { animation: fadeSlideIn 0.5s ease-out; }
@keyframes slideRight {
    from { opacity: 0; transform: translateX(-30px); }
    to   { opacity: 1; transform: translateX(0); }
}
.slide-in { animation: slideRight 0.4s ease-out; }
@keyframes softPulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(255,255,255,0); }
    50%      { box-shadow: 0 0 12px 4px rgba(100,200,100,0.25); }
}
.pulse { animation: softPulse 2s ease-in-out infinite; }
.stProgress > div > div { transition: width 0.6s ease-in-out; }
.welcome-title {
    font-size: 2.2rem; font-weight: 700;
    background: linear-gradient(135deg, #4CAF50, #2196F3);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    animation: fadeSlideIn 0.8s ease-out;
}
.annotator-badge {
    display: inline-block; padding: 4px 14px; border-radius: 20px;
    background: linear-gradient(135deg, #667eea, #764ba2);
    color: white; font-weight: 600; font-size: 14px;
}
div.stButton > button:hover {
    box-shadow: 0 4px 15px rgba(0,0,0,0.15);
    transform: translateY(-1px); transition: all 0.2s ease;
}
.counter-chip {
    display: inline-block; padding: 6px 16px; border-radius: 20px;
    background: #f0f2f6; font-weight: 600; font-size: 14px;
}
.grade-card {
    border-radius: 10px; padding: 12px 16px; margin-bottom: 8px;
    animation: slideRight 0.4s ease-out;
}
.stat-card {
    text-align: center; padding: 16px; border-radius: 12px;
    background: linear-gradient(145deg, #f8f9fa, #e9ecef);
    animation: fadeSlideIn 0.5s ease-out;
}
.stat-num  { font-size: 2rem; font-weight: 700; }
.stat-label { font-size: 0.85rem; color: #666; margin-top: 4px; }
</style>
""", unsafe_allow_html=True)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
GRADES = {
    "Mild":     {"folder": "Grade1_Mild",     "color": "#C8851A", "emoji": "🟡",
                 "desc": "Minimal IRF · mild thickening · EZ mostly intact"},
    "Moderate": {"folder": "Grade2_Moderate", "color": "#D85A30", "emoji": "🟠",
                 "desc": "IRF + possible SRF · center-involving · EZ disrupted"},
    "Severe":   {"folder": "Grade3_Severe",   "color": "#D43C3C", "emoji": "🔴",
                 "desc": "Diffuse edema · large cysts · EZ near-complete loss"},
}
IMG_MIMES = {"image/png", "image/jpeg", "image/tiff", "image/bmp"}
SHEET_HEADERS = ["timestamp", "annotator", "image", "grade", "drive_file_id"]

# ─── GOOGLE API ───────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

@st.cache_resource
def get_google_clients():
    creds_dict = json.loads(st.secrets["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds)
    sheets = gspread.authorize(creds)
    return drive, sheets

def get_drive():
    return get_google_clients()[0]

def get_sheet():
    _, sheets = get_google_clients()
    return sheets.open(st.secrets["GOOGLE_SHEET_NAME"]).sheet1

# ─── DRIVE HELPERS ────────────────────────────────────────────────────────────
@st.cache_data(ttl=120)
def list_drive_images(folder_id):
    drive = get_drive()
    results = []
    page_token = None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=200, pageToken=page_token,
        ).execute()
        for f in resp.get("files", []):
            if f["mimeType"] in IMG_MIMES:
                results.append({"id": f["id"], "name": f["name"],
                                "mime": f["mimeType"]})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return sorted(results, key=lambda x: x["name"])

def download_image_bytes(file_id):
    drive = get_drive()
    request = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf

# ─── SHEET HELPERS ────────────────────────────────────────────────────────────
def ensure_sheet_headers():
    sheet = get_sheet()
    try:
        row1 = sheet.row_values(1)
        if row1 != SHEET_HEADERS:
            sheet.insert_row(SHEET_HEADERS, index=1)
    except Exception:
        sheet.insert_row(SHEET_HEADERS, index=1)

def load_my_classifications(annotator):
    """Load ONLY this annotator's classifications. {filename: grade}"""
    sheet = get_sheet()
    try:
        records = sheet.get_all_records()
    except Exception:
        return {}
    my = {}
    for r in records:
        if r.get("annotator") == annotator:
            my[r.get("image", "")] = r.get("grade", "")
    return my

def load_other_annotations(annotator):
    """Load what OTHER annotators labeled (for comparison display only)."""
    sheet = get_sheet()
    try:
        records = sheet.get_all_records()
    except Exception:
        return {}
    others = {}  # {filename: [{annotator, grade}, ...]}
    for r in records:
        a = r.get("annotator", "")
        if a and a != annotator:
            fname = r.get("image", "")
            if fname not in others:
                others[fname] = []
            others[fname].append({"annotator": a, "grade": r.get("grade", "")})
    return others

def append_to_sheet(annotator, filename, grade, file_id):
    """Add a row. Unique key = (annotator, image)."""
    sheet = get_sheet()
    sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        annotator, filename, grade, file_id,
    ])

def update_or_append(annotator, filename, grade, file_id):
    """If (annotator, image) exists, update grade. Otherwise append."""
    sheet = get_sheet()
    try:
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if r.get("annotator") == annotator and r.get("image") == filename:
                row_num = i + 2  # 1-indexed + header
                sheet.update_cell(row_num, 1, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                sheet.update_cell(row_num, 4, grade)
                return
    except Exception:
        pass
    append_to_sheet(annotator, filename, grade, file_id)

def remove_from_sheet(annotator, filename):
    """Remove the row for this (annotator, image) pair."""
    sheet = get_sheet()
    try:
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if r.get("annotator") == annotator and r.get("image") == filename:
                sheet.delete_rows(i + 2)
                return
    except Exception:
        pass

def get_all_annotator_stats():
    """Get all annotators + their counts for the login screen."""
    sheet = get_sheet()
    try:
        records = sheet.get_all_records()
    except Exception:
        return {}
    stats = {}
    for r in records:
        a = r.get("annotator", "")
        if a:
            if a not in stats:
                stats[a] = {"total": 0, "Mild": 0, "Moderate": 0, "Severe": 0}
            stats[a]["total"] += 1
            g = r.get("grade", "")
            if g in stats[a]:
                stats[a][g] += 1
    return stats

# ─── SESSION STATE ────────────────────────────────────────────────────────────
defaults = {
    "images": [], "idx": 0, "classifications": {},
    "other_annotations": {},
    "loaded": False, "annotator": "", "logged_in": False,
    "just_classified": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ─── CALLBACKS ────────────────────────────────────────────────────────────────
def do_login():
    name = st.session_state.name_input.strip()
    if name:
        st.session_state.annotator = name
        st.session_state.logged_in = True

def load_images_from_drive():
    src_id = st.secrets["SOURCE_FOLDER_ID"]

    with st.spinner("Loading images from Drive..."):
        images = list_drive_images(src_id)
    if not images:
        st.toast("❌ No images found!", icon="🚫")
        return

    ensure_sheet_headers()

    try:
        my_prev = load_my_classifications(st.session_state.annotator)
    except Exception:
        my_prev = {}

    try:
        others = load_other_annotations(st.session_state.annotator)
    except Exception:
        others = {}

    st.session_state.images = images
    st.session_state.classifications = my_prev
    st.session_state.other_annotations = others

    # Auto-jump to first image THIS annotator hasn't done yet
    first_pending = 0
    for i, img in enumerate(images):
        if img["name"] not in my_prev:
            first_pending = i
            break
    else:
        first_pending = len(images) - 1  # all done, go to last

    st.session_state.idx = first_pending
    st.session_state.loaded = True

    st.toast(
        f"✅ {len(images)} images · you've done {len(my_prev)} · "
        f"{len(images) - len(my_prev)} remaining",
        icon="🎉")

def classify(grade):
    img = st.session_state.images[st.session_state.idx]
    fname = img["name"]
    file_id = img["id"]
    annotator = st.session_state.annotator

    # Update or append to Sheet (annotator + image = unique key)
    update_or_append(annotator, fname, grade, file_id)

    # Update local state
    st.session_state.classifications[fname] = grade
    st.session_state.just_classified = grade

    # Auto-advance to next image THIS annotator hasn't done
    advance_to_my_next()

def advance_to_my_next():
    """Jump to the next image THIS annotator hasn't classified."""
    images = st.session_state.images
    my = st.session_state.classifications
    for i in range(st.session_state.idx + 1, len(images)):
        if images[i]["name"] not in my:
            st.session_state.idx = i
            return
    # All remaining done — just go to next image
    if st.session_state.idx < len(images) - 1:
        st.session_state.idx += 1

def clear_current():
    img = st.session_state.images[st.session_state.idx]
    fname = img["name"]
    grade = st.session_state.classifications.pop(fname, None)
    if grade:
        remove_from_sheet(st.session_state.annotator, fname)
    st.session_state.just_classified = None

def go_prev():
    if st.session_state.idx > 0:
        st.session_state.idx -= 1
    st.session_state.just_classified = None

def go_next():
    if st.session_state.idx < len(st.session_state.images) - 1:
        st.session_state.idx += 1
    st.session_state.just_classified = None

def jump_my_next_pending():
    """Skip to next image THIS annotator hasn't done."""
    images = st.session_state.images
    my = st.session_state.classifications
    for i in range(st.session_state.idx + 1, len(images)):
        if images[i]["name"] not in my:
            st.session_state.idx = i
            return
    # Wrap around from beginning
    for i in range(0, st.session_state.idx):
        if images[i]["name"] not in my:
            st.session_state.idx = i
            return
    st.toast("🎉 You've classified all images!", icon="✅")

def logout():
    for k in defaults:
        st.session_state[k] = defaults[k]

# ══════════════════════════════════════════════════════════════════════════════
# PAGE 1: LOGIN
# ══════════════════════════════════════════════════════════════════════════════
if not st.session_state.logged_in:
    st.markdown("")
    st.markdown('<p class="welcome-title">🔬 DME OCT Classifier</p>',
                unsafe_allow_html=True)
    st.markdown(
        '<div class="fade-in" style="font-size:1.05rem;color:#666;margin-bottom:8px">'
        'Manual severity grading for inter-rater agreement</div>',
        unsafe_allow_html=True)
    st.markdown(
        '<div class="fade-in" style="font-size:0.85rem;color:#999;margin-bottom:32px">'
        'Each annotator classifies the full set independently · resume anytime</div>',
        unsafe_allow_html=True)

    col1, col2 = st.columns([3, 1])
    with col1:
        st.text_input("Enter your name", key="name_input",
                      placeholder="e.g. Gadha Suresh", label_visibility="collapsed")
    with col2:
        st.button("Start →", on_click=do_login, type="primary",
                  use_container_width=True)

    st.markdown("---")

    # Show all annotators and their progress
    try:
        all_stats = get_all_annotator_stats()
        if all_stats:
            st.markdown("**Annotator progress**")
            for name, s in all_stats.items():
                st.markdown(
                    f'<div class="grade-card" style="border-left:4px solid #667eea;">'
                    f'👤 <b>{name}</b> — {s["total"]} done '
                    f'(🟡{s["Mild"]} 🟠{s["Moderate"]} 🔴{s["Severe"]})</div>',
                    unsafe_allow_html=True)
    except Exception:
        pass

    st.markdown("")
    st.markdown("**Grading reference**")
    for name, g in GRADES.items():
        st.markdown(
            f'<div class="grade-card" style="border-left:4px solid {g["color"]};">'
            f'{g["emoji"]} <b style="color:{g["color"]}">{name}</b> — '
            f'<span style="color:#888">{g["desc"]}</span></div>',
            unsafe_allow_html=True)
    st.stop()

# ══════════════════════════════════════════════════════════════════════════════
# PAGE 1.5: RESUME CHECK
# ══════════════════════════════════════════════════════════════════════════════
if st.session_state.logged_in and not st.session_state.loaded:
    try:
        my_prev = load_my_classifications(st.session_state.annotator)
        if len(my_prev) > 0 and "resume_answered" not in st.session_state:
            st.markdown(
                f'<div class="fade-in" style="text-align:center;padding:20px">'
                f'<div style="font-size:1.3rem;font-weight:600">Welcome back, '
                f'{st.session_state.annotator}!</div>'
                f'<div style="color:#888;margin-top:8px">'
                f'You have {len(my_prev)} classifications saved. '
                f'You\'ll continue from where you left off.</div></div>',
                unsafe_allow_html=True)
            st.markdown("")
            r1, r2 = st.columns(2)
            with r1:
                if st.button("▶️ Resume", type="primary", use_container_width=True):
                    st.session_state.resume_answered = True
                    load_images_from_drive()
                    st.rerun()
            with r2:
                if st.button("🆕 Start from image 1", use_container_width=True):
                    st.session_state.resume_answered = True
                    st.rerun()
            st.stop()
    except Exception:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown(
        f'<span class="annotator-badge">👤 {st.session_state.annotator}</span>',
        unsafe_allow_html=True)
    st.markdown("")
    st.button("📂 Load images from Drive", on_click=load_images_from_drive,
              type="primary", use_container_width=True)

    if st.session_state.loaded:
        images = st.session_state.images
        total = len(images)
        my_done = len(st.session_state.classifications)
        my_pending = total - my_done

        st.markdown("---")

        # Your progress
        st.markdown(
            f'<div class="stat-card">'
            f'<div class="stat-num">{my_done}/{total}</div>'
            f'<div class="stat-label">your progress</div></div>',
            unsafe_allow_html=True)
        st.markdown("")

        if my_pending > 0:
            st.markdown(
                f'<div style="color:#D85A30;font-weight:600">'
                f'⏳ {my_pending} images remaining</div>',
                unsafe_allow_html=True)
        else:
            st.markdown(
                '<div style="color:#4CAF50;font-weight:600">'
                '✅ You\'ve classified all images!</div>',
                unsafe_allow_html=True)

        st.markdown("")

        # Your grade distribution
        counts = {"Mild": 0, "Moderate": 0, "Severe": 0}
        for g in st.session_state.classifications.values():
            if g in counts:
                counts[g] += 1
        for name, g in GRADES.items():
            st.markdown(f'{g["emoji"]} **{name}**: {counts[name]}')

        st.markdown("")
        st.button("⏭️ Jump to next pending", on_click=jump_my_next_pending,
                  use_container_width=True, disabled=(my_pending == 0))

        # Sheet link
        st.markdown("---")
        st.markdown(
            '<div style="font-size:12px">'
            '📊 <a href="https://docs.google.com/spreadsheets" '
            'target="_blank" style="color:#667eea">Open Google Sheets ↗</a>'
            ' — all annotators\' responses</div>',
            unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("**Grading reference**")
    for name, g in GRADES.items():
        st.markdown(
            f'<div class="grade-card" style="border-left:4px solid {g["color"]};">'
            f'{g["emoji"]} <b style="color:{g["color"]}">{name}</b><br>'
            f'<span style="font-size:12px;color:#888">{g["desc"]}</span></div>',
            unsafe_allow_html=True)
    st.markdown("---")
    st.button("🚪 Switch annotator", on_click=logout, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN: CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════
st.markdown(
    '<div class="slide-in" style="font-size:1.5rem;font-weight:700">'
    '🔬 Classify OCT Images</div>', unsafe_allow_html=True)

if not st.session_state.loaded:
    st.info("👈 Click **Load images from Drive** in the sidebar.")
    st.stop()

images = st.session_state.images
idx    = st.session_state.idx
total  = len(images)

if total == 0:
    st.warning("No images loaded.")
    st.stop()

cur      = images[idx]
fname    = cur["name"]
file_id  = cur["id"]
my_grade = st.session_state.classifications.get(fname)
others   = st.session_state.other_annotations.get(fname, [])

# Toast
if st.session_state.just_classified:
    g = st.session_state.just_classified
    st.toast(f'{GRADES[g]["emoji"]} Labeled as {g}!', icon="✅")
    st.session_state.just_classified = None

# Your progress bar
my_done = len(st.session_state.classifications)
st.progress(my_done / total, text=f"Your progress: {my_done}/{total}")

# Image counter
st.markdown(
    f'<div class="fade-in">'
    f'<span class="counter-chip">Image {idx+1} of {total}</span> '
    f'&nbsp; <span style="color:#999">{fname}</span></div>',
    unsafe_allow_html=True)

# Your label for this image
if my_grade:
    c = GRADES[my_grade]["color"]
    e = GRADES[my_grade]["emoji"]
    st.markdown(
        f'<div class="slide-in pulse" style="display:inline-block;'
        f'background:{c}15;border:1.5px solid {c};border-radius:10px;'
        f'padding:6px 16px;color:{c};font-weight:600;margin:8px 0">'
        f'{e} Your label: {my_grade}</div>', unsafe_allow_html=True)
else:
    st.markdown(
        '<div class="fade-in" style="display:inline-block;'
        'background:#f0f2f6;border:1.5px dashed #ccc;border-radius:10px;'
        'padding:6px 16px;color:#999;margin:8px 0">'
        '⬜ Not yet classified by you</div>', unsafe_allow_html=True)

# Show what OTHER annotators labeled (for reference, not for skipping)
if others:
    other_text = " · ".join(
        [f'{GRADES.get(o["grade"],{}).get("emoji","")} {o["annotator"]}: {o["grade"]}'
         for o in others])
    st.markdown(
        f'<div class="fade-in" style="font-size:12px;color:#999;margin-bottom:4px">'
        f'Other annotators: {other_text}</div>',
        unsafe_allow_html=True)

# Image
try:
    with st.spinner(""):
        img_bytes = download_image_bytes(file_id)
        img = Image.open(img_bytes)
    st.markdown('<div class="fade-in">', unsafe_allow_html=True)
    st.image(img, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)
except Exception as e:
    st.error(f"Could not load image: {e}")

# Classification buttons
st.markdown("")
b1, b2, b3 = st.columns(3)
with b1:
    st.button("🟡 Mild", on_click=classify, args=("Mild",),
              use_container_width=True,
              type="primary" if my_grade == "Mild" else "secondary")
with b2:
    st.button("🟠 Moderate", on_click=classify, args=("Moderate",),
              use_container_width=True,
              type="primary" if my_grade == "Moderate" else "secondary")
with b3:
    st.button("🔴 Severe", on_click=classify, args=("Severe",),
              use_container_width=True,
              type="primary" if my_grade == "Severe" else "secondary")

# Navigation
st.markdown("")
n1, n2, n3, n4 = st.columns([1, 1, 1, 1])
with n1:
    st.button("← Prev", on_click=go_prev, use_container_width=True,
              disabled=(idx == 0))
with n2:
    st.button("🗑️ Unselect", on_click=clear_current, use_container_width=True,
              disabled=(my_grade is None))
with n3:
    st.button("Next →", on_click=go_next, use_container_width=True,
              disabled=(idx == total - 1))
with n4:
    my_pending = total - my_done
    st.button(f"⏭️ ({my_pending})", on_click=jump_my_next_pending,
              use_container_width=True, disabled=(my_pending == 0))

# Completion
if my_done == total and total > 0:
    st.balloons()
    st.markdown(
        '<div class="fade-in" style="text-align:center;padding:20px">'
        '<div style="font-size:2.5rem">🎉</div>'
        '<div style="font-size:1.3rem;font-weight:700;color:#4CAF50">'
        'You\'ve classified all images!</div></div>', unsafe_allow_html=True)

    counts = {"Mild": 0, "Moderate": 0, "Severe": 0}
    for g in st.session_state.classifications.values():
        if g in counts:
            counts[g] += 1
    c1, c2, c3 = st.columns(3)
    for col, (name, g) in zip([c1, c2, c3], GRADES.items()):
        with col:
            st.markdown(
                f'<div class="stat-card">'
                f'<div class="stat-num" style="color:{g["color"]}">{counts[name]}</div>'
                f'<div class="stat-label">{g["emoji"]} {name}</div></div>',
                unsafe_allow_html=True)

    st.success("📊 Your responses are saved in Google Sheets!")
