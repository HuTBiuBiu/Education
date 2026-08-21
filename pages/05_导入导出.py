"""
Excel 导入导出页面
支持客户数据的 Excel 批量导入与导出，含模板下载功能
"""

import streamlit as st
import pandas as pd
import io
from datetime import datetime
from auth import can, current_user, get_visible_teacher, is_admin, require_login, require_page
from database import (
    add_customer, get_all_customers, get_all_users, get_follow_ups,
    get_course_packages, get_course_records,
    LIFECYCLE_STAGES, SOURCE_CHANNELS, INTENT_FRUIT_OPTIONS, GRADE_OPTIONS,
)

# ---------- 上传安全限制（防止超大文件导致内存耗尽 DoS） ----------
MAX_IMPORT_SIZE = 10 * 1024 * 1024  # 文件大小上限 10MB（与服务端 maxUploadSize 一致）
MAX_IMPORT_ROWS = 5000              # 单次导入行数上限

# ---------- 登录校验与权限守卫 ----------
require_login()
require_page("page_io")

TEACHER = get_visible_teacher()
IS_ADMIN = is_admin()

st.title("📁 Excel 导入导出")

# 选项卡按权限过滤：选项卡可见 + 对应操作权限（无操作权限的选项卡整体隐藏）
_tab_io_action = {
    "tab_io_import": "action_io_import",
    "tab_io_export": "action_io_export",
    "tab_io_template": "action_io_template",
}
tab_defs = [
    ("tab_io_import", "📥 导入客户"),
    ("tab_io_export", "📤 导出数据"),
    ("tab_io_template", "📋 模板下载"),
]
tab_defs = [(k, l) for k, l in tab_defs if can(k) and can(_tab_io_action[k])]
if not tab_defs:
    st.info("当前角色无导入导出选项卡权限。")
    st.stop()
tabs = st.tabs([label for _, label in tab_defs])
tab_map = {key: tabs[i] for i, (key, _label) in enumerate(tab_defs)}

# ==================== Tab1: 导入客户 ====================
if "tab_io_import" in tab_map:
    with tab_map["tab_io_import"]:
        st.subheader("📥 批量导入客户数据")

        if not can("action_io_import"):
            st.stop()

        st.markdown("""
        **导入要求：**
        - 支持 `.xlsx` / `.xls` 格式
        - 表头需包含以下列：`姓名`、`手机号`、`微信号`、`来源渠道`、`生命周期阶段`、`客户意向（苹果）`、`学校`、`年级`、`备注`
        - `姓名`、`年级` 为必填；其他列留空则使用默认值
        - `年级` 必须为以下选项之一（用于报名选课时包时按年级匹配，请与课时包「适用年级」保持一致）：
          `一年级 / 二年级 / 三年级 / 四年级 / 五年级 / 六年级 / 初一 / 初二 / 初三 / 高一 / 高二 / 高三`
        """)

        uploaded_file = st.file_uploader("选择 Excel 文件", type=["xlsx", "xls"], key="import_upload")

        # 导入归属：学管自动归属自己；管理员可指定学管（默认未分配）
        if IS_ADMIN:
            all_users = get_all_users()
            import_teacher_options = ["（未分配）"] + [u["username"] for u in all_users]
            import_teacher_labels = ["（未分配）"] + [f"{u['display_name']}（@{u['username']}）" for u in all_users]
            sel_import_teacher = st.selectbox(
                "👨‍🏫 导入客户归属学管", import_teacher_labels, key="import_teacher"
            )
            import_teacher = import_teacher_options[import_teacher_labels.index(sel_import_teacher)] if sel_import_teacher != "（未分配）" else ""
        else:
            import_teacher = TEACHER
            st.caption(f"👨‍🏫 导入客户将自动归属当前学管：{current_user().get('display_name', TEACHER)}")

        if uploaded_file is not None and getattr(uploaded_file, "size", 0) <= MAX_IMPORT_SIZE:
            try:
                df_import = pd.read_excel(uploaded_file)

                if len(df_import) > MAX_IMPORT_ROWS:
                    st.error(f"❌ 单次最多导入 {MAX_IMPORT_ROWS} 行数据，当前文件共 {len(df_import)} 行，请拆分后重试。")
                else:
                    st.markdown(f"**已读取 {len(df_import)} 行数据**")

                    # 列名映射（中文 → 英文）
                    col_mapping = {
                        "姓名": "name",
                        "手机号": "phone",
                        "微信号": "wechat",
                        "来源渠道": "source",
                        "生命周期阶段": "lifecycle_stage",
                        "客户意向（苹果）": "intent_fruit",
                        "学校": "school",
                        "年级": "grade",
                        "备注": "notes",
                    }

                    # 检查必须列
                    if "姓名" not in df_import.columns:
                        st.error("❌ Excel 文件中缺少「姓名」列！请使用模板文件。")
                    else:
                        # 预览数据
                        st.dataframe(df_import.head(10), use_container_width=True)

                        if st.button("🚀 开始导入", type="primary"):
                            success_count = 0
                            skip_count = 0
                            errors = []

                            for idx, row in df_import.iterrows():
                                name = str(row.get("姓名", "")).strip()
                                if not name or name == "nan":
                                    skip_count += 1
                                    continue

                                phone = str(row.get("手机号", "")).strip() if pd.notna(row.get("手机号")) else ""
                                phone = "" if phone == "nan" else phone

                                wechat = str(row.get("微信号", "")).strip() if pd.notna(row.get("微信号")) else ""
                                wechat = "" if wechat == "nan" else wechat

                                notes = str(row.get("备注", "")).strip() if pd.notna(row.get("备注")) else ""
                                notes = "" if notes == "nan" else notes

                                # 来源渠道校验
                                source = str(row.get("来源渠道", "")).strip() if pd.notna(row.get("来源渠道")) else ""
                                if source not in SOURCE_CHANNELS:
                                    source = "自然流量"

                                # 生命周期阶段校验
                                stage = str(row.get("生命周期阶段", "")).strip() if pd.notna(row.get("生命周期阶段")) else ""
                                if stage not in LIFECYCLE_STAGES:
                                    stage = "新线索"

                                # 客户意向（苹果）校验
                                fruit = str(row.get("客户意向（苹果）", "")).strip() if pd.notna(row.get("客户意向（苹果）")) else ""
                                if fruit not in INTENT_FRUIT_OPTIONS:
                                    fruit = "🍏 青苹果"

                                # 学校与年级
                                school = str(row.get("学校", "")).strip() if pd.notna(row.get("学校")) else ""
                                school = "" if school == "nan" else school
                                grade = str(row.get("年级", "")).strip() if pd.notna(row.get("年级")) else ""
                                grade = "" if grade == "nan" else grade
                                # 年级为必填项（用于报名时按年级匹配课时包），且必须为系统选项
                                if not grade:
                                    skip_count += 1
                                    errors.append(f"第 {idx + 2} 行「{name}」跳过：年级为必填项，未填写年级")
                                    continue
                                if grade not in GRADE_OPTIONS:
                                    skip_count += 1
                                    errors.append(f"第 {idx + 2} 行「{name}」跳过：年级「{grade}」不是系统选项（可选：{'、'.join(GRADE_OPTIONS)}）")
                                    continue

                                try:
                                    add_customer(
                                        name=name, phone=phone, wechat=wechat,
                                        source=source,
                                        lifecycle_stage=stage, notes=notes,
                                        intent_fruit=fruit,
                                        school=school, grade=grade,
                                        teacher=import_teacher,
                                    )
                                    success_count += 1
                                except Exception as e:
                                    errors.append(f"第 {idx + 2} 行「{name}」导入失败：{str(e)}")

                            # 结果反馈
                            if success_count > 0:
                                st.success(f"✅ 成功导入 {success_count} 条客户记录！")
                            if skip_count > 0:
                                st.warning(f"⚠️ 跳过 {skip_count} 条（空姓名或未填写年级的行）。")
                            if errors:
                                for err in errors[:5]:
                                    st.error(err)
                                if len(errors) > 5:
                                    st.error(f"...共 {len(errors)} 条错误")

            except Exception as e:
                st.error(f"❌ 读取 Excel 文件失败：{str(e)}")
        elif uploaded_file is not None:
            st.error(f"❌ 文件超过 {MAX_IMPORT_SIZE // (1024 * 1024)}MB 大小限制，请压缩或拆分后重试。")


# ==================== Tab2: 导出数据 ====================
if "tab_io_export" in tab_map:
    with tab_map["tab_io_export"]:
        st.subheader("📤 导出数据到 Excel")

        if not can("action_io_export"):
            st.stop()

        export_type = st.radio("导出内容", ["客户数据", "跟进记录", "课时包数据", "消课记录"], horizontal=True)

        if export_type == "客户数据":
            customers = get_all_customers(teacher=TEACHER)
            if customers:
                df_export = pd.DataFrame(customers)
                # 重命名列为中文
                col_rename = {
                    "id": "ID", "name": "姓名", "phone": "手机号", "wechat": "微信号",
                    "source": "来源渠道",
                    "intent_fruit": "客户意向（苹果）",
                    "lifecycle_stage": "生命周期阶段", "school": "学校", "grade": "年级",
                    "notes": "备注", "created_at": "创建时间", "updated_at": "更新时间",
                }
                df_export = df_export.rename(columns=col_rename)
                # 排列顺序
                col_order = ["ID", "姓名", "手机号", "微信号", "来源渠道",
                             "客户意向（苹果）", "生命周期阶段", "学校", "年级",
                             "备注", "创建时间", "更新时间"]
                df_export = df_export[[c for c in col_order if c in df_export.columns]]
            else:
                df_export = pd.DataFrame()
                st.info("暂无客户数据可导出。")

        elif export_type == "跟进记录":
            follow_ups = get_follow_ups(teacher=TEACHER)
            if follow_ups:
                df_export = pd.DataFrame(follow_ups)
                col_rename = {
                    "id": "ID", "customer_id": "客户ID", "customer_name": "客户姓名",
                    "lifecycle_stage": "生命周期", "follow_type": "跟进方式",
                    "content": "跟进内容", "plan_time": "计划时间",
                    "status": "状态", "created_at": "创建时间",
                }
                df_export = df_export.rename(columns=col_rename)
            else:
                df_export = pd.DataFrame()
                st.info("暂无跟进记录可导出。")

        elif export_type == "课时包数据":
            packages = get_course_packages(teacher=TEACHER)
            if packages:
                df_export = pd.DataFrame(packages)
                col_rename = {
                    "id": "ID", "customer_id": "客户ID", "customer_name": "客户姓名",
                    "lifecycle_stage": "生命周期", "package_name": "课时包名称",
                    "total_hours": "到手课时（节）", "used_hours": "已消耗课时",
                    "remaining_hours": "剩余课时", "purchase_date": "购买日期",
                    "expiry_date": "到期日期", "original_price": "原价（元）",
                    "discount_amount": "优惠价格（元）", "price": "实收价格（元）",
                    "unit_price": "实际单课时价格（元/节）",
                    "status": "状态", "notes": "备注",
                    "created_at": "创建时间", "updated_at": "更新时间",
                }
                df_export = df_export.rename(columns=col_rename)
                col_order = ["ID", "客户ID", "客户姓名", "生命周期", "课时包名称",
                             "到手课时（节）", "已消耗课时", "剩余课时", "购买日期", "到期日期",
                             "原价（元）", "优惠价格（元）", "实收价格（元）", "实际单课时价格（元/节）",
                             "状态", "备注", "创建时间", "更新时间"]
                df_export = df_export[[c for c in col_order if c in df_export.columns]]
            else:
                df_export = pd.DataFrame()
                st.info("暂无课时包数据可导出。")

        else:  # 消课记录
            records = get_course_records(teacher=TEACHER)
            if records:
                df_export = pd.DataFrame(records)
                col_rename = {
                    "id": "ID", "package_id": "课时包ID", "customer_id": "客户ID",
                    "customer_name": "客户姓名", "package_name": "课时包名称",
                    "record_date": "上课日期", "hours_used": "消耗课时",
                    "course_type": "课程类型", "teacher": "授课老师",
                    "notes": "备注", "created_at": "创建时间",
                }
                df_export = df_export.rename(columns=col_rename)
                col_order = ["ID", "课时包ID", "客户ID", "客户姓名", "课时包名称",
                             "上课日期", "消耗课时", "课程类型", "授课老师",
                             "备注", "创建时间"]
                df_export = df_export[[c for c in col_order if c in df_export.columns]]
            else:
                df_export = pd.DataFrame()
                st.info("暂无消课记录可导出。")

        if not df_export.empty:
            st.dataframe(df_export.head(20), use_container_width=True)
            st.caption(f"共 {len(df_export)} 条记录")

            # 生成 Excel 文件
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                df_export.to_excel(writer, index=False, sheet_name=export_type)
                # 调整列宽
                ws = writer.sheets[export_type]
                for col in ws.columns:
                    max_len = max(len(str(cell.value or "")) for cell in col)
                    ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

            output.seek(0)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.download_button(
                label=f"📥 下载 {export_type}.xlsx",
                data=output,
                file_name=f"{export_type}_{timestamp}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )


# ==================== Tab3: 模板下载 ====================
if "tab_io_template" in tab_map:
    with tab_map["tab_io_template"]:
        st.subheader("📋 下载导入模板")

        if not can("action_io_template"):
            st.stop()

        st.markdown("""
        使用模板文件可以确保导入格式正确，避免导入失败。

        **模板说明：**
        - `姓名`、`年级` 列必填
        - `年级` 可选：一年级、二年级、三年级、四年级、五年级、六年级、初一、初二、初三、高一、高二、高三
        - `手机号`、`微信号`、`学校`、`备注` 选填
        - `来源渠道` 可选：自然流量、转介绍、地推活动、线上广告、社群引流、其他
        - `客户意向（苹果）` 可选：🍎 红苹果、🍏 青苹果、🪲 坏苹果
        - `生命周期阶段` 可选：新线索、已加企微、预约试听、到访、已试听未成交、在读、待续费、流失
        """)

        # 生成模板
        template_data = {
            "姓名": ["张三", "李四"],
            "手机号": ["13800138000", "13900139000"],
            "微信号": ["zhangsan_wx", ""],
            "来源渠道": ["自然流量", "转介绍"],
            "客户意向（苹果）": ["🍎 红苹果", "🍏 青苹果"],
            "生命周期阶段": ["新线索", "已加企微"],
            "学校": ["第一中学", "实验学校"],
            "年级": ["初三", "高二"],
            "备注": ["家长意向明确", "需要进一步跟进"],
        }
        df_template = pd.DataFrame(template_data)

        output_template = io.BytesIO()
        with pd.ExcelWriter(output_template, engine="openpyxl") as writer:
            df_template.to_excel(writer, index=False, sheet_name="客户导入模板")
            ws = writer.sheets["客户导入模板"]
            # 调整列宽
            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                ws.column_dimensions[col[0].column_letter].width = max(max_len + 4, 16)

        output_template.seek(0)

        st.download_button(
            label="📥 下载客户导入模板.xlsx",
            data=output_template,
            file_name="客户导入模板.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

        st.markdown("---")
        st.caption("💡 下载模板后，请按照模板格式填写数据，再通过「导入客户」页签上传。")
