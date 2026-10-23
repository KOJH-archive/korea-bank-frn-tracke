"""은행 FRN 데이터 갱신과 Streamlit 대시보드를 순서대로 실행한다."""
import os
import sys
import subprocess
import argparse

def main():
    parser = argparse.ArgumentParser(description="은행 FRN 대시보드 실행")
    parser.add_argument("--offline", action="store_true", help="API ETL을 생략하고 기존 DB를 조회")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    project_dir = os.path.dirname(os.path.abspath(__file__))
    venv_python = os.path.join(project_dir, ".venv", "Scripts", "python.exe")

    if not os.path.exists(venv_python):
        venv_python = sys.executable  # 가상환경이 없으면 현재 파이썬 사용

    print("=" * 60)
    print("Korea Bank FRN Tracker")
    print("=" * 60)

    if not args.offline:
        print("\n[Step 1] FRN 데이터 갱신 및 검증...")
        subprocess.run([venv_python, "refresh_db.py"], check=True, cwd=project_dir)

    # 2. Streamlit 대시보드 구동
    print("\n[Step 2] Streamlit 대시보드 실행...")
    print("종료하려면 Ctrl+C를 누르세요.\n")
    try:
        subprocess.run([venv_python, "-m", "streamlit", "run", "app.py"], check=True, cwd=project_dir)
    except KeyboardInterrupt:
        print("\n프로그램이 종료되었습니다.")

if __name__ == "__main__":
    main()
