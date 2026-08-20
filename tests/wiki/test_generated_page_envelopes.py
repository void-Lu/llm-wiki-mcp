from __future__ import annotations

from pathlib import Path

from tests.helpers import write_test_page
from wiki.wiki_index import refresh_navigation
from wiki.wiki_overview import refresh_overview
from wiki.wiki_paths import create_wiki_root


# G4 基线字节样本：覆盖顶层导航、列表导航和概览三种生成页信封。
EXPECTED_GENERATED_PAGE_BYTES = {
    "wiki/index.md": bytes.fromhex(
        "2d2d2d0a747970653a20696e6465780a67656e6572617465643a20747275650a"
        "2d2d2d0a0a2320496e6465780a0a23232050726f6a656374730a2d205b5b7072"
        "6f6a656374732f616c7068612f696e6465782e6d647c616c7068615d5d0a0a2323"
        "20436f6e63657074730a2d205b5b636f6e63657074732f696e6465782e6d647c43"
        "6f6e63657074735d5d0a0a232320456e7469746965730a2d205b5b656e74697469"
        "65732f696e6465782e6d647c456e7469746965735d5d0a0a232320417263686976"
        "65730a2d205b5b61726368697665732f6c6f672e6d647c4172636869766573204c6f"
        "675d5d0a"
    ),
    "wiki/concepts/index.md": bytes.fromhex(
        "2d2d2d0a747970653a20696e6465780a67656e6572617465643a20747275650a"
        "2d2d2d0a0a2320436f6e63657074730a0a2d205b5b73756974657363726970742f"
        "696e6465782e6d647c73756974657363726970745d5d0a"
    ),
    "wiki/overview.md": bytes.fromhex(
        "2d2d2d0a747970653a206f766572766965770a67656e6572617465643a20747275"
        "650a2d2d2d0a0a23204f766572766965770a0a232320436f756e74730a2d205072"
        "6f6a656374733a20310a2d2047656e6572617465642070616765733a20330a2d20"
        "4d616e75616c2070616765733a20300a0a232320526563656e74204c6f6720456e"
        "74726965730a2d20e697a00a"
    ),
}


def test_generated_page_envelopes_preserve_baseline_utf8_bytes(tmp_path: Path) -> None:
    create_wiki_root(tmp_path)
    write_test_page(tmp_path, "wiki/projects/alpha/specs/spec.md", {"title": "Spec", "generated": True}, "spec")
    write_test_page(tmp_path, "wiki/concepts/suitescript/module.md", {"title": "Module", "generated": True}, "module")
    write_test_page(tmp_path, "wiki/entities/customer/customer.md", {"title": "Customer", "generated": True}, "customer")

    assert refresh_navigation(tmp_path)["ok"] is True
    assert refresh_overview(tmp_path)["ok"] is True

    for relative, expected in EXPECTED_GENERATED_PAGE_BYTES.items():
        actual = (tmp_path / relative).read_bytes()
        assert len(actual) == len(expected)
        assert actual == expected

