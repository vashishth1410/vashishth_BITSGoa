!pip install -q fastapi uvicorn pyngrok pillow pymupdf openai psutil nest-asyncio requests

# -------------------------------
# Kill old servers on :8000
# -------------------------------
import os, signal, psutil, time

# Kill uvicorn/ngrok processes if any
os.system("pkill -f uvicorn 2>/dev/null || true")
os.system("pkill -f ngrok 2>/dev/null || true")

for p in psutil.process_iter():
    try:
        for c in p.connections(kind="inet"):
            if c.laddr.port == 8000:
                p.kill()
    except Exception:
        pass

time.sleep(1)

# -------------------------------
# Ngrok setup
# -------------------------------
from pyngrok import ngrok

# Kill any existing ngrok session in this process
try:
    ngrok.kill()
except Exception:
    pass

# TODO: put your real ngrok token in an ENV VAR instead of hardcoding
ngrok.set_auth_token("36BnEYl1fkkpNnROxPhO5sbdv1Y_5QtyrMXSL9TwxXMiyTAe3")

# -------------------------------
# Imports
# -------------------------------
import base64, io, json, logging, threading, queue, sys
from urllib.parse import urlparse

from PIL import Image
import fitz  # from pymupdf
import requests

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI
import uvicorn
import nest_asyncio

nest_asyncio.apply()

# -------------------------------
# LOGGING SETUP (LIVE STREAM)
# -------------------------------
log_queue = queue.Queue()

class QueueHandler(logging.Handler):
    def emit(self, record):
        log_queue.put(self.format(record))

logger = logging.getLogger("billapi")
logger.setLevel(logging.INFO)
handler = QueueHandler()
formatter = logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S")
handler.setFormatter(formatter)
logger.addHandler(handler)

def print_logs():
    while True:
        msg = log_queue.get()
        print(msg)
        sys.stdout.flush()

threading.Thread(target=print_logs, daemon=True).start()

# -------------------------------
# XAI Model Client
# -------------------------------
# ✅ Better: use environment variable instead of hardcoding key
# os.environ["XAI_API_KEY"] = "..."
client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY", "3CoLQcN9JDlXzxF4MyuKXI66FKBHgksQnDO2yLZvSLJJwkBAwHr4r9y1J4OClaFNVCIyaK8tkducbATL"),
    base_url="https://api.x.ai/v1",
)

# -------------------------------
# FastAPI Models
# -------------------------------
app = FastAPI(title="Bill Extractor API")

class RequestModel(BaseModel):
    document: str  # URL to PDF or image

class BillItem(BaseModel):
    item_name: str
    item_amount: float
    item_rate: float
    item_quantity: float

class PageData(BaseModel):
    page_no: str
    page_type: str
    bill_items: list[BillItem]

class Data(BaseModel):
    pagewise_line_items: list[PageData]
    total_item_count: int

class ResponseModel(BaseModel):
    is_success: bool = True
    data: Data

# Optional health check
@app.get("/health")
def health():
    return {"status": "ok"}

# -------------------------------
# PDF → Images
# -------------------------------
def pdf_to_images(pdf_bytes: bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    imgs = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
        img = Image.open(io.BytesIO(pix.tobytes("jpeg", jpg_quality=70)))
        imgs.append(img)
    doc.close()
    return imgs

# -------------------------------
# Detect if URL is a PDF
# -------------------------------
def is_pdf_url(url: str, resp: requests.Response) -> bool:
    """
    More robust PDF detection:
    - Check URL path extension (.pdf) ignoring query params
    - Fallback to Content-Type header
    """
    path = urlparse(url).path.lower()
    if path.endswith(".pdf"):
        return True

    ct = resp.headers.get("Content-Type", "").lower()
    if "application/pdf" in ct:
        return True

    return False

# -------------------------------
# Extract Single Page
# -------------------------------
async def extract_page(img: Image.Image, page_num: int):
    logger.info(f"Processing page {page_num}...")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    b64 = base64.b64encode(buf.getvalue()).decode()

    prompt = (
        f"You are an invoice/bill parser. Extract all item rows for page {page_num}.\n"
        f"Return a valid JSON object of the form:\n"
        f'{{"page_type": "Bill Detail | Final Bill | Pharmacy | Unknown", '
        f'"bill_items": [{{"item_name": "...", "item_amount": 0.0, "item_rate": 0.0, "item_quantity": 0.0}}...]}}\n'
        f"Do not include any extra text, only JSON."
    )

    try:
        resp = client.chat.completions.create(
            model="grok-4",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}"
                        }
                    }
                ]
            }],
            max_tokens=1000,
            temperature=0,
        )

        # xAI Grok returns OpenAI-style chat completion
        txt = resp.choices[0].message.content
        if isinstance(txt, list):
            # defensive: join parts if returned as structured content
            txt = "".join(part.get("text", "") for part in txt if isinstance(part, dict))
        txt = txt.strip()

        # Strip code fences if present
        txt = txt.replace("```json", "").replace("```", "").strip()

        # Try to coerce into a single JSON object
        if "{" in txt and "}" in txt:
            txt = txt[txt.find("{"): txt.rfind("}") + 1]

        parsed = json.loads(txt)
        bill_items = parsed.get("bill_items", [])
        logger.info(f"Page {page_num} → {len(bill_items)} items")

        return parsed

    except Exception as e:
        logger.error(f"Page {page_num} failed → {e}")
        return {"page_type": "Unknown", "bill_items": []}

# -------------------------------
# API Endpoint
# -------------------------------
@app.post("/extract-bill-data")
async def api(req: RequestModel):
    logger.info(f"Judge triggered API with: {req.document}")

    # Download the document
    try:
        r = requests.get(req.document, timeout=30)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to download document: {e}")
        raise HTTPException(status_code=400, detail=f"Could not download document: {e}")

    # ✅ Robust PDF detection (handles ?query= etc.)
    try:
        if is_pdf_url(req.document, r):
            images = pdf_to_images(r.content)
        else:
            # Treat as image (PNG/JPEG/etc.)
            images = [Image.open(io.BytesIO(r.content))]
    except Exception as e:
        logger.error(f"Failed to parse document as PDF/image: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid or unsupported file format: {e}")

    pages: list[PageData] = []
    total = 0

    for i, img in enumerate(images, 1):
        res = await extract_page(img, i)
        bill_items_raw = res.get("bill_items", []) or []
        # Safely build BillItem objects
        items: list[BillItem] = []
        for x in bill_items_raw:
            try:
                items.append(BillItem(**x))
            except Exception as e:
                logger.error(f"Failed to parse item on page {i}: {e}")

        pages.append(
            PageData(
                page_no=str(i),
                page_type=res.get("page_type", "Unknown"),
                bill_items=items,
            )
        )
        total += len(items)

    logger.info(f"FINISHED → Total extracted items: {total}")

    data = Data(pagewise_line_items=pages, total_item_count=total)
    return ResponseModel(data=data)

# ===================================================
# START SERVER — SAFE (NO ^C) + ACCESS LOGS ENABLED
# ===================================================
def start_server():
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        access_log=True,
        loop="asyncio",
        reload=False,
        lifespan="on",
        workers=1,
        timeout_keep_alive=120,
        use_colors=True,
    )

threading.Thread(target=start_server, daemon=True).start()

time.sleep(3)

# ---- ngrok ----
tunnel = ngrok.connect(8000, bind_tls=True)
url = tunnel.public_url

print("\n" + "=" * 80)
print("API READY WITH CONTINUOUS LOGS")
print("=" * 80)
print("Swagger:", url + "/docs")
print("Endpoint:", url + "/extract-bill-data")
print("Health:", url + "/health")
print("=" * 80)
