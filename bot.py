import os, re, uuid, base64, json
from datetime import date, datetime
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters)

# ── Configuración de Variables ────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN","")
SUPA_URL       = os.environ.get("SUPA_URL","")
SUPA_KEY       = os.environ.get("SUPA_KEY","")
GEMINI_KEY      = os.environ.get("GEMINI_API_KEY","")
ALLOWED_IDS    = [int(x) for x in os.environ.get("ALLOWED_USER_IDS","").split(",") if x.strip()]
EUR            = 4  # Factor para Winamax

# Estados de la conversación
(S_DESC, S_TIPSTER, S_N_BOOKIES, S_BOOKIE, S_TICKETS,
 S_INV, S_INV_MANUAL, S_CONFIRM) = range(8)

# ── Gemini Vision — Extraer datos de la foto ──────────────────────────────────
async def analyze_bet_photo(image_bytes: bytes) -> dict:
    """Envía la foto a Gemini 1.5 Flash y extrae datos en JSON."""
    if not GEMINI_KEY:
        return {"error": "GEMINI_API_KEY no configurada"}
    
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    
    # Prompt optimizado para apuestas ganadas/perdidas y casas de Perú
    prompt = """Analiza esta imagen de una apuesta deportiva y extrae los datos en JSON.
Responde ÚNICAMENTE con el objeto JSON, sin texto adicional.
Estructura:
{
  "bookie": "nombre de la casa de apuestas",
  "descripcion": "evento y mercado (ej: Real Madrid vs Barca - Gana Local)",
  "estado": "ganada / perdida / pendiente",
  "tickets": [
    {"monto": 50.0, "cuota": 1.90, "retorno": 95.0}
  ]
}
Notas:
- Si ves 'Pagado', 'Ganado' o 'Check verde', el estado es 'ganada'.
- Si ves 'Perdida' o 'X roja', el estado es 'perdida'.
- Si no hay resultado claro, el estado es 'pendiente'."""

    try:
        async with httpx.AsyncClient(timeout=40) as cl:
            # USAMOS 1.5-FLASH PARA EVITAR EL ERROR DE CUOTA 0
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
            
            payload = {
                "contents": [{
                    "parts": [
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                        {"text": prompt}
                    ]
                }],
                "generationConfig": {
                    "temperature": 0.1,
                    "response_mime_type": "application/json"
                }
            }
            
            r = await cl.post(url, json=payload)
            data = r.json()

            if "candidates" not in data:
                return {"error": f"API Error: {data.get('error', {}).get('message', 'Desconocido')}"}

            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return json.loads(text)
    except Exception as e:
        return {"error": str(e)}

# ── Helpers de Base de Datos (Supabase) ───────────────────────────────────────
def H():
    return {"apikey":SUPA_KEY,"Authorization":f"Bearer {SUPA_KEY}",
            "Content-Type":"application/json","Prefer":"return=minimal"}

async def sb_post(table, data):
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{SUPA_URL}/rest/v1/{table}", headers=H(), json=data)).status_code

# ── Manejador de Fotos ────────────────────────────────────────────────────────
async def handle_photo(u: Update, ctx):
    """Recibe la foto, la analiza y muestra resumen."""
    if not (not ALLOWED_IDS or u.effective_user.id in ALLOWED_IDS): return
    
    waiting_msg = await u.message.reply_text("🔍 Leyendo ticket de apuesta...")
    
    photo = u.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    img_bytes = await file.download_as_bytearray()
    
    result = await analyze_bet_photo(bytes(img_bytes))
    
    if "error" in result:
        await waiting_msg.edit_text(f"❌ Error: {result['error']}\n\nRevisa si tu API KEY es correcta.")
        return

    # Guardar temporalmente para el flujo de guardado
    ctx.user_data["photo_data"] = result
    
    resumen = (
        f"✅ **Ticket Detectado**\n\n"
        f"🏠 Casa: {result.get('bookie')}\n"
        f"📋 Evento: {result.get('descripcion')}\n"
        f"📊 Estado: {result.get('estado').upper()}\n"
    )
    
    for t in result.get('tickets', []):
        resumen += f"💰 {t.get('monto')} @{t.get('cuota')} (Retorno: {t.get('retorno')})\n"

    await waiting_msg.edit_text(
        f"{resumen}\n¿Deseas registrar esta apuesta en Supabase?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Sí, Guardar", callback_data="save_photo_bet")],
            [InlineKeyboardButton("❌ Cancelar", callback_data="cancel_photo")]
        ]),
        parse_mode="Markdown"
    )

async def confirm_photo_save(u: Update, ctx):
    q = u.callback_query; await q.answer()
    data = ctx.user_data.get("photo_data")
    
    if q.data == "cancel_photo":
        await q.edit_message_text("❌ Registro cancelado.")
        return

    # Lógica para guardar en Supabase (Ajusta los nombres de tus tablas)
    try:
        group_id = str(uuid.uuid4())[:12]
        await sb_post("bet_groups", {
            "id": group_id,
            "descr": data.get("descripcion"),
            "status": "pending" if data.get("estado") == "pendiente" else "completed"
        })
        await q.edit_message_text("🚀 **¡Apuesta guardada con éxito!**", parse_mode="Markdown")
    except Exception as e:
        await q.edit_message_text(f"❌ Error al guardar: {e}")

# ── Función Principal (Main) ──────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Comandos básicos
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Bot Activo. Envíame una foto de tu apuesta.")))
    
    # Manejador de fotos
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    # Manejador de botones
    app.add_handler(CallbackQueryHandler(confirm_photo_save, pattern="^(save_photo_bet|cancel_photo)$"))

    print("Bot corriendo en US East...")
    app.run_polling()

if __name__ == "__main__":
    main()
