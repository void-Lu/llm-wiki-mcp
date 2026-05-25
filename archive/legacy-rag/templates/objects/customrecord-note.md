---
type: object
object_type: customrecord
project: project-a
object_id: customrecord_example
related_objects: [salesorder]
related_scripts: [customscript_example_ue]
status: active
tags: [netsuite, customrecord]
---

# Custom Record - Example Custom Record

## 关联需求
- 禅道: []

## 业务目的
说明该自定义记录支撑的业务场景：扩展数据模型、存储配置。

## 关键字段
| 字段 | 类型 | 说明 |
|---|---|---|
| name | Text | 记录名称 |
| custrecord_status | List | 状态 |

## 使用位置
- UserEvent 脚本: `customscript_example_ue` 在 afterSubmit 中查询
- Suitelet 页面: 表格展示

## 维护注意事项
- 修改字段类型前确认是否有脚本依赖
- 自定义记录权限需单独配置

## 排坑记录
- 暂无