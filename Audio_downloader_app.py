import streamlit as st
import pandas as pd
import requests
import io
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
    page_icon="🎧",
    layout="wide"
)


# ============================================================
# CUSTOM CSS
# ============================================================

st.markdown("""
<style>

div[data-testid="stButton"] > button {
    background-color: #0066CC;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 18px;
    font-size: 14px;
    font-weight: 600;
    width: auto;
    min-width: 190px;
}

div[data-testid="stButton"] > button:hover {
    background-color: #004C99;
    color: white;
    border: none;
}

div[data-testid="stDownloadButton"] > button {
    background-color: #0066CC;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 14px;
    font-weight: 600;
    width: auto;
    min-width: 170px;
}

div[data-testid="stDownloadButton"] > button:hover {
    background-color: #004C99;
    color: white;
}

.auth-success {
    padding: 10px;
    border-radius: 6px;
    background-color: #e8f5e9;
    color: #1b5e20;
    font-weight: 600;
}

</style>
""", unsafe_allow_html=True)


# ============================================================
# SESSION STATE
# ============================================================

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if "session" not in st.session_state:
    st.session_state.session = None

if "username" not in st.session_state:
    st.session_state.username = None

if "download_running" not in st.session_state:
    st.session_state.download_running = False

if "download_complete" not in st.session_state:
    st.session_state.download_complete = False

if "zip_data" not in st.session_state:
    st.session_state.zip_data = None

if "failed_csv" not in st.session_state:
    st.session_state.failed_csv = None

if "download_results" not in st.session_state:
    st.session_state.download_results = []

if "progress" not in st.session_state:
    st.session_state.progress = 0


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.title("Audio Downloader")


# ------------------------------------------------------------
# AUTHENTICATION
# ------------------------------------------------------------

if not st.session_state.authenticated:

    st.sidebar.subheader("CommCare Authentication")

    commcare_domain = st.sidebar.text_input(
        "CommCare Domain",
        value="m-e-uganda"
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

            # Create authenticated session
            session = requests.Session()

            session.headers.update({
                "Authorization": f"ApiKey {username}:{api_key}",
                "User-Agent": "CommCare-Audio-Downloader/1.0",
                "Accept": "*/*"
            })

            # Save session temporarily
            st.session_state.session = session

            # ------------------------------------------------
            # Test authentication
            # ------------------------------------------------

            try:

                # CommCare API endpoint used to test authentication
                test_url = (
                    f"https://www.commcarehq.org/a/"
                    f"{commcare_domain}/api/v0.5/case/"
                )

                response = session.get(
                    test_url,
                    params={"limit": 1},
                    timeout=20
                )

                if response.status_code == 200:

                    st.session_state.authenticated = True
                    st.session_state.username = username

                    st.rerun()

                elif response.status_code == 401:

                    st.session_state.session = None

                    st.sidebar.error(
                        "Invalid username or API key."
                    )

                elif response.status_code == 403:

                    st.session_state.session = None

                    st.sidebar.error(
                        "Authentication succeeded, "
                        "but you do not have permission."
                    )

                else:

                    st.session_state.session = None

                    st.sidebar.error(
                        f"Authentication failed. "
                        f"HTTP {response.status_code}"
                    )

            except requests.exceptions.RequestException as e:

                st.session_state.session = None

                st.sidebar.error(
                    f"Connection error: {e}"
                )


# ------------------------------------------------------------
# AUTHENTICATED STATE
# ------------------------------------------------------------

else:

    st.sidebar.markdown(
        """
        <div class="auth-success">
        ✅ CommCare authenticated
        </div>
        """,
        unsafe_allow_html=True
    )

    st.sidebar.write("")

    st.sidebar.write(
        f"User: {st.session_state.username}"
    )

    logout = st.sidebar.button(
        "Log out"
    )

    if logout:

        st.session_state.authenticated = False
        st.session_state.session = None
        st.session_state.username = None

        st.session_state.download_running = False
        st.session_state.download_complete = False
        st.session_state.zip_data = None
        st.session_state.failed_csv = None
        st.session_state.download_results = []

        st.rerun()


# ============================================================
# MAIN TITLE
# ============================================================

st.title("🎧 CommCare Audio Downloader")

st.write(
    "Upload your Excel/CSV file, authenticate with CommCare, "
    "then download and rename the audio files automatically."
)


# ============================================================
# AUTHENTICATION CHECK
# ============================================================

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
# READ FILE
# ============================================================

try:

    file_name = uploaded_file.name.lower()

    if file_name.endswith(".csv"):

        df = pd.read_csv(uploaded_file)

    else:

        df = pd.read_excel(uploaded_file)

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

missing_columns = [
    col for col in required_columns
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


# Remove rows without audio URL
df = df[
    (df["audio_name"] != "") &
    (df["survey_audio_link"] != "")
].copy()

df.reset_index(drop=True, inplace=True)


# ============================================================
# SHOW DATA
# ============================================================

st.subheader("Audio Files preview")

st.write(
    f"**{len(df)} audio files** ready for download."
)

st.dataframe(
    df,
    use_container_width=True,
    height=300
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
        max_value=10,
        value=5,
        step=1
    )

with col2:

    timeout = st.number_input(
        "Timeout per file (seconds)",
        min_value=10,
        max_value=300,
        value=60,
        step=10
    )


st.caption(
    "5 simultaneous downloads is a reasonable starting point. "
    "Increase this only if your connection and CommCare server "
    "handle the extra requests well."
)


# ============================================================
# FILENAME CLEANING
# ============================================================

def clean_filename(name):

    name = str(name).strip()

    # Remove invalid Windows filename characters
    name = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        name
    )

    # Remove control characters
    name = re.sub(
        r"[\x00-\x1f]",
        "",
        name
    )

    # Remove trailing dots/spaces
    name = name.rstrip(". ")

    if not name:
        name = "audio"

    return name


# ============================================================
# GET EXTENSION
# ============================================================

def get_extension(url):

    url_lower = url.lower()

    # Remove query parameters
    clean_url = url_lower.split("?")[0]

    extensions = [
        ".m4a",
        ".mp3",
        ".wav",
        ".aac",
        ".ogg",
        ".amr"
    ]

    for ext in extensions:

        if clean_url.endswith(ext):
            return ext

    return ".m4a"


# ============================================================
# MAKE UNIQUE FILENAMES
# ============================================================

def create_filename_map(dataframe):

    used_names = {}

    filename_map = []

    for name, url in zip(
        dataframe["audio_name"],
        dataframe["survey_audio_link"]
    ):

        base_name = clean_filename(name)

        extension = get_extension(url)

        filename = base_name + extension

        if filename.lower() not in used_names:

            used_names[filename.lower()] = 0

        else:

            used_names[filename.lower()] += 1

            count = used_names[filename.lower()]

            filename = (
                f"{base_name}_{count}{extension}"
            )

        filename_map.append(filename)

    return filename_map


df["filename"] = create_filename_map(df)


# ============================================================
# DOWNLOAD ONE AUDIO FILE
# ============================================================

def download_audio(
    session,
    url,
    filename,
    timeout
):

    max_retries = 3

    for attempt in range(max_retries):

        try:

            response = session.get(
                url,
                stream=True,
                timeout=timeout
            )

            # Authentication problem
            if response.status_code == 401:

                return {
                    "success": False,
                    "filename": filename,
                    "error": "HTTP 401 - Unauthorized"
                }

            if response.status_code == 403:

                return {
                    "success": False,
                    "filename": filename,
                    "error": "HTTP 403 - Permission denied"
                }

            if response.status_code != 200:

                if response.status_code in [
                    408,
                    429,
                    500,
                    502,
                    503,
                    504
                ] and attempt < max_retries - 1:

                    time.sleep(2 ** attempt)

                    continue

                return {
                    "success": False,
                    "filename": filename,
                    "error": f"HTTP {response.status_code}"
                }

            # Read audio into memory
            audio_data = io.BytesIO()

            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):

                if chunk:

                    audio_data.write(chunk)

            response.close()

            audio_data.seek(0)

            return {
                "success": True,
                "filename": filename,
                "data": audio_data.getvalue(),
                "error": ""
            }

        except requests.exceptions.RequestException as e:

            if attempt < max_retries - 1:

                time.sleep(2 ** attempt)

                continue

            return {
                "success": False,
                "filename": filename,
                "error": str(e)
            }

        except Exception as e:

            return {
                "success": False,
                "filename": filename,
                "error": str(e)
            }

    return {
        "success": False,
        "filename": filename,
        "error": "Unknown error"
    }


# ============================================================
# START DOWNLOAD
# ============================================================

start_download = st.button(
    "Download & Rename Audio",
    type="primary"
)


if start_download:

    st.session_state.download_running = True
    st.session_state.download_complete = False
    st.session_state.zip_data = None
    st.session_state.failed_csv = None
    st.session_state.download_results = []

    # --------------------------------------------------------
    # Create ZIP in memory
    # --------------------------------------------------------

    zip_buffer = io.BytesIO()

    successful_files = []
    failed_files = []

    total_files = len(df)

    progress_bar = st.progress(0)

    status_text = st.empty()

    # --------------------------------------------------------
    # Use authenticated session
    # --------------------------------------------------------

    session = st.session_state.session

    # --------------------------------------------------------
    # Background / parallel downloads
    # --------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=int(workers)
    ) as executor:

        futures = {}

        for _, row in df.iterrows():

            future = executor.submit(
                download_audio,
                session,
                row["survey_audio_link"],
                row["filename"],
                int(timeout)
            )

            futures[future] = row

        completed = 0

        for future in as_completed(futures):

            row = futures[future]

            try:

                result = future.result()

            except Exception as e:

                result = {
                    "success": False,
                    "filename": row["filename"],
                    "error": str(e)
                }

            completed += 1

            percentage = completed / total_files

            progress_bar.progress(
                percentage
            )

            status_text.write(
                f"⬇ Downloading... "
                f"{completed}/{total_files}"
            )

            # -----------------------------------------------
            # Successful
            # -----------------------------------------------

            if result["success"]:

                successful_files.append(
                    result
                )

            # -----------------------------------------------
            # Failed
            # -----------------------------------------------

            else:

                failed_files.append({
                    "audio_name": row["audio_name"],
                    "survey_audio_link": row["survey_audio_link"],
                    "filename": row["filename"],
                    "error": result["error"]
                })

    # ========================================================
    # CREATE ZIP
    # ========================================================

    status_text.write(
        "Creating ZIP file..."
    )

    with zipfile.ZipFile(
        zip_buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED
    ) as zip_file:

        for result in successful_files:

            zip_file.writestr(
                result["filename"],
                result["data"]
            )

    zip_buffer.seek(0)

    # Save ZIP to session state
    st.session_state.zip_data = zip_buffer.getvalue()

    # ========================================================
    # CREATE FAILED CSV
    # ========================================================

    if failed_files:

        failed_df = pd.DataFrame(
            failed_files
        )

        st.session_state.failed_csv = (
            failed_df.to_csv(index=False)
            .encode("utf-8")
        )

    else:

        st.session_state.failed_csv = None

    # ========================================================
    # SAVE RESULTS
    # ========================================================

    st.session_state.download_results = {
        "successful": len(successful_files),
        "failed": len(failed_files),
        "total": total_files
    }

    st.session_state.download_running = False
    st.session_state.download_complete = True

    progress_bar.progress(1.0)

    status_text.success(
        "✅ Download completed!"
    )


# ============================================================
# RESULTS
# ============================================================

if st.session_state.download_complete:

    results = st.session_state.download_results

    st.subheader("Download Results")

    col1, col2, col3 = st.columns(3)

    with col1:

        st.metric(
            "Total Files",
            results["total"]
        )

    with col2:

        st.metric(
            "Downloaded",
            results["successful"]
        )

    with col3:

        st.metric(
            "Failed",
            results["failed"]
        )


    # ========================================================
    # DOWNLOAD BUTTONS
    # ========================================================

    st.subheader("Download Results")

    col1, col2 = st.columns(2)

    # --------------------------------------------------------
    # ZIP DOWNLOAD
    # --------------------------------------------------------

    with col1:

        if st.session_state.zip_data is not None:

            st.download_button(
                label="Download Audio ZIP",
                data=st.session_state.zip_data,
                file_name="commcare_audio_files.zip",
                mime="application/zip"
            )

    # --------------------------------------------------------
    # FAILED CSV
    # --------------------------------------------------------

    with col2:

        if st.session_state.failed_csv is not None:

            st.download_button(
                label="Download Failed CSV",
                data=st.session_state.failed_csv,
                file_name="failed_audio_files.csv",
                mime="text/csv"
            )

        else:

            st.success(
                "All audio files downloaded successfully!"
            )