"""
权限验证模块 - 登录、登出、用户信息展示
所有页面顶部调用 require_login() 进行登录校验
"""
import html as _html

import streamlit as st

from database import (
    verify_login, ROLE_LABELS, get_role_permissions, update_user,
    check_login_locked, check_ip_locked, check_login_rate,
    record_failed_login, clear_failed_logins,
    LOGIN_LOCK_MINUTES, IP_LOCK_MINUTES, PASSWORD_MIN_LENGTH,
)

ROLE_ICONS = {
    "admin": "🛡️",
    "staff": "🧑‍🏫",
    "teacher": "👩‍🏫",
    "hr": "📋",
    "finance": "💰",
}


def current_user() -> dict:
    """返回当前登录用户信息字典，未登录返回 None"""
    return st.session_state.get("user")


def is_admin() -> bool:
    """当前用户是否为管理员"""
    user = current_user()
    return bool(user and user.get("role") == "admin")


def current_role() -> str:
    """返回当前登录用户的角色 key，未登录返回空字符串"""
    user = current_user()
    return user.get("role", "") if user else ""


def can(resource_key: str) -> bool:
    """当前用户是否拥有指定资源权限（管理员恒为全部权限）"""
    role = current_role()
    if role == "admin":
        return True
    cache = st.session_state.get("_perm_cache")
    if cache is None:
        cache = {}
        st.session_state["_perm_cache"] = cache
    if role not in cache:
        cache[role] = get_role_permissions(role)
    return bool(cache[role].get(resource_key, False))


def require_page(page_key: str) -> None:
    """页面访问守卫：无该页面权限则提示并停止执行"""
    if not can(page_key):
        st.error("⛔ 您没有访问该页面的权限，请联系管理员开通。")
        st.stop()


def _get_client_ip() -> str:
    """尽力获取客户端 IP：Streamlit Cloud 经代理时解析 X-Forwarded-For / X-Real-IP。
    旧版 Streamlit 无 st.context 或取不到头时返回空串（调用方自动降级为纯账号级防护）。"""
    try:
        headers = st.context.headers
    except Exception:
        return ""
    if not headers:
        return ""
    for key in ("X-Forwarded-For", "X-Real-IP", "X-Client-IP"):
        val = headers.get(key) or ""
        if val:
            return val.split(",")[0].strip()
    return ""


def login_form() -> None:
    """渲染登录表单"""
    st.markdown(
        """
        <style>
        .login-box {
            max-width: 380px;
            margin: 12vh auto 0 auto;
            padding: 32px 36px;
            border-radius: 16px;
            background: #ffffff;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08);
            border: 1px solid #eee;
        }
        .login-brand {
            background: linear-gradient(135deg, #2b6cb0, #2c5282);
            color: #ffffff;
            font-size: 26px;
            font-weight: 700;
            text-align: center;
            padding: 18px 0;
            margin-bottom: 4px;
            letter-spacing: 2px;
        }
        .login-title { font-size: 22px; font-weight: 700; color: #1a1a1a; text-align: center; margin-bottom: 4px; }
        .login-sub { font-size: 13px; color: #888; text-align: center; margin-bottom: 24px; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown('<div class="login-brand">济南慕尚教育</div>', unsafe_allow_html=True)
    st.markdown('<div class="login-title">学管工作台</div>', unsafe_allow_html=True)
    st.markdown('<div class="login-sub">请登录后使用</div>', unsafe_allow_html=True)

    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("👤 用户名", key="login_username")
        password = st.text_input("🔒 密码", type="password", key="login_password")
        submitted = st.form_submit_button("登 录", use_container_width=True, type="primary")

    if submitted:
        if not username or not password:
            st.error("请输入用户名和密码")
        else:
            uname = username.strip()
            client_ip = _get_client_ip()
            # 防暴力破解三层防护（IP 取不到时自动降级，仅保留账号级锁定）：
            # 1) IP 熔断：同一网络地址 15 分钟内失败过多则整网锁定
            if check_ip_locked(client_ip):
                st.error(f"该网络地址登录失败次数过多，已临时锁定 {IP_LOCK_MINUTES} 分钟，请稍后再试")
            # 2) IP 限流：短时间内尝试过于频繁则临时拒绝
            elif check_login_rate(client_ip):
                st.error("登录尝试过于频繁，请稍候再试")
            # 3) 账号级锁定：单个账号失败次数过多
            elif check_login_locked(uname):
                st.error(f"该账号登录失败次数过多，已临时锁定 {LOGIN_LOCK_MINUTES} 分钟，请稍后再试")
            else:
                user = verify_login(uname, password)
                if user:
                    clear_failed_logins(uname, client_ip)
                    st.session_state.user = user
                    if user.get("must_change_password"):
                        st.warning("🔒 首次登录需先修改初始密码，请在弹出的窗口中设置新密码。")
                        change_password_dialog()
                    else:
                        st.success("登录成功，正在进入系统…")
                        st.rerun()
                else:
                    record_failed_login(uname, client_ip)
                    st.error("用户名或密码错误")

    st.markdown("</div>", unsafe_allow_html=True)


def require_login() -> None:
    """未登录则显示登录界面并停止当前页面执行；已登录但需强制改密时先完成改密"""
    if not current_user():
        login_form()
        st.stop()
    if current_user().get("must_change_password"):
        st.warning("🔒 出于安全考虑，首次登录需先修改初始密码，完成后才能继续使用系统。")
        change_password_dialog()
        st.stop()


@st.dialog("🔒 修改密码", width="small")
def change_password_dialog() -> None:
    """修改当前登录用户的密码（对话框）"""
    user = current_user()
    if not user:
        return
    old_pwd = st.text_input("当前密码", type="password", key="cp_old")
    new_pwd = st.text_input("新密码", type="password", key="cp_new",
                            help=f"至少 {PASSWORD_MIN_LENGTH} 位")
    confirm_pwd = st.text_input("确认新密码", type="password", key="cp_confirm")
    if st.button("确认修改", type="primary", use_container_width=True):
        if len(new_pwd) < PASSWORD_MIN_LENGTH:
            st.error(f"新密码长度至少 {PASSWORD_MIN_LENGTH} 位")
        elif new_pwd != confirm_pwd:
            st.error("两次输入的新密码不一致")
        elif new_pwd == old_pwd:
            st.error("新密码不能与当前密码相同")
        elif not verify_login(user.get("username", ""), old_pwd):
            st.error("当前密码不正确")
        else:
            update_user(user["id"], password=new_pwd, must_change_password=False)
            if st.session_state.get("user"):
                st.session_state["user"] = dict(st.session_state["user"], must_change_password=False)
            st.success("✅ 密码修改成功，请牢记新密码！")
            st.rerun()


def render_user_bar() -> None:
    """在侧边栏展示当前用户信息、修改密码和退出登录按钮"""
    user = current_user()
    if not user:
        return
    with st.sidebar:
        role = user.get("role", "")
        # 用户可控字段（显示名/用户名）一律 HTML 转义后再插入，防止存储型 XSS
        display_name = _html.escape(str(user.get("display_name", "")))
        username = _html.escape(str(user.get("username", "")))
        role_tag = f"{ROLE_ICONS.get(role, '👤')} {_html.escape(str(ROLE_LABELS.get(role, role)))}"
        st.markdown(
            f"""
            <div style="padding:12px 14px;border-radius:10px;background:#f0f2f6;margin-bottom:4px;">
                <div style="font-size:15px;font-weight:700;color:#1a1a1a;">{display_name}</div>
                <div style="font-size:12px;color:#666;">{role_tag} · @{username}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("🔒 修改密码", use_container_width=True):
            change_password_dialog()
        if st.button("🚪 退出登录", use_container_width=True):
            for key in list(st.session_state.keys()):
                if key != "form_submitter":
                    del st.session_state[key]
            st.rerun()


def get_visible_teacher() -> str:
    """返回当前用户应使用的学管过滤值：管理员/人事/财务查看全部，学管/教师仅看自己名下客户"""
    user = current_user()
    if not user:
        return ""
    role = user.get("role", "")
    if role in ("admin", "hr", "finance"):
        return ""
    return user.get("username", "")


def get_viewer_scope() -> tuple:
    """返回当前用户的数据范围 (role, name)：
    admin/人事/财务 → ('all', '')；学管 → ('staff', username)；教师 → ('teacher', username)"""
    user = current_user()
    if not user:
        return ("all", "")
    role = user.get("role", "")
    if role in ("admin", "hr", "finance"):
        return ("all", "")
    return (role, user.get("username", ""))
