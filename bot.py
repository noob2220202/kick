"""A 그룹 미가입자를 B 채널에서 킥(재입장 가능)하는 봇.

- B 입장 요청: A 가입자면 바로 승인, 아니면 개인 메시지로 A 가입 안내 + [확인하기] 버튼
- A 를 나가면: B 에서 즉시 킥 (ban 후 바로 unban → 다시 들어올 수 있음)
- B 에 누가 들어오면: A 가입 여부 확인 후 미가입이면 킥
- 정기/수동(/sweep) 정리: 봇이 아는 B 회원 전체를 다시 검사
"""

import asyncio
import html
import logging
import sqlite3
import time

from telegram import (
    ChatMember,
    LinkPreviewOptions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

import config
import messages as M

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kickbot")

A = config.A_CHAT_ID
B = config.B_CHAT_ID
ADMINS = set(getattr(config, "ADMIN_IDS", []))
CHECK_DATA = "check_a"

MEMBER_STATUSES = {ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER}
STAFF_STATUSES = {ChatMember.ADMINISTRATOR, ChatMember.OWNER}


# ---------------------------------------------------------------- DB
db = sqlite3.connect(getattr(config, "DB_PATH", "kick.db"))
db.execute("CREATE TABLE IF NOT EXISTS members (user_id INTEGER PRIMARY KEY, seen_at INTEGER)")
db.commit()


def db_add(uid: int) -> None:
    db.execute("INSERT OR REPLACE INTO members VALUES (?, ?)", (uid, int(time.time())))
    db.commit()


def db_add_many(uids) -> int:
    before = db_count()
    now = int(time.time())
    db.executemany("INSERT OR IGNORE INTO members VALUES (?, ?)", [(u, now) for u in uids])
    db.commit()
    return db_count() - before


def db_remove(uid: int) -> None:
    db.execute("DELETE FROM members WHERE user_id = ?", (uid,))
    db.commit()


def db_all() -> list[int]:
    return [r[0] for r in db.execute("SELECT user_id FROM members")]


def db_count() -> int:
    return db.execute("SELECT COUNT(*) FROM members").fetchone()[0]


# ---------------------------------------------------------------- 텔레그램 헬퍼
async def call(fn, *args, **kwargs):
    """RetryAfter(속도 제한) 이면 기다렸다가 다시 시도."""
    while True:
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as e:
            wait = e.retry_after if isinstance(e.retry_after, (int, float)) else e.retry_after.total_seconds()
            log.warning("속도 제한, %s초 대기", wait)
            await asyncio.sleep(wait + 1)


async def status_in(bot, chat_id: int, uid: int):
    """해당 그룹/채널에서의 상태 문자열. 확인 실패 시 None."""
    try:
        m = await call(bot.get_chat_member, chat_id, uid)
    except TelegramError as e:
        log.warning("get_chat_member(%s, %s) 실패: %s", chat_id, uid, e)
        return None
    if m.status == ChatMember.RESTRICTED:
        return ChatMember.MEMBER if m.is_member else ChatMember.LEFT
    return m.status


async def in_a(bot, uid: int):
    """A 가입 여부. True / False / None(확인 실패)."""
    s = await status_in(bot, A, uid)
    return None if s is None else s in MEMBER_STATUSES


async def kick_from_b(bot, uid: int) -> bool:
    """B 에서 내보내기. 차단하지 않고 바로 풀어줘서 재입장 가능."""
    try:
        await call(bot.ban_chat_member, B, uid)
        await call(bot.unban_chat_member, B, uid, only_if_banned=True)
    except TelegramError as e:
        log.warning("킥 실패 %s: %s", uid, e)
        return False
    db_remove(uid)
    log.info("B 에서 킥: %s", uid)
    return True


# ---------------------------------------------------------------- 이름/링크/메시지 헬퍼
def short(s: str, n: int = 24) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def T(context: ContextTypes.DEFAULT_TYPE, tpl: str, html_mode: bool = True, **kw) -> str:
    """메시지 템플릿 채우기. {a} {b} 는 그룹/채널 이름으로 자동 치환."""
    bd = context.bot_data
    a, b = bd.get("a_name", "A 그룹"), bd.get("b_name", "B 채널")
    if html_mode:
        a, b = html.escape(a), html.escape(b)
        kw = {k: html.escape(v) if isinstance(v, str) else v for k, v in kw.items()}
    return tpl.format(a=a, b=b, **kw)


def B_(context, tpl: str) -> str:
    """버튼/팝업용 (서식 없음, 이름 짧게)."""
    bd = context.bot_data
    return tpl.format(a=short(bd.get("a_name", "A 그룹")), b=short(bd.get("b_name", "B 채널")))


async def a_link(context: ContextTypes.DEFAULT_TYPE) -> str:
    """A 그룹 링크. 없으면 봇이 만들고, 권한이 없으면 "" (버튼 없이 안내)."""
    bd = context.bot_data
    if not bd.get("a_link"):
        bd["a_link"] = await make_a_link(context.bot)
    return bd["a_link"]


async def make_a_link(bot) -> str:
    try:
        inv = await call(bot.create_chat_invite_link, A, name="B 입장 안내용")
        return inv.invite_link
    except TelegramError as e:
        log.error(
            "A 그룹 초대 링크 생성 실패: %s → A 그룹에서 봇에게 '링크로 초대' 권한을 주거나 "
            "config.py 의 A_INVITE_LINK 에 링크를 넣으세요", e
        )
        return ""


async def guide_markup(context, origin: str) -> InlineKeyboardMarkup:
    rows = []
    link = await a_link(context)
    if link:
        rows.append([InlineKeyboardButton(B_(context, M.BTN_JOIN_A), url=link)])
    rows.append([InlineKeyboardButton(B_(context, M.BTN_CHECK), callback_data=f"{CHECK_DATA}:{origin}")])
    return InlineKeyboardMarkup(rows)


def b_markup(context, tpl: str):
    link = context.bot_data.get("b_link")
    if not link:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(B_(context, tpl), url=link)]])


# ---------------------------------------------------------------- 입장 요청
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if req.chat.id != B:
        return
    uid = req.from_user.id

    if await in_a(context.bot, uid):
        try:
            await call(req.approve)
            log.info("A 가입자 바로 승인: %s", uid)
        except TelegramError as e:
            log.warning("승인 실패 %s: %s", uid, e)
        return

    try:
        await call(
            context.bot.send_message,
            req.user_chat_id,
            T(context, M.GUIDE, name=req.from_user.first_name),
            reply_markup=await guide_markup(context, "g"),
        )
        log.info("A 가입 안내 발송: %s", uid)
    except TelegramError as e:
        log.warning("안내 발송 실패 %s: %s", uid, e)


async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    uid = user.id
    origin = q.data.split(":", 1)[1] if ":" in q.data else "g"

    ok = await in_a(context.bot, uid)
    if ok is None:
        await q.answer(B_(context, M.ALERT_ERROR), show_alert=True)
        return
    if not ok:
        await q.answer(B_(context, M.ALERT_NOT_JOINED), show_alert=True)
        base = M.GUIDE if origin == "g" else M.START_NOT
        text = T(context, base + M.LAST_CHECK, name=user.first_name, time=time.strftime("%H:%M:%S"))
        try:
            await q.edit_message_text(text, reply_markup=await guide_markup(context, origin))
        except BadRequest:
            pass
        return

    try:
        await call(context.bot.approve_chat_join_request, B, uid)
        log.info("A 가입 확인 후 승인: %s", uid)
        await q.answer(B_(context, M.ALERT_APPROVED), show_alert=True)
        await q.edit_message_text(
            T(context, M.APPROVED, name=user.first_name), reply_markup=b_markup(context, M.BTN_OPEN_B)
        )
    except BadRequest as e:
        # 처리할 요청 없음 (이미 입장했거나, 만료됐거나, 아직 요청 안 함)
        if await status_in(context.bot, B, uid) in MEMBER_STATUSES:
            await q.answer(B_(context, M.ALERT_ALREADY), show_alert=True)
            await q.edit_message_text(
                T(context, M.ALREADY_IN), reply_markup=b_markup(context, M.BTN_OPEN_B)
            )
        else:
            log.info("승인할 요청 없음 %s: %s", uid, e)
            await q.answer(B_(context, M.ALERT_EXPIRED), show_alert=True)
            await q.edit_message_text(
                T(context, M.EXPIRED), reply_markup=b_markup(context, M.BTN_RETRY_B)
            )
    except TelegramError as e:
        log.warning("승인 실패 %s: %s", uid, e)
        await q.answer(B_(context, M.ALERT_ERROR), show_alert=True)



# ---------------------------------------------------------------- 회원 변동
async def on_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    uid = cm.new_chat_member.user.id
    new = cm.new_chat_member.status
    if new == ChatMember.RESTRICTED:
        new = ChatMember.MEMBER if cm.new_chat_member.is_member else ChatMember.LEFT

    if cm.chat.id == A:
        if new not in MEMBER_STATUSES:
            # A 를 나감 → B 에 있으면 킥
            s = await status_in(context.bot, B, uid)
            if s == ChatMember.MEMBER:
                log.info("A 탈퇴 감지: %s", uid)
                await kick_from_b(context.bot, uid)
            elif s is not None and s not in MEMBER_STATUSES:
                db_remove(uid)

    elif cm.chat.id == B:
        if new in MEMBER_STATUSES:
            db_add(uid)
            if new == ChatMember.MEMBER and await in_a(context.bot, uid) is False:
                log.info("A 미가입자 B 입장 감지: %s", uid)
                await kick_from_b(context.bot, uid)
        else:
            db_remove(uid)


# ---------------------------------------------------------------- 전체 정리
sweep_lock = asyncio.Lock()


async def sweep(bot) -> dict:
    """봇이 아는 B 회원 전체를 검사해서 A 미가입자 킥."""
    async with sweep_lock:
        r = {"checked": 0, "kicked": 0, "gone": 0, "error": 0}
        for uid in db_all():
            r["checked"] += 1
            s = await status_in(bot, B, uid)
            if s is None:
                r["error"] += 1
            elif s in STAFF_STATUSES:
                pass
            elif s != ChatMember.MEMBER:
                db_remove(uid)
                r["gone"] += 1
            else:
                ok = await in_a(bot, uid)
                if ok is None:
                    r["error"] += 1
                elif not ok:
                    if await kick_from_b(bot, uid):
                        r["kicked"] += 1
                    else:
                        r["error"] += 1
            await asyncio.sleep(0.05)
        log.info("정리 완료: %s", r)
        return r


async def sweep_loop(app: Application):
    hours = float(getattr(config, "SWEEP_INTERVAL_HOURS", 0) or 0)
    if hours <= 0:
        return
    while True:
        await asyncio.sleep(hours * 3600)
        try:
            await sweep(app.bot)
        except Exception:
            log.exception("자동 정리 실패")


# ---------------------------------------------------------------- 관리자 명령
def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in ADMINS


async def cmd_sweep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if sweep_lock.locked():
        await update.message.reply_text(M.SWEEP_BUSY)
        return
    await update.message.reply_text(M.SWEEP_START.format(count=db_count()))

    async def run():
        r = await sweep(context.bot)
        await context.bot.send_message(update.effective_chat.id, M.SWEEP_DONE.format(left=db_count(), **r))

    context.application.create_task(run())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(T(context, M.STATS, count=db_count()))


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """user id 가 한 줄씩 적힌 .txt 파일을 캡션 /import 와 함께 보내면 기록에 추가."""
    if not is_admin(update):
        return
    doc = update.message.document
    f = await doc.get_file()
    data = (await f.download_as_bytearray()).decode("utf-8", "ignore")
    uids = {int(t) for t in data.replace(",", " ").split() if t.isdigit() and int(t) > 0}
    added = db_add_many(uids)
    await update.message.reply_text(M.IMPORTED.format(read=len(uids), added=added, count=db_count()))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """설정용: 현재 채팅 ID 와 보낸 사람 ID 알려줌. 채널은 채널 글에 /id 를 쓰면 됨."""
    text = M.ID_INFO.format(chat=update.effective_chat.id)
    if update.effective_user:
        text += M.ID_USER.format(user=update.effective_user.id)
    await update.effective_message.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if await in_a(context.bot, user.id):
        await update.message.reply_text(
            T(context, M.START_OK, name=user.first_name), reply_markup=b_markup(context, M.BTN_OPEN_B)
        )
    else:
        await update.message.reply_text(
            T(context, M.START_NOT, name=user.first_name), reply_markup=await guide_markup(context, "s")
        )


# ---------------------------------------------------------------- 실행
async def post_init(app: Application):
    me = await app.bot.get_me()
    bd = app.bot_data
    for key, label, cid in (("a", "A 그룹", A), ("b", "B 채널", B)):
        try:
            chat = await app.bot.get_chat(cid)
            bd[f"{key}_name"] = getattr(config, f"{key.upper()}_NAME", "") or chat.title or label
            bd[f"{key}_link"] = (
                getattr(config, f"{key.upper()}_INVITE_LINK", "")
                or (f"https://t.me/{chat.username}" if chat.username else "")
                or chat.invite_link
                or ""
            )
            m = await app.bot.get_chat_member(cid, me.id)
            if m.status != ChatMember.ADMINISTRATOR:
                log.error("봇이 %s 관리자가 아님! (%s)", label, m.status)
        except TelegramError as e:
            log.error("%s(%s) 접근 불가: %s", label, cid, e)
            bd.setdefault(f"{key}_name", getattr(config, f"{key.upper()}_NAME", "") or label)
    if not bd.get("a_link"):
        bd["a_link"] = await make_a_link(app.bot)
    app.create_task(sweep_loop(app))
    log.info(
        "봇 시작: @%s | A=%s (%s) | B=%s | 기록된 B 회원 %s명",
        me.username, bd.get("a_name"), bd.get("a_link") or "링크 없음!", bd.get("b_name"), db_count(),
    )


def main():
    defaults = Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
    app = Application.builder().token(config.BOT_TOKEN).defaults(defaults).post_init(post_init).build()
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(on_check, pattern=f"^{CHECK_DATA}"))
    app.add_handler(ChatMemberHandler(on_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CommandHandler("sweep", cmd_sweep))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(
        MessageHandler(filters.Document.ALL & filters.CaptionRegex(r"^/import"), cmd_import)
    )
    app.add_handler(CommandHandler("start", cmd_start, filters=filters.ChatType.PRIVATE))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
