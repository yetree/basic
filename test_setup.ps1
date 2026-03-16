# test_setup.ps1
# 로컬 테스트 환경 구성 스크립트 (Windows PowerShell / pwsh)
#
# 사전 조건:
#   - git, p4, p4d 가 PATH에 있어야 함
#   - PowerShell 5.1 이상 또는 pwsh (PowerShell 7)
#
# 사용법:
#   .\test_setup.ps1          # 환경 구성
#   .\test_setup.ps1 clean    # 생성된 파일/프로세스 정리
#
# 실행 정책 오류 시:
#   Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned

param(
    [string]$Action = ""
)

$ErrorActionPreference = "Stop"

$WorkDir     = "$PSScriptRoot\test_tmp"
$GitRepo     = "$WorkDir\git_repo"
$P4Root      = "$WorkDir\p4root"
$P4Workspace = "$WorkDir\p4workspace"
$P4Port      = "localhost:1667"
$P4User      = "testuser"
$P4Client    = "testclient"
$P4DepotBase = "//depot/myproject"
$P4dPidFile  = "$WorkDir\p4d.pid"

# ── 정리 ────────────────────────────────────────────────────────────────────
if ($Action -eq "clean") {
    Write-Host "=== 테스트 환경 정리 ===" -ForegroundColor Cyan
    if (Test-Path $P4dPidFile) {
        $pid = Get-Content $P4dPidFile
        try { Stop-Process -Id $pid -Force; Write-Host "  p4d 종료 (PID $pid)" } catch {}
        Remove-Item $P4dPidFile -Force
    }
    if (Test-Path $WorkDir)           { Remove-Item $WorkDir -Recurse -Force }
    if (Test-Path ".env.test")        { Remove-Item ".env.test" -Force }
    if (Test-Path "submitted_prs.json") { Remove-Item "submitted_prs.json" -Force }
    if (Test-Path "bitbucket_to_perforce.log") { Remove-Item "bitbucket_to_perforce.log" -Force }
    Write-Host "완료." -ForegroundColor Green
    exit 0
}

# ── 명령어 확인 ───────────────────────────────────────────────────────────────
foreach ($cmd in @("git", "p4", "p4d")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "'$cmd' 를 찾을 수 없습니다. 설치 후 다시 실행하세요."
        exit 1
    }
}

Write-Host "=== 테스트 디렉토리 초기화 ===" -ForegroundColor Cyan
if (Test-Path $WorkDir) { Remove-Item $WorkDir -Recurse -Force }
New-Item -ItemType Directory -Path $GitRepo, $P4Root, $P4Workspace | Out-Null

# ── 테스트용 git 저장소 생성 ─────────────────────────────────────────────────
Write-Host ""
Write-Host "=== 테스트 git 저장소 구성 ===" -ForegroundColor Cyan

git -C $GitRepo init -b master
git -C $GitRepo config user.email "test@example.com"
git -C $GitRepo config user.name "Test User"

# (1) initial commit
New-Item -ItemType Directory -Path "$GitRepo\src" | Out-Null
"# My Project"   | Set-Content "$GitRepo\README.md"
"print('hello')" | Set-Content "$GitRepo\src\main.py"
git -C $GitRepo add .
git -C $GitRepo commit -m "Initial commit"
Write-Host "  [OK] initial commit" -ForegroundColor Green

# (2) master 직접 push 커밋
"VERSION=1.0" | Set-Content "$GitRepo\.env"
git -C $GitRepo add .
git -C $GitRepo commit -m "Add .env config (direct push to master)"
Write-Host "  [OK] direct commit" -ForegroundColor Green

# (3) PR #1 — 파일 추가
git -C $GitRepo checkout -b feature/login
"def login(): pass" | Set-Content "$GitRepo\src\login.py"
git -C $GitRepo add .
git -C $GitRepo commit -m "Add login module"

"def logout(): pass" | Add-Content "$GitRepo\src\login.py"
git -C $GitRepo add .
git -C $GitRepo commit -m "Add logout to login module"

git -C $GitRepo checkout master
git -C $GitRepo merge --no-ff feature/login -m "Merged in feature/login (pull request #1)"
Write-Host "  [OK] PR merge commit (#1)" -ForegroundColor Green

# (4) PR #2 — 파일 수정 + 삭제
git -C $GitRepo checkout -b feature/refactor
"print('world')" | Add-Content "$GitRepo\src\main.py"
Remove-Item "$GitRepo\.env" -Force
git -C $GitRepo add .
git -C $GitRepo commit -m "Refactor main, remove .env"

git -C $GitRepo checkout master
git -C $GitRepo merge --no-ff feature/refactor -m "Merged in feature/refactor (pull request #2)"
Write-Host "  [OK] PR merge commit (#2, 수정+삭제 포함)" -ForegroundColor Green

Write-Host ""
Write-Host "--- git log ---"
git -C $GitRepo log --oneline --graph

# ── 로컬 p4d 기동 ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== 로컬 p4d 기동 (포트 $P4Port) ===" -ForegroundColor Cyan

# 기존 p4d 종료
Get-Process -Name "p4d" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

$p4dProc = Start-Process -FilePath "p4d" `
    -ArgumentList "-r", $P4Root, "-p", $P4Port `
    -RedirectStandardOutput "$WorkDir\p4d.log" `
    -RedirectStandardError  "$WorkDir\p4d_err.log" `
    -PassThru -WindowStyle Hidden
$p4dProc.Id | Set-Content $P4dPidFile
Start-Sleep -Seconds 2
Write-Host "  p4d PID: $($p4dProc.Id)" -ForegroundColor Green

$env:P4PORT  = $P4Port
$env:P4USER  = $P4User
$env:P4CLIENT = $P4Client

# 사용자 생성
@"
User: $P4User
Email: test@example.com
FullName: Test User
"@ | p4 -p $P4Port -u $P4User user -i
Write-Host "  [OK] p4 user 생성" -ForegroundColor Green

# depot 생성
@"
Depot: depot
Owner: $P4User
Type: local
Map: depot/...
"@ | p4 -p $P4Port -u $P4User depot -i
Write-Host "  [OK] depot 생성" -ForegroundColor Green

# 워크스페이스(client) 생성 — Windows 경로는 슬래시로 변환
$P4WorkspaceP4 = $P4Workspace -replace "\\", "/"
@"
Client: $P4Client
Owner: $P4User
Root: $P4Workspace
Options: noallwrite noclobber nocompress unlocked nomodtime normdir
View:
	//depot/... //$P4Client/...
"@ | p4 -p $P4Port -u $P4User client -i
Write-Host "  [OK] p4 client 생성 (root: $P4Workspace)" -ForegroundColor Green

# ── .env.test 생성 ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== .env.test 파일 생성 ===" -ForegroundColor Cyan

# Windows 경로를 슬래시로 변환 (python-dotenv / pathlib 호환)
$GitRepoFwd = $GitRepo -replace "\\", "/"

@"
GIT_REPO_PATH=$GitRepoFwd
GIT_BRANCH=master
GIT_REMOTE=origin
P4PORT=$P4Port
P4USER=$P4User
P4CLIENT=$P4Client
P4PASSWD=
P4DEPOT_BASE=$P4DepotBase
STATE_FILE=submitted_prs.json
"@ | Set-Content ".env.test" -Encoding UTF8
Write-Host "  [OK] .env.test 생성" -ForegroundColor Green

# ── 완료 메시지 ───────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=========================================" -ForegroundColor Yellow
Write-Host " 테스트 환경 준비 완료!"               -ForegroundColor Yellow
Write-Host "=========================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "  git 저장소 : $GitRepo"
Write-Host "  p4d 포트   : $P4Port"
Write-Host "  p4 client  : $P4Client  (root: $P4Workspace)"
Write-Host ""
Write-Host "[1] dry-run 테스트 (git 파싱만 확인):" -ForegroundColor Cyan
Write-Host "    python bitbucket_to_perforce.py --dry-run --no-fetch --env-file .env.test"
Write-Host ""
Write-Host "[2] 실제 submit 테스트:" -ForegroundColor Cyan
Write-Host "    python bitbucket_to_perforce.py --no-fetch --env-file .env.test"
Write-Host ""
Write-Host "[3] 재실행 (스킵 확인):" -ForegroundColor Cyan
Write-Host "    python bitbucket_to_perforce.py --no-fetch --env-file .env.test"
Write-Host ""
Write-Host "[4] 강제 재처리:" -ForegroundColor Cyan
Write-Host "    python bitbucket_to_perforce.py --no-fetch --force --env-file .env.test"
Write-Host ""
Write-Host "[5] changelist 확인:" -ForegroundColor Cyan
Write-Host "    p4 -p $P4Port -u $P4User -c $P4Client changes -l"
Write-Host ""
Write-Host "[6] depot 파일 확인:" -ForegroundColor Cyan
Write-Host "    p4 -p $P4Port -u $P4User files //depot/..."
Write-Host ""
Write-Host "[정리] .\test_setup.ps1 clean" -ForegroundColor Red
