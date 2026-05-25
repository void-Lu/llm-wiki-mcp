---
type: object
object_type: workflow
project: project-a
object_id: customworkflow_example
related_objects: [salesorder]
related_scripts: [customscript_example_ue]
status: active
tags: [netsuite, workflow]
---

# Workflow - Example Workflow

## 关联需求
- 禅道: []

## 业务目的
说明该工作流支撑的业务场景：审批流程、状态流转、自动通知。

## 工作流状态
| 状态 | 说明 |
|---|---|
| Pending Review | 待审核 |
| Approved | 已批准 |
| Rejected | 已拒绝 |

## 关键转场
| 从状态 | 到状态 | 触发条件 | 动作 |
|---|---|---|---|
| Pending Review | Approved | 审批通过 | 发送通知 |
| Pending Review | Rejected | 审批拒绝 | 记录原因 |

## 使用位置
- SalesOrder 记录类型
- 触发脚本: `customscript_example_ue`

## 维护注意事项
- 修改状态流转前确认脚本依赖
- 工作流权限需配合角色设置

## 排坑记录
- 暂无