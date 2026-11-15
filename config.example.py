# 이 파일을 config.py 로 복사해서 값을 채우세요.

# @BotFather 에서 받은 봇 토큰
BOT_TOKEN = "123456:ABC..."

# A 채널(필수 가입 채널)과 B 채널의 ID (-100 으로 시작하는 숫자)
A_CHAT_ID = -1001111111111
B_CHAT_ID = -1002222222222

# 유저에게 안내할 A 채널 링크 (공개 채널이면 https://t.me/채널아이디)
# 비워두면 봇이 A 채널용 초대 링크를 직접 만들어서 사용함
A_INVITE_LINK = ""

# /sweep, /stats 명령을 쓸 수 있는 관리자 텔레그램 user id 목록
ADMIN_IDS = [111111111]

# 자동 정리 주기(시간). 0 이면 자동 정리 안 함 (/sweep 으로 수동만)
SWEEP_INTERVAL_HOURS = 6

# 회원 기록 DB 파일
DB_PATH = "kick.db"

# (선택) 기존 회원 정리용 import_members.py 에서만 사용. my.telegram.org 에서 발급
# 비워두면 실행할 때 물어봄
API_ID = 0
API_HASH = ""
