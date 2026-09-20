import streamlit as st
import pandas as pd
import requests
import io
import threading
import zipfile
import re
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="CommCare Audio Downloader",
    layout="wide"
)


# ============================================================
# SETTINGS
# ============================================================

DOWNLOAD_DIR = "commcare_audio_downloads"
TEMP_DIR = os.path.join(DOWNLOAD_DIR, "_partial")
MANIFEST_FILE = os.path.join(DOWNLOAD_DIR, "download_manifest.csv")
FINAL_ZIP_FILE = os.path.join(DOWNLOAD_DIR, "commcare_audio_files_final.zip")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

manifest_lock = threading.Lock()


# ============================================================
# CUSTOM CSS
# ============================================================

st.markdown("""
<style>
div[data-testid="stButton"] > button,
div[data-testid="stDownloadButton"] > button {
    background-color: #0066CC;
    color: white;
    border: none;
    border-radius: 3px;
    padding: 6px 16px;
    font-size: 14px;
    font-weight: 600;
}

div[data-testid="stButton"] > button:hover,
div[data-testid="stDownloadButton"] > button:hover {
    background-color: #004C99;
    color: white;
}

.auth-success {
    padding: 10px;
    border-radius: 3px;
    background-color: #e8f5e9;
    color: #1b5e20;
    font-weight: 600;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# SESSION STATE
# ============================================================

defaults = {
    "authenticated": False,
    "session_headers": None,
    "username": None,
    "commcare_domain": "m-e-uganda",
    "download_complete": False,
    "failed_csv": None,
    "download_results": None,
    "zip_bytes": None,
    "zip_filename": None,
    "final_zip_path": None,
    "final_zip_size": 0,
    "zip_ready": False,
    "final_zip_ready": False,
}

for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ============================================================
# CONSTANTS
# ============================================================

MANIFEST_COLUMNS = [
    "filename",
    "audio_name",
    "survey_audio_link",
    "status",
    "size_bytes",
    "downloaded_at",
    "error",
]


# ============================================================
# FAST / SMALL HELPER FUNCTIONS
# ============================================================

def clean_filename(name):
    name = str(name).strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"[\x00-\x1f]", "", name)
    name = name.rstrip(". ")
    return name or "audio"


def get_extension(url):
    clean_url = str(url).lower().split("?")[0]

    for ext in [".m4a", ".mp3", ".wav", ".aac", ".ogg", ".amr"]:
        if clean_url.endswith(ext):
            return ext

    return ".m4a"


def create_filename_map(dataframe):
    """Vector-independent, fast enough for normal upload sizes."""
    used_names = {}
    filenames = []

    for name, url in zip(
        dataframe["audio_name"].tolist(),
        dataframe["survey_audio_link"].tolist()
    ):
        base_name = clean_filename(name)
        extension = get_extension(url)

        filename = base_name + extension
        key = filename.lower()

        if key not in used_names:
            used_names[key] = 0
        else:
            used_names[key] += 1
            filename = f"{base_name}_{used_names[key]}{extension}"

        filenames.append(filename)

    return filenames


def file_path(filename):
    return os.path.join(DOWNLOAD_DIR, filename)


def partial_file_path(filename):
    return os.path.join(TEMP_DIR, filename + ".part")


def get_existing_files():
    """
    Build the existing-file set once instead of calling os.path.isfile()
    repeatedly for every row and every Streamlit rerun.
    """
    try:
        return {
            name
            for name in os.listdir(DOWNLOAD_DIR)
            if os.path.isfile(os.path.join(DOWNLOAD_DIR, name))
            and not name.endswith(".zip")
            and name != os.path.basename(MANIFEST_FILE)
        }
    except OSError:
        return set()


def get_completed_files():
    existing = get_existing_files()
    completed = []

    for filename in existing:
        try:
            if os.path.getsize(file_path(filename)) > 0:
                completed.append(filename)
        except OSError:
            pass

    return sorted(completed)


# ============================================================
# MANIFEST
# ============================================================

@st.cache_data(ttl=10, show_spinner=False)
def load_manifest_cached(manifest_path, modified_time):
    if not os.path.exists(manifest_path):
        return pd.DataFrame(columns=MANIFEST_COLUMNS)

    try:
        manifest = pd.read_csv(manifest_path)

        for col in MANIFEST_COLUMNS:
            if col not in manifest.columns:
                manifest[col] = ""

        return manifest[MANIFEST_COLUMNS]

    except Exception:
        return pd.DataFrame(columns=MANIFEST_COLUMNS)


def load_manifest():
    try:
        mtime = os.path.getmtime(MANIFEST_FILE)
    except OSError:
        mtime = 0

    return load_manifest_cached(MANIFEST_FILE, mtime)


def append_manifest_rows(rows):
    """
    Write the manifest ONCE after a download batch.

    The old code read and rewrote the whole CSV for every audio file.
    With hundreds/thousands of files that caused a major slowdown.
    """
    if not rows:
        return

    with manifest_lock:
        try:
            existing = load_manifest().copy()
        except Exception:
            existing = pd.DataFrame(columns=MANIFEST_COLUMNS)

        new_df = pd.DataFrame(rows, columns=MANIFEST_COLUMNS)

        if not existing.empty:
            keys = set(
                zip(
                    existing["filename"].astype(str),
                    existing["survey_audio_link"].astype(str)
                )
            )

            new_df = new_df[
                ~new_df.apply(
                    lambda r: (
                        str(r["filename"]),
                        str(r["survey_audio_link"])
                    ) in keys,
                    axis=1
                )
            ]

        combined = pd.concat(
            [existing, new_df],
            ignore_index=True
        )

        temp_manifest = MANIFEST_FILE + ".tmp"
        combined.to_csv(temp_manifest, index=False)
        os.replace(temp_manifest, MANIFEST_FILE)

        load_manifest_cached.clear()


def make_manifest_row(filename, audio_name, url, status, size_bytes=0, error=""):
    return {
        "filename": filename,
        "audio_name": audio_name,
        "survey_audio_link": url,
        "status": status,
        "size_bytes": size_bytes,
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
    }


# ============================================================
# ZIP FUNCTIONS
# ============================================================

def create_final_zip_file():
    """Create the final ZIP directly on disk for more reliable deployment downloads."""
    try:
        if os.path.exists(FINAL_ZIP_FILE):
            os.remove(FINAL_ZIP_FILE)
    except OSError as e:
        raise OSError(f"Could not replace the existing final ZIP: {e}")

    completed_files = get_completed_files()
    if not completed_files:
        raise ValueError("No completed audio files are available.")

    try:
        with zipfile.ZipFile(FINAL_ZIP_FILE, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zip_file:
            for filename in completed_files:
                path = file_path(filename)
                try:
                    zip_file.write(path, arcname=filename)
                except OSError:
                    continue
    except Exception:
        try:
            if os.path.exists(FINAL_ZIP_FILE):
                os.remove(FINAL_ZIP_FILE)
        except OSError:
            pass
        raise

    if not os.path.exists(FINAL_ZIP_FILE) or os.path.getsize(FINAL_ZIP_FILE) <= 0:
        raise IOError("The final ZIP was not created or is empty.")

    return FINAL_ZIP_FILE, os.path.getsize(FINAL_ZIP_FILE)


def make_failed_csv(failed_files):
    if not failed_files:
        return None

    return pd.DataFrame(failed_files).to_csv(
        index=False
    ).encode("utf-8")


# ============================================================
# DELETE SAVED AUDIO
# ============================================================

def delete_saved_audio(reset_manifest=True):
    deleted = 0
    errors = []

    # Completed audio
    for filename in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, filename)

        if not os.path.isfile(path):
            continue

        if filename == os.path.basename(MANIFEST_FILE):
            continue

        if filename.endswith(".zip"):
            continue

        try:
            os.remove(path)
            deleted += 1
        except OSError as e:
            errors.append(f"{filename}: {e}")

    # Partial files
    if os.path.isdir(TEMP_DIR):
        for filename in os.listdir(TEMP_DIR):
            path = os.path.join(TEMP_DIR, filename)

            if not os.path.isfile(path):
                continue

            try:
                os.remove(path)
                deleted += 1
            except OSError as e:
                errors.append(f"_partial/{filename}: {e}")

    if reset_manifest:
        try:
            empty_manifest = pd.DataFrame(columns=MANIFEST_COLUMNS)
            temp_manifest = MANIFEST_FILE + ".tmp"
            empty_manifest.to_csv(temp_manifest, index=False)
            os.replace(temp_manifest, MANIFEST_FILE)
            load_manifest_cached.clear()
        except OSError as e:
            errors.append(f"download_manifest.csv: {e}")

    return deleted, errors


# ============================================================
# DOWNLOAD ONE AUDIO
# ============================================================

def download_audio(
    headers,
    url,
    filename,
    audio_name,
    timeout,
    existing_files
):
    """
    Download one audio file.

    Important performance changes:
    - Existing files are checked using an in-memory set.
    - Manifest is NOT rewritten by each worker.
    - A fresh requests.Session is used per worker call.
    - Files are written directly to disk.
    - .part files protect incomplete downloads.
    """

    final_path = file_path(filename)
    temp_path = partial_file_path(filename)

    # Fast existing-file check
    if filename in existing_files:
        try:
            size = os.path.getsize(final_path)
        except OSError:
            size = 0

        return {
            "success": True,
            "skipped": True,
            "filename": filename,
            "audio_name": audio_name,
            "url": url,
            "error": "",
            "size_bytes": size,
        }

    max_retries = 3

    for attempt in range(max_retries):
        try:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

            # Each worker gets its own session.
            # This avoids sharing one requests.Session between threads.
            session = requests.Session()
            session.headers.update(headers)

            with session.get(
                url,
                stream=True,
                timeout=timeout
            ) as response:

                if response.status_code == 401:
                    return {
                        "success": False,
                        "skipped": False,
                        "filename": filename,
                        "audio_name": audio_name,
                        "url": url,
                        "error": "HTTP 401 - Unauthorized",
                        "size_bytes": 0,
                    }

                if response.status_code == 403:
                    return {
                        "success": False,
                        "skipped": False,
                        "filename": filename,
                        "audio_name": audio_name,
                        "url": url,
                        "error": "HTTP 403 - Permission denied",
                        "size_bytes": 0,
                    }

                if response.status_code != 200:
                    retryable = response.status_code in [
                        408, 429, 500, 502, 503, 504
                    ]

                    if retryable and attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue

                    return {
                        "success": False,
                        "skipped": False,
                        "filename": filename,
                        "audio_name": audio_name,
                        "url": url,
                        "error": f"HTTP {response.status_code}",
                        "size_bytes": 0,
                    }

                total_bytes = 0

                with open(temp_path, "wb") as audio_file:
                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024
                    ):
                        if chunk:
                            audio_file.write(chunk)
                            total_bytes += len(chunk)

            if total_bytes <= 0:
                raise IOError(
                    "The server returned an empty audio file."
                )

            # Atomic completion
            os.replace(temp_path, final_path)

            return {
                "success": True,
                "skipped": False,
                "filename": filename,
                "audio_name": audio_name,
                "url": url,
                "error": "",
                "size_bytes": total_bytes,
            }

        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue

            return {
                "success": False,
                "skipped": False,
                "filename": filename,
                "audio_name": audio_name,
                "url": url,
                "error": str(e),
                "size_bytes": 0,
            }

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue

            return {
                "success": False,
                "skipped": False,
                "filename": filename,
                "audio_name": audio_name,
                "url": url,
                "error": str(e),
                "size_bytes": 0,
            }

    return {
        "success": False,
        "skipped": False,
        "filename": filename,
        "audio_name": audio_name,
        "url": url,
        "error": "Unknown download error",
        "size_bytes": 0,
    }


# ============================================================
# SIDEBAR / AUTHENTICATION
# ============================================================

st.sidebar.title("Audio Downloader")

if not st.session_state.authenticated:

    st.sidebar.subheader("CommCare Authentication")

    commcare_domain = st.sidebar.text_input(
        "CommCare Domain",
        value=st.session_state.commcare_domain
    )

    username = st.sidebar.text_input(
        "CommCare Username"
    )

    api_key = st.sidebar.text_input(
        "CommCare API Key",
        type="password"
    )

    verify_key = st.sidebar.button(
        "Verify API Key",
        type="primary"
    )

    if verify_key:

        if not username or not api_key:
            st.sidebar.error(
                "Please enter your username and API key."
            )

        else:
            headers = {
                "Authorization": f"ApiKey {username}:{api_key}",
                "User-Agent": "CommCare-Audio-Downloader/3.0",
                "Accept": "*/*",
            }

            test_url = (
                f"https://www.commcarehq.org/a/"
                f"{commcare_domain}/api/v0.5/case/"
            )

            try:
                response = requests.get(
                    test_url,
                    headers=headers,
                    params={"limit": 1},
                    timeout=20
                )

                if response.status_code == 200:
                    st.session_state.authenticated = True
                    st.session_state.username = username
                    st.session_state.commcare_domain = commcare_domain
                    st.session_state.session_headers = headers
                    st.rerun()

                elif response.status_code == 401:
                    st.sidebar.error(
                        "Invalid username or API key."
                    )

                elif response.status_code == 403:
                    st.sidebar.error(
                        "Authentication succeeded, but you do not have permission."
                    )

                else:
                    st.sidebar.error(
                        f"Authentication failed. HTTP {response.status_code}"
                    )

            except requests.exceptions.RequestException as e:
                st.sidebar.error(f"Connection error: {e}")

else:

    st.sidebar.markdown(
        """
        <div class="auth-success">
        ✅ CommCare authenticated
        </div>
        """,
        unsafe_allow_html=True
    )

    st.sidebar.write(
        f"User: {st.session_state.username}"
    )

    if st.sidebar.button("Log out"):
        st.session_state.authenticated = False
        st.session_state.session_headers = None
        st.session_state.username = None
        st.session_state.download_complete = False
        st.session_state.failed_csv = None
        st.session_state.download_results = None
        st.session_state.zip_bytes = None
        st.session_state.zip_filename = None
        st.session_state.final_zip_path = None
        st.session_state.final_zip_size = 0
        st.session_state.zip_ready = False
        st.rerun()


# ============================================================
# MAIN TITLE
# ============================================================

st.title("CommCare Audio Downloader")

st.write(
    "Upload your Excel/CSV file, authenticate with CommCare, "
    "then download and rename the audio files automatically."
)


if not st.session_state.authenticated:
    st.info(
        "Please enter your CommCare username and API key "
        "in the sidebar to continue."
    )
    st.stop()


# ============================================================
# FILE UPLOAD
# ============================================================

st.subheader("📁 Upload Audio List")

uploaded_file = st.file_uploader(
    "Upload Excel or CSV file",
    type=["xlsx", "xls", "csv"]
)

if uploaded_file is None:
    st.info(
        "Upload a file containing the columns "
        "`audio_name` and `survey_audio_link`."
    )
    st.stop()


# ============================================================
# FAST FILE READING
# ============================================================

# Streamlit reruns the script frequently. Cache the parsed upload.
@st.cache_data(show_spinner="Reading uploaded file...")
def read_uploaded_file(file_bytes, file_name):
    buffer = io.BytesIO(file_bytes)

    if file_name.lower().endswith(".csv"):
        return pd.read_csv(buffer)

    return pd.read_excel(buffer)


try:
    file_bytes = uploaded_file.getvalue()
    df = read_uploaded_file(
        file_bytes,
        uploaded_file.name
    )

except Exception as e:
    st.error(f"Could not read the file: {e}")
    st.stop()


# ============================================================
# CHECK REQUIRED COLUMNS
# ============================================================

required_columns = [
    "audio_name",
    "survey_audio_link"
]

# Strip whitespace from headers only once
df.columns = [
    str(col).strip()
    for col in df.columns
]

missing_columns = [
    col
    for col in required_columns
    if col not in df.columns
]

if missing_columns:
    st.error(
        "Missing required columns: "
        + ", ".join(missing_columns)
    )

    st.write("Columns found in your file:")
    st.write(list(df.columns))
    st.stop()


# ============================================================
# CLEAN DATA
# ============================================================

df = df[required_columns].copy()

df["audio_name"] = (
    df["audio_name"]
    .fillna("")
    .astype(str)
    .str.strip()
)

df["survey_audio_link"] = (
    df["survey_audio_link"]
    .fillna("")
    .astype(str)
    .str.strip()
)

df = df[
    (df["audio_name"] != "") &
    (df["survey_audio_link"] != "")
].reset_index(drop=True)


# ============================================================
# CREATE FILENAMES
# ============================================================

df["filename"] = create_filename_map(df)


# ============================================================
# EXISTING FILES
# ============================================================

# Build this set ONCE for this Streamlit run.
existing_files = get_existing_files()

df["already_downloaded"] = df["filename"].isin(
    existing_files
)

already_count = int(
    df["already_downloaded"].sum()
)

remaining_count = len(df) - already_count


# ============================================================
# PREVIEW
# ============================================================

st.subheader("Audio Files Preview")

st.write(
    f"**{len(df):,} audio files** found in your upload."
)

preview_columns = [
    "audio_name",
    "filename",
    "already_downloaded"
]

st.dataframe(
    df[preview_columns],
    use_container_width=True,
    height=300
)

preview_col1, preview_col2 = st.columns(2)

with preview_col1:
    st.metric(
        "Already downloaded",
        already_count
    )

with preview_col2:
    st.metric(
        "Remaining",
        remaining_count
    )


# ============================================================
# DOWNLOAD SETTINGS
# ============================================================

st.subheader("Download Settings")

col1, col2 = st.columns(2)

with col1:
    workers = st.number_input(
        "Number of simultaneous downloads",
        min_value=1,
        max_value=20,
        value=3,
        step=1,
        help="3 is a good starting point. Increase carefully if CommCare/network allows it."
    )

with col2:
    timeout = st.number_input(
        "Timeout per file (seconds)",
        min_value=10,
        max_value=300,
        value=100,
        step=10
    )

st.caption(
    "Higher simultaneous downloads can be faster, but too many workers "
    "may overload the connection or trigger server rate limits."
)


# ============================================================
# DOWNLOAD BUTTON
# ============================================================

start_download = st.button(
    "⬇ Download",
    type="primary"
)


# ============================================================
# START DOWNLOAD
# ============================================================

if start_download:

    st.session_state.download_complete = False
    st.session_state.failed_csv = None
    st.session_state.download_results = None

    # Use the already-created in-memory set.
    pending_df = df[
        ~df["filename"].isin(existing_files)
    ].copy()

    total_requested = len(df)
    skipped_count = total_requested - len(pending_df)

    successful_count = 0
    failed_count = 0
    failed_files = []
    manifest_rows = []

    if pending_df.empty:

        st.success(
            "✅ All files in this list are already downloaded."
        )

        st.session_state.download_results = {
            "total": total_requested,
            "downloaded": 0,
            "skipped": skipped_count,
            "failed": 0,
        }

        st.session_state.download_complete = True

    else:

        st.info(
            f"Downloading {len(pending_df):,} remaining files. "
            f"{skipped_count:,} files will be skipped."
        )

        progress_bar = st.progress(0)
        status_text = st.empty()

        headers = st.session_state.session_headers
        total_pending = len(pending_df)
        completed = 0

        # Convert to tuples once instead of repeatedly using iterrows()
        download_tasks = [
            (
                row.audio_name,
                row.survey_audio_link,
                row.filename
            )
            for row in pending_df.itertuples(index=False)
        ]

        with ThreadPoolExecutor(
            max_workers=int(workers)
        ) as executor:

            futures = {
                executor.submit(
                    download_audio,
                    headers,
                    url,
                    filename,
                    audio_name,
                    int(timeout),
                    existing_files
                ): (
                    audio_name,
                    url,
                    filename
                )
                for audio_name, url, filename in download_tasks
            }

            for future in as_completed(futures):

                audio_name, url, filename = futures[future]

                try:
                    result = future.result()

                except Exception as e:
                    result = {
                        "success": False,
                        "skipped": False,
                        "filename": filename,
                        "audio_name": audio_name,
                        "url": url,
                        "error": str(e),
                        "size_bytes": 0,
                    }

                completed += 1

                progress_bar.progress(
                    completed / total_pending
                )

                if result["success"]:

                    successful_count += 1

                    manifest_rows.append(
                        make_manifest_row(
                            filename=result["filename"],
                            audio_name=result["audio_name"],
                            url=result["url"],
                            status="downloaded",
                            size_bytes=result["size_bytes"]
                        )
                    )

                    status_text.write(
                        f"✅ {completed:,}/{total_pending:,} "
                        f"completed — {result['filename']}"
                    )

                else:

                    failed_count += 1

                    failed_files.append({
                        "audio_name": audio_name,
                        "survey_audio_link": url,
                        "filename": filename,
                        "error": result["error"],
                    })

                    manifest_rows.append(
                        make_manifest_row(
                            filename=filename,
                            audio_name=audio_name,
                            url=url,
                            status="failed",
                            size_bytes=0,
                            error=result["error"]
                        )
                    )

                    status_text.write(
                        f"❌ {completed:,}/{total_pending:,} "
                        f"failed — {filename}"
                    )

        # IMPORTANT PERFORMANCE CHANGE:
        # Write the manifest once instead of once per audio file.
        append_manifest_rows(manifest_rows)

        progress_bar.progress(1.0)

        st.session_state.download_results = {
            "total": total_requested,
            "downloaded": successful_count,
            "skipped": skipped_count,
            "failed": failed_count,
        }

        st.session_state.failed_csv = make_failed_csv(
            failed_files
        )

        st.session_state.download_complete = True

        if failed_count == 0:
            status_text.success(
                "✅ Download completed successfully!"
            )
        else:
            status_text.warning(
                f"⚠️ Finished with {failed_count} failed file(s). "
                "Click download again to retry the failed files."
            )


# ============================================================
# RESULTS
# ============================================================

if st.session_state.download_complete:

    results = st.session_state.download_results

    if results:

        st.subheader("Download Results")

        col1, col2, col3, col4 = st.columns(4)

        with col1:
            st.metric("Total Files", results["total"])

        with col2:
            st.metric(
                "Newly Downloaded",
                results["downloaded"]
            )

        with col3:
            st.metric(
                "Already Saved",
                results["skipped"]
            )

        with col4:
            st.metric(
                "Failed",
                results["failed"]
            )


# ============================================================
# FINAL ZIP + CLEANUP
# ============================================================

st.divider()
st.subheader("Final ZIP")

completed_files = get_completed_files()

if completed_files:

    current_pending = df[
        ~df["filename"].isin(set(completed_files))
    ]

    all_current_files_complete = (
        len(df) > 0 and current_pending.empty
    )

    if all_current_files_complete:

        st.success(
            f"✅ All {len(df):,} files in the current upload are downloaded."
        )

        # ----------------------------------------------------
        # CREATE + DOWNLOAD ZIP WITH ONE BUTTON
        # ----------------------------------------------------

        if st.button(
            "Create & Download Final ZIP",
            key="create_download_zip",
            type="primary"
        ):

            try:

                with st.spinner(
                    f"Creating final ZIP from "
                    f"{len(completed_files):,} audio files..."
                ):

                    zip_path, zip_size = create_final_zip_file()

                # Read ZIP into memory
                with open(zip_path, "rb") as zip_file:
                    zip_data = zip_file.read()

                st.session_state.final_zip_path = zip_path
                st.session_state.final_zip_size = zip_size
                st.session_state.zip_filename = (
                    "commcare_audio_files_final.zip"
                )
                st.session_state.zip_ready = True
                st.session_state.final_zip_ready = True

                # ------------------------------------------------
                # AUTOMATIC BROWSER DOWNLOAD
                # ------------------------------------------------

                import base64

                zip_base64 = base64.b64encode(zip_data).decode()

                download_html = f"""
                <html>
                <body>

                <a
                    id="auto_download"
                    href="data:application/zip;base64,{zip_base64}"
                    download="commcare_audio_files_final.zip"
                >
                </a>

                <script>
                    document.getElementById(
                        "auto_download"
                    ).click();
                </script>

                </body>
                </html>
                """

                import streamlit.components.v1 as components

                components.html(
                    download_html,
                    height=0,
                    scrolling=False
                )

                st.success(
                    f"✅ Final ZIP created and download started "
                    f"({zip_size / (1024 * 1024):.1f} MB)."
                )

                st.caption(
                    "Your browser should now be downloading "
                    "commcare_audio_files_final.zip"
                )

            except Exception as e:

                st.session_state.final_zip_path = None
                st.session_state.final_zip_size = 0
                st.session_state.zip_ready = False
                st.session_state.final_zip_ready = False

                st.error(
                    f"❌ Could not create/download the Final ZIP: {e}"
                )

        # ----------------------------------------------------
        # SHOW ZIP INFORMATION IF IT ALREADY EXISTS
        # ----------------------------------------------------

        final_zip_path = st.session_state.get("final_zip_path")

        if (
            st.session_state.get("final_zip_ready", False)
            and final_zip_path
            and os.path.exists(final_zip_path)
            and os.path.getsize(final_zip_path) > 0
        ):

            zip_size_mb = (
                os.path.getsize(final_zip_path)
                / (1024 * 1024)
            )

            st.info(
                f"Final ZIP available on server: "
                f"{zip_size_mb:.1f} MB"
            )

            # Optional normal download button
            # Useful if automatic download is blocked by browser
            with open(final_zip_path, "rb") as zip_file:
                existing_zip_data = zip_file.read()

            st.download_button(
                label="⬇ Download Final ZIP Again",
                data=existing_zip_data,
                file_name="commcare_audio_files_final.zip",
                mime="application/zip",
                key="final_zip_download_again"
            )

            # ------------------------------------------------
            # CLEAR SERVER FILES
            # ------------------------------------------------

            if st.button(
                "🗑️ Clear Server Audio Files",
                key="clear_server_audio"
            ):

                deleted_count, cleanup_errors = (
                    delete_saved_audio(reset_manifest=True)
                )

                st.session_state.zip_bytes = None
                st.session_state.zip_filename = None
                st.session_state.final_zip_path = None
                st.session_state.final_zip_size = 0
                st.session_state.zip_ready = False
                st.session_state.final_zip_ready = False
                st.session_state.failed_csv = None
                st.session_state.download_complete = False
                st.session_state.download_results = None

                if cleanup_errors:

                    st.warning(
                        f"{deleted_count} file(s) removed, "
                        f"but some files could not be deleted: "
                        + "; ".join(cleanup_errors)
                    )

                else:

                    st.success(
                        f"✅ {deleted_count} server file(s) cleared."
                    )

                st.rerun()

    else:

        st.info(
            f"{len(current_pending):,} file(s) from the current "
            f"upload are still missing. The Final ZIP will be "
            f"available after all files have been downloaded."
        )

else:

    st.info(
        "No completed audio files are currently saved."
    )


# ============================================================
# FAILED FILES
# ============================================================

if st.session_state.failed_csv is not None:

    st.subheader("⚠️ Failed Downloads")

    st.download_button(
        label="⬇ Download Failed CSV",
        data=st.session_state.failed_csv,
        file_name="failed_audio_files.csv",
        mime="text/csv",
        key="failed_csv_download"
    )

    st.info(
        "Run the downloader again to retry failed files. "
        "Successfully downloaded files will be skipped."
    )


# ============================================================
# INFORMATION
# ============================================================

st.divider()

st.caption(
    f"Persistent download folder: {os.path.abspath(DOWNLOAD_DIR)}"
)
