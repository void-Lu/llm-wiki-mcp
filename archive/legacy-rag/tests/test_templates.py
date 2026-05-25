from pathlib import Path


def read_template(name: str) -> str:
    return Path("templates", name).read_text(encoding="utf-8")


def test_script_template_contains_required_fields():
    text = read_template("scripts/default-script-note.md")

    for field in [
        "type: script",
        "project:",
        "author:",
        "script_type:",
        "script_id:",
        "deployment_id:",
        "related_objects:",
        "related_scripts:",
        "status:",
        "tags:",
        "## 关联需求",
        "## 相关脚本",
        "## 排坑记录",
    ]:
        assert field in text


def test_object_template_contains_required_fields():
    text = read_template("objects/default-object-note.md")

    for field in [
        "type: object",
        "project:",
        "object_type:",
        "related_objects:",
        "status:",
        "tags:",
        "## 关联需求",
        "## 业务目的",
        "## 使用位置",
    ]:
        assert field in text


def test_requirement_template_contains_required_fields():
    text = read_template("requirement-note.md")

    for field in [
        "type: requirement",
        "project:",
        "zentao_urls:",
        "related_objects:",
        "related_scripts:",
        "related_objects:",
        "status:",
        "tags:",
        "## 禅道链接",
        "## 业务背景",
        "## 验收标准",
    ]:
        assert field in text


def test_troubleshooting_template_contains_required_fields():
    text = read_template("troubleshooting-note.md")

    for field in [
        "type: troubleshooting",
        "project:",
        "author:",
        "related_objects:",
        "related_scripts:",
        "status:",
        "tags:",
        "## 现象",
        "## 排查过程",
        "## 根因",
        "## 解决方案",
    ]:
        assert field in text


def test_decision_template_contains_required_fields():
    text = read_template("decision-note.md")

    for field in [
        "type: decision",
        "project:",
        "author:",
        "decision_status:",
        "related_objects:",
        "related_scripts:",
        "decision_date:",
        "tags:",
        "## 背景",
        "## 可选方案",
        "## 决策",
        "## 理由",
    ]:
        assert field in text


def test_knowledge_template_contains_required_fields():
    text = read_template("knowledge-note.md")

    for field in [
        "type: knowledge",
        "topic:",
        "author:",
        "related_objects:",
        "related_script_types:",
        "tags:",
        "## 适用场景",
        "## 推荐做法",
        "## 常见问题",
    ]:
        assert field in text