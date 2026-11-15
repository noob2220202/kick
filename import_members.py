"""(선택) 기존 B 채널 회원 목록을 봇 DB 로 가져오는 스크립트.

봇 API 로는 채널 회원 목록을 볼 수 없어서, 기존 회원은 관리자 계정으로 한 번만 가져와야 함.
필요: pip install --user telethon, my.telegram.org 에서 받은 api_id / api_hash
처음 실행 시 전화번호 + 인증코드로 로그인 (B 채널 관리자 계정이어야 함)

사용: python3 import_members.py
이후 봇에게 /sweep 보내면 A 미가입자 정리됨 (봇이 켜져 있으면 바로 사용 가능)
"""

import sqlite3
import time

from telethon.sync import TelegramClient

import config

API_ID = int(input("api_id: "))
API_HASH = input("api_hash: ").strip()

with TelegramClient("admin", API_ID, API_HASH) as client:
    ids = [u.id for u in client.iter_participants(config.B_CHAT_ID) if not u.bot]

print(f"가져온 B 회원: {len(ids)}명")
db = sqlite3.connect(getattr(config, "DB_PATH", "kick.db"))
db.execute("CREATE TABLE IF NOT EXISTS members (user_id INTEGER PRIMARY KEY, seen_at INTEGER)")
now = int(time.time())
db.executemany("INSERT OR IGNORE INTO members VALUES (?, ?)", [(i, now) for i in ids])
db.commit()
print("DB 저장 완료. 봇에게 /sweep 을 보내세요.")
with open("b_member_ids.txt", "w") as f:
    f.write("\n".join(map(str, ids)))
