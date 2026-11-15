"""A 그룹 미가입자를 B 채널에서 킥(재입장 가능)하는 봇.

- B 입장 요청: A 가입자면 바로 승인, 아니면 개인 메시지로 A 가입 안내 + [입장완료] 버튼
- A 를 나가면: B 에서 즉시 킥 (ban 후 바로 unban → 다시 들어올 수 있음)
- B 에 누가 들어오면: A 가입 여부 확인 후 미가입이면 킥
- 정기/수동(/sweep) 정리: 봇이 아는 B 회원 전체를 다시 검사
"""

import asyncio
import logging
import sqlite3
import time

from telegram import (
    ChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config

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


async def a_link(context: ContextTypes.DEFAULT_TYPE) -> str:
    link = context.bot_data.get("a_link") or getattr(config, "A_INVITE_LINK", "")
    if not link:
        inv = await call(context.bot.create_chat_invite_link, A, name="B 입장 안내용")
        link = inv.invite_link
    context.bot_data["a_link"] = link
    return link


def guide_markup(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("A 그룹 입장하기", url=link)],
            [InlineKeyboardButton("입장완료", callback_data=CHECK_DATA)],
        ]
    )


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

    text = (
        "B 채널에 입장하려면 먼저 A 그룹에 들어와 주세요.\n\n"
        "1) 아래 [A 그룹 입장하기] 버튼으로 A 그룹에 들어오기\n"
        "2) [입장완료] 버튼 누르기\n\n"
        "확인되면 B 채널 입장 요청이 자동으로 승인돼요."
    )
    try:
        await call(
            context.bot.send_message,
            req.user_chat_id,
            text,
            reply_markup=guide_markup(await a_link(context)),
        )
        log.info("A 가입 안내 발송: %s", uid)
    except TelegramError as e:
        log.warning("안내 발송 실패 %s: %s", uid, e)


async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id

    ok = await in_a(context.bot, uid)
    if ok is None:
        await q.answer("확인 중 오류가 났어요. 잠시 후 다시 눌러주세요.", show_alert=True)
        return
    if not ok:
        await q.answer("아직 A 그룹에 입장하지 않았어요. A 그룹에 들어온 뒤 다시 눌러주세요.", show_alert=True)
        return

    try:
        await call(context.bot.approve_chat_join_request, B, uid)
        await q.answer("확인됐어요! B 채널 입장이 승인됐어요.", show_alert=True)
        await q.edit_message_text("확인 완료! B 채널 입장이 승인됐어요.")
        log.info("A 가입 확인 후 승인: %s", uid)
    except BadRequest as e:
        # 이미 처리된 요청 (이미 입장했거나 요청이 사라짐)
        if await status_in(context.bot, B, uid) in MEMBER_STATUSES:
            await q.answer("이미 B 채널에 입장되어 있어요.", show_alert=True)
            await q.edit_message_text("이미 B 채널에 입장되어 있어요.")
        else:
            log.info("승인할 요청 없음 %s: %s", uid, e)
            await q.answer(
                "입장 요청이 만료됐어요. B 채널에 다시 입장 요청을 보내주세요.",
                show_alert=True,
            )
    except TelegramError as e:
        log.warning("승인 실패 %s: %s", uid, e)
        await q.answer("오류가 났어요. 잠시 후 다시 눌러주세요.", show_alert=True)


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


def fmt(r: dict) -> str:
    return (
        f"정리 완료\n검사: {r['checked']}명\n킥: {r['kicked']}명\n"
        f"이미 나감: {r['gone']}명\n확인 실패: {r['error']}명\n남은 기록: {db_count()}명"
    )


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
        await update.message.reply_text("이미 정리 중이에요.")
        return
    await update.message.reply_text(f"정리 시작 (기록된 B 회원 {db_count()}명)")

    async def run():
        r = await sweep(context.bot)
        await context.bot.send_message(update.effective_chat.id, fmt(r))

    context.application.create_task(run())


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(f"기록된 B 회원: {db_count()}명")


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """user id 가 한 줄씩 적힌 .txt 파일을 캡션 /import 와 함께 보내면 기록에 추가."""
    if not is_admin(update):
        return
    doc = update.message.document
    f = await doc.get_file()
    data = (await f.download_as_bytearray()).decode("utf-8", "ignore")
    uids = {int(t) for t in data.replace(",", " ").split() if t.lstrip("-").isdigit() and int(t) > 0}
    added = db_add_many(uids)
    await update.message.reply_text(
        f"{len(uids)}개 읽음, 새로 추가 {added}명 (전체 {db_count()}명)\n/sweep 으로 정리하세요."
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """설정용: 현재 채팅 ID 와 보낸 사람 ID 알려줌. 채널은 채널 글에 /id 를 쓰면 됨."""
    msg = update.effective_message
    text = f"이 채팅 ID: {update.effective_chat.id}"
    if update.effective_user:
        text += f"\n내 user id: {update.effective_user.id}"
    await msg.reply_text(text)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if await in_a(context.bot, uid):
        await update.message.reply_text("A 그룹 가입이 확인됐어요. B 채널에 입장 요청을 보내주세요.")
    else:
        await update.message.reply_text(
            "B 채널에 입장하려면 먼저 A 그룹에 들어와 주세요.",
            reply_markup=guide_markup(await a_link(context)),
        )


# ---------------------------------------------------------------- 실행
async def post_init(app: Application):
    me = await app.bot.get_me()
    for name, cid in (("A 그룹", A), ("B 채널", B)):
        try:
            m = await app.bot.get_chat_member(cid, me.id)
            if m.status != ChatMember.ADMINISTRATOR:
                log.error("봇이 %s 관리자가 아님! (%s)", name, m.status)
        except (BadRequest, Forbidden) as e:
            log.error("%s(%s) 접근 불가: %s", name, cid, e)
    app.create_task(sweep_loop(app))
    log.info("봇 시작: @%s, 기록된 B 회원 %s명", me.username, db_count())


def main():
    app = Application.builder().token(config.BOT_TOKEN).post_init(post_init).build()
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(on_check, pattern=f"^{CHECK_DATA}$"))
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
