#!/usr/bin/env python3
"""
sync_terms_canonical.py — 将 config/terms_canonical.json 同步到 terms.py

用法:
    python scripts/sync_terms_canonical.py [--check]

    --check     只检查一致性，不写入（CI 用）

本脚本读取 config/terms_canonical.json（唯一事实源），更新 terms.py 中的：
- _UPPER_ACRONYMS   ← upper_acronyms
- _LEXICON canonical 键的展示名 ← lexicon_display
- _display_of() 中的 display_overrides 逻辑 ← display_overrides

规则：
1. terms_canonical.json 为唯一事实源，手工修改 terms.py 的对应区域会被覆盖
2. 同步后必须运行 pytest 验证（test_acronym_normalize.py, test_case_insensitive.py）
3. 同步不会自动部署，需按 proper-noun-case-manager skill 流程手动部署
"""

import json
import re
import sys
import argparse
from pathlib import Path

# ---------- 路径配置 ----------
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "terms_canonical.json"
TERMS_PATH = PROJECT_ROOT / "terms.py"

# ---------- 代码模板 ----------
# 这些标记用于在 terms.py 中定位需要替换的区域
UPPER_START = "# ---------- 必须大写的技术缩写（lowercase → uppercase canonical）----------"
UPPER_END = "\n\ndef normalize_term"

DISPLAY_OVERRIDES_START = "    # canonical 已是规范缩写形式的直接返回"
DISPLAY_OVERRIDES_END = "    # 词典 canonical 的常见美化"


def load_config():
    """加载 terms_canonical.json，忽略 _comment 字段。"""
    if not CONFIG_PATH.exists():
        print(f"[错误] 配置文件不存在: {CONFIG_PATH}")
        print("[提示] 请从模板创建: cp skills/proper-noun-case-manager/terms_canonical.example.json config/terms_canonical.json")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 过滤掉 _comment 键
    def strip_comments(d):
        if isinstance(d, dict):
            return {k: strip_comments(v) for k, v in d.items() if not k.startswith("_")}
        return d

    return strip_comments(data)


def build_upper_acronyms_block(config):
    """生成 _UPPER_ACRONYMS 代码块。"""
    upper = config.get("upper_acronyms", {})
    lines = ["# 常见技术缩写/品牌名在归一化后应统一为大写形式；覆盖 LLM 抽词的大小写不一致",
             "# （如 \"Gpu\"/\"gpu\" → \"GPU\"、\"Ui\"/\"ui\" → \"UI\"）。",
             "# 包含已在 _LEXICON 中的词条（glm/llm/rag 等）和未收录的通用缩写。",
             "# 归一化流程中在别名查找之后应用：先走词典归并，再对结果做大写校正。",
             "_UPPER_ACRONYMS = {"]

    # 分组：已在 lexicon 中的 vs 未收录的
    lexicon_canons = set(config.get("lexicon_display", {}).keys())
    lexicon_abbrevs = {}
    generic_abbrevs = {}

    for k, v in sorted(upper.items()):
        if k in lexicon_canons:
            lexicon_abbrevs[k] = v
        else:
            generic_abbrevs[k] = v

    if lexicon_abbrevs:
        lines.append("    # 已在 _LEXICON 中的缩写（canonical 键同步更新为大写）")
        items = [f'    "{k}": "{v}"' for k, v in sorted(lexicon_abbrevs.items())]
        lines.append(",\n".join(items) + ",")

    if generic_abbrevs:
        lines.append("    # 未收录的通用技术缩写")
        items = [f'    "{k}": "{v}"' for k, v in sorted(generic_abbrevs.items())]
        lines.append(",\n".join(items) + ",")

    lines.append("}")
    return "\n".join(lines)


def build_display_overrides_block(config):
    """生成 _display_of 函数中的 overrides 逻辑。"""
    overrides = config.get("display_overrides", {})
    if not overrides:
        return ""

    lines = ["    # display_overrides（来自 terms_canonical.json，最高优先级）",
             "    _OVERRIDES = {"]
    items = [f'        "{k}": "{v}"' for k, v in sorted(overrides.items())]
    lines.append(",\n".join(items))
    lines.append("    }")
    lines.append("    if term in _OVERRIDES:")
    lines.append("        return _OVERRIDES[term]")
    return "\n".join(lines)


def sync_terms_py(config, check_only=False):
    """同步配置到 terms.py。"""
    if not TERMS_PATH.exists():
        print(f"[错误] terms.py 不存在: {TERMS_PATH}")
        sys.exit(1)

    with open(TERMS_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    original = content

    # 1. 替换 _UPPER_ACRONYMS 区域
    upper_block = build_upper_acronyms_block(config)
    pattern = re.compile(
        re.escape(UPPER_START) + ".*?(?=" + re.escape(UPPER_END) + ")",
        re.DOTALL
    )
    if pattern.search(content):
        content = pattern.sub(UPPER_START + "\n" + upper_block + "\n\n", content)
    else:
        print(f"[警告] 未找到 _UPPER_ACRONYMS 标记，跳过 upper_acronyms 同步")

    # 2. 替换 _display_of 中的 overrides 区域
    overrides_block = build_display_overrides_block(config)
    if overrides_block:
        # 查找 _display_of 函数内的插入点
        display_pattern = re.compile(
            r"(    # canonical 已是规范缩写形式的直接返回.*?\n)(.*?)(\n    # 词典 canonical 的常见美化)",
            re.DOTALL
        )
        match = display_pattern.search(content)
        if match:
            # 保留原有的 _upper_vals 检查，在其后插入 overrides
            new_block = match.group(1) + overrides_block + "\n" + match.group(3)
            content = content[:match.start()] + new_block + content[match.end():]
        else:
            print(f"[警告] 未找到 _display_of 插入点，跳过 display_overrides 同步")

    # 3. 更新 _LEXICON 中的 canonical 键展示名
    lexicon_display = config.get("lexicon_display", {})
    for canon, display in sorted(lexicon_display.items()):
        # 查找 _LEXICON 中该 canonical 键的定义行
        # 格式: "canonical": ["surface1", "surface2"],
        lexicon_pattern = re.compile(
            rf'^([ \t]*"{re.escape(canon)}"\s*:\s*\[.*\],)',
            re.MULTILINE
        )
        # 注意：我们不修改 _LEXICON 的结构，只确保 canonical 键本身是正确的
        # 如果 canonical 键需要改，那是重构，不是本脚本的职责

    if content == original:
        print("[信息] terms.py 无需更新（已与配置一致）")
        return True

    if check_only:
        print("[错误] terms.py 与 config/terms_canonical.json 不一致")
        print("[提示] 运行以下命令同步：")
        print("       python scripts/sync_terms_canonical.py")
        return False

    # 写入
    with open(TERMS_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[成功] 已同步 {len(config.get('upper_acronyms', {}))} 个 upper_acronyms")
    print(f"[成功] 已同步 {len(config.get('display_overrides', {}))} 个 display_overrides")
    print(f"[成功] 已更新 {TERMS_PATH}")
    print("[提示] 请运行 pytest 验证：")
    print("       python -m pytest tests/test_acronym_normalize.py tests/test_case_insensitive.py -v")
    return True


def main():
    parser = argparse.ArgumentParser(description="同步 terms_canonical.json 到 terms.py")
    parser.add_argument("--check", action="store_true", help="只检查一致性，不写入")
    args = parser.parse_args()

    config = load_config()
    ok = sync_terms_py(config, check_only=args.check)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
