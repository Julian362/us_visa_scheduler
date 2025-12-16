import time
import json
import random
import os
import requests
import configparser
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait as Wait
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from embassy import *

config = configparser.ConfigParser()
config.read('config.ini')

# Personal Info:
# Account and current appointment info from https://ais.usvisa-info.com
USERNAME = config['PERSONAL_INFO']['USERNAME']
PASSWORD = config['PERSONAL_INFO']['PASSWORD']
# Find SCHEDULE_ID in re-schedule page link:
# https://ais.usvisa-info.com/en-am/niv/schedule/{SCHEDULE_ID}/appointment
SCHEDULE_ID = config['PERSONAL_INFO']['SCHEDULE_ID']
# Target Period:
PRIOD_START = config['PERSONAL_INFO']['PRIOD_START']
PRIOD_END = config['PERSONAL_INFO']['PRIOD_END']
# Cutoff date: before this, only notify; on/after this, attempt to reschedule
ASSIGN_CUTOFF = config['PERSONAL_INFO'].get('ASSIGN_CUTOFF', '').strip()
# CAS facility (optional)
CAS_FACILITY_ID = config['PERSONAL_INFO'].get('CAS_FACILITY_ID', '').strip()
# Embassy Section:
YOUR_EMBASSY = config['PERSONAL_INFO']['YOUR_EMBASSY'].strip()
try:
    EMBASSY = Embassies[YOUR_EMBASSY][0]
    FACILITY_ID = Embassies[YOUR_EMBASSY][1]
    REGEX_CONTINUE = Embassies[YOUR_EMBASSY][2]
except KeyError:
    available = ", ".join(sorted(Embassies.keys()))
    raise KeyError(f"Invalid YOUR_EMBASSY='{YOUR_EMBASSY}'. Available: {available}")

# Notification:
# Get email notifications via https://sendgrid.com/ (Optional)
SENDGRID_API_KEY = config['NOTIFICATION']['SENDGRID_API_KEY']
# Get push notifications via https://pushover.net/ (Optional)
PUSHOVER_TOKEN = config['NOTIFICATION']['PUSHOVER_TOKEN']
PUSHOVER_USER = config['NOTIFICATION']['PUSHOVER_USER']
# Get push notifications via PERSONAL WEBSITE http://yoursite.com (Optional)
PERSONAL_SITE_USER = config['NOTIFICATION']['PERSONAL_SITE_USER']
PERSONAL_SITE_PASS = config['NOTIFICATION']['PERSONAL_SITE_PASS']
PUSH_TARGET_EMAIL = config['NOTIFICATION']['PUSH_TARGET_EMAIL']
PERSONAL_PUSHER_URL = config['NOTIFICATION']['PERSONAL_PUSHER_URL']

# Time Section:
minute = 60
hour = 60 * minute
# Time between steps (interactions with forms)
STEP_TIME = 0.5
# Time between retries/checks for available dates (seconds)
RETRY_TIME_L_BOUND = config['TIME'].getfloat('RETRY_TIME_L_BOUND')
RETRY_TIME_U_BOUND = config['TIME'].getfloat('RETRY_TIME_U_BOUND')
# Cooling down after WORK_LIMIT_TIME hours of work (Avoiding Ban)
WORK_LIMIT_TIME = config['TIME'].getfloat('WORK_LIMIT_TIME')
WORK_COOLDOWN_TIME = config['TIME'].getfloat('WORK_COOLDOWN_TIME')
# Temporary Banned (empty list): wait COOLDOWN_TIME hours
BAN_COOLDOWN_TIME = config['TIME'].getfloat('BAN_COOLDOWN_TIME')

# CHROMEDRIVER
# Details for the script to control Chrome
LOCAL_USE = config['CHROMEDRIVER'].getboolean('LOCAL_USE')
# Optional: HUB_ADDRESS is mandatory only when LOCAL_USE = False
HUB_ADDRESS = config['CHROMEDRIVER']['HUB_ADDRESS']

# RUN MODE (optional)
HEADLESS = False
DRY_RUN = False
ONE_SHOT = False
UPDATE_CAS = False
CAS_OFFSET_DAYS = 3
if config.has_section('RUN'):
    HEADLESS = config['RUN'].getboolean('HEADLESS', fallback=False)
    DRY_RUN = config['RUN'].getboolean('DRY_RUN', fallback=False)
    ONE_SHOT = config['RUN'].getboolean('ONE_SHOT', fallback=False)
    UPDATE_CAS = config['RUN'].getboolean('UPDATE_CAS', fallback=False)
    CAS_OFFSET_DAYS = config['RUN'].getint('CAS_OFFSET_DAYS', fallback=3)

SIGN_IN_LINK = f"https://ais.usvisa-info.com/{EMBASSY}/niv/users/sign_in"
APPOINTMENT_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment"
DATE_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/days/{FACILITY_ID}.json?appointments[expedite]=false"
TIME_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/times/{FACILITY_ID}.json?date=%s&appointments[expedite]=false"
SIGN_OUT_LINK = f"https://ais.usvisa-info.com/{EMBASSY}/niv/users/sign_out"

# CAS endpoints (default to same facility if CAS_FACILITY_ID not provided)
CAS_FACILITY_ID = str(CAS_FACILITY_ID or FACILITY_ID)
CAS_DATE_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/days/{CAS_FACILITY_ID}.json?appointments[expedite]=false"
CAS_TIME_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/times/{CAS_FACILITY_ID}.json?date=%s&appointments[expedite]=false"

JS_SCRIPT = ("var req = new XMLHttpRequest();"
             f"req.open('GET', '%s', false);"
             "req.setRequestHeader('Accept', 'application/json, text/javascript, */*; q=0.01');"
             "req.setRequestHeader('X-Requested-With', 'XMLHttpRequest');"
             f"req.setRequestHeader('Cookie', '_yatri_session=%s');"
             "req.send(null);"
             "return req.responseText;")

def send_notification(title, msg):
    print(f"Sending notification!")
    if SENDGRID_API_KEY:
        message = Mail(from_email=USERNAME, to_emails=USERNAME, subject=msg, html_content=msg)
        try:
            sg = SendGridAPIClient(SENDGRID_API_KEY)
            response = sg.send(message)
            print(response.status_code)
            print(response.body)
            print(response.headers)
        except Exception as e:
            print(e.message)
    if PUSHOVER_TOKEN:
        url = "https://api.pushover.net/1/messages.json"
        data = {
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "message": msg
        }
        requests.post(url, data)
    if PERSONAL_SITE_USER:
        url = PERSONAL_PUSHER_URL
        data = {
            "title": "VISA - " + str(title),
            "user": PERSONAL_SITE_USER,
            "pass": PERSONAL_SITE_PASS,
            "email": PUSH_TARGET_EMAIL,
            "msg": msg,
        }
        requests.post(url, data)


def auto_action(label, find_by, el_type, action, value, sleep_time=0):
    print("\t"+ label +":", end="")
    # Find Element By
    match find_by.lower():
        case 'id':
            item = driver.find_element(By.ID, el_type)
        case 'name':
            item = driver.find_element(By.NAME, el_type)
        case 'class':
            item = driver.find_element(By.CLASS_NAME, el_type)
        case 'xpath':
            item = driver.find_element(By.XPATH, el_type)
        case _:
            return 0
    # Do Action:
    match action.lower():
        case 'send':
            item.send_keys(value)
        case 'click':
            item.click()
        case _:
            return 0
    print("\t\tCheck!")
    if sleep_time:
        time.sleep(sleep_time)


def get_cas_facility_info():
    # 1) Explicit config overrides everything
    cfg_id = (config['PERSONAL_INFO'].get('CAS_FACILITY_ID', '') or '').strip()
    if cfg_id:
        return cfg_id, 'config-override'
    # 2) Try to read from page select
    try:
        if APPOINTMENT_URL not in (driver.current_url or ''):
            driver.get(APPOINTMENT_URL)
            time.sleep(STEP_TIME)
        sel = driver.find_elements(By.ID, 'appointments_asc_appointment_facility_id')
        if sel:
            select_el = sel[0]
            options = select_el.find_elements(By.TAG_NAME, 'option')
            # Prefer selected non-empty option; else first non-empty
            for opt in options:
                if opt.get_attribute('selected') and (opt.get_attribute('value') or '').strip():
                    return opt.get_attribute('value').strip(), opt.text.strip()
            for opt in options:
                if (opt.get_attribute('value') or '').strip():
                    return opt.get_attribute('value').strip(), opt.text.strip()
    except Exception:
        pass
    # 3) Fallback to embassy facility id
    return str(FACILITY_ID), 'embassy-default'


def start_process():
    # Bypass and robust waits: ensure we are on sign_in and fields exist
    driver.get(SIGN_IN_LINK)
    time.sleep(STEP_TIME)
    try:
        Wait(driver, 60).until(lambda d: d.find_elements(By.ID, 'user_email') or d.find_elements(By.NAME, 'commit'))
    except Exception:
        driver.get(SIGN_IN_LINK)
        try:
            Wait(driver, 90).until(lambda d: d.find_elements(By.ID, 'user_email') and d.find_elements(By.ID, 'user_password'))
        except Exception:
            try:
                with open("page_debug.html", "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
            except Exception:
                pass
            try:
                driver.save_screenshot("screenshot.png")
            except Exception:
                pass
            raise
    # Try clicking bounce if present
    try:
        elems = driver.find_elements(By.XPATH, '//a[contains(@class, "down-arrow")]')
        if elems:
            elems[0].click(); time.sleep(STEP_TIME)
    except Exception:
        pass
    # Accept cookie banner variants if present
    try:
        for sel in [
            'button#onetrust-accept-btn-handler',
            'button[class*="accept"]',
            'button[aria-label*="Accept"]',
            'button[title*="Accept"]']:
            btns = driver.find_elements(By.CSS_SELECTOR, sel)
            if btns:
                btns[0].click(); time.sleep(STEP_TIME); break
    except Exception:
        pass
    # Reduce automation fingerprint
    try:
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    except Exception:
        pass
    auto_action("Email", "id", "user_email", "send", USERNAME, STEP_TIME)
    auto_action("Password", "id", "user_password", "send", PASSWORD, STEP_TIME)
    # Try privacy checkbox variants
    try:
        auto_action("Privacy", "class", "icheckbox", "click", "", STEP_TIME)
    except Exception:
        try:
            driver.find_element(By.CSS_SELECTOR, 'label[for="policy_confirmed"]').click(); time.sleep(STEP_TIME)
        except Exception:
            pass
    # Click submit via multiple selectors
    try:
        auto_action("Enter Panel", "name", "commit", "click", "", STEP_TIME)
    except Exception:
        try:
            driver.find_element(By.CSS_SELECTOR, 'button[type="submit"], input[type="submit"]').click(); time.sleep(STEP_TIME)
        except Exception:
            pass
    Wait(driver, 120).until(EC.presence_of_element_located((By.XPATH, "//a[contains(text(), '" + REGEX_CONTINUE + "')]")))
    print("\n\tlogin successful!\n")
    try:
        info_logger(LOG_FILE_NAME, "Login successful and session established.")
    except Exception:
        pass

def reschedule(date):
    # if cutoff is set and date is before cutoff, force notify-only
    if ASSIGN_CUTOFF:
        try:
            cutoff_dt = datetime.strptime(ASSIGN_CUTOFF, "%Y-%m-%d")
            date_dt = datetime.strptime(date, "%Y-%m-%d")
            if date_dt < cutoff_dt:
                local_dry = True
            else:
                local_dry = DRY_RUN
        except Exception:
            local_dry = DRY_RUN
    else:
        local_dry = DRY_RUN

    # Navigate to appointment page early so CAS facility can be detected
    driver.get(APPOINTMENT_URL)
    try:
        info_logger(LOG_FILE_NAME, f"Opened appointment page for target date {date}.")
    except Exception:
        pass
    if local_dry:
        selected_time = "(dry-run)"
        cas_date, cas_time = None, None
    else:
        selected_time = get_time(date)
        try:
            info_logger(LOG_FILE_NAME, f"Embassy time chosen: {date} {selected_time}.")
        except Exception:
            pass
        # CAS fetching trace
        try:
            info_logger(LOG_FILE_NAME, f"UPDATE_CAS={UPDATE_CAS}; starting CAS availability fetch...")
            print("Fetching CAS availability...")
        except Exception:
            pass
        cas_date, cas_time = (get_cas_date_and_time(date, selected_time) if UPDATE_CAS else (None, None))
        if not UPDATE_CAS:
            try:
                info_logger(LOG_FILE_NAME, "UPDATE_CAS=False; skipping CAS selection.")
            except Exception:
                pass
        try:
            if UPDATE_CAS:
                info_logger(LOG_FILE_NAME, f"CAS selection proposal: date={cas_date}, time={cas_time}.")
        except Exception:
            pass
    page = driver.page_source
    # Try to extract hidden inputs from page source
    def extract_input(name):
        try:
            import re
            # match name='...' or name="..." with value='...' or value="..."
            m = re.search(rf"name=\s*[\'\"]{name}[\'\"][^>]*value=\s*[\'\"]([^\'\"]+)[\'\"]", page)
            return m.group(1) if m else None
        except Exception:
            return None
    headers = {
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Referer": APPOINTMENT_URL,
        "Cookie": "_yatri_session=" + driver.get_cookie("_yatri_session")["value"]
    }
    # Notificar inmediatamente al encontrar cita, antes de intentar reasignar
    pre_msg = f"Date available: {date} {selected_time}."
    try:
        send_notification("FOUND", pre_msg)
    except Exception:
        pass
    if local_dry:
        title = "FOUND"
        msg = f"{pre_msg} DRY_RUN=True (no changes made)."
        return [title, msg]
    # Fill form via Selenium using provided selectors and submit
    try:
        # Set embassy appointment date via JS (handles readonly/datepicker)
        try:
            info_logger(LOG_FILE_NAME, "Setting embassy date field and loading times...")
        except Exception:
            pass
        cons_date_el = driver.find_element(By.ID, "appointments_consulate_appointment_date")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", cons_date_el)
        driver.execute_script("arguments[0].removeAttribute('readonly');", cons_date_el)
        driver.execute_script("arguments[0].value = arguments[1];", cons_date_el, date)
        # Press Enter to confirm date selection
        try:
            from selenium.webdriver.common.keys import Keys
            cons_date_el.send_keys(Keys.ENTER)
        except Exception:
            pass
        # Trigger change so times load
        driver.execute_script("var e=new Event('change', {bubbles:true}); arguments[0].dispatchEvent(e);", cons_date_el)
        # Wait until time select has options
        cons_time_el = driver.find_element(By.ID, "appointments_consulate_appointment_time")
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", cons_time_el)
        Wait(driver, 20).until(EC.element_to_be_clickable((By.ID, "appointments_consulate_appointment_time")))
        Wait(driver, 15).until(lambda d: len(cons_time_el.find_elements(By.TAG_NAME, 'option')) > 1)
        # Seleccionar siempre la primera hora disponible
        try:
            info_logger(LOG_FILE_NAME, "Selecting first available embassy time option.")
        except Exception:
            pass
        for opt in cons_time_el.find_elements(By.TAG_NAME, "option"):
            if (opt.get_attribute("value") or "").strip():
                opt.click(); break
        # Optionally set CAS fields
        if UPDATE_CAS and cas_date and cas_time:
            try:
                info_logger(LOG_FILE_NAME, "Setting CAS date field and loading times...")
            except Exception:
                pass
            asc_date_el = driver.find_element(By.ID, "appointments_asc_appointment_date")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", asc_date_el)
            driver.execute_script("arguments[0].removeAttribute('readonly');", asc_date_el)
            driver.execute_script("arguments[0].value = arguments[1];", asc_date_el, cas_date)
            try:
                from selenium.webdriver.common.keys import Keys
                asc_date_el.send_keys(Keys.ENTER)
            except Exception:
                pass
            driver.execute_script("var e=new Event('change', {bubbles:true}); arguments[0].dispatchEvent(e);", asc_date_el)
            asc_time_el = driver.find_element(By.ID, "appointments_asc_appointment_time")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", asc_time_el)
            Wait(driver, 20).until(EC.element_to_be_clickable((By.ID, "appointments_asc_appointment_time")))
            Wait(driver, 15).until(lambda d: len(asc_time_el.find_elements(By.TAG_NAME, 'option')) > 1)
            # Seleccionar siempre la primera hora disponible para CAS
            try:
                info_logger(LOG_FILE_NAME, "Selecting first available CAS time option.")
            except Exception:
                pass
            for opt in asc_time_el.find_elements(By.TAG_NAME, "option"):
                if (opt.get_attribute("value") or "").strip():
                    opt.click(); break
        # Submit reprogramar
        submit_el = driver.find_element(By.ID, "appointments_submit")
        try:
            info_logger(LOG_FILE_NAME, "Clicking Reprogramar button.")
        except Exception:
            pass
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_el)
        Wait(driver, 20).until(EC.element_to_be_clickable((By.ID, "appointments_submit")))
        clicked = False
        try:
            submit_el.click()
            clicked = True
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", submit_el)
                clicked = True
                try:
                    info_logger(LOG_FILE_NAME, "Submit clicked via JS fallback.")
                except Exception:
                    pass
            except Exception:
                pass

        # Confirm alert/modal (robust selectors + JS fallback)
        try:
            # Wait for any modal with a primary confirm action
            Wait(driver, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, 'div[class*="modal"], div[id*="fancybox"], div[role="dialog"]'))
            confirm_candidates = []
            confirm_candidates.extend(driver.find_elements(By.CSS_SELECTOR, 'a.btn.btn-primary, a.button.alert, a[onclick*="confirm"], a[data-method="post"]'))
            confirm_candidates.extend(driver.find_elements(By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'confirm') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'confirmar')]"))
            if confirm_candidates:
                confirm_el = confirm_candidates[-1]
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", confirm_el)
                try:
                    confirm_el.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", confirm_el)
                        try:
                            info_logger(LOG_FILE_NAME, "Confirm clicked via JS fallback.")
                        except Exception:
                            pass
                    except Exception:
                        pass
                try:
                    info_logger(LOG_FILE_NAME, "Clicked Confirmar in modal.")
                except Exception:
                    pass
        except Exception:
            # If no modal appeared, continue to success detection
            try:
                info_logger(LOG_FILE_NAME, "No confirmation modal detected; proceeding.")
            except Exception:
                pass

        # Wait and detect success by URL/banners/text
        success = False
        end_time = time.time() + 20
        last_url = driver.current_url
        while time.time() < end_time:
            try:
                if any(s in (driver.current_url or '') for s in ["/appointment/instructions", "/instructions"]):
                    success = True
                    break
                page_after_loop = driver.page_source
                if ("Successfully Scheduled" in page_after_loop) or ("Programado exitosamente" in page_after_loop):
                    success = True
                    break
            except Exception:
                pass
            time.sleep(1)

        page_after = driver.page_source
        if success:
            title = "SUCCESS"
            suffix = ""
            if UPDATE_CAS and cas_date and cas_time:
                suffix = f"; CAS set to {cas_date} {cas_time}"
            msg = f"Rescheduled Successfully! {date} {selected_time}{suffix}"
            try:
                info_logger(LOG_FILE_NAME, f"Success detected. URL: {driver.current_url}")
            except Exception:
                pass
        else:
            title = "FAIL"
            # Capture banner messages if present
            banners_txt = []
            try:
                banners = driver.find_elements(By.CSS_SELECTOR, ".alert, .flash, .notice, .error, .alert-success, .alert-danger")
                for b in banners:
                    t = (b.text or '').strip()
                    if t:
                        banners_txt.append(t)
            except Exception:
                pass
            snippet = page_after[:400].replace('\n', ' ')
            banner_blob = (" | Banners: " + " || ".join(banners_txt)) if banners_txt else ""
            msg = f"Reschedule Failed!!! {date} {selected_time}. URL: {driver.current_url}. Error snippet: {snippet}{banner_blob}"
            # Persist artifacts for diagnostics
            try:
                with open("page_debug.html", "w", encoding="utf-8") as f:
                    f.write(page_after)
            except Exception:
                pass
            try:
                driver.save_screenshot("screenshot.png")
            except Exception:
                pass
            try:
                info_logger(LOG_FILE_NAME, "Reschedule failed; page saved to page_debug.html and screenshot.png")
            except Exception:
                pass
    except Exception as e:
        title = "FAIL"
        msg = f"Reschedule Failed!!! {date} {selected_time}. Exception: {e}"
        # Save artifacts to aid debugging on exceptions
        try:
            with open("page_debug.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
        except Exception:
            pass
        try:
            driver.save_screenshot("screenshot.png")
        except Exception:
            pass
        try:
            info_logger(LOG_FILE_NAME, f"Exception during reschedule: {type(e).__name__}: {e}")
        except Exception:
            pass
    return [title, msg]


def get_date():
    # Requesting to get the whole available dates
    session = driver.get_cookie("_yatri_session")["value"]
    script = JS_SCRIPT % (str(DATE_URL), session)
    content = driver.execute_script(script)
    return json.loads(content)

def get_time(date):
    time_url = TIME_URL % date
    session = driver.get_cookie("_yatri_session")["value"]
    script = JS_SCRIPT % (str(time_url), session)
    content = driver.execute_script(script)
    data = json.loads(content)
    times = data.get("available_times") or []
    time = times[0] if times else None
    print(f"Got time successfully! {date} {time}")
    try:
        info_logger(LOG_FILE_NAME, f"Embassy available times response; chosen: {date} {time}")
    except Exception:
        pass
    return time

def get_cas_date_and_time(interview_date, interview_time=None):
    try:
        session = driver.get_cookie("_yatri_session")["value"]
        cas_id, cas_label = get_cas_facility_info()
        # Ensure we have the embassy time to inform CAS query (server expects consulate context)
        if not interview_time:
            try:
                data_time = json.loads(driver.execute_script(JS_SCRIPT % (str(TIME_URL % interview_date), session)))
                times_list = data_time.get("available_times") or []
                interview_time = times_list[0] if times_list else None
            except Exception:
                interview_time = None
        # Compose CAS days URL including consulate context
        cas_date_url = (
            f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/days/{cas_id}.json"
            f"?consulate_id={FACILITY_ID}"
            f"&consulate_date={interview_date}"
            f"&consulate_time={interview_time or ''}"
            f"&appointments[expedite]=false"
        )
        cas_time_url_tpl = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/times/{cas_id}.json?date=%s&appointments[expedite]=false"

        try:
            info_logger(LOG_FILE_NAME, f"CAS days URL: {cas_date_url}")
        except Exception:
            pass
        content = driver.execute_script(JS_SCRIPT % (str(cas_date_url), session))
        data = json.loads(content)
        available = [d.get('date') for d in data]
        # Debug: print CAS available dates with facility info
        try:
            cas_msg = f"CAS facility: {cas_id} ({cas_label})\nCAS Available dates ({len(available)}):\n" + ", ".join(available)
            print(cas_msg)
            info_logger(LOG_FILE_NAME, cas_msg)
        except Exception:
            pass
        if not available:
            return None, None
        # Política solicitada: usar la última fecha disponible del CAS
        chosen_str = sorted(available)[-1]
        cas_time_url = cas_time_url_tpl % chosen_str
        try:
            info_logger(LOG_FILE_NAME, f"CAS times URL: {cas_time_url}")
        except Exception:
            pass
        content2 = driver.execute_script(JS_SCRIPT % (str(cas_time_url), session))
        data2 = json.loads(content2)
        times = data2.get("available_times") or []
        # Debug: print CAS available times for chosen date
        try:
            times_msg = f"CAS Available times for {chosen_str}:\n" + ", ".join(times)
            print(times_msg)
            info_logger(LOG_FILE_NAME, times_msg)
        except Exception:
            pass
        if not times:
            return chosen_str, None
        cas_time = times[0]  # primera hora disponible del día elegido
        return chosen_str, cas_time
    except Exception:
        return None, None


def is_logged_in():
    content = driver.page_source
    if(content.find("error") != -1):
        return False
    return True


def get_available_date(dates):
    # Evaluation of different available dates (inclusive bounds)
    def is_in_period(date, PSD, PED):
        new_date = datetime.strptime(date, "%Y-%m-%d")
        return (PSD <= new_date <= PED)

    PED = datetime.strptime(PRIOD_END, "%Y-%m-%d")
    PSD = datetime.strptime(PRIOD_START, "%Y-%m-%d")
    in_range = []
    for d in dates:
        date = d.get('date')
        if date and is_in_period(date, PSD, PED):
            in_range.append(date)
    if in_range:
        return sorted(in_range)[0]  # primera fecha dentro del período
    # Fallback: tomar la primera fecha disponible de la lista completa si ninguna cae en el período
    try:
        all_dates = sorted([d.get('date') for d in dates if d.get('date')])
    except Exception:
        all_dates = []
    print(f"\n\nNo available dates between ({PSD.date()}) and ({PED.date()})! Fallback will use earliest available if permitted.")
    return all_dates[0] if all_dates else None


def info_logger(file_path, log):
    # file_path: e.g. "log.txt"
    with open(file_path, "a") as file:
        file.write(str(datetime.now().time()) + ":\n" + log + "\n")


chrome_options = webdriver.ChromeOptions()
if HEADLESS:
    chrome_options.add_argument("--headless=new")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--window-size=1920,1080")
chrome_options.add_argument("--lang=es-CO")
chrome_options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
chrome_options.add_experimental_option('useAutomationExtension', False)
if os.environ.get('CHROME_BIN'):
    chrome_options.binary_location = os.environ['CHROME_BIN']

if LOCAL_USE:
    # Use Selenium Manager (selenium >= 4.6) to auto-manage ChromeDriver
    driver = webdriver.Chrome(options=chrome_options)
else:
    driver = webdriver.Remote(command_executor=HUB_ADDRESS, options=chrome_options)


if __name__ == "__main__":
    first_loop = True
    while 1:
        LOG_FILE_NAME = "log_" + str(datetime.now().date()) + ".txt"
        if first_loop:
            t0 = time.time()
            total_time = 0
            Req_count = 0
            start_process()
            first_loop = False
        Req_count += 1
        try:
            msg = "-" * 60 + f"\nRequest count: {Req_count}, Log time: {datetime.today()}\n"
            print(msg)
            info_logger(LOG_FILE_NAME, msg)
            dates = get_date()
            if not dates:
                # Ban Situation
                msg = f"List is empty, Probabely banned!\n\tSleep for {BAN_COOLDOWN_TIME} hours!\n"
                print(msg)
                info_logger(LOG_FILE_NAME, msg)
                driver.get(SIGN_OUT_LINK)
                if ONE_SHOT:
                    END_MSG_TITLE = "BAN"
                    break
                time.sleep(BAN_COOLDOWN_TIME * hour)
                first_loop = True
            else:
                # Print Available dates:
                msg = ""
                for d in dates:
                    msg = msg + "%s" % (d.get('date')) + ", "
                msg = "Available dates:\n"+ msg
                print(msg)
                info_logger(LOG_FILE_NAME, msg)
                date = get_available_date(dates)
                if date:
                    # A good date to schedule for
                    END_MSG_TITLE, msg = reschedule(date)
                    print(msg)
                    info_logger(LOG_FILE_NAME, msg)
                    if ONE_SHOT:
                        break
                try:
                    RETRY_WAIT_TIME = random.randint(int(RETRY_TIME_L_BOUND), int(RETRY_TIME_U_BOUND))
                except Exception:
                    RETRY_WAIT_TIME = 60
                t1 = time.time()
                total_time = t1 - t0
                msg = "\nWorking Time:  ~ {:.2f} minutes".format(total_time/minute)
                print(msg)
                info_logger(LOG_FILE_NAME, msg)
                if ONE_SHOT:
                    END_MSG_TITLE = "DONE"
                    msg = "ONE_SHOT=True: Finished single iteration."
                    break
                if total_time > WORK_LIMIT_TIME * hour:
                    # Let program rest a little
                    driver.get(SIGN_OUT_LINK)
                    time.sleep(WORK_COOLDOWN_TIME * hour)
                    first_loop = True
                else:
                    msg = "Retry Wait Time: "+ str(RETRY_WAIT_TIME)+ " seconds"
                    print(msg)
                    info_logger(LOG_FILE_NAME, msg)
                    time.sleep(RETRY_WAIT_TIME)
        except Exception as e:
            # Exception occurred after finding dates or during reschedule
            END_MSG_TITLE = "EXCEPTION"
            msg = f"Break the loop after exception! {type(e).__name__}: {e}\n"
            # Try to include a small page snippet for context if possible
            try:
                snippet = driver.page_source[:200].replace('\n', ' ')
                msg += f"Snippet: {snippet}"
            except Exception:
                pass
            break

print(msg)
info_logger(LOG_FILE_NAME, msg)
# Notificar también en caso de EXCEPTION para visibilidad
if END_MSG_TITLE in ("FOUND", "SUCCESS", "FAIL", "EXCEPTION"):
    send_notification(END_MSG_TITLE, msg)
driver.get(SIGN_OUT_LINK)
driver.stop_client()
driver.quit()
