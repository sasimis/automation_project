
from pathlib import Path
from bs4 import BeautifulSoup
from typing import List
from app.utils.schema import LeadRecord

def parse_forms_dir(forms_dir: str) -> List[LeadRecord]:
    path = Path(forms_dir)
    if not path.exists():
        return []
    records = []
    for fp in sorted(path.glob("*.html")):
        try:
            html = fp.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(html, "lxml")
            def get_val(names):
                for n in names:
                    el = soup.find(attrs={"name": n}) or soup.find(id=n)
                    if el and el.get("value"):
                        return el.get("value")
                    el2 = soup.find(attrs={"class": n})
                    if el2 and el2.text:
                        return el2.text.strip()
                return None
            rec = LeadRecord(
                name=get_val(["name","full_name","fullname","customer_name"]),
                email=get_val(["email","email_address","mail"]),
                phone=get_val(["phone","tel","telephone","mobile"]),
                company=get_val(["company","org","organization"]),
                service=get_val(["service","interest","product"]),
                source="forms"
            )
            records.append(rec)
        except Exception:
            continue
    return records
