from jobspy import scrape_jobs
import pandas as pd

try:
    jobs = scrape_jobs(
        site_name=["indeed"],
        search_term="software engineer",
        location="Canada",
        results_wanted=1,
        country_indeed='canada',
        hours_old=72
    )
    if not jobs.empty:
        desc = jobs.iloc[0]['description']
        print(f"Description length: {len(desc)}")
        print(f"Description sample: {desc[:500]}")
    else:
        print("No jobs found")
except Exception as e:
    print(f"Error: {e}")
