# Maavnica PayLink — Stripe Connect (V1)

Cette version évite que Maavnica encaisse l'argent : **le marchand encaisse directement sur SON Stripe** via Stripe Connect.

## 1) Variables d'environnement

- `STRIPE_SECRET_KEY` : clé secrète Stripe (sk_...)
- `BASE_URL` : URL publique (ex: `https://paylink.maavnica.com`)

Optionnel :
- `STRIPE_CLIENT_ID` : laissé en place si tu veux évoluer vers OAuth Standard. (V1 utilise Account Links.)

## 2) Lancer en local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export STRIPE_SECRET_KEY="sk_test_..."
export BASE_URL="http://127.0.0.1:8000"

uvicorn app.main:app --reload
```

Ouvre : http://127.0.0.1:8000

## 3) Parcours

- Landing: `/`
- Formulaire: `/start`
- Connexion Stripe: `/connect/{merchant_id}` puis bouton
- Page client: `/p/{slug}`

## 4) Notes

- Les pages légales du site historique sont sous `/legacy/*`.
- V1 : les boutons montants créent une Checkout Session sur le compte connecté (direct charge).
