"""从明日方舟 Wiki 公招页面快照（template-html.html）生成结构化干员数据。

用法：
    python tools/build_recruit_data.py [源 html 路径] [-o 输出 json 路径]

页面结构（biligame 公招计算页）：
    <div class="contentDetail" data-param1="职业, 性别, 星级, 资深干员, 高级资深干员, 阵营..., 词条...">
        <p class="picture"><a ...><img ...></a><span class="picText">干员名</span></p>
        <p class="tags"><span class="btn btn-default tagText"> 近战位</span>...</p>
    </div>

其中 ``<p class="tags">`` 只列出「特性标签 + 部署位置」，职业与资历需要从
``data-param1`` 补齐，因此本脚本会合并两边得到「招募词条全集」。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import re
from pathlib import Path

# 公招界面的词条分组（顺序与游戏内一致，见 template-html.html 的 table 部分）
TAG_GROUPS: dict[str, list[str]] = {
    "职业": ["近卫", "狙击", "重装", "医疗", "辅助", "术师", "特种", "先锋"],
    "部署位置": ["近战位", "远程位"],
    "特性标签": [
        "控场",
        "爆发",
        "治疗",
        "支援",
        "费用回复",
        "输出",
        "生存",
        "群攻",
        "防护",
        "减速",
        "削弱",
        "快速复活",
        "位移",
        "召唤",
        "支援机械",
        "元素",
    ],
    "干员资历": ["新手", "资深干员", "高级资深干员"],
}

ALL_TAGS: list[str] = [tag for group in TAG_GROUPS.values() for tag in group]
CLASS_TAGS = set(TAG_GROUPS["职业"])

_BLOCK_SPLIT = re.compile(r'<div class="contentDetail"')
_PARAM1_RE = re.compile(r'data-param1="([^"]*)"')
_NAME_RE = re.compile(r'<span class="picText">([^<]*)</span>')
_TAG_RE = re.compile(r'class="btn btn-default tagText">\s*([^<]+?)\s*</span>')
_POSITION_RE = re.compile(r"(近战位|远程位)")


def parse_operators(html: str) -> list[dict]:
    """解析页面中的所有干员条目。"""

    operators: list[dict] = []
    seen: set[str] = set()

    for block in _BLOCK_SPLIT.split(html)[1:]:
        param_match = _PARAM1_RE.search(block)
        name_match = _NAME_RE.search(block)
        if not param_match or not name_match:
            continue

        name = name_match.group(1).strip()
        if not name or name in seen:
            continue
        seen.add(name)

        fields = [field.strip() for field in param_match.group(1).split(",")]
        if len(fields) < 3:
            continue
        try:
            star = int(fields[2])
        except ValueError:
            continue

        klass = fields[0]
        raw_tags = [tag.strip() for tag in _TAG_RE.findall(block)]

        # 特性标签里偶尔混入职业名（属于 Wiki 数据噪音），以 data-param1 的职业为准
        traits = [tag for tag in raw_tags if tag in ALL_TAGS and tag not in CLASS_TAGS]
        # 支援机械等标签可能重复出现，去重保序
        traits = list(dict.fromkeys(traits))

        tags: list[str] = []
        if klass in CLASS_TAGS:
            tags.append(klass)
        tags.extend(traits)

        # 部署位置：优先取特性标签里的，其次回落到 data-param1 的整行文本
        if not any(tag in ("近战位", "远程位") for tag in tags):
            pos = _POSITION_RE.search(param_match.group(1))
            if pos:
                tags.append(pos.group(1))

        # 资历词条（新手/资深干员/高级资深干员）不写进 tags，由插件按星级处理
        operators.append(
            {
                "name": name,
                "star": star,
                "class": klass,
                "tags": tags,
            }
        )

    operators.sort(key=lambda item: (-item["star"], item["name"]))
    return operators


def build(source: Path, output: Path) -> dict:
    html = io.open(source, encoding="utf-8").read()
    operators = parse_operators(html)
    if not operators:
        raise SystemExit(f"未能从 {source} 解析出任何干员，请检查页面结构是否变化")

    data = {
        "source": source.name,
        "generated_at": _dt.date.today().isoformat(),
        "tag_groups": TAG_GROUPS,
        "all_tags": ALL_TAGS,
        "operators": operators,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="生成公招干员数据")
    parser.add_argument(
        "source",
        nargs="?",
        default=str(Path(__file__).resolve().parents[2] / "template-html.html"),
        help="Wiki 公招页面 html 快照路径",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "data" / "recruit_data.json"),
        help="输出 json 路径",
    )
    args = parser.parse_args()

    data = build(Path(args.source), Path(args.output))
    operators = data["operators"]

    stars: dict[int, int] = {}
    for op in operators:
        stars[op["star"]] = stars.get(op["star"], 0) + 1

    print(f"已写入 {args.output}")
    print(f"干员总数 {len(operators)}，星级分布 " + " ".join(
        f"{star}★×{stars[star]}" for star in sorted(stars, reverse=True)
    ))
    missing = [op["name"] for op in operators if not any(t in CLASS_TAGS for t in op["tags"])]
    if missing:
        print("缺少职业词条的干员：" + "、".join(missing))


if __name__ == "__main__":
    main()
