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

app = FastAPI(title="SIH Multi-Payer Expense Splitter API")

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
conn.close()
# -----------------------------------------

load_dotenv()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
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
    d_idx, c_idx = 0

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
def update_fixed(req: FixedUpdate):
    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE budget SET fixed_expenses = ? WHERE id = 1", (req.fixed,))
    conn.commit()
    conn.close()
    return {"message": "Fixed expenses updated"}

@app.post("/api/budget/add")
def add_expense(req: ExpenseAdd):
    conn = sqlite3.connect("history.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE budget SET current_spent = current_spent + ? WHERE id = 1", (req.amount,))
    conn.commit()
    conn.close()
    return {"message": "Expense added manually"}

# --- VERY IMPORTANT: Static Files Must Be Mounted LAST ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "../frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")