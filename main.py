"""猜选手 · 多题库（AstrBot 插件）

一套对局骨架，三个题库：
  - 弗一把  CS:GO / CS2 Major 选手（8 项属性）
  - 足一把  足球球员（9 项属性）
  - 撸一把  LoL 职业选手（9 项属性）

判定口径分别对齐 shnlfriberg/csgofriberg 与 zuyiba 的页面逻辑；
LoL 站点的判定在服务端，这里是按其棋盘列的语义复刻（见 README 说明）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_friberg_guess"
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"

GREEN, YELLOW, RED = "🟩", "🟨", "🟥"
LEVEL_EMOJI = {"correct": GREEN, "close": YELLOW, "wrong": RED}
LEGEND = "🟩 完全一致　🟨 接近　🟥 不符\n↑/↓ 表示答案更大 / 更小"

_LEET_TABLE = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g"}
)
_NOISE_RE = re.compile(r"[\s\u3000,，.。!！?？~～、:：;；'\"“”‘’()（）\[\]【】]+")
_PAREN_RE = re.compile(r"^(.+?)\s*[（(]([^）)]{1,10})[）)]\s*$")


# ============================================================
# 通用工具
# ============================================================


def normalize_name(text: str) -> str:
    """归一化：全角转半角、去符号空格、小写、leet 归一。"""
    text = unicodedata.normalize("NFKC", text or "").strip().lower()
    text = _NOISE_RE.sub("", text)
    return text.translate(_LEET_TABLE)


# 位置显示：题库里多数沿用英文标签，替补这类新值给个中文叫法
_ROLE_LABELS = {"Substitute": "替补", "Sub": "替补"}


def role_label(role: Any) -> str:
    text = str(role or "").strip()
    return _ROLE_LABELS.get(text, text or "—")


def split_nickname(nickname: str) -> tuple[str, str]:
    """拆出主体名与括号后缀，例如 `NiKo（波黑）` → (`NiKo`, `波黑`)。"""
    matched = _PAREN_RE.match((nickname or "").strip())
    if matched:
        return matched.group(1).strip(), matched.group(2).strip()
    return (nickname or "").strip(), ""


@dataclass
class Cell:
    """棋盘上的一格。"""

    label: str
    value: str
    level: str  # correct / close / wrong


def num_cell(label: str, guess_val: Any, target_val: Any, tolerance: int, suffix: str = "") -> Cell:
    """数值型属性：一致 / 差在容差内 / 不符，并给出方向。"""
    if guess_val == target_val:
        return Cell(label, f"{guess_val}{suffix}", "correct")
    arrow = "↑" if target_val > guess_val else "↓"
    level = "close" if abs(target_val - guess_val) <= tolerance else "wrong"
    return Cell(label, f"{guess_val}{suffix}{arrow}", level)


BOARD_LINE_BUDGET = 32  # 一行最多占多少半角单位；手机放大字号后约 16 个汉字宽，超了就会折行


def _disp_width(text: str) -> int:
    """估算一段文字在聊天窗里的显示宽度：全角字符与 emoji 算 2，其它算 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _wrap_join(parts: list[str], budget: int = BOARD_LINE_BUDGET, sep: str = " · ") -> list[str]:
    """把若干片段用分隔符连起来，超过行宽就换行，避免在窄屏上被硬折断。"""
    lines: list[str] = []
    current = ""
    for part in (p for p in parts if p):
        candidate = f"{current}{sep}{part}" if current else part
        if current and _disp_width(candidate) > budget:
            lines.append(current)
            current = part
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


BOARD_COLUMNS = 2


def render_board(theme: "Theme", index: int, total: int, guess: dict, target: dict) -> str:
    """把一次猜测渲染成群聊可读的棋盘：一行两项，该项太长就单独占一行，绝不折行。"""
    cells = theme.judge(guess, target)
    hit = all(c.level == "correct" for c in cells)
    blocks = [f"{LEVEL_EMOJI[c.level]}{c.label} {c.value}" for c in cells]

    lines: list[str] = []
    current = ""
    count = 0
    for block in blocks:
        if not current:
            current, count = block, 1
            continue
        candidate = f"{current}　{block}"
        if count < BOARD_COLUMNS and _disp_width(candidate) <= BOARD_LINE_BUDGET:
            current, count = candidate, count + 1
        else:
            lines.append(current)
            current, count = block, 1
    if current:
        lines.append(current)

    head = "🎯 命中" if hit else f"第 {index}/{total} 猜"
    # 标题只要主体名：全名（Erling Haaland 这类）会把标题撑到折行
    short = re.split(r"[（(]", theme.title_of(guess))[0].strip() or theme.title_of(guess)
    return f"{head}｜{short}\n" + "\n".join(lines)


# ============================================================
# 题库主题
# ============================================================


class Theme:
    """一个题库（一种游戏）的定义。"""

    key: str = ""
    command: str = ""
    aliases: tuple[str, ...] = ()
    label: str = ""  # 「CS Major 选手」
    icon: str = "🎮"
    db_file: str = ""
    alias_file: str = ""  # 外号表（data 目录下的 json），留空表示不支持外号
    attempts: int = 8
    pool_alias: dict[str, str] = {}
    default_pool: str = "normal"
    difficulty_hint: str = "普通"

    # 猜的队伍是答案选手效力过的老东家时，算不算「接近」；由配置 history_team 控制
    history_team: bool = False

    # -- 数据 --
    def load(self, raw: Any) -> list[dict]:
        raise NotImplementedError

    def keys_of(self, player: dict) -> set[str]:
        raise NotImplementedError

    def title_of(self, player: dict) -> str:
        raise NotImplementedError

    def pools(self, players: list[dict]) -> dict[str, list[dict]]:
        return {"normal": players}

    def pool_label(self, pool: str) -> str:
        return pool

    # -- 玩法 --
    def judge(self, guess: dict, target: dict) -> list[Cell]:
        raise NotImplementedError

    def card(self, player: dict) -> str:
        raise NotImplementedError

    def hints(self, player: dict) -> list[tuple[str, str]]:
        raise NotImplementedError

    # -- 组装 --
    def resolve_pool(self, raw: str, default: str) -> str | None:
        """把用户输入的难度词转成 pool key；无法识别返回 None。"""
        token = (raw or "").strip().split()[0] if (raw or "").strip() else ""
        if not token:  # 没写难度：把配置里的默认值也换算成 pool key
            word = (default or "").strip()
            return (
                self.pool_alias.get(word)
                or self.pool_alias.get(word.lower())
                or self.default_pool
            )
        return self.pool_alias.get(token) or self.pool_alias.get(token.lower())


def _diff_cell(label: str, guess_val: int, target_val: int, tolerance: int, suffix: str = "") -> Cell:
    if guess_val == target_val:
        return Cell(label, f"{guess_val}{suffix}", "correct")
    arrow = "↑" if target_val > guess_val else "↓"
    level = "close" if abs(target_val - guess_val) <= tolerance else "wrong"
    return Cell(label, f"{guess_val}{suffix}{arrow}", level)


class CSTheme(Theme):
    """CS:GO / CS2 Major 选手。"""

    key = "cs"
    command = "弗一把"
    aliases = ("猜选手", "cs猜选手")
    label = "CS Major 选手"
    icon = "🔫"
    db_file = "players.json"
    alias_file = "cs_aliases.json"
    attempts = 8
    pool_alias = {
        "易": "easy", "入门": "easy", "新手": "easy", "easy": "easy", "beginner": "easy",
        "中": "normal", "简单": "normal", "进阶": "normal", "normal": "normal",
        "难": "hard", "普通": "hard", "标准": "hard", "困难": "hard", "地狱": "hard", "hard": "hard",
    }
    difficulty_hint = "易 / 中 / 难"

    def load(self, raw: Any) -> list[dict]:
        players = raw.get("players") if isinstance(raw, dict) else raw
        out = []
        for p in players or []:
            if not p.get("nickname") or not p.get("is_enabled", True):
                continue
            p = dict(p)
            p["_base"] = split_nickname(p["nickname"])[0]
            out.append(p)
        return out

    def keys_of(self, player: dict) -> set[str]:
        return {player["nickname"], player.get("_base", "")}

    def title_of(self, player: dict) -> str:
        return player["nickname"]

    def pools(self, players: list[dict]) -> dict[str, list[dict]]:
        hottest = _cs_famous_pool(players)
        mainstream = [p for p in players if "easy" in (p.get("difficulties") or [])]
        return {
            "easy": hottest,
            "normal": mainstream or players,
            "hard": players,
        }

    def pool_label(self, pool: str) -> str:
        return {
            "easy": "易（最火 200 人）",
            "normal": "中（主流）",
            "hard": "难（全量）",
        }.get(pool, pool)

    def judge(self, guess: dict, target: dict) -> list[Cell]:
        cells: list[Cell] = []

        # 国家 / 地区：相同=一致；不同但同赛区=接近
        if guess.get("nationality") == target.get("nationality"):
            cells.append(Cell("国家", guess.get("nationality", "—"), "correct"))
        elif guess.get("region") and guess.get("region") == target.get("region"):
            cells.append(Cell("国家", guess.get("nationality", "—"), "close"))
        else:
            cells.append(Cell("国家", guess.get("nationality", "—"), "wrong"))

        same_region = guess.get("region") == target.get("region")
        cells.append(Cell("赛区", guess.get("region", "—"), "correct" if same_region else "wrong"))

        history = target.get("team_history") or []
        if guess.get("team") == target.get("team"):
            cells.append(Cell("战队", guess.get("team", "—"), "correct"))
        elif self.history_team and guess.get("team") and guess["team"] in history:
            cells.append(Cell("战队", guess["team"], "close"))  # 关掉时老东家一律算不符
        else:
            cells.append(Cell("战队", guess.get("team", "—"), "wrong"))

        cells.append(_diff_cell("年龄", int(guess.get("age") or 0), int(target.get("age") or 0), 3))
        same_role = guess.get("role") == target.get("role")
        cells.append(Cell("位置", role_label(guess.get("role")), "correct" if same_role else "wrong"))
        cells.append(
            _diff_cell("冠军", int(guess.get("major_championships") or 0), int(target.get("major_championships") or 0), 1)
        )
        cells.append(
            _diff_cell("出场", int(guess.get("major_appearances") or 0), int(target.get("major_appearances") or 0), 1)
        )
        same_active = bool(guess.get("is_active")) == bool(target.get("is_active"))
        cells.append(
            Cell("状态", "现役" if guess.get("is_active") else "已退役", "correct" if same_active else "wrong")
        )
        return cells

    def card(self, player: dict) -> str:
        status = "现役" if player.get("is_active") else "已退役"
        facts = _wrap_join(
            [
                player.get("nationality", "—"),
                player.get("region", "—"),
                player.get("team", "—"),
                role_label(player.get("role")),
                f"{player.get('age', '—')} 岁",
            ]
        )
        honors = _wrap_join(
            [
                f"Major 冠军 {player.get('major_championships', 0)}",
                f"出场 {player.get('major_appearances', 0)}",
            ]
        )
        return "\n".join([f"📇 {player['nickname']}（{status}）", *facts, *(f"🏆 {h}" for h in honors)])

    def hints(self, player: dict) -> list[tuple[str, str]]:
        return [
            ("位置", f"打的是 {role_label(player.get('role'))}"),
            ("赛区", f"来自 {player.get('region', '—')} 赛区"),
            ("国家", f"{player.get('nationality', '—')} 人"),
            ("冠军", f"Major 冠军 {player.get('major_championships', 0)} 次"),
            ("出场", f"Major 出场 {player.get('major_appearances', 0)} 次"),
            ("状态", "现役选手" if player.get("is_active") else "已经退役"),
            ("年龄", f"{player.get('age', '—')} 岁"),
        ]


TOP5_LEAGUES = {"英超", "西甲", "德甲", "意甲", "法甲"}

# 「易」档用的热门球队（名单与题库里的队名一致）
# 「最火」的 CS 选手名单（按主体名比对，忽略括号后缀）。只用于挑人物，不参与判定。
CS_FAMOUS = {
    # 传奇 / GOAT 级
    "s1mple", "zywoo", "niko", "device", "dupreeh", "xyp9x", "gla1ve", "magisk", "olofmeister",
    "get-right", "f0rest", "neo", "taz", "markeloff", "edward", "zeus", "seized", "flamie",
    "boombl4", "adren", "hobbit", "ax1le", "sh1ro", "donk", "m0nesy", "kjaerbye", "jw", "flusha",
    "krimz", "pronax", "xizt", "friberg", "dennis", "rez", "brollan", "draken",
    # 现役顶级
    "ropz", "twistzz", "naf", "elige", "stewie2k", "autimatic", "brehze", "tarik", "nitr0",
    "coldzera", "fallen", "fer", "fnx", "taco", "kscerato", "yuurih", "snax", "byali",
    "pashabiceps", "karrigan", "rain", "broky", "frozen", "torzsi", "siuhy", "jimpphat", "xertion",
    "apex", "shox", "kennys", "scream", "bodyy", "spinx", "flamez", "mezii", "rpk",
    "snappi", "sunpayus", "maden", "nertz", "hades", "sdy", "jl", "im", "aleksib", "b1t", "w0nderful",
    "electronic", "perfecto", "chopper", "zont1x", "cadian", "stavn", "jabbi", "teses", "sjuush",
    "blamef", "k0nfig", "refrezh", "art", "yekindar", "jame", "qikert", "grim", "rush", "boltz",
    "hunter-", "nexa", "jks", "hooxi", "xantares", "woxic", "calyx", "maj3r", "attacker", "summer",
    "somebody", "jamyoung", "westmelon", "kaze", "buster", "keoz", "kylar", "nbk-", "kioshima",
    "ex6tenz", "smithzz", "sixer", "jackz", "amanek", "lucky", "liazz", "azr", "gratisfaction",
    "ustilo", "rickeh", "malta", "get-right", "guardian", "dosia", "skadoodle", "fifflaren",
    "cajunb", "msl", "pyth", "delpan",
}

BIG_CLUBS = {
    # 英超
    "曼城", "利物浦", "阿森纳", "曼联", "切尔西", "热刺", "纽卡斯尔联", "阿斯顿维拉",
    # 西甲
    "皇家马德里", "巴塞罗那", "马德里竞技", "塞维利亚", "皇家社会", "比利亚雷亚尔",
    "皇家贝蒂斯", "瓦伦西亚",
    # 德甲
    "拜仁慕尼黑", "多特蒙德", "勒沃库森", "莱比锡红牛", "法兰克福", "斯图加特",
    # 意甲
    "国际米兰", "AC米兰", "尤文图斯", "那不勒斯", "罗马", "拉齐奥", "亚特兰大",
    # 法甲
    "巴黎圣日耳曼", "马赛", "里昂", "摩纳哥", "里尔",
}

# 题目范围：单联赛 / 五大
LEAGUE_SCOPES = {
    "英超": "epl", "西甲": "laliga", "德甲": "bundesliga", "意甲": "seriea", "法甲": "ligue1",
    "五大": "top5", "五大联赛": "top5",
}
FAMOUS = {
    "梅西", "C罗", "姆巴佩", "哈兰德", "内马尔", "贝林厄姆",
    "维尼修斯", "萨拉赫", "德布劳内", "凯恩", "莱万多夫斯基", "莫德里奇",
    "克罗斯", "本泽马", "苏亚雷斯", "伊布拉希莫维奇", "贝尔", "阿扎尔",
    "卡卡", "罗纳尔迪尼奥", "大罗", "齐达内", "贝克汉姆", "亨利",
    "杰拉德", "兰帕德", "鲁尼", "马尔蒂尼", "托蒂", "布冯",
    "卡西利亚斯", "哈维", "伊涅斯塔", "皮尔洛", "罗本", "里贝里",
    "德罗巴", "埃托奥", "阿圭罗", "巴乔", "菲戈", "贝利",
    "马拉多纳", "克鲁伊夫", "范巴斯滕", "普拉蒂尼", "济科", "罗马里奥",
    "里瓦尔多", "卡福", "罗伯特·卡洛斯", "内斯塔", "卡纳瓦罗", "拉姆",
    "克洛泽", "施魏因施泰格", "巴拉克", "欧文", "劳尔", "范尼斯特鲁伊",
    "博格坎普", "皮克", "布斯克茨", "若尔迪·阿尔巴", "马塞洛", "佩佩",
    "瓦拉内", "亚马尔", "罗德里", "福登", "萨卡", "厄德高",
    "拉菲尼亚", "库尔图瓦", "阿利松", "范迪克", "吕迪格", "戴维斯",
    "基米希", "托马斯·穆勒", "维尔茨", "穆西亚拉", "劳塔罗·马丁内斯", "巴雷拉",
    "莱奥", "多纳鲁马", "格列兹曼", "奥布拉克", "特尔施特根", "诺伊尔",
    "迪马利亚", "孙兴慜", "郑智", "孙继海", "中田英寿", "中村俊辅",
    "朴智星", "本田圭佑", "范志毅", "保利尼奥", "孔卡", "马内",
    "马赫雷斯", "库利巴利", "米林科维奇-萨维奇", "菲利克斯", "布罗佐维奇", "纳乔",
    "鲁本·内维斯", "沃特金斯", "托尼", "坎特", "博格巴", "拉什福德",
    "布鲁诺·费尔南德斯", "麦迪逊", "凯尔·沃克", "戈登", "埃德森", "奥纳纳",
    # 早退役但历史地位够的，不能因为没欧冠/世界杯数据就被当成路人
    "贝肯鲍尔", "普约尔", "巴雷西", "哈维·阿隆索", "加图索", "因扎吉",
    "比利亚", "费尔南多·托雷斯", "德尔·皮耶罗", "盖德·穆勒", "马斯切拉诺",
    "卡恩", "亚亚·图雷", "斯内德", "阿什利·科尔", "西多夫", "里杰卡尔德",
    "范德萨", "斯科尔斯", "罗纳德·科曼", "特里", "特维斯", "切赫",
    "里奥·费迪南德", "维迪奇", "埃夫拉", "基耶利尼", "萨内蒂", "吉格斯",
    "维埃拉", "罗伊·基恩", "德科", "里克尔梅", "古利特", "克鲁伊维特",
    "弗兰", "克雷斯波", "巴蒂斯图塔", "范佩西", "尤西比奥", "舍甫琴科",
    "内德维德", "马特乌斯", "坎通纳", "阿兰·希勒", "乔治·维阿", "阿德里亚诺",
    "特雷泽盖", "阿内尔卡", "拉维奇", "卡索拉", "坎贝尔", "欧文·哈格里夫斯",
    "博努奇", "法布雷加斯", "大卫·席尔瓦", "奥斯卡", "米尔纳",
}

SCOPE_LABELS = {
    "all": "全部联赛", "top5": "五大联赛", "epl": "英超", "laliga": "西甲",
    "bundesliga": "德甲", "seriea": "意甲", "ligue1": "法甲",
}
DIFF_WORDS = {
    "易": "easy", "简单": "easy", "easy": "easy",
    "中": "normal", "普通": "normal", "标准": "normal", "normal": "normal",
    "难": "hard", "困难": "hard", "地狱": "hard", "hard": "hard",
}


def _eff_club(player: dict) -> str:
    if player.get("league") == "已退役":
        return "已退役"
    if player.get("league") not in TOP5_LEAGUES:
        return "非五大联赛"
    return player.get("club", "—")


class FootballTheme(Theme):
    """足球球员（复刻 zuyiba 的 9 项对比）。"""

    key = "football"
    command = "足一把"
    aliases = ("足球一把", "猜球员", "猜球星")
    label = "足球球员"
    icon = "⚽"
    db_file = "football_players.json"
    attempts = 10
    pool_alias = {**DIFF_WORDS, "核心": "normal", "全部": "hard", "全量": "hard", "full": "hard"}
    difficulty_hint = "易（默认，知名 200 人）/ 中（五大联赛）/ 难（全部），或指定联赛：英超 / 西甲 / 德甲 / 意甲 / 法甲 / 五大"

    def resolve_pool(self, raw: str, default: str) -> str | None:
        """支持「联赛 + 难度」组合，如 `英超 易`、`五大 难`。"""
        scope = ""
        diff = DIFF_WORDS.get(str(default).strip(), "normal")
        for token in (raw or "").split():
            if token in LEAGUE_SCOPES:
                scope = LEAGUE_SCOPES[token]
            elif token in DIFF_WORDS:
                diff = DIFF_WORDS[token]
            elif token in {"全部", "全量", "所有", "full", "all"}:
                scope, diff = "all", "hard"
            else:
                return None
        # 「易」（知名 250 人）和「难」都在全库取，「中」默认限定五大联赛
        if not scope:
            scope = "all" if diff in ("easy", "hard") else "top5"
        return f"{scope}:{diff}"

    def load(self, raw: Any) -> list[dict]:
        players = raw.get("players") if isinstance(raw, dict) else raw
        return [dict(p) for p in players or [] if p.get("name")]

    def keys_of(self, player: dict) -> set[str]:
        keys = {player.get("name", ""), player.get("cnName", "")}
        keys.update(player.get("aliases") or [])
        return {k for k in keys if k}

    def title_of(self, player: dict) -> str:
        cn = player.get("cnName")
        return f"{cn}（{player['name']}）" if cn and cn != player["name"] else player["name"]

    def pools(self, players: list[dict]) -> dict[str, list[dict]]:
        famous = _easy_pool(players)
        top5 = [p for p in players if p.get("league") in TOP5_LEAGUES]
        out = {
            # 全库：易=知名 250 人（现役 190 + 退役传奇 60），中=五大联赛，难=全部
            "all:easy": famous,
            "all:normal": top5 or players,
            "all:hard": players,
            "top5:easy": [p for p in famous if p.get("league") in TOP5_LEAGUES] or top5,
            "top5:normal": top5,
            "top5:hard": top5,
        }
        for league, key in LEAGUE_SCOPES.items():
            if key == "top5":
                continue
            pool = [p for p in players if p.get("league") == league]
            out[f"{key}:easy"] = _league_easy(pool, famous)
            out[f"{key}:normal"] = pool
            out[f"{key}:hard"] = pool
        return out

    def pool_label(self, pool: str) -> str:
        scope, sep, diff = pool.partition(":")
        if not sep:  # 只给了难度词（如 normal），按全库处理
            scope, diff = "all", pool
        scope_label = SCOPE_LABELS.get(scope, scope)
        word = {"easy": "易", "normal": "中", "hard": "难"}.get(diff, diff)
        if scope == "all":
            return {"easy": "易（知名 250 人）", "normal": "中（五大联赛）", "hard": "难（全量）"}.get(diff, word)
        return f"{scope_label} · {word}"

    def judge(self, guess: dict, target: dict) -> list[Cell]:
        cells: list[Cell] = []

        # 国籍：双国籍互认；否则同大洲算接近
        g_nat, t_nat = guess.get("nationality"), target.get("nationality")
        g_dual = guess.get("dualNationality") or []
        t_dual = target.get("dualNationality") or []
        if g_nat == t_nat or t_nat in g_dual or g_nat in t_dual:
            cells.append(Cell("国籍", g_nat or "—", "correct"))
        elif guess.get("continent") and guess.get("continent") == target.get("continent"):
            cells.append(Cell("国籍", g_nat or "—", "close"))
        else:
            cells.append(Cell("国籍", g_nat or "—", "wrong"))

        # 联赛
        cells.append(
            Cell("联赛", guess.get("league", "—"), "correct" if guess.get("league") == target.get("league") else "wrong")
        )
        # 俱乐部（非五大联赛统一显示）
        g_club, t_club = _eff_club(guess), _eff_club(target)
        cells.append(Cell("俱乐部", g_club, "correct" if g_club == t_club else "wrong"))
        # 位置
        cells.append(
            Cell("位置", guess.get("position", "—"), "correct" if guess.get("position") == target.get("position") else "wrong")
        )
        # 出生年 ±2
        cells.append(_diff_cell("出生年", int(guess.get("birthYear") or 0), int(target.get("birthYear") or 0), 2))
        # 球衣号 ±2
        cells.append(_diff_cell("号码", int(guess.get("number") or 0), int(target.get("number") or 0), 2, ""))
        # 欧冠冠军数 ±1
        cells.append(_diff_cell("欧冠", int(guess.get("ucl") or 0), int(target.get("ucl") or 0), 1))
        # 世界杯参赛届数 ±1
        cells.append(_diff_cell("世界杯", int(guess.get("wc") or 0), int(target.get("wc") or 0), 1))
        # 世界杯夺冠次数：必须一致
        cells.append(
            _diff_cell("世界杯冠军", int(guess.get("wcTitle") or 0), int(target.get("wcTitle") or 0), 0)
        )
        return cells

    def card(self, player: dict) -> str:
        facts = _wrap_join(
            [
                player.get("nationality", "—"),
                player.get("league", "—"),
                _eff_club(player),
                player.get("position", "—"),
                f"{player.get('birthYear', '—')} 年生",
                f"{player.get('number', '—')} 号",
            ]
        )
        honors = _wrap_join(
            [
                f"欧冠 {player.get('ucl', 0)}",
                f"世界杯 {player.get('wc', 0)}",
                f"夺冠 {player.get('wcTitle', 0)}",
            ]
        )
        return "\n".join(
            [f"📇 {player.get('cnName', player['name'])}（{player['name']}）", *facts, *(f"🏆 {h}" for h in honors)]
        )

    def hints(self, player: dict) -> list[tuple[str, str]]:
        return [
            ("国籍", f"{player.get('nationality', '—')}（{player.get('continent', '—')}）"),
            ("联赛", f"{player.get('league', '—')}"),
            ("俱乐部", f"{_eff_club(player)}"),
            ("位置", f"{player.get('position', '—')}"),
            ("出生年", f"{player.get('birthYear', '—')} 年"),
            ("号码", f"{player.get('number', '—')} 号"),
            ("欧冠", f"欧冠冠军 {player.get('ucl', 0)} 次"),
        ]


LOL_EASY_LIMIT = 150
LOL_NORMAL_LIMIT = 250
LOL_RETIRED_KEEP = 40  # 「中」档里给知名退役留多少名额
LOL_HOT = [
    "Faker", "Chovy", "Knight", "Ruler", "Zeus", "Oner", "Gumayusi", "Keria",
    "Canyon", "ShowMaker", "Viper", "Scout", "Meiko", "Bin", "Xun", "Elk",
    "JackeyLove", "Tian", "Rookie", "TheShy", "Doinb", "Crisp", "Xiaohu", "Wei",
    "Breathe", "Light", "Creme", "Kanavi", "369", "Missing", "Hope", "Tarzan",
    "Peanut", "Kiin", "Bdd", "Cuzz", "Lehends", "Doran", "Zeka", "Kingen",
    "Pyosik", "Caps", "Humanoid", "Hans Sama", "Elyoya", "Blaber", "Jojopyun",
    "Impact", "CoreJJ", "Uzi", "Clearlove", "Bengi", "Bang", "Wolf", "PraY",
    "Smeb", "Deft", "Mata", "PawN", "Imp", "xPeke", "Jankos", "Doublelift",
]
LOL_HOT_KEYS = {name.lower() for name in LOL_HOT}


class LolTheme(Theme):
    """LoL 职业选手（战队 / S 赛数据来自 guessassin，真名与别名来自 Liquipedia）。"""

    key = "lol"
    command = "撸一把"
    aliases = ("lol一把", "猜撸", "猜联盟")
    label = "LoL 职业选手"
    icon = "🖱"
    db_file = "lol_players.json"
    attempts = 8
    pool_alias = {
        "易": "easy", "简单": "easy", "easy": "easy",
        "中": "normal", "普通": "normal", "标准": "normal", "normal": "normal",
        "难": "hard", "全部": "hard", "地狱": "hard", "hard": "hard",
    }
    difficulty_hint = "易 / 中 / 难"

    def load(self, raw: Any) -> list[dict]:
        players = raw.get("players") if isinstance(raw, dict) else raw
        out = []
        for p in players or []:
            if not p.get("gameId"):
                continue
            p = dict(p)
            positions = p.get("positions")
            p["_positions"] = positions if isinstance(positions, list) else [str(positions or "—")]
            # 教练不算选手：只挂教练头衔的跳过
            if p["_positions"] and all(x == "教练" for x in p["_positions"]):
                continue
            p["_team"] = (p.get("currentTeam") or "").strip()
            p["_history"] = [t for t in (p.get("historicalTeams") or []) if t]
            p["_country"] = (p.get("country") or "").strip()
            p["_continent"] = (p.get("continent") or "").strip()
            p["_region"] = (p.get("region") or "").strip()
            p["_worlds_app"] = int(p.get("worldsApp") or 0)
            p["_worlds_title"] = int(p.get("worldsTitle") or 0)
            out.append(p)
        return out

    def keys_of(self, player: dict) -> set[str]:
        keys = {player.get("gameId", ""), player.get("realName", "")}
        keys.update(player.get("aliases") or [])
        return {k for k in keys if k}

    def title_of(self, player: dict) -> str:
        return player["gameId"]

    @staticmethod
    def _fame(player: dict) -> tuple:
        """热门排序：有战队 > S 赛冠军 > S 赛出场 > 现役。"""
        return (
            0 if player.get("_team") else 1,
            0 if str(player.get("status", "")).lower().startswith("act") else 1,
            -int(player.get("_worlds_title") or 0),
            -int(player.get("_worlds_app") or 0),
            player["gameId"].lower(),
        )

    @staticmethod
    def _retired_fame(player: dict) -> tuple:
        """知名退役的排序：纯选手优先（教练转型的往后），再看 S 赛冠军与出场。"""
        return (
            1 if "教练" in (player.get("positions") or []) else 0,
            -int(player.get("_worlds_title") or 0),
            -int(player.get("_worlds_app") or 0),
            player["gameId"].lower(),
        )

    def pools(self, players: list[dict]) -> dict[str, list[dict]]:
        def is_active(player: dict) -> bool:
            return str(player.get("status", "")).lower().startswith("act")

        @staticmethod
        def take(seq, limit: int) -> list[dict]:
            out: list[dict] = []
            seen: set[str] = set()
            for player in seq:
                key = player["gameId"]
                if key in seen:
                    continue
                seen.add(key)
                out.append(player)
                if len(out) >= limit:
                    break
            return out

        active = [p for p in players if is_active(p)]
        retired = [p for p in players if not is_active(p)]

        # 易：点名名单优先，其余按热度补到 150
        hot = [p for p in players if p["gameId"].lower() in LOL_HOT_KEYS]
        easy = take(hot + sorted(players, key=self._fame), LOL_EASY_LIMIT)

        # 中：易档原样当底（保证易档能出的答案中档也能出），
        # 再补一批知名退役和现役，凑满 250
        keep_retired = sorted(retired, key=self._retired_fame)[:LOL_RETIRED_KEEP]
        normal = take(easy + keep_retired + sorted(active, key=self._fame), LOL_NORMAL_LIMIT)
        return {
            "easy": easy or players,
            "normal": normal or active or players,
            "hard": players,
        }

    def pool_label(self, pool: str) -> str:
        return {
            "easy": "易（热门 150 人）",
            "normal": "中（250 人 · 含退役）",
            "hard": "难（全量）",
        }.get(pool, pool)

    def judge(self, guess: dict, target: dict) -> list[Cell]:
        cells: list[Cell] = []

        # 位置（有交集就算一致）
        g_pos = set(guess.get("_positions") or [])
        same_pos = bool(g_pos & set(target.get("_positions") or []))
        cells.append(Cell("位置", "/".join(guess.get("_positions") or ["—"]), "correct" if same_pos else "wrong"))

        # 国家：相同=一致，同大洲=接近
        g_country, t_country = guess.get("_country", ""), target.get("_country", "")
        if g_country and g_country == t_country:
            cells.append(Cell("国家", g_country, "correct"))
        elif guess.get("_continent") and guess.get("_continent") == target.get("_continent"):
            cells.append(Cell("国家", g_country or "—", "close"))
        else:
            cells.append(Cell("国家", g_country or "—", "wrong"))

        # 大洲
        same_continent = bool(guess.get("_continent")) and guess.get("_continent") == target.get("_continent")
        cells.append(Cell("大洲", guess.get("_continent") or "—", "correct" if same_continent else "wrong"))

        # 战队：同名=一致；效力过对方现队=接近（受 history_team 开关控制）；同赛区也接近
        g_team, t_team = guess.get("_team", ""), target.get("_team", "")
        g_hist = {t.lower() for t in guess.get("_history") or []}
        t_hist = {t.lower() for t in target.get("_history") or []}
        if g_team and g_team.lower() == t_team.lower():
            level = "correct"
        elif self.history_team and g_team and (g_team.lower() in t_hist or t_team.lower() in g_hist):
            level = "close"
        elif guess.get("_region") and guess.get("_region") == target.get("_region"):
            level = "close"
        else:
            level = "wrong"
        cells.append(Cell("战队", g_team or "—", level))

        # 年龄 ±2
        cells.append(_diff_cell("年龄", int(guess.get("age") or 0), int(target.get("age") or 0), 2))

        # S 赛参赛次数 ±1、S 赛冠军必须相同
        cells.append(_diff_cell("S赛参赛", int(guess.get("_worlds_app") or 0), int(target.get("_worlds_app") or 0), 1))
        cells.append(
            _diff_cell("S赛冠军", int(guess.get("_worlds_title") or 0), int(target.get("_worlds_title") or 0), 0)
        )

        # 状态
        same_status = str(guess.get("status", "")).lower() == str(target.get("status", "")).lower()
        cells.append(
            Cell(
                "状态",
                "现役" if str(guess.get("status", "")).lower().startswith("act") else "退役",
                "correct" if same_status else "wrong",
            )
        )
        return cells

    def card(self, player: dict) -> str:
        status = "现役" if str(player.get("status", "")).lower().startswith("act") else "退役"
        where = player.get("_country") or "—"
        if player.get("_continent"):
            where += f"（{player['_continent']}）"
        parts = [
            player.get("realName") or "",
            player.get("_team") or "无队伍",
            where,
            "/".join(player.get("_positions") or ["—"]),
            f"{player.get('age', '—')} 岁",
        ]
        honors = _wrap_join(
            [
                f"S 赛 {player.get('_worlds_app', 0)} 次",
                f"冠军 {player.get('_worlds_title', 0)}",
            ]
        )
        body = _wrap_join(parts)
        if honors:
            body.append("🏆 " + honors[0])
            body.extend(honors[1:])
        return "\n".join([f"📇 {player['gameId']}（{status}）", *body])

    def hints(self, player: dict) -> list[tuple[str, str]]:
        return [
            ("位置", "/".join(player.get("_positions") or ["—"])),
            ("战队", player.get("_team") or "无队伍"),
            ("赛区", player.get("_region") or "—"),
            ("国家", player.get("_country") or "—"),
            ("大洲", player.get("_continent") or "—"),
            ("年龄", f"{player.get('age', '—')} 岁"),
            ("S赛参赛", f"S 赛出场 {player.get('_worlds_app', 0)} 次"),
            ("S赛冠军", f"S 赛冠军 {player.get('_worlds_title', 0)} 次"),
            ("状态", "现役" if str(player.get("status", "")).lower().startswith("act") else "退役"),
        ]


def _fame_score(player: dict) -> tuple:
    """给「谁更容易被猜到」打分：传奇履历 + 豪门（退役不再额外加分）。"""
    return (
        1 if (player.get("ucl") or 0) > 0 else 0,
        1 if (player.get("wcTitle") or 0) > 0 else 0,
        int(player.get("wc") or 0),
        1 if player.get("club") in BIG_CLUBS else 0,
        int(player.get("ucl") or 0),
    )


FOOTBALL_EASY_LIMIT = 250
FOOTBALL_RETIRED_CAP = 60  # 易档里最多留多少个退役传奇，早退役又没名气的自然挤不进来


def _active_key(player: dict) -> tuple:
    """现役球员的热度：进过名单 > 豪门 > 有欧冠 > 世界杯履历。"""
    return (
        1 if player.get("cnName") in FAMOUS else 0,
        1 if player.get("club") in BIG_CLUBS else 0,
        1 if (player.get("ucl") or 0) > 0 else 0,
        1 if (player.get("wcTitle") or 0) > 0 else 0,
        int(player.get("wc") or 0),
        int(player.get("ucl") or 0),
    )


def _retired_key(player: dict) -> tuple:
    """退役球员的热度：名字在名单里 > 世界杯冠军 > 欧冠 > 世界杯出场；同分时近代的靠前。"""
    return (
        1 if player.get("cnName") in FAMOUS else 0,
        int(player.get("wcTitle") or 0),
        int(player.get("ucl") or 0),
        int(player.get("wc") or 0),
        1 if int(player.get("birthYear") or 0) >= 1970 else 0,
    )


def _famous_pool(players: list[dict], limit: int = 200) -> list[dict]:
    """最出名的一批：名单内的优先，不够再按履历补齐。"""
    picked = [p for p in players if p.get("cnName") in FAMOUS]
    if len(picked) < limit:
        chosen = {p["cnName"] for p in picked}
        rest = sorted(
            (p for p in players if p.get("cnName") not in chosen),
            key=_fame_score,
            reverse=True,
        )
        picked.extend(rest[: limit - len(picked)])
    return picked[:limit]


def _easy_pool(
    players: list[dict], limit: int = FOOTBALL_EASY_LIMIT, retired_cap: int = FOOTBALL_RETIRED_CAP
) -> list[dict]:
    """「易」档：现役优先，退役只留最有名的一批。

    以前是把退役也一起按履历补，结果 300 人里塞了 113 个老将，
    连几十年前退役、年轻玩家不认识的也进来了。现在退役单独限额，
    按「世界杯冠军 → 欧冠 → 世界杯出场」排序取前 60，剩下的名额全给现役。
    """
    active = [p for p in players if p.get("league") != "已退役"]
    retired = [p for p in players if p.get("league") == "已退役"]

    keep_ret = sorted(retired, key=_retired_key, reverse=True)[:retired_cap]
    slots = max(0, limit - len(keep_ret))
    out = sorted(active, key=_active_key, reverse=True)[:slots] + keep_ret

    if len(out) < limit:  # 人不够就把剩下的现役按履历补进来
        chosen = {p.get("cnName") for p in out}
        more = [p for p in sorted(players, key=_active_key, reverse=True) if p.get("cnName") not in chosen]
        out.extend(more[: limit - len(out)])
    return out[:limit]


def _league_easy(pool: list[dict], famous: list[dict], limit: int = 150) -> list[dict]:
    """单个联赛的「易」：该联赛知名球员 ∪ 豪门球员，最多 limit 人；不足 40 按履历补齐。"""
    if not pool:
        return pool

    names = {p.get("cnName") for p in pool}
    seen: set[str] = set()
    picked: list[dict] = []
    for p in famous:
        key = p.get("cnName")
        if p.get("league") in {q.get("league") for q in pool} and key in names and key not in seen:
            seen.add(key)
            picked.append(p)
    for p in pool:
        key = p.get("cnName")
        if p.get("club") in BIG_CLUBS and key not in seen:
            seen.add(key)
            picked.append(p)

    if len(picked) < 40:  # 联赛太小或豪门太少，按履历补，避免反复出同一人
        for p in sorted(pool, key=_fame_score, reverse=True):
            if len(picked) >= min(40, len(pool)):
                break
            key = p.get("cnName")
            if key not in seen:
                seen.add(key)
                picked.append(p)
    return picked[:limit] or pool


VALORANT_POPULAR_LIMIT = 150
VALORANT_STATUS_CN = {"现役": "现役", "下放或替补": "自由人/替补", "退役": "退役", "未知": "未知"}


def _valorant_status(player: dict) -> str:
    """展示用的身份：自由人单独标出来。"""
    status = player.get("_status") or "未知"
    if status == "下放或替补" and player.get("freeAgent"):
        return "自由人"
    if status == "下放或替补":
        return "下放或替补"
    return VALORANT_STATUS_CN.get(status, status)


def _valorant_popular(players: list[dict], limit: int = VALORANT_POPULAR_LIMIT) -> list[dict]:
    """热门档：站点热门池里的退役/自由人全收，其余按站点热度分补到 limit 人。"""
    pool = [p for p in players if p.get("popular")]
    must = [p for p in pool if p.get("_status") == "退役" or p.get("freeAgent")]
    rest = sorted(
        (p for p in pool if p not in must),
        key=lambda p: (
            -int(p.get("popularScore") or 0),
            -int(p.get("championsTitle") or 0),
            -int(p.get("mastersTitle") or 0),
            -int(p.get("championsApp") or 0),
            -int(p.get("mastersApp") or 0),
            p["gameId"].lower(),
        ),
    )
    picked = must + rest[: max(0, limit - len(must))]
    return picked[:limit] or pool


class ValorantTheme(Theme):
    """瓦罗兰特（无畏契约）职业选手，数据来自 wayiba.com。"""

    key = "valorant"
    command = "瓦一把"
    aliases = ("猜瓦", "瓦罗兰特", "valorant")
    label = "瓦罗兰特选手"
    icon = "🎯"
    db_file = "valorant_players.json"
    attempts = 8
    default_pool = "easy"
    pool_alias = {
        "易": "easy", "简单": "easy", "热门": "easy", "easy": "easy",
        "中": "normal", "普通": "normal", "标准": "normal", "normal": "normal",
        "难": "hard", "全部": "hard", "全量": "hard", "地狱": "hard", "hard": "hard",
    }
    difficulty_hint = "易 / 中 / 难"

    def load(self, raw: Any) -> list[dict]:
        players = raw.get("players") if isinstance(raw, dict) else raw
        out = []
        for p in players or []:
            if not p.get("gameId"):
                continue
            p = dict(p)
            p["_roles"] = _as_list(p.get("roles"))
            p["_team"] = str(p.get("team") or "无队伍")
            p["_tier"] = str(p.get("teamTier") or "")
            p["_status"] = str(p.get("status") or "未知")
            out.append(p)
        return out

    def keys_of(self, player: dict) -> set[str]:
        keys = {player.get("gameId", ""), player.get("cnName", ""), player.get("realName", "")}
        keys.update(player.get("aliases") or [])
        return {k for k in keys if k}

    def title_of(self, player: dict) -> str:
        return player["gameId"]

    def pools(self, players: list[dict]) -> dict[str, list[dict]]:
        hot = _valorant_popular(players)
        intl = [
            p
            for p in players
            if (p.get("mastersApp") or 0) > 0 or (p.get("championsApp") or 0) > 0
        ]
        return {
            "easy": hot or players,
            "normal": intl or players,
            "hard": players,
        }

    def pool_label(self, pool: str) -> str:
        return {
            "easy": "易（热门 150 人）",
            "normal": "中（国际赛）",
            "hard": "难（全量）",
        }.get(pool, pool)

    @staticmethod
    def _identity_state(player: dict) -> tuple[str, str]:
        status = player.get("_status") or "未知"
        if status == "退役":
            return status, ""
        return status, player.get("_tier") or ""

    def judge(self, guess: dict, target: dict) -> list[Cell]:
        cells: list[Cell] = []

        # 国籍：相同一致；不同但同大洲接近
        g_nat, t_nat = guess.get("nationality", "—"), target.get("nationality", "—")
        if g_nat == t_nat and g_nat != "未知":
            cells.append(Cell("国籍", g_nat, "correct"))
        elif guess.get("continent") and guess.get("continent") == target.get("continent"):
            cells.append(Cell("国籍", g_nat, "close"))
        else:
            cells.append(Cell("国籍", g_nat, "wrong"))

        # 赛区：只有一致 / 不一致
        g_region, t_region = guess.get("region", "—"), target.get("region", "—")
        cells.append(Cell("赛区", g_region, "correct" if g_region == t_region else "wrong"))

        # 战队：相同一致；不同但同赛区接近
        g_team, t_team = guess.get("_team", "无队伍"), target.get("_team", "无队伍")
        if g_team == t_team:
            cells.append(Cell("战队", g_team, "correct"))
        elif g_region == t_region:
            cells.append(Cell("战队", g_team, "close"))
        else:
            cells.append(Cell("战队", g_team, "wrong"))

        # 身份：状态 + 是否一线队，两项都对才算一致，对一项算接近
        g_state, t_state = self._identity_state(guess), self._identity_state(target)
        if "退役" in (g_state[0], t_state[0]):
            level = "correct" if g_state[0] == t_state[0] else "wrong"
        else:
            hits = int(g_state[0] == t_state[0]) + int(bool(g_state[1]) and g_state[1] == t_state[1])
            level = "correct" if hits == 2 else ("close" if hits == 1 else "wrong")
        cells.append(Cell("身份", _valorant_status(guess), level))

        # 职责：集合相同一致，有交集接近
        g_roles = set(guess.get("_roles") or [])
        t_roles = set(target.get("_roles") or [])
        if g_roles and g_roles == t_roles:
            level = "correct"
        elif g_roles & t_roles:
            level = "close"
        else:
            level = "wrong"
        cells.append(Cell("职责", "/".join(guess.get("_roles") or ["—"]), level))

        # 年龄：只比大小
        cells.append(_diff_cell("年龄", int(guess.get("age") or 0), int(target.get("age") or 0), 0))
        # 大师赛参赛 ±2，冠军赛参赛 ±1
        cells.append(_diff_cell("大师赛参赛", int(guess.get("mastersApp") or 0), int(target.get("mastersApp") or 0), 2))
        cells.append(
            _diff_cell("冠军赛参赛", int(guess.get("championsApp") or 0), int(target.get("championsApp") or 0), 1)
        )
        # 冠军数必须一致
        cells.append(_diff_cell("大师赛冠军", int(guess.get("mastersTitle") or 0), int(target.get("mastersTitle") or 0), 0))
        cells.append(
            _diff_cell("冠军赛冠军", int(guess.get("championsTitle") or 0), int(target.get("championsTitle") or 0), 0)
        )
        return cells

    def card(self, player: dict) -> str:
        title = player["gameId"]
        extra = player.get("cnName") or player.get("realName") or ""
        head = f"📇 {title}（{_valorant_status(player)}）"
        # 真名/中文名单独占一项，名字长也不会把标题撑到折行
        facts = [
            extra,
            player.get("_team") or "",
            player.get("region") or "",
            "/".join(player.get("_roles") or ["—"]),
            f"{player.get('age', '—')} 岁",
        ]
        honors = []
        if player.get("mastersTitle"):
            honors.append(f"大师赛冠军 {player['mastersTitle']}")
        if player.get("championsTitle"):
            honors.append(f"冠军赛冠军 {player['championsTitle']}")
        body = _wrap_join([x for x in facts if x])
        honor_lines = _wrap_join(honors)
        if honor_lines:
            body.append("🏆 " + honor_lines[0])
            body.extend(honor_lines[1:])
        return "\n".join([head, *body])

    def hints(self, player: dict) -> list[tuple[str, str]]:
        return [
            ("职责", "/".join(player.get("_roles") or ["—"])),
            ("赛区", player.get("region") or "—"),
            ("国籍", player.get("nationality") or "—"),
            ("年龄", f"{player.get('age', '—')} 岁"),
            ("身份", _valorant_status(player)),
            ("大师赛参赛", f"{player.get('mastersApp', 0)} 次"),
            ("冠军赛参赛", f"{player.get('championsApp', 0)} 次"),
        ]


def _cs_fame_score(player: dict) -> tuple:
    """CS 选手的「火」程度：Major 冠军 > 出场数 > 现役 > 是否在名人名单。"""
    return (
        1 if (player.get("_base") or "").lower() in CS_FAMOUS else 0,
        int(player.get("major_championships") or 0),
        int(player.get("major_appearances") or 0),
        1 if player.get("is_active") else 0,
    )


def _cs_famous_pool(players: list[dict], limit: int = 200) -> list[dict]:
    """最火的 200 人：名人名单打底，再按 Major 履历补齐；教练不作为答案。"""
    roster = [p for p in players if p.get("role") != "Coach"]
    picked = [p for p in roster if (p.get("_base") or "").lower() in CS_FAMOUS]
    if len(picked) >= limit:
        picked.sort(key=_cs_fame_score, reverse=True)
        return picked[:limit]

    seen = {p["nickname"] for p in picked}
    # 补齐只看 Major 履历，不做主观「冷门」判断
    rest = sorted(
        (p for p in roster if p["nickname"] not in seen),
        key=_cs_fame_score,
        reverse=True,
    )
    return picked + rest[: limit - len(picked)]


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value:
        return [value]
    return []


THEMES: dict[str, Theme] = {
    t.key: t for t in (CSTheme(), FootballTheme(), LolTheme(), ValorantTheme())
}


# ============================================================
# 选手库
# ============================================================


class PlayerDB:
    """某个题库的选手数据：加载、检索、按难度取池。"""

    def __init__(self, theme: Theme, path: Path) -> None:
        self.theme = theme
        self.path = path
        self.version = ""
        self.players: list[dict] = []
        self._index: dict[str, list[dict]] = {}
        self._pools: dict[str, list[dict]] = {}
        self._ambiguous: set[str] = set()
        self.alias_version = ""
        self.alias_report: dict[str, Any] = {"added": 0, "conflicts": [], "orphans": []}
        self.single_char_aliases: set[str] = set()
        self._preferred: dict[str, dict] = {}  # 外号表显式点名的，优先于同名选手
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(f"[{PLUGIN_NAME}] 题库 {self.path.name} 读取失败：{exc}")
            self.players, self._index, self._pools = [], {}, {}
            return
        self.version = str(raw.get("version") or "") if isinstance(raw, dict) else ""
        self.players = self.theme.load(raw)
        self._index = {}
        for player in self.players:
            for key in self.theme.keys_of(player):
                normalized = normalize_name(key)
                if normalized:
                    self._index.setdefault(normalized, []).append(player)
        self._pools = self.theme.pools(self.players)
        self._load_aliases()
        # 同名选手（如 LoL 里两个 Ace）不能当答案：否则猜中了也可能判到另一个人身上
        counter: dict[str, int] = {}
        for player in self.players:
            title = self.theme.title_of(player)
            counter[title] = counter.get(title, 0) + 1
        self._ambiguous = {title for title, n in counter.items() if n > 1}
        logger.info(
            f"[{PLUGIN_NAME}] {self.theme.label}题库加载 {len(self.players)} 人（{self.version or '未知版本'}）"
            + (f"，其中 {len(self._ambiguous)} 个同名不参与出题" if self._ambiguous else "")
        )

    @staticmethod
    def _prominence(player: dict) -> tuple:
        """同名时优先现役、履历更厚的那个。"""
        return (
            0 if str(player.get("is_active", player.get("status", "Active"))).lower() not in ("false", "retired") else 1,
            -int(player.get("major_appearances") or player.get("worldsCount") or 0),
            len(str(player.get("nickname") or player.get("name") or player.get("gameId") or "")),
        )

    def _locate(self, target: str) -> dict | None:
        """按 title / 昵称 / 主体名找选手，同名时取最显眼的那个。"""
        found = self.get(target)
        if found:
            return found
        hits = self._index.get(normalize_name(target)) or []
        if not hits:
            return None
        return sorted(hits, key=self._prominence)[0]

    def _load_aliases(self) -> None:
        """把外号挂进检索索引，这样「虾哥」「载物」也能当猜测提交。

        外号撞上别人的正式昵称时直接跳过——宁可少一条，也不要把两个人的判断搅混。
        """
        name = getattr(self.theme, "alias_file", "")
        if not name:
            return
        path = self.path.parent / name
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 外号表 {name} 读取失败：{exc}")
            return
        mapping = raw.get("players") if isinstance(raw, dict) else raw
        if not isinstance(mapping, dict):
            return
        self.alias_version = str(raw.get("version") or "") if isinstance(raw, dict) else ""
        self.single_char_aliases = set()
        self._preferred = {}
        added, conflicts, orphans = 0, [], []
        for target, names in mapping.items():
            player = self._locate(target)
            if not player:
                orphans.append(target)
                continue
            aliases = [names] if isinstance(names, str) else list(names or [])
            for alias in aliases:
                key = normalize_name(str(alias))
                if not key:
                    continue
                bucket = self._index.get(key)
                if bucket is None:
                    self._index[key] = [player]
                    added += 1
                    if len(key) == 1:
                        self.single_char_aliases.add(key)
                elif player in bucket:
                    # 同一个名字对得上多人（niko 有波黑和丹麦两位）时，以外号表点名的为准
                    self._preferred[key] = player
                else:
                    conflicts.append(f"{alias}→{target}")
        if added or conflicts or orphans:
            logger.info(
                f"[{PLUGIN_NAME}] {self.theme.label}外号 {added} 条"
                + (f"，{len(conflicts)} 条与昵称冲突已跳过" if conflicts else "")
                + (f"，{len(orphans)} 个选手没对上" if orphans else "")
            )
        self.alias_report = {"added": added, "conflicts": conflicts, "orphans": orphans}

    def pool(self, name: str) -> list[dict]:
        players = self._pools.get(name) or self.players
        return players or self.players

    def pool_size(self, name: str) -> int:
        """开局文案里的人数：按真正能出题的候选算（同名选手已排除）。"""
        return len(self.answer_pool(name))

    def match(self, query: str) -> tuple[str, list[dict]]:
        """检索，返回 (匹配类型, 候选)。类型：exact / prefix / contains / none。"""
        key = normalize_name(query)
        if not key:
            return "none", []
        exact = self._index.get(key)
        if exact:
            ordered = sorted(exact, key=self._prominence)
            preferred = self._preferred.get(key)
            if preferred is not None and any(p is preferred for p in ordered):
                ordered = [preferred] + [p for p in ordered if p is not preferred]
            return "exact", ordered[:5]

        starts, contains = [], []
        for player in self.players:
            names = [normalize_name(k) for k in self.theme.keys_of(player)]
            if any(n.startswith(key) for n in names if n):
                starts.append(player)
            elif any(key in n for n in names if n):
                contains.append(player)
        if starts:
            return "prefix", sorted(starts, key=self._prominence)[:5]
        if contains:
            return "contains", sorted(contains, key=self._prominence)[:5]
        return "none", []

    def find(self, query: str) -> list[dict]:
        return self.match(query)[1]

    def preferred(self, query: str) -> dict | None:
        """外号表显式点名的那个（比如 niko 指向波黑的 NiKo，而不是丹麦同名选手）。"""
        return self._preferred.get(normalize_name(query))

    def answer_pool(self, name: str) -> list[dict]:
        """出题候选：去掉同名选手，保证答案能被唯一判定。"""
        return [p for p in self.pool(name) if self.theme.title_of(p) not in self._ambiguous]

    def get(self, title: str) -> dict | None:
        for player in self.players:
            if self.theme.title_of(player) == title:
                return player
        return None


# ============================================================
# 对局
# ============================================================


@dataclass
class Game:
    key: str  # 会话 + 题库
    theme: str
    umo: str
    group_id: str
    user_id: str
    user_name: str
    target: str  # 答案的 title_of
    pool: str
    attempts: int = 8
    created_at: float = field(default_factory=time.time)
    guessed: list[str] = field(default_factory=list)
    board_history: list[dict[str, Any]] = field(default_factory=list)  # 每次猜测时的判定快照
    revealed: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None
    # 组队局：同一个群共享一个答案。solo 时这些字段都是空的
    mode: str = "solo"  # solo / team
    members: list[dict[str, str]] = field(default_factory=list)  # [{"id","name"}]
    started: bool = True
    guessed_by: list[str] = field(default_factory=list)  # 每次猜的人名，和 guessed 一一对应
    team_size: int = 0  # 房间想凑几个人（0 = 不限，凑满 TEAM_MAX）
    turn: int = 0  # 回合制：轮到第几个队员
    # 双人赛制：best_of=3 表示三局两胜；0 表示一局定胜负
    best_of: int = 0
    round_no: int = 1  # 当前第几局（仅 BO3 会变）
    scores: dict[str, int] = field(default_factory=dict)  # uid → 本场已赢局数
    hint_used: dict[str, int] = field(default_factory=dict)  # uid → 本局已用提示次数
    guesses_done: int = 0  # 已结束的局累计用掉的猜测次数（结算用）

    @property
    def is_team(self) -> bool:
        return self.mode == "team"

    @property
    def is_bo3(self) -> bool:
        return self.best_of >= 3 and self.is_team

    def has(self, user_id: str) -> bool:
        return any(m.get("id") == user_id for m in self.members)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "theme": self.theme,
            "umo": self.umo,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "target": self.target,
            "pool": self.pool,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "guessed": self.guessed,
            "board_history": self.board_history,
            "revealed": self.revealed,
            "mode": self.mode,
            "members": self.members,
            "started": self.started,
            "guessed_by": self.guessed_by,
            "team_size": self.team_size,
            "turn": self.turn,
            "best_of": self.best_of,
            "round_no": self.round_no,
            "scores": self.scores,
            "hint_used": self.hint_used,
            "guesses_done": self.guesses_done,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Game":
        return cls(
            key=data["key"],
            theme=data.get("theme", "cs"),
            umo=data["umo"],
            group_id=data.get("group_id", ""),
            user_id=data.get("user_id", ""),
            user_name=data.get("user_name", ""),
            target=data.get("target", ""),
            pool=data.get("pool", "normal"),
            attempts=int(data.get("attempts") or 8),
            created_at=data.get("created_at", time.time()),
            guessed=list(data.get("guessed") or []),
            board_history=list(data.get("board_history") or []),
            revealed=list(data.get("revealed") or []),
            mode=data.get("mode", "solo"),
            members=list(data.get("members") or []),
            started=bool(data.get("started", True)),
            guessed_by=list(data.get("guessed_by") or []),
            team_size=int(data.get("team_size") or 0),
            turn=int(data.get("turn") or 0),
            best_of=int(data.get("best_of") or 0),
            round_no=int(data.get("round_no") or 1),
            scores={str(k): int(v) for k, v in (data.get("scores") or {}).items()},
            hint_used={str(k): int(v) for k, v in (data.get("hint_used") or {}).items()},
            guesses_done=int(data.get("guesses_done") or 0),
        )


class SessionKey:
    """会话键：群内每人一份，私聊单独一份。"""

    @staticmethod
    def user(group_id: str, user_id: str) -> str:
        return f"{group_id or 'private'}:{user_id}"

    @staticmethod
    def game(group_id: str, user_id: str, theme: str) -> str:
        return f"{group_id or 'private'}:{user_id}:{theme}"

    @staticmethod
    def stat(group_id: str, user_id: str, theme: str) -> str:
        return f"{group_id or 'private'}:{user_id}:{theme}"

    @staticmethod
    def team(group_id: str, theme: str) -> str:
        """组队局：同一个群、同一个题库共用一个房间。"""
        return f"{group_id or 'private'}:team:{theme}"

# ============================================================
# 文本入口解析
# ============================================================

_START_PATTERNS: list[tuple[re.Pattern[str], str]] = []
for _key, _theme in THEMES.items():
    _names = "|".join(re.escape(n) for n in (_theme.command, *_theme.aliases))
    _START_PATTERNS.append((re.compile(rf"^[/／]?(?:{_names})(?:\s+(.+))?$", re.I), _key))

_LOOKUP_RE = re.compile(r"^[/／]?(?:图鉴|查选手|查球员|查资料)\s*(\S+)?$")
_LEADING_RE = re.compile(r"^[/／]?(?:我猜|猜一下|猜|guess|answer|是)\s*", re.I)
_TRAILING_RE = re.compile(r"(?:吧|呢|啊|呀|吗|了)+$")

# 组队只在开局词里显式带「双人 / 多人」时触发，其余一律独猜
_TEAM_WORDS = ("双人", "多人")
TEAM_MAX = 6  # 一个房间最多几个人
TEAM_LOBBY_SECONDS = 300  # 房间最多等人等多久
_MODE_RE = re.compile(rf"^({'|'.join(_TEAM_WORDS)})\s*(\d+)?\s*(.*)$")
# 双人赛制：三局两胜。写成 bo3 / BO3 / 三局两胜 / best of 3 都认
_BO3_RE = re.compile(r"(?<![a-z0-9])(?:bo3|best\s*of\s*3|三局两胜)(?![a-z0-9])", re.I)


def _split_mode(raw: str) -> tuple[str, int, int, str]:
    """把「双人」「多人 4 难」「双人 bo3」拆成 (模式, 人数, 赛制, 剩余难度词)。"""
    text = (raw or "").strip()
    head = _MODE_RE.match(text)
    if not head:
        best_of = 3 if _BO3_RE.search(text) else 0
        rest = _BO3_RE.sub(" ", text).strip() if best_of else text
        return "solo", 0, best_of, rest
    word, num, rest = head.group(1), head.group(2), (head.group(3) or "").strip()
    size = int(num) if num else (2 if word == "双人" else 0)
    best_of = 3 if _BO3_RE.search(rest) else 0
    if best_of:
        rest = _BO3_RE.sub(" ", rest).strip()
    return "team", size, best_of, rest


# 不带唤醒前缀时也能用的口令 → 插件方法名
_SILENT_WITHOUT_GAME = {"_hint_text", "_giveup_text", "_join_team", "_start_team"}
_BARE_COMMANDS = {
    "提示": "_hint_text",
    "线索": "_hint_text",
    "放弃": "_giveup_text",
    "结束": "_giveup_text",
    "认输": "_giveup_text",
    "战绩": "_stats_text",
    "排行": "_rank_text",
    "排行榜": "_rank_text",
    "加入": "_join_team",
    "加我": "_join_team",
    "开始": "_start_team",
}


# ============================================================
# 插件
# ============================================================


class GuessProPlugin(Star):
    """猜选手 · 多题库

    群里发「弗一把」/「足一把」/「撸一把」/「瓦一把」开局，然后直接发选手名开猜。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config or {}
        # 主题是类，开关统一挂在基类上
        Theme.history_team = bool(self._cfg("history_team", False))
        self.dbs: dict[str, PlayerDB] = {
            key: PlayerDB(theme, DATA_DIR / theme.db_file) for key, theme in THEMES.items()
        }
        self.games: dict[str, Game] = {}
        self.active: dict[str, str] = {}  # 会话键 → 题库_key
        self.stats: dict[str, dict[str, dict[str, Any]]] = {"users": {}, "groups": {}}
        self._lock = asyncio.Lock()
        self.data_dir = self._resolve_data_dir()
        self.state_file = self.data_dir / "state.json"
        self._load_state()
        self._restart_timers()

    # ---------------- 基础设施 ----------------

    def _cfg(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default) if isinstance(self.config, dict) else default
        return default if value is None else value

    def _enabled(self, theme_key: str) -> bool:
        return bool(self._cfg(f"enable_{theme_key}", True))

    def _session_ok(self, event: AstrMessageEvent) -> bool:
        """配置了「仅私聊」时，群聊里完全不响应。"""
        if not self._cfg("private_only", False):
            return True
        return not bool(event.get_group_id())

    def _attempts(self, theme: Theme) -> int:
        configured = int(self._cfg("max_attempts", 0) or 0)
        return configured if configured > 0 else theme.attempts

    def _resolve_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = Path(get_astrbot_data_path())
        except Exception:  # pragma: no cover
            import tempfile

            base = Path(tempfile.gettempdir()) / "astrbot"
        path = base / "plugin_data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取存档失败，已忽略：{exc}")
            return
        for item in data.get("games") or []:
            with contextlib.suppress(Exception):
                game = Game.from_dict(item)
                self.games[game.key] = game
        for ukey, theme_key in (data.get("active") or {}).items():
            if theme_key in THEMES:
                self.active[ukey] = theme_key
        stats = data.get("stats") or {}
        self.stats = {"users": stats.get("users") or {}, "groups": stats.get("groups") or {}}

    def _save_state(self) -> None:
        payload = {
            "games": [g.to_dict() for g in self.games.values()],
            "active": self.active,
            "stats": self.stats,
        }
        tmp = self.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)
        except Exception as exc:
            logger.warning(f"[{PLUGIN_NAME}] 保存存档失败：{exc}")

    def _spawn_timer(self, game: Game) -> None:
        minutes = int(self._cfg("timeout_minutes", 30) or 0)
        if minutes <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 非事件循环上下文（例如单元测试）
            return
        game.task = loop.create_task(self._timeout_watch(game, minutes * 60))

    def _restart_timers(self) -> None:
        for game in list(self.games.values()):
            if self.games.get(game.key) is game:
                self._spawn_timer(game)

    async def _timeout_watch(self, game: Game, seconds: int) -> None:
        deadline = game.created_at + seconds
        try:
            await asyncio.sleep(max(1.0, deadline - time.time()))
        except asyncio.CancelledError:  # pragma: no cover
            return
        if self.games.get(game.key) is not game:
            return
        self.games.pop(game.key, None)
        self._settle_stats(game, won=False)
        self._save_state()
        theme = THEMES.get(game.theme)
        db = self.dbs.get(game.theme)
        target = db.get(game.target) if db else None
        title = theme.label if theme else "对局"
        what = f"第 {game.round_no} 局" if game.is_bo3 else "这一局"
        extra = f"（比分 {self._bo3_score_text(game)}）\n" if game.is_bo3 else ""
        text = (
            f"⏳ 时间到，{title}{what}结束。{extra}答案：{game.target}\n"
            + (theme.card(target) + "\n" if theme and target else "")
            + f"发送「{theme.command if theme else '弗一把'}」再来一局。"
        )
        with contextlib.suppress(Exception):
            await self.context.send_message(game.umo, MessageChain().message(text))

    # ---------------- 战绩 ----------------

    def _settle_stats(
        self, game: Game, won: bool, winner_id: str = "", only_winner: bool = False
    ) -> None:
        # 组队局全队各记一局；猜中的那个人额外留个记号
        # only_winner=True 用于 BO3：只有赢下整场的那个人记胜
        members = (
            [m for m in game.members if m.get("id")]
            if game.is_team and game.members
            else [{"id": game.user_id, "name": game.user_name}]
        )
        for member in members:
            key = SessionKey.stat(game.group_id, member["id"], game.theme)
            user = self.stats["users"].setdefault(
                key, {"name": member.get("name", ""), "wins": 0, "plays": 0, "guesses": 0, "best": 0}
            )
            user["name"] = member.get("name") or user.get("name", "")
            user["plays"] = int(user.get("plays", 0)) + 1
            # 组队局记团队用了几次，和个人「最少 N 猜」的口径保持一致
            user["guesses"] = (
                int(user.get("guesses", 0))
                + int(getattr(game, "guesses_done", 0))
                + len(game.guessed)
            )
            if won and (not only_winner or member["id"] == winner_id):
                user["wins"] = int(user.get("wins", 0)) + 1
                best = len(game.guessed)
                if not user.get("best") or best < int(user["best"]):
                    user["best"] = best
        group = self.stats["groups"].setdefault(
            f"{game.group_id or 'private'}:{game.theme}", {"plays": 0, "wins": 0}
        )
        group["plays"] = int(group.get("plays", 0)) + 1
        if won:
            group["wins"] = int(group.get("wins", 0)) + 1

    def _user_stats_text(self, group_id: str, user_id: str, theme_key: str) -> str:
        theme = THEMES[theme_key]
        stat = self.stats["users"].get(SessionKey.stat(group_id, user_id, theme_key))
        if not stat or not stat.get("plays"):
            return f"还没有战绩，发「{theme.command}」开局。"
        plays = int(stat["plays"])
        wins = int(stat["wins"])
        avg = int(stat["guesses"]) / plays if plays else 0
        best = stat.get("best") or 0
        head = _wrap_join([f"📊 {stat.get('name', '你')}", f"{wins} 胜 / {plays} 局", f"胜率 {wins / plays:.0%}"])
        tail = f"平均 {avg:.1f} 猜" + (f" · 最少 {best} 猜命中" if best else "")
        return "\n".join([*head, tail])

    def _rank_board(self, group_id: str, theme_key: str) -> str:
        theme = THEMES[theme_key]
        gid = group_id or "private"
        prefix = f"{gid}:"
        suffix = f":{theme_key}"
        rows = [
            (value, key)
            for key, value in self.stats["users"].items()
            if key.startswith(prefix) and key.endswith(suffix) and int(value.get("wins", 0)) > 0
        ]
        if not rows:
            return f"还没有人猜中，发「{theme.command}」开局。"
        rows.sort(key=lambda item: (-int(item[0].get("wins", 0)), int(item[0].get("guesses", 0))))
        lines = [f"🏆 本群战绩榜 · {theme.label}"]
        for i, (stat, _key) in enumerate(rows[:10], start=1):
            plays = int(stat.get("plays", 0)) or 1
            avg = int(stat.get("guesses", 0)) / plays
            lines.append(f"{i}. {stat.get('name', '匿名')}　{int(stat.get('wins', 0))} 胜 · 平均 {avg:.1f} 猜")
        return "\n".join(lines)

    # ---------------- 开局 ----------------

    def _args_after_command(self, event: AstrMessageEvent, theme: Theme) -> str:
        """取命令词后面的全部文字，支持「英超 易」这种两个词。"""
        text = (event.message_str or "").strip()
        for name in sorted((theme.command, *theme.aliases), key=len, reverse=True):
            if text.startswith(name):
                return text[len(name) :].strip()
        return ""

    def _pk(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        gid, uid = event.get_group_id(), event.get_sender_id()
        return SessionKey.user(gid, uid), gid, uid

    def _game_of(self, event: AstrMessageEvent, theme_key: str) -> Game | None:
        _ukey, gid, uid = self._pk(event)
        return self.games.get(SessionKey.game(gid, uid, theme_key))

    def _play_game(self, event: AstrMessageEvent, theme_key: str) -> Game | None:
        """该用户在这个题库的对局：组队局优先（必须是队里的人），否则看自己的独猜。"""
        team = self._team_of(event, theme_key)
        if team and team.has(event.get_sender_id()):
            return team
        return self._game_of(event, theme_key)

    def _current_theme(self, event: AstrMessageEvent) -> str:
        """当前会话正在玩的题库；没有进行中的对局时回落到默认题库。"""
        ukey, _gid, uid = self._pk(event)
        theme_key = self.active.get(ukey)
        if theme_key and self._play_game(event, theme_key):
            return theme_key
        for key in THEMES:
            if self._play_game(event, key):
                self.active[ukey] = key
                return key
        team = self._team_of(event)
        if team and team.has(uid):
            self.active[ukey] = team.theme
            return team.theme
        return theme_key or "cs"

    def _begin(self, event: AstrMessageEvent, theme_key: str, raw_difficulty: str) -> str:
        theme = THEMES[theme_key]
        db = self.dbs[theme_key]
        if not self._enabled(theme_key):
            return f"{theme.label}题库已在配置里关闭。"
        if not db.players:
            if theme_key == "lol":
                return (
                    "撸一把的题库还没导入（data/lol_players.json）。\n"
                    "可以运行仓库里的 `python tools/crawl_lol.py` 抓一份，脚本支持中断续抓。"
                )
            return f"{theme.label}题库没加载成功，检查 data/{theme.db_file}。"

        mode, size, best_of, rest = _split_mode(raw_difficulty)
        if mode == "solo" and best_of:
            return f"BO3 是双人赛制：发「{theme.command} 双人 bo3」开一场三局两胜。"
        ukey, gid, uid = self._pk(event)
        default_pool = str(self._cfg("difficulty", "") or self._cfg("default_difficulty", "易"))
        pool = theme.resolve_pool(rest, default_pool)
        if not pool:
            return f"{theme.command}的难度只有：{theme.difficulty_hint}。"
        if not db.pool(pool):
            pool = theme.default_pool

        if mode == "team":
            return self._open_team(event, theme, pool, size, best_of)

        gkey = SessionKey.game(gid, uid, theme_key)
        if gkey in self.games:
            return f"这局{theme.label}还没结束呢，先猜，或者发「放弃」结束。"

        candidates = db.answer_pool(pool) or db.pool(pool)
        target = random.choice(candidates)
        game = Game(
            key=gkey,
            theme=theme_key,
            umo=event.unified_msg_origin,
            group_id=gid,
            user_id=uid,
            user_name=event.get_sender_name(),
            target=theme.title_of(target),
            pool=pool,
            attempts=self._attempts(theme),
        )
        self.games[gkey] = game
        self.active[ukey] = theme_key
        self._spawn_timer(game)
        self._save_state()
        return self._start_text(theme, db, game)

    # ---------------- 组队局 ----------------

    def _team_of(self, event: AstrMessageEvent, theme_key: str | None = None) -> Game | None:
        """群里进行中的组队局；theme_key 为空时返回任意一个。"""
        gid = event.get_group_id()
        if not gid:
            return None
        keys = [theme_key] if theme_key else list(THEMES)
        for key in keys:
            game = self.games.get(SessionKey.team(gid, key))
            if game and game.is_team:
                return game
        return None

    def _lobby_of(self, event: AstrMessageEvent, theme_key: str | None = None) -> Game | None:
        game = self._team_of(event, theme_key)
        if not game or game.started:
            return None
        # 大厅等太久就当散了，避免有人挂着「加入」永远不进
        if time.time() - game.created_at > TEAM_LOBBY_SECONDS:
            self.games.pop(game.key, None)
            self._save_state()
            return None
        return game

    def _open_team(
        self, event: AstrMessageEvent, theme: Theme, pool: str, size: int, best_of: int = 0
    ) -> str:
        gid = event.get_group_id()
        uid = event.get_sender_id()
        if self._cfg("solo_only", False):
            return f"当前只开独猜，发「{theme.command}」自己猜。"
        if not gid:
            return f"双人 / 多人只能在群里开，私聊发「{theme.command}」自己猜。"
        if size > TEAM_MAX:
            return f"最多 {TEAM_MAX} 人。"
        if size == 1:
            return f"一个人就发「{theme.command}」，不用带「双人」。"
        if best_of and size != 2:
            return f"BO3 只支持双人赛：发「{theme.command} 双人 bo3」。"

        tkey = SessionKey.team(gid, theme.key)
        exist = self.games.get(tkey)
        if exist and exist.is_team:
            if exist.started:
                return (
                    f"群里已经有一局{theme.label}组队局在打了（{len(exist.members)} 人）。\n"
                    f"发「加入」参与，或等它结束。"
                )
            names = "、".join(m.get("name") or "某人" for m in exist.members)
            return f"房间已经开着了（{len(exist.members)} 人：{names}）。\n发「加入」进队。"

        name = event.get_sender_name() or "房主"
        game = Game(
            key=tkey,
            theme=theme.key,
            umo=event.unified_msg_origin,
            group_id=gid,
            user_id=uid,
            user_name=name,
            target="",
            pool=pool,
            attempts=0,
            mode="team",
            members=[{"id": uid, "name": name}],
            started=False,
            team_size=size,
            best_of=best_of,
        )
        self.games[tkey] = game
        self.active[SessionKey.user(gid, uid)] = theme.key
        self._save_state()
        want = f"要 {size} 人" if size else f"最多 {TEAM_MAX} 人"
        how = f"凑齐 {size} 人自动开局" if size else "人齐后房主发「开始」"
        rules = " · 三局两胜" if best_of else ""
        duel = (
            f"本局先手没提示、后手 {self._duel_hint()} 次（每局换个先手）\n"
            if best_of
            else ""
        )
        return (
            f"👥 {theme.command} · 组队局（{want}{rules}）\n"
            f"难度：{theme.pool_label(pool)}\n"
            f"房主：{name}\n"
            f"别人发「加入」进队\n"
            f"{how}\n"
            f"{duel}"
            f"{LEGEND}"
        )

    def _join_team(self, event: AstrMessageEvent) -> str:
        if self._cfg("solo_only", False):
            return "当前只开独猜。"
        gid = event.get_group_id()
        if not gid:
            return "双人 / 多人只能在群里玩。"
        game = self._lobby_of(event)
        if not game:
            running = self._team_of(event)
            if running:
                return f"这局{THEMES[running.theme].label}组队局已经开始了，等下一局吧。"
            return "现在没有在招人的房间，房主发「弗一把 双人」开一个。"
        uid = event.get_sender_id()
        if game.has(uid):
            return "你已经在队里了。"
        if len(game.members) >= TEAM_MAX:
            return f"房间满了（{TEAM_MAX} 人）。"
        name = event.get_sender_name() or "某人"
        game.members.append({"id": uid, "name": name})
        self._save_state()
        names = "、".join(m.get("name") or "某人" for m in game.members)
        # 双人房这种人头固定的，凑齐就直接开
        if len(game.members) >= 2 and len(game.members) >= self._team_target(game):
            return self._launch_team(game)
        return (
            f"✅ {name} 加入（{len(game.members)} 人）\n"
            f"当前：{names}\n"
            f"房主发「开始」就出题。"
        )

    @staticmethod
    def _team_target(game: Game) -> int:
        """房间想凑几个人；「多人」没指定就是 TEAM_MAX。"""
        return game.team_size or TEAM_MAX

    def _duel_hint(self) -> int:
        """双人局后手每局的提示次数（先手固定 0 次）。"""
        try:
            return max(0, int(self._cfg("duel_hint", 2) or 0))
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def _first_index(game: Game) -> int:
        """本局先手：BO3 每局换一个人先，其余赛制固定在队首。"""
        if not game.members:
            return 0
        if not game.best_of:
            return 0
        return (max(1, game.round_no) - 1) % len(game.members)

    def _first_member(self, game: Game) -> dict | None:
        if not game.members:
            return None
        return game.members[self._first_index(game)]

    def _round_brief(self, game: Game) -> list[str]:
        """双人局的先后手提示额度说明（多人局不适用）。"""
        if not game.is_team or len(game.members) != 2:
            return []
        first = game.members[self._first_index(game)]
        second = game.members[(self._first_index(game) + 1) % 2]
        quota = self._duel_hint()
        return [
            f"本局先手：{first.get('name') or '某人'}（没提示）",
            f"本局后手：{second.get('name') or '某人'}（{quota} 次提示）",
        ]

    def _start_team(self, event: AstrMessageEvent) -> str:
        gid = event.get_group_id()
        if not gid:
            return "双人 / 多人只能在群里玩。"
        game = self._lobby_of(event)
        if not game:
            running = self._team_of(event)
            if running:
                return f"这局{THEMES[running.theme].label}组队局已经在打了。"
            return "没有等待开始的房间。"
        if game.user_id != event.get_sender_id():
            return f"只有房主（{game.user_name}）能开始。"
        if len(game.members) < 2:
            return "至少 2 个人，再等等。"
        return self._launch_team(game)

    def _launch_team(self, game: Game) -> str:
        gid = game.group_id
        theme = THEMES[game.theme]
        db = self.dbs[game.theme]
        pool = game.pool if db.pool(game.pool) else theme.default_pool
        candidates = db.answer_pool(pool) or db.pool(pool)
        game.target = theme.title_of(random.choice(candidates))
        game.pool = pool
        game.attempts = self._team_attempts(theme, len(game.members))
        game.started = True
        game.created_at = time.time()
        game.round_no = 1
        game.scores = {m["id"]: 0 for m in game.members if m.get("id")}
        game.hint_used = {}
        game.guesses_done = 0
        game.turn = self._first_index(game)
        self._spawn_timer(game)
        for member in game.members:
            self.active[SessionKey.user(gid, member["id"])] = game.theme
        self._save_state()
        order = " → ".join(m.get("name") or "某人" for m in game.members)
        lines = [
            f"👥 {theme.icon} {theme.command} · 组队局开始",
            f"{len(game.members)} 人回合制：{order}",
        ]
        if game.is_bo3:
            lines.append(f"赛制：三局两胜（先赢 {game.best_of // 2 + 1} 局）")
        lines.append(f"难度：{theme.pool_label(pool)}")
        lines.append(
            f"机会：{game.attempts} 次（每局重置）"
            if game.is_bo3
            else f"机会：{game.attempts} 次（全队共用）"
        )
        lines.extend(self._round_brief(game))
        lines.append(
            "回合制轮流猜，谁猜中谁拿这一局"
            if game.is_bo3
            else "依次猜，猜中全队记一胜"
        )
        lines.append(f"👉 先由 {game.members[self._first_index(game)].get('name') or '房主'} 猜")
        lines.append("指令 提示 放弃 战绩")
        lines.append(LEGEND)
        return "\n".join(lines)

    @staticmethod
    def _current_member(game: Game) -> dict | None:
        if not game.members:
            return None
        return game.members[game.turn % len(game.members)]

    def _team_attempts(self, theme: Theme, size: int) -> int:
        base = self._attempts(theme)
        return min(base + 8, base + 2 * max(0, size - 1))

    # ---------------- 双人 BO3 ----------------

    @staticmethod
    def _bo3_score_text(game: Game) -> str:
        return " : ".join(
            f"{m.get('name') or '某人'} {int(game.scores.get(m.get('id'), 0))}"
            for m in game.members
        )

    def _reset_round(self, game: Game) -> None:
        """BO3 开下一局：换答案、清棋盘、重置机会，先手按局数轮换。"""
        theme = THEMES[game.theme]
        db = self.dbs[game.theme]
        candidates = db.answer_pool(game.pool) or db.pool(game.pool)
        game.target = theme.title_of(random.choice(candidates))
        game.guessed = []
        game.board_history = []
        game.guessed_by = []
        game.revealed = []
        game.hint_used = {}
        game.attempts = self._team_attempts(theme, len(game.members))
        game.turn = self._first_index(game)
        game.created_at = time.time()
        if game.task:
            game.task.cancel()
            game.task = None
        self._spawn_timer(game)

    def _bo3_round_end(
        self,
        game: Game,
        theme: Theme,
        board: str,
        title: str,
        target: dict[str, Any],
        hit_id: str = "",
        hit_name: str = "",
    ) -> str:
        """BO3 里一局打完：分出名次就收场，没分出来就接着打下一局。"""
        if hit_id:
            game.scores[hit_id] = int(game.scores.get(hit_id, 0)) + 1
        need = game.best_of // 2 + 1
        leader = [m for m in game.members if int(game.scores.get(m.get("id"), 0)) >= need]
        over = bool(leader) or game.round_no >= game.best_of

        if over:
            self.games.pop(game.key, None)
            if game.task:
                game.task.cancel()
            winner = leader[0] if leader else None
            self._settle_stats(
                game,
                won=bool(winner),
                winner_id=winner.get("id") if winner else "",
                only_winner=True,
            )
            self._save_state()
            score = self._bo3_score_text(game)
            if winner:
                head = f"🏆 三局两胜打完了：{winner.get('name') or '某人'} 拿下"
            else:
                head = f"🤝 {game.best_of} 局打完，谁都没到 {need} 胜，算平局"
            return (
                f"{board}\n\n{head}\n"
                f"比分：{score}\n"
                f"最后一局答案：{title}\n{theme.card(target)}\n"
                f"发「{theme.command} 双人 bo3」再来一场。"
            )

        game.guesses_done += len(game.guessed)
        game.round_no += 1
        self._reset_round(game)
        self._save_state()
        score = self._bo3_score_text(game)
        lines = [board, ""]
        if hit_name:
            lines.append(f"✅ 本局 {hit_name} 拿下，比分 {score}")
        else:
            lines.append(f"💀 本局没人猜中，答案是 {title}")
            lines.append(f"比分 {score}（本局作废，不计分）")
        lines.append(f"—— 第 {game.round_no} 局 ——")
        lines.extend(self._round_brief(game))
        lines.append(
            f"👉 先由 {game.members[self._first_index(game)].get('name') or '某人'} 猜"
        )
        return "\n".join(lines)

    def _start_text(self, theme: Theme, db: PlayerDB, game: Game) -> str:
        minutes = int(self._cfg("timeout_minutes", 30) or 0)
        limit = f" · 限时 {minutes} 分钟" if minutes > 0 else ""
        others = " ".join(
            f"{t.command}"
            for k, t in THEMES.items()
            if k != theme.key and self._enabled(k) and self.dbs[k].players
        )
        label = theme.pool_label(game.pool)
        size = db.pool_size(game.pool)
        # 标签里已经带了人数就不再重复（如「易（最火 200 人）」），没带就补进括号里
        if re.search(r"\d+\s*人", label):
            shown = label
        elif label.endswith("）"):
            shown = f"{label[:-1]} {size} 人）"
        else:
            shown = f"{label}（{size} 人）"
        tail = f"\n其它题库 {others}" if others else ""
        team_hint = ""
        if not self._cfg("solo_only", False) and game.group_id:
            team_hint = f"\n想一起猜发「{theme.command} 双人」"
        return (
            f"{theme.icon} {theme.command} · 猜{theme.label}\n"
            f"难度：{shown}\n"
            f"机会：{game.attempts} 次{limit}\n"
            f"直接发选手名就能猜，大小写不限\n"
            f"指令 提示 放弃 战绩 排行 图鉴{tail}{team_hint}\n"
            f"{LEGEND}"
        )

    # ---------------- 猜测 ----------------

    def _apply_guess(self, game: Game, guess: dict[str, Any], who_id: str = "", who_name: str = "",
                     snapshot: dict[str, Any] | None = None) -> str:
        theme = THEMES[game.theme]
        if game.is_team and not game.started:
            return "组队局还没开始，等房主发「开始」。"
        if game.is_team:
            # 回合制：只能轮到的那个人猜
            now = self._current_member(game)
            if now and who_id and now.get("id") != who_id:
                left = game.attempts - len(game.guessed)
                return f"现在轮到 {now.get('name')} 猜（还剩 {left} 次）。"
        db = self.dbs[game.theme]
        title = theme.title_of(guess)
        if title in game.guessed:
            return f"「{title}」已经猜过了，换一个。"
        target = db.get(game.target)
        if not target:
            self.games.pop(game.key, None)
            return "这一局的答案数据丢了，重新开一局吧。"

        # Older saves only contain names. Reconstruct missing rows once; new
        # rows are persisted as snapshots so later data/config changes do not
        # change an already displayed comparison.
        while len(game.board_history) < len(game.guessed):
            previous = game.guessed[len(game.board_history)]
            player = db.get(previous)
            old_cells = theme.judge(player, target) if player else []
            game.board_history.append({
                "name": previous,
                "cells": [vars(c).copy() for c in old_cells] if player else [
                    {"label": "历史数据", "value": "选手数据已缺失", "level": "unknown"}
                ],
            })
        game.guessed.append(title)
        cells = theme.judge(guess, target)
        game.board_history.append({"name": title, "cells": [vars(c).copy() for c in cells]})
        board = render_board(theme, len(game.guessed), game.attempts, guess, target)
        if snapshot is not None:
            snapshot.update({
                "title": f"{theme.command} · 第 {game.round_no} 局" if game.is_bo3 else theme.command,
                "rows": [{"name": row["name"], "cells": [dict(c) for c in row["cells"]]}
                         for row in game.board_history],
                "status": f"第 {len(game.guessed)} / {game.attempts} 猜 · 绿色一致  黄色接近  红色不符 · 箭头指向答案",
                "board": board,
            })

        if all(c.level == "correct" for c in cells):
            if game.is_bo3:
                return self._bo3_round_end(
                    game, theme, board, title, target, hit_id=who_id, hit_name=who_name
                )
            self.games.pop(game.key, None)
            if game.task:
                game.task.cancel()
            self._settle_stats(game, won=True, winner_id=who_id)
            self._save_state()
            hit_head = f"🎉 {who_name} 猜中！" if game.is_team and who_name else "🎉 猜中了！"
            return (
                f"{board}\n\n{hit_head}{len(game.guessed)} 猜命中 {title}\n{theme.card(target)}\n"
                + self._user_stats_text(game.group_id, who_id or game.user_id, game.theme)
            )

        if len(game.guessed) >= game.attempts:
            if game.is_bo3:
                return self._bo3_round_end(game, theme, board, title, target)
            self.games.pop(game.key, None)
            if game.task:
                game.task.cancel()
            self._settle_stats(game, won=False)
            self._save_state()
            if game.is_team:
                return (
                    f"{board}\n\n💀 {game.attempts} 次机会用完了，答案是 {title}\n{theme.card(target)}\n"
                    f"全队都没猜出来，发「{theme.command} 多人」再来一局。"
                )
            return (
                f"{board}\n\n💀 {game.attempts} 次机会用完了，答案是 {title}\n{theme.card(target)}\n"
                f"发送「{theme.command}」再来一局。"
            )

        if game.is_team and game.members:
            game.turn = (game.turn + 1) % len(game.members)
        self._save_state()

        lines = [board]
        if self._cfg("show_legend", True) and len(game.guessed) == 1:
            lines.append(LEGEND)
        left = game.attempts - len(game.guessed)
        if left == 1:
            lines.append("⚠️ 最后一次机会了。")
        else:
            lines.append(f"还剩 {left} 次机会")
        if game.is_team:
            nxt = self._current_member(game)
            if nxt:
                lines.append(f"👉 轮到 {nxt.get('name')}")
        return "\n".join(lines)

    def _with_at(self, event: AstrMessageEvent, text: str) -> list:
        if self._cfg("at_sender", True) and event.get_group_id():
            with contextlib.suppress(Exception):
                return [At(qq=event.get_sender_id()), Plain(f" {text}")]
        return [Plain(text)]

    # ---------------- 口令实现 ----------------

    def _hint_text(self, event: AstrMessageEvent) -> str:
        if not self._cfg("enable_hint", True):
            return "本插件已关闭提示功能。"
        theme_key = self._current_theme(event)
        theme = THEMES[theme_key]
        game = self._play_game(event, theme_key)
        if not game:
            return f"现在没有进行中的对局，发送「{theme.command}」开局。"
        if game.is_team and not game.started:
            return "组队局还没开始，等房主发「开始」。"

        # 双人局按人给额度：先手 0 次、后手 duel_hint 次，每局重置
        uid = event.get_sender_id()
        duel = game.is_team and len(game.members) == 2
        if duel:
            first = self._first_member(game)
            quota = self._duel_hint()
            if first and first.get("id") == uid:
                return f"本局你是先手，没有提示（后手有 {quota} 次）。"
            used = int(game.hint_used.get(uid, 0))
            if used >= quota:
                return f"本局的 {quota} 次提示用完了。" if quota else "本局没有提示额度。"
        else:
            quota = int(self._cfg("hint_limit", 2) or 0)
            used = len(game.revealed)
            if used >= quota:
                return f"提示已经用完了（每局 {quota} 次）。"
        target = self.dbs[theme_key].get(game.target)
        if not target:
            self.games.pop(game.key, None)
            return "这一局的数据丢了，重新开一局吧。"

        pool = [h for h in theme.hints(target) if h[0] not in game.revealed]
        if not pool:
            return "没什么可提示的了，直接猜吧。"
        label, value = random.choice(pool)
        game.revealed.append(label)
        if duel:
            game.hint_used[uid] = used + 1
            left = quota - game.hint_used[uid]
        else:
            left = quota - len(game.revealed)
        self._save_state()
        return f"💡 线索：{value}（提示剩 {left} 次）"

    def _giveup_text(self, event: AstrMessageEvent) -> str:
        theme_key = self._current_theme(event)
        theme = THEMES[theme_key]
        game = self._play_game(event, theme_key)
        if not game:
            return "现在没有进行中的对局。"
        if game.is_team and not game.started:
            if game.user_id != event.get_sender_id():
                return "组队局还没开始，等房主开始或者解散。"
            self.games.pop(game.key, None)
            self._save_state()
            return "🚪 房间已解散。"
        self.games.pop(game.key, None)
        if game.task:
            game.task.cancel()
        self._settle_stats(game, won=False)
        self._save_state()
        target = self.dbs[game.theme].get(game.target)
        head = "🏳️ 组队局结束" if game.is_team else "🏳️ 已放弃"
        if game.is_bo3:
            head += f"（第 {game.round_no} 局中断，比分 {self._bo3_score_text(game)}）"
        return f"{head}。答案：{game.target}\n" + (theme.card(target) if target else "")

    def _stats_text(self, event: AstrMessageEvent) -> str:
        _ukey, gid, uid = self._pk(event)
        return self._user_stats_text(gid, uid, self._current_theme(event))

    def _rank_text(self, event: AstrMessageEvent) -> str:
        _ukey, gid, _uid = self._pk(event)
        return self._rank_board(gid, self._current_theme(event))

    def _lookup_text(self, event: AstrMessageEvent, query: str) -> str:
        if not query:
            return "用法：图鉴 <选手名>"
        theme_key = self._current_theme(event)
        order = [theme_key] + [k for k in THEMES if k != theme_key]
        for key in order:
            db = self.dbs[key]
            candidates = db.find(query)
            if len(candidates) == 1:
                return THEMES[key].card(candidates[0])
            if len(candidates) > 1:
                # 外号表点过名的（niko → 波黑 NiKo）直接给卡，不再让人挑
                pref = db.preferred(query)
                if pref is not None and candidates[0] is pref:
                    return THEMES[key].card(pref)
                names = " / ".join(THEMES[key].title_of(p) for p in candidates)
                return f"{THEMES[key].label}里找到多个：{names}，换个更精确的名字。"
        return f"没找到「{query}」，试试英文名或别的写法。"

    # ---------------- 指令 ----------------

    @filter.command("弗一把", alias={"猜选手", "cs猜选手"})
    async def start_cs(self, event: AstrMessageEvent, difficulty: str = ""):
        """CS:GO / CS2 Major 选手猜谜"""
        if not self._session_ok(event):
            return
        args = self._args_after_command(event, THEMES["cs"]) or difficulty
        yield event.chain_result(self._with_at(event, self._begin(event, "cs", args)))
        event.stop_event()

    @filter.command("足一把", alias={"足球一把", "猜球员", "猜球星"})
    async def start_football(self, event: AstrMessageEvent, difficulty: str = ""):
        """足球球员猜谜"""
        if not self._session_ok(event):
            return
        args = self._args_after_command(event, THEMES["football"]) or difficulty
        yield event.chain_result(self._with_at(event, self._begin(event, "football", args)))
        event.stop_event()

    @filter.command("撸一把", alias={"lol一把", "猜撸", "猜联盟"})
    async def start_lol(self, event: AstrMessageEvent, difficulty: str = ""):
        """LoL 职业选手猜谜"""
        if not self._session_ok(event):
            return
        args = self._args_after_command(event, THEMES["lol"]) or difficulty
        yield event.chain_result(self._with_at(event, self._begin(event, "lol", args)))
        event.stop_event()

    @filter.command("瓦一把", alias={"猜瓦", "瓦罗兰特", "valorant"})
    async def start_valorant(self, event: AstrMessageEvent, difficulty: str = ""):
        """瓦罗兰特选手猜谜"""
        if not self._session_ok(event):
            return
        args = self._args_after_command(event, THEMES["valorant"]) or difficulty
        yield event.chain_result(self._with_at(event, self._begin(event, "valorant", args)))
        event.stop_event()

    @filter.command("提示", alias={"线索"})
    async def hint(self, event: AstrMessageEvent):
        """给一条线索，每局限用若干次"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._hint_text(event))
        event.stop_event()

    @filter.command("放弃", alias={"结束", "认输"})
    async def give_up(self, event: AstrMessageEvent):
        """放弃本局并公布答案"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._giveup_text(event))
        event.stop_event()

    @filter.command("加入", alias={"加我"})
    async def join_team(self, event: AstrMessageEvent):
        """加入群里的双人 / 多人房间"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._join_team(event))
        event.stop_event()

    @filter.command("开始", alias={"开局"})
    async def start_team(self, event: AstrMessageEvent):
        """房主开始组队局"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._start_team(event))
        event.stop_event()

    @filter.command("战绩")
    async def my_stats(self, event: AstrMessageEvent):
        """查看当前题库的战绩"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._stats_text(event))
        event.stop_event()

    @filter.command("排行", alias={"排行榜"})
    async def leaderboard(self, event: AstrMessageEvent):
        """本群当前题库的胜场排行"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._rank_text(event))
        event.stop_event()

    @filter.command("图鉴", alias={"查选手", "查球员", "查资料"})
    async def lookup(self, event: AstrMessageEvent, query: str = ""):
        """查选手资料，例如 图鉴 哈兰德"""
        if not self._session_ok(event):
            return
        yield event.plain_result(self._lookup_text(event, query))
        event.stop_event()

    @filter.command("猜选手帮助", alias={"弗一把帮助", "足一把帮助", "撸一把帮助"})
    async def help(self, event: AstrMessageEvent):
        """玩法说明"""
        if not self._session_ok(event):
            return
        lines = ["🎮 猜选手 · 多题库"]
        for key, theme in THEMES.items():
            if not self._enabled(key) or not self.dbs[key].players:
                continue
            db = self.dbs[key]
            lines.append(
                f"{theme.icon} {theme.command}（{theme.label} {len(db.players)} 人，{self._attempts(theme)} 次机会）"
                f"　难度：{theme.difficulty_hint}"
            )
        attempts = self._attempts(THEMES["cs"])
        lines.append(f"1. 发「弗一把」「足一把」「撸一把」开局，默认 {attempts} 次机会，可用难度词")
        lines.append("2. 直接发选手名就能猜（也支持「猜 xxx」）")
        if self.dbs["cs"].alias_report.get("added"):
            lines.append("   弗一把也认外号：虾哥=NiKo、载物=ZywOo、小孩=m0NESY、大表哥=karrigan…")
        lines.append(f"3. 每次猜测按各题库的属性逐格反馈：{LEGEND}")
        lines.append("4. 指令：提示 · 放弃 · 战绩 · 排行 · 图鉴 <名字>")
        if self._cfg("solo_only", False):
            lines.append("5. 当前只开独猜，每人各猜各的")
        else:
            lines.append("5. 组队：发「弗一把 双人」开房，别人发「加入」，凑齐自动开局")
            lines.append("   「多人」最多 6 人，房主发「开始」；答案和机会全队共用")
            lines.append(
                f"   双人局先手没提示、后手 {self._duel_hint()} 次；"
                f"「{THEMES['cs'].command} 双人 bo3」打三局两胜"
            )
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ---------------- 自由文本 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """自由文本入口：裸口令开局、图鉴、常用口令、直接发选手名猜。"""
        if not self._session_ok(event):
            return
        raw = (event.message_str or "").strip()
        if not raw:
            return

        ukey, _gid, _uid = self._pk(event)

        # 1) 开局
        for pattern, theme_key in _START_PATTERNS:
            matched = pattern.match(raw)
            if matched:
                event.stop_event()
                text = self._begin(event, theme_key, (matched.group(1) or "").strip())
                await event.send(event.chain_result(self._with_at(event, text)))
                return

        # 2) 图鉴
        looked = _LOOKUP_RE.match(raw)
        if looked:
            event.stop_event()
            await event.send(event.plain_result(self._lookup_text(event, (looked.group(1) or "").strip())))
            return

        # 3) 其它裸口令
        method = _BARE_COMMANDS.get(raw.lstrip("/／").strip())
        if method:
            theme_key = self._current_theme(event)
            if method == "_join_team" and not self._team_of(event):
                return
            if method == "_start_team" and not self._lobby_of(event):
                return
            if method in ("_hint_text", "_giveup_text") and not self._play_game(event, theme_key):
                return
            event.stop_event()
            await event.send(event.plain_result(getattr(self, method)(event)))
            return

        # 4) 直接发选手名
        if not self._cfg("free_text_guess", True):
            return
        theme_key = self._current_theme(event)
        game = self._play_game(event, theme_key)
        if not game or len(raw) > 32:
            return
        if game.is_team and not game.started:
            return
        query = _TRAILING_RE.sub("", _LEADING_RE.sub("", raw)).strip()
        if not query or re.search(r"[?？]$", raw):
            return
        db = self.dbs[theme_key]
        # 单字外号（「虾」「驴」）太容易撞上日常聊天，必须带「猜」才认
        if normalize_name(query) in db.single_char_aliases and not _LEADING_RE.match(raw):
            return
        kind, candidates = db.match(query)
        if not candidates:
            return
        if len(candidates) > 1:
            if kind == "exact":
                candidates = candidates[:1]
            elif _LEADING_RE.match(raw):
                names = " / ".join(THEMES[theme_key].title_of(p) for p in candidates)
                event.stop_event()
                await event.send(event.plain_result(f"你想猜谁？{names}"))
                return
            else:
                return

        event.stop_event()
        round_no, target_title = game.round_no, game.target
        snapshot: dict[str, Any] = {}
        async with self._lock:
            # A prior queued message may already have finished or advanced this round.
            if self.games.get(game.key) is not game or game.round_no != round_no or game.target != target_title:
                return
            text = self._apply_guess(
                game, candidates[0], event.get_sender_id(), event.get_sender_name() or "", snapshot
            )
        if snapshot:
            try:
                from .image_renderer import render_board_image
                png = await asyncio.to_thread(render_board_image, snapshot["title"], snapshot["rows"], snapshot["status"])
                # Keep turn, result and stats text, but omit the old single-row board.
                detail = text.removeprefix(snapshot["board"]).lstrip("\n")
                chain = self._with_at(event, detail) if detail else []
                chain.append(Image.fromBytes(png))
            except Exception:
                logger.exception("[%s] 图片棋盘生成失败，回退文字", PLUGIN_NAME)
            else:
                # A send failure may have an uncertain outcome: do not send twice.
                await event.send(event.chain_result(chain))
                return
        await event.send(event.chain_result(self._with_at(event, text)))

    async def terminate(self) -> None:
        for game in list(self.games.values()):
            if game.task:
                game.task.cancel()
        self._save_state()
        logger.info(f"[{PLUGIN_NAME}] 已卸载，状态已保存")
