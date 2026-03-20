import os, re, uuid, base64, json, httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

# ── 1. CONFIGURACIÓN ────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "").strip()
SUPA_URL    = os.environ.get("SUPA_URL", "").strip()
SUPA_KEY    = os.environ.get("SUPA_KEY", "").strip()
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "").strip()

# ── 2. MOTOR GEMINI (VERSION ESTABLE) ─────────────────────────────────────────
async def analyze_bet_photo(image_bytes: bytes) -> dict:
    if not GEMINI_KEY:
        return {"error": "Falta la llave GEMINI_API_KEY en Railway."}
    
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    
    # URL ESTABLE v1 (Menos errores que v1beta)
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
    
    prompt = (
        "Analiza esta apuesta. Responde SOLO un JSON con: "
        '{"bookie": "nombre", "descripcion": "evento", "estado": "ganada/perdida/pendiente", '
        '"tickets": [{"monto": 0.0, "cuota": 0.0, "retorno": 0.0}]}.'
    )

    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}
            ]
        }]
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload)
            
            if response.status_code != 200:
                # Esto nos dirá exactamente qué dice Google ahora
                return {"error": f"Google dice: {response.text}"}
            
            data = response.json()
            text_out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Limpiar por si la IA se pone creativa
            clean_json = re.sub(r"```json|```", "", text_out).strip()
            return json.loads(clean_json)

    except Exception as e:
        return {"error": f"Fallo de conexión: {str(e)}"}

# ── 3. SUPABASE & BOT (SIMPLIFICADO) ──────────────────────────────────────────
async def start(u: Update, c):
    await u.message.reply_text("🚀 Bot listo con llave nueva. Pásame una foto.")

async def on_photo(u: Update, c):
    wait_msg = await u.message.reply_text("⚙️ Analizando...")
    try:
        photo_file = await u.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        res = await analyze_bet_photo(bytes(img_bytes))
        
        if "error" in res:
            await wait_msg.edit_text(f"❌ ERROR:\n`{res['error']}`", parse_mode="Markdown")
            return

        c.user_data["last_bet"] = res
        txt = f"🏠 {res.get('bookie')}\n🏆 {res.get('estado').upper()}\n💰 S/ {res['tickets'][0]['monto']}"
        btns = [[InlineKeyboardButton("✅ Guardar", callback_data="s"), InlineKeyboardButton("🗑️ No", callback_data="c")]]
        await wait_msg.edit_text(txt, reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait_msg.edit_text(f"❌ Error: {str(e)}")

async def on_callback(u: Update, c):
    q = u.callback_query; await q.answer()
    if q.data == "s":
        bet = c.user_data.get("last_bet")
        async with httpx.AsyncClient() as client:
            headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}
            payload = {"id": str(uuid.uuid4())[:8], "descr": bet['descripcion'], "status": "completed" if bet['estado'] == 'ganada' else 'pending'}
            res = await client.post(f"{SUPA_URL}/rest/v1/bet_groups", headers=headers, json=payload)
            await q.edit_message_text("✅ Guardado" if res.status_code in [200, 201, 204] else f"❌ Error DB: {res.status_code}")
    else:
        await q.edit_message_text("🗑️ Cancelado.")

if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.run_polling()
