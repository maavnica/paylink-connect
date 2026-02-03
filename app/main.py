import os
import io
import re
from datetime import datetime
from typing import Optional

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
BASE_URL_ENV = os.getenv("BASE_URL", "").strip()  # optionnel

# IMPORTANT: si DATABASE_URL existe => Postgres/Neon. Sinon => SQLite (dev)
DB_URL = DATABASE_URL if DATABASE_URL else "sqlite:///./paylink.db"

print("🗄️ DB =", "Postgres/Neon" if DATABASE_URL else "SQLite local")

engine = create_engine(DB_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
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

    created_at = Column(DateTime, default=datetime.utcnow)


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup():
    print("🧱 Initialisation DB (create_all)")
    Base.metadata.create_all(bind=engine)


# =========================================================
# HELPERS
# =========================================================

def db():
    return SessionLocal()


def require_admin(request: Request):
    key = request.query_params.get("key", "")
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def slugify(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:30] if s else "paylink"


def unique_slug(session, base: str) -> str:
    # évite collision sur unique(slug)
    slug = base
    i = 2
    while session.query(Merchant).filter_by(slug=slug).first() is not None:
        slug = f"{base[:26]}-{i}"
        i += 1
    return slug


def parse_amounts(amounts: str) -> str:
    """
    On stocke en string simple (comme aujourd'hui) pour ne rien casser.
    Formats acceptés: "25,49,79" / "25 49 79" / "25|49|79"
    """
    if not amounts:
        return ""
    cleaned = re.sub(r"[^\d,| ]", "", amounts)
    parts = re.split(r"[,| ]+", cleaned.strip())
    parts = [p for p in parts if p.isdigit()]
    return ",".join(parts[:6])  # limite sécurité


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
    currency: str = Form("USD"),           # ✅ plus de blocage si le champ manque
    amounts: str = Form(""),               # ✅ optionnel
):
    session = db()

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
    session.close()

    print(f"✅ Merchant créé : {merchant.id} / {merchant.slug}")

    # ✅ après start, on renvoie vers la page publique (plus logique que /success vide)
    return RedirectResponse(f"/p/{merchant.slug}", status_code=302)


# ---------- PAGE PUBLIQUE ----------
@app.get("/p/{slug}", response_class=HTMLResponse)
def public_page(request: Request, slug: str):
    session = db()
    merchant = session.query(Merchant).filter_by(slug=slug).first()
    session.close()

    if not merchant:
        raise HTTPException(404, "Merchant introuvable")

    return templates.TemplateResponse("paypage.html", {"request": request, "merchant": merchant})


# ---------- ADMIN LIST ----------
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    require_admin(request)
    session = db()
    merchants = session.query(Merchant).order_by(Merchant.id.desc()).all()
    session.close()

    return templates.TemplateResponse("admin.html", {"request": request, "merchants": merchants})


# ---------- ADMIN CONNECT ----------
@app.get("/connect/{merchant_id}", response_class=HTMLResponse)
def connect_page(request: Request, merchant_id: int):
    require_admin(request)
    session = db()
    merchant = session.get(Merchant, merchant_id)
    session.close()

    if not merchant:
        raise HTTPException(404, "Merchant introuvable")

    return templates.TemplateResponse("connect.html", {"request": request, "merchant": merchant})


@app.post("/connect/{merchant_id}/save")
def connect_save(
    request: Request,
    merchant_id: int,
    payment_link: Optional[str] = Form(None),
    photo_url: Optional[str] = Form(None),
    maps_url: Optional[str] = Form(None),
    headline: Optional[str] = Form(None),
):
    require_admin(request)

    session = db()
    merchant = session.get(Merchant, merchant_id)

    if not merchant:
        session.close()
        raise HTTPException(404, "Merchant introuvable")

    merchant.payment_link = (payment_link or "").strip() or None
    merchant.photo_url = (photo_url or "").strip() or None
    merchant.maps_url = (maps_url or "").strip() or None
    merchant.headline = (headline or "").strip() or None

    session.commit()
    session.close()

    return RedirectResponse(f"/connect/{merchant_id}?key={ADMIN_KEY}", status_code=302)


# ---------- QR INTERNE ----------
@app.get("/qr/{slug}.png")
def qr_png(request: Request, slug: str):
    # ✅ si BASE_URL env manquant, on construit depuis la requête Render
    base_url = BASE_URL_ENV.rstrip("/") if BASE_URL_ENV else str(request.base_url).rstrip("/")
    url = f"{base_url}/p/{slug}"

    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")




