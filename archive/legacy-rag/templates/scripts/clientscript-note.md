---
type: script
script_type: clientscript
project: project-a
author: developer-name
script_id: customscript_example_cs
script_name: Example Client Script
deployment_id: customdeploy_example_cs
related_objects: [salesorder]
related_scripts: []
status: active
tags: [netsuite, clientscript]
---

# Client Script - Example Client Script

## 关联需求
- 禅道: []

## 用途
说明 Client Script 解决的业务问题：在浏览器端对表单进行动态交互和校验。

## 入口函数
| 函数 | 说明 |
|---|---|
| pageInit | 页面加载初始化 |
| fieldChanged | 字段值变更时触发 |
| postSourcing | 字段源数据加载后触发 |
| sublistChanged | 子列表行变更时触发 |
| lineInit | 子列表行初始化 |
| validateField | 字段级校验 |
| validateLine | 子列表行级校验 |
| validateInsert | 子列表插入校验 |
| validateDelete | 子列表删除校验 |
| saveRecord | 保存前校验 |

## 核心逻辑
1. pageInit: 设置初始状态与默认值。
2. fieldChanged: 根据字段变更动态联动其他字段。
3. saveRecord: 提交前综合校验。

## 部署配置
- Script ID: `customscript_example_cs`
- Deployment ID: `customdeploy_example_cs`

## 关联对象
- salesorder

## 相关脚本
- 无

## 注意事项
- ⚠️ Client Script 在浏览器端运行，无法使用服务端 API（N/record 等）
- ⚠️ 避免在 pageInit 中执行耗时操作

## 排坑记录
- 暂无