import os, re, uuid, base64, json
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters)

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
        return {"error": "GEMINI_API_KEY no configurada"}
    
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    
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
            # URL ESTABLE (v1)
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
            clean_json = re.sub(r"```json|```", "", raw_text).strip()
            return json.loads(clean_json)

    except Exception as e:
        return {"error": f"Excepción IA: {str(e)}"}

# ── Helpers Supabase (CORREGIDO) ──────────────────────────────────────────────
def get_headers():
    return {
        "apikey": SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

async def sb_insert(table, data):
    async with httpx.AsyncClient() as c:
        url = f"{SUPA_URL}/rest/v1/{table}"
        response = await c.post(url, headers=get_headers(), json=data)
        return response.status_code

# ── Manejadores de Telegram ───────────────────────────────────────────────────
async def start(u: Update, ctx):
    if ALLOWED_IDS and u.effective_user.id not in ALLOWED_IDS: return
    await u.message.reply_text("👋 ¡BotLog en línea! Envíame una foto de tu ticket de apuesta.")

async def handle_photo(u: Update, ctx):
    if ALLOWED_IDS and u.effective_user.id not in ALLOWED_IDS: return
    
    msg = await u.message.reply_text("🔍 Analizando con Gemini 1.5 Flash...")
    
    try:
        photo = u.message.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        
        res = await analyze_bet_photo(bytes(img_bytes))
        
        if "error" in res:
            await msg.edit_text(f"❌ {res['error']}")
            return

        ctx.user_data["last_bet"] = res
        
        # Formatear el resumen para el usuario
        resumen = (
            f"✅ **Ticket Detectado**\n"
            f"🏠 Casa: `{res.get('bookie')}`\n"
            f"📝 Detalle: {res.get('descripcion')}\n"
            f"📊 Estado: *{res.get('estado').upper()}*\n\n"
        )
        for t in res.get("tickets", []):
            resumen += f"💰 S/ {t.get('monto')} @{t.get('cuota')} → {t.get('retorno')}\n"

        await msg.edit_text(
            f"{resumen}\n¿Guardar esta apuesta?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Guardar", callback_data="db_save")],
                [InlineKeyboardButton("🗑 Cancelar", callback_data="db_cancel")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(f"❌ Error crítico: {e}")

async def callback_handler(u: Update, ctx):
    q = u.callback_query
    await q.answer()
    
    if q.data == "db_save":
        bet = ctx.user_data.get("last_bet")
        if not bet:
            await q.edit_message_text("⚠️ No hay datos para guardar.")
            return
        
        # Inserción en tabla 'bet_groups'
        group_id = str(uuid.uuid4())[:8]
        status = await sb_insert("bet_groups", {
            "id": group_id,
            "descr": bet.get("descripcion"),
            "status": "pending" if bet.get("estado") == "pendiente" else "completed"
        })
        
        if status in [200, 201, 204]:
            await q.edit_message_text(f"🚀 **¡Guardado!** (ID: {group_id})", parse_mode="Markdown")
        else:
            await q.edit_message_text(f"⚠️ Error Supabase: Código {status}")
    else:
        await q.edit_message_text("❌ Registro descartado.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN no encontrado."); return

    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    print("Bot activo en Railway...")
    app.run_polling()

if __name__ == "__main__":
    main()
    
