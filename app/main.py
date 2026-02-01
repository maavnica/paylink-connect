import os
import io
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime
)
from sqlalchemy.orm import declarative_base, sessionmaker

import qrcode


# =========================================================
# ENV & CONFIG
# =========================================================

ENV = os.getenv("ENV", "development")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_KEY = os.getenv("ADMIN_KEY", "")

if ENV == "production" and not DATABASE_URL:
    raise RuntimeError("❌ DATABASE_URL manquant en production")

print("🚀 ENV =", ENV)
print("🗄️ DATABASE_URL =", "OK (Neon)" if DATABASE_URL else "LOCAL DEV")

# =========================================================
# DB SETUP (NEON / POSTGRES)
# =========================================================

engine = create_engine(
    DATABASE_URL if DATABASE_URL else "sqlite:///./paylink.db",
    pool_pre_ping=True,
)
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


# =========================================================
# APP
# =========================================================

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
    key = request.query_params.get("key")
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


# =========================================================
# ROUTES
# =========================================================

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/start")


# ---------- START (CLIENT) ----------
@app.get("/start", response_class=HTMLResponse)
def start_page(request: Request):
    return templates.TemplateResponse("start.html", {"request": request})


@app.post("/start")
def start_create(
    business_name: str = Form(...),
    whatsapp: str = Form(...),
    currency: str = Form(...),
    amounts: str = Form(...)
):
    slug = business_name.lower().replace(" ", "").replace("/", "")[:30]

    session = db()
    merchant = Merchant(
        slug=slug,
        business_name=business_name,
        whatsapp=whatsapp,
        currency=currency,
        amounts=amounts,
    )
    session.add(merchant)
    session.commit()
    session.refresh(merchant)
    session.close()

    print(f"✅ Merchant créé : {merchant.id} / {merchant.slug}")

    return RedirectResponse("/success", status_code=302)


@app.get("/success", response_class=HTMLResponse)
def success(request: Request):
    return templates.TemplateResponse("success.html", {"request": request})


# ---------- PAGE PUBLIQUE ----------
@app.get("/p/{slug}", response_class=HTMLResponse)
def public_page(request: Request, slug: str):
    session = db()
    merchant = session.query(Merchant).filter_by(slug=slug).first()
    session.close()

    if not merchant:
        raise HTTPException(404, "Merchant introuvable")

    return templates.TemplateResponse(
        "paypage.html",
        {"request": request, "merchant": merchant}
    )


# ---------- ADMIN LIST ----------
@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    require_admin(request)
    session = db()
    merchants = session.query(Merchant).order_by(Merchant.id.desc()).all()
    session.close()

    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "merchants": merchants}
    )


# ---------- ADMIN CONNECT ----------
@app.get("/connect/{merchant_id}", response_class=HTMLResponse)
def connect_page(request: Request, merchant_id: int):
    require_admin(request)
    session = db()
    merchant = session.query(Merchant).get(merchant_id)
    session.close()

    if not merchant:
        raise HTTPException(404, "Merchant introuvable")

    return templates.TemplateResponse(
        "connect.html",
        {"request": request, "merchant": merchant}
    )


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
    merchant = session.query(Merchant).get(merchant_id)

    if not merchant:
        session.close()
        raise HTTPException(404, "Merchant introuvable")

    merchant.payment_link = payment_link
    merchant.photo_url = photo_url
    merchant.maps_url = maps_url
    merchant.headline = headline

    session.commit()
    session.close()

    print(f"✏️ Merchant {merchant_id} mis à jour")

    return RedirectResponse(
        f"/connect/{merchant_id}?key={ADMIN_KEY}",
        status_code=302
    )


# ---------- QR INTERNE ----------
@app.get("/qr/{slug}.png")
def qr_png(slug: str):
    url = f"{os.getenv('BASE_URL', '').rstrip('/')}/p/{slug}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")



