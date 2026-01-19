import os
import re
import sqlite3
from datetime import datetime
from typing import Optional

from urllib.parse import quote_plus

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import stripe

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "db", "paylink.db")

# ---- Config via env ----
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_CLIENT_ID = os.getenv("STRIPE_CLIENT_ID", "").strip()  # starts with ca_
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS merchants (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slug TEXT UNIQUE NOT NULL,
              business_name TEXT NOT NULL,
              contact_name TEXT,
              whatsapp TEXT NOT NULL,
              country TEXT,
              currency TEXT,
              amount1 INTEGER,
              amount2 INTEGER,
              amount3 INTEGER,
              stripe_account_id TEXT,
              stripe_onboarding_complete INTEGER DEFAULT 0,
              created_at TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_merchants_slug ON merchants(slug);")


def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-") or "cliente"


def unique_slug(base_slug: str) -> str:
    base_slug = slugify(base_slug)
    with db() as conn:
        slug = base_slug
        i = 2
        while conn.execute("SELECT 1 FROM merchants WHERE slug=?", (slug,)).fetchone():
            slug = f"{base_slug}-{i}"
            i += 1
        return slug


def get_merchant_by_id(merchant_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
    return row


def get_merchant_by_slug(slug: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM merchants WHERE slug=?", (slug,)).fetchone()
    return row


def require_stripe_config():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(500, "STRIPE_SECRET_KEY missing")


app = FastAPI(title="Maavnica PayLink (Stripe Connect)")

# templates
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# static + legacy pages
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
app.mount("/legacy", StaticFiles(directory=os.path.join(BASE_DIR, "legacy")), name="legacy")


@app.on_event("startup")
def _startup():
    init_db()
    # seed a demo merchant if none exists
    with db() as conn:
        if not conn.execute("SELECT 1 FROM merchants WHERE slug='demo-carlos'").fetchone():
            conn.execute(
                """
                INSERT INTO merchants(slug,business_name,contact_name,whatsapp,country,currency,amount1,amount2,amount3,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "demo-carlos",
                    "Carlos Taxi",
                    "Carlos",
                    "50581234567",
                    "NI",
                    "USD",
                    10,
                    20,
                    50,
                    datetime.utcnow().isoformat(),
                ),
            )


# ---------------- Pages ----------------

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(
        "landing.html",
        {
            "request": request,
            "base_url": BASE_URL,
            "demo_url": f"{BASE_URL}/p/demo-carlos",
        },
    )


@app.get("/start", response_class=HTMLResponse)
def start_form(request: Request):
    return templates.TemplateResponse(
        "start.html",
        {
            "request": request,
            "base_url": BASE_URL,
        },
    )


@app.post("/start")
def start_submit(
    business_name: str = Form(...),
    contact_name: str = Form(""),
    whatsapp: str = Form(...),
    country: str = Form(""),
    currency: str = Form("USD"),
    amount1: int = Form(10),
    amount2: int = Form(20),
    amount3: int = Form(50),
):
    slug = unique_slug(business_name)

    # normalize whatsapp to digits only
    whatsapp_norm = re.sub(r"\D+", "", whatsapp)
    if len(whatsapp_norm) < 8:
        raise HTTPException(400, "WhatsApp number seems invalid")

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO merchants(slug,business_name,contact_name,whatsapp,country,currency,amount1,amount2,amount3,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                slug,
                business_name.strip(),
                contact_name.strip(),
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


@app.get("/connect/{merchant_id}", response_class=HTMLResponse)
def connect_page(request: Request, merchant_id: int):
    m = get_merchant_by_id(merchant_id)
    if not m:
        raise HTTPException(404, "Merchant not found")

    return templates.TemplateResponse(
        "connect.html",
        {
            "request": request,
            "merchant": dict(m),
            "stripe_client_id": STRIPE_CLIENT_ID,
            "base_url": BASE_URL,
        },
    )


@app.get("/stripe/connect/start")
def stripe_connect_start(merchant_id: int):
    require_stripe_config()

    m = get_merchant_by_id(merchant_id)
    if not m:
        raise HTTPException(404, "Merchant not found")

    # Create (or reuse) a connected account
    acct_id = m["stripe_account_id"]
    if not acct_id:
        # Standard account (recommended). Stripe handles KYC.
        acct = stripe.Account.create(
            type="standard",
            country=(m["country"] or None),
            business_profile={
                "name": m["business_name"],
                "url": f"{BASE_URL}/p/{m['slug']}",
            },
        )
        acct_id = acct["id"]
        with db() as conn:
            conn.execute(
                "UPDATE merchants SET stripe_account_id=? WHERE id=?",
                (acct_id, merchant_id),
            )

    refresh_url = f"{BASE_URL}/connect/{merchant_id}?refresh=1"
    return_url = f"{BASE_URL}/stripe/connect/return?merchant_id={merchant_id}"

    link = stripe.AccountLink.create(
        account=acct_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )

    return RedirectResponse(url=link["url"], status_code=303)


@app.get("/stripe/connect/return", response_class=HTMLResponse)
def stripe_connect_return(request: Request, merchant_id: int):
    require_stripe_config()

    m = get_merchant_by_id(merchant_id)
    if not m:
        raise HTTPException(404, "Merchant not found")

    acct_id = m["stripe_account_id"]
    if not acct_id:
        raise HTTPException(400, "No connected account")

    # Check whether onboarding is complete
    acct = stripe.Account.retrieve(acct_id)
    complete = bool(acct.get("charges_enabled")) and bool(acct.get("payouts_enabled"))

    with db() as conn:
        conn.execute(
            "UPDATE merchants SET stripe_onboarding_complete=? WHERE id=?",
            (1 if complete else 0, merchant_id),
        )

    return templates.TemplateResponse(
        "connected.html",
        {
            "request": request,
            "merchant": dict(m),
            "complete": complete,
            "paypage_url": f"{BASE_URL}/p/{m['slug']}",
        },
    )


@app.get("/p/{slug}", response_class=HTMLResponse)
def pay_page(request: Request, slug: str, success: Optional[str] = None):
    m = get_merchant_by_slug(slug)
    if not m:
        raise HTTPException(404, "Page not found")

    data = dict(m)
    wa = data.get("whatsapp", "")
    wa_msg = f"Hola, quiero pagar por PayLink ({data.get('business_name','')})."

    return templates.TemplateResponse(
        "paypage.html",
        {
            "request": request,
            "merchant": data,
            "base_url": BASE_URL,
            "whatsapp_url": f"https://wa.me/{wa}?text=" + quote_plus(wa_msg),
            "success": success,
        },
    )


# ---------------- API ----------------

@app.post("/api/create-checkout-session")
def create_checkout_session(slug: str = Form(...), amount: int = Form(...)):
    """Creates a Stripe Checkout Session on the connected account.

    amount is in major currency units (e.g. 10 USD). This V1 is intentionally simple.
    """
    require_stripe_config()

    m = get_merchant_by_slug(slug)
    if not m:
        raise HTTPException(404, "Merchant not found")

    acct_id = m["stripe_account_id"]
    if not acct_id:
        raise HTTPException(400, "Merchant has not connected Stripe")

    currency = (m["currency"] or "USD").lower()

    # Minimal product. You can later replace with real products or metadata.
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": currency,
                    "product_data": {"name": m["business_name"]},
                    "unit_amount": int(amount) * 100,
                },
                "quantity": 1,
            }
        ],
        success_url=f"{BASE_URL}/p/{m['slug']}?success=1",
        cancel_url=f"{BASE_URL}/p/{m['slug']}?success=0",
        # This makes the API call on the connected account (direct charge)
        stripe_account=acct_id,
    )

    return JSONResponse({"url": session["url"]})


# Convenience: redirect old static site paths to legacy
@app.get("/paylink")
@app.get("/paylink/")
def _old_paylink_redirect():
    return RedirectResponse(url="/legacy/index.html", status_code=302)

