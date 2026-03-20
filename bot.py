import os, re, uuid, base64, json, httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

# ── 1. CONFIGURACIÓN ────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "").strip()
SUPA_URL    = os.environ.get("SUPA_URL", "").strip()
SUPA_KEY    = os.environ.get("SUPA_KEY", "").strip()
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "").strip()
ALLOWED_IDS = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()]

# ── 2. EL MOTOR DE GEMINI CON AUTO-REINTENTO ──────────────────────────────────
async def analyze_bet_photo(image_bytes: bytes) -> dict:
    if not GEMINI_KEY:
        return {"error": "No has puesto la GEMINI_API_KEY en Railway."}
    
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    
    # Lista de nombres que Google reconoce (intentaremos uno por uno si falla el 404)
    model_variants = ["gemini-1.5-flash-latest", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
    
    prompt = (
        "Analiza esta imagen de apuesta. Devuelve SOLO un objeto JSON con: "
        '{"bookie": "nombre", "descripcion": "evento", "estado": "ganada/perdida/pendiente", '
        '"tickets": [{"monto": float, "cuota": float, "retorno": float}]}.'
    )

    last_error = ""
    for model in model_variants:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
        payload = {
            "contents": [{"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}, {"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"}
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, json=payload)
                
                if response.status_code == 200:
                    data = response.json()
                    text_out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    clean_json = re.sub(r"```json|```", "", text_out).strip()
                    return json.loads(clean_json)
                
                # Si es 404, probamos el siguiente modelo de la lista
                if response.status_code == 404:
                    last_error = f"Modelo {model} no encontrado (404)."
                    continue 
                
                return {"error": f"Google Error {response.status_code}: {response.text}"}
        
        except Exception as e:
            last_error = str(e)
            continue

    return {"error": f"Ningún modelo funcionó. Último error: {last_error}"}

# ── 3. SUPABASE ───────────────────────────────────────────────────────────────
async def save_to_supabase(bet_data: dict):
    headers = {
        "apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json", "Prefer": "return=minimal"
    }
    payload = {
        "id": str(uuid.uuid4())[:8],
        "descr": bet_data.get("descripcion", "Apuesta"),
        "status": "completed" if bet_data.get("estado") == "ganada" else "pending"
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(f"{SUPA_URL}/rest/v1/bet_groups", headers=headers, json=payload)
        return res.status_code

# ── 4. MANEJADORES DEL BOT ────────────────────────────────────────────────────
async def start(u: Update, c):
    await u.message.reply_text("✅ Bot activo. Envíame la foto de tu apuesta.")

async def on_photo(u: Update, c):
    if ALLOWED_IDS and u.effective_user.id not in ALLOWED_IDS: return
    wait_msg = await u.message.reply_text("⏳ Leyendo ticket...")
    
    try:
        photo_file = await u.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        res = await analyze_bet_photo(bytes(img_bytes))
        
        if "error" in res:
            await wait_msg.edit_text(f"❌ **FALLO DE CONFIGURACIÓN:**\n\n`{res['error']}`", parse_mode="Markdown")
            return

        c.user_data["last_bet"] = res
        resumen = (
            f"📍 **Casa:** {res.get('bookie')}\n"
            f"🏆 **Estado:** {res.get('estado', '').upper()}\n"
            f"💰 **Monto:** {res['tickets'][0]['monto']} | **Cuota:** {res['tickets'][0]['cuota']}"
        )
        btns = [[InlineKeyboardButton("✅ Guardar", callback_data="save"), InlineKeyboardButton("🗑️ Cancelar", callback_data="cancel")]]
        await wait_msg.edit_text(resumen, reply_markup=InlineKeyboardMarkup(btns))
    except Exception as e:
        await wait_msg.edit_text(f"❌ Error: {str(e)}")

async def on_callback(u: Update, c):
    q = u.callback_query; await q.answer()
    if q.data == "save":
        status = await save_to_supabase(c.user_data.get("last_bet", {}))
        await q.edit_message_text("✅ Guardado en Supabase" if status in [200, 201, 204] else f"❌ Error DB: {status}")
    else:
        await q.edit_message_text("🗑️ Cancelado.")

# ── 5. EJECUCIÓN ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if BOT_TOKEN:
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.PHOTO, on_photo))
        app.add_handler(CallbackQueryHandler(on_callback))
        print("Bot encendido..."); app.run_polling()
