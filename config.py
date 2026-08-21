"""
Neon PostgreSQL 连接配置

数据库连接字符串（含密码）不再硬编码在代码中，避免泄露到 Git 仓库。
支持三种配置来源，按优先级读取：
  1. 环境变量 NEON_DATABASE_URL（本地环境变量，或云平台的环境变量配置）
  2. Streamlit 云端 Secrets：~/.streamlit/secrets.toml 中的 NEON_DATABASE_URL
     （Streamlit Community Cloud 部署时，在 Settings → Secrets 中配置）
  3. 项目根目录（或打包后 exe 同目录）下的 .env 文件（KEY=VALUE 格式，已被 git 忽略）

连接字符串格式:
    postgresql://用户名:密码@主机名/数据库名?sslmode=require

获取方式: 在 Neon Console 控制台中 → Dashboard → Connection string 复制
"""
import os
import re
import sys
from pathlib import Path

_STREAMILT_SECRETS_PATH = Path.home() / ".streamlit" / "secrets.toml"


def _load_dotenv(dotenv_path: Path) -> bool:
    """轻量加载 .env 文件（KEY=VALUE，支持 # 注释与引号包裹），已存在的环境变量不覆盖"""
    if not dotenv_path.is_file():
        return False
    with open(dotenv_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    return True


def _load_streamlit_secrets(target_key: str) -> str:
    """
    从 Streamlit secrets.toml 读取指定顶层 key 的字符串值。
    Python 3.11+ 使用内置 tomllib；更低的版本退化为轻量 TOML 行解析。
    """
    if not _STREAMILT_SECRETS_PATH.is_file():
        return ""
    try:
        import tomllib  # Python 3.11+

        with open(_STREAMILT_SECRETS_PATH, "rb") as f:
            data = tomllib.load(f)
        value = data.get(target_key, "")
        return value if isinstance(value, str) else ""
    except ImportError:
        pattern = re.compile(
            r'^\s*' + re.escape(target_key) + r'\s*=\s*"((?:[^"\\]|\\.)*)"'
        )
        try:
            with open(_STREAMILT_SECRETS_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    m = pattern.match(line)
                    if m:
                        return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        except OSError:
            pass
        return ""


def _get_database_url() -> str:
    """按优先级获取数据库连接串"""
    # 1. 环境变量（本地环境变量或云平台环境变量）
    url = os.environ.get("NEON_DATABASE_URL", "").strip()
    if url:
        return url

    # 2. Streamlit 云端 Secrets（~/.streamlit/secrets.toml）
    url = _load_streamlit_secrets("NEON_DATABASE_URL").strip()
    if url:
        return url

    # 3. 本地 .env 文件（项目根目录 / 打包后 exe 同目录）
    candidates = [
        Path(__file__).resolve().parent / ".env",
        Path(sys.executable).resolve().parent / ".env" if getattr(sys, "frozen", False) else None,
    ]
    for p in candidates:
        if p and p.is_file():
            _load_dotenv(p)
            url = os.environ.get("NEON_DATABASE_URL", "").strip()
            if url:
                return url
    return ""


DATABASE_URL = _get_database_url()
if not DATABASE_URL:
    raise RuntimeError(
        "未检测到数据库连接配置（NEON_DATABASE_URL）。\n"
        "可通过以下任一方式配置：\n"
        "  1) 本地运行：在项目根目录创建 .env 文件并写入\n"
        "       NEON_DATABASE_URL=postgresql://用户名:密码@主机名/数据库名?sslmode=require\n"
        "  2) Streamlit Cloud 云运行：在 Settings → Secrets 中配置\n"
        "       NEON_DATABASE_URL = \"postgresql://用户名:密码@主机名/数据库名?sslmode=require\"\n"
        "连接字符串可在 Neon Console → Dashboard → Connection string 获取。"
    )
