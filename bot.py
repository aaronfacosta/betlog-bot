# ── Gemini Vision — extract bet data from photo ────────────────────────────────
async def analyze_bet_photo(image_bytes: bytes) -> dict:
    """Send photo to Gemini 1.5 Flash and extract bet data. Returns dict with fields."""
    if not GEMINI_KEY:
        return {"error": "GEMINI_API_KEY no configurada"}
    
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    
    prompt = """Analiza esta imagen de una apuesta deportiva y extrae los datos en JSON.
Responde ÚNICAMENTE con el objeto JSON, sin explicaciones ni bloques de código markdown.
Estructura:
{
  "bookie": "nombre de la casa de apuestas",
  "descripcion": "evento y mercado (ej: Real Madrid vs Barca - Gana Local)",
  "tickets": [
    {"monto": 50.0, "cuota": 1.90}
  ]
}
Notas: 
- Si es Winamax, usa el monto en Euros que veas.
- Si no ves la cuota pero sí el retorno, cuota = retorno / monto."""

    try:
        async with httpx.AsyncClient(timeout=40) as cl:
            # USAMOS EL MODELO 1.5-FLASH PARA EVITAR EL ERROR DE CUOTA 0
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
                    "maxOutputTokens": 1000,
                    "response_mime_type": "application/json" # Forzamos salida JSON
                }
            }
            
            r = await cl.post(url, json=payload, headers={"Content-Type": "application/json"})
            res_json = r.json()

            if "candidates" not in res_json:
                error_info = res_json.get("error", {}).get("message", "Error desconocido")
                return {"error": f"API Error: {error_info}"}

            raw_text = res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
            
            # Limpieza de posibles marcas de markdown
            clean_json = re.sub(r"```json|```", "", raw_text).strip()
            return json.loads(clean_json)

    except Exception as e:
        return {"error": f"Excepción: {str(e)}"}
