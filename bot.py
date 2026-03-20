import os, re, uuid, base64, json, httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

# ── 1. CONFIGURACIÓN ────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "").strip()
SUPA_URL    = os.environ.get("SUPA_URL", "").strip()
SUPA_KEY    = os.environ.get("SUPA_KEY", "").strip()
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "").strip()

# ── 2. MOTOR DE DIAGNÓSTICO Y ANÁLISIS ──────────────────────────────────────
async def analyze_bet_photo(image_bytes: bytes) -> dict:
    # Verificación de seguridad de la llave
    if not GEMINI_KEY.startswith("AIza"):
        return {"error": "Tu GEMINI_API_KEY parece incorrecta. Asegúrate de copiar la API KEY (empieza con AIza) y no el Project ID."}
    
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    
    # Intentaremos estos 3 en orden. Uno de ellos DEBE funcionar.
    models_to_try = ["gemini-1.5-flash-latest", "gemini-1.5-flash", "gemini-pro-vision"]
    
    last_raw_error = ""

    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        
        payload = {
            "contents": [{
                "parts": [
                    {"text": "Analiza esta apuesta deportiva y devuelve SOLO un JSON: {'bookie': 'string', 'descripcion': 'string', 'estado': 'ganada/perdida/pendiente', 'tickets': [{'monto': 0.0, 'cuota': 0.0, 'retorno': 0.0}]}"},
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}
                ]
            }],
            "generationConfig": {"temperature": 0.1}
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, json=payload)
                
                if response.status_code == 200:
                    data = response.json()
                    text_out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    clean_json = re.sub(r"```json|```", "", text_out).strip()
                    return json.loads(clean_json)
                
                last_raw_error = response.text
                print(f"DEBUG: Intento con {model} falló: {response.status_code}")
                
        except Exception as e:
            last_raw_error = str(e)
            continue

    return {"error": f"Ningún modelo respondió. Error final de Google: {last_raw_error}"}

# ── 3. LÓGICA DE TELEGRAM Y SUPABASE ──────────────────────────────────────────
async def start(u: Update, c):
    await u.message.reply_text("💪 Intento final. Pásame la foto del ticket.")

async def on_photo(u: Update, c):
    wait_msg = await u.message.reply_text("🔍 Procesando con protocolo de emergencia...")
    try:
        photo_file = await u.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        res = await analyze_bet_photo(bytes(img_bytes))
        
        if "error" in res:
            await wait_msg.edit_text(f"❌ **FALLÓ NUEVAMENTE:**\n\n`{res['error']}`", parse_mode="Markdown")
            return

        c.user_data["last_bet"] = res
        resumen = f"🏠 {res.get('bookie')}\n🏆 {res.get('estado').upper()}\n💰 S/ {res['tickets'][0]['monto']}"
        btns = [[InlineKeyboardButton("✅ Guardar", callback_data="s"), InlineKeyboardButton("🗑️ No", callback_data="c")]]
        await wait_msg.edit_text(resumen, reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait_msg.edit_text(f"❌ Error interno: {str(e)}")

async def on_callback(u: Update, c):
    q = u.callback_query; await q.answer()
    if q.data == "s":
        bet = c.user_data.get("last_bet")
        async with httpx.AsyncClient() as client:
            headers = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}
            payload = {"id": str(uuid.uuid4())[:8], "descr": bet['descripcion'], "status": "completed" if bet['estado'] == 'ganada' else 'pending'}
            res = await client.post(f"{SUPA_URL}/rest/v1/bet_groups", headers=headers, json=payload)
            await q.edit_message_text("✅ Guardado en DB" if res.status_code in [200, 201, 204] else f"❌ Error Supabase: {res.status_code}")
    else:
        await q.edit_message_text("🗑️ Cancelado.")

if __name__ == "__main__":
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.run_polling()
