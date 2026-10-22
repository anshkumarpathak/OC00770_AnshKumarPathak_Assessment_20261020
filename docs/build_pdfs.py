"""Render the two submission PDFs from the HTML sources in this folder.

    pip install playwright==1.48.0
    python docs/build_pdfs.py          # uses the installed Google Chrome; no browser download

Screenshots in docs/img/ were captured from the public portal.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
PDFS = [
    ("task5.html", "OC00770_Task5_Architecture_Demo_Monitoring_Safeguards.pdf",
     "Task 5 · Architecture, demo, monitoring dashboard and safeguards"),
    ("walkthrough.html", "OC00770_Demo_Portal_Walkthrough.pdf",
     "Demo portal walkthrough"),
    # v2: written for upper management, submitted with the live portal link
    ("task5_v2.html", "OC00770_Task5_Executive_Brief_v2.pdf",
     "Task 5 executive brief"),
    ("walkthrough_v2.html", "OC00770_Demo_Portal_Guide_v2.pdf",
     "Demo portal guide for reviewers"),
]
FOOTER = """<div style="width:100%; padding:0 15mm; font-family:Arial, sans-serif; font-size:7.5px; color:#6A7280;
  display:flex; justify-content:space-between;"><span>OC00770 · Ansh Kumar Pathak · {title}</span>
  <span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>"""

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page()
    for src, out, title in PDFS:
        page.goto((DOCS / src).as_uri(), wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.pdf(path=str(ROOT / out), format="A4", print_background=True, prefer_css_page_size=True,
                 display_header_footer=True, header_template="<span></span>",
                 footer_template=FOOTER.format(title=title))
        print("wrote", out)
    browser.close()
