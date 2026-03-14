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

(S_DESC, S_TIPSTER, S_BOOKIE, S_STAKE, S_CUOTA_OR_POT,
 S_CUOTA, S_POTENCIAL, S_MORE_TICKETS, S_INV_STAKE, S_CONFIRM) = range(10)

(SR_PICK_GROUP, SR_PICK_TICKET, SR_SET_RETURN) = range(10, 13)
(SE_PICK_GROUP, SE_PICK_FIELD, SE_SET_VALUE) = range(13, 16)
(SC_PICK_GROUP, SC_PICK_TICKET, SC_SET_RETURN) = range(16, 19)

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

async def sb_delete(table, match_col, match_val):
    async with httpx.AsyncClient() as c:
        return (await c.delete(f"{SUPA_URL}/rest/v1/{table}?{match_col}=eq.{match_val}", headers=H())).status_code

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

async def delete_msg(src):
    try:
        if hasattr(src, 'delete'): await src.delete()
        elif hasattr(src, 'message'): await src.message.delete()
    except: pass

def gs(ctx):
    if "s" not in ctx.user_data:
        ctx.user_data["s"] = {"desc":"","date":str(date.today()),
            "tickets":[],"cur":{},"tipsters":[],"bookies":[],"investors":[],
            "inv_idx":0,"inv_stakes":{},"msg_ids":[]}
    return ctx.user_data["s"]

def rs(ctx): ctx.user_data["s"] = {"desc":"","date":str(date.today()),
    "tickets":[],"cur":{},"tipsters":[],"bookies":[],"investors":[],
    "inv_idx":0,"inv_stakes":{},"msg_ids":[]}

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

def build_summary(s):
    tix = s["tickets"]
    ts  = sum(t["stake"] for t in tix)
    tp  = sum(t["potencial"] for t in tix)
    cc  = combined_cuota(tix)
    lines = [f"📋 *{s['desc']}*", f"📅 {s['date']}", ""]
    for i,t in enumerate(tix):
        ss = f"{t['eur']}€ = {fmt(t['stake'])} soles" if t.get("eur") else fmt(t["stake"])
        lines.append(f"  └ {t['tipster']} · {t['bookie']} · stake {ss} · @{t['cuota']} · pot {fmt(t['potencial'])}")
    lines += ["",
              f"📊 Cuota combinada: *@{cc}*",
              f"💰 Total apostado: *{fmt(ts)} soles*",
              f"🎯 Potencial: *{fmt(tp)} soles*",
              f"📈 Ganancia neta: *+{fmt(tp-ts)} soles*"]
    invs = {k:v for k,v in s["inv_stakes"].items() if v>0}
    if invs:
        lines += ["", "👥 *Inversores:*"]
        for name,stake in invs.items():
            pot = round(stake*cc, 2)
            gan = round(pot-stake, 2)
            # show per-ticket breakdown
            for i,t in enumerate(tix):
                prop = t["stake"]/ts if ts>0 else 1/len(tix)
                t_stake = round(stake*prop, 2)
                lines.append(f"  • *{name}* ticket #{i+1}: {fmt(t_stake)} soles")
            lines.append(f"  → Total {name}: {fmt(stake)} · Pot: {fmt(pot)} · +{fmt(gan)}")
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
        "/editar — editar apuesta pendiente\n"
        "/corregir — corregir resultado ya marcado\n"
        "/cancelar — cancelar registro en curso",
        parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# /nueva
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_nueva(u:Update, ctx):
    if not is_ok(u): return
    rs(ctx); await load(ctx)
    msg = await u.message.reply_text("✍️ *Nueva apuesta*\n\nDescripción:", parse_mode="Markdown")
    gs(ctx)["msg_ids"].append(msg.message_id)
    return S_DESC

async def r_desc(u:Update, ctx):
    s=gs(ctx); s["desc"]=u.message.text.strip()
    s["date"]=str(date.today())
    await u.message.delete()
    return await ask_tipster(u.message, ctx)

async def ask_tipster(src, ctx):
    s=gs(ctx); s["cur"]={"num":len(s["tickets"])+1}
    tips=s["tipsters"]
    if not tips:
        msg = await src.reply_text("👤 Tipster:")
        return S_TIPSTER
    rows=[[InlineKeyboardButton(t,callback_data=f"tip_{t}")] for t in tips]
    await send(src,"👤 *Tipster:*",rows); return S_TIPSTER

async def r_tip_cb(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gs(ctx)["cur"]["tipster"]=q.data[4:]
    return await ask_bookie(q,ctx)

async def r_tip_txt(u:Update, ctx):
    await u.message.delete()
    gs(ctx)["cur"]["tipster"]=u.message.text.strip()
    return await ask_bookie(u.message,ctx)

async def ask_bookie(src, ctx):
    s=gs(ctx); bks=s["bookies"]
    if not bks:
        await send(src,"🏠 Bookie:"); return S_BOOKIE
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
    await u.message.delete()
    s=gs(ctx); bk=u.message.text.strip()
    s["cur"]["bookie"]=bk; s["cur"]["wnx"]="winamax" in bk.lower()
    return await ask_stake(u.message,ctx)

async def ask_stake(src, ctx):
    s=gs(ctx)
    lbl = "💶 *Euros* a apostar (×4 = soles):" if s["cur"].get("wnx") else f"💰 *Stake* ticket #{s['cur']['num']} (soles):"
    await send(src,lbl); return S_STAKE

async def r_stake(u:Update, ctx):
    s=gs(ctx)
    try: raw=float(u.message.text.strip().replace(",",".")); assert raw>0
    except: await u.message.reply_text("⚠️ Número válido:"); return S_STAKE
    await u.message.delete()
    wnx=s["cur"].get("wnx")
    s["cur"]["raw"]=raw; s["cur"]["stake"]=round(raw*EUR_TO_SOLES,2) if wnx else raw
    if wnx: s["cur"]["eur"]=raw
    await send(u.message,
        (f"💶 {raw}€ = *{fmt(s['cur']['stake'])} soles*\n\n" if wnx else "") +
        "¿Cuota o retorno total?",[[
        InlineKeyboardButton("@ Cuota",callback_data="mo_cuota"),
        InlineKeyboardButton("↩ Retorno total",callback_data="mo_ret")
    ]]); return S_CUOTA_OR_POT

async def r_mode_cb(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    s["cur"]["mode"]=q.data[3:]
    if q.data=="mo_cuota":
        await q.edit_message_text(f"📊 Cuota ticket #{s['cur']['num']} (ej: 1.90):",parse_mode="Markdown")
        return S_CUOTA
    await q.edit_message_text(f"💵 Retorno total ticket #{s['cur']['num']} (ej: 285):",parse_mode="Markdown")
    return S_POTENCIAL

async def r_cuota(u:Update, ctx):
    s=gs(ctx)
    try: c=float(u.message.text.strip().replace(",",".")); assert c>1
    except: await u.message.reply_text("⚠️ Cuota mayor a 1.0:"); return S_CUOTA
    stake=s["cur"]["stake"]; pot=round(stake*c,2)
    s["cur"]["cuota"]=c; s["cur"]["potencial"]=pot
    await u.message.delete()
    await send(u.message,f"✅ Ticket #{s['cur']['num']}: @{c} · Pot *{fmt(pot)}* · +*{fmt(pot-stake)}*\n\n¿Agregar otro ticket?",[[
        InlineKeyboardButton("➕ Otro ticket",callback_data="more_yes"),
        InlineKeyboardButton("✅ Listo",callback_data="more_no")
    ]]); s["tickets"].append(dict(s["cur"])); return S_MORE_TICKETS

async def r_pot(u:Update, ctx):
    s=gs(ctx)
    try: pot=float(u.message.text.strip().replace(",",".")); assert pot>s["cur"]["stake"]
    except: await u.message.reply_text("⚠️ Retorno mayor al stake:"); return S_POTENCIAL
    stake=s["cur"]["stake"]; c=round(pot/stake,3)
    s["cur"]["cuota"]=c; s["cur"]["potencial"]=pot
    await u.message.delete()
    await send(u.message,f"✅ Ticket #{s['cur']['num']}: @{c} · Pot *{fmt(pot)}* · +*{fmt(pot-stake)}*\n\n¿Agregar otro ticket?",[[
        InlineKeyboardButton("➕ Otro ticket",callback_data="more_yes"),
        InlineKeyboardButton("✅ Listo",callback_data="more_no")
    ]]); s["tickets"].append(dict(s["cur"])); return S_MORE_TICKETS

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
    ts=sum(t["stake"] for t in s["tickets"])
    lbl = f"💶 Inversión de *{inv['name']}* en euros\n_(total apuesta = {fmt(ts/EUR_TO_SOLES)}€, 0 = no participa)_" if wnx else f"💰 Inversión de *{inv['name']}* en soles\n_(total apuesta = {fmt(ts)}, 0 = no participa)_"
    await send(src,lbl,[[
        InlineKeyboardButton("0 — No participa",callback_data=f"inv0_{inv['id']}"),
        InlineKeyboardButton("Todo",callback_data=f"invf_{inv['id']}")
    ]]); return S_INV_STAKE

async def r_inv_btn(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    idx=s["inv_idx"]; inv=s["investors"][idx]
    if q.data.startswith("inv0_"): s["inv_stakes"][inv["name"]]=0
    elif q.data.startswith("invf_"):
        s["inv_stakes"][inv["name"]]=sum(t["stake"] for t in s["tickets"])
    s["inv_idx"]+=1; return await ask_next_inv(q,ctx)

async def r_inv_txt(u:Update, ctx):
    s=gs(ctx)
    try: raw=float(u.message.text.strip().replace(",",".")); assert raw>=0
    except: await u.message.reply_text("⚠️ Número válido:"); return S_INV_STAKE
    await u.message.delete()
    idx=s["inv_idx"]; inv=s["investors"][idx]
    wnx=any(t.get("wnx") for t in s["tickets"])
    soles=round(raw*EUR_TO_SOLES,2) if wnx and raw>0 else raw
    s["inv_stakes"][inv["name"]]=soles; s["inv_idx"]+=1
    return await ask_next_inv(u.message,ctx)

async def show_confirm(src, ctx):
    s=gs(ctx)
    await send(src, build_summary(s)+"\n\n¿Guardar?",[[
        InlineKeyboardButton("✅ Confirmar",callback_data="ok_yes"),
        InlineKeyboardButton("❌ Cancelar",callback_data="ok_no")
    ]]); return S_CONFIRM

async def r_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer(); s=gs(ctx)
    if q.data=="ok_no":
        rs(ctx)
        await q.edit_message_text("❌ Cancelado.")
        return ConversationHandler.END
    try:
        gid=gen_id()
        await sb_insert("bet_groups",{"id":gid,"date":s["date"],"descr":s["desc"],"status":"pending"})
        trows=[]
        for t in s["tickets"]:
            tid=gen_id(); t["_id"]=tid
            trows.append({"id":tid,"group_id":gid,"tipster":t["tipster"],"casa":t["bookie"],
                "stake":t["stake"],"cuota":t["cuota"],"potencial":t["potencial"],"status":"pending","returned":None})
        await sb_insert_many("tickets",trows)
        ts_total=sum(t["stake"] for t in s["tickets"])
        irows=[]
        for inv in s["investors"]:
            stake=s["inv_stakes"].get(inv["name"],0)
            if stake<=0: continue
            for t in s["tickets"]:
                prop=t["stake"]/ts_total if ts_total>0 else 1/len(s["tickets"])
                irows.append({"id":gen_id(),"ticket_id":t["_id"],"investor_id":inv["id"],"stake":round(stake*prop,2)})
        if irows: await sb_insert_many("ticket_investors",irows)
        # Final confirmation — permanent message
        await q.edit_message_text(build_summary(s) + "\n\n✅ *Apuesta registrada.*", parse_mode="Markdown")
        rs(ctx)
    except Exception as e:
        await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# /pendientes
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_pendientes(u:Update, ctx):
    if not is_ok(u): return
    groups  = await sb_get("bet_groups","status=eq.pending&order=date.desc")
    tickets = await sb_get("tickets","status=eq.pending&order=group_id.asc")
    inv_rows = await sb_get("ticket_investors","")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("✅ Sin apuestas pendientes."); return
    tmap={}
    for t in (tickets if isinstance(tickets,list) else []):
        tmap.setdefault(t["group_id"],[]).append(t)
    imap={}
    for ir in (inv_rows if isinstance(inv_rows,list) else []):
        imap.setdefault(ir["ticket_id"],[]).append(ir)
    # Load investor names
    investors = await sb_get("investors","order=name.asc")
    inv_names = {i["id"]:i["name"] for i in (investors if isinstance(investors,list) else [])}
    lines=[f"⏳ *{len(groups)} pendiente(s):*\n"]
    for g in groups[:10]:
        ts=tmap.get(g["id"],[])
        total_stake=sum(t["stake"] for t in ts)
        total_pot=sum(t["potencial"] for t in ts)
        cc=round(total_pot/total_stake,3) if total_stake>0 else 0
        lines.append(f"📋 *{g['descr'] or '(sin desc)'}*  📅 {g['date']}")
        lines.append(f"   Stake total: {fmt(total_stake)} · Pot: {fmt(total_pot)} · @{cc}")
        for t in ts:
            lines.append(f"   └ {t['tipster']} · {t['casa']} · stake {fmt(t['stake'])} · @{t['cuota']}")
        # Investor breakdown
        inv_totals={}
        for t in ts:
            for ir in imap.get(t["id"],[]):
                name=inv_names.get(ir["investor_id"],"?")
                inv_totals[name]=inv_totals.get(name,0)+ir["stake"]
        if inv_totals:
            for name,stake in inv_totals.items():
                pot=round(stake*cc,2); gan=round(pot-stake,2)
                lines.append(f"   💼 {name}: {fmt(stake)} soles · Pot: {fmt(pot)} · +{fmt(gan)}")
        lines.append("")
    await u.message.reply_text("\n".join(lines),parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# /hoy
# ══════════════════════════════════════════════════════════════════════════════
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
        pnl=""
        if g["status"]=="settled":
            ret=sum(t.get("returned") or 0 for t in ts)
            p=round(ret-gs,2)
            pnl=f" · P&L: {'+' if p>=0 else ''}{fmt(p)}"
        lines.append(f"{icon} *{g['descr'] or '(sin desc)'}* — {fmt(gs)} soles{pnl}")
        for t in ts:
            lines.append(f"   └ {t['tipster']} · {t['casa']} · @{t['cuota']}")
    lines.append(f"\n💰 Total apostado: *{fmt(total)} soles*")
    await u.message.reply_text("\n".join(lines),parse_mode="Markdown")

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
    rows.append([InlineKeyboardButton("🗑 Eliminar apuesta",callback_data="rg_delete_mode")])
    await u.message.reply_text("⚡ ¿Cuál apuesta liquidar?",reply_markup=InlineKeyboardMarkup(rows))
    return SR_PICK_GROUP

async def r_res_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rg_delete_mode":
        groups=ctx.user_data.get("res_groups",[])
        rows=[[InlineKeyboardButton(f"🗑 {g['descr'] or g['id'][:8]} ({g['date']})",callback_data=f"rd_{g['id']}")] for g in groups[:10]]
        rows.append([InlineKeyboardButton("↩ Cancelar",callback_data="rd_cancel")])
        await q.edit_message_text("🗑 ¿Cuál eliminar?",reply_markup=InlineKeyboardMarkup(rows))
        return SR_PICK_GROUP
    gid=q.data[3:]; ctx.user_data["res_gid"]=gid
    tickets=await sb_get("tickets",f"group_id=eq.{gid}&order=id.asc")
    if not isinstance(tickets,list) or not tickets:
        await q.edit_message_text("⚠️ Sin tickets."); return ConversationHandler.END
    ctx.user_data["res_tickets"]=tickets; ctx.user_data["res_idx"]=0; ctx.user_data["res_returns"]={}
    return await ask_ticket_result(q,ctx)

async def ask_ticket_result(src, ctx):
    tickets=ctx.user_data["res_tickets"]; idx=ctx.user_data["res_idx"]
    if idx>=len(tickets): return await finalize_resultado(src,ctx)
    t=tickets[idx]
    await send(src,
        f"🎯 *Ticket #{idx+1}/{len(tickets)}*\n{t['tipster']} · {t['casa']}\n"
        f"Stake: {fmt(t['stake'])} · @{t['cuota']} · Pot: {fmt(t['potencial'])}\n\n¿Resultado?",[[
        InlineKeyboardButton("✅ Win",callback_data="rr_win"),
        InlineKeyboardButton("❌ Loss",callback_data="rr_loss"),
        InlineKeyboardButton("🔵 Void",callback_data="rr_void"),
        InlineKeyboardButton("✏️ Monto exacto",callback_data="rr_manual"),
    ]]); return SR_PICK_TICKET

async def r_ticket_result(u:Update, ctx):
    q=u.callback_query; await q.answer()
    tickets=ctx.user_data["res_tickets"]; idx=ctx.user_data["res_idx"]
    t=tickets[idx]; stake=t["stake"]; pot=t["potencial"]
    action=q.data[3:]
    if action=="win":    ctx.user_data["res_returns"][t["id"]]=pot
    elif action=="loss": ctx.user_data["res_returns"][t["id"]]=0
    elif action=="void": ctx.user_data["res_returns"][t["id"]]=stake
    elif action=="manual":
        await q.edit_message_text(f"✏️ Ticket #{idx+1} — {t['tipster']} · {t['casa']}\nStake: {fmt(stake)}\n\nMonto retornado exacto:")
        return SR_SET_RETURN
    ctx.user_data["res_idx"]+=1
    return await ask_ticket_result(q,ctx)

async def r_manual_return(u:Update, ctx):
    try: ret=float(u.message.text.strip().replace(",",".")); assert ret>=0
    except: await u.message.reply_text("⚠️ Número válido:"); return SR_SET_RETURN
    await u.message.delete()
    tickets=ctx.user_data["res_tickets"]; idx=ctx.user_data["res_idx"]
    t=tickets[idx]; stake=t["stake"]
    ctx.user_data["res_returns"][t["id"]]=ret
    ctx.user_data["res_idx"]+=1
    return await ask_ticket_result(u.message,ctx)

async def finalize_resultado(src, ctx):
    tickets=ctx.user_data["res_tickets"]; rets=ctx.user_data["res_returns"]
    total_ret=sum(rets.values()); total_stake=sum(t["stake"] for t in tickets)
    pnl=round(total_ret-total_stake,2)
    lines=["📊 *Resumen resultado:*",""]
    for t in tickets:
        r=rets.get(t["id"],0); s=t["stake"]
        lbl="✅ Win" if r>s else ("🔵 Void" if abs(r-s)<0.01 else "❌ Loss")
        lines.append(f"{lbl} {t['tipster']} · {t['casa']}: devuelto {fmt(r)} soles")
    lines.append(f"\nP&L total: *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*")
    await send(src,"\n".join(lines),[[
        InlineKeyboardButton("✅ Guardar",callback_data="rf_save"),
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
            inv_rows=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(inv_rows,list):
                for ir in inv_rows:
                    inv_stake=ir["stake"]; prop=inv_stake/s if s>0 else 0
                    inv_ret=round(r*prop,2); inv_pnl=round(inv_ret-inv_stake,2)
                    if inv_pnl!=0:
                        await sb_insert("investor_movements",{
                            "id":gen_id(),"investor_id":ir["investor_id"],
                            "type":"bet_result","amount":inv_pnl,
                            "note":"Resultado apuesta","date":str(date.today()),
                            "ticket_id":t["id"]})
        await sb_patch("bet_groups","id",gid,{"status":"settled"})
        total_ret=sum(rets.values()); total_stake=sum(t["stake"] for t in tickets)
        pnl=round(total_ret-total_stake,2)
        await q.edit_message_text(
            f"✅ *Resultado guardado*\n\nP&L: *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*",
            parse_mode="Markdown")
    except Exception as e: await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def r_delete_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rd_cancel": await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    gid=q.data[3:]
    groups=ctx.user_data.get("res_groups",[])
    g=next((x for x in groups if x["id"]==gid),None)
    desc=g["descr"] if g else gid
    await q.edit_message_text(f"⚠️ ¿Confirmas eliminar *{desc}*?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Sí, eliminar",callback_data=f"rdc_{gid}"),
            InlineKeyboardButton("↩ No",callback_data="rd_cancel")
        ]]),parse_mode="Markdown")
    return SR_PICK_GROUP

async def r_delete_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="rd_cancel": await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    gid=q.data[4:]
    try:
        tickets=await sb_get("tickets",f"group_id=eq.{gid}")
        if isinstance(tickets,list):
            for t in tickets:
                mvs=await sb_get("investor_movements",f"ticket_id=eq.{t['id']}")
                if isinstance(mvs,list):
                    for mv in mvs: await sb_delete("investor_movements","id",mv["id"])
                await sb_delete("ticket_investors","ticket_id",t["id"])
                await sb_delete("tickets","id",t["id"])
        await sb_delete("bet_groups","id",gid)
        await q.edit_message_text("✅ *Apuesta eliminada.*",parse_mode="Markdown")
    except Exception as e: await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# /editar
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_editar(u:Update, ctx):
    if not is_ok(u): return
    groups=await sb_get("bet_groups","status=eq.pending&order=date.desc")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("✅ Sin apuestas pendientes para editar."); return
    ctx.user_data["edit_groups"]=groups
    rows=[[InlineKeyboardButton(f"✎ {g['descr'] or g['id'][:8]} ({g['date']})",callback_data=f"eg_{g['id']}")] for g in groups[:10]]
    await u.message.reply_text("✎ ¿Cuál apuesta editar?",reply_markup=InlineKeyboardMarkup(rows))
    return SE_PICK_GROUP

async def r_edit_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gid=q.data[3:]; ctx.user_data["edit_gid"]=gid
    tickets=await sb_get("tickets",f"group_id=eq.{gid}&order=id.asc")
    groups=ctx.user_data.get("edit_groups",[])
    g=next((x for x in groups if x["id"]==gid),{})
    ctx.user_data["edit_group"]=g
    ctx.user_data["edit_tickets"]=tickets if isinstance(tickets,list) else []
    rows=[[InlineKeyboardButton("📝 Descripción",callback_data="ef_desc")]]
    for i,t in enumerate(ctx.user_data["edit_tickets"]):
        rows.append([InlineKeyboardButton(f"Ticket #{i+1}: {t['tipster']} @{t['cuota']}",callback_data=f"et_{t['id']}")])
    await q.edit_message_text("✎ ¿Qué editar?",reply_markup=InlineKeyboardMarkup(rows))
    return SE_PICK_FIELD

async def r_edit_field(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="ef_desc":
        ctx.user_data["edit_field"]="desc"
        ctx.user_data["edit_tid"]=None
        g=ctx.user_data.get("edit_group",{})
        await q.edit_message_text(f"📝 Descripción actual: *{g.get('descr','')}*\n\nNueva descripción:",parse_mode="Markdown")
        return SE_SET_VALUE
    tid=q.data[3:]; ctx.user_data["edit_tid"]=tid
    tickets=ctx.user_data.get("edit_tickets",[])
    t=next((x for x in tickets if x["id"]==tid),{})
    ctx.user_data["edit_ticket"]=t
    rows=[[
        InlineKeyboardButton(f"Stake ({fmt(t.get('stake',0))})",callback_data="ef_stake"),
        InlineKeyboardButton(f"Cuota ({t.get('cuota',0)})",callback_data="ef_cuota"),
    ],[
        InlineKeyboardButton(f"Bookie ({t.get('casa','')})",callback_data="ef_bookie"),
    ]]
    await q.edit_message_text(f"✎ Ticket: {t.get('tipster','')} · {t.get('casa','')} · @{t.get('cuota','')}",
        reply_markup=InlineKeyboardMarkup(rows))
    return SE_PICK_FIELD

async def r_edit_subfield(u:Update, ctx):
    q=u.callback_query; await q.answer()
    field=q.data[3:]
    ctx.user_data["edit_field"]=field
    t=ctx.user_data.get("edit_ticket",{})
    labels={"stake":f"Stake actual: {fmt(t.get('stake',0))} soles\n\nNuevo stake:",
            "cuota":f"Cuota actual: {t.get('cuota',0)}\n\nNueva cuota:",
            "bookie":f"Bookie actual: {t.get('casa','')}\n\nNuevo bookie:"}
    await q.edit_message_text(labels.get(field,"Nuevo valor:"),parse_mode="Markdown")
    return SE_SET_VALUE

async def r_edit_value(u:Update, ctx):
    s=ctx.user_data; field=s.get("edit_field"); val=u.message.text.strip()
    await u.message.delete()
    try:
        if field=="desc":
            await sb_patch("bet_groups","id",s["edit_gid"],{"descr":val})
            await u.message.reply_text(f"✅ Descripción actualizada: *{val}*",parse_mode="Markdown")
        elif field=="stake":
            new_stake=float(val.replace(",",".")); assert new_stake>0
            t=s.get("edit_ticket",{}); new_pot=round(new_stake*t.get("cuota",1),2)
            await sb_patch("tickets","id",s["edit_tid"],{"stake":new_stake,"potencial":new_pot})
            await u.message.reply_text(f"✅ Stake actualizado: {fmt(new_stake)} · Pot: {fmt(new_pot)}",parse_mode="Markdown")
        elif field=="cuota":
            new_cuota=float(val.replace(",",".")); assert new_cuota>1
            t=s.get("edit_ticket",{}); new_pot=round(t.get("stake",0)*new_cuota,2)
            await sb_patch("tickets","id",s["edit_tid"],{"cuota":new_cuota,"potencial":new_pot})
            await u.message.reply_text(f"✅ Cuota actualizada: @{new_cuota} · Pot: {fmt(new_pot)}",parse_mode="Markdown")
        elif field=="bookie":
            await sb_patch("tickets","id",s["edit_tid"],{"casa":val})
            await u.message.reply_text(f"✅ Bookie actualizado: {val}",parse_mode="Markdown")
    except Exception as e:
        await u.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# /corregir
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_corregir(u:Update, ctx):
    if not is_ok(u): return
    groups=await sb_get("bet_groups","status=eq.settled&order=date.desc&limit=20")
    if not isinstance(groups,list) or not groups:
        await u.message.reply_text("Sin apuestas liquidadas recientes."); return
    ctx.user_data["corr_groups"]=groups
    rows=[[InlineKeyboardButton(f"✎ {g['descr'] or g['id'][:8]} ({g['date']})",callback_data=f"cg_{g['id']}")] for g in groups[:10]]
    await u.message.reply_text("✎ ¿Cuál resultado corregir?",reply_markup=InlineKeyboardMarkup(rows))
    return SC_PICK_GROUP

async def r_corr_group(u:Update, ctx):
    q=u.callback_query; await q.answer()
    gid=q.data[3:]; ctx.user_data["corr_gid"]=gid
    tickets=await sb_get("tickets",f"group_id=eq.{gid}&order=id.asc")
    if not isinstance(tickets,list) or not tickets:
        await q.edit_message_text("⚠️ Sin tickets."); return ConversationHandler.END
    ctx.user_data["corr_tickets"]=tickets; ctx.user_data["corr_idx"]=0; ctx.user_data["corr_returns"]={}
    # Pre-fill with existing returns
    for t in tickets: ctx.user_data["corr_returns"][t["id"]]=t.get("returned") or 0
    return await ask_corr_ticket(q,ctx)

async def ask_corr_ticket(src, ctx):
    tickets=ctx.user_data["corr_tickets"]; idx=ctx.user_data["corr_idx"]
    if idx>=len(tickets): return await finalize_correccion(src,ctx)
    t=tickets[idx]; cur_ret=ctx.user_data["corr_returns"].get(t["id"],0)
    cur_lbl="Win" if cur_ret>t["stake"] else ("Void" if abs(cur_ret-t["stake"])<0.01 else "Loss")
    await send(src,
        f"✎ *Ticket #{idx+1}* — {t['tipster']} · {t['casa']}\n"
        f"Stake: {fmt(t['stake'])} · @{t['cuota']}\n"
        f"Resultado actual: *{cur_lbl}* ({fmt(cur_ret)} soles)\n\nNuevo resultado:",[[
        InlineKeyboardButton("✅ Win",callback_data="cr_win"),
        InlineKeyboardButton("❌ Loss",callback_data="cr_loss"),
        InlineKeyboardButton("🔵 Void",callback_data="cr_void"),
        InlineKeyboardButton("✏️ Monto exacto",callback_data="cr_manual"),
        InlineKeyboardButton("↩ Sin cambio",callback_data="cr_skip"),
    ]]); return SC_PICK_TICKET

async def r_corr_ticket(u:Update, ctx):
    q=u.callback_query; await q.answer()
    tickets=ctx.user_data["corr_tickets"]; idx=ctx.user_data["corr_idx"]
    t=tickets[idx]; stake=t["stake"]; pot=t["potencial"]
    action=q.data[3:]
    if action=="win":    ctx.user_data["corr_returns"][t["id"]]=pot
    elif action=="loss": ctx.user_data["corr_returns"][t["id"]]=0
    elif action=="void": ctx.user_data["corr_returns"][t["id"]]=stake
    elif action=="skip": pass
    elif action=="manual":
        await q.edit_message_text(f"✏️ Ticket #{idx+1} — monto retornado exacto:")
        return SC_SET_RETURN
    ctx.user_data["corr_idx"]+=1
    return await ask_corr_ticket(q,ctx)

async def r_corr_manual(u:Update, ctx):
    try: ret=float(u.message.text.strip().replace(",",".")); assert ret>=0
    except: await u.message.reply_text("⚠️ Número válido:"); return SC_SET_RETURN
    await u.message.delete()
    tickets=ctx.user_data["corr_tickets"]; idx=ctx.user_data["corr_idx"]
    ctx.user_data["corr_returns"][tickets[idx]["id"]]=ret
    ctx.user_data["corr_idx"]+=1
    return await ask_corr_ticket(u.message,ctx)

async def finalize_correccion(src, ctx):
    tickets=ctx.user_data["corr_tickets"]; rets=ctx.user_data["corr_returns"]
    total_ret=sum(rets.values()); total_stake=sum(t["stake"] for t in tickets)
    pnl=round(total_ret-total_stake,2)
    lines=["📊 *Corrección:*",""]
    for t in tickets:
        r=rets.get(t["id"],0); s=t["stake"]
        lbl="✅ Win" if r>s else ("🔵 Void" if abs(r-s)<0.01 else "❌ Loss")
        lines.append(f"{lbl} {t['tipster']} · {t['casa']}: {fmt(r)} soles")
    lines.append(f"\nNuevo P&L: *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*")
    await send(src,"\n".join(lines),[[
        InlineKeyboardButton("✅ Guardar corrección",callback_data="cf_save"),
        InlineKeyboardButton("❌ Cancelar",callback_data="cf_cancel")
    ]]); return SC_PICK_GROUP

async def r_corr_confirm(u:Update, ctx):
    q=u.callback_query; await q.answer()
    if q.data=="cf_cancel": await q.edit_message_text("❌ Cancelado."); return ConversationHandler.END
    tickets=ctx.user_data["corr_tickets"]; rets=ctx.user_data["corr_returns"]; gid=ctx.user_data["corr_gid"]
    try:
        for t in tickets:
            r=rets.get(t["id"],0); s=t["stake"]
            await sb_patch("tickets","id",t["id"],{"returned":r,"status":"settled"})
            # Delete old movements and recreate
            old_mvs=await sb_get("investor_movements",f"ticket_id=eq.{t['id']}&type=eq.bet_result")
            if isinstance(old_mvs,list):
                for mv in old_mvs: await sb_delete("investor_movements","id",mv["id"])
            inv_rows=await sb_get("ticket_investors",f"ticket_id=eq.{t['id']}")
            if isinstance(inv_rows,list):
                for ir in inv_rows:
                    inv_stake=ir["stake"]; prop=inv_stake/s if s>0 else 0
                    inv_ret=round(r*prop,2); inv_pnl=round(inv_ret-inv_stake,2)
                    if inv_pnl!=0:
                        await sb_insert("investor_movements",{
                            "id":gen_id(),"investor_id":ir["investor_id"],
                            "type":"bet_result","amount":inv_pnl,
                            "note":"Corrección resultado","date":str(date.today()),
                            "ticket_id":t["id"]})
        total_ret=sum(rets.values()); total_stake=sum(t["stake"] for t in tickets)
        pnl=round(total_ret-total_stake,2)
        await q.edit_message_text(f"✅ *Corrección guardada*\n\nNuevo P&L: *{'+' if pnl>=0 else ''}{fmt(pnl)} soles*",parse_mode="Markdown")
    except Exception as e: await q.edit_message_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def cmd_cancelar(u:Update, ctx):
    rs(ctx); await u.message.reply_text("❌ Cancelado."); return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    app=Application.builder().token(BOT_TOKEN).build()

    nueva_conv=ConversationHandler(
        entry_points=[CommandHandler("nueva",cmd_nueva)],
        states={
            S_DESC:        [MessageHandler(filters.TEXT&~filters.COMMAND,r_desc)],
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
                            CallbackQueryHandler(r_delete_group,pattern="^rd_"),
                            CallbackQueryHandler(r_delete_confirm,pattern="^rdc_"),
                            CallbackQueryHandler(r_res_confirm,pattern="^rf_")],
            SR_PICK_TICKET:[CallbackQueryHandler(r_ticket_result,pattern="^rr_")],
            SR_SET_RETURN: [MessageHandler(filters.TEXT&~filters.COMMAND,r_manual_return)],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )

    edit_conv=ConversationHandler(
        entry_points=[CommandHandler("editar",cmd_editar)],
        states={
            SE_PICK_GROUP: [CallbackQueryHandler(r_edit_group,pattern="^eg_")],
            SE_PICK_FIELD: [CallbackQueryHandler(r_edit_field,pattern="^(ef_|et_)"),
                            CallbackQueryHandler(r_edit_subfield,pattern="^ef_(stake|cuota|bookie)")],
            SE_SET_VALUE:  [MessageHandler(filters.TEXT&~filters.COMMAND,r_edit_value)],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )

    corr_conv=ConversationHandler(
        entry_points=[CommandHandler("corregir",cmd_corregir)],
        states={
            SC_PICK_GROUP: [CallbackQueryHandler(r_corr_group,pattern="^cg_"),
                            CallbackQueryHandler(r_corr_confirm,pattern="^cf_")],
            SC_PICK_TICKET:[CallbackQueryHandler(r_corr_ticket,pattern="^cr_")],
            SC_SET_RETURN: [MessageHandler(filters.TEXT&~filters.COMMAND,r_corr_manual)],
        },
        fallbacks=[CommandHandler("cancelar",cmd_cancelar)],
        allow_reentry=True,
    )

    app.add_handler(nueva_conv)
    app.add_handler(res_conv)
    app.add_handler(edit_conv)
    app.add_handler(corr_conv)
    app.add_handler(CommandHandler("start",cmd_start))
    app.add_handler(CommandHandler("pendientes",cmd_pendientes))
    app.add_handler(CommandHandler("hoy",cmd_hoy))
    app.add_handler(CommandHandler("cancelar",cmd_cancelar))
    print("BetLog Bot running...")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
