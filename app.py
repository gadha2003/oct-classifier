import streamlit as st
import gspread
import io
import json
from datetime import datetime
from PIL import Image
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

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
    animation: slideRight 0.5s ease-out;
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

# ─── GOOGLE API SETUP ─────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

@st.cache_resource
def get_google_clients():
    """Build Drive + Sheets clients from Streamlit secrets (runs once)."""
    creds_dict = json.loads(st.secrets["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    drive   = build("drive", "v3", credentials=creds)
    sheets  = gspread.authorize(creds)
    return drive, sheets

def get_drive():
    return get_google_clients()[0]

def get_sheet():
    drive, sheets = get_google_clients()
    return sheets.open(st.secrets["GOOGLE_SHEET_NAME"]).sheet1

# ─── DRIVE HELPERS ────────────────────────────────────────────────────────────
@st.cache_data(ttl=120)
def list_drive_images(folder_id):
    """List all image files in a Drive folder. Cached for 2 minutes."""
    drive = get_drive()
    results = []
    page_token = None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=200,
            pageToken=page_token,
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
    """Download an image from Drive into memory."""
    drive = get_drive()
    request = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf

def copy_to_drive_folder(file_id, dest_folder_id, filename):
    """Download image then upload to destination folder (more reliable than copy)."""
    drive = get_drive()
    # Download
    request = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    # Upload to destination
    media = MediaIoBaseUpload(buf, mimetype="image/jpeg", resumable=True)
    drive.files().create(
        body={"name": filename, "parents": [dest_folder_id]},
        media_body=media,
        fields="id",
    ).execute()

def find_subfolder_id(parent_id, subfolder_name):
    """Find or create a subfolder inside a parent folder."""
    drive = get_drive()
    q = (f"'{parent_id}' in parents and name='{subfolder_name}' "
         f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    results = drive.files().list(q=q, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    # Create it
    meta = {"name": subfolder_name, "parents": [parent_id],
            "mimeType": "application/vnd.google-apps.folder"}
    folder = drive.files().create(body=meta, fields="id").execute()
    return folder["id"]

def remove_from_drive_folder(folder_id, filename):
    """Delete a file by name from a specific folder."""
    drive = get_drive()
    q = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
    results = drive.files().list(q=q, fields="files(id)").execute()
    for f in results.get("files", []):
        drive.files().delete(fileId=f["id"]).execute()
def remove_from_drive_folder(folder_id, filename):
    """Delete a file by name from a specific folder."""
    try:
        drive = get_drive()
        q = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
        results = drive.files().list(q=q, fields="files(id)").execute()
        for f in results.get("files", []):
            drive.files().delete(fileId=f["id"]).execute()
    except Exception as e:
        st.toast(f"Could not remove file: {e}", icon="⚠️")        

# ─── GOOGLE SHEET HELPERS (the "Forms" log) ──────────────────────────────────
SHEET_HEADERS = ["timestamp", "annotator", "image", "grade", "drive_file_id"]

def ensure_sheet_headers():
    sheet = get_sheet()
    row1 = sheet.row_values(1)
    if row1 != SHEET_HEADERS:
        sheet.insert_row(SHEET_HEADERS, index=1)

@st.cache_data(ttl=60)
def load_all_classifications():
    """
    Read the Google Sheet and return global progress dict.
    { filename: {"annotator": ..., "grade": ..., "row": ...} }
    """
    sheet = get_sheet()
    records = sheet.get_all_records()
    progress = {}
    for i, r in enumerate(records):
        fname = r.get("image", "")
        if fname:
            progress[fname] = {
                "annotator": r.get("annotator", ""),
                "grade":     r.get("grade", ""),
                "row":       i + 2,   # 1-indexed, header is row 1
            }
    return progress

def append_classification(annotator, filename, grade, file_id):
    sheet = get_sheet()
    sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        annotator, filename, grade, file_id,
    ])
    # Clear cache so next read picks up the new row
    load_all_classifications.clear()

def remove_classification_row(filename, annotator):
    """Remove the row for this image+annotator from the Sheet."""
    sheet = get_sheet()
    records = sheet.get_all_records()
    for i, r in enumerate(records):
        if r.get("image") == filename and r.get("annotator") == annotator:
            sheet.delete_rows(i + 2)   # +2: 1-indexed + header row
            load_all_classifications.clear()
            return

# ─── SESSION STATE ────────────────────────────────────────────────────────────
defaults = {
    "images": [], "idx": 0, "my_classifications": {},
    "global_progress": {}, "loaded": False,
    "annotator": "", "logged_in": False,
    "just_classified": None,
    "source_folder_id": "", "classified_folder_id": "",
    "grade_folder_ids": {},
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
    cls_id = st.secrets["CLASSIFIED_FOLDER_ID"]

    with st.spinner("Loading images from Google Drive..."):
        images = list_drive_images(src_id)

    if not images:
        st.toast("❌ No images found in Drive folder!", icon="🚫")
        return

    # Ensure grade subfolders exist in Classified/
    grade_ids = {}
    for g in GRADES.values():
        grade_ids[g["folder"]] = find_subfolder_id(cls_id, g["folder"])

    # Ensure Sheet has headers
    ensure_sheet_headers()

    # Load global progress from Sheet
    gp = load_all_classifications()

    st.session_state.images = images
    st.session_state.source_folder_id = src_id
    st.session_state.classified_folder_id = cls_id
    st.session_state.grade_folder_ids = grade_ids
    st.session_state.global_progress = gp

    # Rebuild this annotator's own classifications from the sheet
    my = {}
    for fname, info in gp.items():
        if info["annotator"] == st.session_state.annotator:
            my[fname] = info["grade"]
    st.session_state.my_classifications = my

    # Auto-jump to first unclassified
    classified_set = set(gp.keys())
    first_pending = 0
    for i, img in enumerate(images):
        if img["name"] not in classified_set:
            first_pending = i
            break
    st.session_state.idx = first_pending
    st.session_state.loaded = True

    already = len(classified_set & {f["name"] for f in images})
    st.toast(f"✅ {len(images)} images · {already} already classified", icon="🎉")

def classify(grade):
    img = st.session_state.images[st.session_state.idx]
    fname = img["name"]
    file_id = img["id"]
    grade_folder = GRADES[grade]["folder"]
    dest_id = st.session_state.grade_folder_ids[grade_folder]

    # If re-classifying, remove old
    old = st.session_state.my_classifications.get(fname)
    if old and old != grade:
        old_folder = GRADES[old]["folder"]
        old_dest = st.session_state.grade_folder_ids[old_folder]
        remove_from_drive_folder(old_dest, fname)
        remove_classification_row(fname, st.session_state.annotator)

    # Copy image to grade folder in Drive
    copy_to_drive_folder(file_id, dest_id, fname)

    # Log to Google Sheet
    append_classification(st.session_state.annotator, fname, grade, file_id)

    # Update local state
    st.session_state.my_classifications[fname] = grade
    st.session_state.global_progress[fname] = {
        "annotator": st.session_state.annotator, "grade": grade}
    st.session_state.just_classified = grade

    advance_to_next_pending()

def advance_to_next_pending():
    images = st.session_state.images
    gp = st.session_state.global_progress
    for i in range(st.session_state.idx + 1, len(images)):
        if images[i]["name"] not in gp:
            st.session_state.idx = i
            return
    if st.session_state.idx < len(images) - 1:
        st.session_state.idx += 1

def clear_current():
    img = st.session_state.images[st.session_state.idx]
    fname = img["name"]
    grade = st.session_state.my_classifications.pop(fname, None)
    if grade:
        folder = GRADES[grade]["folder"]
        dest_id = st.session_state.grade_folder_ids[folder]
        remove_from_drive_folder(dest_id, fname)
        remove_classification_row(fname, st.session_state.annotator)
        if fname in st.session_state.global_progress:
            del st.session_state.global_progress[fname]
    st.session_state.just_classified = None

def go_prev():
    if st.session_state.idx > 0:
        st.session_state.idx -= 1
    st.session_state.just_classified = None

def go_next():
    if st.session_state.idx < len(st.session_state.images) - 1:
        st.session_state.idx += 1
    st.session_state.just_classified = None

def jump_next_pending():
    images = st.session_state.images
    gp = st.session_state.global_progress
    for i in range(st.session_state.idx + 1, len(images)):
        if images[i]["name"] not in gp:
            st.session_state.idx = i
            return
    st.toast("🎉 No more pending images!", icon="✅")

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
        'Manual severity grading · Google Drive powered · team relay</div>',
        unsafe_allow_html=True)
    st.markdown(
        '<div class="fade-in" style="font-size:0.85rem;color:#999;margin-bottom:32px">'
        'Images load from Drive automatically · results save like Google Forms</div>',
        unsafe_allow_html=True)

    col1, col2 = st.columns([3, 1])
    with col1:
        st.text_input("Enter your name", key="name_input",
                      placeholder="e.g. Gadha Suresh", label_visibility="collapsed")
    with col2:
        st.button("Start →", on_click=do_login, type="primary",
                  use_container_width=True)

    st.markdown("---")
    st.markdown("**Grading reference**")
    for name, g in GRADES.items():
        st.markdown(
            f'<div class="grade-card" style="border-left:4px solid {g["color"]};">'
            f'{g["emoji"]} <b style="color:{g["color"]}">{name}</b> — '
            f'<span style="color:#888">{g["desc"]}</span></div>',
            unsafe_allow_html=True)
    st.stop()

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
        gp = st.session_state.global_progress
        my_done = len(st.session_state.my_classifications)
        all_done = sum(1 for img in images if img["name"] in gp)
        pending = total - all_done

        st.markdown("---")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                f'<div class="stat-card">'
                f'<div class="stat-num">{all_done}/{total}</div>'
                f'<div class="stat-label">total done</div></div>',
                unsafe_allow_html=True)
        with c2:
            st.markdown(
                f'<div class="stat-card" style="background:linear-gradient(145deg,#eef,#e0e0ff)">'
                f'<div class="stat-num" style="color:#667eea">{my_done}</div>'
                f'<div class="stat-label">by you</div></div>',
                unsafe_allow_html=True)
        st.markdown("")

        if pending > 0:
            st.markdown(f'<div style="color:#D85A30;font-weight:600">⏳ {pending} pending</div>',
                        unsafe_allow_html=True)

        counts = {"Mild": 0, "Moderate": 0, "Severe": 0}
        for g in st.session_state.my_classifications.values():
            counts[g] += 1
        for name, g in GRADES.items():
            st.markdown(f'{g["emoji"]} **{name}**: {counts[name]}')

        st.button("⏭️ Jump to next pending", on_click=jump_next_pending,
                  use_container_width=True)

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
# MAIN CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════
st.markdown(
    '<div class="slide-in" style="font-size:1.5rem;font-weight:700">'
    '🔬 Classify OCT Images</div>', unsafe_allow_html=True)

if not st.session_state.loaded:
    st.info("👈 Click **Load images from Drive** in the sidebar to begin.")
    st.stop()

images = st.session_state.images
idx    = st.session_state.idx
total  = len(images)
gp     = st.session_state.global_progress

if total == 0:
    st.warning("No images loaded.")
    st.stop()

cur       = images[idx]
fname     = cur["name"]
file_id   = cur["id"]
my_grade  = st.session_state.my_classifications.get(fname)
other_info = gp.get(fname)

if st.session_state.just_classified:
    g = st.session_state.just_classified
    st.toast(f'{GRADES[g]["emoji"]} Labeled as {g}!', icon="✅")
    st.session_state.just_classified = None

all_done = sum(1 for img in images if img["name"] in gp)
st.progress(all_done / total, text=f"Global: {all_done}/{total}")

st.markdown(
    f'<div class="fade-in">'
    f'<span class="counter-chip">Image {idx+1} of {total}</span> '
    f'&nbsp; <span style="color:#999">{fname}</span></div>',
    unsafe_allow_html=True)

# Status badge
if my_grade:
    c = GRADES[my_grade]["color"]
    e = GRADES[my_grade]["emoji"]
    st.markdown(
        f'<div class="slide-in pulse" style="display:inline-block;'
        f'background:{c}15;border:1.5px solid {c};border-radius:10px;'
        f'padding:6px 16px;color:{c};font-weight:600;margin:8px 0">'
        f'{e} You labeled: {my_grade}</div>', unsafe_allow_html=True)
elif other_info:
    st.markdown(
        f'<div class="slide-in" style="display:inline-block;'
        f'background:#667eea20;border:1.5px solid #667eea;border-radius:10px;'
        f'padding:6px 16px;color:#667eea;font-weight:600;margin:8px 0">'
        f'👤 Done by {other_info["annotator"]} → '
        f'{GRADES.get(other_info["grade"],{}).get("emoji","")} {other_info["grade"]}'
        f'</div>', unsafe_allow_html=True)
else:
    st.markdown(
        '<div class="fade-in" style="display:inline-block;'
        'background:#f0f2f6;border:1.5px dashed #ccc;border-radius:10px;'
        'padding:6px 16px;color:#999;margin:8px 0">'
        '⬜ Not yet classified</div>', unsafe_allow_html=True)

# Download + display image from Drive
try:
    with st.spinner("Loading image..."):
        img_bytes = download_image_bytes(file_id)
        img = Image.open(img_bytes)
    st.markdown('<div class="fade-in">', unsafe_allow_html=True)
    st.image(img, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)
except Exception as e:
    st.error(f"Could not load image from Drive: {e}")

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
    pending = total - all_done
    st.button(f"⏭️ ({pending})", on_click=jump_next_pending,
              use_container_width=True, disabled=(pending == 0))

# Completion
if all_done == total and total > 0:
    st.balloons()
    st.markdown(
        '<div class="fade-in" style="text-align:center;padding:20px">'
        '<div style="font-size:2.5rem">🎉</div>'
        '<div style="font-size:1.3rem;font-weight:700;color:#4CAF50">'
        'All images classified!</div></div>', unsafe_allow_html=True)
    st.success("📊 Open your Google Sheet to see all responses (like Google Forms)")
