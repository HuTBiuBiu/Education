"""
客户管理页面
管理客户生命周期：新线索 → 已加企微 → 预约试听 → 到访 → 已试听未成交 → 在读 → 待续费 → 流失
支持客户增删改查、阶段流转、跟进记录管理
"""

import streamlit as st
import pandas as pd
from datetime import datetime
from auth import can, current_user, get_visible_teacher, is_admin, require_login, require_page
from database import (
    LIFECYCLE_STAGES, SOURCE_CHANNELS, INTENT_FRUIT_OPTIONS, GRADE_OPTIONS,
    get_all_customers, get_customer_by_id, get_all_users,
    add_customer, update_customer, delete_customer,
    add_follow_up, get_follow_ups, update_follow_up, delete_follow_up,
    add_course_package, get_course_package_templates,
)

# ---------- 登录校验与权限守卫 ----------
require_login()
require_page("page_customers")

TEACHER = get_visible_teacher()          # 当前用户的学管过滤值（admin 为空串）
IS_ADMIN = is_admin()

# ---------- 会话状态管理 ----------
if "edit_customer_id" not in st.session_state:
    st.session_state.edit_customer_id = None


def reset_form():
    """重置表单状态"""
    st.session_state.edit_customer_id = None


# ---------- 跟进记录辅助函数 ----------
def fmt_time(dt_str: str) -> str:
    """把数据库时间字符串精简为 YYYY-MM-DD HH:MM 的可读格式"""
    if not dt_str:
        return ""
    return dt_str[:16]


def parse_days_ago(dt_str: str) -> int:
    """解析时间字符串返回距今天数"""
    if not dt_str:
        return 0
    today = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return (today - datetime.strptime(dt_str[:19], fmt)).days
        except ValueError:
            continue
    return 0


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


@st.dialog("📝 填写跟进信息")
def follow_up_dialog(cust):
    """跟进完成对话框：填写本次跟进情况，保存并标记为已完成"""
    st.markdown(f"**客户：** {cust['name']}　|　阶段：{cust['lifecycle_stage']}")
    fu_type = st.selectbox("跟进方式", ["电话", "企微", "面谈", "其他"], key="dlg_fu_type")
    fu_content = st.text_area(
        "跟进内容", height=120, placeholder="请输入本次跟进情况...", key="dlg_fu_content"
    )
    plan_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    d1, d2 = st.columns(2)
    with d1:
        if st.button("✅ 完成", type="primary", use_container_width=True):
            if fu_content.strip():
                new_id = add_follow_up(cust["id"], fu_type, fu_content.strip(), plan_time)
                update_follow_up(new_id, status="已完成")
                st.success(f"已记录对「{cust['name']}」的跟进并完成！")
                st.rerun()
            else:
                st.error("请输入跟进内容")
    with d2:
        if st.button("❌ 取消", use_container_width=True):
            st.rerun()


@st.dialog("📦 填写课时包信息", width="large",
           on_dismiss=lambda: st.session_state.pop("pending_stage_customer", None))
def course_package_dialog(cust):
    """学员转为「在读」时，弹出对话框填写购买的课时包信息"""
    cust_grade = (cust.get("grade") or "").strip()
    st.markdown(
        f"**学员：** {cust['name']}　|　阶段：{cust['lifecycle_stage']} → **在读**　|　"
        f"**年级：** {cust_grade or '（未填写）'}"
    )
    st.caption("该学员将转入在读阶段，请填写其购买的课时包信息（仅显示当前年级对应的启用课时包）。")

    # 预设课时包选择（在「课时管理 → 课时包」中维护），仅显示该年级或「不限年级」的启用模板
    templates = get_course_package_templates(status="启用", grade=cust_grade)
    tpl_options = {}
    if templates:
        tpl_options = {
            f"{t['name']}（{float(t['total_hours']):.0f}课时 / ¥{float(t['price']):.0f}｜{t.get('type') or '1v1'}）"
            + ("" if not (t.get("grade") or "").strip() else f"｜{t['grade']}"): t
            for t in templates
        }
        tpl_options["✍️ 自定义"] = None
    else:
        st.info("当前年级暂无匹配的启用课时包，请手动填写信息，或先在「课时管理 → 课时包」中新建该年级的课时包。")

    def _apply_pkg_template():
        tpl = tpl_options.get(st.session_state.get("pkg_tpl_sel", ""))
        if tpl:
            st.session_state["pkg_name"] = tpl["name"]
            st.session_state["pkg_hours"] = float(tpl["total_hours"])
            st.session_state["pkg_price"] = float(tpl["price"])
            st.session_state["pkg_type"] = tpl.get("type") or "1v1"
        else:
            # 选择「自定义」时没有预设模板，原价归零（原价仅随所选课时包自动带入，不可手动填写）
            st.session_state["pkg_price"] = 0.0
            st.session_state.setdefault("pkg_type", "1v1")

    if tpl_options:
        st.selectbox("选择报名课时包 *", list(tpl_options.keys()),
                     key="pkg_tpl_sel", on_change=_apply_pkg_template)
        # 首次打开时按当前选中的课时包预填名称/到手课时/原价
        if "pkg_name" not in st.session_state:
            _apply_pkg_template()

    # 选中了预设课时包时，类型与名称跟随所选课时包自动带出，不可手动修改；选择「自定义」时方可手动填写
    _selected_tpl = tpl_options.get(st.session_state.get("pkg_tpl_sel", ""))
    _pkg_locked = bool(_selected_tpl)

    pkg_type = st.selectbox(
        "课时包类型 *", ["1v1", "1v多"], index=0, key="pkg_type",
        format_func=lambda t: "一对一（1v1）" if t == "1v1" else "一对多（1v多）",
        disabled=_pkg_locked,
        help="根据所选课时包自动带出，不可修改" if _pkg_locked
        else "选择「1v1」用于一对一课程，选择「1v多」用于班级（一对多）课程；同类型的多个课时包可组合使用",
    )

    pkg_name = st.text_input("课时包名称 *", value=f"{cust['name']}的课时包", key="pkg_name",
                             disabled=_pkg_locked,
                             help="根据所选课时包自动带出，不可修改" if _pkg_locked
                             else "选择「自定义」后可按需填写")

    pc1, pc2 = st.columns(2)
    with pc1:
        pkg_price = st.number_input("原价（元）", min_value=0.0, step=100.0, format="%.2f",
                                    value=0.0, key="pkg_price", disabled=True,
                                    help="按所选课时包自动带入，不可修改")
        pkg_hours = st.number_input("到手课时（节） *", min_value=0.5, step=1.0, format="%.1f",
                                    value=1.0, key="pkg_hours")
        pkg_purchase = st.date_input("购买日期", value=datetime.now().date(), key="pkg_purchase")
    with pc2:
        pkg_discount = st.number_input("优惠价格（元）", min_value=0.0, step=50.0, format="%.2f",
                                       value=0.0, key="pkg_discount",
                                       help="默认 0，实收价格 = 原价 - 优惠价格")
        pkg_expiry = st.date_input("到期日期", value=datetime.now().date(), key="pkg_expiry")

    # 自动计算：实收价格 = 原价 - 优惠价格；实际单课时价格 = 实收价格 ÷ 到手课时
    received_amount = max(0.0, float(pkg_price) - float(pkg_discount))
    unit_price = received_amount / float(pkg_hours) if float(pkg_hours) > 0 else 0.0
    m1, m2 = st.columns(2)
    with m1:
        st.metric("实收价格（元）", f"{received_amount:,.2f}",
                  help="实收价格 = 原价 - 优惠价格（自动计算）")
    with m2:
        st.metric("实际单课时价格（元/节）", f"{unit_price:,.2f}",
                  help="实际单课时价格 = 实收价格 ÷ 到手课时（自动计算）")

    pkg_notes = st.text_area("备注", placeholder="可选，例如：包含教材费、赠送课时等...",
                             key="pkg_notes")

    b1, b2 = st.columns(2)
    with b1:
        if can("action_customers_pkg_add"):
            if st.button("💾 保存课时包", type="primary", use_container_width=True):
                if pkg_name.strip() and pkg_hours > 0:
                    add_course_package(
                        cust["id"], pkg_name.strip(), float(pkg_hours),
                        pkg_purchase.strftime("%Y-%m-%d"), pkg_expiry.strftime("%Y-%m-%d"),
                        float(received_amount), pkg_notes.strip(),
                        original_price=float(pkg_price),
                        discount_amount=float(pkg_discount),
                        unit_price=float(unit_price),
                        pkg_type=pkg_type,
                    )
                    if cust.get("lifecycle_stage") != "在读":
                        update_customer(cust["id"], lifecycle_stage="在读")
                    st.session_state.pop("pending_stage_customer", None)
                    st.success(f"已为「{cust['name']}」保存课时包（{pkg_hours} 节），并转入在读！")
                    st.rerun()
                else:
                    st.error("请填写课时包名称，且到手课时需大于 0")
    with b2:
        if st.button("⏭ 暂不填写", use_container_width=True):
            st.session_state.pop("pending_stage_customer", None)
            st.rerun()


def customer_form_section():
    """客户新增 / 编辑表单"""
    # 权限守卫：新增需 add 权限，编辑需 edit 权限
    if st.session_state.edit_customer_id is None:
        if not can("action_customers_add"):
            return
    else:
        if not can("action_customers_edit"):
            st.session_state.edit_customer_id = None
            st.rerun()
            return
    st.subheader("✏️ 添加客户" if st.session_state.edit_customer_id is None else "✏️ 编辑客户")

    # 如果是编辑模式，加载已有数据
    default_values = {
        "name": "", "phone": "", "wechat": "", "source": "自然流量",
        "lifecycle_stage": "新线索", "notes": "",
        "intent_fruit": "🍏 青苹果", "school": "", "grade": "", "teacher": "",
    }
    if st.session_state.edit_customer_id:
        cust = get_customer_by_id(st.session_state.edit_customer_id)
        if cust:
            default_values = cust
            # 兼容旧数据无 intent_fruit 字段
            if "intent_fruit" not in default_values or not default_values.get("intent_fruit"):
                default_values["intent_fruit"] = "🍏 青苹果"

    # 学管归属：管理员可选任意用户；学管固定自己
    all_users = get_all_users() if IS_ADMIN else []
    if not IS_ADMIN:
        default_values["teacher"] = TEACHER

    col1, col2, col3 = st.columns(3)
    with col1:
        name = st.text_input("客户姓名 *", value=default_values["name"])
        phone = st.text_input("手机号", value=default_values["phone"])
        wechat = st.text_input("微信号", value=default_values["wechat"])
    with col2:
        source = st.selectbox("来源渠道", SOURCE_CHANNELS,
                              index=SOURCE_CHANNELS.index(default_values["source"])
                              if default_values["source"] in SOURCE_CHANNELS else 0)
        school = st.text_input("学校", value=default_values.get("school", ""))
    with col3:
        lifecycle_stage = st.selectbox("生命周期阶段", LIFECYCLE_STAGES,
                                       index=LIFECYCLE_STAGES.index(default_values["lifecycle_stage"])
                                       if default_values["lifecycle_stage"] in LIFECYCLE_STAGES else 0)
        # ---- 客户意向：苹果图标单选 ----
        fruit_idx = INTENT_FRUIT_OPTIONS.index(default_values["intent_fruit"]) \
            if default_values["intent_fruit"] in INTENT_FRUIT_OPTIONS else 1
        intent_fruit = st.radio(
            "🍎 客户意向",
            INTENT_FRUIT_OPTIONS,
            index=fruit_idx,
            horizontal=True,
        )
        # 年级：统一从下拉列表选择（历史遗留的非常规值会临时加入选项以便编辑保存）
        cur_grade = (default_values.get("grade") or "").strip()
        grade_options = list(GRADE_OPTIONS)
        if cur_grade and cur_grade not in grade_options:
            grade_options.insert(0, cur_grade)
        grade = st.selectbox(
            "年级 *", grade_options,
            index=grade_options.index(cur_grade) if cur_grade in grade_options else None,
            placeholder="请选择年级（必填）",
        )
        notes = st.text_area("备注", value=default_values["notes"], height=80)

    # 学管字段（管理员可分配，学管固定为自己）
    if IS_ADMIN:
        teacher_options = ["（未分配）"] + [u["username"] for u in all_users]
        teacher_labels = ["（未分配）"] + [f"{u['display_name']}（@{u['username']}）" for u in all_users]
        cur_teacher = default_values.get("teacher", "")
        teacher_idx = teacher_options.index(cur_teacher) if cur_teacher in teacher_options else 0
        sel_label = st.selectbox("👨‍🏫 学管归属", teacher_labels, index=teacher_idx)
        sel_username = teacher_options[teacher_labels.index(sel_label)]
        teacher_value = "" if sel_username == "（未分配）" else sel_username
    else:
        teacher_value = TEACHER
        st.caption(f"👨‍🏫 学管归属：{current_user().get('display_name', TEACHER)}（固定）")

    cb1, cb2, _ = st.columns([1, 1, 4])
    with cb1:
        if st.button("💾 保存", use_container_width=True):
            if not name.strip():
                st.error("请输入客户姓名！")
            elif not grade or not grade.strip():
                st.error("请选择年级（必填项）！")
            else:
                if st.session_state.edit_customer_id:
                    # 更新客户
                    update_customer(
                        st.session_state.edit_customer_id,
                        name=name.strip(), phone=phone.strip(), wechat=wechat.strip(),
                        source=source,
                        lifecycle_stage=lifecycle_stage, notes=notes.strip(),
                        intent_fruit=intent_fruit,
                        school=school.strip(), grade=grade.strip(),
                        teacher=teacher_value,
                    )
                    st.success(f"客户「{name}」更新成功！")
                    # 阶段从其他状态转为「在读」时，弹出对话框填写课时包
                    if lifecycle_stage == "在读" and default_values.get("lifecycle_stage") != "在读":
                        for _k in ("pkg_name", "pkg_hours", "pkg_price", "pkg_discount", "pkg_tpl_sel", "pkg_type"):
                            st.session_state.pop(_k, None)
                        st.session_state.pending_stage_customer = st.session_state.edit_customer_id
                else:
                    # 新增客户
                    new_id = add_customer(
                        name=name.strip(), phone=phone.strip(), wechat=wechat.strip(),
                        source=source,
                        lifecycle_stage=lifecycle_stage, notes=notes.strip(),
                        intent_fruit=intent_fruit,
                        school=school.strip(), grade=grade.strip(),
                        teacher=teacher_value,
                    )
                    st.success(f"客户「{name}」添加成功！")
                    # 新增即选「在读」时，弹出对话框填写课时包
                    if lifecycle_stage == "在读":
                        for _k in ("pkg_name", "pkg_hours", "pkg_price", "pkg_discount", "pkg_tpl_sel", "pkg_type"):
                            st.session_state.pop(_k, None)
                        st.session_state.pending_stage_customer = new_id
                reset_form()
                st.rerun()
    with cb2:
        if st.button("❌ 取消", use_container_width=True):
            reset_form()
            st.rerun()


def customer_list_section():
    """客户列表与筛选"""
    st.subheader("📋 客户列表")

    # 筛选栏
    fcol1, fcol2, fcol3 = st.columns([2, 1.5, 1.5])
    with fcol1:
        search = st.text_input("🔍 搜索姓名/手机号", placeholder="输入关键词搜索...")
    with fcol2:
        stage_filter = st.selectbox("按阶段筛选", ["全部"] + LIFECYCLE_STAGES)
    with fcol3:
        fruit_filter = st.selectbox("🍎 苹果意向", ["全部"] + INTENT_FRUIT_OPTIONS)

    # 获取数据（学管仅能查看自己名下客户）
    customers = get_all_customers(
        search=search,
        stage_filter="" if stage_filter == "全部" else stage_filter,
        fruit_filter="" if fruit_filter == "全部" else fruit_filter,
        teacher=TEACHER,
    )

    if not customers:
        st.info("暂无客户数据，请在上方添加第一位客户。")
        return

    # 用户名 -> 显示名映射
    users_map = {}
    if IS_ADMIN:
        users_map = {u["username"]: u["display_name"] for u in get_all_users()}

    def teacher_label(t):
        if not t:
            return "未分配"
        return users_map.get(t, t)

    # ---- 传统表格展示（ID 为按当前列表临时编号，非数据库 ID） ----
    df = pd.DataFrame([{
        "ID": i,
        "意向": (c.get("intent_fruit") or "🍏 青苹果").split(" ")[0],
        "姓名": c["name"],
        "手机号": c["phone"] or "-",
        "微信": c["wechat"] or "-",
        "来源渠道": c["source"],
        "生命周期": c["lifecycle_stage"],
        "学校": c.get("school", "") or "-",
        "年级": c.get("grade", "") or "-",
        "学管": teacher_label(c.get("teacher", "")),
        "创建时间": (c.get("created_at") or "")[:16],
        "更新时间": (c.get("updated_at") or "")[:16],
    } for i, c in enumerate(customers, 1)])
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"共 {len(customers)} 条记录")

    # ---- 客户操作区（编号与表格 ID 列对应） ----
    st.markdown("#### ⚙️ 客户操作")
    cust_ops = {
        f"#{i} {c['name']}（{c['phone'] or '无电话'} · {c['lifecycle_stage']}）": c["id"]
        for i, c in enumerate(customers, 1)
    }
    sel_label = st.selectbox("选择客户", list(cust_ops.keys()))
    sel_id = cust_ops[sel_label]
    sel = next(c for c in customers if c["id"] == sel_id)

    # 按操作权限动态展示操作按钮
    current_idx = LIFECYCLE_STAGES.index(sel["lifecycle_stage"]) if sel["lifecycle_stage"] in LIFECYCLE_STAGES else 0
    ops = []
    if can("action_customers_edit"):
        ops.append(("edit", "✏️ 编辑"))
    if can("action_customers_follow"):
        ops.append(("follow", "📝 跟进"))
    if can("action_customers_delete"):
        ops.append(("delete", "🗑 删除"))
    if can("action_customers_stage"):
        ops.append(("stage_prev", "⬅ 上一阶段"))
        ops.append(("stage_next", "➡ 下一阶段"))

    if ops:
        op_cols = st.columns(len(ops))
        for col, (op, label) in zip(op_cols, ops):
            with col:
                if op == "edit":
                    if st.button(label, use_container_width=True):
                        st.session_state.edit_customer_id = sel_id
                        st.rerun()
                elif op == "follow":
                    if st.button(label, use_container_width=True):
                        follow_up_dialog(sel)
                elif op == "delete":
                    if st.button(label, use_container_width=True):
                        delete_customer(sel_id)
                        st.warning(f"已删除客户「{sel['name']}」")
                        st.rerun()
                elif op == "stage_prev":
                    if st.button(label, use_container_width=True, disabled=current_idx <= 0):
                        update_customer(sel_id, lifecycle_stage=LIFECYCLE_STAGES[current_idx - 1])
                        st.rerun()
                elif op == "stage_next":
                    if st.button(label, use_container_width=True,
                                 disabled=current_idx >= len(LIFECYCLE_STAGES) - 1):
                        next_stage = LIFECYCLE_STAGES[current_idx + 1]
                        update_customer(sel_id, lifecycle_stage=next_stage)
                        # 阶段流转为「在读」时，弹出对话框填写课时包
                        if next_stage == "在读":
                            for _k in ("pkg_name", "pkg_hours", "pkg_price", "pkg_discount", "pkg_tpl_sel", "pkg_type"):
                                st.session_state.pop(_k, None)
                            st.session_state.pending_stage_customer = sel_id
                        st.rerun()


def follow_up_section():
    """跟进记录管理：展示全部跟进记录（原「跟进提醒」页面的全部跟进记录迁移至此）"""
    st.subheader("📝 全部跟进记录")

    follow_ups = get_follow_ups(teacher=TEACHER)
    if not follow_ups:
        st.info("暂无跟进记录。可在「客户列表」中点击客户的「跟进」按钮记录已完成跟进，"
                "或在「跟进提醒」页面添加跟进任务。")
        return

    # 用户名 -> 显示名映射（学管列展示用）
    users_map = {}
    if IS_ADMIN:
        users_map = {u["username"]: u["display_name"] for u in get_all_users()}

    def teacher_label(t):
        if not t:
            return "未分配"
        return users_map.get(t, t)

    # ---- 全部跟进记录表格（ID 为临时编号，学管为跟进客户的负责学管） ----
    df_fu = pd.DataFrame([{
        "ID": i,
        "提醒": stale_icon(parse_days_ago(fu.get("updated_at", fu.get("created_at", "")))),
        "客户": fu.get("customer_name", ""),
        "学管": teacher_label(fu.get("teacher", "")),
        "生命周期": fu.get("lifecycle_stage", ""),
        "跟进方式": fu.get("follow_type", ""),
        "跟进时间": fu.get("plan_time", ""),
        "状态": fu["status"],
        "跟进内容": fu.get("content", ""),
        "更新时间": fmt_time(fu.get("updated_at", fu.get("created_at", ""))),
        "距今天数": parse_days_ago(fu.get("updated_at", fu.get("created_at", ""))),
    } for i, fu in enumerate(follow_ups, 1)])
    st.dataframe(df_fu.style.map(stale_cell_style, subset=["距今天数"]),
                 use_container_width=True, hide_index=True)
    st.caption(f"共 {len(follow_ups)} 条跟进记录")

    # ---- 待跟进记录操作（需操作权限，编号与表格 ID 列对应） ----
    pending = [fu for fu in follow_ups if fu["status"] == "待跟进"]
    if pending and can("action_followups_process"):
        st.markdown("#### ✅ 处理待跟进记录")
        fu_ord = {fu["id"]: i for i, fu in enumerate(follow_ups, 1)}
        p_ops = {
            f"#{fu_ord[fu['id']]} {fu.get('customer_name', '')} {fu.get('plan_time', '')} [{fu.get('follow_type', '')}]": fu["id"]
            for fu in pending
        }
        p_sel = st.selectbox("选择跟进记录", list(p_ops.keys()), key="fu_global_pending")
        fc1, fc2, _ = st.columns([1, 1, 4])
        with fc1:
            if st.button("✅ 完成", use_container_width=True, key="fu_global_done"):
                update_follow_up(p_ops[p_sel], status="已完成")
                st.rerun()
        with fc2:
            if st.button("❌ 取消", use_container_width=True, key="fu_global_cancel"):
                update_follow_up(p_ops[p_sel], status="已取消")
                st.rerun()


# ---------- 主界面（选项卡按权限过滤） ----------
st.title("👥 客户管理")

# ---------- 在读学员课时包弹窗 ----------
pending_pkg_cust = st.session_state.get("pending_stage_customer")
if pending_pkg_cust:
    pending_cust = get_customer_by_id(pending_pkg_cust)
    if pending_cust:
        course_package_dialog(pending_cust)
    else:
        st.session_state.pop("pending_stage_customer", None)

tab_defs = [
    ("tab_customers_list", "📋 客户列表"),
    ("tab_customers_followups", "📝 跟进记录"),
]
tab_defs = [(k, l) for k, l in tab_defs if can(k)]

if not tab_defs:
    st.info("当前角色无客户管理选项卡权限。")
else:
    tabs = st.tabs([label for _, label in tab_defs])
    for tab, (key, _label) in zip(tabs, tab_defs):
        with tab:
            if key == "tab_customers_list":
                customer_form_section()
                st.markdown("---")
                customer_list_section()
            elif key == "tab_customers_followups":
                follow_up_section()
