import os
import re
import sqlite3
from datetime import datetime
from typing import Optional, Any, Dict
from urllib.parse import quote_plus

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import stripe

# ============================================================
# CONFIG
# ============================================================

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PROJECT_ROOT)  # go up from /app to project root

DB_PATH = os.path.join(PROJECT_ROOT, "db", "paylink.db")
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "templates")
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
APP_ENV = os.getenv("APP_ENV", "dev")

SECRET_KEY = os.getenv("SECRET_KEY", "dev_secret_change_me")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_CONNECT_RETURN_URL = os.getenv("STRIPE_CONNECT_RETURN_URL", f"{BASE_URL}/stripe/connect/return")
STRIPE_CONNECT_REFRESH_URL = os.getenv("STRIPE_CONNECT_REFRESH_URL", f"{BASE_URL}/stripe/connect/refresh")

PAYLINK_SUBSCRIBE_URL = os.getenv("PAYLINK_SUBSCRIBE_URL", "").strip()

# ============================================================
# APP
# ============================================================

app = FastAPI(title="PayLink Connect")

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

# ============================================================
# DB helpers
# ============================================================

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merchants (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slug TEXT UNIQUE NOT NULL,
              business_name TEXT NOT NULL,
              contact_name TEXT,
              whatsapp TEXT,
              country TEXT,
              currency TEXT,
              amount1 INTEGER DEFAULT 25,
              amount2 INTEGER DEFAULT 49,
              amount3 INTEGER DEFAULT 79,
              stripe_account_id TEXT,
              stripe_onboarding_complete INTEGER DEFAULT 0,
              created_at TEXT
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_merchants_slug ON merchants(slug);")

init_db()

def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "-", s, flags=re.UNICODE)
    s = re.sub(r"^-+|-+$", "", s)
    return s or "merchant"

def unique_slug(base: str) -> str:
    base = slugify(base)
    slug = base
    i = 2
    with db() as conn:
        while conn.execute("SELECT 1 FROM merchants WHERE slug=?", (slug,)).fetchone():
            slug = f"{base}-{i}"
            i += 1
    return slug

def get_merchant_by_id(merchant_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()

def get_merchant_by_slug(slug: str):
    with db() as conn:
        return conn.execute("SELECT * FROM merchants WHERE slug=?", (slug,)).fetchone()

def norm_whatsapp(w: str) -> str:
    w = (w or "").strip()
    w = re.sub(r"[^\d+]", "", w)
    if w.startswith("00"):
        w = "+" + w[2:]
    if not w.startswith("+") and w.isdigit():
        # keep digits; some LATAM users type without +
        pass
    return w

def require_stripe_config():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(500, "Stripe not configured (STRIPE_SECRET_KEY missing)")
    stripe.api_key = STRIPE_SECRET_KEY

# ============================================================
# PAGES
# ============================================================

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(
        "landing.html",
        {
            "request": request,
            "base_url": BASE_URL,
            "subscribe_url": PAYLINK_SUBSCRIBE_URL,
        },
    )

@app.get("/start", response_class=HTMLResponse)
def start_form(request: Request):
    """Form to create a PayLink page (merchant)."""
    return templates.TemplateResponse(
        "start.html",
        {
            "request": request,
            "base_url": BASE_URL,
        },
    )


@app.get("/mentions-legales", response_class=HTMLResponse)
def mentions_legales(request: Request):
    return templates.TemplateResponse("mentions-legales.html", {"request": request, "base_url": BASE_URL})

@app.get("/cgv", response_class=HTMLResponse)
def cgv(request: Request):
    return templates.TemplateResponse("cgv-paylink.html", {"request": request, "base_url": BASE_URL})

@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse("contact.html", {"request": request, "base_url": BASE_URL})


@app.post("/start")
def start_create(
    business_name: str = Form(...),
    contact_name: str = Form(""),
    whatsapp: str = Form(""),
    country: str = Form("NI"),
    currency: str = Form("USD"),
    amount1: int = Form(25),
    amount2: int = Form(49),
    amount3: int = Form(79),
):
    business_name = (business_name or "").strip()
    if len(business_name) < 2:
        raise HTTPException(400, "business_name is required")

    slug = unique_slug(business_name)
    whatsapp_norm = norm_whatsapp(whatsapp)

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO merchants(slug,business_name,contact_name,whatsapp,country,currency,amount1,amount2,amount3,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                slug,
                business_name.strip(),
                (contact_name or "").strip(),
                whatsapp_norm,
                (country or "").strip().upper()[:2],
                (currency or "USD").strip().upper()[:3],
                int(amount1),
                int(amount2),
                int(amount3),
                datetime.utcnow().isoformat(),
            ),
        )
        merchant_id = cur.lastrowid

    return RedirectResponse(url=f"/connect/{merchant_id}", status_code=303)

# --- IMPORTANT: typed int route so it doesn't conflict with /connect/{slug}
@app.get("/connect/{merchant_id:int}", response_class=HTMLResponse)
def connect_page(request: Request, merchant_id: int):
    m = get_merchant_by_id(merchant_id)
    if not m:
        raise HTTPException(404, "Merchant not found")

    return templates.TemplateResponse(
        "connect.html",
        {
            "request": request,
            "merchant": dict(m),
            "base_url": BASE_URL,
        },
    )

# ✅ NEW: connect page by slug (stable in prod)
@app.get("/connect/{slug}", response_class=HTMLResponse)
def connect_page_by_slug(request: Request, slug: str):
    m = get_merchant_by_slug(slug)
    if not m:
        raise HTTPException(404, "Merchant not found")

    return templates.TemplateResponse(
        "connect.html",
        {
            "request": request,
            "merchant": dict(m),
            "base_url": BASE_URL,
        },
    )

# ============================================================
# DEMO
# ============================================================

@app.get("/demo/carlos")
def demo_carlos():
    return RedirectResponse(url="/p/carlos", status_code=302)

# ============================================================
# STRIPE CONNECT (EXPRESS via Account Links) ✅
# ============================================================

@app.get("/stripe/connect/start")
def stripe_connect_start(merchant_id: Optional[int] = None, slug: Optional[str] = None):
    """
    Supports:
      - /stripe/connect/start?merchant_id=123
      - /stripe/connect/start?slug=carlos
    """
    require_stripe_config()

    m = None
    if merchant_id is not None:
        m = get_merchant_by_id(int(merchant_id))
    elif slug:
        m = get_merchant_by_slug(slug.strip())
    else:
        raise HTTPException(400, "Missing merchant_id or slug")

    if not m:
        raise HTTPException(404, "Merchant not found")

    merchant_id = int(m["id"])
    acct_id = m["stripe_account_id"]

    if not acct_id:
        acct = stripe.Account.create(
            type="express",
            country=(m["country"] or None),
            business_profile={
                "name": m["business_name"],
                "url": f"{BASE_URL}/p/{m['slug']}",
            },
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
        )
        acct_id = acct["id"]
        with db() as conn:
            conn.execute("UPDATE merchants SET stripe_account_id=? WHERE id=?", (acct_id, merchant_id))

    refresh_url = f"{STRIPE_CONNECT_REFRESH_URL}?merchant_id={merchant_id}"
    return_url = f"{STRIPE_CONNECT_RETURN_URL}?merchant_id={merchant_id}"

    link = stripe.AccountLink.create(
        account=acct_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )
    return RedirectResponse(url=link["url"], status_code=303)

@app.get("/stripe/connect/refresh")
def stripe_connect_refresh(merchant_id: int):
    return RedirectResponse(url=f"/stripe/connect/start?merchant_id={merchant_id}", status_code=303)

@app.get("/stripe/connect/return", response_class=HTMLResponse)
def stripe_connect_return(request: Request, merchant_id: int):
    require_stripe_config()

    m = get_merchant_by_id(merchant_id)
    if not m:
        raise HTTPException(404, "Merchant not found")

    acct_id = m["stripe_account_id"]
    if not acct_id:
        raise HTTPException(400, "No connected account")

    acct = stripe.Account.retrieve(acct_id)
    complete = bool(acct.get("charges_enabled")) and bool(acct.get("payouts_enabled"))

    with db() as conn:
        conn.execute(
            "UPDATE merchants SET stripe_onboarding_complete=? WHERE id=?",
            (1 if complete else 0, merchant_id),
        )

    m2 = get_merchant_by_id(merchant_id)

    return templates.TemplateResponse(
        "connected.html",
        {
            "request": request,
            "merchant": dict(m2),
            "complete": complete,
            "paypage_url": f"{BASE_URL}/p/{m2['slug']}",
        },
    )

# ============================================================
# PAYPAGE
# ============================================================

@app.get("/p/{slug}", response_class=HTMLResponse)
def paypage(request: Request, slug: str):
    m = get_merchant_by_slug(slug)
    if not m:
        raise HTTPException(404, "Not Found")

    data = dict(m)
    data["stripe_connected"] = bool(data.get("stripe_account_id"))
    data["onboarding_complete"] = bool(data.get("stripe_onboarding_complete"))

    return templates.TemplateResponse(
        "paypage.html",
        {
            "request": request,
            "merchant": data,
            "base_url": BASE_URL,
        },
    )

# ============================================================
# CREATE PAYMENT (Checkout Session) — placeholder kept as-is
# (Your template JS should call this route if it exists in your project.)
# ============================================================

class PayReq(BaseModel):
    slug: str = Field(...)
    amount: int = Field(..., ge=1)
    currency: str = Field(default="USD")

@app.post("/api/create-checkout")
def api_create_checkout(payload: PayReq):
    # NOTE: This is kept minimal, you can expand later.
    require_stripe_config()

    m = get_merchant_by_slug(payload.slug.strip())
    if not m:
        raise HTTPException(404, "Merchant not found")

    acct_id = m["stripe_account_id"]
    if not acct_id:
        raise HTTPException(400, "Merchant has not connected Stripe yet")

    # Simple Checkout Session on connected account
    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=f"{BASE_URL}/success?slug={quote_plus(payload.slug)}",
        cancel_url=f"{BASE_URL}/p/{quote_plus(payload.slug)}",
        line_items=[
            {
                "price_data": {
                    "currency": (payload.currency or "USD").lower(),
                    "product_data": {"name": f"Pago — {m['business_name']}"},
                    "unit_amount": int(payload.amount) * 100,
                },
                "quantity": 1,
            }
        ],
        payment_intent_data={
            "transfer_data": {"destination": acct_id},
        },
    )
    return {"url": session["url"]}

@app.get("/success", response_class=HTMLResponse)
def success_page(request: Request, slug: str = ""):
    return templates.TemplateResponse("success.html", {"request": request, "slug": slug})





