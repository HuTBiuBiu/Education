"""
跟进提醒工作台
集中管理所有跟进任务，按创建时间自动标注醒目度，支持快速添加和状态更新
"""

import streamlit as st
import pandas as pd
from datetime import datetime
from auth import can, get_visible_teacher, require_login, require_page
from database import (
    add_follow_up, get_all_customers, get_stale_customers,
)

# ---------- 登录校验与权限守卫 ----------
require_login()
require_page("page_followup")

TEACHER = get_visible_teacher()

st.title("⏰ 跟进提醒工作台")

today = datetime.now()


# ---------- 辅助函数 ----------
def fmt_time(dt_str: str) -> str:
    """把数据库时间字符串精简为 YYYY-MM-DD HH:MM 的可读格式"""
    if not dt_str:
        return ""
    return dt_str[:16]  # 截取 "2026-08-06 14:32"


def stale_icon(days_ago: int) -> str:
    """按距今天数返回醒目度图标"""
    if days_ago >= 30:
        return "🔴"
    elif days_ago >= 14:
        return "🟠"
    elif days_ago >= 7:
        return "🟡"
    elif days_ago >= 5:
        return "⚪"
    else:
        return "🟢"


def stale_cell_style(v):
    """按距今天数为表格单元格着色（保留醒目度设计）"""
    if v >= 30:
        return "background:#ffe0e0;color:#d62728;font-weight:bold"
    elif v >= 14:
        return "background:#ffe8d6;color:#e65100;font-weight:bold"
    elif v >= 7:
        return "background:#fff3cd;color:#c77d0a;font-weight:bold"
    elif v >= 5:
        return "background:#f2f2f2;color:#666"
    else:
        return "background:#e8f5e9;color:#1b7a1b"


# ---------- 从客户管理跳转过来的预选客户 ----------
target_customer_id = st.session_state.pop("follow_target_customer", None)

# ---------- 添加跟进任务面板（需操作权限；从客户页跳转时自动展开） ----------
if can("action_followups_add"):
    with st.expander("➕ 添加跟进任务", expanded=bool(target_customer_id)):
        customers = get_all_customers(teacher=TEACHER)
        if customers:
            cust_options = {f"{i}. {c['name']}": c["id"] for i, c in enumerate(customers, 1)}
            cust_keys = list(cust_options.keys())

            # 如果有跳转过来的目标客户，自动选中
            default_index = 0
            if target_customer_id:
                for key, cid in cust_options.items():
                    if cid == target_customer_id:
                        default_index = cust_keys.index(key)
                        break

            col_a, col_b = st.columns(2)
            with col_a:
                selected_cust = st.selectbox("选择客户", cust_keys, index=default_index)
                quick_type = st.selectbox("跟进方式", ["电话", "企微", "面谈", "其他"])
            with col_b:
                quick_date = st.date_input("计划日期", value=today)
                quick_time = st.time_input(
                    "计划时间", value=today.time().replace(second=0, microsecond=0)
                )

            quick_content = st.text_area("跟进内容", height=80, placeholder="请输入本次跟进内容...")

            c_save, c_cancel, _ = st.columns([1, 1, 6])
            with c_save:
                if st.button("💾 保存跟进任务", use_container_width=True, type="primary"):
                    if quick_content.strip():
                        plan_time = f"{quick_date.strftime('%Y-%m-%d')} {quick_time.strftime('%H:%M')}"
                        add_follow_up(
                            cust_options[selected_cust], quick_type, quick_content.strip(), plan_time
                        )
                        st.success("跟进任务已添加！")
                        st.rerun()
                    else:
                        st.error("请输入跟进内容")
        else:
            st.info("暂无客户，请先在客户管理中添加客户。")

# ---------- 查询过期客户（按学管权限过滤） ----------
stale_customers = get_stale_customers(days=5, teacher=TEACHER)

st.markdown("---")

# ---------- 过期客户提醒 ----------
st.subheader("📋 跟进任务列表")

if not stale_customers:
    st.info("暂无超过 5 日未跟进的客户。全部跟进记录请前往「客户管理 → 跟进记录」页面查看。")
else:
    st.markdown("**🔔 超过 5 日未跟进的客户**")
    df_stale = pd.DataFrame([{
        "ID": i,
        "提醒": stale_icon(sc.get("days_since_touch", 0)),
        "姓名": sc["name"],
        "生命周期": sc.get("lifecycle_stage", ""),
        "电话": sc.get("phone") or "-",
        "上次跟进": fmt_time(sc.get("last_follow_time", "")),
        "距今天数": sc.get("days_since_touch", 0),
    } for i, sc in enumerate(stale_customers, 1)])
    st.dataframe(df_stale.style.map(stale_cell_style, subset=["距今天数"]),
                 use_container_width=True, hide_index=True)

    if can("action_customers_follow"):
        stale_ops = {f"#{i} {sc['name']}（{sc.get('lifecycle_stage', '')}）": sc["id"]
                     for i, sc in enumerate(stale_customers, 1)}
        stale_sel = st.selectbox("选择需要跟进的客户", list(stale_ops.keys()), key="stale_sel")
        if st.button("📝 跟进该客户", type="primary"):
            st.session_state.follow_target_customer = stale_ops[stale_sel]
            st.rerun()

st.markdown("---")
st.caption("💡 上方为超过5日未跟进的客户提醒；全部跟进记录请前往「客户管理 → 跟进记录」页面查看。")
