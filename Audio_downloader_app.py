import streamlit as st
import pandas as pd
import requests
import io
import zipfile
import re
import time
from pathlib import Path
from urllib.parse import urlparse


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="CommCare Audio Downloader",
    page_icon="🎧",
    layout="wide"
)


# ============================================================
# CUSTOM BUTTON STYLE
# ============================================================

st.markdown("""
<style>

div.stButton > button {
    background-color: #0066CC;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 18px;
    font-size: 14px;
    font-weight: 600;
    width: auto;
    min-width: 200px;
}

div.stButton > button:hover {
    background-color: #004C99;
    color: white;
    border: none;
}

div.stDownloadButton > button {
    background-color: #0066cc;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 14px;
    font-weight: 600;
    width: auto;
    min-width: 180px;
}

div.stDownloadButton > button:hover {
    background-color: #004c99;
    color: white;
}

</style>
""", unsafe_allow_html=True)


# ============================================================
# CONSTANTS
# ============================================================

AUDIO_NAME_COLUMN = "audio_name"
AUDIO_URL_COLUMN = "survey_audio_link"

DEFAULT_DOMAIN = "m-e-uganda"

CHUNK_SIZE = 1024 * 1024  # 1 MB

RETRY_STATUS_CODES = {
    408,
    429,
    500,
    502,
    503,
    504
}

DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 3


# ============================================================
# SESSION STATE
# ============================================================

if "zip_data" not in st.session_state:
    st.session_state.zip_data = None

if "failed_csv" not in st.session_state:
    st.session_state.failed_csv = None

if "download_results" not in st.session_state:
    st.session_state.download_results = None


# ============================================================
# PAGE TITLE
# ============================================================

st.title("🎧 CommCare Audio Downloader")

st.write(
    "Upload your CommCare Excel/CSV file, authenticate with "
    "a CommCare API key, download the audio files, rename them, "
    "and download the results as a ZIP file."
)


# ============================================================
# SIDEBAR - AUTHENTICATION
# ============================================================

st.sidebar.header("CommCare Authentication")

st.sidebar.info(
    "Enter your CommCare domain, username, and API key."
)

commcare_domain = st.sidebar.text_input(
    "CommCare Domain",
    value=DEFAULT_DOMAIN,
    help="Example: m-e-uganda"
)

commcare_username = st.sidebar.text_input(
    "CommCare Username",
    placeholder="Enter your CommCare username"
)

commcare_api_key = st.sidebar.text_input(
    "CommCare API Key",
    type="password",
    placeholder="Enter your CommCare API key"
)


# ============================================================
# SIDEBAR - DOWNLOAD SETTINGS
# ============================================================

st.sidebar.header("⚙️ Download Settings")

timeout = st.sidebar.number_input(
    "Timeout per request (seconds)",
    min_value=10,
    max_value=300,
    value=DEFAULT_TIMEOUT,
    step=10
)

max_retries = st.sidebar.number_input(
    "Maximum retries",
    min_value=0,
    max_value=10,
    value=DEFAULT_RETRIES,
    step=1
)


# ============================================================
# CLEAN FILENAME
# ============================================================

def clean_filename(filename):
    """
    Clean a filename so it is safe for
    Windows/Linux/macOS.
    """

    if pd.isna(filename):
        return "unknown_audio"

    filename = str(filename).strip()

    if not filename:
        return "unknown_audio"

    # Remove invalid Windows filename characters
    filename = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        filename
    )

    # Remove control characters
    filename = re.sub(
        r"[\x00-\x1f]",
        "_",
        filename
    )

    # Replace multiple spaces
    filename = re.sub(
        r"\s+",
        " ",
        filename
    )

    # Avoid "." and ".."
    if filename in {".", ".."}:
        filename = "unknown_audio"

    # Remove trailing spaces/dots
    filename = filename.rstrip(" .")

    if not filename:
        filename = "unknown_audio"

    return filename


# ============================================================
# GET FILE EXTENSION
# ============================================================

def get_extension(url):
    """
    Extract file extension from the URL.

    Falls back to .m4a if the URL has no extension.
    """

    try:

        parsed_url = urlparse(str(url))

        extension = Path(
            parsed_url.path
        ).suffix.lower()

        # Only accept reasonable extensions
        if extension and len(extension) <= 10:
            return extension

    except Exception:
        pass

    return ".m4a"


# ============================================================
# CREATE UNIQUE FILENAME
# ============================================================

def make_unique_filename(filename, used_names):
    """
    Ensure filenames inside the ZIP are unique.
    """

    if filename not in used_names:

        used_names.add(filename)

        return filename

    path = Path(filename)

    stem = path.stem
    extension = path.suffix

    counter = 1

    while True:

        new_filename = (
            f"{stem}_{counter}{extension}"
        )

        if new_filename not in used_names:

            used_names.add(new_filename)

            return new_filename

        counter += 1


# ============================================================
# CREATE COMMCARE SESSION
# ============================================================

def create_commcare_session(
    username,
    api_key
):
    """
    Create a requests session authenticated using
    CommCare API-key authentication.
    """

    session = requests.Session()

    session.headers.update({

        "Authorization":
            f"ApiKey {username}:{api_key}",

        "User-Agent":
            "CommCare-Audio-Downloader/1.0",

        "Accept":
            "*/*"
    })

    return session


# ============================================================
# AUTHENTICATED REQUEST WITH RETRIES
# ============================================================

def download_with_retries(
    session,
    url,
    timeout,
    max_retries,
    chunked=True
):
    """
    Download an authenticated URL with retry handling.

    Returns:
        requests.Response

    The caller is responsible for closing the response.
    """

    last_exception = None

    for attempt in range(max_retries + 1):

        try:

            response = session.get(
                url,
                timeout=timeout,
                stream=chunked
            )

            # ------------------------------------------------
            # Successful response
            # ------------------------------------------------

            if response.status_code == 200:

                return response


            # ------------------------------------------------
            # Authentication errors
            # ------------------------------------------------

            if response.status_code in {
                401,
                403
            }:

                return response


            # ------------------------------------------------
            # Retry transient errors
            # ------------------------------------------------

            if (
                response.status_code
                in RETRY_STATUS_CODES
                and attempt < max_retries
            ):

                response.close()

                wait_time = min(
                    2 ** attempt,
                    10
                )

                time.sleep(wait_time)

                continue


            # ------------------------------------------------
            # Non-retryable error
            # ------------------------------------------------

            return response


        except requests.RequestException as e:

            last_exception = e

            if attempt >= max_retries:
                raise

            wait_time = min(
                2 ** attempt,
                10
            )

            time.sleep(wait_time)


    if last_exception:

        raise last_exception

    raise RuntimeError(
        "Download failed without a response."
    )


# ============================================================
# STREAM RESPONSE INTO ZIP
# ============================================================

def stream_response_to_zip(
    response,
    zip_file,
    filename
):
    """
    Stream the HTTP response directly into the ZIP file.

    This avoids loading the entire audio file into memory.
    """

    with zip_file.open(
        filename,
        mode="w"
    ) as destination:

        for chunk in response.iter_content(
            chunk_size=CHUNK_SIZE
        ):

            if chunk:

                destination.write(chunk)


# ============================================================
# READ UPLOADED FILE
# ============================================================

def read_uploaded_file(uploaded_file):

    filename = uploaded_file.name.lower()

    if filename.endswith(".csv"):

        return pd.read_csv(
            uploaded_file
        )

    elif filename.endswith(
        (".xlsx", ".xls")
    ):

        return pd.read_excel(
            uploaded_file
        )

    else:

        raise ValueError(
            "Unsupported file format."
        )


# ============================================================
# FILE UPLOADER
# ============================================================

uploaded_file = st.file_uploader(
    "📁 Upload your CommCare Excel or CSV file",
    type=[
        "xlsx",
        "xls",
        "csv"
    ]
)


# ============================================================
# PROCESS UPLOADED FILE
# ============================================================

if uploaded_file is not None:

    # --------------------------------------------------------
    # READ FILE
    # --------------------------------------------------------

    try:

        df = read_uploaded_file(
            uploaded_file
        )

    except Exception as e:

        st.error(
            f"❌ Error reading the file: {e}"
        )

        st.stop()


    # --------------------------------------------------------
    # CLEAN COLUMN NAMES
    # --------------------------------------------------------

    df.columns = (
        df.columns
        .astype(str)
        .str.strip()
    )


    # --------------------------------------------------------
    # CHECK REQUIRED COLUMNS
    # --------------------------------------------------------

    missing_columns = []

    if AUDIO_NAME_COLUMN not in df.columns:

        missing_columns.append(
            AUDIO_NAME_COLUMN
        )

    if AUDIO_URL_COLUMN not in df.columns:

        missing_columns.append(
            AUDIO_URL_COLUMN
        )


    if missing_columns:

        st.error(
            "❌ Required columns are missing:"
        )

        for column in missing_columns:

            st.write(
                f"- `{column}`"
            )

        st.write(
            "### Columns found in your file"
        )

        st.write(
            list(df.columns)
        )

        st.stop()


    # --------------------------------------------------------
    # SELECT AUDIO COLUMNS
    # --------------------------------------------------------

    audio_df = df[
        [
            AUDIO_NAME_COLUMN,
            AUDIO_URL_COLUMN
        ]
    ].copy()


    # --------------------------------------------------------
    # CLEAN AUDIO URLS
    # --------------------------------------------------------

    audio_df[AUDIO_URL_COLUMN] = (
        audio_df[AUDIO_URL_COLUMN]
        .astype("string")
        .str.strip()
    )


    # --------------------------------------------------------
    # REMOVE EMPTY URLS
    # --------------------------------------------------------

    audio_df = audio_df[
        audio_df[AUDIO_URL_COLUMN].notna()
    ]

    audio_df = audio_df[
        audio_df[AUDIO_URL_COLUMN] != ""
    ]

    audio_df = audio_df[
        audio_df[AUDIO_URL_COLUMN].str.lower()
        != "nan"
    ]

    audio_df = audio_df.reset_index(
        drop=True
    )


    # ========================================================
    # SUMMARY
    # ========================================================

    st.subheader("File Summary")

    col1, col2, col3 = st.columns(3)

    with col1:

        st.metric(
            "Total Rows",
            len(df)
        )

    with col2:

        st.metric(
            "Audio URLs Found",
            len(audio_df)
        )

    with col3:

        st.metric(
            "Rows Without Audio URL",
            len(df) - len(audio_df)
        )


    # ========================================================
    # EMPTY AUDIO CHECK
    # ========================================================

    if audio_df.empty:

        st.warning(
            "⚠️ No audio URLs were found in "
            f"`{AUDIO_URL_COLUMN}`."
        )

        st.stop()


    # ========================================================
    # AUDIO PREVIEW
    # ========================================================

    st.subheader("Audio Preview")

    preview_df = audio_df.copy()

    preview_df.columns = [
        "Audio Name",
        "CommCare Audio URL"
    ]

    st.dataframe(
        preview_df,
        use_container_width=True,
        hide_index=True
    )


    # ========================================================
    # FILENAME PREVIEW
    # ========================================================

    st.subheader("Filename Preview")

    filename_preview = []

    preview_count = min(
        10,
        len(audio_df)
    )

    for _, row in audio_df.head(
        preview_count
    ).iterrows():

        audio_name = clean_filename(
            row[AUDIO_NAME_COLUMN]
        )

        extension = get_extension(
            row[AUDIO_URL_COLUMN]
        )

        final_filename = (
            audio_name +
            extension
        )

        filename_preview.append({

            "Audio Name":
                row[AUDIO_NAME_COLUMN],

            "Final Filename":
                final_filename
        })


    filename_preview_df = pd.DataFrame(
        filename_preview
    )

    st.dataframe(
        filename_preview_df,
        use_container_width=True,
        hide_index=True
    )


    # ========================================================
    # AUTHENTICATION INFORMATION
    # ========================================================

    st.subheader("Authentication")

    st.caption(
        "Your API key is used only for authenticated "
        "CommCare requests and is not written into the ZIP "
        "or failure report."
    )

    if not commcare_username:

        st.warning(
            "Enter your CommCare username in the sidebar."
        )

    if not commcare_api_key:

        st.warning(
            "Enter your CommCare API key in the sidebar."
        )


    # ========================================================
    # DOWNLOAD BUTTON
    # ========================================================

    start_download = st.button(
    "🎧 Download & Rename Audio",
    type="primary"
)

    # ========================================================
    # START DOWNLOAD
    # ========================================================

    if start_download:

        # ----------------------------------------------------
        # VALIDATE AUTHENTICATION
        # ----------------------------------------------------

        if not commcare_domain:

            st.error(
                "Please enter your CommCare domain."
            )

            st.stop()


        if not commcare_username:

            st.error(
                "Please enter your CommCare username."
            )

            st.stop()


        if not commcare_api_key:

            st.error(
                "Please enter your CommCare API key."
            )

            st.stop()


        # ----------------------------------------------------
        # CLEAR PREVIOUS RESULTS
        # ----------------------------------------------------

        st.session_state.zip_data = None

        st.session_state.failed_csv = None

        st.session_state.download_results = None


        # ----------------------------------------------------
        # CREATE AUTHENTICATED SESSION
        # ----------------------------------------------------

        session = create_commcare_session(
            commcare_username,
            commcare_api_key
        )


        # ====================================================
        # TEST AUTHENTICATION
        # ====================================================

        st.subheader(
            "Testing Authentication"
        )

        test_url = str(
            audio_df.iloc[0][
                AUDIO_URL_COLUMN
            ]
        ).strip()


        with st.spinner(
            "Testing CommCare API-key authentication..."
        ):

            try:

                test_response = download_with_retries(
                    session=session,
                    url=test_url,
                    timeout=timeout,
                    max_retries=max_retries,
                    chunked=True
                )


            except requests.RequestException as e:

                st.error(
                    "❌ Could not connect to CommCare."
                )

                st.code(
                    str(e)
                )

                st.stop()


        # ----------------------------------------------------
        # CHECK AUTH RESPONSE
        # ----------------------------------------------------

        if test_response.status_code == 401:

            test_response.close()

            st.error(
                "❌ Authentication failed."
            )

            st.info(
                "Check your CommCare username and API key."
            )

            st.stop()


        elif test_response.status_code == 403:

            test_response.close()

            st.error(
                "❌ Access denied."
            )

            st.info(
                "The API key is recognized, but the "
                "account may not have permission to access "
                "the audio attachment."
            )

            st.stop()


        elif test_response.status_code != 200:

            status_code = (
                test_response.status_code
            )

            try:

                error_text = (
                    test_response.text[:500]
                )

            except Exception:

                error_text = (
                    "Unable to read error response."
                )

            test_response.close()

            st.error(
                f"❌ CommCare returned HTTP "
                f"{status_code}."
            )

            st.code(
                error_text
            )

            st.stop()


        else:

            st.success(
                "✅ CommCare API-key authentication "
                "was successful."
            )


        test_response.close()


        # ====================================================
        # PREPARE ZIP
        # ====================================================

        zip_buffer = io.BytesIO()

        successful = 0
        failed = 0

        errors = []

        used_names = set()


        # ====================================================
        # PROGRESS UI
        # ====================================================

        st.subheader(
            "🎧 Downloading Audio"
        )

        progress_bar = st.progress(0)

        status = st.empty()


        # ====================================================
        # CREATE ZIP
        # ====================================================

        with zipfile.ZipFile(
            zip_buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED
        ) as zip_file:

            # ================================================
            # DOWNLOAD EACH AUDIO
            # ================================================

            total_files = len(audio_df)

            for index, row in audio_df.iterrows():

                audio_url = str(
                    row[AUDIO_URL_COLUMN]
                ).strip()


                # --------------------------------------------
                # CREATE FILENAME
                # --------------------------------------------

                audio_name = clean_filename(
                    row[AUDIO_NAME_COLUMN]
                )

                extension = get_extension(
                    audio_url
                )

                filename = (
                    audio_name +
                    extension
                )


                # --------------------------------------------
                # MAKE UNIQUE
                # --------------------------------------------

                filename = make_unique_filename(
                    filename,
                    used_names
                )


                # --------------------------------------------
                # STATUS
                # --------------------------------------------

                status.write(
                    f"Downloading **{index + 1} "
                    f"of {total_files}** — "
                    f"`{filename}`"
                )


                response = None


                try:

                    # ----------------------------------------
                    # AUTHENTICATED REQUEST
                    # ----------------------------------------

                    response = download_with_retries(
                        session=session,
                        url=audio_url,
                        timeout=timeout,
                        max_retries=max_retries,
                        chunked=True
                    )


                    # ----------------------------------------
                    # AUTHENTICATION FAILURE
                    # ----------------------------------------

                    if response.status_code == 401:

                        raise RuntimeError(
                            "HTTP 401 Unauthorized - "
                            "invalid or expired API key."
                        )


                    if response.status_code == 403:

                        raise RuntimeError(
                            "HTTP 403 Forbidden - "
                            "API key does not have "
                            "permission to access this file."
                        )


                    # ----------------------------------------
                    # OTHER HTTP ERRORS
                    # ----------------------------------------

                    if response.status_code != 200:

                        raise RuntimeError(
                            f"HTTP {response.status_code}"
                        )


                    # ----------------------------------------
                    # STREAM DIRECTLY INTO ZIP
                    # ----------------------------------------

                    stream_response_to_zip(
                        response=response,
                        zip_file=zip_file,
                        filename=filename
                    )


                    successful += 1


                except Exception as e:

                    failed += 1

                    errors.append({

                        "Row":
                            index + 1,

                        "Audio Name":
                            str(
                                row[AUDIO_NAME_COLUMN]
                            ),

                        "Audio URL":
                            audio_url,

                        "Filename":
                            filename,

                        "Error":
                            str(e)
                    })


                finally:

                    if response is not None:

                        response.close()


                # --------------------------------------------
                # UPDATE PROGRESS
                # --------------------------------------------

                progress = (
                    (index + 1)
                    /
                    total_files
                )

                progress_bar.progress(
                    progress
                )


        # ====================================================
        # CLEAN PROGRESS
        # ====================================================

        progress_bar.empty()

        status.empty()


        # ====================================================
        # PREPARE ZIP DATA
        # ====================================================

        zip_buffer.seek(0)

        zip_data = zip_buffer.getvalue()


        # ====================================================
        # PREPARE FAILED CSV
        # ====================================================

        failed_csv = None

        if errors:

            errors_df = pd.DataFrame(
                errors
            )

            failed_csv = errors_df.to_csv(
                index=False
            ).encode("utf-8")


        # ====================================================
        # SAVE RESULTS TO SESSION STATE
        # ====================================================

        st.session_state.zip_data = zip_data

        st.session_state.failed_csv = failed_csv

        st.session_state.download_results = {

            "successful":
                successful,

            "failed":
                failed,

            "total":
                len(audio_df)
        }


    # ========================================================
    # DISPLAY RESULTS
    # ========================================================

    if (
        st.session_state.download_results
        is not None
    ):

        results = (
            st.session_state.download_results
        )

        successful = results[
            "successful"
        ]

        failed = results[
            "failed"
        ]

        total = results[
            "total"
        ]


        # ====================================================
        # RESULTS
        # ====================================================

        st.subheader(
            "Download Results"
        )

        result1, result2, result3 = st.columns(3)

        with result1:

            st.metric(
                "Total Audio Files",
                total
            )

        with result2:

            st.metric(
                "Successfully Downloaded",
                successful
            )

        with result3:

            st.metric(
                "Failed",
                failed
            )


        # ====================================================
        # SUCCESS MESSAGE
        # ====================================================

        if successful == total:

            st.success(
                "All audio files were downloaded successfully!"
            )

        elif successful > 0:

            st.warning(
                f"⚠️ {failed} audio files failed to download."
            )

        else:

            st.error(
                "❌ No audio files were downloaded successfully."
            )


        # ====================================================
        # DOWNLOAD BUTTONS
        # ====================================================

        st.subheader(
            "📥 Download Results"
        )

        download_col1, download_col2 = st.columns(2)


        # ----------------------------------------------------
        # ZIP DOWNLOAD
        # ----------------------------------------------------

        with download_col1:

            if st.session_state.zip_data is not None:

                st.download_button(
                    label="⬇️ Download Audio ZIP",
                    data=st.session_state.zip_data,
                    file_name="commcare_audio_files.zip",
                    mime="application/zip"
                )


        # ----------------------------------------------------
        # FAILED CSV DOWNLOAD
        # ----------------------------------------------------

        with download_col2:

            if st.session_state.failed_csv is not None:

                st.download_button(
                    label="⬇️ Download Failed CSV",
                    data=st.session_state.failed_csv,
                    file_name="failed_audio_files.csv",
                    mime="text/csv"
                )


        # ====================================================
        # FAILED FILES TABLE
        # ====================================================

        if st.session_state.failed_csv is not None:

            st.subheader(
                "⚠️ Failed Audio Files"
            )

            failed_data = pd.read_csv(
                io.BytesIO(
                    st.session_state.failed_csv
                )
            )

            st.dataframe(
                failed_data,
                use_container_width=True,
                hide_index=True
            )