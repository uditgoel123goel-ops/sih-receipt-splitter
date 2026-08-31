import base64
import calendar
import datetime
import io
import json
import os
import sqlite3
import urllib.parse
from typing import Dict, List

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from groq import Groq
from PIL import Image, ImageEnhance
from pydantic import BaseModel

_ENV_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ENV_DIR, "../.env"))
load_dotenv(os.path.join(_ENV_DIR, ".env"), override=True)

app = FastAPI(title="SIH Multi-Payer Expense Splitter API", redirect_slashes=False)

# --- DATABASE SETUP ---
conn = sqlite3.connect("history.db", check_same_thread=False)
cursor = conn.cursor()

# 1. Budget Table
cursor.execute('''
    CREATE TABLE IF NOT EXISTS budget (
        id INTEGER PRIMARY KEY,
        monthly_limit REAL,
        current_spent REAL DEFAULT 0.0
    )
''')
cursor.execute('INSERT OR IGNORE INTO budget (id, monthly_limit, current_spent) VALUES (1, 5000.0, 0.0)')
conn.commit()

try:
    cursor.execute("ALTER TABLE budget ADD COLUMN fixed_expenses REAL DEFAULT 0.0")
    conn.commit()
except:
    pass

# 2. History Table
cursor.execute("""
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        total_bill REAL,
        settlements TEXT
    )
""")
conn.commit()

# 3. Personal Expenses Tables (additive only)
cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_expenses (
        id INTEGER PRIMARY KEY,
        title TEXT,
        amount REAL,
        category TEXT,
        date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS fixed_expenses (
        id INTEGER PRIMARY KEY,
        name TEXT,
        amount REAL,
        category TEXT,
        due_date INTEGER
    )
""")
conn.commit()
conn.close()
# -----------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError(
        "GROQ_API_KEY is missing. Add it to backend/.env (or a .env file one level up in the project root) "
        "as GROQ_API_KEY=gsk_... and restart the server."
    )
groq_client = Groq(api_key=GROQ_API_KEY)

# --- Data Models ---
class PayerInfo(BaseModel):
    name: str
    amount_paid: float
    upi_id: str = ""

class ExpenseItem(BaseModel):
    name: str
    price: float
    assigned_to: List[str]

class MultiPayerSplitRequest(BaseModel):
    tax: float = 0.0
    discount: float = 0.0
    service_charge: float = 0.0
    payers: List[PayerInfo]
    items: List[ExpenseItem]

class BudgetUpdate(BaseModel):
    new_limit: float

class ExpenseAdd(BaseModel):
    amount: float

class FixedUpdate(BaseModel):
    fixed: float

class ExpenseTextRequest(BaseModel):
    text: str

class DailyExpense(BaseModel):
    title: str
    amount: float
    category: str
    date: str

class FixedExpense(BaseModel):
    name: str
    amount: float
    category: str
    due_date: int


# --- API Endpoints ---
@app.get("/")
def home():
    return {"status": "success", "message": "Database Backend Active"}

@app.get("/api/history")
def get_history():
    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, timestamp, total_bill, settlements FROM history ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()

    history_data = [{
        "id": r[0],
        "date": r[1],
        "total": r[2],
        "settlements": json.loads(r[3]),
    } for r in rows]
    return {"status": "success", "data": history_data}

@app.post("/api/parse-receipt")
async def parse_receipt(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        img = Image.open(io.BytesIO(contents)).convert("L")
        img.thumbnail((1024, 1024))
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)
        
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=85)
        base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{base64_image}"

        prompt = """
        You are a strict financial AI parser. 
        RULE 1: If it is a picture of a human, animal, scenery, or random object, return exactly: {"is_valid_receipt": false}.
        RULE 2: If it IS a valid receipt, extract details. 
        
        CRITICAL: You MUST return ONLY raw, pure JSON. 
        - DO NOT include any conversational text. 
        - DO NOT wrap the output in ```json markdown blocks. 
        
        Return ONLY valid JSON matching this exact structure:
        {"is_valid_receipt": true, "store_name": "Name", "subtotal": 100.0, "tax": 5.0, "service_charge": 10.0, "discount": 0.0, "total": 115.0, "items": [{"name": "Item 1", "quantity": 1, "price": 100.0}]}
        """

        chat_completion = groq_client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_url}}]}],
            response_format={"type": "json_object"},
            max_tokens=4096
        )

        raw_response = chat_completion.choices[0].message.content
        parsed_data = json.loads(raw_response)
        
        if not parsed_data.get("is_valid_receipt", True):
            raise HTTPException(status_code=400, detail="The AI detected this image is not a valid receipt.")
            
        return {"status": "success", "data": parsed_data}

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="AI failed to generate valid JSON format. Try again.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/calculate-split")
def calculate_multi_payer_split(req: MultiPayerSplitRequest):
    subtotal = sum(item.price for item in req.items)
    if subtotal <= 0:
        raise HTTPException(status_code=400, detail="Subtotal must be > 0")

    effective_total = subtotal + req.tax + req.service_charge - req.discount
    tax_factor = effective_total / subtotal

    paid_map, upi_map = {}, {}
    for p in req.payers:
        paid_map[p.name] = paid_map.get(p.name, 0.0) + p.amount_paid
        if p.upi_id:
            upi_map[p.name] = p.upi_id

    consumed_map = {}
    for item in req.items:
        if not item.assigned_to:
            continue
        share = (item.price * tax_factor) / len(item.assigned_to)
        for person in item.assigned_to:
            consumed_map[person] = consumed_map.get(person, 0.0) + share

    all_members = set(paid_map.keys()).union(set(consumed_map.keys()))
    balances = {
        m: round(paid_map.get(m, 0.0) - consumed_map.get(m, 0.0), 2)
        for m in all_members
    }

    debtors = sorted(
        [{"name": p, "amount": -b} for p, b in balances.items() if b < -0.01],
        key=lambda x: x["amount"],
        reverse=True,
    )
    creditors = sorted(
        [{"name": p, "amount": b} for p, b in balances.items() if b > 0.01],
        key=lambda x: x["amount"],
        reverse=True,
    )

    settlements = []
    d_idx = 0
    c_idx = 0
    while d_idx < len(debtors) and c_idx < len(creditors):
        debtor, creditor = debtors[d_idx], creditors[c_idx]
        settle_amount = round(min(debtor["amount"], creditor["amount"]), 2)

        if settle_amount > 0:
            creditor_upi = upi_map.get(creditor["name"], "")
            qr_url = ""

            if creditor_upi:
                creditor_name = creditor["name"]
                upi_payload = f"upi://pay?pa={creditor_upi}&pn={urllib.parse.quote(creditor_name)}&am={settle_amount:.2f}&cu=INR&tn=Settlement"
                qr_url = f"[https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=](https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=){urllib.parse.quote(upi_payload)}"

            settlements.append({
                "from_person": debtor["name"],
                "to_person": creditor["name"],
                "amount": settle_amount,
                "qr_code_url": qr_url,
            })

        debtor["amount"] -= settle_amount
        creditor["amount"] -= settle_amount
        if debtor["amount"] < 0.01:
            d_idx += 1
        if creditor["amount"] < 0.01:
            c_idx += 1

    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO history (timestamp, total_bill, settlements) VALUES (?, ?, ?)",

        (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            effective_total,
            json.dumps(settlements),
        ),
    )
    conn.commit()
    conn.close()

    return {
        "status": "success",
        "total_bill": round(effective_total, 2),
        "net_balances": balances,
        "settlements": settlements,
    }

@app.get("/api/budget")
def get_budget():
    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute("SELECT monthly_limit, current_spent, fixed_expenses FROM budget WHERE id=1")
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return {"monthly_limit": 5000, "current_spent": 0, "fixed_expenses": 0, "status": "Green", "percentage": 0, "leftover": 5000, "daily_limit": 0}
    
    limit, spent, fixed = row
    if fixed is None: fixed = 0.0
    
    leftover = limit - spent - fixed
    
    today = datetime.date.today()
    _, last_day = calendar.monthrange(today.year, today.month)
    days_left = last_day - today.day + 1
    daily_limit = leftover / days_left if days_left > 0 and leftover > 0 else 0

    total_committed = spent + fixed
    percentage = (total_committed / limit * 100) if limit > 0 else 0
    
    if percentage >= 90: status_color = "Red"
    elif percentage >= 70: status_color = "Yellow"
    else: status_color = "Green"
    
    return {
        "monthly_limit": limit,
        "current_spent": spent,
        "fixed_expenses": fixed,
        "leftover": round(leftover, 2),
        "daily_limit": round(daily_limit, 2),
        "percentage": round(percentage, 1),
        "status": status_color
    }

@app.post("/api/budget")
def update_budget(req: BudgetUpdate):
    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE budget SET monthly_limit = ? WHERE id = 1", (req.new_limit,))
    conn.commit()
    conn.close()
    return {"message": "Budget limit updated"}

@app.post("/api/budget/fixed")
# Create the table for individual fixed expenses if it doesn't exist
def init_fixed_db():
    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fixed_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            amount REAL
        )
    """)
    conn.commit()
    conn.close()

init_fixed_db()

@app.get("/api/budget/fixed/list")
def get_fixed_list():
    conn = sqlite3.connect("history.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM fixed_items")
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items

@app.post("/api/budget/fixed/add")
def add_fixed_item(req: dict):
    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO fixed_items (name, amount) VALUES (?, ?)", (req.get("name"), req.get("amount")))
    
    # Update the total in the budget table automatically
    cursor.execute("UPDATE budget SET fixed_expenses = (SELECT COALESCE(SUM(amount), 0) FROM fixed_items) WHERE id = 1")
    conn.commit()
    conn.close()
    return {"message": "Fixed expense added"}

@app.delete("/api/budget/fixed/{item_id}")
def delete_fixed_item(item_id: int):
    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM fixed_items WHERE id = ?", (item_id,))
    
    # Update the total in the budget table automatically
    cursor.execute("UPDATE budget SET fixed_expenses = (SELECT COALESCE(SUM(amount), 0) FROM fixed_items) WHERE id = 1")
    conn.commit()
    conn.close()
    return {"message": "Fixed expense deleted"}

@app.post("/api/budget/add")
def add_expense(req: ExpenseAdd):
    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE budget SET current_spent = current_spent + ? WHERE id = 1", (req.amount,))
    conn.commit()
    conn.close()
    return {"message": "Expense added manually"}

# --- Personal Expenses Module (additive; does not alter existing routes) ---
EXTRACT_SYSTEM_PROMPT = (
    "You are a financial AI. Extract expenses from the user's text. "
    "Return ONLY a valid JSON array of objects with keys: 'title', 'amount', 'category'. "
    "Do NOT wrap the response in markdown blocks (e.g., no ```json). Do NOT add explanations."
)

@app.post("/api/expenses/extract")
@app.post("/api/expenses/extract/")
def extract_expenses(req: ExpenseTextRequest):
    try:
        chat_completion = groq_client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[
                {"role": "system", "content": "You are a financial AI. Extract expenses from the user's text and return a JSON object containing a key named 'expenses' which is an array of objects with keys: 'title', 'amount', 'category'."},
                {"role": "user", "content": req.text},
            ],
            response_format={"type": "json_object"},
            max_tokens=2048,
        )
        
        import re
        raw_response = chat_completion.choices[0].message.content or ""
        
        # 1. Strip markdown backticks
        cleaned = re.sub(r"```(?:json)?", "", raw_response).replace("```", "").strip()
        
        # 2. Extract ONLY the JSON block
        json_match = re.search(r'(\[.*\]|\{.*\})', cleaned, re.DOTALL)
        if json_match:
            cleaned = json_match.group(1)
            
        parsed = json.loads(cleaned)
        
        # 3. Handle both direct list or wrapped dictionary (e.g., {"expenses": [...]})
        if isinstance(parsed, dict):
            found_list = None
            for value in parsed.values():
                if isinstance(value, list):
                    found_list = value
                    break
            if found_list is not None:
                parsed = found_list
            else:
                parsed = [parsed]
                
        if not isinstance(parsed, list):
            raise ValueError("AI response did not yield a valid expense list")
            
        return parsed

    except HTTPException:
        raise
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="AI failed to generate valid JSON format. Try again.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
@app.post("/api/expenses/daily")
def create_daily_expense(req: DailyExpense):
    try:
        conn = sqlite3.connect("history.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO daily_expenses (title, amount, category, date) VALUES (?, ?, ?, ?)",
            (req.title, req.amount, req.category, req.date),
        )
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/expenses/daily")
def get_daily_expenses(month: str = None):
    try:
        conn = sqlite3.connect("history.db")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if month:
            cursor.execute(
                "SELECT id, title, amount, category, date, created_at FROM daily_expenses WHERE date LIKE ? ORDER BY date DESC, id DESC",
                (month + "%",),
            )
        else:
            cursor.execute(
                "SELECT id, title, amount, category, date, created_at FROM daily_expenses ORDER BY date DESC, id DESC"
            )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/expenses/fixed")
def create_fixed_expense(req: FixedExpense):
    try:
        conn = sqlite3.connect("history.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO fixed_expenses (name, amount, category, due_date) VALUES (?, ?, ?, ?)",
            (req.name, req.amount, req.category, req.due_date),
        )
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/expenses/fixed")
def get_fixed_expenses():
    try:
        conn = sqlite3.connect("history.db")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, amount, category, due_date FROM fixed_expenses ORDER BY due_date ASC, id ASC")
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- VERY IMPORTANT: Static Files Must Be Mounted LAST ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "../frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")