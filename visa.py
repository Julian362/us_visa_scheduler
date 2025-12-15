import time
import json
import random
import os
import requests
import configparser
from datetime import datetime

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
if config.has_section('RUN'):
    HEADLESS = config['RUN'].getboolean('HEADLESS', fallback=False)
    DRY_RUN = config['RUN'].getboolean('DRY_RUN', fallback=False)
    ONE_SHOT = config['RUN'].getboolean('ONE_SHOT', fallback=False)

SIGN_IN_LINK = f"https://ais.usvisa-info.com/{EMBASSY}/niv/users/sign_in"
APPOINTMENT_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment"
DATE_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/days/{FACILITY_ID}.json?appointments[expedite]=false"
TIME_URL = f"https://ais.usvisa-info.com/{EMBASSY}/niv/schedule/{SCHEDULE_ID}/appointment/times/{FACILITY_ID}.json?date=%s&appointments[expedite]=false"
SIGN_OUT_LINK = f"https://ais.usvisa-info.com/{EMBASSY}/niv/users/sign_out"

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

    if local_dry:
        selected_time = "(dry-run)"
    else:
        selected_time = get_time(date)
    driver.get(APPOINTMENT_URL)
    headers = {
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Referer": APPOINTMENT_URL,
        "Cookie": "_yatri_session=" + driver.get_cookie("_yatri_session")["value"]
    }
    if local_dry:
        title = "FOUND"
        msg = f"Date available: {date} {selected_time}. DRY_RUN=True (no changes made)."
        return [title, msg]
    data = {
        "utf8": driver.find_element(by=By.NAME, value='utf8').get_attribute('value'),
        "authenticity_token": driver.find_element(by=By.NAME, value='authenticity_token').get_attribute('value'),
        "confirmed_limit_message": driver.find_element(by=By.NAME, value='confirmed_limit_message').get_attribute('value'),
        "use_consulate_appointment_capacity": driver.find_element(by=By.NAME, value='use_consulate_appointment_capacity').get_attribute('value'),
        "appointments[consulate_appointment][facility_id]": FACILITY_ID,
        "appointments[consulate_appointment][date]": date,
        "appointments[consulate_appointment][time]": selected_time,
    }
    r = requests.post(APPOINTMENT_URL, headers=headers, data=data)
    if(r.text.find('Successfully Scheduled') != -1):
        title = "SUCCESS"
        msg = f"Rescheduled Successfully! {date} {selected_time}"
    else:
        title = "FAIL"
        msg = f"Reschedule Failed!!! {date} {selected_time}"
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
    time = data.get("available_times")[-1]
    print(f"Got time successfully! {date} {time}")
    return time


def is_logged_in():
    content = driver.page_source
    if(content.find("error") != -1):
        return False
    return True


def get_available_date(dates):
    # Evaluation of different available dates
    def is_in_period(date, PSD, PED):
        new_date = datetime.strptime(date, "%Y-%m-%d")
        result = ( PED > new_date and new_date > PSD )
        # print(f'{new_date.date()} : {result}', end=", ")
        return result
    
    PED = datetime.strptime(PRIOD_END, "%Y-%m-%d")
    PSD = datetime.strptime(PRIOD_START, "%Y-%m-%d")
    for d in dates:
        date = d.get('date')
        if is_in_period(date, PSD, PED):
            return date
    print(f"\n\nNo available dates between ({PSD.date()}) and ({PED.date()})!")


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
                    if ONE_SHOT:
                        break
                RETRY_WAIT_TIME = random.randint(RETRY_TIME_L_BOUND, RETRY_TIME_U_BOUND)
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
        except:
            # Exception Occured
            msg = f"Break the loop after exception!\n"
            END_MSG_TITLE = "EXCEPTION"
            break

print(msg)
info_logger(LOG_FILE_NAME, msg)
# Solo notificar cuando se haya encontrado/intentado cita
if END_MSG_TITLE in ("FOUND", "SUCCESS", "FAIL"):
    send_notification(END_MSG_TITLE, msg)
driver.get(SIGN_OUT_LINK)
driver.stop_client()
driver.quit()
