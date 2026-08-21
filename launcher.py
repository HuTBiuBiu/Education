"""
启动器脚本
- 开发环境：直接 python launcher.py 启动
- 打包后：作为 PyInstaller 入口，双击 exe 自动启动服务并打开浏览器
"""

import os
import sys
import webbrowser
import threading
import time
import socket


# ---------- 配置 ----------
SERVER_PORT = 8501
BROWSER_OPEN_DELAY = 2  # 等待服务器启动后再打开浏览器（秒）
# 监听地址：默认仅本机可访问（安全）。
# 需要局域网/其他设备访问时设置环境变量 MUSHANG_HOST=0.0.0.0
#（请确保处于可信内网，跨公网访问请配合 Nginx/HTTPS 使用）
SERVER_HOST = os.environ.get("MUSHANG_HOST", "127.0.0.1")
# 上传文件大小上限（MB），防止超大文件导致内存耗尽（DoS）
MAX_UPLOAD_MB = 10


def find_free_port(start: int = 8501) -> int:
    """自动寻找可用端口"""
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def get_base_dir() -> str:
    """
    获取应用根目录
    PyInstaller 打包后文件在 sys._MEIPASS 临时目录中
    开发环境则返回脚本所在目录
    """
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    else:
        return os.path.dirname(os.path.abspath(__file__))


def open_browser(url: str, delay: float = BROWSER_OPEN_DELAY):
    """延迟后打开浏览器"""
    time.sleep(delay)
    webbrowser.open(url)


def main():
    base_dir = get_base_dir()
    app_path = os.path.join(base_dir, "app.py")

    # 确保根目录在 sys.path 最前面，保证所有模块导入正常
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)

    # 确保 app.py 存在
    if not os.path.exists(app_path):
        print(f"[错误] 找不到 app.py，路径：{app_path}")
        print("请确保 app.py 与启动器在同一目录下。")
        input("按回车键退出...")
        return

    # 数据库连接配置检查：config 会自动加载 .env（已在 .gitignore 中忽略，不提交到 Git）
    try:
        from config import DATABASE_URL
        if not DATABASE_URL:
            raise ValueError("数据库连接字符串为空")
    except Exception:
        print("[错误] 未配置数据库连接（NEON_DATABASE_URL）。")
        print("  本地运行：请通过环境变量设置，或在项目根目录创建 .env 文件，内容示例：")
        print("    NEON_DATABASE_URL=postgresql://用户名:密码@主机名/数据库名?sslmode=require")
        print("  Streamlit Cloud 云运行：Settings → Secrets 中配置 NEON_DATABASE_URL。")
        print("  连接字符串可在 Neon Console → Dashboard → Connection string 获取。")
        input("按回车键退出...")
        return

    port = find_free_port(SERVER_PORT)
    if SERVER_HOST in ("", "0.0.0.0"):
        url = f"http://localhost:{port}"
        host_desc = "局域网/本机均可访问"
    else:
        url = f"http://{SERVER_HOST}:{port}"
        host_desc = "仅本机可访问"

    print("=" * 50)
    print("  学管客户管理系统 v1.0")
    print("=" * 50)
    print(f"  服务端口: {port}")
    print(f"  监听地址: {SERVER_HOST}（{host_desc}）")
    print(f"  本地地址: {url}")
    print(f"  数据库:   Neon PostgreSQL")
    print("-" * 50)
    print("  正在启动服务，浏览器将自动打开...")
    print("  关闭此窗口即可停止服务。")
    print("=" * 50)
    if SERVER_HOST in ("", "0.0.0.0"):
        print("[提示] 当前监听 0.0.0.0，局域网内其他设备可访问。")
        print("[提示] 请务必仅在可信内网使用；跨公网访问请配置 HTTPS（如 Nginx 反向代理）。")
    else:
        print("[提示] 当前仅本机可访问。如需局域网访问，请设置环境变量 MUSHANG_HOST=0.0.0.0 后重启。")

    # 后台线程：延迟打开浏览器
    threading.Thread(target=open_browser, args=(url,), daemon=True).start()

    # 启动 Streamlit 服务（使用内置 CLI 方式，避免 subprocess 兼容问题）
    from streamlit.web import cli as stcli

    sys.argv = [
        "streamlit",
        "run",
        app_path,
        "--server.port", str(port),
        "--server.address", SERVER_HOST,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--server.maxUploadSize", str(MAX_UPLOAD_MB),
        "--global.developmentMode", "false",
        # 注：不显式设置 enableCORS / enableXsrfProtection，保持 Streamlit 默认开启的
        # CORS 保护与 XSRF 防护，防止跨站请求伪造等攻击
    ]

    try:
        stcli.main()
    except SystemExit:
        pass  # Streamlit 退出时捕获 SystemExit，防止控制台闪退


if __name__ == "__main__":
    main()
