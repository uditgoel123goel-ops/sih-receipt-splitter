# AI Receipt Splitter

Snap a receipt, let AI extract every line item, and split the total across your group with UPI-ready settlements.

## Features

- **AI Receipt Parsing** — Upload a photo of any receipt and a vision model extracts store name, items, prices, tax, service charge, discount, and total automatically
- **Multi-Payer Split** — Supports multiple people paying different amounts; calculates proportional shares including tax and discounts
- **Minimum Settlements** — Greedy algorithm determines the fewest transactions needed to settle all debts
- **UPI QR Codes** — Each settlement generates a QR code with a UPI deep link for instant payment
- **Transaction Ledger** — All past splits are stored in a database and can be reviewed anytime
- **Smart Budget Guard** — Monthly budget tracker with fixed expenses, color-coded progress bar, and daily safe spend limit

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, FastAPI, Uvicorn |
| Frontend | HTML, CSS, JavaScript, Tailwind CSS |
| AI Model | Groq API (qwen/qwen3.6-27b vision model) |
| Database | SQLite |
| QR Codes | api.qrserver.com |

## Project Structure

```
sih-receipt-splitter/
├── backend/
│   ├── main.py              # FastAPI server (all API endpoints)
│   ├── requirements.txt     # Python dependencies
│   ├── history.db           # SQLite database (auto-created)
│   └── test_ai.py           # Groq API connectivity test
└── frontend/
    ├── index.html           # Main UI
    └── index_backup.html    # Backup/lighter version
```

## Setup

### Prerequisites

- Python 3.8+
- A free Groq API key from [console.groq.com](https://console.groq.com)

### Installation

```bash
# Clone the repository
git clone https://github.com/<your-username>/sih-receipt-splitter.git
cd sih-receipt-splitter

# Create virtual environment
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create .env file with your API key
echo "GROQ_API_KEY=gsk_your_key_here" > .env

# Start the server
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

> **Note:** The production frontend has hardcoded URLs pointing to the Render deployment. For local development, change the URLs in `frontend/index.html` from `https://sih-receipt-splitter-1.onrender.com` to `http://localhost:8000`, or use `index_backup.html` which already points to localhost.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| POST | `/api/parse-receipt` | Upload receipt image, get AI-extracted items |
| POST | `/api/calculate-split` | Calculate multi-payer split with settlements |
| GET | `/api/history` | Retrieve past split records |
| GET | `/api/budget` | Get current budget status |
| POST | `/api/budget` | Update monthly budget limit |
| POST | `/api/budget/fixed` | Update fixed expenses |
| POST | `/api/budget/add` | Add a manual expense |

## How It Works

1. **Upload** — Snap or upload a receipt image (JPG, PNG)
2. **AI Extracts** — Vision model reads every line item, tax, and total
3. **Configure Group** — Enter names, who paid what, and UPI IDs
4. **Assign Items** — Check/uncheck who consumed each item
5. **Settle** — Algorithm computes minimum settlements with UPI QR codes

## License

MIT
