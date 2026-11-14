import os
import json
import time
from datetime import datetime
import requests
import feedparser
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types

# 1. Povezivanje sa Google Sheets
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)

sh = gc.open_by_key(os.environ["SPREADSHEET_ID"])
tab_glavni = sh.worksheet("Glavni izbor")
tab_rezervni = sh.worksheet("Rezervna lista")

# Uzimamo sve već upisane linkove (kolona J je 10. kolona) kako ne bismo duplirali oglase
existing_links = set(tab_glavni.col_values(10) + tab_rezervni.col_values(10))

# 2. Inicijalizacija Gemini klijenta
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

SYSTEM_PROMPT = f"""
Ti si AI asistent za zapošljavanje koji filtrira oglase za Anđelu Blagić.
Današnji datum je: {datetime.now().strftime('%Y-%m-%d')}.

PROFIL KANDIDATA:
- Iskustvo: 1+ god. prakse u Full-Stack razvoju (Java, Spring Boot, React, TypeScript) + 4 meseca QA Automation (Playwright, TypeScript) + SQL Server Data Warehousing i ETL projekti (SSIS, SSRS, PostgreSQL) + osnovni Python (pandas, scikit-learn).
- Obrazovanje: Master studije Inženjerstva informacionih sistema (FTN Novi Sad).
- Traženi senioritet: Junior, Entry-level, Graduate, Praksa, ili pozicije bez navedenog senioriteta koje traže 0-2 godine iskustva. Strogo ODBACI oglase koji u opisu traže 3+ godina iskustva, Medior ili Senior nivo.
- Lokacija i model rada:
  * Novi Sad (on-site, hibridno ili remote) -> PROLAZI.
  * Remote (Srbija, Evropa, EMEA, Worldwide) -> PROLAZI (odbaci US-only ili EU-citizenship only).
  * Beograd ili okolina -> PROLAZI SAMO AKO je hibridno ili remote po dogovoru. Strogi on-site u Beogradu ODBACI.
- Radno vreme: Full-time, part-time, praksa ili fleksibilno -> sve PROLAZI.

KATEGORIZACIJA:
1. "GLAVNI_IZBOR": Junior/Entry Full-Stack, Backend ili Frontend (Java, Spring Boot, React, Next.js, TypeScript, JS, C#, .NET, Node.js) ILI Data Engineer / SQL / BI / ETL Developer (SQL Server, PostgreSQL, SSIS, SSRS, DWH) ILI osnovni Junior Data Science/ML.
2. "REZERVNA_QA": Junior/Entry QA Automation / SDET / Test Automation (Playwright, Cypress, Selenium, TypeScript/JS).
3. "ODBACI": Sve ostalo.

Vrati ISKLJUČIVO validan JSON objekat:
{{
  "kategorija": "GLAVNI_IZBOR" | "REZERVNA_QA" | "ODBACI",
  "pozicija": "Naziv pozicije",
  "kompanija": "Naziv kompanije",
  "lokacija_model": "npr. Novi Sad / Hibridno ili Remote",
  "tehnologije": ["lista", "tehnologija"],
  "rok_prijave": "Datum isteka ili 'Nije naveden'",
  "razlog": "Kratko obrazloženje u 1 rečenici na srpskom zašto oglas odgovara"
}}
"""


def analyze_job(title, company, link, description, source):
    prompt = f"Sajt: {source}\nNaslov: {title}\nKompanija: {company}\nLink: {link}\nOpis oglasa:\n{description[:4000]}"
    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.1
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Greška pri AI analizi ({link}): {e}")
        return None


def fetch_rss_jobs():
    feeds = [
        ("WeWorkRemotely", "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss"),
        ("WeWorkRemotely", "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss"),
        ("Remotive", "https://remotive.com/remote-jobs/software-dev/feed"),
        ("Remotive Data", "https://remotive.com/remote-jobs/data/feed")
    ]
    jobs = []
    for source, url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                if entry.link not in existing_links:
                    soup = BeautifulSoup(entry.get("summary", ""), "html.parser")
                    jobs.append({
                        "source": source,
                        "title": entry.get("title", ""),
                        "company": entry.get("author", "Nepoznato"),
                        "link": entry.link,
                        "description": soup.get_text(separator=" ", strip=True)
                    })
        except Exception as e:
            print(f"Greška za RSS {source}: {e}")
    return jobs


def fetch_helloworld_jobs():
    urls = [
        "https://www.helloworld.rs/oglasi-za-posao/programiranje",
        "https://www.helloworld.rs/oglasi-za-posao/qa"
    ]
    jobs = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href*='/oglas-za-posao/']")[:15]:
                href = a.get("href")
                full_link = href if href.startswith("http") else f"https://www.helloworld.rs{href}"
                if full_link not in existing_links:
                    existing_links.add(full_link)
                    job_r = requests.get(full_link, headers=headers, timeout=15)
                    job_soup = BeautifulSoup(job_r.text, "html.parser")
                    title = a.get_text(strip=True) or "IT Pozicija"
                    desc = job_soup.get_text(separator=" ", strip=True)
                    jobs.append({
                        "source": "HelloWorld",
                        "title": title,
                        "company": "Pogledaj oglas",
                        "link": full_link,
                        "description": desc
                    })
                    time.sleep(1)
        except Exception as e:
            print(f"Greška za HelloWorld: {e}")
    return jobs


def fetch_linkedin_jobs():
    queries = [
        ("Junior Software Engineer", "Serbia"),
        ("Full Stack Developer", "Novi Sad, Vojvodina, Serbia"),
        ("Data Engineer", "Serbia"),
        ("QA Automation", "Serbia")
    ]
    jobs = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for keywords, location in queries:
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={requests.utils.quote(keywords)}&location={requests.utils.quote(location)}&f_TPR=r604800&start=0"
        try:
            r = requests.get(url, headers=headers, timeout=15)
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.find_all("li")[:10]:
                link_tag = card.find("a", class_="base-card__full-link")
                if not link_tag:
                    continue
                link = link_tag["href"].split("?")[0]
                if link in existing_links:
                    continue
                existing_links.add(link)
                title = card.find("h3", class_="base-search-card__title")
                company = card.find("h4", class_="base-search-card__subtitle")

                job_id = link.split("-")[-1]
                detail_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
                detail_r = requests.get(detail_url, headers=headers, timeout=15)
                detail_soup = BeautifulSoup(detail_r.text, "html.parser")
                desc_div = detail_soup.find("div", class_="show-more-less-html__markup")
                desc = desc_div.get_text(separator=" ", strip=True) if desc_div else ""

                jobs.append({
                    "source": "LinkedIn",
                    "title": title.get_text(strip=True) if title else keywords,
                    "company": company.get_text(strip=True) if company else "LinkedIn",
                    "link": link,
                    "description": desc
                })
                time.sleep(1.5)
        except Exception as e:
            print(f"Greška za LinkedIn ({keywords}): {e}")
    return jobs


def main():
    all_jobs = fetch_helloworld_jobs() + fetch_linkedin_jobs() + fetch_rss_jobs()
    print(f"Ukupno preuzeto novih oglasa za analizu: {len(all_jobs)}")
    today = datetime.now().strftime("%d.%m.%Y.")

    for job in all_jobs:
        if not job["description"]:
            continue
        result = analyze_job(job["title"], job["company"], job["link"], job["description"], job["source"])
        if not result:
            time.sleep(2)
            continue

        kat = result.get("kategorija")
        print(f"[{kat}] {job['source']} - {result.get('pozicija', job['title'])} ({result.get('razlog', '')})")

        if kat == "ODBACI":
            time.sleep(2)
            continue

        row = [
            "Novo",
            today,
            job["source"],
            result.get("pozicija", job["title"]),
            result.get("kompanija", job["company"]),
            result.get("lokacija_model", ""),
            ", ".join(result.get("tehnologije", [])),
            result.get("rok_prijave", "Nije naveden"),
            result.get("razlog", ""),
            job["link"]
        ]

        if kat == "GLAVNI_IZBOR":
            tab_glavni.append_row(row, value_input_option="USER_ENTERED")
        elif kat == "REZERVNA_QA":
            tab_rezervni.append_row(row, value_input_option="USER_ENTERED")

        time.sleep(4)


if __name__ == "__main__":
    main()
