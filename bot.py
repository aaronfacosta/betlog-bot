import os, re, uuid, base64, json
from datetime import date, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters)

BOT_TOKEN      = os.environ.get("BOT_TOKEN","")
SUPA_URL       = os.environ.get("SUPA_URL","")
SUPA_KEY       = os.environ.get("SUPA_KEY","")
OPENAI_KEY     = os.environ.get("OPENAI_API_KEY","")
ALLOWED_IDS    = [int(x) for x in os.environ.get("ALLOWED_USER_IDS","").split(",") if x.strip()]
EUR            = 4
TIMEOUT        = 900

(S_DESC, S_TIPSTER, S_N_BOOKIES, S_BOOKIE, S_TICKETS,
 S_INV, S_INV_MANUAL, S_CONFIRM) = range(8)
(P_LIST, P_DETAIL, P_RESULT, P_EXACT, P_CONFIRM_RES, P_DEL) = range(10,16)

MENU_KB = ReplyKeyboardMarkup([
    [KeyboardButton("📝 Nueva apuesta"), KeyboardButton("⏳ Pendientes")],
    [KeyboardButton("📊 Hoy"),           KeyboardButton("❌ Cancelar")]
], resize_keyboard=True)

BACK = [[InlineKeyboardButton("↩ Atrás", callback_data="back")]]

import httpx

def H():
    return {"apikey":SUPA_KEY,"Authorization":f"Bearer {SUPA_KEY}",
            "Content-Type":"application/json","Prefer":"return=minimal"}

async def sb_get(table, params=""):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{SUPA_URL}/rest/v1/{table}?{params}", headers=H())
        return r.json()

async def sb_post(table, data):
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{SUPA_URL}/rest/v1/{table}", headers=H(), json=data)).status_code

async def sb_patch(table, col, val, data):
    h = H(); h["Prefer"] = "return=minimal"
    async with httpx.AsyncClient() as c:
        return (await c.patch(f"{SUPA_URL}/rest/v1/{table}?{col}=eq.{val}", headers=h, json=data)).status_code

async def sb_delete(table, col, val):
    async with httpx.AsyncClient() as c:
        return (await c.delete(f"{SUPA_URL}/rest/v1/{table}?{col}=eq.{val}", headers=H())).status_code

def gid():    return str(uuid.uuid4())[:12].replace("-","")
def fmt(n):   return f"{n:,.2f}"
def is_ok(u): return not ALLOWED_IDS or u.effective_user.id in ALLOWED_IDS
def now_str(): return datetime.now().strftime("%d-%m-%Y · %H:%M")

async def try_del(msg):
    try: await msg.delete()
    except: pass

async def clear(ctx, chat_id, bot):
    for m in ctx.user_data.get("_msgs",[]):
        try: await bot.delete_message(chat_id=chat_id, message_id=m)
        except: pass
    ctx.user_data["_msgs"] = []

async def track(ctx, msg):
    ctx.user_data.setdefault("_msgs",[]).append(msg.message_id)

async def edit_or_new(src, text, kbd=None, md="Markdown"):
    markup = InlineKeyboardMarkup(kbd) if kbd else None
    if hasattr(src,"edit_message_text"):
        try: await src.edit_message_text(text, reply_markup=markup, parse_mode=md); return
        except: pass
    m = src.message if hasattr(src,"message") else src
    await m.reply_text(text, reply_markup=markup, parse_mode=md)

async def delete_old_confirm(group_id, bot):
    try:
        g = await sb_get("bet_groups", f"id=eq.{group_id}&select=tg_chat_id,tg_msg_id")
        if isinstance(g,list) and g and g[0].get("tg_msg_id") and g[0].get("tg_chat_id"):
            await bot.delete_message(chat_id=g[0]["tg_chat_id"], message_id=g[0]["tg_msg_id"])
    except: pass

async def analyze_bet_photo(image_bytes: bytes) -> dict:
    if not OPENAI_KEY:
        return {"error": "OPENAI_API_KEY no configurada en Railway"}
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt = """Analiza esta imagen de una apuesta deportiva y extrae los datos en JSON.
Responde UNICAMENTE con el objeto JSON, sin explicaciones ni bloques de codigo markdown.
Estructura exacta:
{
  "bookie": "nombre de la casa de apuestas",
  "descripcion": "evento y mercado (ej: Real Madrid vs Barcelona - Gana Local)",
  "tickets": [
    {"monto": 500.0, "cuota": 1.90}
  ]
}
Notas:
- monto: cantidad apostada en la moneda original de la imagen
- cuota: cuota decimal. Si ves retorno en vez de cuota, calcula cuota = retorno / monto
- Si es Winamax, usa el monto en euros
- Si hay multiples selecciones, ponlas como un solo ticket con la cuota combinada
- Si no puedes leer un campo con certeza, usa null"""
    try:
        async with httpx.AsyncClient(timeout=40) as cl:
            r = await cl.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}},
                            {"type": "text", "text": prompt}
                        ]
                    }],
                    "temperature": 0,
                    "max_tokens": 512
                }
            )
        data = r.json()
        if "choices" not in data:
            err = data.get("error", {})
            return {"error": f"API error: {err.get('message', str(err))[:200]}"}
        text = data["choices"][0]["message"]["content"].strip()
        text = re.sub(r"```(?:json)?", "", text).strip().rstrip("```").strip()
        return json.loads(text)
    except Exception as e:
        return {"error": f"Excepcion: {str(e)}"}

def gs(ctx):
    ctx.user_data.setdefault("s",{
        "desc":"","date":str(date.today()),
        "tipster":"","tipsters":[],"bookies":[],"investors":[],
        "inv_tipster_stakes":[],
        "n_bookies":1,"bookie_idx":0,"cur_bookie":"","wnx":False,
        "tickets":[],"inv_stakes":{},
        "_auto_stakes":{},"_undef_inv":[],"_undef_idx":0,"_adding_undef":""
    })
    return ctx.user_data["s"]

def rs(ctx):
    ctx.user_data["s"] = {
        "desc":"","date":str(date.today()),
        "tipster":"","tipsters":[],"bookies":[],"investors":[],
        "inv_tipster_stakes":[],
        "n_bookies":1,"bookie_idx":0,"cur_bookie":"","wnx":False,
        "tickets":[],"inv_stakes":{},
        "_auto_stakes":{},"_undef_inv":[],"_undef_idx":0,"_adding_undef":""
    }
    ctx.user_data["_msgs"] = []

async def load_db(ctx):
    s = gs(ctx)
    tp  = await sb_get("tipsters","order=name.asc")
    bk  = await sb_get("bookies","order=name.asc")
    iv  = await sb_get("investors","order=name.asc")
    its = await sb_get("investor_tipster_stakes","")
    s["tipsters"]           = [t["name"] for t in tp]  if isinstance(tp,list)  else []
    s["bookies"]            = [b["name"] for b in bk]  if isinstance(bk,list)  else []
    s["investors"]          = iv                        if isinstance(iv,list)  else []
    s["inv_tipster_stakes"] = its                       if isinstance(its,list) else []

def parse_tickets(text, wnx=False):
    tickets, errors = [], []
    for i, line in enumerate(text.strip().splitlines(), 1):
        line = line.strip()
        if not line: continue
        m = re.match(r'^([\d.,]+)\s+@?([\d.,]+)$', line)
        if not m:
            errors.append(f"Linea {i}: `{line}` - usa `500 @1.90` o `500 285`")
            continue
        raw  = float(m.group(1).replace(",","."))
        val  = float(m.group(2).replace(",","."))
        stake = round(raw * EUR, 2) if wnx else raw
        has_at = "@" in line
        if has_at:
            if val <= 1: errors.append(f"Linea {i}: cuota debe ser > 1"); continue
            cuota = val; pot = round(stake * cuota, 2)
        else:
            raw_ret = val * EUR if wnx else val
            if raw_ret <= stake: errors.append(f"Linea {i}: retorno debe ser > stake"); continue
            pot = round(raw_ret, 2); cuota = round(pot / stake, 3)
        tickets.append({"stake":stake,"cuota":cuota,"potencial":pot,"eur":raw if wnx else None})
    return tickets, errors

def cc(tickets):
    ts = sum(t["stake"] for t in tickets)
    tp = sum(t["potencial"] for t in tickets)
    return round(tp/ts, 3) if ts > 0 else 0

def get_auto_inv_stakes(s, tipster_name, total_stake):
    result = {}
    for inv in s.get("investors",[]):
        match = next((x for x in s.get("inv_tipster_stakes",[])
            if x["investor_id"]==inv["id"]
            and x["tipster"].lower()==tipster_name.lower()), None)
        if match and float(match["percentage"]) > 0:
            stake = round(total_stake * float(match["percentage"]) / 100, 2)
            result[inv["name"]] = {"stake":stake, "pct":float(match["percentage"]), "id":inv["id"]}
    return result

def build_confirm(s):
    tix   = s["tickets"]
    ts    = sum(t["stake"] for t in tix)
    tp    = sum(t["potencial"] for t in tix)
    cuota = cc(tix)
    d     = s.get("_dt") or now_str()
    SEP   = "╠═══════════════════════"
    lines = ["╔═══════════════════════", f"║ 📅 {d}", f"║ 📋 {s['desc']}", SEP]
    for i,t in enumerate(tix,1):
        eur = f" ({t['eur']}€)" if t.get("eur") else ""
        lines.append(f"║ #{i} {t.get('tipster','')} · {t.get('bookie','')}")
        lines.append(f"║    {fmt(t['stake'])}{eur} @{t['cuota']} → pot {fmt(t['potencial'])}")
    lines += [SEP, f"║ @{cuota}  {fmt(ts)} apostado  +{fmt(tp-ts)}"]
    invs = {k:v for k,v in s.get("inv_stakes",{}).items() if v>0}
    if invs:
        for name,stake in invs.items():
            pot = round(stake * cuota, 2)
            lines.append(f"║ 💼 {name}: {fmt(stake)} → +{fmt(pot-stake)}")
    lines += ["╚═══════════════════════", "✅ *Apuesta registrada*"]
    return "\n".join(lines)

def build_result_msg(desc, tickets, rets, inv_group=None, comb_c=1, orig_date=None):
    ts  = sum(t["stake"] for t in tickets)
    tr  = sum(rets.values())
    pnl = round(tr - ts, 2)
    if pnl > 0.01:    status = "✅ *Apuesta ganada*"
    elif pnl < -0.01: status = "❌ *Apuesta perdida*"
    else:             status = "🔵 *Apuesta void*"
    d   = orig_date or now_str()
    SEP = "╠═══════════════════════"
    lines = ["╔═══════════════════════", f"║ 📅 {d}", f"║ 📋 {desc}", SEP]
    for t in tickets:
        r = rets.get(t["id"],0); sk = t["stake"]
        lbl = "✅" if r > sk else ("🔵" if abs(r-sk) < 0.01 else "❌")
        lines.append(f"║ {lbl} {t.get('tipster','')} · {t.get('casa','')} → {fmt(r)}")
    lines += [SEP, f"║ P&L: *{'+' if pnl>=0 else ''}{fmt(pnl)}*"]
    if inv_group:
        for iid, data in inv_group.items():
            inv_pnl = round(data["stake"] * comb_c - data["stake"], 2)
            if abs(inv_pnl) >= 0.01:
                lines.append(f"║ 💼 {data.get('name','?')}: {'+' if inv_pnl>=0 else ''}{fmt(inv_pnl)}")
    lines += ["╚═══════════════════════", status]
    return "\n".join(lines)

async def handle_photo(u: Update, ctx):
    if not is_ok(u): return
    msg = await u.message.reply_text("🔍 Analizando imagen...")
    try:
        photo = u.message.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()
        result = await analyze_bet_photo(bytes(img_bytes))
        if "error" in result:
            await msg.edit_text(f"❌ No pude analizar la imagen: {result['error']}")
            return
        rs(ctx); await load_db(ctx)
        s = gs(ctx)
        desc = result.get("descripcion") or ""
        bookie = result.get("bookie") or ""
        tickets_raw = result.get("tickets") or []
        lines = ["📋 *Datos detectados:*\n"]
        if desc:   lines.append(f"📝 Descripcion: *{desc}*")
        if bookie: lines.append(f"🏠 Bookie: *{bookie}*")
        for i, t in enumerate(tickets_raw, 1):
            m2 = t.get("monto"); cq = t.get("cuota")
            if m2 and cq:
                lines.append(f"#{i}  {fmt(float(m2))} @{cq}  → pot {fmt(round(float(m2)*float(cq),2))}")
        lines.append("\n¿Los datos son correctos?")
        ctx.user_data["photo_prefill"] = {"desc": desc, "bookie": bookie, "tickets": tickets_raw}
        await msg.edit_text("\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Si, continuar", callback_data="photo_ok"),
                InlineKeyboardButton("❌ Reintentar",    callback_data="photo_retry"),
                InlineKeyboardButton("✏️ Manual",        callback_data="photo_manual"),
            ]]), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")

async def handle_photo_confirm(u: Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "photo_retry":
        await q.edit_message_text("📸 Envia otra foto del slip.")
        return
    if q.data == "photo_manual":
        await q.edit_message_text("✍️ *Nueva apuesta*\n\n¿Descripcion?", parse_mode="Markdown")
        await track(ctx, q.message)
        return S_DESC
    prefill = ctx.user_data.get("photo_prefill", {})
    s = gs(ctx)
    s["desc"]  = prefill.get("desc", "")
    s["date"]  = str(date.today())
    bookie_raw = prefill.get("bookie", "")
    matched_bk = next((b for b in s["bookies"] if b.lower() in bookie_raw.lower() or bookie_raw.lower() in b.lower()), bookie_raw)
    s["cur_bookie"] = matched_bk
    s["wnx"] = "winamax" in matched_bk.lower()
    s["tickets"] = []
    for t in prefill.get("tickets", []):
        try:
            monto = float(t.get("monto", 0)); cuota = float(t.get("cuota", 0))
            if monto > 0 and cuota > 1:
                pot = round(monto * cuota, 2)
                if s["wnx"]: monto = round(monto * EUR, 2)
                s["tickets"].append({"stake": monto, "cuota": cuota, "potencial": pot,
                    "bookie": matched_bk, "tipster": "", "eur": t.get("monto") if s["wnx"] else None})
        except: pass
    if not s["tickets"]:
        await q.edit_message_text("⚠️ No se detectaron tickets validos. Continua manualmente:")
        return await ask_tipster(q, ctx)
    s["bookie_idx"] = 1; s["n_bookies"] = 1
    await q.edit_message_text(
        f"✅ Detectado: *{s['desc']}* — {len(s['tickets'])} ticket(s)\n\nSelecciona el tipster:",
        parse_mode="Markdown")
    return await ask_tipster(q, ctx)

async def cmd_start(u:Update, ctx):
    if not is_ok(u): return
    await u.message.reply_text("👋 *BetLog*\nUsa los botones del menu.", parse_mode="Markdown", reply_markup=MENU_KB)

async def cmd_nueva(u:Update, ctx):
    if not is_ok(u): return
    rs(ctx); await load_db(ctx)
    await try_del(u.message)
    msg = await u.message.reply_text("✍️ *Nueva apuesta*\n\n¿Descripcion?", parse_mode="Markdown")
    await track(ctx, msg)
    return S_DESC

async def r_desc(u:Update, ctx):
    s = gs(ctx); s["desc"] = u.message.text.strip(); s["date"] = str(date.today())
    await try_del(u.message)
    return await ask_tipster(u, ctx)

async def ask_tipster(src, ctx):
    s = gs(ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    rows = [[InlineKeyboardButton(t, callback_data=f"tip_{t}")] for t in s["tipsters"]] + BACK
    msg  = await src.effective_message.reply_text("👤 *Tipster:*",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_TIPSTER

async def r_tipster(u:Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "back": return await cmd_nueva(u, ctx)
    gs(ctx)["tipster"] = q.data[4:]
    return await ask_n_bookies(u, ctx)

async def r_tipster_txt(u:Update, ctx):
    await try_del(u.message); gs(ctx)["tipster"] = u.message.text.strip()
    return await ask_n_bookies(u, ctx)

async def ask_n_bookies(src, ctx):
    await clear(ctx, src.effective_chat.id, ctx.bot)
    rows = [[InlineKeyboardButton(str(n), callback_data=f"nb_{n}") for n in range(1,6)]] + BACK
    msg  = await src.effective_message.reply_text("🏠 *¿Cuantas bookies?*",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_N_BOOKIES

async def r_n_bookies(u:Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "back": return await ask_tipster(u, ctx)
    s = gs(ctx); s["n_bookies"] = int(q.data[3:]); s["bookie_idx"] = 0
    return await ask_bookie(u, ctx)

async def r_n_bookies_txt(u:Update, ctx):
    try: n = int(u.message.text.strip()); assert 1 <= n <= 10
    except: await u.message.reply_text("⚠️ Numero entre 1 y 10:"); return S_N_BOOKIES
    await try_del(u.message)
    s = gs(ctx); s["n_bookies"] = n; s["bookie_idx"] = 0
    return await ask_bookie(u, ctx)

async def ask_bookie(src, ctx):
    s = gs(ctx); idx = s["bookie_idx"]; n = s["n_bookies"]
    await clear(ctx, src.effective_chat.id, ctx.bot)
    rows = []; row = []
    for b in s["bookies"]:
        row.append(InlineKeyboardButton(b, callback_data=f"bk_{b}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows += BACK
    lbl = f"Bookie {idx+1}/{n}" if n > 1 else "Bookie"
    msg = await src.effective_message.reply_text(f"🏠 *{lbl}:*",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_BOOKIE

async def r_bookie(u:Update, ctx):
    q = u.callback_query; await q.answer(); s = gs(ctx)
    if q.data == "back":
        if s["bookie_idx"] == 0: return await ask_n_bookies(u, ctx)
        s["bookie_idx"] -= 1
        last = s.get("_last_bookie","")
        s["tickets"] = [t for t in s["tickets"] if t.get("bookie") != last]
        return await ask_bookie(u, ctx)
    bk = q.data[3:]; s["cur_bookie"] = bk; s["wnx"] = "winamax" in bk.lower()
    return await ask_tickets(u, ctx)

async def r_bookie_txt(u:Update, ctx):
    await try_del(u.message); s = gs(ctx)
    bk = u.message.text.strip(); s["cur_bookie"] = bk; s["wnx"] = "winamax" in bk.lower()
    return await ask_tickets(u, ctx)

async def ask_tickets(src, ctx):
    s = gs(ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    wnx_note = f"\n_Winamax: ingresa euros (x{EUR} = soles)_" if s["wnx"] else ""
    msg = await src.effective_message.reply_text(
        f"📋 *{s['cur_bookie']}* — tickets:{wnx_note}\n\n"
        f"Una linea por ticket:\n`500 @1.90`  cuota\n`500 285`  retorno total",
        reply_markup=InlineKeyboardMarkup(BACK), parse_mode="Markdown")
    await track(ctx, msg); return S_TICKETS

async def r_tickets(u:Update, ctx):
    s = gs(ctx); await try_del(u.message)
    raw_tickets, errors = parse_tickets(u.message.text, wnx=s["wnx"])
    if errors:
        msg = await u.message.reply_text("⚠️ Corrige y rerenvia:\n" + "\n".join(errors),
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(BACK))
        await track(ctx, msg); return S_TICKETS
    for t in raw_tickets:
        t["bookie"] = s["cur_bookie"]; t["tipster"] = s.get("tipster","")
    s["tickets"].extend(raw_tickets)
    s["_last_bookie"] = s["cur_bookie"]; s["bookie_idx"] += 1
    if s["bookie_idx"] < s["n_bookies"]: return await ask_bookie(u, ctx)
    return await ask_inv(u, ctx)

async def ask_inv(src, ctx):
    s = gs(ctx)
    tipster = s.get("tipster","")
    total_stake = sum(t["stake"] for t in s.get("tickets",[]))
    auto = get_auto_inv_stakes(s, tipster, total_stake)
    if auto:
        for name, data in auto.items():
            s["inv_stakes"][name] = data["stake"]
        all_defined = all(inv["name"] in auto for inv in s.get("investors",[]))
        if all_defined: return await show_confirm(src, ctx)
        s["_auto_stakes"] = auto
        s["_undef_inv"]   = [inv for inv in s["investors"] if inv["name"] not in auto]
        s["_undef_idx"]   = 0
        return await ask_next_undef(src, ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    ts = sum(t["stake"] for t in s["tickets"])
    invs = s["investors"]; names = [i["name"] for i in invs]
    rows = []
    if len(names) >= 1: rows.append([InlineKeyboardButton(f"Solo {names[0]}", callback_data="inv_solo0")])
    if len(names) >= 2:
        rows.append([InlineKeyboardButton(f"Solo {names[1]}", callback_data="inv_solo1")])
        rows.append([InlineKeyboardButton("50/50", callback_data="inv_5050"),
                     InlineKeyboardButton("Sin inversores", callback_data="inv_none")])
    else:
        rows.append([InlineKeyboardButton("Sin inversores", callback_data="inv_none")])
    rows += BACK
    wnx = any(t.get("eur") for t in s["tickets"])
    note = f"Total: {fmt(ts)} soles" + (f" = {fmt(ts/EUR)}€" if wnx else "")
    msg = await src.effective_message.reply_text(
        f"💼 *Inversores*\n_{note}_\n\nO escribe: `Alonso 200 RV 50`",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    await track(ctx, msg); return S_INV

async def ask_next_undef(src, ctx):
    s = gs(ctx); invs = s.get("_undef_inv",[]); idx = s.get("_undef_idx",0)
    if idx >= len(invs): return await show_confirm(src, ctx)
    inv = invs[idx]; auto = s.get("_auto_stakes",{})
    await clear(ctx, src.effective_chat.id, ctx.bot)
    auto_lines = "\n".join([f"  💼 {n}: {fmt(d['stake'])} ({d['pct']}%)" for n,d in auto.items()])
    msg = await src.effective_message.reply_text(
        f"💼 *{inv['name']}* no tiene stake definido para este tipster.\n\n"
        f"_Definidos automaticamente:_\n{auto_lines}\n\n¿Agregar a *{inv['name']}*?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Si, agregar",   callback_data=f"undef_add_{inv['id']}"),
             InlineKeyboardButton("❌ No participar", callback_data=f"undef_skip_{inv['id']}")],
            BACK[0]
        ]), parse_mode="Markdown")
    await track(ctx, msg); return S_INV

async def r_undef_btn(u:Update, ctx):
    q = u.callback_query; await q.answer(); s = gs(ctx)
    if q.data == "back": return await ask_inv(u, ctx)
    invs = s.get("_undef_inv",[]); idx = s.get("_undef_idx",0)
    if idx >= len(invs): return await show_confirm(q, ctx)
    inv = invs[idx]
    if q.data == f"undef_skip_{inv['id']}":
        s["_undef_idx"] += 1; return await ask_next_undef(q, ctx)
    elif q.data == f"undef_add_{inv['id']}":
        await clear(ctx, u.effective_chat.id, ctx.bot)
        msg = await q.message.reply_text(f"💰 Stake de *{inv['name']}* (soles):",
            reply_markup=InlineKeyboardMarkup(BACK), parse_mode="Markdown")
        await track(ctx, msg); s["_adding_undef"] = inv["name"]; return S_INV_MANUAL

async def r_inv_btn(u:Update, ctx):
    q = u.callback_query; await q.answer(); s = gs(ctx)
    if q.data == "back": return await ask_bookie(u, ctx)
    ts = sum(t["stake"] for t in s["tickets"])
    invs = s["investors"]; names = [i["name"] for i in invs]
    if   q.data == "inv_none":  s["inv_stakes"] = {}
    elif q.data == "inv_solo0" and names: s["inv_stakes"] = {names[0]: ts}
    elif q.data == "inv_solo1" and len(names)>1: s["inv_stakes"] = {names[1]: ts}
    elif q.data == "inv_5050"  and len(names)>=2:
        half = round(ts/2,2); s["inv_stakes"] = {names[0]:half, names[1]:half}
    return await show_confirm(u, ctx)

async def r_inv_txt(u:Update, ctx):
    s = gs(ctx); txt = u.message.text.strip(); await try_del(u.message)
    invs = s["investors"]; inv_stakes = {}; parts = txt.split(); i = 0
    while i < len(parts) - 1:
        name_part = parts[i].lower()
        try: amount = float(parts[i+1].replace(",",".")); assert amount >= 0
        except: i += 1; continue
        matched = next((inv["name"] for inv in invs if inv["name"].lower().startswith(name_part)
            or name_part in inv["name"].lower()), None)
        if matched: inv_stakes[matched] = amount
        i += 2
    if not inv_stakes:
        msg = await u.message.reply_text("⚠️ No entendi. Ej: `Alonso 200 RV 50`\nO usa los botones.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(BACK))
        await track(ctx, msg); return S_INV
    s["inv_stakes"] = inv_stakes; return await show_confirm(u, ctx)

async def r_inv_manual(u:Update, ctx):
    s = gs(ctx)
    try: amt = float(u.message.text.strip().replace(",",".")); assert amt >= 0
    except: await u.message.reply_text("⚠️ Numero valido:"); return S_INV_MANUAL
    await try_del(u.message)
    name = s.get("_adding_undef","")
    if name: s["inv_stakes"][name] = amt; s["_adding_undef"] = ""
    s["_undef_idx"] += 1; return await ask_next_undef(u.message, ctx)

async def show_confirm(src, ctx):
    s = gs(ctx)
    await clear(ctx, src.effective_chat.id, ctx.bot)
    s["_dt"] = now_str()
    msg = await src.effective_message.reply_text(build_confirm(s),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Guardar",  callback_data="ok_yes"),
             InlineKeyboardButton("❌ Cancelar", callback_data="ok_no")],
            BACK[0]
        ]), parse_mode="Markdown")
    await track(ctx, msg); return S_CONFIRM

async def r_confirm(u:Update, ctx):
    q = u.callback_query; await q.answer(); s = gs(ctx)
    if q.data == "back": return await ask_inv(u, ctx)
    if q.data == "ok_no":
        await clear(ctx, u.effective_chat.id, ctx.bot)
        rs(ctx); await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    try:
        group_id = gid(); chat_id = u.effective_chat.id
        await sb_post("bet_groups",{"id":group_id,"date":s["date"],"descr":s["desc"],"status":"pending"})
        trows = []
        for t in s["tickets"]:
            tid = gid(); t["_id"] = tid
            trows.append({"id":tid,"group_id":group_id,"tipster":t["tipster"],"casa":t["bookie"],
                "stake":t["stake"],"cuota":t["cuota"],"potencial":t["potencial"],"status":"pending","returned":None})
        await sb_post("tickets", trows)
        ts_total = sum(t["stake"] for t in s["tickets"]); irows = []
        for inv in s["investors"]:
            stake = s["inv_stakes"].get(inv["name"], 0)
            if stake <= 0: continue
            for t in s["tickets"]:
                prop = t["stake"]/ts_total if ts_total > 0 else 1/len(s["tickets"])
                irows.append({"id":gid(),"ticket_id":t["_id"],"investor_id":inv["id"],"stake":round(stake*prop,2)})
        if irows: await sb_post("ticket_investors", irows)
        await clear(ctx, chat_id, ctx.bot)
        await q.edit_message_text(build_confirm(s), parse_mode="Markdown")
        await sb_patch("bet_groups","id",group_id,{"tg_chat_id":chat_id,"tg_msg_id":q.message.message_id})
        rs(ctx)
    except Exception as e:
        await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def cmd_pendientes(u:Update, ctx):
    if not is_ok(u): return
    await try_del(u.message); return await show_pending_list(u.message, ctx)

async def show_pending_list(src, ctx):
    groups   = await sb_get("bet_groups","status=eq.pending&order=date.desc")
    tickets  = await sb_get("tickets","order=group_id.asc")
    inv_rows = await sb_get("ticket_investors","")
    investors= await sb_get("investors","order=name.asc")
    if not isinstance(groups,list) or not groups:
        await src.reply_text("✅ Sin pendientes."); return P_LIST
    tmap = {}
    for t in (tickets if isinstance(tickets,list) else []): tmap.setdefault(t["group_id"],[]).append(t)
    imap = {}
    for ir in (inv_rows if isinstance(inv_rows,list) else []): imap.setdefault(ir["ticket_id"],[]).append(ir)
    inv_names = {i["id"]:i["name"] for i in (investors if isinstance(investors,list) else [])}
    ctx.user_data.update({"pd_groups":groups,"pd_tmap":tmap,"pd_imap":imap,"pd_inv_names":inv_names})
    rows = []
    for g in groups[:10]:
        ts = tmap.get(g["id"],[]); total_s = sum(t["stake"] for t in ts)
        total_p = sum(t["potencial"] for t in ts)
        cuota = round(total_p/total_s,3) if total_s > 0 else 0
        rows.append([InlineKeyboardButton(f"▸ {g['descr'] or '(sin desc)'}  {fmt(total_s)} @{cuota}",
            callback_data=f"pd_{g['id']}")])
    await src.reply_text("⏳ *Pendientes:*", reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
    return P_LIST

async def r_pd_select(u:Update, ctx):
    q = u.callback_query; await q.answer()
    gid_val = q.data[3:]; groups = ctx.user_data.get("pd_groups",[])
    g = next((x for x in groups if x["id"]==gid_val), None)
    if not g: await q.edit_message_text("⚠️ No encontrado."); return ConversationHandler.END
    tmap = ctx.user_data.get("pd_tmap",{}); imap = ctx.user_data.get("pd_imap",{})
    inv_names = ctx.user_data.get("pd_inv_names",{})
    ts = tmap.get(gid_val,[]); total_s = sum(t["stake"] for t in ts)
    total_p = sum(t["potencial"] for t in ts)
    cuota = round(total_p/total_s,3) if total_s > 0 else 0
    inv_totals = {}
    for t in ts:
        for ir in imap.get(t["id"],[]):
            inv_totals[ir["investor_id"]] = inv_totals.get(ir["investor_id"],0) + ir["stake"]
    lines = [f"*{g['descr']}*  _{g['date']}_",""]
    for i,t in enumerate(ts,1):
        lines.append(f"#{i} {t['tipster']} · {t['casa']}  {fmt(t['stake'])} @{t['cuota']} → {fmt(t['potencial'])}")
    lines += ["──────────────", f"@{cuota}  {fmt(total_s)} → pot {fmt(total_p)}"]
    if inv_totals:
        for iid,stake in inv_totals.items():
            pot = round(stake*cuota,2)
            lines.append(f"💼 {inv_names.get(iid,'?')}: {fmt(stake)} → +{fmt(pot-stake)}")
    ctx.user_data.update({"pd_gid":gid_val,"pd_tickets":ts,"pd_cuota":cuota})
    await q.edit_message_text("\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Win",    callback_data="pr_win"),
             InlineKeyboardButton("❌ Loss",   callback_data="pr_loss"),
             InlineKeyboardButton("🔵 Void",   callback_data="pr_void"),
             InlineKeyboardButton("✏️ Exacto", callback_data="pr_exact")],
            [InlineKeyboardButton("🗑 Eliminar", callback_data="pr_del"),
             InlineKeyboardButton("↩ Lista",    callback_data="pr_back")]
        ]), parse_mode="Markdown")
    return P_DETAIL

async def r_pd_action(u:Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "pr_back": return await show_pending_list(q.message, ctx)
    if q.data == "pr_del":
        g = next((x for x in ctx.user_data.get("pd_groups",[]) if x["id"]==ctx.user_data.get("pd_gid","")), {})
        await q.edit_message_text(f"⚠️ ¿Eliminar *{g.get('descr','')}*?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑 Si", callback_data="pdel_yes"),
                InlineKeyboardButton("↩ No",  callback_data="pdel_no")
            ]]), parse_mode="Markdown")
        return P_DEL
    tickets = ctx.user_data.get("pd_tickets",[]); action = q.data[3:]
    ctx.user_data["pd_returns"] = {}; ctx.user_data["pd_idx"] = 0
    if action in ("win","loss","void"):
        for t in tickets:
            if action=="win":    ctx.user_data["pd_returns"][t["id"]] = t["potencial"]
            elif action=="loss": ctx.user_data["pd_returns"][t["id"]] = 0
            elif action=="void": ctx.user_data["pd_returns"][t["id"]] = t["stake"]
        if len(tickets) == 1: return await save_result(q, ctx)
        return await show_result_summary(q, ctx)
    elif action == "exact": return await ask_exact_ticket(q, ctx)

async def ask_exact_ticket(src, ctx):
    tickets = ctx.user_data.get("pd_tickets",[]); idx = ctx.user_data.get("pd_idx",0)
    if idx >= len(tickets): return await show_result_summary(src, ctx)
    t = tickets[idx]
    await edit_or_new(src,
        f"✏️ *#{idx+1}/{len(tickets)}*  {t['tipster']} · {t['casa']}\n"
        f"{fmt(t['stake'])} @{t['cuota']} · pot {fmt(t['potencial'])}\n\nWin / Loss / Void o monto exacto:", [[
        InlineKeyboardButton("✅ Win",  callback_data="ex_win"),
        InlineKeyboardButton("❌ Loss", callback_data="ex_loss"),
        InlineKeyboardButton("🔵 Void", callback_data="ex_void"),
    ]])
    return P_EXACT

async def r_exact_btn(u:Update, ctx):
    q = u.callback_query; await q.answer()
    tickets = ctx.user_data.get("pd_tickets",[]); idx = ctx.user_data.get("pd_idx",0)
    t = tickets[idx]
    if   q.data == "ex_win":  ctx.user_data["pd_returns"][t["id"]] = t["potencial"]
    elif q.data == "ex_loss": ctx.user_data["pd_returns"][t["id"]] = 0
    elif q.data == "ex_void": ctx.user_data["pd_returns"][t["id"]] = t["stake"]
    ctx.user_data["pd_idx"] += 1; return await ask_exact_ticket(q, ctx)

async def r_exact_txt(u:Update, ctx):
    try: ret = float(u.message.text.strip().replace(",",".")); assert ret >= 0
    except: await u.message.reply_text("⚠️ Numero valido:"); return P_EXACT
    await try_del(u.message)
    tickets = ctx.user_data.get("pd_tickets",[]); idx = ctx.user_data.get("pd_idx",0)
    ctx.user_data["pd_returns"][tickets[idx]["id"]] = ret
    ctx.user_data["pd_idx"] += 1; return await ask_exact_ticket(u.message, ctx)

async def show_result_summary(src, ctx):
    tickets = ctx.user_data.get("pd_tickets",[]); rets = ctx.user_data.get("pd_returns",{})
    tr = sum(rets.values()); ts = sum(t["stake"] for t in tickets); pnl = round(tr-ts,2)
    lines = []
    for t in tickets:
        r = rets.get(t["id"],0); s = t["stake"]
        lbl = "✅" if r>s else ("🔵" if abs(r-s)<0.01 else "❌")
        lines.append(f"{lbl} {t['tipster']} · {t['casa']}  {fmt(r)}")
    lines += ["──────────────", f"P&L  *{'+' if pnl>=0 else ''}{fmt(pnl)}*"]
    await edit_or_new(src, "\n".join(lines), [[
        InlineKeyboardButton("✅ Guardar",  callback_data="rs_yes"),
        InlineKeyboardButton("❌ Cancelar", callback_data="rs_no")
    ]])
    return P_CONFIRM_RES

async def r_result_confirm(u:Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "rs_no": await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    return await save_result(q, ctx)

async def save_result(src, ctx):
    tickets = ctx.user_data.get("pd_tickets",[]); rets = ctx.user_data.get("pd_returns",{})
    gid_val = ctx.user_data.get("pd_gid","")
    try:
        grp = await sb_get("bet_groups",f"id=eq.{gid_val}&select=descr,date")
        desc = grp[0]["descr"] if isinstance(grp,list) and grp else "Apuesta"
        orig_d = grp[0].get("date","") if isinstance(grp,list) and grp else ""
        orig_date = (datetime.strptime(orig_d,"%Y-%m-%d").strftime("%d-%m-%Y") + " · " +
                     datetime.now().strftime("%H:%M")) if orig_d else None
        ts_total = sum(t["stake"] for t in tickets)
        tr_total = sum(rets.get(t["id"],0) for t in tickets)
        comb_c = tr_total/ts_total if ts_total > 0 else 1
        for t in tickets:
            await sb_patch("tickets","id",t["id"],{"returned":rets.get(t["id"],0),"status":"settled"})
        inv_group = {}
        investors_db = await sb_get("investors","order=name.asc")
        inv_names_db = {i["id"]:i["name"] for i in (investors_db if isinstance(investors_db,list) else [])}
        for t in tickets:
            rows = await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(rows,list):
                for ir in rows:
                    if ir["investor_id"] not in inv_group:
                        inv_group[ir["investor_id"]] = {"stake":0,"ticket_id":t["id"],
                            "name":inv_names_db.get(ir["investor_id"],"?")}
                    inv_group[ir["investor_id"]]["stake"] += ir["stake"]
        for iid, data in inv_group.items():
            inv_pnl = round(data["stake"] * comb_c - data["stake"], 2)
            if abs(inv_pnl) < 0.01: continue
            await sb_post("investor_movements",{"id":gid(),"investor_id":iid,
                "type":"bet_result","amount":inv_pnl,"note":desc,
                "date":str(date.today()),"ticket_id":data["ticket_id"]})
        await sb_patch("bet_groups","id",gid_val,{"status":"settled"})
        await delete_old_confirm(gid_val, ctx.bot)
        chat_id = src.message.chat_id if hasattr(src,"message") else src.effective_chat.id
        new_msg = await ctx.bot.send_message(chat_id=chat_id,
            text=build_result_msg(desc, tickets, rets, inv_group=inv_group, comb_c=comb_c, orig_date=orig_date),
            parse_mode="Markdown")
        await sb_patch("bet_groups","id",gid_val,{"tg_chat_id":chat_id,"tg_msg_id":new_msg.message_id})
        try:
            if hasattr(src,"message"): await src.message.delete()
        except: pass
    except Exception as e:
        if hasattr(src,"edit_message_text"): await src.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def r_pd_del_confirm(u:Update, ctx):
    q = u.callback_query; await q.answer()
    if q.data == "pdel_no": await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    gid_val = ctx.user_data.get("pd_gid","")
    try:
        tix = await sb_get("tickets",f"group_id=eq.{gid_val}")
        if isinstance(tix,list):
            for t in tix:
                mvs = await sb_get("investor_movements",f"ticket_id=eq.{t['id']}")
                if isinstance(mvs,list):
                    for mv in mvs: await sb_delete("investor_movements","id",mv["id"])
                await sb_delete("ticket_investors","ticket_id",t["id"])
                await sb_delete("tickets","id",t["id"])
        await sb_delete("bet_groups","id",gid_val)
        await q.edit_message_text("✅ Eliminada.")
    except Exception as e: await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def cmd_hoy(u:Update, ctx):
    if not is_ok(u): return
    await try_del(u.message)
    today = str(date.today())
    groups = await sb_get("bet_groups",f"date=eq.{today}&order=created_at.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("📭 Sin apuestas hoy."); return
    tall = await sb_get("tickets","order=group_id.asc"); tmap = {}
    for t in (tall if isinstance(tall,list) else []): tmap.setdefault(t["group_id"],[]).append(t)
    lines = [f"📊 *{today}*\n"]; total = 0; total_pnl = 0
    for g in groups:
        ts = tmap.get(g["id"],[]); gs_s = sum(t["stake"] for t in ts); total += gs_s
        icon = "⏳" if g["status"]=="pending" else "✅"
        pnl_str = ""
        if g["status"] == "settled":
            ret = sum((t.get("returned") or 0) for t in ts)
            p = round(ret-gs_s,2); total_pnl += p
            pnl_str = f"  *{'+' if p>=0 else ''}{fmt(p)}*"
        cuota = round(sum(t["potencial"] for t in ts)/gs_s,3) if gs_s > 0 else 0
        lines.append(f"{icon} *{g['descr'] or '(sin desc)'}*  {fmt(gs_s)} @{cuota}{pnl_str}")
    lines.append(f"\n💰 {fmt(total)}")
    if total_pnl != 0: lines.append(f"📈 P&L  *{'+' if total_pnl>=0 else ''}{fmt(total_pnl)}*")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_cancelar(u:Update, ctx):
    await clear(ctx, u.effective_chat.id, ctx.bot)
    rs(ctx); await u.message.reply_text("❌ Cancelado.", reply_markup=MENU_KB)
    return ConversationHandler.END

async def menu_handler(u:Update, ctx):
    txt = u.message.text
    if txt == "📝 Nueva apuesta": return await cmd_nueva(u, ctx)
    if txt == "⏳ Pendientes":    return await cmd_pendientes(u, ctx)
    if txt == "📊 Hoy":           await cmd_hoy(u, ctx)
    if txt == "❌ Cancelar":      return await cmd_cancelar(u, ctx)

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    nueva_conv = ConversationHandler(
        entry_points=[CommandHandler("nueva",cmd_nueva),
                      MessageHandler(filters.Regex("^📝 Nueva apuesta$"),cmd_nueva)],
        states={
            S_DESC:      [MessageHandler(filters.TEXT&~filters.COMMAND, r_desc)],
            S_TIPSTER:   [CallbackQueryHandler(r_tipster, pattern="^(tip_|back)"),
                          MessageHandler(filters.TEXT&~filters.COMMAND, r_tipster_txt)],
            S_N_BOOKIES: [CallbackQueryHandler(r_n_bookies, pattern="^(nb_|back)"),
                          MessageHandler(filters.TEXT&~filters.COMMAND, r_n_bookies_txt)],
            S_BOOKIE:    [CallbackQueryHandler(r_bookie, pattern="^(bk_|back)"),
                          MessageHandler(filters.TEXT&~filters.COMMAND, r_bookie_txt)],
            S_TICKETS:   [CallbackQueryHandler(r_bookie, pattern="^back"),
                          MessageHandler(filters.TEXT&~filters.COMMAND, r_tickets)],
            S_INV:       [CallbackQueryHandler(r_inv_btn,  pattern="^inv_"),
                          CallbackQueryHandler(r_undef_btn, pattern="^undef_"),
                          CallbackQueryHandler(r_inv_btn,  pattern="^back"),
                          MessageHandler(filters.TEXT&~filters.COMMAND, r_inv_txt)],
            S_INV_MANUAL:[CallbackQueryHandler(r_undef_btn, pattern="^back"),
                          MessageHandler(filters.TEXT&~filters.COMMAND, r_inv_manual)],
            S_CONFIRM:   [CallbackQueryHandler(r_confirm, pattern="^(ok_|back)")],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        conversation_timeout=TIMEOUT,
        allow_reentry=True,
    )

    pending_conv = ConversationHandler(
        entry_points=[CommandHandler("pendientes",cmd_pendientes),
                      MessageHandler(filters.Regex("^⏳ Pendientes$"),cmd_pendientes)],
        states={
            P_LIST:       [CallbackQueryHandler(r_pd_select,      pattern="^pd_")],
            P_DETAIL:     [CallbackQueryHandler(r_pd_action,      pattern="^pr_")],
            P_EXACT:      [CallbackQueryHandler(r_exact_btn,      pattern="^ex_"),
                           MessageHandler(filters.TEXT&~filters.COMMAND, r_exact_txt)],
            P_CONFIRM_RES:[CallbackQueryHandler(r_result_confirm, pattern="^rs_")],
            P_DEL:        [CallbackQueryHandler(r_pd_del_confirm, pattern="^pdel_")],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        conversation_timeout=TIMEOUT,
        allow_reentry=True,
    )

    app.add_handler(nueva_conv)
    app.add_handler(pending_conv)
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("hoy",      cmd_hoy))
    app.add_handler(MessageHandler(filters.Regex("^(📊 Hoy|❌ Cancelar)$"), menu_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_photo_confirm, pattern="^photo_"))

    print("BetLog Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
