import base64
import io
import json
from typing import List, Dict
from PIL import Image
import fitz  # PyMuPDF
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI
import uvicorn
import nest_asyncio
from pyngrok import ngrok

nest_asyncio.apply()

# === YOUR KEY + OFFICIAL xAI ENDPOINT ===
client = OpenAI(
    api_key="xai-",
    base_url="https://api.x.ai/v1"
)

# Kill old ngrok sessions
ngrok.kill()
ngrok.set_auth_token("368qDxCdnBkkgNJKVVutxLSbPbI_7k8mZiN8Vd18XijJ8q9TQ")

app = FastAPI(title="HackRx – Grok 4 Vision Bill Extractor")

# === Pydantic Models ===
class RequestModel(BaseModel):
    document: str

class BillItem(BaseModel):
    item_name: str
    item_amount: float
    item_rate: float
    item_quantity: float

class PageData(BaseModel):
    page_no: str
    page_type: str
    bill_items: List[BillItem]

class Data(BaseModel):
    pagewise_line_items: List[PageData]
    total_item_count: int

class ResponseModel(BaseModel):
    is_success: bool = True
    data: Data

# === Convert PDF → List of Images (FIXED LINE) ===
def pdf_to_images(pdf_bytes: bytes) -> List[Image.Image]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")   # ← FIXED: removed duplicate "stream="
    images = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        images.append(img)
    doc.close()
    return images

# === Extract one page using Grok 4 Vision ===
async def extract_page(img: Image.Image) -> Dict:
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    base64_image = base64.b64encode(buffered.getvalue()).decode()

    response = client.chat.completions.create(
        model="grok-4",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": """
Extract every bill line item from this page.
Classify page_type as "Bill Detail", "Final Bill" or "Pharmacy".
Output ONLY valid JSON:
{
  "page_type": "Bill Detail",
  "bill_items": [
    {"item_name": "exact name here", "item_quantity": 1.0, "item_rate": 1000.0, "item_amount": 1000.0}
  ]
}
No duplicates. Default quantity = 1.0. Ignore dates/IDs.
                    """},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
                ]
            }
        ],
        max_tokens=1500,
        temperature=0.0
    )

    try:
        content = response.choices[0].message.content.strip("```json").strip("```")
        return json.loads(content)
    except:
        return {"page_type": "Unknown", "bill_items": []}

# === Main Endpoint ===
@app.post("/extract-bill-data", response_model=ResponseModel)
async def main(req: RequestModel):
    try:
        r = requests.get(req.document, timeout=30)
        r.raise_for_status()

        if req.document.lower().endswith('.pdf'):
            images = pdf_to_images(r.content)
        else:
            images = [Image.open(io.BytesIO(r.content))]

        all_pages = []
        total = 0

        for i, img in enumerate(images, 1):
            result = await extract_page(img)
            items = [BillItem(**it) for it in result.get("bill_items", [])]
            all_pages.append(PageData(
                page_no=str(i),
                page_type=result.get("page_type", "Unknown"),
                bill_items=items
            ))
            total += len(items)

        return ResponseModel(data=Data(pagewise_line_items=all_pages, total_item_count=total))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# === Start Public Tunnel ===
tunnel = ngrok.connect(8000, bind_tls=True)
print(f"\nYOUR PUBLIC API IS LIVE!")
print(f"Swagger: {tunnel.public_url}/docs")
print(f"Endpoint: {tunnel.public_url}/extract-bill-data\n")
