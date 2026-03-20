import os, re, uuid, base64, json
from datetime import date, datetime
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters)

# ── Configuración de Variables (Railway) ──────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN","")
SUPA_URL       = os.environ.get("SUPA_URL","")
SUPA_KEY       = os.environ.get("SUPA_KEY","")
GEMINI_KEY     = os.environ.get("GEMINI_API_KEY","")
ALLOWED_IDS    = [int(x) for x in os.environ.get("ALLOWED_USER_IDS","").split(",") if x.strip()]

# ── Gemini Vision — Análisis de Apuestas ──────────────────────────────────────
async def analyze_bet_photo(image_bytes: bytes) -> dict:
    """Envía foto a Gemini 1.5 Flash usando la API estable (v1)."""
    if not GEMINI_KEY:
        return {"error": "GEMINI_API_KEY no configurada en Railway"}
    
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    
    # Prompt optimizado para detectar Ganadas/Perdidas y montos
    prompt = """Analiza esta imagen de una apuesta deportiva.
Extrae los datos en este formato JSON exacto:
{
  "bookie": "nombre de la casa",
  "descripcion": "evento y mercado",
  "estado": "ganada / perdida / pendiente",
  "tickets": [
    {"monto": 0.0, "cuota": 0.0, "retorno": 0.0}
  ]
}
Notas:
- 'ganada': si dice Pagado, Ganada, cobrado o tiene check verde.
- 'perdida': si dice Perdida o tiene X roja.
- 'pendiente': si no hay resultado aún.
- Responde SOLO el JSON."""

    try:
        async with httpx.AsyncClient(timeout=45) as cl:
            # URL ESTABLE (v1) para evitar el error "model not found"
            url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
            
            payload = {
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                        {"text": prompt}
                    ]
                }],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 800
                }
            }
            
            r = await cl.post(url, json=payload, headers={"Content-Type": "application/json"})
            data = r.json()

            if "candidates" not in data:
                error_msg = data.get("error", {}).get("message", "Error de cuota o API")
                return {"error": f"Gemini Error: {error_msg}"}

            raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Limpiamos el texto por si la IA devuelve marcas de markdown ```json
            clean_json = re.sub(r"```json|```", "", raw_text).strip()
            return json.loads(clean_json)

    except Exception as e:
        return {"error": f"Excepción: {str(e)}"}

# ── Helpers Supabase ──────────────────────────────────────────────────────────
def get_headers():
    return {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
            "Content-Type": "application/json",
