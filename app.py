"""
济南慕尚教育学管系统 - 主入口
使用 st.navigation 动态构建侧边栏导航，按当前角色的页面权限过滤可见页面；
同时关闭 Streamlit 对 pages/ 目录的自动发现，保证权限控制的唯一入口。
"""

import streamlit as st

from auth import can, current_user, render_user_bar, require_login
from database import init_db

# ---------- 页面配置 ----------
st.set_page_config(
    page_title="济南慕尚教育学管系统",
    page_icon="🎹",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------- 初始化数据库 ----------
# init_db 含建表/补列等 DDL，结果按小时缓存，避免每次界面操作都重复执行
@st.cache_data(ttl=3600, show_spinner=False)
def _init_db_cached() -> bool:
    init_db()
    return True


# ---------- 登录校验与用户栏 ----------
if not current_user():
    # 未登录：隐藏左侧选项栏，仅展示登录表单
    # 注意：必须在耗时的 init_db 之前注入，否则侧边栏会先以展开状态闪现几秒
    st.markdown(
        '<style>[data-testid="stSidebar"]{display:none!important;}</style>',
        unsafe_allow_html=True,
    )
    # 关键修复：未登录时也必须显式调用 st.navigation()。
    # 若只注入 CSS 而不注册导航，只要项目存在 pages/ 目录，Streamlit 在收不到
    # st.navigation() 配置时就会回退到"自动页面导航"（auto-pages），
    # 导致登录前的侧边栏依旧显示 01_客户管理、03_跟进提醒 等全部业务菜单。
    # 这里仅注册一个隐藏的登录页（导航菜单为空），页面内容由 home.py 顶部的
    # require_login() 渲染登录表单，登录后按角色权限重建导航。
    _init_db_cached()
    st.navigation(
        [st.Page("home.py", title="登录", url_path="login", default=True, visibility="hidden")],
        position="sidebar",
    ).run()
    st.stop()

_init_db_cached()
require_login()
render_user_bar()

# ---------- 页面注册（按角色权限过滤） ----------
PAGE_DEFS = [
    ("page_home", "🏠 首页", "home.py"),
    ("page_customers", "👥 客户管理", "pages/01_客户管理.py"),
    ("page_followup", "⏰ 跟进提醒", "pages/03_跟进提醒.py"),
    ("page_hours", "📚 课时管理", "pages/04_课时管理.py"),
    ("page_io", "📥 导入导出", "pages/05_导入导出.py"),
    ("page_classes", "🏫 班级管理", "pages/07_班级管理.py"),
    ("page_schedules", "🗓️ 课表管理", "pages/08_课表管理.py"),
    ("page_staff", "👷 员工管理", "pages/09_员工管理.py"),
    ("page_permissions", "🔐 权限管理", "pages/10_权限管理.py"),
]

visible = [(key, title, path) for key, title, path in PAGE_DEFS if can(key)]
if not visible:
    st.error("当前账号未分配任何页面权限，请联系管理员在【权限管理】中配置。")
    st.stop()

pages = [
    st.Page(path, title=title, default=(i == 0))
    for i, (_, title, path) in enumerate(visible)
]
pg = st.navigation(pages, position="sidebar")
pg.run()
