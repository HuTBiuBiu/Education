"""
课表管理页面
支持 日 / 周 / 月 三视图：
- 日视图：纵向时间网格，顶部日期表头可点击弹出当天操作对话框；
- 周视图：横向时间线（时间横排、日期竖排），点击左侧日期列弹出当天操作对话框；
- 月视图：月历色块（已上=浅蓝，未上=橙色），每格下方「＋排课/查看」按钮。
排课分两类：👤 1v1 学员排课（直接选学员+老师，无需课程/班级）与 👥 1v多 班级排课（选班级）。
课表上以「学员名 / 班级名」区分 1v1 与 1v多；排课时展示学员「剩余/已排/可排」课时并限制排课量；
「课堂反馈」选项卡：教师/管理员可对上完的课程填写文字评价（教师仅限本人授课课表）。
"""
import calendar
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from html import escape as _html_escape

import streamlit as st

from auth import can, get_viewer_scope, require_login, require_page, get_visible_teacher
from database import (
    get_all_classes, get_all_users, get_all_customers,
    get_schedules, add_schedule, delete_schedule,
    get_all_schedule_feedback, save_schedule_feedback,
    auto_consume_hours_by_feedback, get_lesson_minutes,
    get_class_students, get_customers_hour_balance,
    get_active_packages_for_customers,
)

# ---------- 登录校验与权限守卫 ----------
require_login()
require_page("page_schedules")

VIEWER_ROLE, VIEWER_NAME = get_viewer_scope()

st.title("📅 课表管理")
st.caption("点击月视图格子下方「＋排课/查看」，或日/周视图中的日期区域（日视图表头、周视图日期列），可在弹出的对话框中新建或删除当天课表。")

# ---------- 基础数据 ----------
users = get_all_users()
USER_DISPLAY = {u["username"]: u["display_name"] for u in users}
USER_LABEL = {u["username"]: f"{u['display_name']}（@{u['username']}）" for u in users}
TEACHER_USERS = [u for u in users if u.get("role") in ("teacher", "admin")]
TEACHER_NAMES = [u["username"] for u in TEACHER_USERS]


def _parse_subjects(u) -> list:
    """把用户 subjects 字段（逗号分隔）解析为科目列表"""
    raw = (u.get("subjects") or "").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


# 教师可带科目：排 1v1 课时从所选老师可带的科目中选择
TEACHER_SUBJECTS = {u["username"]: _parse_subjects(u) for u in TEACHER_USERS}
ALL_CLASSES = get_all_classes(viewer_role=VIEWER_ROLE, viewer_name=VIEWER_NAME)
# 排课可选学员：仅包含「在读」「待续费」生命周期阶段的学员（新线索/流失等阶段不可排课）
SCHEDULABLE_STAGES = ("在读", "待续费")
ALL_CUSTOMERS = [c for c in get_all_customers(teacher=get_visible_teacher())
                 if c.get("lifecycle_stage") in SCHEDULABLE_STAGES]
CUSTOMER_CHOICES = {f"{c['name']}（{c.get('phone', '') or '无电话'}）": c["id"] for c in ALL_CUSTOMERS}


def display_name(username: str) -> str:
    return USER_DISPLAY.get(username, username or "未分配")


def _sched_title(s) -> str:
    """课表显示名：1v1 课表显示学员名，1v多 课表显示班级名；新数据直接取 title，历史数据回退课程名"""
    return (s.get("title") or s.get("customer_name") or s.get("class_name")
            or s.get("course_name") or "未命名课程")


def _build_sched_title(kind: str, customer=None, cls=None, subject: str = "") -> str:
    """课表标题自动生成（不允许自定义）：
    👤 1v1 → 学员姓名 · 归属学管姓名 · 科目（科目从该老师可带科目中选择）
    👥 1v多 → 班级名称
    关联对象缺失时回退为「未命名课程」。存储到 schedules.title，渲染由 _sched_title 兜底。
    """
    if kind.startswith("👤"):
        name = (customer or {}).get("name") or "未命名学员"
        # 归属学管：customers.teacher 字段（学管用户名），标题中展示学管而非任课老师
        mgr = (customer or {}).get("teacher") or ""
        mgr_name = display_name(mgr) if mgr else "未分配学管"
        if subject:
            return f"{name} · {mgr_name} · {subject}"
        return f"{name} · {mgr_name}"
    if kind.startswith("👥") and cls:
        return cls.get("name") or "未命名班级"
    return "未命名课程"


# ---------- 会话状态 ----------
if "cal_date" not in st.session_state:
    st.session_state.cal_date = date.today()
cur = st.session_state.cal_date

def _render_day_click_overlay(days, mode, day_row_h=112):
    """在已渲染的 HTML 日历网格上叠加透明按钮：点击日期即可打开当天课表操作对话框。
    纯 Streamlit 交互（无 URL 跳转），登录态与会话保持不变。
    mode="week"：覆盖左侧日期列（每天一行）；mode="day"：覆盖顶部日期表头。"""
    if mode == "week":
        # 顶部时间轴表头约 27px（26px 刻度 + 1px 边框），之后每行 day_row_h+1px
        header_h = 27
        row_h = day_row_h + 1
        css = [".st-key-week_grid{position:relative!important;}"]
        cols = st.columns(len(days), gap="small")
        for i, d in enumerate(days):
            key = f"ovw_{d.strftime('%Y%m%d')}"
            top = header_h + i * row_h
            css.append(
                f'.st-key-{key}{{position:absolute!important;top:{top}px!important;'
                f'left:1px!important;width:92px!important;height:{day_row_h}px!important;'
                f'z-index:8!important;}}'
                f'.st-key-{key} button{{width:100%!important;height:100%!important;'
                f'opacity:0!important;border:none!important;background:transparent!important;'
                f'padding:0!important;margin:0!important;cursor:pointer!important;'
                f'border-radius:0!important;}}'
            )
            with cols[i]:
                if st.button("", key=key, use_container_width=True,
                             help=f"点击查看/新建 {d.strftime('%m月%d日')} 的课程"):
                    st.session_state.dlg_day = d
                    st.rerun()
        st.markdown("<style>" + "".join(css) + "</style>", unsafe_allow_html=True)
    else:
        d = days[0]
        key = f"ovd_{d.strftime('%Y%m%d')}"
        css = [
            ".st-key-day_grid{position:relative!important;}",
            f'.st-key-{key}{{position:absolute!important;top:2px!important;'
            f'left:82px!important;right:4px!important;height:38px!important;z-index:8!important;}}'
            f'.st-key-{key} button{{width:100%!important;height:100%!important;'
            f'opacity:0!important;border:none!important;background:transparent!important;'
            f'padding:0!important;margin:0!important;cursor:pointer!important;'
            f'border-radius:0!important;}}',
        ]
        if st.button("", key=key, use_container_width=True,
                     help=f"点击查看/新建 {d.strftime('%m月%d日')} 的课程"):
            st.session_state.dlg_day = d
            st.rerun()
        st.markdown("<style>" + "".join(css) + "</style>", unsafe_allow_html=True)


def parse_dt(t: str):
    try:
        return datetime.strptime(t[:16], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def sched_day(s) -> date:
    dt = parse_dt(s.get("start_time", ""))
    return dt.date() if dt else None


def by_day_map(scheds) -> dict:
    m = defaultdict(list)
    for s in scheds:
        d = sched_day(s)
        if d:
            m[d].append(s)
    return m


# ==================== 月视图：月历格子 + 课程色块（与日/周视觉一致） ====================
def _mg_dt(s, k):
    v = s.get(k, "")
    try:
        return datetime.strptime(str(v)[:16], "%Y-%m-%d %H:%M")
    except Exception:
        return None


def _mg_status(s) -> str:
    sdt = _mg_dt(s, "start_time")
    edt = _mg_dt(s, "end_time")
    now = datetime.now()
    if edt and edt < now:
        return "done"
    if sdt and edt and sdt <= now <= edt:
        return "doing"
    return "pending"


def _mg_color(status: str) -> tuple:
    # 色块颜色仅按是否已上区分：已上=浅蓝，未上（含进行中）=橙色，与图例一致
    if status == "done":
        return ("#93c5fd", "#1e3a8a")
    return ("#fdba74", "#7c2d12")


def render_month_grid(year: int, month: int, scheds):
    """月历网格 + 每格内课程色块（宽度撑满格子、高度自适应、标题横向省略）"""
    cal = calendar.Calendar(firstweekday=0)
    weeks = cal.monthdatescalendar(year, month)
    days = [d for week in weeks for d in week]
    by_day = by_day_map(scheds)
    today_d = date.today()
    ref = date(year, month, 1)
    headers = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    st.markdown(
        f"<div style='display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin-bottom:4px;'>"
        + "".join(
            f"<div style='text-align:center;font-size:13px;color:#333;font-weight:600;"
            f"background:#f0f2f6;border-radius:8px;padding:6px 0;'>{h}</div>" for h in headers
        ) + "</div>",
        unsafe_allow_html=True,
    )
    for week in weeks:
        cols = st.columns(7)
        for i, d in enumerate(week):
            with cols[i]:
                items = sorted(by_day.get(d, []), key=lambda x: x.get("start_time", ""))
                in_month = (d.year == ref.year and d.month == ref.month)
                is_today = (d == today_d)
                # 课程色块（宽度撑满格子，高度自适应，标题横向省略）
                blocks_html = []
                for s in items[:4]:
                    status = _mg_status(s)
                    bg, fg = _mg_color(status)
                    title = _sched_title(s)
                    title_s = _html_escape(title)
                    t = _mg_dt(s, "start_time")
                    ts = t.strftime("%H:%M") if t else ""
                    tip = _html_escape(
                        f"{ts} {title}｜{display_name(s.get('teacher') or '')}｜{s.get('location') or ''}"
                    )
                    blocks_html.append(
                        f'<div title="{tip}" '
                        f'style="background:{bg};color:{fg};border-radius:3px;padding:1px 5px;'
                        f'margin-bottom:2px;font-size:11px;line-height:1.5;'
                        f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;">'
                        f'{ts} {title_s}</div>'
                    )
                extra = len(items) - 4
                if extra > 0:
                    blocks_html.append(
                        f'<div style="font-size:11px;color:#6b7280;padding:1px 2px;">＋{extra} 节</div>'
                    )
                cell_bg = "#eff6ff" if is_today else ("#ffffff" if in_month else "#f5f5f5")
                day_fg = "#1d4ed8" if is_today else ("#111827" if in_month else "#c0c0c0")
                cell_border = "2px solid #3b82f6" if is_today else "1px solid #e5e7eb"
                st.markdown(
                    f'<div style="min-height:118px;border:{cell_border};border-radius:8px;'
                    f'padding:4px 6px;background:{cell_bg};">'
                    f'<div style="font-weight:700;font-size:12px;margin-bottom:3px;color:{day_fg};">{d.day}</div>'
                    f'{"".join(blocks_html)}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                if st.button("＋排课/查看", key=f"calbtn_{d.strftime('%Y%m%d')}",
                             use_container_width=True):
                    st.session_state.dlg_day = d
                    st.rerun()


# ==================== 批量排课（日历上方独立区域） ====================
WEEKDAY_CN = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}


def _batch_class_selector() -> int:
    """批量排课用的班级选择控件（仅进行中的 1v多 班级），返回 class_id（0 表示不关联）"""
    active_classes = [cl for cl in ALL_CLASSES if cl.get("status") == "进行中"]
    if active_classes:
        class_ops = {f"{cl['name']}（{cl['class_type']}）": cl["id"] for cl in active_classes}
        class_choice = st.selectbox("选择班级（1v多）", list(class_ops.keys()),
                                    key="batch_cls")
        return class_ops[class_choice]
    else:
        st.selectbox("选择班级", ["暂无进行中的班级，请先到「班级管理」建班"], disabled=True,
                     key="batch_cls_none")
        return 0


def calc_regular_dates(d_start: date, d_end: date, weekdays, freq_weeks: int = 1) -> list:
    """规律排课：返回 [d_start, d_end] 区间内匹配所选星期的全部日期。
    freq_weeks=1 表示每周一次；=2 隔周一次，依此类推。"""
    if not weekdays or d_start > d_end:
        return []
    days, step = [], timedelta(weeks=freq_weeks)
    for wd in sorted(weekdays):
        offset = (wd - d_start.weekday()) % 7
        cur = d_start + timedelta(days=offset)
        while cur <= d_end:
            days.append(cur)
            cur += step
    return sorted(set(days))


def _parse_dates_text(raw: str) -> list:
    """解析粘贴的日期文本，支持 YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD / YYYYMMDD，
    逗号、分号、空白（含换行）均可作为分隔符。"""
    out = []
    for part in re.split(r"[,，;；\s]+", raw.strip()):
        part = part.strip()
        if not part:
            continue
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
            try:
                out.append(datetime.strptime(part, fmt).date())
                break
            except ValueError:
                continue
    return out


def _add_many_schedules(days, s_time, e_time, course_id, class_id,
                        title, teacher, location, notes, customer_id: int = 0,
                        package_id: int = 0) -> int:
    """批量写入课表，返回成功条数；customer_id 用于 1v1 排课（直接挂学员）；
    package_id 为 1v1 排课指定的课时包（0=自动组合同类型课时包）"""
    count = 0
    for d in days:
        add_schedule(class_id=class_id, course_id=course_id, title=title, teacher=teacher,
                     start_time=f"{d} {s_time.strftime('%H:%M:%S')}",
                     end_time=f"{d} {e_time.strftime('%H:%M:%S')}",
                     location=location, notes=notes, customer_id=customer_id,
                     package_id=package_id)
        count += 1
    return count


def _pkg_choice_options(packages) -> dict:
    """构建「使用课时包」下拉选项：🔄 自动组合（同类型课时包）→ 0；各课时包 → 包ID"""
    opts = {"🔄 自动组合（同类型课时包）": 0}
    for p in packages:
        label = f"{p.get('package_name') or '未命名课时包'}（{p.get('type') or '1v1'}，剩 {float(p.get('remaining_hours') or 0):.1f} 课时"
        if (p.get("expiry_date") or ""):
            label += f"，至 {p['expiry_date']}"
        label += "）"
        opts[label] = p["id"]
    return opts


def _class_balance_data(class_id: int):
    """返回 (students, balances)：所选班级学员列表及其课时余额映射；
    未选班级或班级无学员时返回空。"""
    if not class_id:
        return [], {}
    students = get_class_students(class_id)
    if not students:
        return [], {}
    balances = get_customers_hour_balance([s["customer_id"] for s in students])
    return students, balances


def _render_balance_panel(class_id: int):
    """排课界面展示所选班级学员课时余额：剩余课时 / 已排课时 / 可排课时。
    返回 (students, balances) 供提交时校验。"""
    students, balances = _class_balance_data(class_id)
    if not class_id:
        return students, balances
    if not students:
        st.caption("该班级暂无学员，不进行课时余额校验。")
        return students, balances
    rows = []
    for stu in students:
        b = balances.get(stu["customer_id"], {})
        rows.append({
            "学员": stu.get("name") or f"学员#{stu['customer_id']}",
            "剩余课时(1v多)": b.get("remaining_multi", b.get("remaining_hours", 0)),
            "已排课时(1v多)": b.get("scheduled_multi", b.get("scheduled_hours", 0)),
            "可排课时(1v多)": b.get("available_multi", b.get("available_hours", 0)),
        })
    st.caption("📊 学员课时余额（按 1v多 课时包口径）：剩余 / 已排 / 可排；可排 = 1v多剩余 − 1v多已排（同类型课时包组合使用）")
    st.dataframe(rows, use_container_width=True, hide_index=True)
    return students, balances


def _calc_hours_needed(days, s_time, e_time) -> float:
    """本次排课折算的总课时数 = 课次数 × 单节课时（按单课时时长换算）"""
    lesson_minutes = get_lesson_minutes()
    minutes = (e_time.hour - s_time.hour) * 60 + (e_time.minute - s_time.minute)
    per_class = round(max(minutes, 0) / lesson_minutes, 2)
    return round(per_class * len(days), 2)


def _balance_problem(students, balances, new_hours: float, course_type: str = "1v1") -> str:
    """校验新增排课是否超过学员可排课时，超限返回错误说明（含学员名与可排课时），通过返回空串。
    course_type: 1v1 / 1v多，按同类型课时包可排课时校验（同类型课时包组合使用）。"""
    problems = []
    key = "available_1v1" if course_type == "1v1" else "available_multi"
    for stu in students:
        b = balances.get(stu["customer_id"], {})
        avail = b.get(key)
        if avail is None:  # 兼容无类型维度数据
            avail = b.get("available_hours", 0)
        if new_hours > avail:
            name = stu.get("name") or f"学员#{stu['customer_id']}"
            problems.append(f"{name}（可排 {avail} 课时）")
    return "；".join(problems)


def _batch_schedule_section():
    """批量排课：👤 1v1 学员排课 / 👥 1v多 班级排课（🔁 规律 / 🗓️ 非规律 日期）"""
    if not can("action_schedules_batch"):
        return
    if not CUSTOMER_CHOICES and not ALL_CLASSES:
        st.warning("暂无「在读/待续费」学员与班级。请先在「客户管理」将学员阶段调整为在读或待续费，"
                   "或在「班级管理」创建班级后再排课。")
        return

    b_mode = st.radio("排课模式", ["🔁 规律排课（按星期重复）", "🗓️ 非规律排课（自定义日期）"],
                      horizontal=True, key="batch_mode")
    st.caption("🔁 规律：按「星期 + 重复频率」自动延展日期，适合长期固定课程；"
               "🗓️ 非规律：手动多选或粘贴散点日期，适合补课、临时加课。")

    with st.container(border=True):
        sched_kind = st.radio("排课类型", ["👤 1v1 学员排课", "👥 1v多 班级排课"],
                              horizontal=True, key="batch_sched_kind")
        customer_id = 0
        class_id = 0
        students, balances = [], {}
        cust = None
        batch_class = None
        teacher = ""
        subject = ""
        batch_pkg_id = 0
        if sched_kind.startswith("👤"):
            # 1v1：直接选学员 + 老师，无需课程/班级
            c1, c2 = st.columns(2)
            with c1:
                if CUSTOMER_CHOICES:
                    cust_choice = st.selectbox("学员", list(CUSTOMER_CHOICES.keys()), key="batch_cust")
                    customer_id = CUSTOMER_CHOICES[cust_choice]
                else:
                    st.selectbox("学员", ["暂无在读/待续费学员，请先在「客户管理」调整学员阶段"],
                                 disabled=True, key="batch_cust_none")
            with c2:
                t_opts = ["(未分配)"] + [u["username"] for u in TEACHER_USERS]
                teacher = st.selectbox("任课教师", t_opts, key="batch_teacher1v1",
                                       format_func=lambda x: USER_LABEL.get(x, x))
                teacher = "" if teacher == "(未分配)" else teacher
            # 科目：从所选老师的可带科目中选择（标题组成部分，必选）
            if teacher:
                subs = TEACHER_SUBJECTS.get(teacher, [])
                if subs:
                    subject = st.selectbox("科目（该老师可带）", subs, key="batch_subject")
                else:
                    st.warning(f"老师「{display_name(teacher)}」未设置可带科目，"
                               f"请先在「员工管理」中配置科目后再排课。")
            if customer_id:
                cust = next((c for c in ALL_CUSTOMERS if c["id"] == customer_id), None)
                if cust:
                    students = [{"customer_id": customer_id, "name": cust["name"]}]
                    balances = get_customers_hour_balance([customer_id])
                    b = balances.get(customer_id, {})
                    st.caption(f"📊 学员课时余额（1v1 课时包）：**剩余 {b.get('remaining_1v1', b.get('remaining_hours', 0))} / "
                               f"已排 {b.get('scheduled_1v1', 0)} / "
                               f"可排 {b.get('available_1v1', b.get('available_hours', 0))}** 课时"
                               f"（可排 = 1v1剩余 − 1v1已排，同类型课时包组合使用）")
                    # 使用课时包（选填）：1v1 课时包；同类型可组合使用
                    _pkgs = get_active_packages_for_customers([customer_id]).get(customer_id, [])
                    _one_pkgs = [p for p in _pkgs if (p.get("type") or "1v1") == "1v1"]
                    if _one_pkgs:
                        _pkg_opts = _pkg_choice_options(_one_pkgs)
                        _pkg_sel = st.selectbox(
                            "使用课时包（选填）", list(_pkg_opts.keys()), key="batch_pkg",
                            help="选择「🔄 自动组合」时，消课时自动从该学员同类型（1v1）课时包中依次扣减；"
                                 "也可指定某一个课时包优先扣减（不够时仍会从其他 1v1 课时包补充）。",
                        )
                        batch_pkg_id = _pkg_opts[_pkg_sel]
                    elif _pkgs:
                        st.warning("该学员只有「1v多」课时包，暂无 1v1 课时包，无法扣减 1v1 课程课时。"
                                   "可先正常排课，课堂反馈消课时时系统将提示无匹配课时包。")
                    else:
                        st.caption("该学员暂无进行中课时包，可先排课（课堂反馈消课时时将提示无课时包）。")
        else:
            # 1v多：选班级（班级即课程），任课教师随班级自动带出，无需再选老师
            c1, c2 = st.columns(2)
            with c1:
                class_id = _batch_class_selector()
            batch_class = next((cl for cl in ALL_CLASSES if cl["id"] == class_id), None)
            teacher = (batch_class or {}).get("teacher", "") or ""
            with c2:
                if batch_class and teacher:
                    st.info(f"👩‍🏫 任课教师：**{display_name(teacher)}**（随班级自动带出，可在「班级管理」中修改）")
                else:
                    st.error("该班级未设置任课教师，请先在「班级管理」中为该班级指定任课教师。")
            students, balances = _render_balance_panel(class_id)
        auto_title = _build_sched_title(sched_kind, cust, batch_class, subject)
        st.caption(f"课表标题（自动）：**{auto_title}**")

        c5, c6, c7 = st.columns(3)
        with c5:
            s_time = st.time_input("开始时间", value=datetime.strptime("09:00", "%H:%M").time(),
                                   key="batch_st")
        with c6:
            e_time = st.time_input("结束时间", value=datetime.strptime("10:00", "%H:%M").time(),
                                   key="batch_et")
        with c7:
            location = st.text_input("上课地点（选填）", placeholder="例如：3号琴房", key="batch_loc")
        notes = st.text_input("备注（选填）", key="batch_notes")

        st.markdown("---")
        if b_mode.startswith("🔁"):
            w1, w2, w3, w4 = st.columns(4)
            with w1:
                weekdays_cn = st.multiselect("重复星期（可多选）", list(WEEKDAY_CN.keys()),
                                             default=["周一"], key="batch_wd")
            with w2:
                d_start = st.date_input("开始日期",
                                        value=max(st.session_state.get("cal_date", date.today()), date.today()),
                                        min_value=date.today(),
                                        key="batch_ds")
            with w3:
                d_end = st.date_input("结束日期",
                                      value=max(st.session_state.get("cal_date", date.today()) + timedelta(days=30),
                                                date.today()),
                                      min_value=date.today(),
                                      key="batch_de")
            with w4:
                freq_cn = st.selectbox("重复频率", ["每周", "隔周", "每 3 周", "每 4 周"],
                                       index=0, key="batch_freq")
            weekdays = [WEEKDAY_CN[w] for w in weekdays_cn]
            freq_weeks = {"每周": 1, "隔周": 2, "每 3 周": 3, "每 4 周": 4}[freq_cn]
            days = calc_regular_dates(d_start, d_end, weekdays, freq_weeks)
        else:
            # 非规律排课：先选日期范围，再逐天勾选具体日期（最直白）
            _d0 = max(st.session_state.get("cal_date", date.today()), date.today())
            dr = st.date_input("① 选择日期范围",
                               value=(_d0, _d0 + timedelta(days=13)),
                               min_value=date.today(),
                               format="YYYY-MM-DD", key="batch_drange")
            st.caption("② 勾选需要排课的具体日期（默认不勾选，可跨天多选）。")
            picked_days = []
            if isinstance(dr, (list, tuple)) and len(dr) == 2:
                d_lo, d_hi = sorted(dr)
                span = (d_hi - d_lo).days + 1
                if span > 62:
                    st.warning("日期范围过大（超过 62 天），请缩小范围后再勾选。")
                else:
                    all_dates = [d_lo + timedelta(days=i) for i in range(span)]
                    wd_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    for i in range(0, len(all_dates), 7):
                        row_dates = all_dates[i:i + 7]
                        cols = st.columns(len(row_dates))
                        for j, dd in enumerate(row_dates):
                            with cols[j]:
                                if st.checkbox(
                                    f"{dd.month}/{dd.day} {wd_cn[dd.weekday()]}",
                                    key=f"batch_ck_{dd.isoformat()}",
                                ):
                                    picked_days.append(dd)
            days = sorted(set(picked_days))
            raw = st.text_area("③ 或粘贴日期文本（选填，每行一个或逗号分隔）",
                               placeholder="2026-08-10\n2026-08-12, 2026-08-15",
                               key="batch_paste", height=68)
            if raw and raw.strip():
                days = sorted(set(days + _parse_dates_text(raw)))

        if days:
            sample = "、".join(d.strftime("%m-%d") for d in days[:5])
            suffix = f" 等 {len(days)} 天" if len(days) > 5 else ""
            st.info(f"📋 将生成 **{len(days)}** 节课：{sample}{suffix}")
        elif b_mode.startswith("🔁"):
            st.caption("请选择重复星期和有效的日期范围。")
        else:
            st.caption("请勾选需要排课的日期，或粘贴日期文本。")

        if st.button("📦 批量添加课表", type="primary", disabled=not days, use_container_width=True):
            if e_time <= s_time:
                st.error("结束时间必须晚于开始时间")
            elif sched_kind.startswith("👤") and not customer_id:
                st.error("请先选择学员（1v1 排课必须关联学员，用于课时校验与扣减）")
            elif sched_kind.startswith("👤") and not teacher:
                st.error("请选择任课教师（1v1 排课必须指定老师）")
            elif sched_kind.startswith("👤") and not subject:
                st.error("请选择科目（所选老师未设置可带科目，请先在「员工管理」中配置）")
            elif sched_kind.startswith("👥") and not class_id:
                st.error("请先选择班级（1v多 排课必须关联班级）")
            elif sched_kind.startswith("👥") and not teacher:
                st.error("该班级未设置任课教师，请先在「班级管理」中为该班级指定任课教师后，再排 1v多 课程。")
            else:
                new_hours = _calc_hours_needed(days, s_time, e_time)
                course_type = "1v1" if sched_kind.startswith("👤") else "1v多"
                problem = _balance_problem(students, balances, new_hours, course_type)
                if problem:
                    st.error(f"⛔ 本次需 {new_hours} 课时，以下学员可排课时不足，无法排课：{problem}")
                else:
                    final_title = _build_sched_title(sched_kind, cust, batch_class, subject)
                    added = _add_many_schedules(days, s_time, e_time, 0, class_id,
                                                final_title, teacher, location.strip(), notes.strip(),
                                                customer_id=customer_id, package_id=batch_pkg_id)
                    st.success(f"✅ 已批量添加 {added} 节课！")
                    st.rerun()


# ==================== 课堂反馈（简单文字评价） ====================
def _feedback_section():
    """课堂反馈：查看/填写已上完课程的文字评价。
    仅 tab_feedback 权限可见（教师/管理员/学管）；
    教师数据范围仅限本人授课课表（get_schedules 按 viewer_role 过滤）；
    学管仅看自己名下学生的课表（get_schedules 按 staff_scope 过滤）；
    列表展示全部已上完课程（含已反馈内容），未反馈的置顶。
    """
    if not can("tab_feedback"):
        return

    st.subheader("📝 课堂反馈")
    st.caption("为已上完的课程填写课堂反馈（文字评价）。保存反馈后将按课表时长自动换算并扣减学员课时。")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 查询已上完的课表，再过滤 end_time <= 现在
    # 教师：仅本人授课；学管：仅自己名下学生（1v1 按归属学管、班级课表按班级学管）
    scope = VIEWER_NAME if VIEWER_ROLE == "staff" else ""
    done = get_schedules(
        date_from="", date_to=now_str,
        viewer_role=VIEWER_ROLE, viewer_name=VIEWER_NAME,
        staff_scope=scope,
    )
    done = [s for s in done if (s.get("end_time") or "")[:16] <= now_str]

    if not done:
        st.info("暂无已上完的课程。")
        return

    feedback_map = get_all_schedule_feedback()
    # 排序：未反馈的置顶，组内按上课时间倒序
    done.sort(key=lambda s: s.get("end_time", ""), reverse=True)
    done.sort(key=lambda s: bool(feedback_map.get(s["id"])))

    pending_count = sum(1 for s in done if not feedback_map.get(s["id"]))
    st.caption(f"共 {len(done)} 节已上完课程（{pending_count} 节待反馈，未反馈已置顶）。")

    # 保存反馈后的提示信息（跨 rerun 保留）
    fb_msg = st.session_state.pop("fb_msg", None)
    if fb_msg:
        if fb_msg[0] == "success":
            st.success(fb_msg[1])
        else:
            st.warning(fb_msg[1])

    for s in done:
        sid = s["id"]
        dt = parse_dt(s.get("end_time", ""))
        has_fb = bool(feedback_map.get(sid))
        status = "✅ 已反馈" if has_fb else "⚠️ 待反馈"
        with st.container(border=True):
            hc1, hc2 = st.columns([5, 1])
            with hc1:
                st.markdown(f"**{s.get('title', '')}**")
                who = s.get("customer_name") or s.get("class_name") or "-"
                info = f"时间：{dt.strftime('%Y-%m-%d %H:%M') if dt else '-'}　"
                info += f"学员/班级：{who}　教师：{display_name(s.get('teacher', ''))}"
                # 时长换算：课表实际时长 ÷ 单课时时长
                sdt = parse_dt(s.get("start_time", ""))
                edt = parse_dt(s.get("end_time", ""))
                if sdt and edt and edt > sdt:
                    minutes = int((edt - sdt).total_seconds() // 60)
                    lesson_min = get_lesson_minutes()
                    hours = round(minutes / lesson_min, 2)
                    info += f"　⏱️ {minutes} 分钟 ≈ **{hours} 课时**"
                st.caption(info)
            with hc2:
                st.markdown(
                    f"<div style='text-align:right;font-size:13px;'>{status}</div>",
                    unsafe_allow_html=True,
                )

            if not can("action_schedules_feedback"):
                # 无填写权限（如学管）：只读展示反馈内容
                content = feedback_map.get(sid, "")
                if content:
                    st.markdown(f"📄 已反馈：{content}")
                else:
                    st.caption("⚠️ 本课尚未填写反馈")
                continue
            fb_key = f"fb_{sid}"
            val = st.text_area(
                "课堂反馈（文字评价）", key=fb_key,
                value=feedback_map.get(sid, ""),
                placeholder="填写本次课堂的文字评价，如：学员状态良好，完成计划内容…",
                height=90,
            )
            if st.button("💾 保存反馈", key=f"fb_save_{sid}"):
                if val.strip():
                    save_schedule_feedback(sid, VIEWER_NAME, val.strip())
                    result = auto_consume_hours_by_feedback(sid)
                    if result.get("ok"):
                        msg = f"反馈已保存，{result['message']}"
                        if result.get("customers"):
                            detail = "；".join(
                                f"{c['name']}（{c['package']}）扣 {c['hours']} 课时"
                                for c in result["customers"]
                            )
                            msg += f"。扣减明细：{detail}"
                        if result.get("skipped"):
                            skips = "；".join(
                                f"{s['name']}：{s['reason']}" for s in result["skipped"]
                            )
                            msg += f"。未扣减：{skips}"
                        st.session_state["fb_msg"] = ("success", msg)
                    else:
                        st.session_state["fb_msg"] = (
                            "warning",
                            f"反馈已保存，但未自动扣减课时：{result.get('message', '')}",
                        )
                    st.rerun()
                else:
                    st.warning("反馈内容不能为空")


# ==================== 选项卡：课表视图 / 课堂反馈（按权限过滤） ====================
_tab_defs = [("view", "📅 课表视图")]
if can("tab_feedback"):
    _tab_defs.append(("fb", "📝 课堂反馈"))
_tabs = st.tabs([label for _, label in _tab_defs])
_tab_map = {k: t for (k, _), t in zip(_tab_defs, _tabs)}

with _tab_map["view"]:
    # ---- 批量排课（折叠，无权限整体隐藏） ----
    if can("action_schedules_batch"):
        with st.expander("📦 批量排课（规律 / 非规律）", expanded=False):
            _batch_schedule_section()

    # ==================== 课表时间网格视图 ====================
    # 顶部筛选区（教师/教室/课程类型/课程/科目/是否已上课）+ 班/师搜索
    # 课程类型 / 科目选项固定写死，不随数据库课程动态变化
    CLASSROOM_OPTS = ["全部"]  # 教室暂不预扫描，由课堂反馈/搜索时再去重
    SUBJECT_OPTS = ["全部", "语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"]

    fc1, fc2, fc3, fc4, fc5, fc6 = st.columns(6)
    with fc1:
        teacher_f = st.multiselect("教师", ["全部"] + TEACHER_NAMES,
                                   format_func=lambda x: USER_DISPLAY.get(x) or x,
                                   key="sched_filter_teacher")
    with fc2:
        classroom_f = st.selectbox("教室", CLASSROOM_OPTS, key="sched_filter_classroom")
    with fc3:
        course_type_f = st.selectbox("课程类型", ["全部", "一对一", "一对多"], key="sched_filter_ctype")
    with fc4:
        subject_f = st.selectbox("科目", SUBJECT_OPTS, key="sched_filter_subject")
    with fc5:
        done_f = st.selectbox("是否已上课", ["全部", "已上课", "未上课"], key="sched_filter_done")
    with fc6:
        name_q = st.text_input("班级/老师", placeholder="请输入名称搜索",
                               label_visibility="visible", key="sched_filter_name")

    # 已选筛选 chips + 清空筛选
    _filter_chips = []
    if teacher_f and "全部" not in teacher_f:
        for nm in teacher_f:
            _filter_chips.append(("教师", USER_DISPLAY.get(nm) or nm))
    if classroom_f and classroom_f != "全部":
        _filter_chips.append(("教室", classroom_f))
    if course_type_f and course_type_f != "全部":
        _filter_chips.append(("课程类型", course_type_f))
    if subject_f and subject_f != "全部":
        _filter_chips.append(("科目", subject_f))

    if _filter_chips:
        chip_cols = st.columns([1] * len(_filter_chips) + [1])
        for ci, (cat, val) in enumerate(_filter_chips):
            with chip_cols[ci]:
                st.markdown(
                    f"<div style='background:#e0f2fe;color:#0369a1;border-radius:14px;padding:2px 10px;"
                    f"font-size:12px;display:inline-block;'>{_html_escape(str(cat))}：{_html_escape(str(val))} &nbsp;<b>×</b></div>",
                    unsafe_allow_html=True)
        with chip_cols[-1]:
            if st.button("清空筛选", key="clear_filters", use_container_width=False):
                for k in ("sched_filter_teacher", "sched_filter_classroom",
                          "sched_filter_ctype", "sched_filter_subject",
                          "sched_filter_done"):
                    st.session_state.pop(k, None)
                st.rerun()

    # ---- 时间范围（周/日切换 + 日期导航） ----
    sub = st.session_state.get("sched_sub_view", "周")
    # 默认显示"周"
    if sub not in ("日", "周", "月"):
        sub = "周"
    monday = st.session_state.cal_date - timedelta(days=st.session_state.cal_date.weekday())
    if sub == "日":
        range_from, range_to = st.session_state.cal_date, st.session_state.cal_date
    elif sub == "月":
        cmm = st.session_state.cal_date
        range_from = date(cmm.year, cmm.month, 1)
        range_to = date(cmm.year, cmm.month, calendar.monthrange(cmm.year, cmm.month)[1])
    else:
        range_from, range_to = monday, monday + timedelta(days=6)

    schedules = get_schedules(
        date_from=str(range_from), date_to=str(range_to),
        title=None,
        viewer_role=VIEWER_ROLE, viewer_name=VIEWER_NAME,
    )
    if course_type_f and course_type_f != "全部":
        target_kind = "1v1" if course_type_f == "一对一" else "1v多"
        schedules = [s for s in schedules if s.get("sched_kind") == target_kind]
    if name_q:
        schedules = [s for s in schedules
                     if name_q in (s.get("title") or "")
                     or name_q in (s.get("class_name") or "")
                     or name_q in (s.get("customer_name") or "")
                     or name_q in display_name(s.get("teacher") or "")]
    if classroom_f and classroom_f != "全部":
        schedules = [s for s in schedules if s.get("location") == classroom_f]

    # ---- 工具栏：日周月/日期导航/图例 ----
    tba = st.columns([0.8, 0.8, 3.5])
    with tba[0]:
        sub_choice = st.radio(" ", ["日", "周", "月"], horizontal=True,
                              label_visibility="collapsed", key="sched_sub_view",
                              index=["日", "周", "月"].index(sub))
    with tba[1]:
        nav_p, nav_now, nav_n = st.columns(3)

        def _step(delta: int):
            c = st.session_state.cal_date
            st.session_state.pop("dlg_day", None)
            if sub_choice == "月":
                year = c.year + (1 if (c.month == 12 and delta > 0) or (c.month == 1 and delta < 0) else 0)
                month = (1 if c.month == 12 else c.month + 1) if delta > 0 else (12 if c.month == 1 else c.month - 1)
                st.session_state.cal_date = date(year, month, 1)
            elif sub_choice == "周":
                st.session_state.cal_date = c + timedelta(days=7 * delta)
            else:
                st.session_state.cal_date = c + timedelta(days=delta)

        with nav_p:
            if st.button("◀", key="nav_prev", use_container_width=True):
                _step(-1); st.rerun()
        with nav_now:
            if st.button("今天", key="nav_today", use_container_width=True):
                st.session_state.pop("dlg_day", None)
                st.session_state.cal_date = date.today(); st.rerun()
        with nav_n:
            if st.button("▶", key="nav_next", use_container_width=True):
                _step(1); st.rerun()
    with tba[2]:
        c_cur = st.session_state.cal_date
        if sub_choice == "月":
            nav_label = f"{c_cur.year}年{c_cur.month}月"
        elif sub_choice == "周":
            mn = c_cur - timedelta(days=c_cur.weekday())
            nav_label = f"{mn.year}年{mn.month}月{mn.day}-{(mn + timedelta(days=6)).day}日"
        else:
            nav_label = c_cur.strftime("%Y-%m-%d")
        legend = (
            "<span style='background:#93c5fd;color:#fff;border-radius:4px;padding:2px 6px;margin-right:8px;'>已上课程</span>"
            "<span style='background:#fdba74;color:#fff;border-radius:4px;padding:2px 6px;'>未上课程</span>"
        )
        st.markdown(
            f"<div style='text-align:right;color:#374151;font-size:13px;'>"
            f"<span style='margin-right:14px;color:#555;'>📅 {nav_label}</span>{legend}</div>",
            unsafe_allow_html=True,
        )

    # ---- 时间网格主视图 ----
    DEFAULT_GRID_START = time(8, 0)
    DEFAULT_GRID_END = time(21, 0)
    PX_PER_HOUR = 64
    ROW_HALF_HOUR = PX_PER_HOUR / 2
    MIN_COL_W = 176  # 多列视图（周）每列最小像素宽度，不足时横向滚动

    def _grid_range(scheds, days):
        """按视图中课程的实际时间自动计算网格起止（整点 + 上下留白），无课时用默认范围"""
        s_min, e_max = None, None
        for s in scheds:
            sdt = _sched_dt_local(s, "start_time")
            edt = _sched_dt_local(s, "end_time")
            if not sdt or not edt or sdt.date() not in days:
                continue
            if s_min is None or sdt < s_min:
                s_min = sdt
            if e_max is None or edt > e_max:
                e_max = edt
        if s_min is None or e_max is None:
            return DEFAULT_GRID_START, DEFAULT_GRID_END
        gs = time(max(s_min.hour - 1, 0), 0)
        ge = time(min(e_max.hour + 1, 23), 0)
        if ge <= gs:
            ge = DEFAULT_GRID_END
        return gs, ge

    def _hour_label(h):
        if h <= 5:
            return f"凌晨{h}:00"
        if h <= 8:
            return f"早上{h}:00"
        if h <= 11:
            return f"上午{h}:00"
        if h == 12:
            return "中午12:00"
        if h <= 17:
            return f"下午{h - 12}:00"
        return f"晚上{h - 12}:00"

    def _sched_dt_local(s, k):
        v = s.get(k, "")
        try:
            return datetime.strptime(str(v)[:16], "%Y-%m-%d %H:%M")
        except Exception:
            return None

    def _sched_status(s) -> str:
        sdt = _sched_dt_local(s, "start_time")
        edt = _sched_dt_local(s, "end_time")
        now = datetime.now()
        if edt and edt < now:
            return "done"
        if sdt and edt and sdt <= now <= edt:
            return "doing"
        return "pending"

    def _sched_color(status: str) -> tuple:
        # 色块颜色仅按是否已上区分：已上=浅蓝，未上（含进行中）=橙色，与图例一致
        if status == "done":
            return ("#93c5fd", "#1e3a8a")  # 浅蓝 已上
        return ("#fdba74", "#7c2d12")  # 橙色 未上（含进行中）

    if sub_choice in ("日", "周"):
        # 时间网格视图
        if sub_choice == "周":
            days = [monday + timedelta(days=i) for i in range(7)]
        else:
            days = [st.session_state.cal_date]

        weekday_cn = ["一", "二", "三", "四", "五", "六", "日"]
        today_d = date.today()
        grid_start, grid_end = _grid_range(schedules, days)
        total_minutes = (grid_end.hour - grid_start.hour) * 60
        total_h = total_minutes / 60 * PX_PER_HOUR

        # 按天分组
        by_day = defaultdict(list)
        for s in schedules:
            sdt = _sched_dt_local(s, "start_time")
            if not sdt: continue
            d = sdt.date()
            if d in days:
                by_day[d].append(s)

        # 时间轴、表头、课程块渲染已拆分到「周视图（时间横排/日期竖排）」与「日视图」两个分支

        def _layout_day(items):
            """区间着色布局：
            1) 按开始时间顺序，把每节课分配到第一个互不冲突的列（col_idx）；
            2) 用扫掠线统计每节课在自身时间段内最大的并发列数 max_concur，
               该节宽度 = 1 / max_concur，即只有真正时间重叠的课程才共享宽度，
               不重叠的课程各自占满整列。
            items: [(s_min, e_min, schedule), ...]（schedule 是 dict，不能作 key，
            内部一律用索引定位）。返回 [(col_idx, max_concur, schedule), ...]。"""
            items = sorted(items, key=lambda x: x[0])
            n = len(items)
            col_of = [0] * n      # 每个索引 -> 列号
            cols_end = []         # 每列当前的结束分钟
            for idx, (s_min, e_min, _) in enumerate(items):
                placed = False
                for i, ce in enumerate(cols_end):
                    if ce <= s_min:
                        cols_end[i] = e_min
                        col_of[idx] = i
                        placed = True
                        break
                if not placed:
                    cols_end.append(e_min)
                    col_of[idx] = len(cols_end) - 1

            # 扫掠线：按索引记录活跃集合，统计每节课时段内的最大并发列数
            events = []  # (time, delta, idx)
            for idx, (s_min, e_min, _) in enumerate(items):
                events.append((s_min, 1, idx))
                events.append((e_min, -1, idx))
            events.sort(key=lambda x: (x[0], -x[1]))  # 同一时刻先结束再开始
            max_concur = [1] * n
            active = []          # 当前活跃的索引集合
            prev_t = None
            for t, delta, idx in events:
                if prev_t is not None and t > prev_t and active:
                    n_active = len({col_of[i] for i in active})
                    if n_active > 1:
                        for i in active:
                            if n_active > max_concur[i]:
                                max_concur[i] = n_active
                if delta == 1:
                    active.append(idx)
                else:
                    active.remove(idx)
                prev_t = t

            return [(col_of[idx], max_concur[idx], items[idx][2]) for idx in range(n)]

        now = datetime.now()
        if sub_choice == "周":
            # ==================== 周视图：横向时间线（时间横排、日期竖排），全部课程显示 ====================
            DAY_ROW_H = 112          # 每天行的像素高度
            H_PX_PER_HOUR = 64       # 横向每小时的像素宽度
            body_w = total_minutes / 60.0 * H_PX_PER_HOUR

            # 顶部时间轴表头（横排整点刻度）
            head_cells = []
            for i in range((grid_end.hour - grid_start.hour) * 2 + 1):
                h = grid_start.hour + i // 2
                mm = (i % 2) * 30
                if mm == 0:
                    head_cells.append(
                        f'<div style="position:absolute;left:{i / 2 * H_PX_PER_HOUR}px;top:0;'
                        f'font-size:11px;color:#6b7280;transform:translateX(-50%);white-space:nowrap;">'
                        f'{_hour_label(h)}</div>'
                    )

            rows_html = []
            for i, d in enumerate(days):
                is_today = (d == today_d)
                is_weekend = d.weekday() >= 5
                row_bg = "#eff6ff" if is_today else ("#fbfbfb" if is_weekend else "#ffffff")

                # 当天课程（带起止分钟）
                day_items = []
                for s in by_day.get(d, []):
                    sdt = _sched_dt_local(s, "start_time")
                    edt = _sched_dt_local(s, "end_time")
                    if not sdt or not edt: continue
                    t = sdt.time(); et = edt.time()
                    if et <= grid_start or t >= grid_end: continue
                    s_min = max((t.hour - grid_start.hour) * 60 + t.minute, 0)
                    e_min = min((et.hour - grid_start.hour) * 60 + et.minute, int(total_minutes))
                    if e_min <= s_min: continue
                    day_items.append((s_min, e_min, s))

                # 并发课程在行内上下分槽（slot_idx / max_slots），横向跨度按时间计算
                placement = _layout_day(day_items)  # (slot_idx, max_slots, schedule)
                blocks = []
                for slot_idx, max_slots, s in placement:
                    found = next((x for x in day_items if x[2] is s), None)
                    if found is None: continue
                    s_min, e_min, _ = found
                    x0 = s_min / 60.0 * H_PX_PER_HOUR
                    w = (e_min - s_min) / 60.0 * H_PX_PER_HOUR
                    slot_h = DAY_ROW_H / max(max_slots, 1)
                    y0 = slot_idx * slot_h
                    status = _sched_status(s)
                    bg, fg = _sched_color(status)
                    title = _sched_title(s)
                    t_name = display_name(s.get("teacher") or "")
                    loc = (s.get("location") or "")
                    tip = _html_escape(f"{title}｜{t_name}｜{loc}")
                    title_s = _html_escape(title)
                    s2t = _sched_dt_local(s, "start_time")
                    e2t = _sched_dt_local(s, "end_time")
                    ts2 = s2t.strftime("%H:%M") if s2t else "-"
                    ets2 = e2t.strftime("%H:%M") if e2t else ""
                    meta = _html_escape(f"{ts2}-{ets2}" + (f" · {loc}" if loc else ""))
                    blocks.append(
                        f'<div title="{tip}" '
                        f'style="position:absolute;top:{y0 + 1}px;left:{x0 + 1}px;'
                        f'width:{max(w - 2, 18)}px;height:{max(slot_h - 2, 16)}px;'
                        f'background:{bg};color:{fg};border-radius:5px;padding:2px 4px;'
                        f'font-size:11px;line-height:1.25;overflow:hidden;'
                        f'box-shadow:0 1px 2px rgba(0,0,0,.18);cursor:pointer;'
                        f'display:flex;flex-direction:column;gap:1px;'
                        f'border:1px solid rgba(0,0,0,.08);">'
                        f'<div style="font-weight:700;white-space:normal;overflow:hidden;flex:1;min-height:0;'
                        f'display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;">{title_s}</div>'
                        f'<div style="font-size:9px;opacity:.85;flex:none;white-space:nowrap;'
                        f'overflow:hidden;text-overflow:ellipsis;">{meta}</div>'
                        f'</div>'
                    )

                # 半格网格竖线
                gridlines = "".join(
                    f'<div style="position:absolute;top:0;bottom:0;left:{j / 2 * H_PX_PER_HOUR}px;'
                    f'border-left:1px dashed #e5e7eb;"></div>'
                    for j in range(1, (grid_end.hour - grid_start.hour) * 2 + 1)
                )

                date_label = (("今天" if is_today else "周" + weekday_cn[i]) +
                              f"<br><small>{d.strftime('%m月%d日')}</small>")
                rows_html.append(
                    f'<div style="display:flex;border-bottom:1px solid #e5e7eb;">'
                    f'<div style="width:92px;flex:none;text-align:center;padding:4px 2px;'
                    f'font-size:12px;font-weight:600;background:{row_bg};'
                    f'color:{"#1d4ed8" if is_today else "#374151"};'
                    f'border-right:1px solid #e5e7eb;display:flex;flex-direction:column;'
                    f'justify-content:center;">{date_label}</div>'
                    f'<div style="flex:1;position:relative;height:{DAY_ROW_H}px;background:{row_bg};">'
                    f'{gridlines}{"".join(blocks)}</div>'
                    f'</div>'
                )

            # 现在时间竖线（覆盖全部日期行）
            now_vline = ""
            if now.date() in days and grid_start <= now.time() <= grid_end:
                now_x = ((now.hour - grid_start.hour) * 60 + now.minute) / 60.0 * H_PX_PER_HOUR
                now_vline = (
                    f'<div style="position:absolute;top:0;bottom:0;left:{now_x}px;'
                    f'width:0;z-index:6;pointer-events:none;">'
                    f'<div style="border-left:2px solid #ef4444;height:100%;"></div>'
                    f'<span style="position:absolute;top:2px;left:4px;background:#ef4444;color:#fff;'
                    f'font-size:10px;line-height:16px;padding:0 5px;border-radius:3px;white-space:nowrap;">'
                    f'现在 {now.strftime("%H:%M")}</span></div>'
                )

            head_label = f'<div style="position:relative;height:26px;width:{body_w}px;">{"".join(head_cells)}</div>'
            html = (
                f'<div style="overflow-x:auto;">'
                f'<div style="min-width:{92 + body_w}px;background:white;border:1px solid #e5e7eb;'
                f'border-radius:6px;overflow:hidden;">'
                f'<div style="display:flex;border-bottom:1px solid #e5e7eb;background:#f9fafb;">'
                f'<div style="width:92px;flex:none;padding:4px 0;text-align:center;font-size:12px;'
                f'font-weight:600;color:#374151;border-right:1px solid #e5e7eb;">日期</div>'
                f'<div style="flex:1;position:relative;">{head_label}</div>'
                f'</div>'
                f'<div style="position:relative;">{"".join(rows_html)}{now_vline}</div>'
                f'</div>'
                f'</div>'
            )
            with st.container(key="week_grid"):
                st.markdown(html, unsafe_allow_html=True)
                _render_day_click_overlay(days, "week", DAY_ROW_H)
        else:
            # ==================== 日视图：纵向时间网格 ====================
            time_col_html = []
            for i in range((grid_end.hour - grid_start.hour) * 2 + 1):
                h = grid_start.hour + i // 2
                mm = (i % 2) * 30
                if mm == 0:
                    label = _hour_label(h)
                    time_col_html.append(
                        f'<div style="height:{ROW_HALF_HOUR}px;padding-right:6px;text-align:right;'
                        f'color:#6b7280;font-size:11px;box-sizing:border-box;position:relative;">'
                        f'<span style="position:absolute;top:-7px;right:6px;background:white;padding:0 4px;">{label}</span>'
                        f'</div>'
                    )
                else:
                    time_col_html.append(
                        f'<div style="height:{ROW_HALF_HOUR}px;border-top:1px dashed #e5e7eb;"></div>'
                    )

            headers_html = []
            for i, d in enumerate(days):
                is_today = (d == today_d)
                head_bg = "#3b82f6" if is_today else "#f3f4f6"
                head_fg = "white" if is_today else "#374151"
                head_text = (("今天" if is_today else "周" + weekday_cn[i]) +
                             f"<br><small>{d.strftime('%m月%d日')}</small>")
                headers_html.append(
                    f'<div style="flex:1;padding:6px 0;text-align:center;font-size:12px;'
                    f'font-weight:600;background:{head_bg};color:{head_fg};">{head_text}</div>'
                )

            cols_html = []
            col_w = 100.0 / len(days)
            for i, d in enumerate(days):
                left = i * col_w
                rows_bg = "".join(
                    f'<div style="height:{ROW_HALF_HOUR}px;border-top:1px dashed #e5e7eb;"></div>'
                    for _ in range((grid_end.hour - grid_start.hour) * 2)
                )
                is_weekend = d.weekday() >= 5
                col_bg = "#eff6ff" if d == today_d else ("#fbfbfb" if is_weekend else "transparent")
                day_items = []
                for s in by_day.get(d, []):
                    sdt = _sched_dt_local(s, "start_time")
                    edt = _sched_dt_local(s, "end_time")
                    if not sdt or not edt: continue
                    t = sdt.time(); et = edt.time()
                    if et <= grid_start or t >= grid_end: continue
                    s_min = max((t.hour - grid_start.hour) * 60 + t.minute, 0)
                    e_min = min((et.hour - grid_start.hour) * 60 + et.minute,
                                int((grid_end.hour - grid_start.hour) * 60))
                    if e_min <= s_min: continue
                    day_items.append((s_min, e_min, s))

                placement = _layout_day(day_items)
                blocks = []
                for col_idx, total_cols, s in placement:
                    found = next((x for x in day_items if x[2] is s), None)
                    if found is None: continue
                    s_min, e_min, _ = found
                    top = s_min / 60.0 * PX_PER_HOUR
                    h = max((e_min - s_min) / 60.0 * PX_PER_HOUR, 40)
                    status = _sched_status(s)
                    bg, fg = _sched_color(status)
                    title = _sched_title(s)
                    t_name = display_name(s.get("teacher") or "")
                    loc = (s.get("location") or "")
                    sub_w = col_w / max(total_cols, 1)
                    sub_left = left + min(col_idx, total_cols - 1) * sub_w
                    tip = _html_escape(f"{title}｜{t_name}｜{loc}")
                    title_s = _html_escape(title)
                    s2t = _sched_dt_local(s, "start_time")
                    e2t = _sched_dt_local(s, "end_time")
                    ts2 = s2t.strftime("%H:%M") if s2t else "-"
                    ets2 = e2t.strftime("%H:%M") if e2t else ""
                    meta = _html_escape(f"{ts2}-{ets2}" + (f" · {loc}" if loc else ""))
                    block_html = (
                        f'<div title="{tip}" '
                        f'style="position:absolute;top:{top}px;left:calc({sub_left}% + 1px);'
                        f'width:calc({sub_w}% - 3px);height:{h}px;'
                        f'background:{bg};color:{fg};border-radius:6px;padding:4px 6px;'
                        f'font-size:12px;line-height:1.3;overflow:hidden;'
                        f'box-shadow:0 1px 3px rgba(0,0,0,.2);cursor:pointer;'
                        f'display:flex;flex-direction:column;gap:2px;'
                        f'border:1px solid rgba(0,0,0,.08);">'
                        f'<div style="font-weight:700;white-space:normal;overflow:hidden;flex:1;min-height:0;'
                        f'display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;">'
                        f'{title_s}</div>'
                        f'<div style="font-size:10px;opacity:.85;flex:none;white-space:nowrap;'
                        f'overflow:hidden;text-overflow:ellipsis;">{meta}</div>'
                        f'</div>'
                    )
                    blocks.append(block_html)
                cols_html.append(
                    f'<div style="position:absolute;top:0;left:{left}%;width:{col_w}%;height:{total_h}px;'
                    f'background:{col_bg};">'
                    f'<div style="position:absolute;top:0;left:0;right:0;bottom:0;">{rows_bg}</div>'
                    f'{"".join(blocks)}</div>'
                )

            # 当前时间指示线（仅当"现在"落在视图内）
            now_line = ""
            if now.date() in days and grid_start <= now.time() <= grid_end:
                now_top = ((now.hour - grid_start.hour) * 60 + now.minute) / 60.0 * PX_PER_HOUR
                now_line = (
                    f'<div style="position:absolute;top:{now_top}px;left:78px;right:0;z-index:6;'
                    f'pointer-events:none;">'
                    f'<div style="border-top:2px solid #ef4444;"></div>'
                    f'<span style="position:absolute;right:6px;top:-10px;background:#ef4444;color:#fff;'
                    f'font-size:10px;line-height:16px;padding:0 6px;border-radius:3px;">'
                    f'现在 {now.strftime("%H:%M")}</span></div>'
                )
            grid_min_w = 78 + len(days) * MIN_COL_W
            html = (
                f'<div style="overflow-x:auto;">'
                f'<div style="min-width:{grid_min_w}px;background:white;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;">'
                f'<div style="display:flex;border-bottom:1px solid #e5e7eb;background:#f9fafb;">'
                f'<div style="width:78px;padding:6px 0;text-align:center;font-size:12px;font-weight:600;color:#374151;border-right:1px solid #e5e7eb;">时间</div>'
                f'<div style="flex:1;display:flex;">{"".join(headers_html)}</div>'
                f'</div>'
                f'<div style="display:flex;position:relative;">'
                f'<div style="width:78px;border-right:1px solid #e5e7eb;">{"".join(time_col_html)}</div>'
                f'<div style="flex:1;position:relative;height:{total_h}px;">{"".join(cols_html)}</div>'
                f'{now_line}'
                f'</div>'
                f'</div>'
                f'</div>'
            )
            with st.container(key="day_grid"):
                st.markdown(html, unsafe_allow_html=True)
                _render_day_click_overlay(days, "day")

        # 课表统计
        total_count = len(schedules)
        done_count = sum(1 for s in schedules if _sched_status(s) == "done")
        st.caption(f"本周共 {total_count} 节课，已上 {done_count} 节。")
    else:
        # 月视图：调用原 render_month_grid
        cmm = st.session_state.cal_date
        st.markdown(
            f"<div style='text-align:right;color:#888;font-size:12px;'>本月共 {len(schedules)} 节课</div>",
            unsafe_allow_html=True)
        render_month_grid(cmm.year, cmm.month, schedules)

if "fb" in _tab_map:
    with _tab_map["fb"]:
        _feedback_section()


# ==================== 点击日期 → 弹出对话框（新建 / 删除当天课表） ====================
@st.dialog("📅 当天课表操作", width="large")
def day_dialog(day: date, day_scheds):
    st.markdown(f"### {day.strftime('%Y 年 %m 月 %d 日')}　共 {len(day_scheds)} 节课")
    if day_scheds:
        for s in sorted(day_scheds, key=lambda x: x.get("start_time", "")):
            dt = parse_dt(s.get("start_time", ""))
            et = parse_dt(s.get("end_time", ""))
            ts = f"{dt.strftime('%H:%M') if dt else '-'} – {et.strftime('%H:%M') if et else ''}"
            with st.container(border=True):
                c1, c2 = st.columns([5, 1])
                with c1:
                    st.markdown(f"**{ts}**　{s.get('title', '')}")
                    who = s.get("customer_name") or s.get("class_name") or "-"
                    info = (f"学员/班级：{who}　教师：{display_name(s.get('teacher', ''))}　"
                            f"地点：{s.get('location', '') or '-'}")
                    st.caption(info)
                with c2:
                    if can("action_schedules_delete"):
                        if st.button("🗑️ 删除", key=f"dlg_del_{s['id']}", use_container_width=True):
                            delete_schedule(s["id"])
                            st.session_state.dlg_day = day  # 删除后保持对话框打开，可继续操作
                            st.success("课表已删除")
                            st.rerun()
    else:
        st.info("当天暂无课表，可在下方新建。")

    # 新建课表（需操作权限，无权限时不显示）
    if can("action_schedules_create") and not CUSTOMER_CHOICES and not ALL_CLASSES:
        st.warning("暂无「在读/待续费」学员与班级。请先在「客户管理」将学员阶段调整为在读或待续费，"
                   "或在「班级管理」创建班级后再排课。")
    elif can("action_schedules_create"):
        dlg_kind = st.radio("排课类型", ["👤 1v1 学员排课", "👥 1v多 班级排课"],
                            horizontal=True, key=f"dlg_kind_{day}")
        customer_id = 0
        class_id = 0
        students, balances = [], {}
        cust = None
        batch_class = None
        teacher = ""
        subject = ""
        dlg_pkg_id = 0
        if dlg_kind.startswith("👤"):
            # 1v1：直接选学员 + 老师，无需课程/班级
            sc1, sc2 = st.columns(2)
            with sc1:
                if CUSTOMER_CHOICES:
                    cust_choice = st.selectbox("学员", list(CUSTOMER_CHOICES.keys()),
                                               key=f"dlg_cust_{day}")
                    customer_id = CUSTOMER_CHOICES[cust_choice]
                else:
                    st.selectbox("学员", ["暂无在读/待续费学员，请先在「客户管理」调整学员阶段"],
                                 disabled=True, key=f"dlg_cust_none_{day}")
            with sc2:
                st.caption("1v1 排课无需课程/班级，直接选学员 + 老师 + 科目即可。")
            if customer_id:
                cust = next((c for c in ALL_CUSTOMERS if c["id"] == customer_id), None)
                if cust:
                    students = [{"customer_id": customer_id, "name": cust["name"]}]
                    balances = get_customers_hour_balance([customer_id])
                    b = balances.get(customer_id, {})
                    st.caption(f"📊 学员课时余额（1v1 课时包）：**剩余 {b.get('remaining_1v1', b.get('remaining_hours', 0))} / "
                               f"已排 {b.get('scheduled_1v1', 0)} / "
                               f"可排 {b.get('available_1v1', b.get('available_hours', 0))}** 课时"
                               f"（可排 = 1v1剩余 − 1v1已排，同类型课时包组合使用）")
                    # 使用课时包（选填）：1v1 课时包；同类型可组合使用
                    _pkgs = get_active_packages_for_customers([customer_id]).get(customer_id, [])
                    _one_pkgs = [p for p in _pkgs if (p.get("type") or "1v1") == "1v1"]
                    if _one_pkgs:
                        _pkg_opts = _pkg_choice_options(_one_pkgs)
                        _pkg_sel = st.selectbox(
                            "使用课时包（选填）", list(_pkg_opts.keys()), key=f"dlg_pkg_{day}",
                            help="选择「🔄 自动组合」时，消课时自动从该学员同类型（1v1）课时包中依次扣减；"
                                 "也可指定某一个课时包优先扣减（不够时仍会从其他 1v1 课时包补充）。",
                        )
                        dlg_pkg_id = _pkg_opts[_pkg_sel]
                    elif _pkgs:
                        st.warning("该学员只有「1v多」课时包，暂无 1v1 课时包，无法扣减 1v1 课程课时。"
                                   "可先正常排课，课堂反馈消课时时系统将提示无匹配课时包。")
                    else:
                        st.caption("该学员暂无进行中课时包，可先排课（课堂反馈消课时时将提示无课时包）。")
        else:
            # 1v多：选班级（班级即课程），任课教师随班级自动带出
            sc1, sc2 = st.columns(2)
            with sc1:
                active_classes = [cl for cl in ALL_CLASSES if cl.get("status") == "进行中"]
                if active_classes:
                    class_ops = {f"{cl['name']}（{cl['class_type']}）": cl["id"] for cl in active_classes}
                    class_choice = st.selectbox("选择班级（1v多）", list(class_ops.keys()),
                                                key=f"dlg_cls_{day}")
                    class_id = class_ops[class_choice]
                else:
                    st.selectbox("选择班级", ["暂无进行中的班级，请先到「班级管理」建班"], disabled=True,
                                 key=f"dlg_cls_none_{day}")
            batch_class = next((cl for cl in ALL_CLASSES if cl["id"] == class_id), None)
            teacher = (batch_class or {}).get("teacher", "") or ""
            with sc2:
                st.caption("1v多 排课：全班学员统一上课，课时按各学员课时包分别扣减。")
                if batch_class and teacher:
                    st.markdown(f"👩‍🏫 任课教师：**{display_name(teacher)}**（随班级自动带出）")
                else:
                    st.caption("⚠️ 该班级未设置任课教师，请先在「班级管理」中为该班级指定任课教师。")
            students, balances = _render_balance_panel(class_id)

        with st.form(f"dlg_add_form_{day}", clear_on_submit=True):
            sc3, sc4 = st.columns(2)
            with sc3:
                st.caption("课表标题自动生成：1v1 为「学员 · 老师 · 科目」，1v多 为班级名。")
            with sc4:
                if dlg_kind.startswith("👤"):
                    # 1v1：选择任课教师
                    t_opts = ["(未分配)"] + [u["username"] for u in TEACHER_USERS]
                    teacher = st.selectbox("任课教师", t_opts, key=f"dlg_tea_{day}",
                                           format_func=lambda x: USER_LABEL.get(x, x))
                    teacher = "" if teacher == "(未分配)" else teacher
                elif not teacher:
                    # 1v多：任课教师随班级自动带出，无需再选；班级无教师时给出提示
                    st.caption("⚠️ 该班级未设置任课教师，请先在「班级管理」中指定。")
            # 科目：1v1 从所选老师的可带科目中选择
            if dlg_kind.startswith("👤") and teacher:
                subs = TEACHER_SUBJECTS.get(teacher, [])
                if subs:
                    subject = st.selectbox("科目（该老师可带）", subs, key=f"dlg_sub_{day}")
                else:
                    st.warning(f"老师「{display_name(teacher)}」未设置可带科目，"
                               f"请先在「员工管理」中配置科目后再排课。")
            final_title = _build_sched_title(dlg_kind, cust, batch_class, subject)
            st.caption(f"课表标题（自动）：**{final_title}**")
            sc5, sc6, sc7 = st.columns(3)
            with sc5:
                s_time = st.time_input("开始时间", value=datetime.strptime("09:00", "%H:%M").time(),
                                       key=f"dlg_st_{day}")
            with sc6:
                e_time = st.time_input("结束时间", value=datetime.strptime("10:00", "%H:%M").time(),
                                       key=f"dlg_et_{day}")
            with sc7:
                location = st.text_input("上课地点（选填）", placeholder="例如：3号琴房",
                                         key=f"dlg_loc_{day}")
            notes = st.text_input("备注（选填）", key=f"dlg_notes_{day}")
            submitted = st.form_submit_button("添加到课表", type="primary")

        if submitted:
            if e_time <= s_time:
                st.error("结束时间必须晚于开始时间")
            elif dlg_kind.startswith("👤") and not customer_id:
                st.error("请先选择学员（1v1 排课必须关联学员，用于课时校验与扣减）")
            elif dlg_kind.startswith("👤") and not teacher:
                st.error("请选择任课教师（1v1 排课必须指定老师）")
            elif dlg_kind.startswith("👤") and not subject:
                st.error("请选择科目（所选老师未设置可带科目，请先在「员工管理」中配置）")
            elif dlg_kind.startswith("👥") and not class_id:
                st.error("请先选择班级（1v多 排课必须关联班级）")
            elif dlg_kind.startswith("👥") and not teacher:
                st.error("该班级未设置任课教师，请先在「班级管理」中为该班级指定任课教师后，再排 1v多 课程。")
            else:
                new_hours = _calc_hours_needed([day], s_time, e_time)
                course_type = "1v1" if dlg_kind.startswith("👤") else "1v多"
                problem = _balance_problem(students, balances, new_hours, course_type)
                if problem:
                    st.error(f"⛔ 本节课需 {new_hours} 课时，以下学员可排课时不足，无法排课：{problem}")
                else:
                    start_time = f"{day} {s_time.strftime('%H:%M:%S')}"
                    end_time = f"{day} {e_time.strftime('%H:%M:%S')}"
                    final_title = _build_sched_title(dlg_kind, cust, batch_class, subject)
                    add_schedule(class_id=class_id, course_id=0, title=final_title,
                                 teacher=teacher, start_time=start_time, end_time=end_time,
                                 location=location.strip(), notes=notes.strip(),
                                 customer_id=customer_id, package_id=dlg_pkg_id)
                    st.session_state.dlg_day = day  # 添加后保持对话框打开，可连续添加
                    st.success("课表已添加")
                    st.rerun()

    st.markdown("---")
    if st.button("✖ 关闭", key=f"dlg_close_{day}"):
        st.session_state.pop("dlg_day", None)
        st.rerun()


# 点击格子后在本次 rerun 中打开对话框（取出即删，避免外部点击关闭后残留状态导致再次自动弹出）
if "dlg_day" in st.session_state:
    dlg = st.session_state.pop("dlg_day")
    day_scheds = [s for s in schedules if sched_day(s) == dlg]
    day_dialog(dlg, day_scheds)
