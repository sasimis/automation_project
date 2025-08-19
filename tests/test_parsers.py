
from app.processors.forms import parse_forms_dir
from app.processors.emails import parse_emails_dir
from app.processors.invoices import parse_invoices_dir

def test_parsers_empty(tmp_path):
    assert parse_forms_dir(str(tmp_path)) == []
    assert parse_emails_dir(str(tmp_path)) == []
    assert parse_invoices_dir(str(tmp_path)) == []
