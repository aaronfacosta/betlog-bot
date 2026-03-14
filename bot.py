import os
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from datetime import date
import uuid

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
SUPA_URL     = os.environ.get("SUPA_URL", "")
SUPA_KEY     = os.environ.get("SUPA_KEY", "")
ALLOWED_IDS  = [int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()]
EUR_TO_SOLES = 4

# ── STATES ───────────────────────────────────────────────────────────────────
(S_DESC, S_DATE, S_TIPSTER, S_BOOKIE, S_STAKE, S_CUOTA_OR_POT,
 S_CUOTA, S_POTENCIAL, S_MORE_TICKETS, S_INV_STAKE, S_CONFIRM) = range(11)

(SR_PICK_GROUP, SR_PICK_TICKET, SR_SET_RETURN) = range(11, 14)

# ── SUPABASE ─────────────────────────────────────────────────────────────────
def H():
    return {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}",
            "Content-Type": "application/json", "Prefer": "return=minimal"}

async def sb_get(table, params=""):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{SUPA_URL}/rest/v1/{table}?{params}", headers=H())
        return r.json()

async def sb_insert(table, data):
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{SUPA_URL}/rest/v1/{table}", headers=H(), json=data)).status_code

async def sb_insert_many(table, rows):
    async with httpx.AsyncClient() as c:
        return (await c.post(f"{SUPA_URL}/rest/v1/{table}", headers=H(), json=rows)).status_code

async def sb_patch(table, match_col, match_val, data):
    h = H(); h["Prefer"] = "return=minimal"
    async with httpx.AsyncClient() as c:
        return (await c.patch(f"{SUPA_URL}/rest/v1/{table}?{match_col}=eq.{match_val}", headers=h, json=data)).status_code

def gen_id(): return str(uuid.uuid4())[:12].replace("-", "")
def fmt(n):  return f"{n:,.2f}"
def is_ok(u): return not ALLOWED_IDS or u.effective_user.id in ALLOWED_IDS

async def send(src, text, kbd=None, md="Markdown"):
    markup = InlineKeyboardMarkup(kbd) if kbd else None
    if hasattr(src, 'edit_message_text'):
        try: await src.edit_message_text(text, reply_markup=markup, parse_mode=md)
        except: await src.message.reply_text(text, reply_markup=markup, parse_mode=md)
    elif hasattr(src, 'reply_text'):
        await src.reply_text(text, reply_markup=markup, parse_mode=md)
    else:
        await src.message.reply_text(text, reply_markup=markup, parse_mode=md)

# ── SESSION ──────────────────────────────────────────────────────────────────
def gs(ctx):
    if "s" not in ctx.user_data:
        ctx.user_data["s"] = {
            "desc":"","date":str(date.today()),
            "tickets":[],"cur":{},
            "tipsters":[],"bookies":[],"investors":[],
            "inv_idx":0,"inv_stakes":{}
        }
    return ctx.user_data["s"]

def rs(ctx): ctx.user_data["s"] = {
    "desc":"","date":str(date.today()),
    "tickets":[],"cur":{},"tipsters":[],"bookies":[],"investors":[],
    "inv_idx":0,"inv_stakes":{}
}

async def load(ctx):
    s = gs(ctx)
    tp = await sb_get("tipsters","order=name.asc")
    bk = await sb_get("bookies","order=name.asc")
    iv = await sb_get("investors","order=name.asc")
    s["tipsters"]  = [t["name"] for t in tp] if isinstance(tp,list) else []
    s["bookies"]   = [b["name"] for b in bk] if isinstance(bk,list) else []
    s["investors"] = iv if isinstance(iv,list) else []

def combined_cuota(tickets):
    ts = sum(t["stake"] for t in tickets)
    tp = sum(t["potencial"] for t in tickets)
    return round(tp/ts, 3) if ts > 0 else 0

def summary(s):
    tix = s["tickets"]
    ts  = sum(t["stake"] for t in tix)
    tp  = sum(t["potencial"] for t in tix)
    cc  = combined_cuota(tix)
    lines = [f"📋 *{s['desc']}*", f"📅 {s['date']}", ""]
    for i,t in enumerate(tix):
        ss = f"{t['eur']}€ = {fmt(t['stake'])} soles" if t.get("eur") else fmt(t["stake"])
        lines.append(f"*#{i+1}* {t['tipster']} · {t['bookie']}")
        lines.append(f"   {ss} · @{t['cuota']} · Pot: {fmt(t['potencial'])} soles")
    lines += ["", f"📊 Cuota combinada: *@{cc}*",
              f"💰 Total: *{fmt(ts)} soles*",
              f"🎯 Potencial: *{fmt(tp)} soles*",
              f"📈 Ganancia neta: *+{fmt(tp-ts)} soles*"]
    invs = {k:v for k,v in s["inv_stakes"].items() if v>0}
    if invs:
        lines += ["","👥 *Inversores:*"]
        for name,stake in invs.items():
            pot = round(stake*cc, 2)
            gan = round(pot-stake, 2)
            lines.append(f"  • *{name}*: {fmt(stake)} soles → Pot: {fmt(pot)} · +{fmt(gan)}")
    return "\n".join(lines)

# ══════════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(u:Update, ctx):
    if not is_ok(u): return
    await u.message.reply_text(
        "👋 *BetLog Bot*\n\n"
        "/nueva — registrar apuesta\n"
        "/pendientes — ver pendientes\n"
        "/hoy — resumen del día\n"
        "/resultado — marcar resultado\n"
        "/cancelar — cancelar registro",
        parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# /nueva
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_nueva(u:Update, ctx):
    if not is_ok(u): return
    rs(ctx); await load(ctx)
    await u.message.reply_text("✍️ *Nueva apuesta*\n\nDescripción:", parse_mode="Markdown")
    return S_DESC

async def r_desc(u:Update, ctx):
    s=gs(ctx); s["desc"]=u.message.text.strip()
    today=str(date.today())
    await u.message.reply_text("📅 Fecha:", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Hoy ({today})", callback_data=f"dt_{today}"),
        InlineKeyboardButton("Otra fecha", callback_data="dt_custom")
    ]]))
    return S_DATE

async def r_date_cb(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="dt_custom":
        await q.edit_message_text("✏️ Fecha (YYYY-MM-DD):"); return S_DATE
    s["date"]=q.data[3:]; return await ask_tipster(q, ctx)

async def r_date_txt(u:Update, ctx):
    s=gs(ctx)
    try:
        from datetime import datetime; datetime.strptime(u.message.text.strip(),"%Y-%m-%d")
        s["date"]=u.message.text.strip()
    except: await u.message.reply_text("⚠️ Usa YYYY-MM-DD:"); return S_DATE
    return await ask_tipster(u.message, ctx)

async def ask_tipster(src, ctx):
    s=gs(ctx); s["cur"]={"num":len(s["tickets"])+1}
    tips=s["tipsters"]
    if not tips: await send(src,"👤 Tipster:"); return S_TIPSTER
    rows=[[InlineKeyboardButton(t,callback_data=f"tip_{t}")] for t in tips]
    await send(src,"👤 *Tipster:*",rows); return S_TIPSTER

async def r_tip_cb(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gs(ctx)["cur"]["tipster"]=q.data[4:]; return await ask_bookie(q,ctx)

async def r_tip_txt(u:Update, ctx):
    gs(ctx)["cur"]["tipster"]=u.message.text.strip(); return await ask_bookie(u.message,ctx)

async def ask_bookie(src, ctx):
    s=gs(ctx); bks=s["bookies"]
    if not bks: await send(src,"🏠 Bookie:"); return S_BOOKIE
    rows=[]; row=[]
    for b in bks:
        row.append(InlineKeyboardButton(b,callback_data=f"bk_{b}"))
        if len(row)==2: rows.append(row); row=[]
    if row: rows.append(row)
    await send(src,"🏠 *Bookie:*",rows); return S_BOOKIE

async def r_bk_cb(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    bk=q.data[3:]; s["cur"]["bookie"]=bk; s["cur"]["wnx"]="winamax" in bk.lower()
    return await ask_stake(q,ctx)

async def r_bk_txt(u:Update, ctx):
    s=gs(ctx); bk=u.message.text.strip()
    s["cur"]["bookie"]=bk; s["cur"]["wnx"]="winamax" in bk.lower()
    return await ask_stake(u.message,ctx)

async def ask_stake(src, ctx):
    s=gs(ctx)
    lbl="💶 *Euros* a apostar (×4 = soles):" if s["cur"].get("wnx") else "💰 *Stake* en soles:"
    await send(src,lbl); return S_STAKE

async def r_stake(u:Update, ctx):
    s=gs(ctx)
    try: raw=float(u.message.text.strip().replace(",",".")); assert raw>0
    except: await u.message.reply_text("⚠️ Número válido:"); return S_STAKE
    wnx=s["cur"].get("wnx")
    s["cur"]["raw"]=raw; s["cur"]["stake"]=round(raw*EUR_TO_SOLES,2) if wnx else raw
    if wnx: s["cur"]["eur"]=raw; await u.message.reply_text(f"💶 {raw}€ = *{fmt(s['cur']['stake'])} soles*",parse_mode="Markdown")
    await u.message.reply_text("¿Cuota o retorno?",reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("@ Cuota",callback_data="mo_cuota"),
        InlineKeyboardButton("↩ Retorno total",callback_data="mo_ret")
    ]])); return S_CUOTA_OR_POT

async def r_mode_cb(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    s["cur"]["mode"]=q.data[3:]
    if q.data=="mo_cuota": await q.edit_message_text("📊 ¿Cuota? (ej: 1.90)",parse_mode="Markdown"); return S_CUOTA
    await q.edit_message_text("💵 ¿Retorno total? (ej: 285)",parse_mode="Markdown"); return S_POTENCIAL

async def r_cuota(u:Update, ctx):
    s=gs(ctx)
    try: c=float(u.message.text.strip().replace(",",".")); assert c>1
    except: await u.message.reply_text("⚠️ Cuota mayor a 1.0:"); return S_CUOTA
    stake=s["cur"]["stake"]; pot=round(stake*c,2)
    s["cur"]["cuota"]=c; s["cur"]["potencial"]=pot
    await u.message.reply_text(f"✅ @{c} → Pot *{fmt(pot)}* · Ganancia *+{fmt(pot-stake)}*",parse_mode="Markdown")
    return await ask_more(u.message,ctx)

async def r_pot(u:Update, ctx):
    s=gs(ctx)
    try: pot=float(u.message.text.strip().replace(",",".")); assert pot>s["cur"]["stake"]
    except: await u.message.reply_text("⚠️ Retorno mayor al stake:"); return S_POTENCIAL
    stake=s["cur"]["stake"]; c=round(pot/stake,3)
    s["cur"]["cuota"]=c; s["cur"]["potencial"]=pot
    await u.message.reply_text(f"✅ Retorno *{fmt(pot)}* → @{c} · Ganancia *+{fmt(pot-stake)}*",parse_mode="Markdown")
    return await ask_more(u.message,ctx)

async def ask_more(src, ctx):
    s=gs(ctx); s["tickets"].append(dict(s["cur"])); n=len(s["tickets"])
    await send(src,f"Ticket #{n} agregado. ¿Agregar otro?",[[
        InlineKeyboardButton("➕ Otro ticket",callback_data="more_yes"),
        InlineKeyboardButton("✅ Listo",callback_data="more_no")
    ]]); return S_MORE_TICKETS

async def r_more_cb(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="more_yes":
        s["cur"]={"num":len(s["tickets"])+1,"tipster":s["tickets"][0]["tipster"]}
        return await ask_bookie(q,ctx)
    return await ask_inv(q,ctx)

async def ask_inv(src, ctx):
    s=gs(ctx); s["inv_idx"]=0; s["inv_stakes"]={}
    return await ask_next_inv(src,ctx)

async def ask_next_inv(src, ctx):
    s=gs(ctx); invs=s["investors"]; idx=s["inv_idx"]
    if not invs or idx>=len(invs): return await show_confirm(src,ctx)
    inv=invs[idx]; wnx=any(t.get("wnx") for t in s["tickets"])
    lbl=f"💶 Stake de *{inv['name']}* en euros (0 = no participa):" if wnx else f"💰 Stake de *{inv['name']}* (0 = no participa):"
    await send(src,lbl,[[
        InlineKeyboardButton("0 — No participa",callback_data=f"inv0_{inv['id']}"),
        InlineKeyboardButton("Todo el ticket",callback_data=f"invf_{inv['id']}")
    ]]); return S_INV_STAKE

async def r_inv_btn(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    idx=s["inv_idx"]; inv=s["investors"][idx]
    if q.data.startswith("inv0_"): s["inv_stakes"][inv["name"]]=0
    elif q.data.startswith("invf_"):
        total=sum(t["stake"] for t in s["tickets"])
        s["inv_stakes"][inv["name"]]=total
    s["inv_idx"]+=1; return await ask_next_inv(q,ctx)

async def r_inv_txt(u:Update, ctx):
    s=gs(ctx)
    try: raw=float(u.message.text.strip().replace(",",".")); assert raw>=0
    except: await u.message.reply_text("⚠️ Número válido:"); return S_INV_STAKE
    idx=s["inv_idx"]; inv=s["investors"][idx]
    wnx=any(t.get("wnx") for t in s["tickets"])
    soles=round(raw*EUR_TO_SOLES,2) if wnx and raw>0 else raw
    if wnx and raw>0: await u.message.reply_text(f"{inv['name']}: {raw}€ = *{fmt(soles)} soles*",parse_mode="Markdown")
    s["inv_stakes"][inv["name"]]=soles; s["inv_idx"]+=1
    return await ask_next_inv(u.message,ctx)

async def show_confirm(src, ctx):
    s=gs(ctx)
    await send(src, summary(s)+"\n\n¿Guardar?",[[
        InlineKeyboardButton("✅ Confirmar",callback_data="ok_yes"),
        InlineKeyboardButton("❌ Cancelar",callback_data="ok_no")
    ]]); return S_CONFIRM

async def r_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="ok_no": rs(ctx); await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    try:
        gid=gen_id()
        await sb_insert("bet_groups",{"id":gid,"date":s["date"],"descr":s["desc"],"status":"pending"})
        trows=[]
        for t in s["tickets"]:
            tid=gen_id(); t["_id"]=tid
            trows.append({"id":tid,"group_id":gid,"tipster":t["tipster"],"casa":t["bookie"],
                "stake":t["stake"],"cuota":t["cuota"],"potencial":t["potencial"],"status":"pending","returned":None})
        await sb_insert_many("tickets",trows)
        irows=[]
        cc=combined_cuota(s["tickets"])
        for inv in s["investors"]:
            stake=s["inv_stakes"].get(inv["name"],0)
            if stake<=0: continue
            for t in s["tickets"]:
                prop=t["stake"]/sum(x["stake"] for x in s["tickets"])
                irows.append({"id":gen_id(),"ticket_id":t["_id"],"investor_id":inv["id"],"stake":round(stake*prop,2)})
        if irows: await sb_insert_many("ticket_investors",irows)
        ts=sum(t["stake"] for t in s["tickets"]); tp=sum(t["potencial"] for t in s["tickets"])
        await q.edit_message_text(
            f"✅ *¡Guardado!*\n\n📋 {s['desc']}\n💰 {fmt(ts)} soles apostados\n🎯 Potencial: {fmt(tp)} soles",
            parse_mode="Markdown")
        rs(ctx)
    except Exception as e: await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# /resultado
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_resultado(u:Update, ctx):
    if not is_ok(u): return
    groups=await sb_get("bet_groups","status=eq.pending&order=date.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("✅ Sin apuestas pendientes."); return
    ctx.user_data["res_groups"]=groups
    rows=[[InlineKeyboardButton(f"📋 {g['descr'] or g['id'][:8]} ({g['date']})",callback_data=f"rg_{g['id']}")] for g in groups[:10]]
    await u.message.reply_text("⚡ ¿Cuál apuesta liquidar?",reply_markup=InlineKeyboardMarkup(rows))
    return SR_PICK_GROUP

async def r_res_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gid=q.data[3:]; ctx.user_data["res_gid"]=gid
    tickets=await sb_get("tickets",f"group_id=eq.{gid}&order=id.asc")
    if not isinstance(tickets,list) or not tickets:
        await q.edit_message_text("⚠️ Sin tickets en esta apuesta."); return ConversationHandler.END
    ctx.user_data["res_tickets"]=tickets
    ctx.user_data["res_idx"]=0
    ctx.user_data["res_returns"]={}
    return await ask_ticket_result(q,ctx)

async def ask_ticket_result(src, ctx):
    tickets=ctx.user_data["res_tickets"]; idx=ctx.user_data["res_idx"]
    if idx>=len(tickets): return await finalize_resultado(src,ctx)
    t=tickets[idx]
    pot=t["potencial"]; stake=t["stake"]
    msg=(f"🎯 *Ticket #{idx+1}*\n{t['tipster']} · {t['casa']}\n"
         f"Stake: {fmt(stake)} · @{t['cuota']} · Pot: {fmt(pot)}\n\n"
         f"¿Resultado?")
    await send(src, msg,[[
        InlineKeyboardButton("✅ Win",callback_data=f"rr_win"),
        InlineKeyboardButton("❌ Loss",callback_data=f"rr_loss"),
        InlineKeyboardButton("🔵 Void",callback_data=f"rr_void"),
        InlineKeyboardButton("✏️ Monto exacto",callback_data=f"rr_manual"),
    ]]); return SR_PICK_TICKET

async def r_ticket_result(u:Update, ctx):
    q=u.callback_query; await q.answer()
    tickets=ctx.user_data["res_tickets"]; idx=ctx.user_data["res_idx"]
    t=tickets[idx]; stake=t["stake"]; pot=t["potencial"]
    action=q.data[3:]
    if action=="win":   ctx.user_data["res_returns"][t["id"]]=pot
    elif action=="loss": ctx.user_data["res_returns"][t["id"]]=0
    elif action=="void": ctx.user_data["res_returns"][t["id"]]=stake
    elif action=="manual":
        await q.edit_message_text(
            f"✏️ Ticket #{idx+1} — {t['tipster']} · {t['casa']}\nStake: {fmt(stake)}\n\nEscribe el monto retornado exacto:")
        return SR_SET_RETURN
    ctx.user_data["res_idx"]+=1
    return await ask_ticket_result(q,ctx)

async def r_manual_return(u:Update, ctx):
    try: ret=float(u.message.text.strip().replace(",",".")); assert ret>=0
    except: await u.message.reply_text("⚠️ Número válido:"); return SR_SET_RETURN
    tickets=ctx.user_data["res_tickets"]; idx=ctx.user_data["res_idx"]
    t=tickets[idx]; stake=t["stake"]
    ctx.user_data["res_returns"][t["id"]]=ret
    label="Win" if ret>stake else ("Void" if ret==stake else "Loss")
    await u.message.reply_text(f"✅ Ticket #{idx+1}: {fmt(ret)} soles ({label})")
    ctx.user_data["res_idx"]+=1
    return await ask_ticket_result(u.message,ctx)

async def finalize_resultado(src, ctx):
    tickets=ctx.user_data["res_tickets"]
    rets=ctx.user_data["res_returns"]; gid=ctx.user_data["res_gid"]
    total_ret=sum(rets.values()); total_stake=sum(t["stake"] for t in tickets)
    pnl=round(total_ret-total_stake,2)
    status="settled"
    lines=["📊 *Resumen:*",""]
    for t in tickets:
        r=rets.get(t["id"],0); s=t["stake"]
        lbl="✅ Win" if r>s else ("🔵 Void" if abs(r-s)<0.01 else "❌ Loss/Parcial")
        lines.append(f"{lbl} {t['tipster']} · {t['casa']}: {fmt(r)} soles")
    lines+=[f"\nP&L total: *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*"]
    await send(src,"\n".join(lines),[[
        InlineKeyboardButton("✅ Guardar resultado",callback_data="rf_save"),
        InlineKeyboardButton("❌ Cancelar",callback_data="rf_cancel")
    ]]); return SR_PICK_GROUP

async def r_res_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rf_cancel": await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    tickets=ctx.user_data["res_tickets"]; rets=ctx.user_data["res_returns"]; gid=ctx.user_data["res_gid"]
    try:
        for t in tickets:
            r=rets.get(t["id"],0); s=t["stake"]
            await sb_patch("tickets","id",t["id"],{"returned":r,"status":"settled"})
            # Save investor movements
            inv_rows=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(inv_rows,list):
                for ir in inv_rows:
                    inv_stake=ir["stake"]; prop=inv_stake/s if s>0 else 0
                    inv_ret=round(r*prop,2); inv_pnl=round(inv_ret-inv_stake,2)
                    if inv_pnl!=0:
                        await sb_insert("investor_movements",{
                            "id":gen_id(),"investor_id":ir["investor_id"],
                            "type":"bet_result","amount":inv_pnl,
                            "note":f"Resultado apuesta","date":str(date.today()),
                            "ticket_id":t["id"]})
        await sb_patch("bet_groups","id",gid,{"status":"settled"})
        total_ret=sum(rets.values()); total_stake=sum(t["stake"] for t in tickets)
        pnl=round(total_ret-total_stake,2)
        await q.edit_message_text(
            f"✅ *Resultado guardado*\n\nP&L: *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*",
            parse_mode="Markdown")
    except Exception as e: await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# /pendientes  /hoy
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_pendientes(u:Update, ctx):
    if not is_ok(u): return
    groups=await sb_get("bet_groups","status=eq.pending&order=date.desc")
    tickets=await sb_get("tickets","status=eq.pending")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("✅ Sin pendientes."); return
    tmap={}
    for t in (tickets if isinstance(tickets,list) else []): tmap.setdefault(t["group_id"],[]).append(t)
    lines=[f"⏳ *{len(groups)} pendiente(s):*\n"]
    for g in groups[:10]:
        ts=tmap.get(g["id"],[])
        s=sum(t["stake"] for t in ts); p=sum(t["potencial"] for t in ts)
        lines.append(f"📋 *{g['descr'] or '(sin desc)'}*  📅 {g['date']}")
        lines.append(f"   Stake: {fmt(s)} · Pot: {fmt(p)}")
        for t in ts: lines.append(f"   └ {t['tipster']} · {t['casa']} · @{t['cuota']}")
        lines.append("")
    await u.message.reply_text("\n".join(lines),parse_mode="Markdown")

async def cmd_hoy(u:Update, ctx):
    if not is_ok(u): return
    today=str(date.today())
    groups=await sb_get("bet_groups",f"date=eq.{today}&order=created_at.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text(f"📭 Sin apuestas hoy ({today})."); return
    tall=await sb_get("tickets","order=group_id.asc")
    tmap={}
    for t in (tall if isinstance(tall,list) else []): tmap.setdefault(t["group_id"],[]).append(t)
    lines=[f"📊 *Hoy ({today}):*\n"]; total=0
    for g in groups:
        ts=tmap.get(g["id"],[]); gs=sum(t["stake"] for t in ts); total+=gs
        icon="⏳" if g["status"]=="pending" else "✅"
        lines.append(f"{icon} *{g['descr'] or '(sin desc)'}* — {fmt(gs)} soles")
    lines.append(f"\n💰 Total: *{fmt(total)} soles*")
    await u.message.reply_text("\n".join(lines),parse_mode="Markdown")

async def cmd_cancelar(u:Update, ctx):
    rs(ctx); await u.message.reply_text("❌ Cancelado."); return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    app=Application.builder().token(BOT_TOKEN).build()
    conv=ConversationHandler(
        entry_points=[CommandHandler("nueva",cmd_nueva)],
        states={
            S_DESC:        [MessageHandler(filters.TEXT&~filters.COMMAND,r_desc)],
            S_DATE:        [CallbackQueryHandler(r_date_cb,pattern="^dt_"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_date_txt)],
            S_TIPSTER:     [CallbackQueryHandler(r_tip_cb,pattern="^tip_"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_tip_txt)],
            S_BOOKIE:      [CallbackQueryHandler(r_bk_cb,pattern="^bk_"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_bk_txt)],
            S_STAKE:       [MessageHandler(filters.TEXT&~filters.COMMAND,r_stake)],
            S_CUOTA_OR_POT:[CallbackQueryHandler(r_mode_cb,pattern="^mo_")],
            S_CUOTA:       [MessageHandler(filters.TEXT&~filters.COMMAND,r_cuota)],
            S_POTENCIAL:   [MessageHandler(filters.TEXT&~filters.COMMAND,r_pot)],
            S_MORE_TICKETS:[CallbackQueryHandler(r_more_cb,pattern="^more_")],
            S_INV_STAKE:   [CallbackQueryHandler(r_inv_btn,pattern="^inv(0|f)_"),
                            MessageHandler(filters.TEXT&~filters.COMMAND,r_inv_txt)],
            S_CONFIRM:     [CallbackQueryHandler(r_confirm,pattern="^ok_")],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )
    res_conv=ConversationHandler(
        entry_points=[CommandHandler("resultado",cmd_resultado)],
        states={
            SR_PICK_GROUP: [CallbackQueryHandler(r_res_group,pattern="^rg_"),
                            CallbackQueryHandler(r_res_confirm,pattern="^rf_")],
            SR_PICK_TICKET:[CallbackQueryHandler(r_ticket_result,pattern="^rr_")],
            SR_SET_RETURN: [MessageHandler(filters.TEXT&~filters.COMMAND,r_manual_return)],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(res_conv)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("pendientes",cmd_pendientes))
    app.add_handler(CommandHandler("hoy",cmd_hoy))
    app.add_handler(CommandHandler("cancelar",cmd_cancelar))
    print("BetLog Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
