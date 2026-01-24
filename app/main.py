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

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))  # .../paylink_connect
DB_PATH = os.path.join(PROJECT_ROOT, "db", "paylink.db")

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()  # optional

# 🔥 B1 subscription (Stripe Payment Link)
PAYLINK_SUBSCRIBE_URL = os.getenv("PAYLINK_SUBSCRIBE_URL", "").strip()

# Use your Render env vars (you already added them)
STRIPE_CONNECT_RETURN_URL = os.getenv(
    "STRIPE_CONNECT_RETURN_URL",
    f"{BASE_URL}/stripe/connect/return"
).rstrip("/")

STRIPE_CONNECT_REFRESH_URL = os.getenv(
    "STRIPE_CONNECT_REFRESH_URL",
    f"{BASE_URL}/stripe/connect/refresh"
).rstrip("/")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def require_stripe_config():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY missing (env var)")


# ============================================================
# DB
# ============================================================

def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
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


def unique_slug(base: str) -> str:
    base = slugify(base)
    with db() as conn:
        slug = base
        i = 2
        while conn.execute("SELECT 1 FROM merchants WHERE slug=?", (slug,)).fetchone():
            slug = f"{base}-{i}"
            i += 1
        return slug


def normalize_whatsapp(raw: str) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    return digits


def get_merchant_by_id(merchant_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()


def get_merchant_by_slug(slug: str):
    with db() as conn:
        return conn.execute("SELECT * FROM merchants WHERE slug=?", (slug,)).fetchone()


def seed_demo_carlos() -> None:
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM merchants WHERE slug='carlos'").fetchone()
        if exists:
            return

        conn.execute(
            """
            INSERT INTO merchants(
              slug, business_name, contact_name, whatsapp, country, currency,
              amount1, amount2, amount3, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "carlos",
                "Carlos Martinez · Reparación express de teléfonos",
                "Carlos",
                "50588887777",  # fake Nicaragua
                "NI",
                "USD",
                25,
                49,
                79,
                datetime.utcnow().isoformat(),
            ),
        )


# ============================================================
# APP / STATIC / TEMPLATES
# ============================================================

app = FastAPI(title="PayLink Connect (Maavnica)")

templates = Jinja2Templates(directory=os.path.join(PROJECT_ROOT, "templates"))

app.mount("/static", StaticFiles(directory=os.path.join(PROJECT_ROOT, "static")), name="static")
app.mount("/legacy", StaticFiles(directory=os.path.join(PROJECT_ROOT, "legacy")), name="legacy")


@app.on_event("startup")
def startup():
    init_db()
    seed_demo_carlos()


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
            "demo_url": f"{BASE_URL}/demo/carlos",
            # 🔥 B1 subscription CTA
            "subscribe_url": PAYLINK_SUBSCRIBE_URL,
        },
    )


@app.get("/success", response_class=HTMLResponse)
def subscribe_success(request: Request):
    """
    After the user pays the subscription (Stripe Payment Link),
    Stripe redirects here. Next step: create PayLink + connect Stripe.
    """
    return templates.TemplateResponse(
        "success.html",
        {
            "request": request,
            "base_url": BASE_URL,
        },
    )


@app.get("/start", response_class=HTMLResponse)
def start_form(request: Request):
    return templates.TemplateResponse("start.html", {"request": request, "base_url": BASE_URL})


@app.post("/start")
def start_submit(
    business_name: str = Form(...),
    contact_name: str = Form(""),
    whatsapp: str = Form(...),
    country: str = Form("NI"),
    currency: str = Form("USD"),
    amount1: int = Form(25),
    amount2: int = Form(49),
    amount3: int = Form(79),
):
    slug = unique_slug(business_name)

    whatsapp_norm = normalize_whatsapp(whatsapp)
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

    # page that shows "Connect Stripe" button
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

    # ✅ Create Express account (allowed via API)
    if not acct_id:
        acct = stripe.Account.create(
            type="express",
            country=(m["country"] or None),
            business_profile={
                "name": m["business_name"],
                "url": f"{BASE_URL}/p/{m['slug']}",
            },
            # capabilities help avoid “limited” situations
            capabilities={
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
        )
        acct_id = acct["id"]
        with db() as conn:
            conn.execute("UPDATE merchants SET stripe_account_id=? WHERE id=?", (acct_id, merchant_id))

    # Use env URLs and attach merchant_id so return/refresh knows who it is
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
    # just restart the onboarding flow
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
# PAY PAGE
# ============================================================

@app.get("/p/{slug}", response_class=HTMLResponse)
def pay_page(request: Request, slug: str, success: Optional[str] = None):
    m = get_merchant_by_slug(slug)
    if not m:
        raise HTTPException(404, "Page not found")

    data = dict(m)
    wa = data.get("whatsapp", "")

    wa_msg = f"Hola, quiero pagar por PayLink ({data.get('business_name','')}). ¿Qué monto me indicás?"
    whatsapp_url = f"https://wa.me/{wa}?text=" + quote_plus(wa_msg)

    stripe_connected = bool(data.get("stripe_account_id"))
    connect_url = f"{BASE_URL}/stripe/connect/start?slug={data.get('slug')}"

    return templates.TemplateResponse(
        "paypage.html",
        {
            "request": request,
            "merchant": data,
            "base_url": BASE_URL,
            "whatsapp_url": whatsapp_url,
            "success": success,
            # 🔥 used by template to show connect button
            "stripe_connected": stripe_connected,
            "connect_url": connect_url,
        },
    )


# ============================================================
# LEGAL PAGES
# ============================================================

@app.get("/mentions-legales", response_class=HTMLResponse)
def mentions_legales(request: Request):
    return templates.TemplateResponse("mentions-legales.html", {"request": request})


@app.get("/cgv-paylink", response_class=HTMLResponse)
def cgv_paylink(request: Request):
    return templates.TemplateResponse("cgv-paylink.html", {"request": request})


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse("contact.html", {"request": request})


# ============================================================
# API: CHECKOUT SESSION
# ============================================================

class CheckoutRequest(BaseModel):
    amount: int = Field(..., ge=1)
    currency: str = Field("usd", min_length=3, max_length=3)
    slug: str = Field(..., min_length=1)
    title: str = Field("Pago (PayLink)")
    description: str = Field("Cobro vía PayLink")


def create_session_for_merchant(m: sqlite3.Row, amount_major: int, currency: str, title: str, description: str):
    require_stripe_config()

    acct_id = m["stripe_account_id"]
    if not acct_id:
        raise HTTPException(400, "Merchant has not connected Stripe yet")

    currency = (currency or (m["currency"] or "USD")).lower()
    unit_amount = int(amount_major) * 100

    return stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": currency,
                    "product_data": {"name": title, "description": description},
                    "unit_amount": unit_amount,
                },
                "quantity": 1,
            }
        ],
        success_url=f"{BASE_URL}/p/{m['slug']}?success=1",
        cancel_url=f"{BASE_URL}/p/{m['slug']}?success=0",
        stripe_account=acct_id,
    )


@app.post("/api/create-checkout-session")
async def create_checkout_session(request: Request):
    require_stripe_config()

    content_type = (request.headers.get("content-type") or "").lower()

    if "application/json" in content_type:
        try:
            payload: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        data = CheckoutRequest(**payload)
        slug = data.slug.strip()
        amount = int(data.amount)
        currency = (data.currency or "usd").strip().lower()
        title = (data.title or "Pago (PayLink)").strip()
        description = (data.description or "Cobro vía PayLink").strip()
    else:
        form = await request.form()
        slug = str(form.get("slug", "")).strip()
        amount = int(form.get("amount", 0) or 0)
        currency = str(form.get("currency", "usd")).strip().lower()
        title = str(form.get("title", "Pago (PayLink)")).strip()
        description = str(form.get("description", "Cobro vía PayLink")).strip()

        if not slug:
            raise HTTPException(400, "Missing slug")
        if amount <= 0:
            raise HTTPException(400, "Invalid amount")

    m = get_merchant_by_slug(slug)
    if not m:
        raise HTTPException(404, "Merchant not found")

    session = create_session_for_merchant(m, amount, currency, title, description)
    return JSONResponse({"url": session["url"]})


# ============================================================
# LEGACY ENTRYPOINT
# ============================================================

@app.get("/paylink")
@app.get("/paylink/")
def legacy_redirect():
    return RedirectResponse(url="/legacy/index.html", status_code=302)




