---
type: script
script_type: suitelet
project: project-a
author: developer-name
script_id: customscript_example_suitelet
script_name: Example Suitelet
deployment_id: customdeploy_example_suitelet
related_objects: [salesorder]
related_scripts: []
status: active
tags: [netsuite, suitelet]
---

# Suitelet - Example Suitelet

## 关联需求
- 禅道: []

## 用途
说明 Suitelet 解决的业务问题：生成自定义页面或处理表单提交。

## 入口函数
| 函数 | 说明 |
|---|---|
| onRequest | 处理 GET/POST 请求的核心入口 |

## 请求参数
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| example | string | 是 | 示例参数 |

## 核心逻辑
1. 判断请求方法（GET/POST）。
2. GET: 构建 Suitelet 页面表单。
3. POST: 处理表单提交数据。
4. 返回响应或重定向。

## 部署配置
- Script ID: `customscript_example_suitelet`
- Deployment ID: `customdeploy_example_suitelet`

## 相关脚本
- 无

## 排坑记录
- 暂无