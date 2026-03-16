#!/usr/bin/env bash
# test_setup.sh
# 로컬 테스트 환경 구성 스크립트
#
# 수행 내용:
#   1. 테스트용 git 저장소 생성 (initial / direct / PR merge 커밋 포함)
#   2. 로컬 p4d 서버 기동 + depot / user / client 초기화
#   3. .env.test 파일 생성
#
# 사전 조건:
#   - git, p4, p4d 가 PATH에 있어야 함
#
# 사용법:
#   bash test_setup.sh          # 환경 구성
#   bash test_setup.sh clean    # 생성된 파일/프로세스 정리

set -euo pipefail

WORK_DIR="$(pwd)/test_tmp"
GIT_REPO="$WORK_DIR/git_repo"
P4ROOT="$WORK_DIR/p4root"
P4WORKSPACE="$WORK_DIR/p4workspace"
P4PORT="localhost:1667"
P4USER="testuser"
P4CLIENT="testclient"
P4DEPOT_BASE="//depot/myproject"
P4D_PID_FILE="$WORK_DIR/p4d.pid"

# ── 정리 ────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "clean" ]]; then
    echo "=== 테스트 환경 정리 ==="
    if [[ -f "$P4D_PID_FILE" ]]; then
        kill "$(cat "$P4D_PID_FILE")" 2>/dev/null && echo "p4d 종료" || true
        rm -f "$P4D_PID_FILE"
    fi
    rm -rf "$WORK_DIR" .env.test submitted_prs.json bitbucket_to_perforce.log
    echo "완료."
    exit 0
fi

# ── 명령어 확인 ───────────────────────────────────────────────────────────────
for cmd in git p4 p4d; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "오류: '$cmd' 를 찾을 수 없습니다. 설치 후 다시 실행하세요."
        exit 1
    fi
done

echo "=== 테스트 디렉토리 초기화 ==="
rm -rf "$WORK_DIR"
mkdir -p "$GIT_REPO" "$P4ROOT" "$P4WORKSPACE"

# ── 테스트용 git 저장소 생성 ─────────────────────────────────────────────────
echo ""
echo "=== 테스트 git 저장소 구성 ==="

git -C "$GIT_REPO" init -b master
git -C "$GIT_REPO" config user.email "test@example.com"
git -C "$GIT_REPO" config user.name "Test User"

# (1) initial commit
mkdir -p "$GIT_REPO/src"
echo "# My Project"   > "$GIT_REPO/README.md"
echo "print('hello')" > "$GIT_REPO/src/main.py"
git -C "$GIT_REPO" add .
git -C "$GIT_REPO" commit -m "Initial commit"
echo "  [✓] initial commit"

# (2) master 직접 push 커밋
echo "VERSION=1.0" > "$GIT_REPO/.env"
git -C "$GIT_REPO" add .
git -C "$GIT_REPO" commit -m "Add .env config (direct push to master)"
echo "  [✓] direct commit"

# (3) PR 시뮬레이션: feature 브랜치 생성 후 master에 merge
git -C "$GIT_REPO" checkout -b feature/login
echo "def login(): pass" > "$GIT_REPO/src/login.py"
git -C "$GIT_REPO" add .
git -C "$GIT_REPO" commit -m "Add login module"

echo "def logout(): pass" >> "$GIT_REPO/src/login.py"
git -C "$GIT_REPO" add .
git -C "$GIT_REPO" commit -m "Add logout to login module"

git -C "$GIT_REPO" checkout master
git -C "$GIT_REPO" merge --no-ff feature/login -m "Merged in feature/login (pull request #1)"
echo "  [✓] PR merge commit (#1)"

# (4) 두 번째 PR: 파일 수정 + 삭제 포함
git -C "$GIT_REPO" checkout -b feature/refactor
echo "print('world')" >> "$GIT_REPO/src/main.py"   # 수정
rm "$GIT_REPO/.env"                                  # 삭제
git -C "$GIT_REPO" add .
git -C "$GIT_REPO" commit -m "Refactor main, remove .env"

git -C "$GIT_REPO" checkout master
git -C "$GIT_REPO" merge --no-ff feature/refactor -m "Merged in feature/refactor (pull request #2)"
echo "  [✓] PR merge commit (#2, 수정+삭제 포함)"

echo ""
echo "--- git log ---"
git -C "$GIT_REPO" log --oneline --graph

# ── 로컬 p4d 기동 ─────────────────────────────────────────────────────────────
echo ""
echo "=== 로컬 p4d 기동 (포트 $P4PORT) ==="

# 기존 p4d 프로세스 정리
pkill -f "p4d -r $P4ROOT" 2>/dev/null || true
sleep 1

p4d -r "$P4ROOT" -p "$P4PORT" -L "$WORK_DIR/p4d.log" &
echo $! > "$P4D_PID_FILE"
sleep 2

export P4PORT P4USER P4CLIENT

# 사용자 생성
p4 -p "$P4PORT" -u "$P4USER" user -i <<EOF
User: $P4USER
Email: test@example.com
FullName: Test User
EOF
echo "  [✓] p4 user 생성"

# depot 생성
p4 -p "$P4PORT" -u "$P4USER" depot -i <<EOF
Depot: depot
Owner: $P4USER
Type: local
Map: depot/...
EOF
echo "  [✓] depot 생성"

# 워크스페이스(client) 생성
p4 -p "$P4PORT" -u "$P4USER" client -i <<EOF
Client: $P4CLIENT
Owner: $P4USER
Root: $P4WORKSPACE
Options: noallwrite noclobber nocompress unlocked nomodtime normdir
View:
    //depot/... //$P4CLIENT/...
EOF
echo "  [✓] p4 client 생성 (root: $P4WORKSPACE)"

# ── .env.test 생성 ────────────────────────────────────────────────────────────
echo ""
echo "=== .env.test 파일 생성 ==="
cat > .env.test <<EOF
GIT_REPO_PATH=$GIT_REPO
GIT_BRANCH=master
GIT_REMOTE=origin
P4PORT=$P4PORT
P4USER=$P4USER
P4CLIENT=$P4CLIENT
P4PASSWD=
P4DEPOT_BASE=$P4DEPOT_BASE
STATE_FILE=submitted_prs.json
EOF
echo "  [✓] .env.test 생성"

echo ""
echo "========================================="
echo " 테스트 환경 준비 완료!"
echo "========================================="
echo ""
echo "  git 저장소:  $GIT_REPO"
echo "  p4d 포트:    $P4PORT"
echo "  p4 client:  $P4CLIENT  (root: $P4WORKSPACE)"
echo ""
echo "  [1] dry-run 테스트 (git 파싱만 확인, p4 불필요):"
echo "      python bitbucket_to_perforce.py --dry-run --no-fetch --env-file .env.test"
echo ""
echo "  [2] 실제 submit 테스트:"
echo "      python bitbucket_to_perforce.py --no-fetch --env-file .env.test"
echo ""
echo "  [3] 재실행 테스트 (이미 제출된 커밋 스킵 확인):"
echo "      python bitbucket_to_perforce.py --no-fetch --env-file .env.test"
echo ""
echo "  [4] 강제 재처리:"
echo "      python bitbucket_to_perforce.py --no-fetch --force --env-file .env.test"
echo ""
echo "  [5] p4 changelist 확인:"
echo "      p4 -p $P4PORT -u $P4USER -c $P4CLIENT changes -l"
echo ""
echo "  [6] depot 파일 목록 확인:"
echo "      p4 -p $P4PORT -u $P4USER files //depot/..."
echo ""
echo "  [정리] bash test_setup.sh clean"
