---
type: object
object_type: savedsearch
project: project-a
object_id: customsearch_example
related_objects: [salesorder]
related_scripts: [customscript_example_ue]
status: active
tags: [netsuite, savedsearch]
---

# Saved Search - Example Saved Search

## 关联需求
- 禅道: []

## 业务目的
说明该搜索支撑的业务场景：报表、数据提取或触发条件。

## 搜索条件
| 字段 | 运算符 | 值 |
|---|---|---|
| status | is | Pending |

## 结果列
| 字段 | 说明 |
|---|---|
| internalid | 记录内部 ID |
| tranid | 交易编号 |

## 使用位置
- UserEvent 脚本: `customscript_example_ue` 在 afterSubmit 中查询
- 仪表板: 每日待处理订单

## 维护注意事项
- 修改搜索条件前确认下游脚本依赖
- 谨慎调整结果列影响面

## 排坑记录
- 暂无