# TechFlow Automation (Human-in-the-Loop)

A comprehensive Streamlit application for processing and managing emails, forms, and invoices with a human-in-the-loop approval system. This application serves as a reference implementation for the AthenaGen AI Solutions Engineer assignment.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![Streamlit](https://img.shields.io/badge/Streamlit-1.28.0-red)
![License](https://img.shields.io/badge/License-MIT-green)

## ✨ Features

- **Multi-format Processing**: Handle emails (.eml), HTML forms, and invoices (HTML/PDF)
- **Smart Data Extraction**: Automatically extracts contact information, amounts, dates, and invoice numbers
- **Human-in-the-Loop Workflow**: Review, accept, or reject entries before exporting
- **Google Sheets Integration**: Export processed data directly to Google Sheets
- **Interactive UI**: Beautiful Streamlit interface with visual indicators and editing capabilities
- **Tutorial System**: Built-in step-by-step guide for new users
- **Database Management**: SQLite backend with persistent storage
- **Export Options**: Generate PDFs from emails and invoices

## 🚀 Quick Start

### Prerequisites

- Python 3.8 or higher
- Google account (for Sheets integration)

### Installation

1. Clone the repository:
```bash
git clone sasimis/automation_project
cd techflow-automation
```

2. Create and activate a virtual environment:
```bash
# On Unix/macOS
python -m venv .venv
source .venv/bin/activate

# On Windows
python -m venv .venv
.venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Set up your data folders:
```
data/
  forms/      # HTML form submissions
  emails/     # .eml email files
  invoices/   # HTML or PDF invoices
```

5. Run the application:
```bash
streamlit run app/main2.py
```

## 📁 Project Structure

```
techflow-automation/
├── app/
│   └── main2.py              # Main application file
├── data/                    # Sample data (not in repo)
│   ├── emails/              # Email files (.eml)
│   ├── forms/               # HTML form submissions
│   └── invoices/            # Invoice files (HTML/PDF)
├── processors/              # Data processing modules
│   ├── invoice_reader.py    # HTML invoice parser
│   └── pdf_invoice_reader.py # PDF invoice parser
├── assets/                  # Static assets
│   └── pdf_icon2.png       # PDF icon for UI
├── styles.css               # Custom CSS styles
├── requirements.txt         # Python dependencies
└── README.md               # This file
```

## ⚙️ Configuration

### Environment Variables

Set these environment variables for automatic configuration:

```bash
# Data directories
export EMAILS_DIR="./data/emails"
export FORMS_DIR="./data/forms"
export INVOICES_DIR="./data/invoices"

# Google Sheets integration
export GOOGLE_SHEET_ID="1_VQEjJpB_BCmlBSDYvF_GZJMz6LvMotBCMQZTu9CNUw"
export GOOGLE_APPLICATION_CREDENTIALS="google-credentials.json"
```

### Google Sheets Setup

1. Create a new Google Sheet and note its ID (from the URL)
2. Set up Google Service Account credentials:
   - Go to Google Cloud Console
   - Create a new project or select an existing one
   - Enable the Google Sheets API
   - Create a service account and download the JSON credentials
3. Share your Google Sheet with the service account email

## 🎯 Usage

1. **Add Data**: Place your emails, forms, and invoices in the respective folders
2. **Launch Application**: Run `streamlit run app/main2.py`
3. **Review Entries**: Use the interface to review pending entries
4. **Take Action**: Accept, reject, or edit entries as needed
5. **Export**: Push accepted entries to Google Sheets with a single click

### Tutorial System

The application includes a built-in quick tutorial that guides you through:
- Setting up data folders
- Configuring Google Sheets integration
- Reviewing and processing entries
- Exporting data to spreadsheets

## 📊 Output

The application generates several sheets in Google Sheets:
- **Summary**: Overview of processed data with totals
- **Accepted**: All accepted entries with full details
- **Rejected**: All rejected entries
- **Accepted Invoices**: Filtered view of accepted invoices
- **Accepted Forms**: Filtered view of accepted forms
- **Accepted Emails**: Filtered view of accepted emails

## 📝 License

This project is licensed under the MIT License

**Note**: This is a reference implementation for the AthenaGen AI Solutions Engineer assignment by Stratos Asimis.
