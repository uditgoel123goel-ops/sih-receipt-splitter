import base64
import json
import urllib.parse
import os
import io
from dotenv import load_dotenv
from typing import Dict, List
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
from pydantic import BaseModel
from PIL import Image, ImageEnhance

# Load the hidden .env file securely
load_dotenv()

app = FastAPI(title="SIH Multi-Payer Expense Splitter API")

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
    service_charge: float = 0.0  # NEW: Added service charge support
    payers: List[PayerInfo]
    items: List[ExpenseItem]


@app.get("/")
def home():
    return {"status": "success", "message": "Upgraded AI Backend Active"}


@app.post("/api/parse-receipt")
async def parse_receipt(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        
        # --- UPGRADE 1: IMAGE PRE-PROCESSING ---
        # 1. Open the image using Pillow
        img = Image.open(io.BytesIO(contents))
        
        # 2. Convert to grayscale (removes confusing background colors)
        img = img.convert("L")
        
        # 3. Boost contrast by 2.0x (makes faded text pop out)
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)
        
        # 4. Save back to memory as a clean JPEG
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG")
        base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{base64_image}"

        # --- UPGRADE 2: ADVANCED PROMPT ENGINEERING ---
        prompt = """
        You are a strict financial AI parser. Analyze this image carefully.
        
        RULE 1: Determine if this is a receipt, invoice, or bill. If it is a picture of a human, animal, scenery, or random object, return exactly: {"is_valid_receipt": false} and nothing else.
        
        RULE 2: If it IS a valid receipt, extract the details. Combine all taxes (CGST, SGST, VAT) into one total "tax" amount. Separate any "service charge" or "tip" into the "service_charge" field.
        
        Return ONLY valid JSON matching this exact structure:
        {
          "is_valid_receipt": true,
          "store_name": "Cafe Name",
          "date": "2026-08-23",
          "items": [{"name": "Item 1", "quantity": 1, "price": 100.0}],
          "subtotal": 100.0,
          "tax": 5.0,
          "service_charge": 10.0,
          "discount": 0.0,
          "total": 115.0
        }
        """

        chat_completion = groq_client.chat.completions.create(
            model="qwen/qwen3.6-27b",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            response_format={"type": "json_object"},
        )

        parsed_data = json.loads(chat_completion.choices[0].message.content)
        
        # Check if the AI flagged it as a non-receipt
        if not parsed_data.get("is_valid_receipt", True):
            raise HTTPException(status_code=400, detail="The AI detected this image is not a valid receipt. Please upload a clear bill.")

        return {"status": "success", "data": parsed_data}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/calculate-split")
def calculate_multi_payer_split(req: MultiPayerSplitRequest):
    subtotal = sum(item.price for item in req.items)
    if subtotal <= 0:
        raise HTTPException(status_code=400, detail="Subtotal must be > 0")

    # Combine tax and service charge for proportional splitting
    effective_total = subtotal + req.tax + req.service_charge - req.discount
    tax_factor = effective_total / subtotal

    paid_map: Dict[str, float] = {}
    upi_map: Dict[str, str] = {}
    for p in req.payers:
        paid_map[p.name] = paid_map.get(p.name, 0.0) + p.amount_paid
        if p.upi_id:
            upi_map[p.name] = p.upi_id

    consumed_map: Dict[str, float] = {}
    for item in req.items:
        if not item.assigned_to:
            continue
        share = (item.price * tax_factor) / len(item.assigned_to)
        for person in item.assigned_to:
            consumed_map[person] = consumed_map.get(person, 0.0) + share

    all_members = set(paid_map.keys()).union(set(consumed_map.keys()))
    balances: Dict[str, float] = {}
    for m in all_members:
        net = paid_map.get(m, 0.0) - consumed_map.get(m, 0.0)
        balances[m] = round(net, 2)

    debtors = [] 
    creditors = [] 
    for person, bal in balances.items():
        if bal < -0.01:
            debtors.append({"name": person, "amount": -bal})
        elif bal > 0.01:
            creditors.append({"name": person, "amount": bal})

    debtors.sort(key=lambda x: x["amount"], reverse=True)
    creditors.sort(key=lambda x: x["amount"], reverse=True)

    settlements = []
    d_idx, c_idx = 0, 0

    while d_idx < len(debtors) and c_idx < len(creditors):
        debtor = debtors[d_idx]
        creditor = creditors[c_idx]

        settle_amount = round(min(debtor["amount"], creditor["amount"]), 2)

        if settle_amount > 0:
            creditor_upi = upi_map.get(creditor["name"], "")
            upi_payload = ""
            qr_url = ""

            if creditor_upi:
                upi_payload = f"upi://pay?pa={creditor_upi}&pn={urllib.parse.quote(creditor['name'])}&am={settle_amount:.2f}&cu=INR&tn={urllib.parse.quote('Settlement')}"
                qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(upi_payload)}"

            settlements.append({
                "from_person": debtor["name"],
                "to_person": creditor["name"],
                "amount": settle_amount,
                "to_upi": creditor_upi,
                "qr_code_url": qr_url,
            })

        debtor["amount"] -= settle_amount
        creditor["amount"] -= settle_amount

        if debtor["amount"] < 0.01:
            d_idx += 1
        if creditor["amount"] < 0.01:
            c_idx += 1

    return {
        "status": "success",
        "total_bill": round(effective_total, 2),
        "consumed_breakdown": {k: round(v, 2) for k, v in consumed_map.items()},
        "net_balances": balances,
        "settlements": settlements,
    }