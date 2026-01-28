import os
import re
import io
import sqlite3
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import qrcode

# ============================================================
# CONFIG
# ============================================================

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PROJECT_ROOT)  # /app -> project root

DB_PATH = os.path.join(PROJECT_ROOT, "db", "paylink.db")
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "templates")
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
APP_ENV = os.getenv("APP_ENV", "dev").lower().strip()
ADMIN_KEY = os.getenv("ADMIN_KEY", "")

app = FastAPI(title="PayLink Connect (Flow A)")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def require_admin(request: Request) -> None:
    key = request.query_params.get("key", "")
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


# ============================================================
# QR (internal PNG)
# ============================================================

@app.get("/qr/{slug}.png")
def qr_png(slug: str):
    slug = (slug or "").strip().lower()
    if not slug:
        raise HTTPException(404, "Not found")
    url = f"{BASE_URL}/p/{slug}"

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


# ============================================================
# DB helpers
# ============================================================

def db_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Idempotent init + safe SQLite migrations."""
    with db_conn() as conn:
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

              -- admin fields
              payment_link TEXT,
              photo_url TEXT,
              maps_url TEXT,
              headline TEXT,
              whatsapp_message TEXT,

              created_at TEXT
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_merchants_slug ON merchants(slug);")

        cols = [r["name"] for r in conn.execute("PRAGMA table_info(merchants)").fetchall()]

        def add_col(name: str):
            if name not in cols:
                conn.execute(f"ALTER TABLE merchants ADD COLUMN {name} TEXT;")

        # backward compatible migrations
        add_col("payment_link")
        add_col("photo_url")
        add_col("maps_url")
        add_col("headline")
        add_col("whatsapp_message")


init_db()


# ============================================================
# Utils
# ============================================================

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s or "merchant"


def unique_slug(base: str) -> str:
    base = slugify(base)
    with db_conn() as conn:
        row = conn.execute("SELECT 1 FROM merchants WHERE slug=?", (base,)).fetchone()
        if not row:
            return base
        i = 2
        while True:
            candidate = f"{base}-{i}"
            row = conn.execute("SELECT 1 FROM merchants WHERE slug=?", (candidate,)).fetchone()
            if not row:
                return candidate
            i += 1


def norm_whatsapp(w: str) -> str:
    w = (w or "").strip()
    w = re.sub(r"[^\d]", "", w)
    if w.startswith("00"):
        w = w[2:]
    return w


def get_merchant_by_id(mid: int):
    with db_conn() as conn:
        return conn.execute("SELECT * FROM merchants WHERE id=?", (mid,)).fetchone()


def get_merchant_by_slug(slug: str):
    with db_conn() as conn:
        return conn.execute("SELECT * FROM merchants WHERE slug=?", (slug,)).fetchone()


# ============================================================
# Pages
# ============================================================

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(
        "landing.html",
        {"request": request, "base_url": BASE_URL, "env": APP_ENV},
    )


# ----------------------------
# Flow A (client)
# ----------------------------

@app.get("/start", response_class=HTMLResponse)
def start_form(request: Request):
    return templates.TemplateResponse(
        "start.html",
        {"request": request, "base_url": BASE_URL},
    )


@app.post("/start")
def start_create(
    business_name: str = Form(...),
    contact_name: str = Form(""),
    whatsapp: str = Form(...),
    country: str = Form("NI"),
    currency: str = Form("USD"),
    amount1: int = Form(25),
    amount2: int = Form(49),
    amount3: int = Form(79),
):
    business_name = (business_name or "").strip()
    if not business_name:
        raise HTTPException(400, "business_name est requis")

    slug = unique_slug(business_name)
    whatsapp_norm = norm_whatsapp(whatsapp)

    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO merchants(
              slug,business_name,contact_name,whatsapp,country,currency,
              amount1,amount2,amount3,created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                slug,
                business_name,
                (contact_name or "").strip(),
                whatsapp_norm,
                (country or "NI").strip().upper()[:2],
                (currency or "USD").strip().upper()[:3],
                int(amount1),
                int(amount2),
                int(amount3),
                datetime.utcnow().isoformat(),
            ),
        )
        merchant_id = int(cur.lastrowid)

    # client goes to public page
    return RedirectResponse(url=f"/p/{slug}", status_code=303)


# ----------------------------
# Admin (protected)
# ----------------------------

@app.get("/connect/{merchant_id:int}", response_class=HTMLResponse)
def connect_page(request: Request, merchant_id: int):
    require_admin(request)
    merchant = get_merchant_by_id(merchant_id)
    if not merchant:
        raise HTTPException(404, "Merchant introuvable")
    return templates.TemplateResponse(
        "connect.html",
        {"request": request, "merchant": dict(merchant), "base_url": BASE_URL},
    )


@app.post("/connect/{merchant_id:int}/save")
def connect_save(
    request: Request,
    merchant_id: int,
    payment_link: str = Form(""),
    photo_url: str = Form(""),
    maps_url: str = Form(""),
    headline: str = Form(""),
    whatsapp_message: str = Form(""),
):
    require_admin(request)
    merchant = get_merchant_by_id(merchant_id)
    if not merchant:
        raise HTTPException(404, "Merchant introuvable")

    payment_link = (payment_link or "").strip()
    photo_url = (photo_url or "").strip()
    maps_url = (maps_url or "").strip()
    headline = (headline or "").strip()
    whatsapp_message = (whatsapp_message or "").strip()

    if payment_link and not payment_link.startswith("https://"):
        raise HTTPException(400, "Le lien doit commencer par https://")

    with db_conn() as conn:
        conn.execute(
            """
            UPDATE merchants
            SET payment_link=?, photo_url=?, maps_url=?, headline=?, whatsapp_message=?
            WHERE id=?
            """,
            (payment_link, photo_url, maps_url, headline, whatsapp_message, merchant_id),
        )

    key = request.query_params.get("key", "")
    return RedirectResponse(url=f"/connect/{merchant_id}?key={key}", status_code=303)


# ----------------------------
# Public page
# ----------------------------

@app.get("/p/{slug}", response_class=HTMLResponse)
def paypage(request: Request, slug: str):
    merchant = get_merchant_by_slug((slug or "").lower().strip())
    if not merchant:
        raise HTTPException(404, "Page introuvable")
    return templates.TemplateResponse(
        "paypage.html",
        {"request": request, "merchant": dict(merchant), "base_url": BASE_URL},
    )


# ============================================================
# Legal pages
# ============================================================

@app.get("/mentions-legales", response_class=HTMLResponse)
def mentions_legales(request: Request):
    return templates.TemplateResponse("mentions-legales.html", {"request": request, "base_url": BASE_URL})


@app.get("/cgv-paylink", response_class=HTMLResponse)
def cgv(request: Request):
    return templates.TemplateResponse("cgv-paylink.html", {"request": request, "base_url": BASE_URL})


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse("contact.html", {"request": request, "base_url": BASE_URL})


@app.get("/success", response_class=HTMLResponse)
def success(request: Request):
    return templates.TemplateResponse("success.html", {"request": request, "base_url": BASE_URL})
