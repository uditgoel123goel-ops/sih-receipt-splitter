import base64
import datetime
import io
import json
import os
import sqlite3
from typing import Dict, List
import urllib.parse
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
from PIL import Image, ImageEnhance
from pydantic import BaseModel
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import os

app = FastAPI(title="SIH Multi-Payer Expense Splitter API")

# Load environment variables
load_dotenv()

# Get the absolute path to the backend directory, then find the frontend folder next to it
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "../frontend")

# Mount the frontend directory at the root so your UI loads automatically
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")



app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY)


# --- Initialize SQLite Database ---
def init_db():
  conn = sqlite3.connect("history.db")
  cursor = conn.cursor()
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


init_db()


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


@app.get("/")
def home():
  return {"status": "success", "message": "Database Backend Active"}


@app.get("/api/history")
def get_history():
  conn = sqlite3.connect("history.db")
  cursor = conn.cursor()
  cursor.execute(
      "SELECT id, timestamp, total_bill, settlements FROM history ORDER BY id"
      " DESC"
  )
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
        print(f"\n--- NEW REQUEST: Processing {file.filename} ---")
        
        # 1. Read & Process Image
        contents = await file.read()
        img = Image.open(io.BytesIO(contents)).convert("L")
        img.thumbnail((1024, 1024))
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)
        
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=85)
        base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{base64_image}"
        print("✅ Step 1: Image compressed and converted to Base64.")

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

        # 2. Call Groq API
        print("⏳ Step 2: Sending request to Groq API...")
        chat_completion = groq_client.chat.completions.create(
           model="qwen/qwen3.6-27b",  # USING GROQ'S OFFICIAL FAST VISION MODEL
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_url}}]}],
            response_format={"type": "json_object"},
            max_tokens=4096
        )
        print("✅ Step 3: Received response from Groq.")

        # 3. Parse JSON
        raw_response = chat_completion.choices[0].message.content
        print(f"🔍 Raw AI Output: {raw_response[:200]}...") # Print first 200 chars to check format
        
        parsed_data = json.loads(raw_response)
        
        if not parsed_data.get("is_valid_receipt", True):
            raise HTTPException(status_code=400, detail="The AI detected this image is not a valid receipt.")
            
        print("✅ Step 4: JSON parsed successfully. Sending to frontend.")
        return {"status": "success", "data": parsed_data}

    except json.JSONDecodeError as e:
        print(f"❌ JSON ERROR: {e}\nRaw output was: {raw_response}")
        raise HTTPException(status_code=500, detail="AI failed to generate valid JSON format. Try again.")
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ CRITICAL ERROR: {str(e)}")
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
  d_idx, c_idx = 0, 0

  while d_idx < len(debtors) and c_idx < len(creditors):
    debtor, creditor = debtors[d_idx], creditors[c_idx]
    settle_amount = round(min(debtor["amount"], creditor["amount"]), 2)

    if settle_amount > 0:
      creditor_upi = upi_map.get(creditor["name"], "")
      qr_url = ""

      if creditor_upi:
        creditor_name = creditor["name"]
        upi_payload = f"upi://pay?pa={creditor_upi}&pn={urllib.parse.quote(creditor_name)}&am={settle_amount:.2f}&cu=INR&tn=Settlement"
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(upi_payload)}"

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

  # --- Save to SQLite Database ---
  conn = sqlite3.connect("history.db")
  cursor = conn.cursor()
  cursor.execute(
      "INSERT INTO history (timestamp, total_bill, settlements) VALUES (?, ?,"
      " ?)",
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