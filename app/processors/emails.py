
from pathlib import Path
from typing import List
from app.utils.schema import LeadRecord
import email

def parse_emails_dir(emails_dir: str) -> List[LeadRecord]:
    path = Path(emails_dir)
    if not path.exists():
        return []
    out: List[LeadRecord] = []
    for fp in sorted(path.glob("*.eml")):
        try:
            with open(fp, "rb") as f:
                msg = email.message_from_bytes(f.read())
            subject = msg.get("Subject","").lower()
            payload_text = ""
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    if ct in ("text/plain","text/html"):
                        try:
                            payload_text += part.get_payload(decode=True).decode(errors="ignore") + "\n"
                        except Exception:
                            pass
            else:
                try:
                    payload_text = msg.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    payload_text = str(msg.get_payload())

            if "invoice" in subject or "τιμολόγιο" in subject:
                continue

            def find_between(keys):
                for k in keys:
                    for line in payload_text.splitlines():
                        if k.lower() in line.lower():
                            return line.split(":")[-1].strip()
                return None

            rec = LeadRecord(
                name=find_between(["Name","Όνομα","Customer","Full Name"]),
                email=find_between(["Email","Mail"]),
                phone=find_between(["Phone","Τηλέφωνο","Mobile"]),
                company=find_between(["Company","Εταιρεία","Organization"]),
                service=find_between(["Service","Interest","Υπηρεσία"]),
                source="email"
            )
            out.append(rec)
        except Exception:
            continue
    return out
