"""明日方舟公开招募：词条识别、组合计算与结果排版。

本模块与 AstrBot 完全解耦，可单独用脚本测试：

    python -c "from recruit_core import *; db = load_database(); print(render_report(db, ['输出','生存','近战位']))"

机制说明（按 9:00 时长计算，不考虑时长对星级下限的影响）：
- 候选池 = 同时拥有全部所选词条的干员；
- 只有选中「高级资深干员」才只在 6★ 里筛、选中「资深干员」才只在 5★ 里筛，
  这两个词条是稳定出最高星的唯一来源（结论里只提「选择高级资深干员 / 资深干员」）；
- 未选资历词条时 5★ 干员仍可能在池中，但只是「小概率出 5★」，不写“必出”；
- 池中最低星级即该组合的保底星级（池内没有 3★ 时即保底 4★）。
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "TAG_GROUPS",
    "ALL_TAGS",
    "SENIOR_TAG",
    "VETERAN_TAG",
    "DEFAULT_STAR_FLOOR",
    "Operator",
    "Combo",
    "RecruitDatabase",
    "load_database",
    "extract_tags",
    "render_report",
    "render_pool",
    "render_star_pool",
    "render_tag_catalog",
]

# ---------------------------------------------------------------------------
# 词条表（与游戏内公招界面一致；数据文件缺失时作为兜底）
# ---------------------------------------------------------------------------
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

SENIOR_TAG = "高级资深干员"
VETERAN_TAG = "资深干员"

# 「有资深/高资」时优先按最高稀有度出，二者同时选中时以高资为准
STAR_TAGS = (SENIOR_TAG, VETERAN_TAG)

# 9:00 招募的星级下限：低于该星级的干员（1★ 小车 / 2★ 新手）拉满时长也出不到，
# 因此计算时把它们排除出候选池；1 小时招募时可配成 1。
DEFAULT_STAR_FLOOR = 3

# 截图文字 / 模型输出里常见的写法差异，统一到标准词条
TAG_ALIASES: dict[str, str] = {
    "高级资深": SENIOR_TAG,
    "高资": SENIOR_TAG,
    "资深": VETERAN_TAG,
    "资深干员": VETERAN_TAG,
    "近战": "近战位",
    "远程": "远程位",
    "远程位干员": "远程位",
    "近战位干员": "近战位",
    "费用恢复": "费用回复",
    "回复费用": "费用回复",
    "回费": "费用回复",
    "术士": "术师",
    "术师干员": "术师",
    "群伤": "群攻",
    "群攻输出": "群攻",
    "快速复生": "快速复活",
    "复活": "快速复活",
    "辅助干员": "辅助",
    "医疗干员": "医疗",
    "先锋干员": "先锋",
    "近卫干员": "近卫",
    "重装干员": "重装",
    "特种干员": "特种",
    "狙击干员": "狙击",
    "奶妈": "治疗",
    "奶": "治疗",
    "元素损伤": "元素",
    "机械": "支援机械",
    "车辆": "支援机械",
    "小车": "支援机械",
}

# 组合分档：数字越小越优先
TIER_SIX = 0
TIER_FIVE_SENIOR = 1
# 池内有 5★ 且池内没有 3★：保底 4★ 的同时小概率出 5★，单独成一档
TIER_FIVE_FOUR = 2
TIER_FOUR_GUARANTEED = 3
TIER_FIVE_CHANCE = 4
TIER_FOUR_CHANCE = 5
TIER_LOW = 6

# 展示用的档位标题。TIER_LOW（仅 3★）不展示：9 小时本来保底 3★，写出来没有信息量
TIER_TITLES: dict[int, str] = {
    TIER_SIX: "必出 6★",
    TIER_FIVE_SENIOR: "必出 5★",
    TIER_FIVE_FOUR: "保底 4★+极小概率 5★",
    TIER_FOUR_GUARANTEED: "保底 4★",
    TIER_FIVE_CHANCE: "小概率 5★",
    TIER_FOUR_CHANCE: "小概率 4★",
}

_SENTENCE_SPLIT_RE = re.compile(r"[\s,，、/|｜;；:：·•\-—+＋()（）\[\]【】\"'“”]+")
_NUMBER_PREFIX_RE = re.compile(r"^\s*[0-9０-９]+[.、)）:：]?\s*")

# 「高级资深干员」优先抓取名单（来源：公开招募高资词条推荐 #11）。
# 干员 → 推荐组合：均省略了「高级资深干员」，多个等价组合用 | 隔开；
# 只有当前词条能凑出这些组合之一时才提示（光靠「高资」35 人池去赌不算）。
# 推荐抓取 / 随缘抓取不做标记。
_PRIORITY_SIX_RAW: dict[str, str] = {
    "水月": "特种+控场|特种+近战位|近战位+控场|特种|输出+控场",
    "风笛": "先锋|费用回复",
    "史尔特尔": "近卫+输出|近战位+输出|近战位|输出",
    "塞雷娅": "重装+支援|防护+支援|重装+治疗|近战位+治疗|防护+治疗|近战位+支援|治疗|重装",
    "空弦": "狙击|远程位+输出",
    "刻俄柏": "术师+输出|术师+控场|远程位+控场|输出+控场",
    "夜莺": "医疗+支援|医疗|远程位+治疗|治疗+支援|治疗",
    "凯尔希": "召唤|医疗|远程位+治疗|治疗+支援|治疗",
    "铃兰": "辅助|减速|输出+支援|远程位+支援",
    "温蒂": "位移|特种+控场|特种+近战位|近战位+控场|特种|输出+控场",
}

PRIORITY_SIX: dict[str, tuple[frozenset, ...]] = {
    name: tuple(frozenset(part.split("+")) for part in raw.split("|"))
    for name, raw in _PRIORITY_SIX_RAW.items()
}

# 四星里值得单独提一句的干员：只有候选池够小（出率高）时才提示，
# 其他五星 / 四星一律不给具体干员推荐。
TIP_FOUR_OPERATOR = "桃金娘"
TIP_FOUR_MIN_RATE = 0.5

# 只勾中一个资历词条时，整份报告就剩这个池，单独拆一条消息发，标题直接用池名
STAR_POOL_TITLES: dict[str, str] = {
    SENIOR_TAG: "高级资深干员池",
    VETERAN_TAG: "资深干员池",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Operator:
    """一名公招干员。tags 只含职业 / 部署位置 / 特性词条，资历按星级处理。"""

    name: str
    star: int
    klass: str
    tags: frozenset

    def has_all(self, tags) -> bool:
        return all(tag in self.tags for tag in tags)


@dataclass
class Combo:
    """一种词条组合及其计算结果。"""

    tags: tuple
    operators: tuple
    guaranteed: int
    tier: int
    senior: bool = False
    veteran: bool = False

    @property
    def max_star(self) -> int:
        return max((op.star for op in self.operators), default=0)

    @property
    def label(self) -> str:
        return "+".join(self.tags) if self.tags else "（不选词条）"

    def names(self, star: int | None = None) -> list[str]:
        ops = [op for op in self.operators if star is None or op.star == star]
        ops.sort(key=lambda op: (-op.star, op.name))
        return [op.name for op in ops]


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
class RecruitDatabase:
    """公招干员库。"""

    def __init__(self, operators: list[Operator], data: dict | None = None):
        self.operators = tuple(operators)
        self.data = data or {}
        self.tag_groups: dict[str, list[str]] = self.data.get("tag_groups") or TAG_GROUPS
        self.all_tags: list[str] = self.data.get("all_tags") or ALL_TAGS
        self.generated_at: str = str(self.data.get("generated_at") or "")
        self.by_star: dict[int, list[Operator]] = {}
        for op in self.operators:
            self.by_star.setdefault(op.star, []).append(op)

    # -- 查询 ---------------------------------------------------------
    def search(self, tags, stars: tuple[int, ...] | None = None) -> list[Operator]:
        """返回同时拥有全部 tags 的干员（按星级从高到低）。"""

        tags = tuple(tags)
        if stars is None:
            pool = list(self.operators)
        else:
            pool = [op for star in stars for op in self.by_star.get(star, ())]
        result = [op for op in pool if op.has_all(tags)]
        result.sort(key=lambda op: (-op.star, op.name))
        return result

    # -- 组合计算 -----------------------------------------------------
    def evaluate(self, tags, star_floor: int = DEFAULT_STAR_FLOOR) -> Combo | None:
        """计算一种词条组合；候选池为空时返回 None。

        ``star_floor`` 是招募时长对应的星级下限（9:00 → 3），低于它的干员
        在本时长下不可能出现，直接从候选池排除。
        """

        ordered = tuple(sorted(set(tags), key=self._tag_order))
        senior = SENIOR_TAG in ordered
        veteran = VETERAN_TAG in ordered
        if senior and veteran:
            # 高资与资深同时选中时优先最高稀有度，资深词条不再单独展示
            ordered = tuple(tag for tag in ordered if tag != VETERAN_TAG)
        base = tuple(tag for tag in ordered if tag not in STAR_TAGS)
        floor = max(1, int(star_floor))

        if senior:
            # 高资 + 其它词条：只在 6★ 里筛
            pool = self.search(base, stars=(6,))
            guaranteed = 6
        elif veteran:
            pool = self.search(base, stars=(5,))
            guaranteed = 5
        else:
            stars = tuple(star for star in range(floor, 6))
            pool = self.search(base, stars=stars)
            guaranteed = min((op.star for op in pool), default=0)

        if not pool:
            return None

        tier = self._tier(guaranteed, pool, senior=senior, veteran=veteran)
        return Combo(
            tags=ordered,
            operators=tuple(pool),
            guaranteed=guaranteed,
            tier=tier,
            senior=senior,
            veteran=veteran,
        )

    def analyze(
        self,
        tags,
        max_tags: int = 3,
        star_floor: int = DEFAULT_STAR_FLOOR,
    ) -> list[Combo]:
        """枚举 1~max_tags 个词条的全部组合，去重并按优先级排序。"""

        tags = list(dict.fromkeys(tags))
        combos: list[Combo] = []
        for size in range(1, max(1, min(max_tags, len(tags))) + 1):
            for subset in _combinations(tags, size):
                combo = self.evaluate(subset, star_floor=star_floor)
                if combo is not None:
                    combos.append(combo)
        return sort_combos(dedupe_combos(combos))

    def _tag_order(self, tag: str) -> tuple[int, int]:
        """词条排序：职业 → 位置 → 特性 → 资历，保证输出稳定。"""

        for index, group in enumerate(self.tag_groups.values()):
            for position, name in enumerate(group):
                if name == tag:
                    return (index, position)
        return (len(self.tag_groups), 0)

    @staticmethod
    def _tier(guaranteed: int, pool, *, senior: bool, veteran: bool) -> int:
        if senior:
            return TIER_SIX
        if veteran:
            return TIER_FIVE_SENIOR
        # 没有高级资深干员时 5★ 只算概率，哪怕池内全是 5★
        if any(op.star >= 5 for op in pool):
            # 池内同时还有 4★：保底 4★ 与小概率 5★ 同时成立，单独归一档；
            # 池内全是 5★（没 4★）时归入「小概率 5★」
            return TIER_FIVE_FOUR if guaranteed == 4 else TIER_FIVE_CHANCE
        # 池内没有 3★（最低 4★）即保底 4★，比含 3★ 的组合优先
        if guaranteed >= 4:
            return TIER_FOUR_GUARANTEED
        if any(op.star >= 4 for op in pool):
            return TIER_FOUR_CHANCE
        return TIER_LOW


def _combinations(items: list, size: int):
    """枚举 items 中所有 size 元组合（保持词条原始顺序）。"""

    result: list[tuple] = []

    def walk(start: int, picked: list) -> None:
        if len(picked) == size:
            result.append(tuple(picked))
            return
        for index in range(start, len(items)):
            picked.append(items[index])
            walk(index + 1, picked)
            picked.pop()

    walk(0, [])
    return result


def dedupe_combos(combos: list[Combo]) -> list[Combo]:
    """候选池完全相同的组合只保留词条最多的那个（信息量最大）。"""

    kept: dict[tuple, Combo] = {}
    for combo in sorted(combos, key=lambda c: (-len(c.tags), c.tier)):
        signature = (combo.tier, tuple(op.name for op in combo.operators))
        current = kept.get(signature)
        if current is None or len(combo.tags) > len(current.tags):
            kept[signature] = combo
    return list(kept.values())


def sort_combos(combos: list[Combo]) -> list[Combo]:
    """按「档位 → 池内最高星级 → 候选人数（越少越精准）→ 词条数」排序。"""

    return sorted(
        combos,
        key=lambda c: (
            c.tier,
            -c.max_star,
            -c.guaranteed,
            len(c.operators),
            -len(c.tags),
            c.label,
        ),
    )


def is_worth_showing(combo: Combo) -> bool:
    """该组合是否值得出现在报告里。

    - 仅 3★ 的档位不展示；
    - 单词条只有保底 4★ 以上（高资、资深、快速复活这类）才展示，
      近卫、输出这类单词条的信息已经被多词条组合覆盖。
    """

    if combo.tier == TIER_LOW:
        return False
    if len(combo.tags) == 1:
        return combo.guaranteed >= 4
    return True


def load_database(path: str | Path | None = None) -> RecruitDatabase:
    """读取 data/recruit_data.json。"""

    data_path = Path(path) if path else Path(__file__).with_name("data") / "recruit_data.json"
    raw = json.loads(data_path.read_text(encoding="utf-8"))
    operators = [
        Operator(
            name=item["name"],
            star=int(item["star"]),
            klass=item.get("class", ""),
            tags=frozenset(item.get("tags") or ()),
        )
        for item in raw.get("operators", [])
    ]
    if not operators:
        raise ValueError(f"数据文件 {data_path} 中没有干员数据")
    return RecruitDatabase(operators, raw)


# ---------------------------------------------------------------------------
# 词条识别（文本 → 标准词条）
# ---------------------------------------------------------------------------
def _normalize_text(text: str) -> str:
    text = (text or "").strip()
    # 全角英数转半角，去掉零宽字符
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        elif code in (0x200B, 0x200C, 0x200D, 0xFEFF):
            continue
        else:
            out.append(ch)
    return "".join(out)


def extract_tags(text: str, all_tags: list[str] | None = None) -> list[str]:
    """从任意文本（模型输出 / 用户输入）里提取标准词条。

    返回按在原文中出现位置去重排序的列表；无法识别的片段直接丢弃。
    """

    tags = list(all_tags or ALL_TAGS)
    # 长词优先，避免「高级资深干员」被「资深」截断
    ordered = sorted(tags, key=len, reverse=True)
    source = _normalize_text(text)
    pattern = re.compile("|".join(re.escape(tag) for tag in ordered))

    hits: list[tuple[int, str]] = [
        (match.start(), match.group(0)) for match in pattern.finditer(source)
    ]

    # 剩下的文本（含错字、简称）再按标点切开，走别名与模糊匹配
    leftover = pattern.sub(" ", source)
    extras: list[str] = []
    for piece in _SENTENCE_SPLIT_RE.split(leftover):
        piece = _NUMBER_PREFIX_RE.sub("", piece).strip()
        if len(piece) < 2:
            continue
        alias = TAG_ALIASES.get(piece)
        if alias:
            extras.append(alias)
            continue
        for key, value in TAG_ALIASES.items():
            if key in piece:
                extras.append(value)
                break
        else:
            # 「近卫干员」这类词条被 pattern 拆走后会剩下「干员」两字，
            # 对两字片段做模糊匹配会把界面上的后缀误认成「资深干员」，直接丢弃。
            if len(piece) < 3:
                continue
            # 0.6 能把「近站位」这类 3 字错字拉回「近战位」，又不会把界面噪声拉成词条
            match = difflib.get_close_matches(piece, ordered, n=1, cutoff=0.6)
            if match:
                extras.append(match[0])

    found = [tag for _, tag in sorted(hits, key=lambda item: item[0])]
    return _unique(found + extras)


def _unique(items) -> list[str]:
    result: list[str] = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# 排版
# ---------------------------------------------------------------------------
def render_tag_catalog(groups: dict[str, list[str]] | None = None) -> str:
    """列出全部公招词条。"""

    groups = groups or TAG_GROUPS
    lines = ["公开招募词条一览"]
    for name, tags in groups.items():
        lines.append(f"【{name}】{' '.join(tags)}")
    return "\n".join(lines)


def render_pool(combo: Combo, max_per_line: int = 12) -> str:
    """列出某个组合的全部候选干员（/公招干员 指令用）。"""

    lines = [f"词条组合：{combo.label}"]
    if combo.guaranteed >= 4:
        lines.append(f"候选 {len(combo.operators)} 人，保底 {combo.guaranteed}★")
    else:
        lines.append(f"候选 {len(combo.operators)} 人")
    by_star: dict[int, list[str]] = {}
    for op in combo.operators:
        by_star.setdefault(op.star, []).append(op.name)
    for star in sorted(by_star, reverse=True):
        names = by_star[star]
        head = f"{star}★（{len(names)}）："
        for start in range(0, len(names), max_per_line):
            chunk = names[start : start + max_per_line]
            lines.append((head if start == 0 else "　") + "、".join(chunk))
    return "\n".join(lines)


def render_star_pool(
    db: RecruitDatabase, tags, *, star_floor: int = DEFAULT_STAR_FLOOR
) -> str:
    """只勾中「高级资深干员」/「资深干员」时返回单独的池名单，否则返回空串。

    这种场景整份报告就只剩这一个池，名单又长，拆成单独一条消息发，
    标题直接用池名（高级资深干员池 / 资深干员池）。
    """

    tags = list(dict.fromkeys(tags))
    if len(tags) != 1 or tags[0] not in STAR_POOL_TITLES:
        return ""
    combo = db.evaluate(tags, star_floor=star_floor)
    if combo is None:
        return ""
    lines = [f"{STAR_POOL_TITLES[tags[0]]}："]
    lines.extend(_pool_lines(combo, per_line=8, indent=""))
    return "\n".join(lines)


def _pool_lines(combo: Combo, per_line: int = 8, indent: str = "   ") -> list[str]:
    """按星级从高到低列出组合的全部候选干员。"""

    by_star: dict[int, list[str]] = {}
    for op in combo.operators:
        by_star.setdefault(op.star, []).append(op.name)

    lines: list[str] = []
    for star in sorted(by_star, reverse=True):
        names = sorted(by_star[star])
        head = f"{indent}{star}★（{len(names)}）："
        for start in range(0, len(names), per_line):
            chunk = names[start : start + per_line]
            lines.append((head if start == 0 else indent + "　") + "、".join(chunk))
    return lines


def _combo_line(combo: Combo, index: int = 0, per_line: int = 8) -> str:
    """单个组合：标题行 + 按星级分组的全部候选干员。"""

    prefix = f"{index}." if index else "·"
    head = f"{prefix} {combo.label}（{len(combo.operators)} 人）"
    return "\n".join([head] + _pool_lines(combo, per_line=per_line))


def _rate(pool_size: int) -> str:
    """池内 pool_size 人时单人出现的概率（等概率，固定一位小数）。"""

    return f"{100 / max(1, pool_size):.1f}%"


def _priority_hits(combos: list[Combo], tags) -> list[tuple[str, list[Combo]]]:
    """当前词条能凑出的「优先抓取」组合。

    只认推荐表里列出的组合（省略了「高级资深干员」），凑不出来就不提示；
    推荐组合可能因候选池重复被 dedupe 合并进更长的组合（如「召唤」并被
    「治疗+召唤」吃掉），因此按「包含」判定；同时要求干员真在这个池里。
    组合里的词条必须全是本次识别到的词条，凑不出任何一条时调用方不展示此段。
    每名干员的全部可用组合都保留（池小的在前），干员之间按最好的一条排序。
    """

    allowed = frozenset(tags)
    found: dict[str, dict[tuple, Combo]] = {}
    for combo in combos:
        if not combo.senior or not set(combo.tags) <= allowed:
            continue
        own = frozenset(combo.tags) - {SENIOR_TAG}
        pool_names = frozenset(op.name for op in combo.operators)
        pool_key = tuple(op.name for op in combo.operators)
        for name, tag_sets in PRIORITY_SIX.items():
            if name not in pool_names:
                continue
            if not any(tag_set <= own for tag_set in tag_sets):
                continue
            found.setdefault(name, {}).setdefault(pool_key, combo)

    hits: list[tuple[str, list[Combo]]] = []
    for name, by_pool in found.items():
        ordered = sorted(by_pool.values(), key=lambda c: (len(c.operators), -len(c.tags)))
        hits.append((name, ordered))
    hits.sort(key=lambda item: (len(item[1][0].operators), -len(item[1][0].tags), item[0]))
    return hits


def _four_star_tip(combos: list[Combo]) -> str:
    """四星里值得单独提一句的干员（目前只有桃金娘）。

    只有候选池够小（出率不低于 ``TIP_FOUR_MIN_RATE``）时才提示，
    避免把「随缘」写成推荐。
    """

    best: Combo | None = None
    for combo in combos:
        if combo.senior or combo.veteran:
            continue
        if TIP_FOUR_OPERATOR not in {op.name for op in combo.operators}:
            continue
        if 1 / max(1, len(combo.operators)) < TIP_FOUR_MIN_RATE:
            continue
        if best is None or len(combo.operators) < len(best.operators):
            best = combo
    if best is None:
        return ""
    return (
        f"提示：选择「{best.label}」词条可获取{TIP_FOUR_OPERATOR}，建议优先抓取"
        f"（{_rate(len(best.operators))}）"
    )


def render_report(
    db: RecruitDatabase,
    tags,
    combos: list[Combo] | None = None,
    *,
    max_tags: int = 3,
    max_per_tier: int = 6,
    star_floor: int = DEFAULT_STAR_FLOOR,
    label: str = "",
) -> str:
    """把组合分析结果排成一段可直接发给用户的文本。

    ``label`` 用于多图场景（如「第 1/3 张截图」），会拼在标题后。
    """

    tags = list(tags)
    if combos is None:
        combos = db.analyze(tags, max_tags=max_tags, star_floor=star_floor)
    all_combos = list(combos)
    combos = [combo for combo in all_combos if is_worth_showing(combo)]

    lines: list[str] = [f"公开招募小助手 · {label}" if label else "公开招募小助手"]
    lines.append(f"词条：{' / '.join(tags) if tags else '（无）'}")

    if not combos:
        lines.append("")
        if any(tag in ("新手", "支援机械") for tag in tags):
            lines.append("「新手」只对应 2★、「支援机械」只对应 1★，9 小时招募不会出现。")
        # 只有池内最低就是 5★（既没 4★ 也没 3★）的组合，才谈得上「仅含 5★」
        elif any(combo.guaranteed >= 5 for combo in all_combos):
            lines.append("这些组合仅含 5★ 干员，但有一定概率被划掉。")
        else:
            lines.append("这些词条都不保底 4★，建议不选词条。")
        return "\n".join(lines)

    hits = _priority_hits(combos, tags)

    lines.append("")
    lines.append(_advice_text(combos, combos[0]))

    tip = _four_star_tip(combos)
    if tip:
        lines.append(tip)

    if hits:
        lines.append("")
        lines.append("★推荐优先抓取")
        for name, combo_list in hits:
            lines.append(f"  {name}：")
            for combo in combo_list:
                lines.append(f"    · {combo.label} {_rate(len(combo.operators))}")
        lines.append("")
        lines.append("（按概率排序，不考虑词条被划掉的情况）")

    grouped: dict[int, list[Combo]] = {}
    for combo in combos:
        grouped.setdefault(combo.tier, []).append(combo)

    # 只勾中一个资历词条：整个池子交给 render_star_pool 单独发，这里不再列一遍
    star_pool_only = len(tags) == 1 and tags[0] in STAR_POOL_TITLES

    # 「保底 4★+极小概率 5★」本身也是保底 4★：同一个候选池只在更高的一档里出现，
    # 不在下面的「保底 4★」档重复列一次
    five_four_pools = {
        tuple(op.name for op in combo.operators)
        for combo in grouped.get(TIER_FIVE_FOUR, ())
    }
    for tier in () if star_pool_only else sorted(grouped):
        title = TIER_TITLES.get(tier)
        if not title:
            continue
        section = grouped[tier]
        if tier == TIER_FOUR_GUARANTEED:
            section = [
                combo
                for combo in section
                if tuple(op.name for op in combo.operators) not in five_four_pools
            ]
        if not section:
            continue
        lines.append("")
        lines.append(f"■ {title}｜{len(section)} 组")
        # 纯 5★ 池（没有 4★ / 3★ 兜底）才有被划掉的风险，其他档不用提
        if tier == TIER_FIVE_CHANCE and any(combo.guaranteed >= 5 for combo in section):
            lines.append("   （仅含 5★ 的组合，词条有一定概率被划掉）")
        for index, combo in enumerate(section[:max_per_tier], 1):
            lines.append(_combo_line(combo, index))
        if len(section) > max_per_tier:
            lines.append(f"  …另有 {len(section) - max_per_tier} 组同档组合")

    if "支援机械" in tags:
        lines.append("")
        lines.append("提示：支援机械只匹配 1★ 小车，时长拉到 1 小时必出小车。概率较低建议优先选择")
    return "\n".join(lines)


def _advice_text(combos: list[Combo], top: Combo) -> str:
    """顶部一句话结论（只写结论，不重复「识别方式 / 时长」类信息）。

    6★ / 5★ 只给「选哪个资历词条」这一句：具体组合交给下面的档位，
    免得结论里的组合名被当成唯一正确答案（高资池里 6★ 很多）。
    """

    def pool_text(combo: Combo, min_star: int = 0, limit: int = 6) -> str:
        ops = sorted(
            (op for op in combo.operators if op.star >= min_star),
            key=lambda op: (-op.star, op.name),
        )
        text = "、".join(op.name for op in ops[:limit])
        if len(ops) > limit:
            text += f" 等{len(ops)}人"
        return text

    tiers = {combo.tier for combo in combos}
    if TIER_SIX in tiers:
        return "结论：选择高级资深干员，拉满 9 小时必出 6★"
    if TIER_FIVE_SENIOR in tiers:
        return "结论：选择资深干员，拉满 9 小时必出 5★"
    if TIER_FIVE_FOUR in tiers:
        return f"结论：选「{top.label}」保底 4★、极小概率出 5★（{pool_text(top)}）"
    if TIER_FOUR_GUARANTEED in tiers:
        return f"结论：选「{top.label}」保底 4★（{pool_text(top, 4)}）"
    if TIER_FIVE_CHANCE in tiers:
        if top.guaranteed >= 5:
            return f"结论：「{top.label}」池内只有 5★（{pool_text(top)}），小概率出 5★"
        return f"结论：选「{top.label}」小概率出 5★（4★以上：{pool_text(top, 4)}）"
    if TIER_FOUR_CHANCE in tiers:
        return f"结论：选「{top.label}」小概率出 4★（4★以上：{pool_text(top, 4)}）"
    return "结论：这些词条都不保底 4★，建议不选词条"
