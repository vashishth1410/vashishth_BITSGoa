# Full Final Setup: FastAPI OCR API + Ngrok Tunnel with Your Token (Fixed: Port 8001, Syntax Error, No Truncation)
# Run this once in Colab. Outputs your public endpoint. Changes port to 8001 to avoid conflict.

# Install Dependencies (if not already)
!apt update -qq && apt install -y tesseract-ocr
!pip install fastapi uvicorn pytesseract PyMuPDF pillow nest-asyncio pyngrok requests pydantic

# Imports
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
import requests
from io import BytesIO
import fitz  # PyMuPDF
from PIL import Image, ImageEnhance
import pytesseract
import re
import json
import os
from decimal import Decimal
import threading
import uvicorn
import time
import nest_asyncio
from pyngrok import ngrok

# Apply nest_asyncio for Colab
nest_asyncio.apply()

app = FastAPI(title="HackRX Bill Extraction API")

class DocumentRequest(BaseModel):
    document: str

    @field_validator('document')
    @classmethod
    def validate_url(cls, v):
        if not v.startswith('http'):
            raise ValueError('document must be a valid URL')
        return v

class TokenUsage(BaseModel):
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

class BillItem(BaseModel):
    item_name: str
    item_amount: float
    item_rate: float
    item_quantity: float

class PageItems(BaseModel):
    page_no: str
    page_type: str

    @field_validator('page_type')
    @classmethod
    def validate_page_type(cls, v):
        valid_types = ["Bill Detail", "Final Bill", "Pharmacy"]
        if v not in valid_types:
            raise ValueError(f'page_type must be one of {valid_types}')
        return v

    bill_items: List[BillItem]

class ExtractionResponse(BaseModel):
    is_success: bool
    token_usage: TokenUsage
    data: Dict[str, Any]

def infer_page_type(text: str) -> str:
    text_lower = text.lower()
    if any(word in text_lower for word in ["pharmacy", "drug", "qty.", "batch no.", "mfrs."]):
        return "Pharmacy"
    if any(word in text_lower for word in ["total payable", "net payable", "subtotal", "interim bill", "final total"]):
        return "Final Bill"
    return "Bill Detail"

def extract_with_tesseract(img: Image.Image, page_no: str) -> List[Dict[str, Any]]:
    img = img.convert('L')
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(2.0)
    config = r'--oem 3 --psm 6'
    text = pytesseract.image_to_string(img, config=config)
    print(f"Raw OCR for page {page_no}: {text[:200]}...")

    page_type = infer_page_type(text)

    patterns = [
        (r'^\s*(\d{2})\s+(.+?)\s+\d{2}/\d{2}/\d{4}\s+(\d+(?:\.\d+)?)\s+([\d.,]+)\s+([\d.,]+)\s+0\.00\s*$',
         lambda m: {"item_name": m.group(2).strip(), "item_quantity": float(m.group(3)), "item_rate": float(m.group(4).replace(',', '')), "item_amount": float(m.group(5).replace(',', ''))}),
        (r'^\s*(\d+)\s+IP CONSULTATION\s+CHARGES\s+(.+?)\s+\(.+?\)\s+1\.00\s+([\d,]+\.00)\s+([\d,]+\.00)\s+0\s+\d+\s*$',
         lambda m: {"item_name": m.group(2).strip(), "item_quantity": 1.0, "item_rate": float(m.group(3).replace(',', '')), "item_amount": float(m.group(4).replace(',', ''))}),
        (r'^\s*(\d+)\s+(.+?)\(\d+\s*\)\s+([\d,]+\.\d{2})\s*$',
         lambda m: {"item_name": m.group(2).strip(), "item_quantity": 1.0, "item_rate": float(m.group(3).replace(',', '')), "item_amount": float(m.group(3).replace(',', ''))}),
        (r'^\s*(.+?)\s+(\d+(?:\.\d+)?)\s+([\d.,]+)\s*$',
         lambda m: {"item_name": m.group(1).strip(), "item_quantity": float(m.group(2)), "item_rate": float(m.group(3).replace(',', '')) / float(m.group(2)) if float(m.group(2)) > 0 else 0, "item_amount": float(m.group(3).replace(',', ''))}),
    ]

    lines = text.split('\n')
    items = []

    for line in lines:
        line = line.strip()
        if not line or len(line) < 10:
            continue
        for pattern, parser in patterns:
            match = re.match(pattern, line, re.MULTILINE)
            if match:
                try:
                    item = parser(match)
                    items.append({**item, "page_no": page_no, "page_type": page_type})
                    print(f"Parsed ({page_type}): {item['item_name']} - Qty: {item['item_quantity']}, Rate: {item['item_rate']}, Amt: {item['item_amount']}")
                except ValueError:
                    pass
                break

    # Subtotal
    subtotal_patterns = [
        r'category total\s+(.+?)(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        r'subtotal\s*:?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        r'total amount\s*:?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
    ]
    for sp in subtotal_patterns:
        match = re.search(sp, text, re.IGNORECASE)
        if match:
            subtotal = float(match.group(1).replace(',', ''))
            items.append({
                "item_name": "Subtotal",
                "item_amount": subtotal,
                "item_rate": 0.0,
                "item_quantity": 0.0,
                "page_no": page_no,
                "page_type": page_type
            })
            print(f"Parsed Subtotal ({page_type}): {subtotal}")
            break

    return items

def process_document_tesseract(document_url: str) -> Dict[str, Any]:
    try:
        response = requests.get(document_url, timeout=30)
        response.raise_for_status()
        doc_bytes = response.content

        if document_url.lower().endswith('.pdf'):
            pdf = fitz.open(stream=doc_bytes, filetype="pdf")
            page_texts = []
            for page_num in range(len(pdf)):
                page = pdf.load_page(page_num)
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
                img = Image.open(BytesIO(pix.tobytes("png")))
                items = extract_with_tesseract(img, str(page_num + 1))
                page_texts.extend(items)
            pdf.close()
        else:
            img = Image.open(BytesIO(doc_bytes))
            items = extract_with_tesseract(img, "1")
            page_texts = items

        seen = set()
        pagewise = {}
        for item in page_texts:
            key = (item["item_name"], item["item_quantity"], item["item_rate"])
            if key not in seen or item["item_name"] == "Subtotal":
                seen.add(key)
                page_no = item["page_no"]
                if page_no not in pagewise:
                    pagewise[page_no] = {"page_no": page_no, "page_type": item["page_type"], "bill_items": []}
                pagewise[page_no]["bill_items"].append({
                    "item_name": item["item_name"],
                    "item_amount": round(float(item["item_amount"]), 2),
                    "item_rate": round(float(item["item_rate"]), 2),
                    "item_quantity": round(float(item["item_quantity"]), 2)
                })

        pagewise_list = list(pagewise.values())
        total_count = len([item for page in pagewise_list for item in page["bill_items"]])

        return {
            "is_success": True,
            "token_usage": {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0},
            "data": {
                "pagewise_line_items": pagewise_list,
                "total_item_count": total_count
            }
        }
    except requests.RequestException as e:
        print(f"URL fetch error: {e}")
        return {
            "is_success": False,
            "token_usage": {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0},
            "data": {"pagewise_line_items": [], "total_item_count": 0}
        }
    except Exception as e:
        print(f"Extraction error: {e}")
        return {
            "is_success": False,
            "token_usage": {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0},
            "data": {"pagewise_line_items": [], "total_item_count": 0}
        }

@app.post("/extract-bill-data", response_model=ExtractionResponse)
async def extract_bill_data(request: DocumentRequest):
    result = process_document_tesseract(request.document)
    if not result["is_success"]:
        raise HTTPException(status_code=500, detail="Extraction failed - check URL")
    return result

@app.get("/")
async def root():
    return {"message": "HackRX Bill Extraction API - POST to /extract-bill-data with {'document': 'url'}"}

def run_server():
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")  # Changed to 8001 to avoid conflict

# Main: Set Token, Start Server, Ngrok Tunnel
if __name__ == "__main__":
    # Set your authtoken
    ngrok.set_auth_token("ngrok token")
    
    # Kill old tunnels
    ngrok.kill()
    
    # Start server in background
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    
    # Wait for startup
    time.sleep(5)
    
    # Start ngrok tunnel on new port
    public_url = ngrok.connect(8001, bind_tls=True)
    
    print("✅ Server & Tunnel Started!")
    print(f"Local Docs: http://localhost:8001/docs")
    print(f"Public Docs: {public_url}/docs")
    print(f"Full Public Endpoint: {public_url}/extract-bill-data")
    print("Test with Postman: POST to endpoint with JSON {'document': 'sample_url'}")
    
    # Self-test (fixed: use public_url.public_url)
    test_url = public_url.public_url + "/"
    try:
        response = requests.get(test_url, timeout=10)
        if response.status_code == 200:
            print("✅ Self-Test Passed! Response:", response.json())
        else:
            print("❌ Self-Test Failed. Status:", response.status_code)
    except Exception as e:
        print("Self-Test Error:", e)
    
    # Keep alive
    server_thread.join()
