import os, re, uuid, base64, json, httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

# ── 1. VERIFICACIÓN DE VARIABLES AL ARRANQUE ──────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "").strip()
SUPA_URL    = os.environ.get("SUPA_URL", "").strip()
SUPA_KEY    = os.environ.get("SUPA_KEY", "").strip()
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "").strip()
ALLOWED_IDS = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()]

# ── 2. EL MOTOR DE GEMINI (CON DIAGNÓSTICO DETALLADO) ─────────────────────────
async def analyze_bet_photo(image_bytes: bytes) -> dict:
    if not GEMINI_KEY:
        return {"error": "Falta la variable GEMINI_API_KEY en Railway."}
    
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    
    # Probamos con el modelo más estándar del Free Tier
    model_name = "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_KEY}"
    
    prompt = (
        "Analiza esta apuesta. Devuelve un JSON estrictamente con esta estructura: "
        '{"bookie": "string", "descripcion": "string", "estado": "ganada/perdida/pendiente", '
        '"tickets": [{"monto": float, "cuota": float, "retorno": float}]}. '
        "No añadas texto extra."
    )

    payload = {
        "contents": [{"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": b64_image}}, {"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"}
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json=payload)
            
            # SI GOOGLE DA ERROR (400, 403, 404, etc)
            if response.status_code != 200:
                return {"error": f"Google API Error {response.status_code}: {response.text}"}
            
            data = response.json()
            
            if "candidates" not in data or not data["candidates"]:
                return {"error": f"Respuesta vacía de Gemini: {json.dumps(data)}"}
            
            text_out = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Limpieza profunda por si manda Markdown
            clean_json = re.sub(r"```json|```", "", text_out).strip()
            return json.loads(clean_json)

    except Exception as e:
        return {"error": f"Fallo en la petición: {str(e)}"}

# ── 3. CONEXIÓN A SUPABASE (SIN ERRORES DE SINTAXIS) ──────────────────────────
async def save_to_supabase(bet_data: dict):
    headers = {
        "apikey": SUPA_KEY,
        "Authorization": f"Bearer {SUPA_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    payload = {
        "id": str(uuid.uuid4())[:8],
        "descr": bet_data.get("descripcion", "Apuesta sin nombre"),
        "status": "completed" if bet_data.get("estado") == "ganada" else "pending"
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(f"{SUPA_URL}/rest/v1/bet_groups", headers=headers, json=payload)
        return res.status_code

# ── 4. LÓGICA DEL BOT ─────────────────────────────────────────────────────────
async def start(u: Update, c):
    await u.message.reply_text("🚀 Bot listo. Pásame la foto de tu ticket de apuesta.")

async def on_photo(u: Update, c):
    if ALLOWED_IDS and u.effective_user.id not in ALLOWED_IDS: return
    
    wait_msg = await u.message.reply_text("⚙️ Analizando imagen... (esto puede tardar 10s)")
    
    try:
        photo_file = await u.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        
        # LLAMADA A GEMINI
        res = await analyze_bet_photo(bytes(img_bytes))
        
        if "error" in res:
            # ESTO TE DIRÁ EXACTAMENTE QUÉ PASA
            await wait_msg.edit_text(f"❌ **ERROR TÉCNICO:**\n\n`{res['error']}`", parse_mode="Markdown")
            return

        c.user_data["last_bet"] = res
        
        resumen = (
            f"📍 **Casa:** {res.get('bookie')}\n"
            f"📝 **Evento:** {res.get('descripcion')}\n"
            f"🏆 **Estado:** {res.get('estado').upper()}\n"
            f"💰 **Monto:** {res['tickets'][0]['monto']} | **Cuota:** {res['tickets'][0]['cuota']}"
        )
        
        btns = [[InlineKeyboardButton("✅ Guardar en DB", callback_data="save"),
                 InlineKeyboardButton("🗑️ Borrar", callback_data="cancel")]]
        
        await wait_msg.edit_text(resumen, reply_markup=InlineKeyboardMarkup(btns))

    except Exception as e:
        await wait_msg.edit_text(f"❌ Error de sistema: {str(e)}")

async def on_callback(u: Update, c):
    q = u.callback_query; await q.answer()
    if q.data == "save":
        status = await save_to_supabase(c.user_data.get("last_bet", {}))
        txt = "✅ Guardado en Supabase" if status in [200, 201, 204] else f"❌ Error DB: {status}"
        await q.edit_message_text(txt)
    else:
        await q.edit_message_text("🗑️ Descartado.")

# ── 5. ARRANQUE ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("CRÍTICO: No hay BOT_TOKEN.")
    else:
        print("Bot iniciado con éxito...")
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.PHOTO, on_photo))
        app.add_handler(CallbackQueryHandler(on_callback))
        app.run_polling()
