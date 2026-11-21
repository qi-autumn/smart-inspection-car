# -*- coding: utf-8 -*-
"""Run this before pushing the single-repository project to GitHub.

    cd E:\\Harmony\\GitHub-OneRepo
    python verify_before_push.py

Exit code 0 = safe to push. Non-zero = fix the reported items first.
Scans the three version folders plus the shared root files.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSIONS = ["01-TCP-V1", "02-Cloud-V2", "03-Fleet-V3"]

TEXT_EXT = {".py", ".json", ".json5", ".ets", ".ts", ".md", ".html", ".htm",
            ".bat", ".txt", ".properties", ".yaml", ".yml", ".xml", ".js",
            ".cfg", ".gitignore"}

RULES = [
    ("API Key 字面量", r"sk-[A-Za-z0-9_\-]{12,}"),
    ("设备密钥字面量", r"Car123456\d*"),
    ("账号专属标识", r"6ae6e3efcbb0cf6bb993084e|01a195e0cca67d2487b58248caedc495|9dcc8e0e1e"),
    ("本机绝对路径", r"C:[\\/]+Users[\\/]+HP"),
    ("本机签名配置", r'"(?:keyPassword|storePassword)"\s*:|signingConfigs\s*:\s*\[\s*\{'),
    ("私钥 PEM 块", r"-----BEGIN [A-Z ]+-----"),
    ("长十六进制标识", r"\b[0-9A-Fa-f]{40,}\b"),
    ("非空凭据字段", r'"(?:ak|sk|api_key|secret|default_secret)"\s*:\s*"(?!YOUR_|<)[^"]{8,}"'),
]

BAD_SUFFIX = {".db", ".db-wal", ".db-shm", ".log", ".p12", ".p7b", ".cer",
              ".pem", ".hap", ".jar", ".pyc", ".pyo"}
BAD_DIRS = {".idea", ".hvigor", "build", "oh_modules", "__pycache__",
            "node_modules", ".pytest_cache"}
BAD_NAMES = {"local.properties", "Thumbs.db", ".DS_Store"}

REQUIRED = {
    "01-TCP-V1": [
        "README.md",
        "app/entry/src/main/ets/pages/Index.ets",
        "app/entry/src/main/ets/net/CarTcpClient.ets",
        "simulator/car_server.py", "simulator/acceptance_test.py",
        "docs/项目分层验收方案.md",
    ],
    "02-Cloud-V2": [
        "README.md", "set_env.example.bat",
        "backend/cloud_backend.py", "backend/cloud_backend_dual.py",
        "backend/cloud_backend_fleet.py", "backend/car_simulator.py",
        "profile/Car001-产品模型.zip",
        "app/entry/src/main/ets/net/CloudClient.ets",
    ],
    "03-Fleet-V3": [
        "README.md", "setup.py", "start_backend.bat",
        "server/app.py", "server/ai_agent.py", "server/track_store.py",
        "server/ai_config.example.json", "server/cloud_config.example.json",
        "server/devices.example.json", "device/car_mqtt_client.py",
        "web/monitor.html", "web/ai_chat.html", "docs/V3-学习导航.html",
        "app/entry/src/main/ets/pages/Index.ets",
    ],
}

ROOT_FILES = ["README.md", ".gitignore", "verify_before_push.py",
              "docs/V3-学-单仓库还是三个仓库.html"]


def main():
    problems = []

    for rel in ROOT_FILES:
        if not (ROOT / rel).exists():
            problems.append(f"[根目录] 缺少必备文件: {rel}")

    for v in VERSIONS:
        base = ROOT / v
        if not base.exists():
            problems.append(f"[{v}] 目录不存在")
            continue
        for p in base.rglob("*"):
            parts = p.relative_to(base).parts
            if ".git" in parts:          # skip Git internals after `git init`
                continue
            if p.is_dir():
                if p.name in BAD_DIRS:
                    problems.append(f"[{v}] 不该存在的目录: {p.relative_to(base)}")
                continue
            rel = p.relative_to(base).as_posix()
            if p.name in BAD_NAMES or p.suffix.lower() in BAD_SUFFIX:
                problems.append(f"[{v}] 不该上传的文件: {rel}")
            if p.suffix.lower() not in TEXT_EXT and p.name != ".gitignore":
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            for label, pat in RULES:
                for m in re.finditer(pat, text):
                    ln = text.count("\n", 0, m.start()) + 1
                    problems.append(f"[{v}] {label}: {rel}:{ln}")
        for rel in REQUIRED[v]:
            if not (base / rel).exists():
                problems.append(f"[{v}] 缺少必备文件: {rel}")

    print("=" * 60)
    print("GitHub 上传前复检（单仓库版）")
    print("=" * 60)
    for v in VERSIONS:
        fs = [p for p in (ROOT / v).rglob("*")
              if p.is_file() and ".git" not in p.relative_to(ROOT / v).parts]
        size = sum(p.stat().st_size for p in fs)
        print(f"  {v}: {len(fs)} 个文件 / {size/1024/1024:.2f} MB")
    print(f"  根目录必备文件: {'OK' if all((ROOT / r).exists() for r in ROOT_FILES) else '缺失'}")
    if problems:
        print(f"\n[FAIL] 发现 {len(problems)} 个问题：")
        for p in problems[:60]:
            print("  -", p)
        if len(problems) > 60:
            print(f"  ... 还有 {len(problems)-60} 条")
        return 1
    print("\n[PASS] 未发现真实凭据、本机路径或不应上传的产物；必备文件齐全。")
    print("提示：这只是静态扫描。推送前仍请肉眼确认 git add 的文件列表。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
