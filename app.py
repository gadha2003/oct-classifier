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
    "https://www.googleapis.com/auth/drive",
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

# ─── OUTPUT FOLDER MANAGEMENT (service account owns these → no permission issues)
def find_sa_folder(name, parent_id=None):
    """Find a folder by name owned by service account."""
    drive = get_drive()
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         f"and trashed=false")
    if parent_id:
        q += f" and '{parent_id}' in parents"
    results = drive.files().list(q=q, fields="files(id)", pageSize=5).execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None

def create_sa_folder(name, parent_id=None):
    """Create a folder owned by service account."""
    drive = get_drive()
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = drive.files().create(body=meta, fields="id").execute()
    return folder["id"]

def share_folder_with_anyone(folder_id):
    """Make folder viewable by anyone with link so user can access it."""
    drive = get_drive()
    try:
        drive.permissions().create(
            fileId=folder_id,
            body={"type": "anyone", "role": "writer"},
            fields="id",
        ).execute()
    except Exception:
        try:
            drive.permissions().create(
                fileId=folder_id,
                body={"type": "anyone", "role": "reader"},
                fields="id",
            ).execute()
        except Exception:
            pass  # Not critical

@st.cache_data(ttl=300)
def setup_output_folders():
    """
    Create the output folder structure owned by the service account:
    OCT_Classified_Output/
      Grade1_Mild/
      Grade2_Moderate/
      Grade3_Severe/
    Returns: {grade_folder_name: folder_id} or None if failed.
    """
    try:
        root_name = "OCT_Classified_Output"
        root_id = find_sa_folder(root_name)
        if not root_id:
            root_id = create_sa_folder(root_name)
            share_folder_with_anyone(root_id)

        grade_ids = {}
        for g in GRADES.values():
            fid = find_sa_folder(g["folder"], root_id)
            if not fid:
                fid = create_sa_folder(g["folder"], root_id)
            grade_ids[g["folder"]] = fid

        return {"root": root_id, "grades": grade_ids}
    except Exception as e:
        st.toast(f"⚠️ Could not create output folders: {e}", icon="⚠️")
        return None

def upload_to_grade_folder(source_file_id, dest_folder_id, filename, mime="image/jpeg"):
    """Download from source, upload to grade folder. Service account owns dest."""
    drive = get_drive()
    # Download
    request = drive.files().get_media(fileId=source_file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)

    # Check if file already exists in dest (avoid duplicates)
    q = f"'{dest_folder_id}' in parents and name='{filename}' and trashed=false"
    existing = drive.files().list(q=q, fields="files(id)").execute().get("files", [])
    if existing:
        return existing[0]["id"]

    # Upload (non-resumable for small files — more reliable)
    media = MediaIoBaseUpload(buf, mimetype=mime, resumable=False)
    result = drive.files().create(
        body={"name": filename, "parents": [dest_folder_id]},
        media_body=media,
        fields="id",
    ).execute()
    return result["id"]

def remove_from_grade_folder(dest_folder_id, filename):
    """Remove a classified image from a grade folder."""
    drive = get_drive()
    q = f"'{dest_folder_id}' in parents and name='{filename}' and trashed=false"
    results = drive.files().list(q=q, fields="files(id)").execute()
    for f in results.get("files", []):
        drive.files().delete(fileId=f["id"]).execute()

# ─── GOOGLE SHEET HELPERS ─────────────────────────────────────────────────────
def ensure_sheet_headers():
    sheet = get_sheet()
    try:
        row1 = sheet.row_values(1)
        if row1 != SHEET_HEADERS:
            sheet.insert_row(SHEET_HEADERS, index=1)
    except Exception:
        sheet.insert_row(SHEET_HEADERS, index=1)

def load_global_progress():
    sheet = get_sheet()
    try:
        records = sheet.get_all_records()
    except Exception:
        return {}
    progress = {}
    for r in records:
        fname = r.get("image", "")
        if fname:
            progress[fname] = {
                "annotator": r.get("annotator", ""),
                "grade":     r.get("grade", ""),
            }
    return progress

def append_to_sheet(annotator, filename, grade, file_id):
    sheet = get_sheet()
    sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        annotator, filename, grade, file_id,
    ])

def remove_from_sheet(filename, annotator):
    sheet = get_sheet()
    try:
        records = sheet.get_all_records()
        for i, r in enumerate(records):
            if r.get("image") == filename and r.get("annotator") == annotator:
                sheet.delete_rows(i + 2)
                return
    except Exception:
        pass

def get_annotator_history(annotator):
    sheet = get_sheet()
    try:
        records = sheet.get_all_records()
    except Exception:
        return {}, 0
    classifications = {}
    for r in records:
        if r.get("annotator") == annotator:
            classifications[r.get("image", "")] = r.get("grade", "")
    return classifications, len(classifications)

def get_all_annotator_names():
    sheet = get_sheet()
    try:
        records = sheet.get_all_records()
    except Exception:
        return {}
    names = {}
    for r in records:
        a = r.get("annotator", "")
        if a:
            names[a] = names.get(a, 0) + 1
    return names

# ─── SESSION STATE ────────────────────────────────────────────────────────────
defaults = {
    "images": [], "idx": 0, "classifications": {},
    "loaded": False, "annotator": "", "logged_in": False,
    "just_classified": None, "global_progress": {},
    "output_folders": None, "file_sort_ok": True,
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
    

    # Setup output folders (service account creates & owns them)
    output = setup_output_folders()
    st.session_state.output_folders = output

    st.session_state.images = images
    st.session_state.global_progress = gp
    st.session_state.classifications = my_prev

    classified_set = set(gp.keys())
    first_pending = 0
    for i, img in enumerate(images):
        if img["name"] not in classified_set:
            first_pending = i
            break
    st.session_state.idx = first_pending
    st.session_state.loaded = True

    folders_ok = "✅ files will be sorted" if output else "📊 Sheet-only mode"
    already = len(classified_set & {f["name"] for f in images})
    st.toast(f"✅ {len(images)} images · {already} done · {folders_ok}", icon="🎉")

def classify(grade):
    img = st.session_state.images[st.session_state.idx]
    fname = img["name"]
    file_id = img["id"]
    mime = img.get("mime", "image/jpeg")

    # ── Remove old classification if re-labeling
    old = st.session_state.classifications.get(fname)
    if old and old != grade:
        remove_from_sheet(fname, st.session_state.annotator)
        # Try removing old file from Drive grade folder
        output = st.session_state.output_folders
        if output:
            old_folder_id = output["grades"].get(GRADES[old]["folder"])
            if old_folder_id:
                try:
                    remove_from_grade_folder(old_folder_id, fname)
                except Exception:
                    pass

    # ── 1. Always log to Sheet (source of truth — never fails)
    append_to_sheet(st.session_state.annotator, fname, grade, file_id)

    # ── 2. Try to copy file to grade folder on Drive (bonus — may fail)
    output = st.session_state.output_folders
    if output and st.session_state.file_sort_ok:
        dest_folder_id = output["grades"].get(GRADES[grade]["folder"])
        if dest_folder_id:
            try:
                upload_to_grade_folder(file_id, dest_folder_id, fname, mime)
            except Exception as e:
                st.session_state.file_sort_ok = False
                st.toast(f"⚠️ File sort failed (Sheet still logged): {str(e)[:80]}",
                         icon="⚠️")

    # ── 3. Update local state
    st.session_state.classifications[fname] = grade
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
    grade = st.session_state.classifications.pop(fname, None)
    if grade:
        remove_from_sheet(fname, st.session_state.annotator)
        # Try removing from Drive
        output = st.session_state.output_folders
        if output:
            folder_id = output["grades"].get(GRADES[grade]["folder"])
            if folder_id:
                try:
                    remove_from_grade_folder(folder_id, fname)
                except Exception:
                    pass
        gp = st.session_state.global_progress.get(fname)
        if gp and gp["annotator"] == st.session_state.annotator:
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
        '<div class="fade-in" style="font-size:1.05rem;color:#666;margin-bottom:32px">'
        'Manual severity grading · relay mode · resume anytime</div>',
        unsafe_allow_html=True)

    col1, col2 = st.columns([3, 1])
    with col1:
        st.text_input("Enter your name to begin", key="name_input",
                      placeholder="e.g. Gadha Suresh", label_visibility="collapsed")
    with col2:
        st.button("Start →", on_click=do_login, type="primary",
                  use_container_width=True)

    st.markdown("---")
    try:
        prev = get_all_annotator_names()
        if prev:
            st.markdown("**Previous annotators**")
            for name, count in prev.items():
                st.markdown(
                    f'<div class="grade-card" style="border-left:4px solid #667eea;">'
                    f'👤 <b>{name}</b> — {count} images classified</div>',
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
        my_prev, my_count = get_annotator_history(st.session_state.annotator)
        if my_count > 0 and "resume_answered" not in st.session_state:
            st.markdown(
                f'<div class="fade-in" style="text-align:center;padding:20px">'
                f'<div style="font-size:1.3rem;font-weight:600">Welcome back, '
                f'{st.session_state.annotator}!</div>'
                f'<div style="color:#888;margin-top:8px">'
                f'{my_count} previous classifications found.</div></div>',
                unsafe_allow_html=True)
            st.markdown("")
            r1, r2 = st.columns(2)
            with r1:
                if st.button("▶️ Resume", type="primary", use_container_width=True):
                    st.session_state.resume_answered = True
                    load_images_from_drive()
                    st.rerun()
            with r2:
                if st.button("🆕 Start fresh", use_container_width=True):
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
    # Show output folder link (add this right after the Load button in sidebar)
    if st.session_state.output_folders:
        root_id = st.session_state.output_folders["root"]
        st.markdown(
            f'📁 [Open classified folder]'
            f'(https://drive.google.com/drive/folders/{root_id})',
            unsafe_allow_html=True)

      
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

        # File sorting status
        if st.session_state.output_folders and st.session_state.file_sort_ok:
            st.markdown(
                '<div style="color:#4CAF50;font-size:12px">📁 Files sorting to Drive ✓</div>',
                unsafe_allow_html=True)
        else:
            st.markdown(
                '<div style="color:#999;font-size:12px">📊 Sheet-only mode</div>',
                unsafe_allow_html=True)

        if pending > 0:
            st.markdown(
                f'<div style="color:#D85A30;font-weight:600">⏳ {pending} pending</div>',
                unsafe_allow_html=True)

        counts = {"Mild": 0, "Moderate": 0, "Severe": 0}
        for g in st.session_state.classifications.values():
            if g in counts:
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
gp     = st.session_state.global_progress

if total == 0:
    st.warning("No images loaded.")
    st.stop()

cur        = images[idx]
fname      = cur["name"]
file_id    = cur["id"]
my_grade   = st.session_state.classifications.get(fname)
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

if my_grade:
    c = GRADES[my_grade]["color"]
    e = GRADES[my_grade]["emoji"]
    st.markdown(
        f'<div class="slide-in pulse" style="display:inline-block;'
        f'background:{c}15;border:1.5px solid {c};border-radius:10px;'
        f'padding:6px 16px;color:{c};font-weight:600;margin:8px 0">'
        f'{e} You labeled: {my_grade}</div>', unsafe_allow_html=True)
elif other_info:
    oi = other_info.get("grade","")
    oe = GRADES.get(oi,{}).get("emoji","")
    st.markdown(
        f'<div class="slide-in" style="display:inline-block;'
        f'background:#667eea20;border:1.5px solid #667eea;border-radius:10px;'
        f'padding:6px 16px;color:#667eea;font-weight:600;margin:8px 0">'
        f'👤 {other_info["annotator"]} → {oe} {oi}</div>',
        unsafe_allow_html=True)
else:
    st.markdown(
        '<div class="fade-in" style="display:inline-block;'
        'background:#f0f2f6;border:1.5px dashed #ccc;border-radius:10px;'
        'padding:6px 16px;color:#999;margin:8px 0">'
        '⬜ Not yet classified</div>', unsafe_allow_html=True)

try:
    with st.spinner(""):
        img_bytes = download_image_bytes(file_id)
        img = Image.open(img_bytes)
    st.markdown('<div class="fade-in">', unsafe_allow_html=True)
    st.image(img, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)
except Exception as e:
    st.error(f"Could not load image: {e}")

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

if all_done == total and total > 0:
    st.balloons()
    st.markdown(
        '<div class="fade-in" style="text-align:center;padding:20px">'
        '<div style="font-size:2.5rem">🎉</div>'
        '<div style="font-size:1.3rem;font-weight:700;color:#4CAF50">'
        'All images classified!</div></div>', unsafe_allow_html=True)

    annotator_counts = {}
    for f, info in gp.items():
        a = info["annotator"]
        if a not in annotator_counts:
            annotator_counts[a] = {"Mild":0,"Moderate":0,"Severe":0,"total":0}
        if info["grade"] in annotator_counts[a]:
            annotator_counts[a][info["grade"]] += 1
        annotator_counts[a]["total"] += 1

    st.markdown("**Contributions by annotator**")
    for a, c in annotator_counts.items():
        st.markdown(
            f'<div class="grade-card" style="border-left:4px solid #667eea;">'
            f'👤 <b>{a}</b> — {c["total"]} images '
            f'(🟡{c["Mild"]} 🟠{c["Moderate"]} 🔴{c["Severe"]})</div>',
            unsafe_allow_html=True)

    # Show output folder link if available
    output = st.session_state.output_folders
    if output:
        root_id = output["root"]
        st.success(
            f'📁 Classified files: '
            f'[Open in Drive](https://drive.google.com/drive/folders/{root_id})\n\n'
            f'📊 Full log: Open your Google Sheet')
    else:
        st.success("📊 All responses logged in Google Sheet!")
