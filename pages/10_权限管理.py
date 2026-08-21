"""
权限管理 - 配置各角色对页面、选项卡的显示权限与操作权限
仅管理员（admin）可访问。管理员本身恒拥有全部权限。
"""

import streamlit as st

from auth import is_admin, require_login
from database import ROLE_LABELS, get_role_permissions, save_role_permissions
from permissions import ACTION_TAB_MAP, PERMISSION_GROUPS, ROLE_ORDER

require_login()
if not is_admin():
    st.error("⛔ 仅管理员可访问【权限管理】页面。")
    st.stop()

st.title("🔐 权限管理")
st.caption(
    "配置各角色对页面、选项卡的显示权限与操作权限；管理员（admin）始终拥有全部权限。\n\n"
    "数据范围说明（系统内置，不在此配置）：学管仅查看自己名下的客户；教师仅查看自己授课的课表；"
    "人事 / 财务 / 管理员可查看全部数据。"
)
st.markdown("---")

role = st.selectbox(
    "选择要配置的角色",
    ROLE_ORDER,
    format_func=lambda r: f"{ROLE_LABELS.get(r, r)}（{r}）",
    key="perm_role_select",
)

# 切换角色时清理旧角色的未保存勾选状态，保证展示与数据库一致
if st.session_state.get("_perm_edit_role") != role:
    for k in [k for k in st.session_state if k.startswith("pbox_")]:
        del st.session_state[k]
    st.session_state["_perm_edit_role"] = role

current = get_role_permissions(role)

st.markdown(f"### {ROLE_LABELS.get(role, role)} 权限设置")
st.caption("勾选 = 允许；取消勾选 = 禁止。修改后点击底部【💾 保存权限】生效。")

with st.form(f"perm_form_{role}"):
    for group_name, group in PERMISSION_GROUPS.items():
        with st.expander(f"{group_name}", expanded=True):
            # 页面权限
            for key, label in group["pages"]:
                st.checkbox(
                    f"📄 页面权限：{label}",
                    value=current.get(key, False),
                    key=f"pbox_{role}_{key}",
                )
            # 选项卡权限 + 其下的功能权限（按「选项卡 → 功能」层级展示）
            tab_keys = {k for k, _ in group["tabs"]}
            for tkey, tlabel in group["tabs"]:
                st.markdown(f"**🗂️ {tlabel}**")
                st.checkbox(
                    f"☑ 查看「{tlabel}」选项卡",
                    value=current.get(tkey, False),
                    key=f"pbox_{role}_{tkey}",
                )
                for akey, alabel in group["actions"]:
                    if ACTION_TAB_MAP.get(akey) == tkey:
                        st.checkbox(
                            f"✏️ 功能权限：{alabel}",
                            value=current.get(akey, False),
                            key=f"pbox_{role}_{akey}",
                        )
            # 不隶属于任何选项卡的操作权限（页面级操作）
            unassigned = [
                (k, l) for k, l in group["actions"]
                if ACTION_TAB_MAP.get(k) not in tab_keys
            ]
            if unassigned:
                st.markdown("**🔧 页面级功能权限**")
                for key, label in unassigned:
                    st.checkbox(
                        f"✏️ 功能权限：{label}",
                        value=current.get(key, False),
                        key=f"pbox_{role}_{key}",
                    )
    submitted = st.form_submit_button("💾 保存权限", type="primary", use_container_width=True)

if submitted:
    mapping = {}
    for group in PERMISSION_GROUPS.values():
        for kind in ("pages", "tabs", "actions"):
            for key, _label in group[kind]:
                mapping[key] = st.session_state.get(f"pbox_{role}_{key}", False)
    save_role_permissions(role, mapping)
    # 清除权限缓存，使本会话立即生效
    st.session_state.pop("_perm_cache", None)
    st.success(f"已保存【{ROLE_LABELS.get(role, role)}】的权限设置，立即生效！")
    st.rerun()
