"""EU Legislative Observatory scraper. See README.md for architecture/methodology notes."""

import os
import re
import json
import time
import hashlib
import datetime
import traceback
import requests
from bs4 import BeautifulSoup


# The individual procedure page uses h2 tags to indicate headings on each page
# this function finds a section by its heading text, then returns the content block right after it
def section_body(soup, heading_text):
    h2 = None
    for candidate in soup.find_all("h2"):
        if candidate.get_text(strip=True) == heading_text:
            h2 = candidate
            break
    if not h2:
        return None
    row = h2.parent.parent
    return row.find_next_sibling()


# extracting the technical information table, one table describes one record
def extract_kv_table(table):
    result = {}
    if not table:
        return result
    for row in table.find_all("tr"):
        header = row.find("th")
        value = row.find("td")
        if not header or not value:
            continue
        key = header.get_text(strip=True)
        text = value.get_text(separator=" ", strip=True)
        links = [a["href"] for a in value.find_all("a") if a.get("href")]
        result[key] = {"text": text, "links": links} if links else text
    return result


# also extracting a table, but this one has a different structure - Key events, Documentation gateway, committee/rapporteur tables
def extract_header_table(table):
    rows_out = []
    if not table:
        return rows_out
    thead = table.find("thead")
    headers = [th.get_text(strip=True) for th in thead.find_all("th")] if thead else []
    tbody = table.find("tbody") or table
    for row in tbody.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        entry = {}
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) else f"col_{i}"
            entry[key] = cell.get_text(separator=" ", strip=True)
            links = [a["href"] for a in cell.find_all("a") if a.get("href")]
            if links:
                entry[f"{key}_links"] = links
        rows_out.append(entry)
    return rows_out


# this function walks through each institution's block, grabs its name from its button and looks for a table inside its section
def extract_accordion(accordion_div):
    sections = []
    if not accordion_div:
        return sections
    for item in accordion_div.find_all("li", class_="es_accordion-item"):
        title_el = item.find("span", class_="t-x")
        institution = title_el.get_text(strip=True) if title_el else None
        table = item.find("table")
        sections.append({
            "institution": institution,
            "rows": extract_header_table(table) if table else []
        })
    return sections


# Assembling everything together
# Function that takes one already-downloaded legislation doc and turns it into one Python dictionary with all the specified fields
def extract_data_points(procedure_page, url, hash_id, reference):
    fields = {}
    fields["content_hash"] = hash_id
    fields["url"] = url
    fields["reference"] = reference  # e.g. "2026/0181(COD)"

    h2s = procedure_page.find_all("h2")
    fields["title"] = h2s[1].get_text(strip=True) if len(h2s) > 1 else ""

    # Basic information column: Status and Subject
    status_label = procedure_page.find(string=re.compile(r"^Status$"))
    fields["status"] = status_label.parent.find_next_sibling().get_text(strip=True) if status_label else ""

    subject_label = procedure_page.find(string=re.compile(r"^Subject$"))
    fields["subjects"] = []
    if subject_label:
        subj_p = subject_label.parent.find_next_sibling()
        if subj_p:
            fields["subjects"] = [s.strip() for s in subj_p.get_text(separator="|").split("|") if s.strip()]

    # Technical information: instrument, legal basis, procedure subtype, stage, etc.
    tech_table = section_body(procedure_page, "Technical information")
    fields["technical_information"] = extract_kv_table(tech_table.find("table") if tech_table else None)

    # Key players: committees responsible and rapporteurs per institution
    key_players = section_body(procedure_page, "Key players")
    fields["key_players"] = extract_accordion(key_players)

    # Key events / timeline: equivalent to "last_action"
    key_events = section_body(procedure_page, "Key events")
    fields["timeline"] = extract_header_table(key_events.find("table") if key_events else None)
    fields["last_action"] = (
        f"{fields['timeline'][-1].get('Date', '')} - {fields['timeline'][-1].get('Event', '')}".strip(" -")
        if fields["timeline"] else ""
    )

    # Documentation gateway: documents per institution
    doc_gateway = section_body(procedure_page, "Documentation gateway")
    fields["documents"] = extract_accordion(doc_gateway)

    # Additional information: source/document/date table
    add_info = section_body(procedure_page, "Additional information")
    fields["additional_information"] = extract_header_table(add_info.find("table") if add_info else None)

    return fields


########################SCRAPING LOGIC##############################

#########STEP ONE#########

DATA_FILE = "data/eu_procedures.json"
TODAY_STR = datetime.date.today().isoformat()
BASE_URL = "https://oeil.europarl.europa.eu"
SEARCH_URL = f"{BASE_URL}/oeil/en/search"
HEAD = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
MAX_INDEX_PAGES = 25   # ~750 procedure refs collected from the index
MAX_PROCEDURES = 750   # scrape all of them -- matches the assignment's 500-1000+ row target

os.makedirs("data/changelogs", exist_ok=True)
os.makedirs("data/error_logs", exist_ok=True)
os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

if os.path.exists(DATA_FILE):
    print("Found existing dataset. Loading history...")
    with open(DATA_FILE, 'r') as f:
        yesterdays_list = json.load(f)
    old_data_map = {item['url']: item for item in yesterdays_list}
else:
    print("No existing dataset found. Initializing a baseline run...")
    old_data_map = {}


#########STEP TWO#########

changelog = {"date": TODAY_STR, "additions": [], "deletions": [], "modifications": []}
error_log = {"date": TODAY_STR, "errors": []}

#########STEP THREE#########

all_procedure_urls = []
for page_index in range(MAX_INDEX_PAGES):
    r = requests.get(SEARCH_URL, params={"pageIndex": page_index}, headers=HEAD, timeout=60)
    index_page = BeautifulSoup(r.content, "html.parser")
    links = index_page.select('a[href*="procedure-file?reference"]')
    if not links:
        print(f"pageIndex={page_index}: no more results, stopping index pagination")
        break
    all_procedure_urls += [BASE_URL + a['href'] for a in links]
    print(f"pageIndex={page_index}: {len(links)} refs (total so far: {len(all_procedure_urls)})")
    time.sleep(1)

all_procedure_urls = list(dict.fromkeys(all_procedure_urls))
print(f"Found {len(all_procedure_urls)} procedures total.")

for url, old_item in old_data_map.items():
    if url not in all_procedure_urls:
        changelog["deletions"].append({"reference": old_item["reference"], "url": url, "title": old_item.get("title")})


#########STEP FOUR#########

todays_procedures = []

for url in all_procedure_urls[:MAX_PROCEDURES]:
    yesterdays_item = old_data_map.get(url)
    reference = url.split("reference=")[-1]
    try:
        time.sleep(1)
        r = requests.get(url, headers=HEAD, timeout=60)
        procedure_page = BeautifulSoup(r.content, "html.parser")
        tech_table = section_body(procedure_page, "Technical information")
        tech_dict = extract_kv_table(tech_table.find("table") if tech_table else None)
        key_events = section_body(procedure_page, "Key events")
        events_text = key_events.get_text(" ", strip=True) if key_events else ""
        hash_string = " ".join((json.dumps(tech_dict, sort_keys=True) + events_text).split()).lower()
        hash_id = hashlib.md5(hash_string.encode("utf-8")).hexdigest()

        if url not in old_data_map:
            print(f"New: {reference}")
            procedure_dict = extract_data_points(procedure_page, url, hash_id, reference)
            todays_procedures.append(procedure_dict)
            changelog["additions"].append({"reference": reference, "url": url, "title": procedure_dict.get("title")})
        else:
            yesterdays_hash = yesterdays_item["content_hash"]
            if yesterdays_hash == hash_id:
                print(f"No change: {reference}")
                todays_procedures.append(yesterdays_item)
            else:
                print(f"Changed: {reference}")
                procedure_dict = extract_data_points(procedure_page, url, hash_id, reference)
                todays_procedures.append(procedure_dict)
                meaningful_changes = {}
                for key, value in procedure_dict.items():
                    if yesterdays_item.get(key) != value:
                        meaningful_changes[key] = {"from": yesterdays_item.get(key), "to": value}
                if meaningful_changes:
                    changelog["modifications"].append({"reference": reference, "changes": meaningful_changes})

    except Exception as e:
        print(f"Error scraping {url}: {str(e)}")
        if yesterdays_item:
            todays_procedures.append(yesterdays_item)
        error_log["errors"].append({
            "reference": reference, "url": url, "error_type": type(e).__name__,
            "message": str(e), "traceback": traceback.format_exc().splitlines()[-3:]
        })


#########STEP FIVE#########

with open(DATA_FILE, 'w') as f:
    sorted_data = sorted(todays_procedures, key=lambda x: x['reference'])
    json.dump(sorted_data, f, indent=2)

if changelog["additions"] or changelog["deletions"] or changelog["modifications"]:
    with open(f"data/changelogs/{TODAY_STR}.json", 'w') as f:
        json.dump(changelog, f, indent=2)

if error_log["errors"]:
    with open(f"data/error_logs/{TODAY_STR}.json", 'w') as f:
        json.dump(error_log, f, indent=2)

print("Scrape done!")
