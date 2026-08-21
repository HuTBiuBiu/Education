# -*- coding: utf-8 -*-
"""
权限资源目录与内置默认权限
资源分三类：
  page   - 页面访问权限（决定侧边栏是否显示该页面、能否进入）
  tab    - 页面内选项卡的显示权限
  action - 页面内操作权限（新增 / 删除 / 修改 等）

管理员（admin）恒拥有全部权限，不参与配置。
所有角色权限可在「权限管理」页面中调整。
"""

from typing import Dict, List, Set, Tuple


# ---------------- 可配置角色（admin 恒全权，不参与） ----------------
ROLE_ORDER = ["staff", "teacher", "hr", "finance"]


# ---------------- 权限资源目录（按页面分组，供权限管理页与守卫使用） ----------------
# 每项为 (资源key, 显示名)
PERMISSION_GROUPS: Dict[str, Dict[str, List[Tuple[str, str]]]] = {
    "首页": {
        "pages":   [("page_home", "访问首页")],
        "tabs":    [],
        "actions": [],
    },
    "客户管理": {
        "pages":   [("page_customers", "访问客户管理页面")],
        "tabs":    [
            ("tab_customers_list", "客户列表"),
            ("tab_customers_followups", "跟进记录"),
        ],
        "actions": [
            ("action_customers_add", "新增客户"),
            ("action_customers_edit", "编辑客户"),
            ("action_customers_delete", "删除客户"),
            ("action_customers_follow", "完成跟进"),
            ("action_customers_stage", "生命周期阶段流转"),
            ("action_customers_pkg_add", "转在读登记课时包"),
            ("action_followups_process", "处理待跟进记录（完成/取消）"),
        ],
    },
    "跟进提醒": {
        "pages":   [("page_followup", "访问跟进提醒页面")],
        "tabs":    [],
        "actions": [
            ("action_followups_add", "添加跟进任务"),
        ],
    },
    "课时管理": {
        "pages":   [("page_hours", "访问课时管理页面")],
        "tabs":    [
            ("tab_hours_overview", "课时总览"),
            ("tab_hours_renewal", "续费倒计时"),
            ("tab_hours_packages", "课时包"),
            ("tab_hours_records", "消课记录"),
            ("tab_hours_stats", "消课统计"),
        ],
        "actions": [
            ("action_hours_add_rec", "新增消课记录"),
            ("action_hours_del_rec", "删除消课记录"),
            ("action_hours_set_lesson", "设置单课时时长"),
            ("action_hours_pkg_add", "新建课时包"),
            ("action_hours_pkg_import", "批量导入课时包"),
            ("action_hours_pkg_del", "删除课时包"),
        ],
    },
    "导入导出": {
        "pages":   [("page_io", "访问导入导出页面")],
        "tabs":    [
            ("tab_io_import", "导入客户"),
            ("tab_io_export", "导出数据"),
            ("tab_io_template", "模板下载"),
        ],
        "actions": [
            ("action_io_import", "导入客户"),
            ("action_io_export", "导出数据"),
            ("action_io_template", "下载模板"),
        ],
    },
    "班级管理": {
        "pages":   [("page_classes", "访问班级管理页面")],
        "tabs":    [],
        "actions": [
            ("action_classes_create", "新建班级"),
            ("action_classes_add_student", "添加学员"),
            ("action_classes_remove_student", "移除学员"),
            ("action_classes_update_status", "更新班级状态"),
            ("action_classes_delete", "删除班级"),
        ],
    },
    "课表管理": {
        "pages":   [("page_schedules", "访问课表管理页面")],
        "tabs":    [
            ("tab_feedback", "课堂反馈"),
        ],
        "actions": [
            ("action_schedules_create", "新建课表"),
            ("action_schedules_batch", "批量排课"),
            ("action_schedules_import", "导入课表"),
            ("action_schedules_delete", "删除课表"),
            ("action_schedules_feedback", "填写课堂反馈"),
        ],
    },
    "员工管理": {
        "pages":   [("page_staff", "访问员工管理页面")],
        "tabs":    [],
        "actions": [
            ("action_staff_add", "添加员工"),
            ("action_staff_edit_role", "修改员工角色"),
            ("action_staff_reset_pwd", "重置员工密码"),
            ("action_staff_delete", "删除员工账号"),
        ],
    },
    "权限管理": {
        "pages":   [("page_permissions", "访问权限管理页面")],
        "tabs":    [],
        "actions": [],
    },
}

# ---------------- 操作权限 → 所属选项卡 映射 ----------------
# 用于权限管理页按「选项卡 → 其下功能」层级展示与配置；
# value 为 None 表示该操作不隶属于任何选项卡，直接归在页面级操作下。
ACTION_TAB_MAP: Dict[str, str] = {
    "action_customers_add": "tab_customers_list",
    "action_customers_edit": "tab_customers_list",
    "action_customers_delete": "tab_customers_list",
    "action_customers_stage": "tab_customers_list",
    "action_customers_pkg_add": "tab_customers_list",
    "action_customers_follow": "tab_customers_followups",
    "action_followups_process": "tab_customers_followups",
    "action_hours_set_lesson": "tab_hours_renewal",
    "action_hours_pkg_add": "tab_hours_packages",
    "action_hours_pkg_import": "tab_hours_packages",
    "action_hours_pkg_del": "tab_hours_packages",
    "action_hours_add_rec": "tab_hours_records",
    "action_hours_del_rec": "tab_hours_records",
    "action_io_import": "tab_io_import",
    "action_io_export": "tab_io_export",
    "action_io_template": "tab_io_template",
    "action_followups_add": None,
    "action_classes_create": None,
    "action_classes_add_student": None,
    "action_classes_remove_student": None,
    "action_classes_update_status": None,
    "action_classes_delete": None,
    "action_schedules_create": None,
    "action_schedules_batch": None,
    "action_schedules_import": None,
    "action_schedules_delete": None,
    "action_schedules_feedback": "tab_feedback",
    "action_staff_add": None,
    "action_staff_edit_role": None,
    "action_staff_reset_pwd": None,
    "action_staff_delete": None,
}

# ---------------- 扁平化：key -> (类型, 所属页面, 显示名) ----------------
PERMISSION_FLAT: Dict[str, Tuple[str, str, str]] = {}
for _group_name, _group in PERMISSION_GROUPS.items():
    for _kind, _items in (
        ("page", _group["pages"]),
        ("tab", _group["tabs"]),
        ("action", _group["actions"]),
    ):
        for _key, _label in _items:
            PERMISSION_FLAT[_key] = (_kind, _group_name, _label)


def all_permission_keys() -> List[str]:
    """返回全部权限资源 key"""
    return list(PERMISSION_FLAT.keys())


# ---------------- 内置默认权限 ----------------
# 各角色默认允许的资源集合；未在集合中的资源默认不允许。
# admin 恒为全部权限，无需在此配置。
DEFAULT_PERMISSIONS: Dict[str, Set[str]] = {
    # 学管：只能看自己的客户（数据范围由 auth.get_visible_teacher 控制），
    #       客户相关全部内容可见可操作；课表管理只读（可看所有课程，不可新建/删除）。
    "staff": {
        # 页面
        "page_home", "page_customers", "page_followup",
        "page_hours", "page_io", "page_classes", "page_schedules",
        # 选项卡
        "tab_customers_list", "tab_customers_followups",
        "tab_hours_renewal", "tab_hours_packages", "tab_hours_records", "tab_hours_stats",
        "tab_hours_overview",
        "tab_io_import", "tab_io_export", "tab_io_template",
        # 课堂反馈：可查看自己名下学生的反馈（填写权限需另行授予 action_schedules_feedback）
        "tab_feedback",
        # 操作：客户相关全部
        "action_customers_add", "action_customers_edit", "action_customers_delete",
        "action_customers_follow", "action_customers_stage", "action_customers_pkg_add",
        "action_followups_process",
        "action_followups_add",
        # 课时管理
        "action_hours_add_rec", "action_hours_del_rec",
        "action_hours_pkg_add", "action_hours_pkg_import", "action_hours_pkg_del",
        # 导入导出
        "action_io_import", "action_io_export", "action_io_template",
        # 班级
        "action_classes_create", "action_classes_add_student", "action_classes_remove_student",
        "action_classes_update_status", "action_classes_delete",
        # 课表：只读（无 action_schedules_create / action_schedules_delete）
    },
    # 人事：能看到所有学管客户的跟进记录（数据范围全部）；
    #       具备 创建班级 / 创建课程 / 新建和删除课表 权限；
    #       可进入员工管理页管理员工（但不可操作「管理员」角色账号，见 09_员工管理.py）。
    "hr": {
        # 页面
        "page_home", "page_customers", "page_followup",
        "page_hours", "page_io", "page_classes", "page_schedules",
        "page_staff",
        # 选项卡
        "tab_customers_list", "tab_customers_followups",
        "tab_hours_renewal", "tab_hours_packages", "tab_hours_records", "tab_hours_stats",
        "tab_hours_overview",
        "tab_io_import", "tab_io_export", "tab_io_template",
        # 操作：创建班级 / 新建与删除课表 / 导出 / 员工管理
        "action_classes_create",
        "action_schedules_create", "action_schedules_batch", "action_schedules_import",
        "action_schedules_delete",
        "action_io_export",
        "action_staff_add", "action_staff_edit_role", "action_staff_reset_pwd",
        "action_staff_delete",
    },
    # 教师：只能看到课表选项卡（含课堂反馈），且数据范围仅限本人授课课表（auth.get_viewer_scope）。
    "teacher": {
        "page_schedules",
        "tab_feedback",
        "action_schedules_feedback",
    },
    # 财务：课时 / 导入导出 / 客户只读
    "finance": {
        "page_home", "page_hours", "page_io", "page_customers",
        "tab_hours_renewal", "tab_hours_packages", "tab_hours_records", "tab_hours_stats",
        "tab_hours_overview",
        "tab_io_import", "tab_io_export", "tab_io_template",
        "action_io_export",
    },
}
