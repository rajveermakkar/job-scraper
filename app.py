import os
import csv
import json
import time
import queue
import logging
import threading
import requests
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, Response, stream_with_context
from jobspy import scrape_jobs

# Load environment variables from .env
load_dotenv()

# --------------------------------------------------
# Configuration (Now Fully Using .env)
# --------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", 120))  # Default: 2 min

SEARCH_TERMS = os.getenv("SEARCH_TERMS", "").split(",")  # Required
JOB_TYPES = os.getenv("JOB_TYPES", "").split(",")  # Required
LOCATION = os.getenv("JOB_LOCATION", "Canada")  # Default: Canada
RESULTS_WANTED = int(os.getenv("RESULTS_WANTED", 20))  # Default: 20 jobs per query
HOURS_OLD = int(os.getenv("HOURS_OLD", 1))  # Default: Last 1 hour

PLATFORM = os.getenv("PLATFORM", "linkedin").lower()
COUNTRY_INDEED = os.getenv("COUNTRY_INDEED", "canada")

LOG_FILE = os.getenv("LOG_FILE", "logs.txt")
SENT_JOBS_FILE = os.getenv("SENT_JOBS_FILE", "sent_jobs.json")
CSV_FILE = os.getenv("CSV_FILE", "jobs.csv")

# Validate critical environment variables
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("⚠️ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env!")

if not SEARCH_TERMS or not JOB_TYPES:
    raise ValueError("⚠️ SEARCH_TERMS and JOB_TYPES must be set in .env!")

if PLATFORM == "indeed" and not COUNTRY_INDEED:
     raise ValueError("⚠️ COUNTRY_INDEED must be set when scraping Indeed!")

# Flask app setup
app = Flask(__name__)

# Queue for real-time logging
log_queue = queue.Queue()

# --------------------------------------------------
# Logging Configuration
# --------------------------------------------------

class SSELogHandler(logging.Handler):
    def emit(self, record):
        try:
            log_queue.put(self.format(record))
        except Exception:
            self.handleError(record)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger()
logger.addHandler(SSELogHandler())

def log_message(message):
    """Logs messages to console and log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} - {message}"
    print(log_entry)
    logger.info(message)

# --------------------------------------------------
# Telegram Notifications
# --------------------------------------------------

def send_to_telegram(message):
    """Send job notifications to Telegram with retry logic."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}

    for attempt in range(3):  # Retry up to 3 times
        try:
            response = requests.post(url, json=data)
            if response.status_code == 200:
                log_message(f"✅ Sent to Telegram: {message.splitlines()[0]}")
                time.sleep(1)  # Prevent rate limiting
                return True
            log_message(f"⚠️ Telegram Error (Attempt {attempt+1}): {response.text}")
        except Exception as e:
            log_message(f"❌ Telegram Request Failed (Attempt {attempt+1}): {e}")
        time.sleep(2)

    log_message("❌ Failed to send message after retries.")
    return False

# --------------------------------------------------
# Job Processing Helpers
# --------------------------------------------------

def extract_job_id(job_url):
    """Extracts job ID from LinkedIn job URL."""
    if "linkedin.com/jobs/view/" in job_url:
        return job_url.split("/jobs/view/")[1].split("/")[0]  # Extract numeric job ID
    return job_url.strip()  # Fallback

def extract_description_snippet(description, terms):
    """Extracts a relevant snippet from the description based on search terms."""
    if not description:
        return "No description available."
    
    description_lower = description.lower()
    best_idx = -1
    
    # split terms into individual keywords if needed, or use as phrases
    # for now, use the terms as provided (phrases)
    for term in terms:
        idx = description_lower.find(term.lower())
        if idx != -1:
            best_idx = idx
            break
    
    if best_idx != -1:
        # Extract 100 chars before and after
        start = max(0, best_idx - 50)
        end = min(len(description), best_idx + 150)
        snippet = description[start:end].replace("\n", " ").strip()
        return f"...{snippet}..."
    else:
        # Return first 200 chars if no term match
        return description[:200].replace("\n", " ").strip() + "..."

def load_sent_jobs():
    """Load previously sent job IDs from JSON file."""
    if os.path.exists(SENT_JOBS_FILE):
        with open(SENT_JOBS_FILE, "r") as file:
            return set(json.load(file))
    return set()

def save_sent_jobs(sent_jobs):
    """Save sent job IDs to JSON file after every batch."""
    with open(SENT_JOBS_FILE, "w") as file:
        json.dump(list(sent_jobs), file)

def save_jobs_to_csv(jobs):
    """Save jobs to CSV."""
    mode = 'a' if os.path.exists(CSV_FILE) else 'w'
    header = not os.path.exists(CSV_FILE)

    jobs.to_csv(CSV_FILE, mode=mode, header=header, quoting=csv.QUOTE_NONNUMERIC, index=False)
    log_message(f"📁 Jobs saved to CSV ({len(jobs)} new entries).")

# --------------------------------------------------
# Job Scraper
# --------------------------------------------------

def scrape_and_notify():
    """Main job scraping and notification loop."""
    log_message("🚀 Scraper started!")
    sent_jobs = load_sent_jobs()
    log_message(f"📂 Loaded {len(sent_jobs)} previously sent jobs.")

    while True:
        log_message("🔍 Scraping jobs...")
        all_jobs = []
        job_ids_found = set()  # Track job IDs within the same scrape

        # Determine platforms to scrape
        site_list = ["linkedin", "indeed"] if PLATFORM == "both" else [PLATFORM]

        for term in SEARCH_TERMS:
            for job_type in JOB_TYPES:
                log_message(f"🔎 Searching for: {term} ({job_type}) in {LOCATION} via {site_list}...")

                jobs = scrape_jobs(
                    site_name=site_list,
                    search_term=f"{term} {job_type}",
                    location=LOCATION,
                    results_wanted=RESULTS_WANTED,
                    hours_old=HOURS_OLD,
                    country_indeed=COUNTRY_INDEED
                )

                if not jobs.empty:
                    jobs["category"] = job_type  # Add job type column
                    all_jobs.append(jobs)

        if all_jobs:
            combined_jobs = pd.concat(all_jobs, ignore_index=True)

            # **Remove duplicates within the current scrape**
            new_jobs = []
            for _, job in combined_jobs.iterrows():
                job_id = extract_job_id(job["job_url"])

                # Check if job was already scraped or sent before
                if job_id not in job_ids_found and job_id not in sent_jobs:
                    job_ids_found.add(job_id)  # Mark as seen in this batch
                    new_jobs.append(job)

            log_message(f"📤 Found {len(new_jobs)} new jobs.")

            if new_jobs:
                df_new_jobs = pd.DataFrame(new_jobs)
                save_jobs_to_csv(df_new_jobs)

                # 🔹 **Batch Send to Telegram**
                messages = []
                for _, job in df_new_jobs.iterrows():
                    job_id = extract_job_id(job["job_url"])
                    sent_jobs.add(job_id)  # Add to sent jobs immediately

                    snippet = extract_description_snippet(job.get('description', ''), SEARCH_TERMS)
                    
                    message = (
                        f"*{job['title']}*\n{job['company']}\n📍 {job['location']}\n"
                        f"🌐 Platform: {job.get('site', 'Unknown')}\n"
                        f"📝 {snippet}\n"
                        f"🗂 Category: {job['category']}\n🔗 [Apply here]({job['job_url']})"
                    )
                    messages.append(message)

                # **Avoid Telegram Rate Limits: Send in Batches**
                for i in range(0, len(messages), 5):  # Send 5 at a time
                    batch = messages[i:i+5]
                    for msg in batch:
                        send_to_telegram(msg)
                        time.sleep(1.5)  # Delay between messages
                    time.sleep(5)  # Extra delay after each batch

                save_sent_jobs(sent_jobs)  # Save updated sent jobs list

        log_message("⏳ Sleeping before next scrape...")
        time.sleep(SCRAPE_INTERVAL)


# --------------------------------------------------
# Flask Routes
# --------------------------------------------------

@app.route("/")
def home():
    return "🚀 LinkedIn Scraper is Running!"


@app.route("/logs")
def logs_page():
    """Real-time logs page."""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
      <title>Live Logs</title>
      <style>
        body { font-family: Arial, sans-serif; background: #f4f4f4; margin: 0; padding: 0; }
        #log-container { background: #000; color: #0f0; padding: 10px; height: 90vh; overflow-y: scroll; }
        h1 { text-align: center; }
      </style>
    </head>
    <body>
      <h1>Live Logs</h1>
      <pre id="log-container"></pre>
      <script>
        var evtSource = new EventSource("/logs/stream");
        evtSource.onmessage = function(e) {
          var container = document.getElementById("log-container");
          container.innerText += e.data + "\\n";
          container.scrollTop = container.scrollHeight;
        };
      </script>
    </body>
    </html>
    '''


@app.route("/logs/stream")
def stream_logs():
    """Stream logs via SSE."""
    def generate():
        while True:
            try:
                yield f"data: {log_queue.get(timeout=1)}\n\n"
            except queue.Empty:
                yield ": heartbeat\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# --------------------------------------------------
# Run App
# --------------------------------------------------

if __name__ == "__main__":
    threading.Thread(target=scrape_and_notify, daemon=True).start()
    app.run(host="0.0.0.0", port=2300)