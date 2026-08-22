import base64
import json
import urllib.parse
from typing import Dict, List
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from groq import Groq
from pydantic import BaseModel

app = FastAPI(title="SIH Multi-Payer Expense Splitter API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = "HIDDEN_FOR_GITHUB"
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
    payers: List[PayerInfo]
    items: List[ExpenseItem]


@app.get("/")
def home():
    return {"status": "success", "message": "Multi-Payer Backend Active"}


@app.post("/api/parse-receipt")
async def parse_receipt(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        base64_image = base64.b64encode(contents).decode("utf-8")
        mime_type = file.content_type if file.content_type else "image/jpeg"
        data_url = f"data:{mime_type};base64,{base64_image}"

        prompt = """
        Extract store_name (string), date (string or null), items (array of {name, quantity, price}), 
        subtotal (float), tax (float), discount (float), and total (float) from this receipt.
        Return ONLY valid JSON matching this structure.
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
        return {"status": "success", "data": parsed_data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/calculate-split")
def calculate_multi_payer_split(req: MultiPayerSplitRequest):
    subtotal = sum(item.price for item in req.items)
    if subtotal <= 0:
        raise HTTPException(
            status_code=400, detail="Subtotal must be greater than 0"
        )

    effective_total = subtotal + req.tax - req.discount
    tax_factor = effective_total / subtotal

    # 1. Map who paid what upfront and store UPI IDs
    paid_map: Dict[str, float] = {}
    upi_map: Dict[str, str] = {}
    for p in req.payers:
        paid_map[p.name] = paid_map.get(p.name, 0.0) + p.amount_paid
        if p.upi_id:
            upi_map[p.name] = p.upi_id

    # 2. Calculate consumption per individual
    consumed_map: Dict[str, float] = {}
    for item in req.items:
        if not item.assigned_to:
            continue
        share = (item.price * tax_factor) / len(item.assigned_to)
        for person in item.assigned_to:
            consumed_map[person] = consumed_map.get(person, 0.0) + share

    # 3. Calculate net balances (Net = Paid - Consumed)
    all_members = set(paid_map.keys()).union(set(consumed_map.keys()))
    balances: Dict[str, float] = {}
    for m in all_members:
        net = paid_map.get(m, 0.0) - consumed_map.get(m, 0.0)
        balances[m] = round(net, 2)

    # 4. Greedy Settlement Minimization
    debtors = []  # Owe money (negative balance)
    creditors = []  # Owed money (positive balance)

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

        settle_amount = min(debtor["amount"], creditor["amount"])
        settle_amount = round(settle_amount, 2)

        if settle_amount > 0:
            creditor_upi = upi_map.get(creditor["name"], "")
            upi_payload = ""
            qr_url = ""

            if creditor_upi:
                upi_payload = f"upi://pay?pa={creditor_upi}&pn={urllib.parse.quote(creditor['name'])}&am={settle_amount:.2f}&cu=INR&tn={urllib.parse.quote('Settlement')}"
                qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(upi_payload)}"

            settlements.append(
                {
                    "from_person": debtor["name"],
                    "to_person": creditor["name"],
                    "amount": settle_amount,
                    "to_upi": creditor_upi,
                    "qr_code_url": qr_url,
                }
            )

        debtor["amount"] -= settle_amount
        creditor["amount"] -= settle_amount

        if debtor["amount"] < 0.01:
            d_idx += 1
        if creditor["amount"] < 0.01:
            c_idx += 1

    return {
        "status": "success",
        "total_bill": round(effective_total, 2),
        "consumed_breakdown": {
            k: round(v, 2) for k, v in consumed_map.items()
        },
        "net_balances": balances,
        "settlements": settlements,
    }