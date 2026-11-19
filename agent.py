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

print("1. Povezivanje sa Google Sheets...")
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

existing_links = set(tab_glavni.col_values(10) + tab_rezervni.col_values(10))
print(f"Uspešno povezano sa tabelom. Već upisano linkova: {len(existing_links)}")

print("2. Inicijalizacija Gemini klijenta...")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

ACTIVE_MODEL = "gemini-3.5-flash-lite"
for candidate in ["gemini-3.5-flash-lite", "gemini-3.8-flash"]:
    try:
        client.models.generate_content(model=candidate, contents="Test")
        ACTIVE_MODEL = candidate
        print(f"Aktivan Gemini model: {ACTIVE_MODEL}")
        break
    except Exception as e:
        print(f"Model {candidate} nije dostupan ({e}), probam sledeći...")

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sr-RS,sr;q=0.9,en-US;q=0.8,en;q=0.7"
}

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
1. "GLAVNI_IZBOR": Junior/Entry Full-Stack, Backend ili Frontend (Java, Spring Boot, React, Next.js, TypeScript, JS, C#, .NET, Node.js) ILI Data Engineer / BI Developer / SQL Developer / ETL (SQL Server, PostgreSQL, SSIS, SSRS, PowerBI, DWH) ILI osnovni Junior Data Science/ML.
2. "REZERVNA_QA": Junior/Entry QA Automation / SDET / Test Automation / Junior QA Engineer (Playwright, Cypress, Selenium, TypeScript/JS ili automatsko/manuelno testiranje za juniore).
3. "ODBACI": Sve ostalo.

Vrati ISKLJUČIVO validan JSON objekat sa dvostrukim navodnicima:
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
    prompt = f"Sajt: {source}\nNaslov: {title}\nKompanija: {company}\nLink: {link}\nOpis oglasa:\n{description[:3800]}"
    models_to_try = [ACTIVE_MODEL, "gemini-3.8-flash"] if ACTIVE_MODEL == "gemini-3.5-flash-lite" else [ACTIVE_MODEL, "gemini-3.5-flash-lite"]

    for attempt in range(2):
        for model_name in models_to_try:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        temperature=0.1
                    )
                )
                return json.loads(response.text)
            except json.JSONDecodeError:
                print(f"   Neispravan JSON odgovor modela za ({link}), preskačem...")
                return None
            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg:
                    print("   Dostignut besplatni limit (15 zahteva/min), pauziram 35s pa nastavljam...")
                    time.sleep(35)
                    break
                if "503" in err_msg:
                    print(f"   Model {model_name} trenutno zauzet (503), probam rezervni...")
                    time.sleep(2)
                    continue
                print(f"   Greška pri AI analizi ({link}): {e}")
                return None
    return None


def fetch_rss_jobs():
    print("3. Preuzimam RSS remote oglase (WeWorkRemotely, Remotive, WorkingNomads, Himalayas)...")
    feeds = [
        ("WeWorkRemotely FullStack", "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss"),
        ("WeWorkRemotely Backend", "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss"),
        ("Remotive Dev", "https://remotive.com/remote-jobs/software-dev/feed"),
        ("Remotive Data", "https://remotive.com/remote-jobs/data/feed"),
        ("WorkingNomads", "https://www.workingnomads.com/jobs?category=development&format=rss"),
        ("Himalayas", "https://himalayas.app/jobs/rss")
    ]
    jobs = []
    for source, url in feeds:
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=(5, 10))
            feed = feedparser.parse(r.text)
            for entry in feed.entries[:6]:
                if entry.link not in existing_links:
                    existing_links.add(entry.link)
                    soup = BeautifulSoup(entry.get("summary", ""), "html.parser")
                    jobs.append({
                        "source": source,
                        "title": entry.get("title", ""),
                        "company": entry.get("author", "Pogledaj oglas"),
                        "link": entry.link,
                        "description": soup.get_text(separator=" ", strip=True)
                    })
        except Exception as e:
            print(f"   Preskačem RSS {source}: {e}")
    print(f"   -> Pronađeno {len(jobs)} novih RSS oglasa.")
    return jobs


def fetch_infostud_jobs():
    print("4. Preuzimam oglase sa Poslovi Infostud...")
    jobs = []
    infostud_urls = [
        "https://poslovi.infostud.com/oglasi-za-posao/it?category%5B0%5D=5",
        "https://poslovi.infostud.com/oglasi-za-posao-novi-sad/it?category%5B0%5D=5"
    ]
    for url in infostud_urls:
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=(5, 10))
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href*='/posao/']")[:15]:
                href = a.get("href")
                full_link = href if href.startswith("http") else f"https://poslovi.infostud.com{href}"
                full_link = full_link.split("?")[0]
                if full_link not in existing_links:
                    existing_links.add(full_link)
                    try:
                        jr = requests.get(full_link, headers=BROWSER_HEADERS, timeout=(5, 10))
                        jsoup = BeautifulSoup(jr.text, "html.parser")
                        h1 = jsoup.find("h1")
                        title = h1.get_text(strip=True) if h1 else (a.get_text(strip=True) or "IT Pozicija")
                        desc = jsoup.get_text(separator=" ", strip=True)
                        if len(desc) > 200:
                            jobs.append({
                                "source": "Infostud",
                                "title": title,
                                "company": "Pogledaj oglas",
                                "link": full_link,
                                "description": desc
                            })
                        time.sleep(0.5)
                    except Exception:
                        continue
        except Exception as e:
            print(f"   Infostud preskočen: {e}")

    print(f"   -> Pronađeno {len(jobs)} novih Infostud oglasa.")
    return jobs


def fetch_linkedin_jobs():
    print("5. Preuzimam LinkedIn oglase (šire ključne reči, poslednjih 7 dana)...")
    queries = [
        ("Junior Software Engineer", "Serbia"),
        ("Software Developer", "Serbia"),
        ("Full Stack Developer", "Serbia"),
        ("Frontend Developer", "Serbia"),
        ("Backend Developer", "Serbia"),
        ("Java Developer", "Serbia"),
        ("React Developer", "Serbia"),
        ("BI Developer", "Serbia"),
        ("SQL Developer", "Serbia"),
        ("Data Engineer", "Serbia"),
        ("Junior QA", "Serbia"),
        ("QA Automation", "Serbia"),
        ("Software Engineer Intern", "Serbia")
    ]
    jobs = []
    for keywords, location in queries:
        url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={requests.utils.quote(keywords)}&location={requests.utils.quote(location)}&f_TPR=r604800&start=0"
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=(5, 10))
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
                try:
                    detail_r = requests.get(detail_url, headers=BROWSER_HEADERS, timeout=(5, 10))
                    detail_soup = BeautifulSoup(detail_r.text, "html.parser")
                    desc_div = detail_soup.find("div", class_="show-more-less-html__markup")
                    desc = desc_div.get_text(separator=" ", strip=True) if desc_div else ""
                    if desc:
                        jobs.append({
                            "source": "LinkedIn",
                            "title": title.get_text(strip=True) if title else keywords,
                            "company": company.get_text(strip=True) if company else "LinkedIn",
                            "link": link,
                            "description": desc
                        })
                except Exception:
                    continue
                time.sleep(0.6)
        except Exception as e:
            print(f"   Preskačem LinkedIn ({keywords}): {e}")
    print(f"   -> Pronađeno {len(jobs)} novih jedinstvenih LinkedIn oglasa.")
    return jobs


def main():
    all_jobs = fetch_rss_jobs() + fetch_infostud_jobs() + fetch_linkedin_jobs()
    total_found = len(all_jobs)
    # Obrađujemo do 35 oglasa po pokretanju da se posao uvek završi za ~5 min bez prekida
    all_jobs = all_jobs[:35]
    print(f"\n6. Pronađeno ukupno {total_found} novih oglasa; u ovoj turi analiziram prvih {len(all_jobs)}.\n")
    today = datetime.now().strftime("%d.%m.%Y.")

    for idx, job in enumerate(all_jobs, start=1):
        if not job["description"]:
            continue
        print(f"[{idx}/{len(all_jobs)}] Analiziram: {job['source']} - {job['title']}...")
        result = analyze_job(job["title"], job["company"], job["link"], job["description"], job["source"])
        if not result:
            time.sleep(4.5)
            continue

        kat = result.get("kategorija")
        print(f"   => Odluka: {kat} | {result.get('razlog', '')}")

        if kat != "ODBACI":
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

        time.sleep(4.5)

    print("\nZavršeno!")


if __name__ == "__main__":
    main()
