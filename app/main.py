import os
import io
import re
from datetime import datetime
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker

import qrcode


# =========================================================
# CONFIG
# =========================================================

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()
BASE_URL_ENV = os.getenv("BASE_URL", "").strip()  # optionnel (ex: https://paylink.maavnica.com)
SQLITE_DIR = os.getenv("SQLITE_DIR", "/tmp").strip() or "/tmp"  # Render-safe

# Paths (robuste sur Render / en local)
# NOTE: __file__ = .../paylink_connect/app/main.py (souvent)
# parents[1] => .../paylink_connect (racine projet) si main.py est dans /app
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"

# DB path
# - Si DATABASE_URL => Postgres/Neon
# - Sinon => SQLite dans SQLITE_DIR (par défaut /tmp sur Render)
if DATABASE_URL:
    DB_URL = DATABASE_URL
    print("🗄️ DB = Postgres/Neon (DATABASE_URL)")
    engine = create_engine(DB_URL, pool_pre_ping=True)
else:
    db_dir = Path(SQLITE_DIR)
    db_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = db_dir / "paylink.db"
    DB_URL = f"sqlite:///{sqlite_path.as_posix()}"
    print("🗄️ DB = SQLite", sqlite_path)
    engine = create_engine(DB_URL, connect_args={"check_same_thread": False})

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(Integer, primary_key=True)
    slug = Column(String, unique=True, index=True)

    business_name = Column(String)
    whatsapp = Column(String)
    currency = Column(String)
    amounts = Column(String)

    payment_link = Column(String, nullable=True)
    photo_url = Column(String, nullable=True)
    maps_url = Column(String, nullable=True)
    headline = Column(String, nullable=True)

    # ✅ utilisé par connect.html / bouton "Tester WhatsApp"
    whatsapp_message = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


app = FastAPI()

# Static/templates (chemins absolus)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.on_event("startup")
def startup():
    """
    ⚠️ create_all ne modifie pas un schéma existant (ex: ajout de colonne).
    - En SQLite : supprimer /tmp/paylink.db si tu veux recréer.
    - En Postgres : faire ALTER TABLE pour ajouter whatsapp_message.
    """
    print("🧱 Initialisation DB (create_all)")
    Base.metadata.create_all(bind=engine)


# =========================================================
# HELPERS
# =========================================================

def get_db():
    """Retourne une session SQLAlchemy."""
    return SessionLocal()


def require_admin(request: Request) -> None:
    """
    Accès admin via ?key=...
    - Refuse si ADMIN_KEY n'est pas défini (sécurité)
    """
    if not ADMIN_KEY:
        raise HTTPException(status_code=500, detail="ADMIN_KEY is not set")
    key = (request.query_params.get("key") or "").strip()
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def slugify(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:30] if s else "paylink"


def unique_slug(session, base: str) -> str:
    slug = base
    i = 2
    while session.query(Merchant).filter_by(slug=slug).first() is not None:
        slug = f"{base[:26]}-{i}"
        i += 1
    return slug


def parse_amounts(amounts: str) -> str:
    """
    On stocke en string simple.
    Formats acceptés: "25,49,79" / "25 49 79" / "25|49|79"
    """
    if not amounts:
        return ""
    cleaned = re.sub(r"[^\d,| ]", "", amounts)
    parts = re.split(r"[,| ]+", cleaned.strip())
    parts = [p for p in parts if p.isdigit()]
    return ",".join(parts[:6])


def get_base_url(request: Request) -> str:
    """
    Construit l'URL publique (utile pour QR) :
    - BASE_URL env si fourni
    - sinon request.base_url
    """
    base = BASE_URL_ENV.rstrip("/") if BASE_URL_ENV else str(request.base_url).rstrip("/")
    return base


# =========================================================
# ROUTES
# =========================================================

# ✅ Landing en home
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})


# ---------- START (CLIENT) ----------
@app.get("/start", response_class=HTMLResponse)
def start_page(request: Request):
    return templates.TemplateResponse("start.html", {"request": request})


@app.post("/start")
def start_create(
    business_name: str = Form(...),
    whatsapp: str = Form(...),
    currency: str = Form("USD"),
    amounts: str = Form(""),
):
    session = get_db()
    try:
        base = slugify(business_name)
        slug = unique_slug(session, base)

        merchant = Merchant(
            slug=slug,
            business_name=business_name.strip(),
            whatsapp=whatsapp.strip(),
            currency=(currency or "USD").strip().upper(),
            amounts=parse_amounts(amounts),
        )
        session.add(merchant)
        session.commit()
        session.refresh(merchant)

        print(f"✅ Merchant créé : {merchant.id} / {merchant.slug}")
        return RedirectResponse(f"/p/{merchant.slug}", status_code=302)
    finally:
        session.close()


# ---------- PAGE PUBLIQUE ----------
@app.get("/p/{slug}", response_class=HTMLResponse)
def public_page(request: Request, slug: str):
    session = get_db()
    try:
        merchant = session.query(Merchant).filter_by(slug=slug).first()
        if not merchant:
            raise HTTPException(404, "Merchant introuvable")

        return templates.TemplateResponse(
            "paypage.html",
            {"request": request, "merchant": merchant},
        )
    except Exception as e:
        # Log utile dans Render si 500
        print("❌ Erreur /p/{slug} :", slug, "=>", repr(e))
        raise
    finally:
        session.close()


# ---------- ADMIN LIST ----------
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    require_admin(request)
    session = get_db()
    try:
        merchants = session.query(Merchant).order_by(Merchant.id.desc()).all()
        return templates.TemplateResponse("admin.html", {"request": request, "merchants": merchants})
    finally:
        session.close()


# ---------- ADMIN CONNECT ----------
@app.get("/connect/{merchant_id}", response_class=HTMLResponse)
def connect_page(request: Request, merchant_id: int):
    require_admin(request)
    session = get_db()
    try:
        merchant = session.get(Merchant, merchant_id)
        if not merchant:
            raise HTTPException(404, "Merchant introuvable")
        return templates.TemplateResponse("connect.html", {"request": request, "merchant": merchant})
    finally:
        session.close()


@app.post("/connect/{merchant_id}/save")
def connect_save(
    request: Request,
    merchant_id: int,
    payment_link: Optional[str] = Form(None),
    photo_url: Optional[str] = Form(None),
    maps_url: Optional[str] = Form(None),
    headline: Optional[str] = Form(None),
    whatsapp: Optional[str] = Form(None),
    whatsapp_message: Optional[str] = Form(None),
):
    require_admin(request)
    session = get_db()
    try:
        merchant = session.get(Merchant, merchant_id)
        if not merchant:
            raise HTTPException(404, "Merchant introuvable")

        merchant.payment_link = (payment_link or "").strip() or None
        merchant.photo_url = (photo_url or "").strip() or None
        merchant.maps_url = (maps_url or "").strip() or None
        merchant.headline = (headline or "").strip() or None

        # ✅ WhatsApp + message
        if whatsapp is not None:
            w = whatsapp.strip()
            if w:
                merchant.whatsapp = w

        merchant.whatsapp_message = (whatsapp_message or "").strip() or None

        session.commit()
        return RedirectResponse(f"/connect/{merchant_id}?key={ADMIN_KEY}", status_code=302)
    finally:
        session.close()


# ---------- QR INTERNE ----------
@app.get("/qr/{slug}.png")
def qr_png(request: Request, slug: str):
    base_url = get_base_url(request)
    url = f"{base_url}/p/{slug}"

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")
