# SplitSphere

Snap a receipt, let AI extract every line item, and split the total across your group with UPI-ready settlements.

**Live Website:** [https://uditgoel123goel-ops.github.io/sih-receipt-splitter/](https://uditgoel123goel-ops.github.io/sih-receipt-splitter/)

> The frontend is hosted on GitHub Pages and calls the backend API deployed on [Render](https://sih-receipt-splitter-1.onrender.com). All receipt parsing, split calculations, and budget tracking are handled by the Render backend — the frontend is a static HTML/JS interface that communicates with it via REST API calls.

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
├── index.html               # Main UI (served by GitHub Pages & Render)
└── backend/
    ├── main.py              # FastAPI server (all API endpoints)
    ├── requirements.txt     # Python dependencies
    ├── history.db           # SQLite database (auto-created)
    └── test_ai.py           # Groq API connectivity test
```

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
