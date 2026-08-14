import argparse
import logging
import os
import re
import shutil
import string
import time
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build as build_drive_api
from googleapiclient.http import MediaFileUpload
from pypdf import PdfWriter
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
)
from rich.theme import Theme
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    NoSuchWindowException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC  # noqa: N812
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

URL = "http://dorchester4.enrichcloudsc.com/"
SEARCH_URL = "https://dorchester4.enrichcloudsc.com/Students/Search.aspx"
DEFAULT_URL = "https://dorchester4.enrichcloudsc.com/Students/Default.aspx"
SCHOOL = "OPEC"
BREADCRUMB_RESET_INTERVAL = 20
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
FAILED_LOG_PATH = BASE_DIR / "failed_students.log"

GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
GOOGLE_CLIENT_SECRETS_FILE = BASE_DIR / "credentials.json"
GOOGLE_TOKEN_FILE = BASE_DIR / "token.json"

SUCCESS_LEVEL = 25
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")


def _log_success(self, message, *args, **kwargs):
    if self.isEnabledFor(SUCCESS_LEVEL):
        self._log(SUCCESS_LEVEL, message, args, **kwargs)


logging.Logger.success = _log_success

logger = logging.getLogger("frontline_extractor")

RICH_THEME = Theme(
    {
        "logging.level.success": "bold green",
    }
)


def wait_ready(driver, timeout=20):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def build_driver():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    options = webdriver.ChromeOptions()
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": str(DOWNLOAD_DIR),
            "download.prompt_for_download": False,
            "safebrowsing.enabled": True,
            "plugins.always_open_pdf_externally": True,
        },
    )
    driver = webdriver.Chrome(options=options)
    driver.maximize_window()
    return driver


def build_drive_service():
    if not GOOGLE_CLIENT_SECRETS_FILE.exists():
        return None

    creds = None
    if GOOGLE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(GOOGLE_TOKEN_FILE), GOOGLE_DRIVE_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GOOGLE_CLIENT_SECRETS_FILE), GOOGLE_DRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with GOOGLE_TOKEN_FILE.open("w") as f:
            f.write(creds.to_json())

    return build_drive_api("drive", "v3", credentials=creds)


def upload_to_drive(service, folder_id, file_path):
    file_metadata = {"name": file_path.name}
    if folder_id:
        file_metadata["parents"] = [folder_id]
    media = MediaFileUpload(str(file_path), mimetype="application/pdf")
    service.files().create(body=file_metadata, media_body=media, fields="id").execute()


def reset_breadcrumbs(driver):
    logger.info("Resetting breadcrumbs")
    driver.get(DEFAULT_URL)
    wait_ready(driver)


def login(driver, username, password):
    logger.info("Logging in as %s", username)
    driver.get(URL)

    context_select = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, "contextListEnrich"))
    )
    Select(context_select).select_by_value(SCHOOL)

    driver.find_element(By.ID, "txtUsernameEnrich").send_keys(username)
    driver.find_element(By.ID, "txtPasswordEnrich").send_keys(password)
    driver.find_element(By.ID, "btnSignInEnrich").click()

    WebDriverWait(driver, 20).until(EC.url_contains("/Students/Default.aspx"))
    wait_ready(driver)
    logger.info("Login successful")


def click_and_wait_for_postback(driver, element, timeout=20):
    element.click()
    WebDriverWait(driver, timeout).until(EC.staleness_of(element))
    wait_ready(driver, timeout)


def search_last_name(driver, letter):
    logger.info("Searching last names starting with '%s'", letter)
    driver.get(SEARCH_URL)
    last_name_field = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, "ctl00_content_studentSearch_lastName"))
    )
    last_name_field.send_keys(letter)
    search_button = driver.find_element(By.ID, "ctl00_content_studentSearch_Button1")
    click_and_wait_for_postback(driver, search_button)


def collect_file_links(driver):
    links = driver.find_elements(By.CSS_SELECTOR, "a[href*='ViewFiles.aspx?s=']")
    return [link.get_attribute("href") for link in links]


PAGER_HREF_MARKER = "studentsGrid$studentGrid$ctl24"


def collect_all_file_links(driver, progress=None, tasks=()):
    all_links = []
    while True:
        page_links = collect_file_links(driver)
        all_links.extend(page_links)
        if progress is not None:
            for task in tasks:
                current_total = progress.tasks[task].total
                progress.update(task, total=current_total + len(page_links))

        pager_cells = driver.find_elements(
            By.XPATH,
            f'//td[@colspan="6"][.//a[contains(@href, "{PAGER_HREF_MARKER}")]]',
        )
        if not pager_cells:
            break

        pager_items = pager_cells[0].find_elements(By.XPATH, "./span | ./a")
        current_index = next(
            (i for i, el in enumerate(pager_items) if el.tag_name == "span"), None
        )
        if current_index is None or current_index + 1 >= len(pager_items):
            break

        click_and_wait_for_postback(driver, pager_items[current_index + 1])

    logger.info("Found %d file link(s)", len(all_links))
    return all_links


def show_all(driver, select_id):
    select_el = driver.find_element(By.ID, select_id)
    select = Select(select_el)
    if select.first_selected_option.get_attribute("value") == "all":
        return
    select.select_by_value("all")
    WebDriverWait(driver, 20).until(EC.staleness_of(select_el))
    wait_ready(driver)


def get_student_name(driver):
    name_el = driver.find_element(By.CSS_SELECTOR, "td.info h1")
    return name_el.text.strip()


def get_student_dob(driver):
    dob_el = driver.find_element(
        By.XPATH, "//p[@id='info-sub']//span[strong[contains(text(), 'DOB')]]"
    )
    text = dob_el.text.split(":", 1)[-1].strip()
    return re.sub(r"\(.*?\)", "", text).strip()


def sanitize_filename(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def last_name_first(name):
    parts = name.split()
    if len(parts) < 2:
        return name

    is_suffix = parts[-1].strip(".").lower() in NAME_SUFFIXES
    if is_suffix and len(parts) < 3:
        # not enough parts to tell what the actual last name is
        return name

    suffix = parts.pop() if is_suffix else None
    if len(parts) < 2:
        return name

    reordered = [parts[-1], *parts[:-1]]
    if suffix:
        reordered.append(suffix)
    return " ".join(reordered)


def record_failed_student(student_name, student_dob, reason):
    with FAILED_LOG_PATH.open("a") as f:
        f.write(f"{student_name} (DOB {student_dob}): {reason}\n")


def dedupe_path(path):
    if not path.exists():
        return path
    n = 2
    candidate = path.with_stem(f"{path.stem}_{n}")
    while candidate.exists():
        n += 1
        candidate = path.with_stem(f"{path.stem}_{n}")
    return candidate


DOWNLOAD_EXTENSIONS = (".pdf", ".zip")


def latest_download(existing_files):
    downloads = [
        p
        for p in DOWNLOAD_DIR.iterdir()
        if p.suffix.lower() in DOWNLOAD_EXTENSIONS and p.name not in existing_files
    ]
    if not downloads:
        return None
    return max(downloads, key=lambda p: p.stat().st_mtime).name


def wait_for_new_download(existing_files, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        # a .crdownload file means Chrome is still writing the download
        if any(p.suffix == ".crdownload" for p in DOWNLOAD_DIR.iterdir()):
            time.sleep(0.5)
            continue
        download = latest_download(existing_files)
        if download:
            return download
        time.sleep(0.5)
    return None


def extract_pdfs_from_zip(zip_path, extract_dir):
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".pdf"):
                zf.extract(name, extract_dir)
    return sorted(extract_dir.rglob("*.pdf"))


def merge_pdfs(pdf_paths, dest):
    writer = PdfWriter()
    for path in pdf_paths:
        writer.append(path)
    with dest.open("wb") as f:
        writer.write(f)
    writer.close()


def select_filter(dropdown, filter_value):
    select = Select(dropdown)
    try:
        select.select_by_value(filter_value)
        return
    except NoSuchElementException:
        pass
    select.select_by_visible_text(filter_value)


def generate_batch_pdf(driver, student_name, student_dob, filter_value, first=True):
    """Runs the batch-print dialog for the given filter and returns the
    downloaded PDF filename (relative to DOWNLOAD_DIR), or None on failure.

    If `first` is False, the dialog is assumed to already be open (left on the
    printing step from a previous call) and is navigated back to the filter
    step instead of being re-opened, since the overlay blocks re-clicking the
    outer "batch print" link while the dialog is still up.
    """
    logger.debug("[%s/%s] generate_batch_pdf starting (first=%s)", student_name, filter_value, first)

    if first:
        batch_print_link = driver.find_elements(By.ID, "ctl00_options_ctl00")
        if not batch_print_link:
            logger.warning("No batch print link found for %s", student_name)
            record_failed_student(student_name, student_dob, "no batch print link")
            return None
        batch_print_link[0].click()
        logger.debug("[%s/%s] clicked batch print link", student_name, filter_value)

        WebDriverWait(driver, 10).until(
            EC.frame_to_be_available_and_switch_to_it(
                (By.CSS_SELECTOR, "iframe.vc3-dialog-frameSelector")
            )
        )
    else:
        WebDriverWait(driver, 10).until(
            EC.frame_to_be_available_and_switch_to_it(
                (By.CSS_SELECTOR, "iframe.vc3-dialog-frameSelector")
            )
        )
        back_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//input[@type='submit' and contains(@value, 'Back')]")
            )
        )
        back_button.click()
        logger.debug("[%s/%s] clicked Back to filter step", student_name, filter_value)

    filter_dropdown = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located(
            (By.CSS_SELECTOR, "[id$='_searchStep_documentFilterDropdown']")
        )
    )
    select_filter(filter_dropdown, filter_value)
    WebDriverWait(driver, 20).until(EC.staleness_of(filter_dropdown))
    wait_ready(driver)
    logger.debug("[%s/%s] filter selected", student_name, filter_value)

    driver.find_element(By.CSS_SELECTOR, "input[onclick*='checkAll']").click()

    WebDriverWait(driver, 10).until(
        lambda d: any(
            cb.is_selected()
            for cb in d.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
        )
    )
    logger.debug("[%s/%s] all items checked", student_name, filter_value)

    print_buttons = driver.find_elements(
        By.CSS_SELECTOR, "[id$='_searchStep_printButton']"
    )
    download_buttons = driver.find_elements(
        By.CSS_SELECTOR, "[id$='_searchStep_batchDownload']"
    )

    original_windows = driver.window_handles
    existing_files = {p.name for p in DOWNLOAD_DIR.iterdir()}

    if print_buttons:
        logger.debug("[%s/%s] using print flow", student_name, filter_value)
        print_buttons[0].click()

        try:
            WebDriverWait(driver, 60).until(
                EC.any_of(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "[id$='_printingStep_manualPrintLink']")
                    ),
                    EC.presence_of_element_located((By.CSS_SELECTOR, "span.error")),
                )
            )
        except TimeoutException:
            logger.error(
                "Timed out waiting for the %s print link for %s", filter_value, student_name
            )
            input("Inspect the browser now, then press Enter to skip this student...")
            driver.switch_to.default_content()
            record_failed_student(
                student_name, student_dob, f"{filter_value} print link timeout"
            )
            return None

        error_spans = driver.find_elements(By.CSS_SELECTOR, "span.error")
        if error_spans:
            reason = error_spans[0].get_attribute("title") or error_spans[0].text
            reason = " ".join(reason.split())
            logger.warning(
                "Generation error for %s (%s): %s", student_name, filter_value, reason
            )
            driver.switch_to.default_content()
            record_failed_student(student_name, student_dob, reason)
            return None

        print_link = driver.find_element(
            By.CSS_SELECTOR, "[id$='_printingStep_manualPrintLink']"
        )
        print_link.click()
        logger.debug("[%s/%s] clicked manual print link", student_name, filter_value)
    elif download_buttons:
        logger.debug("[%s/%s] using download flow", student_name, filter_value)
        download_buttons[0].click()
    else:
        logger.warning(
            "No print or download button found for %s (%s)", student_name, filter_value
        )
        driver.switch_to.default_content()
        record_failed_student(
            student_name, student_dob, f"{filter_value} no print/download button"
        )
        return None

    new_download = wait_for_new_download(existing_files)

    driver.switch_to.default_content()
    for handle in driver.window_handles:
        if handle not in original_windows:
            try:
                driver.switch_to.window(handle)
                driver.close()
            except NoSuchWindowException:
                pass

    driver.switch_to.window(original_windows[0])
    driver.switch_to.default_content()

    if not new_download:
        logger.warning("No file appeared for %s (%s)", student_name, filter_value)
        record_failed_student(student_name, student_dob, f"{filter_value} no file appeared")
        return None

    logger.debug("[%s/%s] downloaded %s", student_name, filter_value, new_download)
    return new_download


def download_student_files(driver, file_url, drive_service=None, drive_folder_id=None):
    driver.get(file_url)
    wait_ready(driver)

    student_name = get_student_name(driver)
    student_dob = get_student_dob(driver)
    logger.info("Checking files for %s (DOB %s)", student_name, student_dob)

    has_documents = bool(
        driver.find_elements(
            By.XPATH, "//tr[@class='grid-header'][.//a[text()='Document']]"
        )
    )
    has_attachments = bool(
        driver.find_elements(
            By.XPATH, "//tr[@class='grid-header'][.//a[text()='Attachment']]"
        )
    )

    logger.debug(
        "[%s] has_documents=%s has_attachments=%s",
        student_name,
        has_documents,
        has_attachments,
    )

    if not (has_documents or has_attachments):
        logger.info("No documents or attachments for %s", student_name)
        return False

    if has_documents:
        show_all(driver, "ctl00_content_pagingDropDownDocument")
    if has_attachments:
        show_all(driver, "ctl00_content_pagingDropDownAttachment")

    # when a student has only attachments, the filter dropdown has no
    # "Everything" option at all - "Attachments" is the only valid choice
    initial_filter = "Everything" if has_documents else "Attachments"

    logger.info("Fetching files for %s", student_name)
    primary_file = generate_batch_pdf(
        driver, student_name, student_dob, initial_filter, first=True
    )
    if not primary_file:
        return False

    pdf_paths = []
    raw_pdf_downloads = []
    zip_artifacts = []

    def collect(filename):
        path = DOWNLOAD_DIR / filename
        if path.suffix.lower() == ".zip":
            extract_dir = path.with_suffix("")
            extracted = extract_pdfs_from_zip(path, extract_dir)
            logger.debug(
                "[%s] extracted %d PDF(s) from %s", student_name, len(extracted), path.name
            )
            pdf_paths.extend(extracted)
            zip_artifacts.append((path, extract_dir))
        else:
            pdf_paths.append(path)
            raw_pdf_downloads.append(path)

    collect(primary_file)

    # only worth a second "Attachments" pass when both documents and
    # attachments exist - if there are only attachments, the "Everything"
    # pass above already covers them
    if has_documents and has_attachments:
        logger.info("Fetching attachments for %s", student_name)
        attachments_file = generate_batch_pdf(
            driver, student_name, student_dob, "Attachments", first=False
        )
        if attachments_file:
            collect(attachments_file)

    if not pdf_paths:
        logger.warning("No PDF content found for %s", student_name)
        record_failed_student(student_name, student_dob, "no PDF content in download(s)")
        for zip_path, extract_dir in zip_artifacts:
            zip_path.unlink(missing_ok=True)
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
        return False

    filename = f"{sanitize_filename(last_name_first(student_name))}_{sanitize_filename(student_dob)}.pdf"
    dest = dedupe_path(DOWNLOAD_DIR / filename)

    if len(pdf_paths) > 1:
        logger.debug("[%s] merging %d PDF(s) into %s", student_name, len(pdf_paths), dest.name)
        merge_pdfs(pdf_paths, dest)
        for path in raw_pdf_downloads:
            path.unlink()
    else:
        pdf_paths[0].rename(dest)

    for zip_path, extract_dir in zip_artifacts:
        zip_path.unlink(missing_ok=True)
        if extract_dir.exists():
            shutil.rmtree(extract_dir)

    logger.success("Saved %s", dest.name)

    if drive_service is not None:
        try:
            upload_to_drive(drive_service, drive_folder_id, dest)
            logger.success("Uploaded %s to Google Drive", dest.name)
        except Exception:
            logger.exception("Failed to upload %s to Google Drive", dest.name)

    return True


def expand_letters(spec):
    letters = []
    for raw_token in spec.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if len(token) == 3 and token[1] == "-":
            start, end = token[0].lower(), token[2].lower()
            if start > end:
                start, end = end, start
            letters.extend(chr(c) for c in range(ord(start), ord(end) + 1))
        else:
            letters.extend(token.lower())
    return "".join(letters)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=None,
        help="Stop after downloading this many file sets (default: no limit)",
    )
    parser.add_argument(
        "--letters",
        default=string.ascii_lowercase,
        type=expand_letters,
        help="Letters to search last names by, e.g. 'c-z' or 'a,d-f,z' (default: a-z)",
    )
    parser.add_argument(
        "--max-downloads-per-letter",
        type=int,
        default=None,
        help="Stop downloading after this many successes per letter (default: no limit)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug-level logging (step-by-step batch print/download trace)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    console = Console(theme=RICH_THEME)
    handler = RichHandler(console=console, show_path=False, markup=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[handler],
    )

    logger.info(
        "Options: letters=%s max_downloads=%s max_downloads_per_letter=%s verbose=%s",
        args.letters,
        args.max_downloads,
        args.max_downloads_per_letter,
        args.verbose,
    )

    load_dotenv()
    username = os.environ["FRONTLINE_USERNAME"]
    password = os.environ["FRONTLINE_PASSWORD"]

    drive_service = build_drive_service()
    drive_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if drive_service is not None:
        logger.info("Google Drive upload enabled")

    driver = build_driver()
    login(driver, username, password)

    processed = set()
    downloads = 0
    progress_columns = (
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
    )
    with Progress(*progress_columns, console=console) as progress:
        overall_task = progress.add_task("All letters", total=0)
        letter_task = progress.add_task("Letter", total=0)

        for letter in args.letters:
            if args.max_downloads is not None and downloads >= args.max_downloads:
                break

            progress.reset(letter_task, total=0)
            progress.update(letter_task, description=f"Letter '{letter}'")

            search_last_name(driver, letter)
            file_links = collect_all_file_links(
                driver, progress, (letter_task, overall_task)
            )

            downloads_this_letter = 0
            for link in file_links:
                if args.max_downloads is not None and downloads >= args.max_downloads:
                    break
                if (
                    args.max_downloads_per_letter is not None
                    and downloads_this_letter >= args.max_downloads_per_letter
                ):
                    break
                if link in processed:
                    progress.advance(letter_task)
                    progress.advance(overall_task)
                    continue
                processed.add(link)
                if download_student_files(driver, link, drive_service, drive_folder_id):
                    downloads += 1
                    downloads_this_letter += 1
                progress.advance(letter_task)
                progress.advance(overall_task)

                if len(processed) % BREADCRUMB_RESET_INTERVAL == 0:
                    reset_breadcrumbs(driver)

    logger.info("Downloaded %d set(s)", downloads)
    input("Press Enter to close...")
    driver.quit()


if __name__ == "__main__":
    main()
