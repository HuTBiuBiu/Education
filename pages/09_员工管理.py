"""
员工管理页面（管理员 / 人事可访问）
管理员工账号与角色：人事、财务、学管、教师、管理员。
权限细化：管理员可管理全部角色；人事可进入页面，但看不到/操作不了管理员账号，
且角色选项不含「管理员」。
"""
import streamlit as st
import pandas as pd

from auth import can, is_admin, require_login, require_page
from database import (
    ROLE_LABELS, get_all_users, add_user, update_user, delete_user,
    PASSWORD_MIN_LENGTH,
)

# ---------- 登录校验与权限守卫 ----------
require_login()
require_page("page_staff")  # admin 恒可访问；其余角色需拥有 page_staff（默认仅人事）

st.title("👥 员工管理")
st.caption("管理员工账号与角色。角色说明：管理员（全部权限）、人事、财务、学管（管理名下客户）、教师（授课与课表）。")

# 非管理员角色不可见/不可选「管理员」角色
if is_admin():
    ROLE_ORDER = ["admin", "staff", "teacher", "hr", "finance"]
else:
    ROLE_ORDER = ["staff", "teacher", "hr", "finance"]
ROLE_OPTIONS = {ROLE_LABELS[r]: r for r in ROLE_ORDER}

# ---------- 员工列表 ----------
st.subheader("🗂️ 员工列表")
users = get_all_users()
if not is_admin():
    users = [u for u in users if u.get("role") != "admin"]
if not users:
    st.info("暂无员工账号。")
else:
    df = pd.DataFrame([{
        "ID": i,
        "用户名": u["username"],
        "姓名": u["display_name"],
        "角色": ROLE_LABELS.get(u.get("role", ""), u.get("role", "")),
        "可带科目": u.get("subjects", "") or "—",
        "创建时间": u.get("created_at", ""),
    } for i, u in enumerate(users, 1)])
    st.dataframe(df, use_container_width=True, hide_index=True)

# ---------- 添加员工（需操作权限，默认人事可添加） ----------
if can("action_staff_add"):
    st.markdown("---")
    st.subheader("➕ 添加员工")
    with st.form("add_user_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            username = st.text_input("登录用户名", placeholder="例如：zhangsan")
        with c2:
            display_name = st.text_input("姓名", placeholder="例如：张三")
        c3, c4 = st.columns(2)
        with c3:
            password = st.text_input("初始密码", type="password")
        with c4:
            # 默认学管（角色选项对非管理员不含「管理员」，见上方 ROLE_ORDER）
            default_idx = list(ROLE_OPTIONS.keys()).index(ROLE_LABELS["staff"]) if ROLE_LABELS["staff"] in ROLE_OPTIONS else 0
            role_label = st.selectbox("角色", list(ROLE_OPTIONS.keys()), index=default_idx)
            role = ROLE_OPTIONS[role_label]
        c5, c6 = st.columns(2)
        with c5:
            subjects = st.text_input("可带科目（教师填，逗号分隔）", placeholder="例如：钢琴,声乐,乐理")
        with c6:
            st.caption("教师可带科目将用于 1v1 排课时选择，填逗号分隔的科目名即可。")
        submitted = st.form_submit_button("添加员工", type="primary")

    if submitted:
        if not username.strip() or not display_name.strip() or not password:
            st.error("用户名、姓名、密码均不能为空")
        elif len(password) < PASSWORD_MIN_LENGTH:
            st.error(f"初始密码长度至少 {PASSWORD_MIN_LENGTH} 位")
        else:
            if add_user(username.strip(), password, display_name.strip(), role,
                        subjects=subjects.strip()):
                st.success(f"员工「{display_name.strip()}」添加成功，该员工首次登录需先修改初始密码")
                st.rerun()
            else:
                st.error("用户名已存在，请换一个用户名")

# ---------- 编辑角色 / 重置密码 / 删除 ----------
# user_ops 基于已过滤的 users：非管理员角色看不到「管理员」账号，无法维护
st.markdown("---")
st.subheader("🛠️ 角色与账号维护")

user_ops = {f"#{i} {u['display_name']}（@{u['username']} · {ROLE_LABELS.get(u.get('role', ''), '')}）": u
            for i, u in enumerate(users, 1)}
if not user_ops:
    st.info("暂无员工账号。")
else:
    sel_label = st.selectbox("选择员工", list(user_ops.keys()))
    target = user_ops[sel_label]
    target_id = target["id"]

    t1, t2 = st.columns(2)
    with t1:
        if can("action_staff_edit_role"):
            st.markdown("**修改角色**")
            new_role_label = st.selectbox("新角色", list(ROLE_OPTIONS.keys()),
                                          index=list(ROLE_OPTIONS.keys()).index(
                                              ROLE_LABELS.get(target.get("role", "staff"), "学管")) if ROLE_LABELS.get(target.get("role", "")) in ROLE_OPTIONS else 2)
            if st.button("保存角色", type="primary"):
                if target.get("username") == "admin":
                    st.error("不能修改 admin 管理员的角色")
                else:
                    update_user(target_id, role=ROLE_OPTIONS[new_role_label])
                    st.success("角色已更新")
                    st.rerun()
    with t2:
        if can("action_staff_reset_pwd"):
            st.markdown("**重置密码**")
            new_pwd = st.text_input("新密码", type="password", key="reset_pwd",
                                    help=f"至少 {PASSWORD_MIN_LENGTH} 位；重置后该员工下次登录需先修改密码")
            if st.button("重置密码", type="secondary"):
                if not new_pwd:
                    st.error("请输入新密码")
                elif len(new_pwd) < PASSWORD_MIN_LENGTH:
                    st.error(f"新密码长度至少 {PASSWORD_MIN_LENGTH} 位")
                else:
                    update_user(target_id, password=new_pwd, must_change_password=True)
                    st.success("密码已重置，该员工下次登录需先修改密码")
                    st.rerun()

    if can("action_staff_edit_role"):
        st.markdown("---")
        st.markdown("**可带科目**（教师授课科目，逗号分隔；排 1v1 课将从中选择科目）")
        c_sub = st.columns([3, 1])
        with c_sub[0]:
            cur_subjects = target.get("subjects", "") or ""
            new_subjects = st.text_input("可带科目", value=cur_subjects, key=f"subs_{target_id}")
        with c_sub[1]:
            st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
            if st.button("保存科目", type="secondary", key=f"save_subs_{target_id}"):
                update_user(target_id, subjects=new_subjects.strip())
                st.success("可带科目已更新")
                st.rerun()

    if can("action_staff_delete"):
        st.markdown("---")
        st.markdown("**删除账号**（删除后该员工将无法登录；其名下客户自动转为未分配）")
        if st.button("删除该员工", type="secondary"):
            if target.get("username") == "admin":
                st.error("不能删除 admin 管理员")
            else:
                delete_user(target_id)
                st.success("员工已删除")
                st.rerun()
