import os
import re
import io
import html
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
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
UPLOADS_DIR = os.path.join(STATIC_DIR, "uploads")

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
APP_ENV = os.getenv("APP_ENV", "dev").lower().strip()
ADMIN_KEY = os.getenv("ADMIN_KEY", "")

app = FastAPI(title="PayLink Connect (Flow A)")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

if os.path.isdir(STATIC_DIR):
    os.makedirs(UPLOADS_DIR, exist_ok=True)
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

        def add_col(name: str, col_def: str = "TEXT"):
            if name not in cols:
                conn.execute(f"ALTER TABLE merchants ADD COLUMN {name} {col_def};")

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


def safe_filename(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9\.\-_]+", "-", name)
    name = name.strip("-")
    return name or "file"


def get_merchant_by_id(mid: int):
    with db_conn() as conn:
        return conn.execute("SELECT * FROM merchants WHERE id=?", (mid,)).fetchone()


def get_merchant_by_slug(slug: str):
    with db_conn() as conn:
        return conn.execute("SELECT * FROM merchants WHERE slug=?", (slug,)).fetchone()


def list_merchants(limit: int = 500):
    with db_conn() as conn:
        return conn.execute(
            "SELECT * FROM merchants ORDER BY datetime(created_at) DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()


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
        conn.execute(
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

    # client goes to public page
    return RedirectResponse(url=f"/p/{slug}", status_code=303)


# ----------------------------
# Admin: LIST (protected)
# ----------------------------

@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    require_admin(request)
    key = request.query_params.get("key", "")
    rows = list_merchants()

    # Page HTML simple (pas besoin de template)
    items_html = []
    for r in rows:
        m = dict(r)
        mid = m.get("id")
        slug = m.get("slug") or ""
        business = m.get("business_name") or ""
        created = m.get("created_at") or ""
        photo = m.get("photo_url") or ""
        pay = m.get("payment_link") or ""

        items_html.append(f"""
          <tr>
            <td style="padding:10px;border-bottom:1px solid #2a2f44;">{mid}</td>
            <td style="padding:10px;border-bottom:1px solid #2a2f44;"><code>{html.escape(slug)}</code></td>
            <td style="padding:10px;border-bottom:1px solid #2a2f44;">{html.escape(business)}</td>
            <td style="padding:10px;border-bottom:1px solid #2a2f44;">{html.escape(created[:19].replace("T"," "))}</td>
            <td style="padding:10px;border-bottom:1px solid #2a2f44;">
              {"✅" if pay else "—"}
            </td>
            <td style="padding:10px;border-bottom:1px solid #2a2f44;">
              {"✅" if photo else "—"}
            </td>
            <td style="padding:10px;border-bottom:1px solid #2a2f44;white-space:nowrap;">
              <a href="/p/{html.escape(slug)}" target="_blank">Public</a>
              &nbsp;|&nbsp;
              <a href="/connect/{mid}?key={html.escape(key)}" target="_blank">Connect</a>
            </td>
          </tr>
        """)

    page = f"""
    <!doctype html>
    <html lang="fr">
    <head>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width,initial-scale=1"/>
      <title>PayLink Admin</title>
      <style>
        body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;margin:0;background:#0b1020;color:#e8ecff}}
        .wrap{{max-width:1100px;margin:0 auto;padding:22px}}
        .card{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:16px;padding:16px}}
        table{{width:100%;border-collapse:collapse}}
        th{{text-align:left;font-size:12px;opacity:.8;padding:10px;border-bottom:1px solid #2a2f44}}
        a{{color:#9db4ff;text-decoration:none}}
        a:hover{{text-decoration:underline}}
        .top{{display:flex;gap:10px;justify-content:space-between;align-items:center;flex-wrap:wrap;margin-bottom:12px}}
        .btn{{display:inline-block;padding:10px 12px;border-radius:12px;border:1px solid rgba(255,255,255,.18);background:rgba(255,255,255,.08);color:#fff;text-decoration:none}}
      </style>
    </head>
    <body>
      <div class="wrap">
        <div class="top">
          <div>
            <h2 style="margin:0 0 6px;">Admin — toutes les créations</h2>
            <div style="opacity:.8;font-size:13px;">URL client : <code>/start</code> • Pages publiques : <code>/p/&lt;slug&gt;</code></div>
          </div>
          <div>
            <a class="btn" href="/start" target="_blank">Ouvrir /start</a>
          </div>
        </div>

        <div class="card">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Slug</th>
                <th>Business</th>
                <th>Créé</th>
                <th>Stripe</th>
                <th>Photo</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {''.join(items_html) if items_html else '<tr><td colspan="7" style="padding:12px;">Aucune création.</td></tr>'}
            </tbody>
          </table>
        </div>

        <div style="opacity:.75;font-size:12px;margin-top:10px;">
          Astuce : sauvegarde ce lien admin dans tes favoris → <code>/admin?key=TA_CLE</code>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(page)


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
async def connect_save(
    request: Request,
    merchant_id: int,
    payment_link: str = Form(""),
    photo_url: str = Form(""),
    maps_url: str = Form(""),
    headline: str = Form(""),
    whatsapp_message: str = Form(""),
    photo_file: UploadFile = File(None),  # upload optionnel
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
        raise HTTPException(400, "Le lien Stripe doit commencer par https://")

    # Upload photo (optionnel) => stocké en /static/uploads/xxx.ext
    if photo_file and photo_file.filename:
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        filename = safe_filename(photo_file.filename)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            raise HTTPException(400, "Photo invalide. Formats acceptés: JPG, PNG, WEBP.")

        # nom stable: slug + timestamp
        slug = (merchant["slug"] if isinstance(merchant, sqlite3.Row) else dict(merchant).get("slug")) or "merchant"
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        final_name = f"{safe_filename(slug)}-{ts}{ext}"
        out_path = os.path.join(UPLOADS_DIR, final_name)

        content = await photo_file.read()
        if not content:
            raise HTTPException(400, "Fichier photo vide.")
        if len(content) > 6 * 1024 * 1024:
            raise HTTPException(400, "Photo trop lourde (max 6 Mo).")

        with open(out_path, "wb") as f:
            f.write(content)

        # On force photo_url vers le fichier uploadé
        photo_url = f"/static/uploads/{final_name}"

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

