import argparse
import logging
import os
import re
import string
import time

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build as build_drive_api
from googleapiclient.http import MediaFileUpload
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
from selenium.common.exceptions import NoSuchWindowException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait

URL = "http://dorchester4.enrichcloudsc.com/"
SEARCH_URL = "https://dorchester4.enrichcloudsc.com/Students/Search.aspx"
DEFAULT_URL = "https://dorchester4.enrichcloudsc.com/Students/Default.aspx"
SCHOOL = "OPEC"
BREADCRUMB_RESET_INTERVAL = 20
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
FAILED_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "failed_students.log"
)

GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
GOOGLE_CLIENT_SECRETS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "credentials.json"
)
GOOGLE_TOKEN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "token.json"
)

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
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    options = webdriver.ChromeOptions()
    options.add_experimental_option(
        "prefs",
        {
            "download.default_directory": DOWNLOAD_DIR,
            "download.prompt_for_download": False,
            "safebrowsing.enabled": True,
            "plugins.always_open_pdf_externally": True,
        },
    )
    driver = webdriver.Chrome(options=options)
    driver.maximize_window()
    return driver


def build_drive_service():
    if not os.path.exists(GOOGLE_CLIENT_SECRETS_FILE):
        return None

    creds = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            GOOGLE_TOKEN_FILE, GOOGLE_DRIVE_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CLIENT_SECRETS_FILE, GOOGLE_DRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GOOGLE_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build_drive_api("drive", "v3", credentials=creds)


def upload_to_drive(service, folder_id, file_path):
    file_metadata = {"name": os.path.basename(file_path)}
    if folder_id:
        file_metadata["parents"] = [folder_id]
    media = MediaFileUpload(file_path, mimetype="application/pdf")
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


def record_failed_student(student_name, student_dob, reason):
    with open(FAILED_LOG_PATH, "a") as f:
        f.write(f"{student_name} (DOB {student_dob}): {reason}\n")


def dedupe_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"


def latest_pdf(existing_files):
    pdfs = [
        f
        for f in os.listdir(DOWNLOAD_DIR)
        if f.lower().endswith(".pdf") and f not in existing_files
    ]
    if not pdfs:
        return None
    return max(pdfs, key=lambda f: os.path.getmtime(os.path.join(DOWNLOAD_DIR, f)))


def wait_for_new_pdf(existing_files, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        # a .crdownload file means Chrome is still writing the download
        if any(f.endswith(".crdownload") for f in os.listdir(DOWNLOAD_DIR)):
            time.sleep(0.5)
            continue
        pdf = latest_pdf(existing_files)
        if pdf:
            return pdf
        time.sleep(0.5)
    return None


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

    if not (has_documents or has_attachments):
        logger.info("No documents or attachments for %s", student_name)
        return False

    if has_documents:
        show_all(driver, "ctl00_content_pagingDropDownDocument")
    if has_attachments:
        show_all(driver, "ctl00_content_pagingDropDownAttachment")

    batch_print_link = driver.find_elements(By.ID, "ctl00_options_ctl00")
    if not batch_print_link:
        logger.warning("No batch print link found for %s", student_name)
        return False
    batch_print_link[0].click()

    WebDriverWait(driver, 10).until(
        EC.frame_to_be_available_and_switch_to_it(
            (By.CSS_SELECTOR, "iframe.vc3-dialog-frameSelector")
        )
    )

    filter_dropdown = WebDriverWait(driver, 10).until(
        EC.visibility_of_element_located(
            (By.ID, "ctl00_content_ctl01_searchStep_documentFilterDropdown")
        )
    )
    Select(filter_dropdown).select_by_value("Everything")
    WebDriverWait(driver, 20).until(EC.staleness_of(filter_dropdown))
    wait_ready(driver)

    driver.find_element(By.CSS_SELECTOR, "input[onclick*='checkAll']").click()

    WebDriverWait(driver, 10).until(
        lambda d: any(
            cb.is_selected()
            for cb in d.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
        )
    )

    driver.find_element(By.ID, "ctl00_content_ctl01_searchStep_printButton").click()

    try:
        WebDriverWait(driver, 60).until(
            EC.any_of(
                EC.presence_of_element_located(
                    (By.ID, "ctl00_content_ctl01_printingStep_manualPrintLink")
                ),
                EC.presence_of_element_located((By.CSS_SELECTOR, "span.error")),
            )
        )
    except TimeoutException:
        logger.error("Timed out waiting for the print link for %s", student_name)
        input("Inspect the browser now, then press Enter to skip this student...")
        driver.switch_to.default_content()
        record_failed_student(student_name, student_dob, "print link timeout")
        return False

    error_spans = driver.find_elements(By.CSS_SELECTOR, "span.error")
    if error_spans:
        reason = error_spans[0].get_attribute("title") or error_spans[0].text
        reason = " ".join(reason.split())
        logger.warning("Generation error for %s: %s", student_name, reason)
        driver.switch_to.default_content()
        record_failed_student(student_name, student_dob, reason)
        return False

    print_link = driver.find_element(
        By.ID, "ctl00_content_ctl01_printingStep_manualPrintLink"
    )

    original_windows = driver.window_handles
    existing_files = set(os.listdir(DOWNLOAD_DIR))
    print_link.click()

    new_pdf = wait_for_new_pdf(existing_files)

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

    if not new_pdf:
        logger.warning("No PDF appeared for %s", student_name)
        return False

    filename = f"{sanitize_filename(student_name)}_{sanitize_filename(student_dob)}.pdf"
    dest = dedupe_path(os.path.join(DOWNLOAD_DIR, filename))
    os.rename(os.path.join(DOWNLOAD_DIR, new_pdf), dest)
    logger.success("Saved %s", os.path.basename(dest))

    if drive_service is not None:
        try:
            upload_to_drive(drive_service, drive_folder_id, dest)
            logger.success("Uploaded %s to Google Drive", os.path.basename(dest))
        except Exception:
            logger.exception("Failed to upload %s to Google Drive", os.path.basename(dest))

    return True


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
        help="Letters to search last names by (default: a-z)",
    )
    parser.add_argument(
        "--max-downloads-per-letter",
        type=int,
        default=None,
        help="Stop downloading after this many successes per letter (default: no limit)",
    )
    return parser.parse_args()


def main():
    console = Console(theme=RICH_THEME)
    handler = RichHandler(console=console, show_path=False, markup=True)
    logging.basicConfig(
        level=logging.INFO, format="%(message)s", datefmt="[%X]", handlers=[handler]
    )

    args = parse_args()

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
