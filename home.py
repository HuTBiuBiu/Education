"""
首页 - 系统概览
在 app.py 中通过 st.navigation 注册为默认页面。
"""

import streamlit as st

from auth import current_user, get_visible_teacher, is_admin, require_login
from database import ROLE_LABELS, get_pending_follow_ups_count, get_stage_statistics, get_total_customers

require_login()


def main():
    """首页 - 系统概览"""
    user = current_user()
    st.title("📊 济南慕尚教育学管系统")
    if user:
        role_label = ROLE_LABELS.get(user.get("role", ""), user.get("role", ""))
        st.caption(f"当前登录：{role_label} {user.get('display_name', '')}")
    st.markdown("---")

    # 快速统计卡片
    teacher = get_visible_teacher()

    col1, col2, col3, col4 = st.columns(4)

    total = get_total_customers(teacher)
    stage_stats = get_stage_statistics(teacher)
    pending_count = get_pending_follow_ups_count(teacher)
    enrolled_count = stage_stats.get("在读", 0)

    col1.metric("总客户数", f"{total} 人")
    col2.metric("在读学员", f"{enrolled_count} 人")
    col3.metric("待跟进任务", f"{pending_count} 条")
    col4.metric("新线索", f"{stage_stats.get('新线索', 0)} 人")

    st.markdown("---")

    # 快速入口
    st.subheader("⚡ 快捷操作")
    qcol1, qcol2 = st.columns(2)

    with qcol1:
        st.info(
            "### 👥 客户管理\n"
            "管理所有客户信息，维护客户生命周期阶段，"
            "添加跟进记录，跟踪客户转化全流程。\n\n"
            "👉 左侧导航栏进入【客户管理】"
        )

    with qcol2:
        st.success(
            "### 📚 课时管理\n"
            "统计昨日课消与今日排课，课时不足的家长卡片提醒，"
            "消课记录与月度统计一目了然。\n\n"
            "👉 左侧导航栏进入【课时管理】"
        )

    if is_admin():
        st.markdown("---")
        st.subheader("👥 员工与角色管理")
        st.info("员工账号、角色设置（人事 / 财务 / 学管 / 教师 / 管理员）请前往侧边栏【员工管理】页面操作。")

main()
