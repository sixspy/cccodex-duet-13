"""多源左轨串扰：左轨多源同键的隔离/合并形态 + 键对齐隔离主链。

题面：两渠道源同业务键灌左后，按业务键汇总对齐会串源（混源单行），
或源隔离失败导致差额错误。本模块落两种可选形态（轨规.多源形态）：

- 复合（默认）：对齐键 = 源键 + 业务键。两源同键各一笔 → 对齐差两行，
  源键可区分；不写源键归档。
- 合并：同业务键合并进现行（对齐差一行，基线口径），源明细进只读
  源键归档；读引用与复合不同（差行数 1 vs N）。

硬约束：
- 对齐独占锁（不加锁会双成功）：跑对齐/登记源/设形态同票互斥，忙则拒。
- 源键汇总与对齐差同库同事务，同成败（源登记与对齐差同票）。
- 禁止只改源登记徽章：隔离体现在对齐键、差行数与只读归档，不在皮上。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from app.db import 连接
from app.seed import 此刻

NAME = "多源左轨串扰"

复合 = "复合"
合并 = "合并"
形态集 = (复合, 合并)
默认形态 = 复合


class 对齐忙(Exception):
    """对齐独占锁被占用：拒绝并发双成功。"""


class 请求非法(Exception):
    """形态/源登记参数非法。"""


对齐锁 = threading.Lock()


@contextmanager
def 对齐独占():
    """非阻塞互斥：占不到锁即拒，杜绝并发双成功。"""
    if not 对齐锁.acquire(blocking=False):
        raise 对齐忙("对齐进行中，拒绝并发双成功")
    try:
        yield
    finally:
        对齐锁.release()


def 形态(conn: sqlite3.Connection) -> str:
    r = conn.execute("SELECT 值 FROM 轨规 WHERE 键='多源形态'").fetchone()
    v = (r["值"] if r else "") or 默认形态
    return v if v in 形态集 else 默认形态


def _容差(conn: sqlite3.Connection) -> int:
    r = conn.execute("SELECT 值 FROM 轨规 WHERE 键='金额容差分'").fetchone()
    try:
        return int(r["值"]) if r else 0
    except (TypeError, ValueError):
        return 0


def 算对齐(conn: sqlite3.Connection, 容差: int) -> dict:
    """重算对齐差/悬账单（+合并形态的源键归档）。不写库外状态、不提交：

    调用方在同一连接同一事务里提交或回滚 —— 源键汇总与对齐差同成败。
    """
    形 = 形态(conn)
    conn.execute("DELETE FROM 对齐差")
    conn.execute("DELETE FROM 悬账单")
    conn.execute("DELETE FROM 源键归档")
    时刻 = 此刻()

    # 左轨按 (源键, 业务键) 分组汇总：隔离的最小单位，绝不跨源混行。
    左组: dict[tuple[str, str], list] = {}
    for r in conn.execute("SELECT * FROM 左轨流水 WHERE 命运='已入左'").fetchall():
        k = (r["源键"], r["业务键"])
        g = 左组.setdefault(k, [0, r["事件编号"], 0])
        g[0] += int(r["金额分"])
        g[2] += 1

    右汇总: dict[str, int] = {}
    右代表: dict[str, str] = {}
    for r in conn.execute("SELECT * FROM 右轨流水 WHERE 命运='已入右'").fetchall():
        k = r["业务键"]
        右汇总[k] = 右汇总.get(k, 0) + int(r["金额分"])
        右代表[k] = r["事件编号"]

    对齐 = 0
    差 = 0
    悬 = 0
    归档数 = 0
    左键集: set[str] = set()

    def 落差(键: str, 源: str, 左额, 右额, 型: str, 说: str) -> None:
        conn.execute(
            "INSERT INTO 对齐差(业务键,源键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?,?)",
            (键, 源, 左额, 右额, 型, 说, 时刻),
        )

    def 落悬(键: str, 源: str, 边: str, 额: int, 事件: str, 因: str) -> None:
        conn.execute(
            "INSERT INTO 悬账单(业务键,源键,悬边,金额分,事件编号,原因,时刻) VALUES (?,?,?,?,?,?,?)",
            (键, 源, 边, 额, 事件, 因, 时刻),
        )

    if 形 == 复合:
        # 形态一：源键复合对齐。对齐键 = 源键 + 业务键，两源同键 → 两行差。
        for (源, 键) in sorted(左组):
            左键集.add(键)
            额, 代表, _笔 = 左组[(源, 键)]
            if 键 in 右汇总:
                ra = 右汇总[键]
                if abs(额 - ra) <= 容差:
                    落差(键, 源, 额, ra, "对齐", f"源{源}双边金额在容差内")
                    对齐 += 1
                else:
                    落差(键, 源, 额, ra, "金额不符", f"源{源} 左{额}-右{ra}")
                    差 += 1
            else:
                落差(键, 源, 额, None, "右缺", f"源{源}仅左轨有键")
                落悬(键, 源, "左", 额, 代表, "右轨缺键")
                悬 += 1
                差 += 1
        键总数 = len(左组)
    else:
        # 形态二：业务键合并双写两轨。现行一行（基线口径），源明细进只读归档。
        左汇总: dict[str, int] = {}
        左代表: dict[str, str] = {}
        for (源, 键), (额, 代表, 笔) in sorted(左组.items()):
            左键集.add(键)
            左汇总[键] = 左汇总.get(键, 0) + 额
            左代表.setdefault(键, 代表)
            conn.execute(
                "INSERT INTO 源键归档(业务键,源键,金额分,笔数,时刻) VALUES (?,?,?,?,?)",
                (键, 源, 额, 笔, 时刻),
            )
            归档数 += 1
        for 键 in sorted(左汇总):
            la = 左汇总[键]
            if 键 in 右汇总:
                ra = 右汇总[键]
                if abs(la - ra) <= 容差:
                    落差(键, "", la, ra, "对齐", "双边金额在容差内")
                    对齐 += 1
                else:
                    落差(键, "", la, ra, "金额不符", f"左{la}-右{ra}")
                    差 += 1
            else:
                落差(键, "", la, None, "右缺", "仅左轨有键")
                落悬(键, "", "左", la, 左代表.get(键, ""), "右轨缺键")
                悬 += 1
                差 += 1
        键总数 = len(左汇总)

    for 键 in sorted(set(右汇总) - 左键集):
        落差(键, "", None, 右汇总[键], "左缺", "仅右轨有键")
        落悬(键, "", "右", 右汇总[键], 右代表.get(键, ""), "左轨缺键")
        悬 += 1
        差 += 1
    键总数 += len(set(右汇总) - 左键集)

    return {
        "形态": 形,
        "键总数": 键总数,
        "对齐": 对齐,
        "差与悬": 差,
        "悬账": 悬,
        "归档笔数": 归档数,
        "容差分": 容差,
        "时刻": 时刻,
    }


def 跑对齐() -> dict:
    """独占锁 + 单事务重算。并发第二跑直接被拒（不加锁会双成功）。"""
    with 对齐独占():
        conn = 连接()
        try:
            out = 算对齐(conn, _容差(conn))
            conn.commit()
            return out
        finally:
            conn.close()


def 设形态同票(形: str) -> dict:
    """设多源形态与重算对齐同票：一票事务，同成败。"""
    if 形 not in 形态集:
        raise 请求非法(f"多源形态须为 {'|'.join(形态集)}")
    with 对齐独占():
        conn = 连接()
        try:
            conn.execute(
                "INSERT INTO 轨规(键, 值) VALUES ('多源形态', ?) "
                "ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
                (形,),
            )
            out = 算对齐(conn, _容差(conn))
            conn.commit()
            return {"形态": 形, "已同票重算": True, "对齐": out}
        finally:
            conn.close()


def 登记源同票(体: dict) -> dict:
    """源登记与对齐差同票：登记新源并重算对齐，同一事务同成败。"""
    体 = 体 or {}
    源 = str(体.get("源键") or "").strip()
    轨 = str(体.get("轨别") or "").strip()
    名 = str(体.get("名称") or "").strip()
    说 = str(体.get("说明") or "")
    if not 源 or len(源) > 64 or any(c in 源 for c in " \t"):
        raise 请求非法("源键必填且不可含空白")
    if 轨 not in ("左", "右"):
        raise 请求非法("轨别须为 左|右")
    if not 名:
        raise 请求非法("名称必填")
    with 对齐独占():
        conn = 连接()
        try:
            if conn.execute("SELECT 1 FROM 源登记 WHERE 源键=?", (源,)).fetchone():
                raise 请求非法(f"源键已登记:{源}")
            conn.execute(
                "INSERT INTO 源登记(源键,轨别,名称,说明) VALUES (?,?,?,?)",
                (源, 轨, 名, 说),
            )
            out = 算对齐(conn, _容差(conn))
            conn.commit()
            return {"源键": 源, "轨别": 轨, "已登记": True, "已同票重算": True, "对齐": out}
        finally:
            conn.close()


def 隔离目录读() -> dict:
    """多源隔离只读目录：业务键、源列表、差行数、valid。"""
    conn = 连接()
    try:
        return _隔离目录(conn)
    finally:
        conn.close()


def _隔离目录(conn: sqlite3.Connection) -> dict:
    形 = 形态(conn)
    左源: dict[str, set] = {}
    for r in conn.execute(
        "SELECT 业务键, 源键 FROM 左轨流水 WHERE 命运='已入左'"
    ).fetchall():
        左源.setdefault(r["业务键"], set()).add(r["源键"])
    差行: dict[str, list] = {}
    for r in conn.execute("SELECT 业务键, 源键 FROM 对齐差").fetchall():
        差行.setdefault(r["业务键"], []).append(r["源键"])
    归档: dict[str, set] = {}
    for r in conn.execute("SELECT 业务键, 源键 FROM 源键归档").fetchall():
        归档.setdefault(r["业务键"], set()).add(r["源键"])

    明细 = []
    for 键 in sorted(set(左源) | set(差行)):
        源列表 = sorted(左源.get(键, set()))
        行源 = 差行.get(键, [])
        差行数 = len(行源)
        if not 源列表:
            ok = 差行数 == 1  # 仅右轨有键：单行左缺
        elif 形 == 复合:
            # 复合：每源一行，差行源键集合须等于左轨源集合（不混源、不缺源）
            ok = 差行数 == len(源列表) and set(行源) == set(源列表)
        else:
            # 合并：现行一行，且归档源明细覆盖全部左源
            ok = 差行数 == 1 and 归档.get(键, set()) == set(源列表)
        明细.append({"业务键": 键, "源列表": 源列表, "差行数": 差行数, "valid": ok})
    return {"形态": 形, "条数": len(明细), "明细": 明细}


def 归档列表() -> list[dict]:
    """源键归档只读列表（合并形态的源明细，无写接口）。"""
    conn = 连接()
    try:
        rows = conn.execute("SELECT * FROM 源键归档 ORDER BY 编号 DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
