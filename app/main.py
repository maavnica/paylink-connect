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
BASE_URL_ENV = os.getenv("BASE_URL", "").strip()
SQLITE_DIR = os.getenv("SQLITE_DIR", "/tmp").strip() or "/tmp"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"

if DATABASE_URL:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    db_dir = Path(SQLITE_DIR)
    db_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = db_dir / "paylink.db"
    engine = create_engine(
        f"sqlite:///{sqlite_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# =========================================================
# MODEL
# =========================================================

class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(Integer, primary_key=True)
    slug = Column(String, unique=True, index=True)

    business_name = Column(String)
    whatsapp = Column(String)
    currency = Column(String)
    amounts = Column(String)

    # Legacy (tu peux garder, fallback possible)
    payment_link = Column(String, nullable=True)

    photo_url = Column(String, nullable=True)
    maps_url = Column(String, nullable=True)
    headline = Column(String, nullable=True)

    whatsapp_message = Column(String, nullable=True)

    # NEW: 3 offres (label + amount affiché + lien Stripe fixe)
    offer1_label = Column(String, nullable=True)
    offer1_amount = Column(String, nullable=True)
    offer1_link = Column(String, nullable=True)

    offer2_label = Column(String, nullable=True)
    offer2_amount = Column(String, nullable=True)
    offer2_link = Column(String, nullable=True)

    offer3_label = Column(String, nullable=True)
    offer3_amount = Column(String, nullable=True)
    offer3_link = Column(String, nullable=True)

    # draft / active
    status = Column(String, default="draft", nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)


# =========================================================
# APP
# =========================================================

app = FastAPI()

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


# =========================================================
# HELPERS
# =========================================================

def get_db():
    return SessionLocal()


def require_admin(request: Request):
    if not ADMIN_KEY:
        raise HTTPException(500, "ADMIN_KEY not configured")
    if request.query_params.get("key") != ADMIN_KEY:
        raise HTTPException(403, "Forbidden")


def slugify(text: str) -> str:
    s = (text or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:30] or "paylink"


def unique_slug(session, base: str) -> str:
    slug = base
    i = 2
    while session.query(Merchant).filter_by(slug=slug).first():
        slug = f"{base[:26]}-{i}"
        i += 1
    return slug


def parse_amounts(amounts: str) -> str:
    if not amounts:
        return ""
    cleaned = re.sub(r"[^\d,| ]", "", amounts)
    parts = re.split(r"[,| ]+", cleaned.strip())
    parts = [p for p in parts if p.isdigit()]
    return ",".join(parts[:6])


def get_base_url(request: Request) -> str:
    return BASE_URL_ENV.rstrip("/") if BASE_URL_ENV else str(request.base_url).rstrip("/")


def clean_amount(v: Optional[str]) -> Optional[str]:
    """Garde uniquement des digits (affichage), ex: '25 USD' -> '25'."""
    if v is None:
        return None
    s = re.sub(r"[^\d]", "", v.strip())
    return s or None


def clean_text(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = v.strip()
    return s or None


# =========================================================
# ROUTES
# =========================================================

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})


@app.get("/start", response_class=HTMLResponse)
def start_page(request: Request):
    return templates.TemplateResponse("start.html", {"request": request})


@app.post("/start")
def start_create(
    business_name: str = Form(...),
    whatsapp: str = Form(...),
    currency: str = Form("USD"),
    amounts: str = Form("")
):
    session = get_db()
    try:
        base = slugify(business_name)
        slug = unique_slug(session, base)

        merchant = Merchant(
            slug=slug,
            business_name=business_name.strip(),
            whatsapp=whatsapp.strip(),
            currency=currency.strip().upper(),
            amounts=parse_amounts(amounts),
            status="draft",
        )

        session.add(merchant)
        session.commit()
        session.refresh(merchant)

        return RedirectResponse(f"/p/{merchant.slug}", status_code=302)
    finally:
        session.close()


@app.get("/p/{slug}", response_class=HTMLResponse)
def public_page(request: Request, slug: str):
    session = get_db()
    try:
        merchant = session.query(Merchant).filter_by(slug=slug).first()
        if not merchant:
            raise HTTPException(404, "Merchant introuvable")

        return templates.TemplateResponse(
            "paypage.html",
            {"request": request, "merchant": merchant}
        )
    finally:
        session.close()


# =========================================================
# ADMIN
# =========================================================

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    require_admin(request)
    session = get_db()
    try:
        merchants = session.query(Merchant).order_by(Merchant.id.desc()).all()
        return templates.TemplateResponse(
            "admin.html",
            {"request": request, "merchants": merchants}
        )
    finally:
        session.close()


@app.get("/connect/{merchant_id}", response_class=HTMLResponse)
def connect_page(request: Request, merchant_id: int):
    require_admin(request)
    session = get_db()
    try:
        merchant = session.get(Merchant, merchant_id)
        if not merchant:
            raise HTTPException(404, "Merchant introuvable")

        return templates.TemplateResponse(
            "connect.html",
            {"request": request, "merchant": merchant}
        )
    finally:
        session.close()


@app.post("/connect/{merchant_id}/save")
def connect_save(
    request: Request,
    merchant_id: int,

    # patch existant
    whatsapp: Optional[str] = Form(None),

    # legacy
    payment_link: Optional[str] = Form(None),

    photo_url: Optional[str] = Form(None),
    maps_url: Optional[str] = Form(None),
    headline: Optional[str] = Form(None),
    whatsapp_message: Optional[str] = Form(None),

    # NEW: 3 offres
    offer1_label: Optional[str] = Form(None),
    offer1_amount: Optional[str] = Form(None),
    offer1_link: Optional[str] = Form(None),

    offer2_label: Optional[str] = Form(None),
    offer2_amount: Optional[str] = Form(None),
    offer2_link: Optional[str] = Form(None),

    offer3_label: Optional[str] = Form(None),
    offer3_amount: Optional[str] = Form(None),
    offer3_link: Optional[str] = Form(None),
):
    require_admin(request)
    session = get_db()
    try:
        merchant = session.get(Merchant, merchant_id)
        if not merchant:
            raise HTTPException(404, "Merchant introuvable")

        # WhatsApp (ne pas écraser si vide)
        if whatsapp is not None and whatsapp.strip():
            merchant.whatsapp = whatsapp.strip()

        # Champs généraux
        merchant.photo_url = clean_text(photo_url)
        merchant.maps_url = clean_text(maps_url)
        merchant.headline = clean_text(headline)
        merchant.whatsapp_message = clean_text(whatsapp_message)

        # Legacy payment_link (fallback)
        merchant.payment_link = clean_text(payment_link)

        # Offres (label + amount + link)
        merchant.offer1_label = clean_text(offer1_label)
        merchant.offer1_amount = clean_amount(offer1_amount)
        merchant.offer1_link = clean_text(offer1_link)

        merchant.offer2_label = clean_text(offer2_label)
        merchant.offer2_amount = clean_amount(offer2_amount)
        merchant.offer2_link = clean_text(offer2_link)

        merchant.offer3_label = clean_text(offer3_label)
        merchant.offer3_amount = clean_amount(offer3_amount)
        merchant.offer3_link = clean_text(offer3_link)

        session.commit()

        return RedirectResponse(
            f"/connect/{merchant_id}?key={ADMIN_KEY}",
            status_code=302
        )
    finally:
        session.close()


@app.get("/activate/{merchant_id}")
def activate_merchant(request: Request, merchant_id: int):
    require_admin(request)
    session = get_db()
    try:
        merchant = session.get(Merchant, merchant_id)
        if not merchant:
            raise HTTPException(404)

        merchant.status = "active"
        session.commit()

        return RedirectResponse(f"/admin?key={ADMIN_KEY}", status_code=302)
    finally:
        session.close()


# =========================================================
# QR
# =========================================================

@app.get("/qr/{slug}.png")
def qr_png(request: Request, slug: str):
    base_url = get_base_url(request)
    url = f"{base_url}/p/{slug}"

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    return Response(content=buf.getvalue(), media_type="image/png")


