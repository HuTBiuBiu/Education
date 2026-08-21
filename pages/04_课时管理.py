"""
课时管理页面
学管仅查看自己名下已报名学生的课时情况；
顶部统计：昨日课消课时 / 今日已排课表 / 课时不足 10 的家长数；
「课时总览」以表格形式展示全部进行中课时包的剩余课时、到期日与预警等级；
「续费倒计时」以卡片形式列出课时小于 10 的家长；
「课时包」用于维护预设课时包模板（学员转在读时选择报名课时）；
「消课记录」与「消课统计」用于记录与统计每月课时消耗。
"""
import io
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta

from auth import can, get_visible_teacher, get_viewer_scope, require_login, require_page
from database import (
    COURSE_TYPES, GRADE_OPTIONS,
    add_course_record, delete_course_record, get_course_records,
    get_course_packages,
    get_course_package_templates, add_course_package_template, delete_course_package_template,
    get_all_course_packages_for_renewal,
    get_monthly_course_records,
    get_schedules,
    get_setting, set_setting, get_lesson_minutes,
)

# ---------- 登录校验与权限守卫 ----------
require_login()
require_page("page_hours")

TEACHER = get_visible_teacher()
VIEWER_ROLE, VIEWER_NAME = get_viewer_scope()

# 课时包类型（与课表 class_type 同口径：1v1 一对一 / 1v多 一对多）
PKG_TYPE_OPTIONS = ["1v1", "1v多"]
PKG_TYPE_LABEL = {"1v1": "一对一", "1v多": "一对多"}

# ---------- 上传安全限制（防止超大文件导致内存耗尽 DoS） ----------
MAX_IMPORT_SIZE = 10 * 1024 * 1024  # 文件大小上限 10MB（与服务端 maxUploadSize 一致）
MAX_IMPORT_ROWS = 5000              # 单次导入行数上限

st.title("⏰ 课时管理")
st.caption("学管仅查看自己名下已报名学生的课时情况。")

# ==================== 顶部统计卡片 ====================
today_str = datetime.now().strftime("%Y-%m-%d")
yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
yesterday_records = get_course_records(date_from=yesterday_str, date_to=yesterday_str, teacher=TEACHER)
yesterday_hours = round(sum(r["hours_used"] for r in yesterday_records), 1)

manager_q = VIEWER_NAME if VIEWER_ROLE == "staff" else ""
today_schedules = get_schedules(date_from=today_str, date_to=today_str, manager=manager_q)
scheduled_count = len(today_schedules)

renew_packages = get_all_course_packages_for_renewal(teacher=TEACHER)
low_hours = [p for p in renew_packages if p["remaining_hours"] < 10]
low_parents = len({p["customer_id"] for p in low_hours})

col1, col2, col3 = st.columns(3)
col1.metric("昨日课消课时", f"{yesterday_hours:.1f} 课时")
col2.metric("今日已排课表", f"{scheduled_count} 节")
col3.metric("课时不足 10 的家长", f"{low_parents} 位")

st.markdown("---")

# ==================== 单课时时长设置（仅管理员） ====================
if can("action_hours_set_lesson"):
    with st.expander("⚙️ 单课时时长设置（全局生效）", expanded=False):
        st.caption("设置每个课时的标准时长（分钟）。设置后，老师提交课堂反馈时将按「课表实际时长 ÷ 单课时时长」自动换算课时数，并从对应学员课时包中扣减。")
        current_lesson_minutes = get_lesson_minutes()
        c1, c2 = st.columns([2, 1])
        with c1:
            new_lesson_minutes = st.number_input(
                "单课时时长（分钟）",
                min_value=1, max_value=480, value=int(current_lesson_minutes),
                step=5, key="lesson_minutes_input",
            )
        with c2:
            st.metric("当前设置", f"{current_lesson_minutes} 分钟/课时")
        if st.button("💾 保存设置", key="save_lesson_minutes"):
            set_setting("lesson_minutes", str(int(new_lesson_minutes)))
            st.success(f"单课时时长已更新为 {int(new_lesson_minutes)} 分钟/课时，全系统课时换算同步生效。")
            st.rerun()
    st.markdown("---")

# ==================== Tab 页签（按权限过滤） ====================
tab_defs = [
    ("tab_hours_overview", "📋 课时总览"),
    ("tab_hours_renewal", "⚠️ 续费倒计时"),
    ("tab_hours_packages", "📦 课时包"),
    ("tab_hours_records", "📝 消课记录"),
    ("tab_hours_stats", "📊 消课统计"),
]
tab_defs = [(k, l) for k, l in tab_defs if can(k)]

if not tab_defs:
    st.info("当前角色无课时管理选项卡权限。")
    st.stop()
tabs = st.tabs([label for _, label in tab_defs])
tab_map = {k: t for (k, _), t in zip(tab_defs, tabs)}


def _render_renewal_card(p: dict):
    """课时不足 10 的家长卡片"""
    days_left = p["days_left"]
    remain = p["remaining_hours"]
    if days_left <= 0:
        level = "🔴 已到期"
    elif days_left <= 7 or remain <= 2:
        level = "🟠 紧急"
    elif days_left <= 30:
        level = "🟡 即将到期"
    else:
        level = "🟢 正常"
    with st.container(border=True):
        st.markdown(f"**👤 {p['customer_name']}**　{level}")
        _pt = p.get("type") or "1v1"
        st.caption(f"📞 {p.get('phone') or '-'}　|　课时包：{p['package_name']}（{_pt}）")
        m1, m2, m3 = st.columns(3)
        m1.metric("剩余课时", f"{remain:.1f}")
        m2.metric("到期日", p["expiry_date"])
        m3.metric("剩余天数", f"{days_left}")
        st.caption(f"总课时 {p['total_hours']:.0f} · 已用 {p['used_hours']:.1f}")


# ==================== Tab1: 课时总览（全部进行中课时包剩余情况） ====================
with tab_map["tab_hours_overview"]:
    st.subheader("📋 课时总览")
    st.caption("全部进行中课时包的课时剩余一览：按剩余课时从少到多排序；支持按预警等级筛选与关键字搜索。")

    all_pkgs = get_all_course_packages_for_renewal(teacher=TEACHER)
    if not all_pkgs:
        st.info("暂无进行中的课时包，请联系管理员为学员登记课时包。")
    else:
        o1, o2, _ = st.columns([1, 1, 2])
        with o1:
            ov_level = st.selectbox(
                "预警等级", ["全部", "🔴 已过期", "🟠 紧急", "🟡 提醒", "🟢 正常"],
                key="ov_level",
            )
        with o2:
            ov_kw = st.text_input("搜索客户 / 课时包", key="ov_kw")

        def _ov_level(p: dict) -> str:
            days = p.get("days_left")
            if days is None:
                days = 9999
            remain = float(p.get("remaining_hours") or 0)
            if days <= 0:
                return "🔴 已过期"
            if days <= 7 or remain <= 1:
                return "🟠 紧急"
            if days <= 30 or remain <= 5:
                return "🟡 提醒"
            return "🟢 正常"

        rows = []
        for p in all_pkgs:
            days = p.get("days_left")
            _pt = p.get("type") or "1v1"
            rows.append({
                "预警": _ov_level(p),
                "客户": p.get("customer_name", ""),
                "联系电话": p.get("phone") or "-",
                "课时包": p.get("package_name", ""),
                "类型": f"{_pt}（{PKG_TYPE_LABEL[_pt]}）",
                "总课时": float(p.get("total_hours") or 0),
                "已用课时": round(float(p.get("used_hours") or 0), 1),
                "剩余课时": round(float(p.get("remaining_hours") or 0), 1),
                "到期日": p.get("expiry_date") or "-",
                "剩余天数": days if days is not None else "-",
            })

        if ov_level != "全部":
            rows = [r for r in rows if r["预警"] == ov_level]
        if ov_kw and ov_kw.strip():
            k = ov_kw.strip()
            rows = [r for r in rows if k in r["客户"] or k in r["课时包"]]
        rows = sorted(rows, key=lambda r: r["剩余课时"])

        total_remain = round(sum(r["剩余课时"] for r in rows), 1)
        urgent_count = sum(1 for r in rows if r["预警"] in ("🔴 已过期", "🟠 紧急"))

        g1, g2, g3 = st.columns(3)
        g1.metric("课时包数", f"{len(rows)} 个")
        g2.metric("剩余课时合计", f"{total_remain:.1f} 节")
        g3.metric("紧急 / 已过期", f"{urgent_count} 个")

        st.dataframe(rows, use_container_width=True, hide_index=True)


# ==================== Tab2: 续费倒计时（课时小于 10 的家长卡片） ====================
with tab_map["tab_hours_renewal"]:
    st.subheader("⏰ 续费倒计时")
    if not low_hours:
        st.success("🎉 暂无剩余课时不足 10 节的家长。")
    else:
        st.caption(f"共 {low_parents} 位家长 / {len(low_hours)} 个课时包剩余课时不足 10 节，请及时跟进续费。")
        for i in range(0, len(low_hours), 2):
            row = low_hours[i:i + 2]
            cols = st.columns(len(row))
            for col, p in zip(cols, row):
                with col:
                    _render_renewal_card(p)


# ==================== Tab3: 课时包（预设课时包模板） ====================
with tab_map["tab_hours_packages"]:
    st.subheader("📦 课时包")
    st.caption("维护预设课时包模板：学员在「客户管理」中转入在读填写报名课时时，可直接选择课时包并自动带入课时数与售价；「适用年级」选择「不限」表示所有年级通用，报名时仅显示与该学员年级匹配的课时包。")

    # 新建课时包（需操作权限）
    if can("action_hours_pkg_add"):
        with st.expander("➕ 新建课时包", expanded=False):
            tp1, tp2 = st.columns(2)
            with tp1:
                tpl_name = st.text_input("课时包名称 *", placeholder="例如：48 课时标准包", key="tpl_name")
                tpl_hours = st.number_input(
                    "课时数（节） *", min_value=0.5, step=1.0, format="%.1f",
                    value=48.0, key="tpl_hours",
                )
            with tp2:
                tpl_price = st.number_input(
                    "课时包价格（元） *", min_value=0.0, step=100.0, format="%.2f",
                    value=0.0, key="tpl_price",
                )
                tpl_grade = st.selectbox(
                    "适用年级", ["不限"] + GRADE_OPTIONS, index=0, key="tpl_grade",
                    help="选择「不限」表示该课时包所有年级通用；报名选课时包时仅显示学员当前年级的课时包",
                )
                tpl_status = st.selectbox("状态", ["启用", "停用"], key="tpl_status")

            tpl_type = st.selectbox(
                "类型", PKG_TYPE_OPTIONS, index=0, key="tpl_type",
                format_func=lambda t: f"{t}（{PKG_TYPE_LABEL[t]}）",
                help="一对一课时包仅用于 1v1 课程消课，一对多课时包仅用于 1v多 课程消课；同类型的多个课时包可组合使用",
            )

            if st.button("💾 保存课时包", key="tpl_save"):
                if not tpl_name.strip():
                    st.error("请输入课时包名称！")
                elif tpl_hours <= 0:
                    st.error("课时数必须大于 0！")
                else:
                    try:
                        add_course_package_template(
                            tpl_name.strip(), float(tpl_hours), float(tpl_price),
                            tpl_status, "" if tpl_grade == "不限" else (tpl_grade or "").strip(),
                            tpl_type,
                        )
                        st.success(f"课时包「{tpl_name.strip()}」已创建！")
                        st.rerun()
                    except Exception as e:
                        st.error(f"创建失败：{str(e)}")

    # 批量导入课时包（需操作权限）
    if can("action_hours_pkg_import"):
        with st.expander("📥 批量导入课时包", expanded=False):
            st.markdown(
                "**导入要求：**\n"
                "- 支持 `.xlsx` / `.xls` 格式\n"
                "- 表头需包含：`课时包名称`、`课时数（节）`、`价格（元）`、`适用年级`、`状态`、`类型`\n"
                "- `课时包名称`、`课时数（节）` 为必填；`价格（元）` 留空默认 0\n"
                "- `适用年级` 留空或填「不限」表示所有年级通用；填写时必须为系统选项：一年级、二年级、三年级、四年级、五年级、六年级、初一、初二、初三、高一、高二、高三\n"
                "- `状态` 可选「启用/停用」，留空默认「启用」\n"
                "- `类型` 填「1v1 / 一对一」或「1v多 / 一对多」，留空默认「1v1」\n"
                "- 名称重复或年级非法的课时包会被跳过"
            )
            # 课时包导入模板下载
            _tpl_template_data = {
                "课时包名称": ["48 课时标准包", "24 课时进阶包"],
                "课时数（节）": [48, 24],
                "价格（元）": [4800, 2600],
                "适用年级": ["不限", "三年级"],
                "状态": ["启用", "启用"],
                "类型": ["1v1", "1v多"],
            }
            _df_tpl_template = pd.DataFrame(_tpl_template_data)
            _output_tpl = io.BytesIO()
            with pd.ExcelWriter(_output_tpl, engine="openpyxl") as writer:
                _df_tpl_template.to_excel(writer, index=False, sheet_name="课时包导入模板")
                ws_tpl = writer.sheets["课时包导入模板"]
                for col in ws_tpl.columns:
                    max_len = max(len(str(cell.value or "")) for cell in col)
                    ws_tpl.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)
            _output_tpl.seek(0)
            st.download_button(
                label="📥 下载课时包导入模板.xlsx",
                data=_output_tpl,
                file_name="课时包导入模板.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="tpl_template_dl",
            )
            tpl_import_file = st.file_uploader(
                "选择 Excel 文件", type=["xlsx", "xls"], key="tpl_import_file"
            )
            if tpl_import_file is not None and getattr(tpl_import_file, "size", 0) <= MAX_IMPORT_SIZE:
                try:
                    df_import = pd.read_excel(tpl_import_file)
                except Exception as e:
                    st.error(f"❌ 读取 Excel 文件失败：{str(e)}")
                    df_import = None
                if df_import is not None and len(df_import) <= MAX_IMPORT_ROWS:
                    st.markdown(f"**已读取 {len(df_import)} 行数据**（预览前 10 行）")
                    st.dataframe(df_import.head(10), use_container_width=True)
                    if "课时包名称" not in df_import.columns:
                        st.error("❌ Excel 文件中缺少「课时包名称」列，请按列名 `课时包名称 / 课时数（节） / 价格（元） / 适用年级 / 状态 / 类型` 填写！")
                    elif st.button("🚀 开始导入", type="primary", key="tpl_import_btn"):
                        ok_count, skip_count = 0, 0
                        for idx, row in df_import.iterrows():
                            tpl_name_i = str(row.get("课时包名称", "")).strip()
                            if not tpl_name_i or tpl_name_i == "nan":
                                skip_count += 1
                                continue
                            try:
                                tpl_hours_i = float(row.get("课时数（节）", 0) or 0)
                            except (TypeError, ValueError):
                                tpl_hours_i = 0
                            try:
                                tpl_price_i = float(row.get("价格（元）", 0) or 0)
                            except (TypeError, ValueError):
                                tpl_price_i = 0
                            tpl_grade_i = str(row.get("适用年级", "")).strip() if pd.notna(row.get("适用年级")) else ""
                            if tpl_grade_i in ("nan", "不限"):
                                tpl_grade_i = ""
                            if tpl_grade_i and tpl_grade_i not in GRADE_OPTIONS:
                                skip_count += 1
                                continue
                            tpl_status_i = str(row.get("状态", "")).strip() if pd.notna(row.get("状态")) else ""
                            if tpl_status_i not in ("启用", "停用"):
                                tpl_status_i = "启用"
                            tpl_type_i = str(row.get("类型", "")).strip() if pd.notna(row.get("类型")) else ""
                            if tpl_type_i in ("1v多", "一对多", "一对多(1v多)", "1对多"):
                                tpl_type_i = "1v多"
                            else:
                                tpl_type_i = "1v1"
                            if tpl_hours_i <= 0:
                                skip_count += 1
                                continue
                            try:
                                add_course_package_template(
                                    tpl_name_i, tpl_hours_i, tpl_price_i, tpl_status_i, tpl_grade_i,
                                    tpl_type_i,
                                )
                                ok_count += 1
                            except Exception:
                                skip_count += 1
                        if ok_count > 0:
                            st.success(f"✅ 成功导入 {ok_count} 个课时包！")
                        if skip_count > 0:
                            st.warning(f"⚠️ 跳过 {skip_count} 行（空名称 / 课时数无效 / 名称重复等）。")
                        st.caption("提示：课时包列表已刷新，可点击「开始导入」重复导入其他文件，名称重复的会被自动跳过。")
                elif df_import is not None:
                    st.error(f"❌ 单次最多导入 {MAX_IMPORT_ROWS} 行数据，当前文件共 {len(df_import)} 行，请拆分后重试。")
            elif tpl_import_file is not None:
                st.error(f"❌ 文件超过 {MAX_IMPORT_SIZE // (1024 * 1024)}MB 大小限制，请压缩或拆分后重试。")

    st.markdown("---")

    # 现有课时包列表（表格展示）
    st.subheader("📋 现有课时包")
    templates = get_course_package_templates()
    if templates:
        df_tpl = pd.DataFrame([{
            "ID": t["id"],
            "课时包名称": t["name"],
            "类型": f"{t.get('type') or '1v1'}（{PKG_TYPE_LABEL[t.get('type') or '1v1']}）",
            "适用年级": (t.get("grade") or "").strip() or "不限",
            "课时数（节）": float(t["total_hours"]),
            "价格（元）": float(t["price"]),
            "状态": t.get("status", ""),
            "创建时间": t.get("created_at", ""),
        } for t in templates])
        st.dataframe(df_tpl, use_container_width=True, hide_index=True)

        # 删除课时包（需操作权限）
        if can("action_hours_pkg_del"):
            with st.expander("🗑 删除课时包（点击展开）"):
                tpl_del_ops = {
                    f"#{t['id']} {t['name']}（{t.get('type') or '1v1'} / {float(t['total_hours']):.0f}课时 / ¥{float(t['price']):.0f}"
                    + ("" if not (t.get("grade") or "").strip() else f" / {t['grade']}年级") + "）": t["id"]
                    for t in templates
                }
                tpl_del_sel = st.selectbox("选择要删除的课时包", list(tpl_del_ops.keys()), key="tpl_del_sel")
                if st.button("确认删除", key="tpl_del_btn"):
                    delete_course_package_template(tpl_del_ops[tpl_del_sel])
                    st.success("已删除该课时包")
                    st.rerun()
    else:
        st.info("暂无课时包，请先新建。")


# ==================== Tab4: 消课记录 ====================
with tab_map["tab_hours_records"]:
    st.subheader("📝 消课记录")
    st.caption(f"当前单课时时长：{get_lesson_minutes()} 分钟/课时（课堂反馈提交后将自动按课表时长换算扣减）。")

    # 新增消课（需操作权限）
    if can("action_hours_add_rec"):
        with st.expander("➕ 新增消课记录", expanded=False):
            active_packages = get_course_packages(status="进行中", teacher=TEACHER)
            if active_packages:
                pkg_options = {
                    f"{p['customer_name']} | {p['package_name']} ({p.get('type') or '1v1'}，剩余{p['remaining_hours']:.1f}课时)": p
                    for p in active_packages
                }
                selected_label = st.selectbox("选择课时包 *", list(pkg_options.keys()), key="rec_pkg")
                selected_pkg = pkg_options[selected_label]

                rc1, rc2, rc3 = st.columns(3)
                with rc1:
                    record_date = st.date_input("上课日期", value=datetime.now(), key="rec_date")
                    hours_used = st.number_input(
                        "消耗课时 *", min_value=0.1, max_value=float(selected_pkg["remaining_hours"]),
                        value=1.0, step=0.5, key="rec_hours",
                    )
                with rc2:
                    course_type = st.selectbox("课程类型", COURSE_TYPES, key="rec_type")
                    teacher = st.text_input("授课老师", key="rec_teacher")
                with rc3:
                    rec_notes = st.text_area("备注", key="rec_notes", height=80)

                if st.button("💾 记录消课", key="rec_save"):
                    if hours_used <= 0:
                        st.error("消耗课时必须大于0！")
                    elif hours_used > selected_pkg["remaining_hours"]:
                        st.error(f"消耗课时({hours_used})超过剩余课时({selected_pkg['remaining_hours']:.1f})！")
                    else:
                        add_course_record(
                            package_id=selected_pkg["id"],
                            customer_id=selected_pkg["customer_id"],
                            record_date=record_date.strftime("%Y-%m-%d"),
                            hours_used=hours_used,
                            course_type=course_type,
                            teacher=teacher.strip(),
                            notes=rec_notes.strip(),
                        )
                        st.success(f"消课记录已添加！消耗 {hours_used} 课时。")
                        st.rerun()
            else:
                st.info("暂无进行中的课时包，请先为学员购买课时包（联系管理员）。")

    st.markdown("---")

    # 消课记录列表
    st.subheader("📋 消课记录列表")
    rcol1, rcol2 = st.columns([1, 1])
    with rcol1:
        rec_date_from = st.date_input("开始日期", value=datetime.now().replace(day=1), key="rec_from")
    with rcol2:
        rec_date_to = st.date_input("结束日期", value=datetime.now(), key="rec_to")

    records = get_course_records(
        date_from=rec_date_from.strftime("%Y-%m-%d"),
        date_to=rec_date_to.strftime("%Y-%m-%d"),
        teacher=TEACHER,
    )

    if records:
        total_hours = sum(r["hours_used"] for r in records)
        st.caption(f"共 {len(records)} 条记录，合计消课 {total_hours:.1f} 课时")

        # ID 为按当前列表临时编号，非数据库 ID
        df_records = pd.DataFrame([{
            "ID": i,
            "日期": r.get("record_date", ""),
            "客户": r.get("customer_name", ""),
            "课时包": r.get("package_name", ""),
            "消耗课时": r.get("hours_used", 0),
            "课程类型": r.get("course_type", ""),
            "老师": r.get("teacher", ""),
            "备注": r.get("notes", ""),
        } for i, r in enumerate(records, 1)])
        display_cols = ["ID", "日期", "客户", "课时包", "消耗课时", "课程类型", "老师", "备注"]
        df_display = df_records[[c for c in display_cols if c in df_records.columns]]
        st.dataframe(df_display, use_container_width=True, hide_index=True)

        # 删除按钮（需操作权限，按列表选择删除）
        if can("action_hours_del_rec"):
            with st.expander("🗑 删除消课记录（点击展开）"):
                rec_del_ops = {
                    f"#{i} {r.get('record_date', '')} {r.get('customer_name', '')} | {r.get('package_name', '')}": r["id"]
                    for i, r in enumerate(records, 1)
                }
                rec_del_choice = st.selectbox("选择要删除的记录", list(rec_del_ops.keys()), key="del_rec_sel")
                if st.button("确认删除", key="del_rec_btn"):
                    success = delete_course_record(rec_del_ops[rec_del_choice])
                    if success:
                        st.success("已删除该消课记录（课时已退回）")
                        st.rerun()
                    else:
                        st.error("未找到该记录")
    else:
        st.info("当前日期范围内无消课记录。")


# ==================== Tab5: 消课统计 ====================
with tab_map["tab_hours_stats"]:
    st.subheader("📊 月度消课统计")

    sy1, sy2, _ = st.columns([1, 1, 2])
    with sy1:
        stat_year = st.selectbox("年份", list(range(2024, 2031)), index=datetime.now().year - 2024, key="stat_year")
    with sy2:
        stat_month = st.selectbox("月份", list(range(1, 13)), index=datetime.now().month - 1, key="stat_month")

    monthly = get_monthly_course_records(year=stat_year, month=stat_month, teacher=TEACHER)

    if monthly:
        df_monthly = pd.DataFrame(monthly)
        df_monthly = df_monthly.rename(columns={
            "record_date": "日期", "daily_hours": "消课课时", "record_count": "上课次数",
        })
        df_monthly["日期"] = pd.to_datetime(df_monthly["日期"])
        df_monthly = df_monthly.sort_values("日期")

        total_month_hours = df_monthly["消课课时"].sum()
        total_month_count = df_monthly["上课次数"].sum()

        mc1, mc2 = st.columns(2)
        mc1.metric("本月消课总计", f"{total_month_hours:.1f} 课时")
        mc2.metric("本月上课次数", f"{total_month_count} 次")

        st.dataframe(
            df_monthly[["日期", "消课课时", "上课次数"]],
            use_container_width=True,
            hide_index=True,
        )

        # 简单柱状图
        st.bar_chart(
            df_monthly.set_index("日期")["消课课时"],
            use_container_width=True,
            height=300,
        )
    else:
        st.info(f"{stat_year}年{stat_month}月暂无消课记录。")

st.markdown("---")
st.caption("💡 提示：课时包到期会自动标记为「已到期」；删除消课记录会自动退回课时。")
