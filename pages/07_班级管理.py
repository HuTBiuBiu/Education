"""
班级管理页面
面向 1v多 小班课：班级即课程，可往班级内添加/移除多名学员。
1v1 学员无需创建班级，直接在「课表管理」中选择学员排课即可。
"""
import streamlit as st
import pandas as pd
from datetime import date

from auth import can, get_viewer_scope, is_admin, require_login, require_page
from database import (
    add_class, update_class, delete_class, get_all_classes, get_class_by_id,
    add_class_student, remove_class_student, get_class_students,
    get_all_users, get_all_customers,
)

# ---------- 登录校验与权限守卫 ----------
require_login()
require_page("page_classes")

IS_ADMIN = is_admin()
VIEWER_ROLE, VIEWER_NAME = get_viewer_scope()

st.title("🏫 班级管理")
st.caption("面向 **1v多 小班课**：班级即课程，一个班聚合多名学员统一排课与扣课时。1v1 学员无需创建班级，直接在「课表管理」中选学员+老师排课即可。")

users = get_all_users()
USER_DISPLAY = {u["username"]: u["display_name"] for u in users}
USER_LABEL = {u["username"]: f"{u['display_name']}（@{u['username']}）" for u in users}
TEACHER_USERS = [u for u in users if u.get("role") in ("teacher", "admin")]
MANAGER_USERS = [u for u in users if u.get("role") in ("staff", "admin")]
# 任课教师必填：1v多 排课时教师随班级自动带出，因此班级必须绑定教师
TEACHER_CHOICES = [u["username"] for u in TEACHER_USERS]
MANAGER_CHOICES = ["(未分配)"] + [u["username"] for u in MANAGER_USERS]


def display_name(username: str) -> str:
    return USER_DISPLAY.get(username, username or "未分配")


# ---------- 新建班级（需操作权限） ----------
if can("action_classes_create"):
    st.subheader("➕ 新建班级（1v多 小班课）")
    with st.form("add_class_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            name = st.text_input("班级名称", placeholder="例如：钢琴小班（周六10:00）")
        with c2:
            if TEACHER_CHOICES:
                teacher = st.selectbox("任课教师", TEACHER_CHOICES, index=0,
                                       format_func=lambda x: USER_LABEL.get(x, x),
                                       help="必填：排 1v多 课程时将自动使用该教师")
            else:
                teacher = ""
                st.selectbox("任课教师", ["暂无教师账号，请先在「员工管理」创建"], disabled=True)
        with c3:
            manager = st.selectbox("班主任 / 学管", MANAGER_CHOICES,
                                   index=0, format_func=lambda x: USER_LABEL.get(x, x))
            manager = "" if manager == "(未分配)" else manager
        c4, c5, c6 = st.columns(3)
        with c4:
            max_students = st.number_input("班级人数上限", min_value=0, max_value=100, value=0,
                                           help="0 表示不限制")
        with c5:
            start_date = st.date_input("开班日期", value=date.today())
        with c6:
            notes = st.text_input("备注（选填）")
        submitted = st.form_submit_button("创建班级", type="primary",
                                          disabled=not TEACHER_CHOICES)

    if submitted:
        if not name.strip():
            st.error("班级名称不能为空")
        elif not teacher:
            st.error("请选择任课教师（必填项）")
        else:
            add_class(
                name=name.strip(), class_type="1v多", course_id=0,
                teacher=teacher, manager=manager, max_students=int(max_students),
                status="进行中", start_date=str(start_date), notes=notes.strip(),
            )
            st.success(f"班级「{name.strip()}」创建成功")
            st.rerun()

# ---------- 班级列表 ----------
st.markdown("---")
st.subheader("🗂️ 班级列表")

classes = get_all_classes(viewer_role=VIEWER_ROLE, viewer_name=VIEWER_NAME)

if not classes:
    st.info("暂无班级，请先在上方创建班级。")
else:
    df = pd.DataFrame([{
        "ID": i,
        "班级名称": c["name"],
        "班型": c["class_type"],
        "任课教师": display_name(c.get("teacher", "")),
        "班主任": display_name(c.get("manager", "")),
        "学员数": f"{c.get('student_count', 0)}/{c.get('max_students') or '∞'}",
        "状态": c.get("status", ""),
        "开班日期": c.get("start_date", "") or "-",
    } for i, c in enumerate(classes, 1)])
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("#### 👨‍👩‍👧‍👦 班级详情与学员管理")
    class_ops = {f"#{i} {c['name']}（{c['class_type']}）": c["id"]
                 for i, c in enumerate(classes, 1)}
    sel_label = st.selectbox("选择一个班级查看详情", list(class_ops.keys()))
    sel_id = class_ops[sel_label]
    cls = get_class_by_id(sel_id)

    if cls:
        info_cols = st.columns(4)
        info_cols[0].metric("班型", cls.get("class_type", ""))
        info_cols[1].metric("状态", cls.get("status", ""))
        info_cols[2].metric("任课教师", display_name(cls.get("teacher", "")))
        info_cols[3].metric("班主任/学管", display_name(cls.get("manager", "")))

        students = get_class_students(sel_id)
        limit = cls.get("max_students") or 0
        can_add = True
        if limit and len(students) >= limit:
            can_add = False

        # 添加学员（需操作权限）
        if can("action_classes_add_student"):
            with st.expander("➕ 添加学员", expanded=len(students) == 0):
                if not can_add:
                    st.warning(f"该班级人数已达上限（{limit} 人），无法继续添加学员。")
                else:
                    # 按当前用户可见范围过滤客户
                    from auth import get_visible_teacher
                    customers = get_all_customers(teacher=get_visible_teacher())
                    existing_ids = {s["customer_id"] for s in students}
                    avail = [c for c in customers if c["id"] not in existing_ids]
                    if not avail:
                        st.info("没有可添加的学员（可见客户均已在班级中）。")
                    else:
                        cust_ops = {f"{c['name']}（{c.get('phone', '') or '无电话'} · {c.get('school', '') or '—'}）": c["id"]
                                    for c in avail}
                        picks = st.multiselect("选择要加入班级的学员", list(cust_ops.keys()))
                        if st.button("确认添加所选学员", type="primary"):
                            for label in picks:
                                add_class_student(sel_id, cust_ops[label])
                            st.success(f"已添加 {len(picks)} 名学员")
                            st.rerun()

        # 学员列表
        st.markdown(f"**当前学员（{len(students)} 人）**")
        if not students:
            st.caption("该班级暂无学员。")
        else:
            sdf = pd.DataFrame([{
                "ID": i,
                "姓名": s.get("name", ""),
                "电话": s.get("phone", "") or "-",
                "学校": s.get("school", "") or "-",
                "年级": s.get("grade", "") or "-",
                "加入时间": s.get("joined_at", ""),
            } for i, s in enumerate(students, 1)])
            st.dataframe(sdf, use_container_width=True, hide_index=True)

            # 移除学员（需操作权限）
            if can("action_classes_remove_student"):
                st.markdown("**移除学员**")
                rm_ops = {f"{s['name']}（{s.get('phone', '') or '无电话'}）": s["customer_id"] for s in students}
                rm_choice = st.selectbox("选择要移出班级的学员", list(rm_ops.keys()))
                if st.button("移出该学员", type="secondary"):
                    remove_class_student(sel_id, rm_ops[rm_choice])
                    st.success("已移出班级")
                    st.rerun()

        # 编辑任课教师 / 班主任（1v多 排课时教师随班级自动带出，须保证班级有教师）
        if can("action_classes_update_status"):
            st.markdown("---")
            st.markdown("**编辑任课教师 / 班主任**")
            e1, e2, e3 = st.columns([2, 2, 1])
            with e1:
                if TEACHER_CHOICES:
                    cur_t = cls.get("teacher", "") or ""
                    new_teacher = st.selectbox(
                        "任课教师", TEACHER_CHOICES,
                        index=TEACHER_CHOICES.index(cur_t) if cur_t in TEACHER_CHOICES else 0,
                        key=f"edit_tea_{sel_id}",
                        format_func=lambda x: USER_LABEL.get(x, x),
                        help="1v多 排课将自动使用该教师",
                    )
                else:
                    new_teacher = ""
                    st.selectbox("任课教师", ["暂无教师账号"], disabled=True)
            with e2:
                cur_m = cls.get("manager", "") or ""
                new_manager = st.selectbox(
                    "班主任 / 学管", MANAGER_CHOICES,
                    index=MANAGER_CHOICES.index(cur_m) if cur_m in MANAGER_CHOICES else 0,
                    key=f"edit_mgr_{sel_id}",
                    format_func=lambda x: USER_LABEL.get(x, x),
                )
            with e3:
                st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                if st.button("保存", key=f"save_staff_{sel_id}",
                             disabled=not TEACHER_CHOICES):
                    update_class(sel_id, teacher=new_teacher, manager=new_manager)
                    st.success("任课教师 / 班主任已更新")
                    st.rerun()

        # 班级状态 / 删除（无对应权限时整块隐藏）
        if can("action_classes_update_status") or can("action_classes_delete"):
            st.markdown("---")
            cA, cB = st.columns([1, 1])
            with cA:
                if can("action_classes_update_status"):
                    new_status = st.selectbox("班级状态", ["进行中", "已结课", "已解散"],
                                              index=["进行中", "已结课", "已解散"].index(cls.get("status", "进行中")))
                    if st.button("更新状态", type="secondary"):
                        update_class(sel_id, status=new_status)
                        st.success("状态已更新")
                        st.rerun()
            with cB:
                if can("action_classes_delete"):
                    if st.button("删除该班级", type="secondary"):
                        delete_class(sel_id)
                        st.success("班级已删除")
                        st.rerun()
