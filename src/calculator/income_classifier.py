"""收入分类器（已弃用）

⚠️ 本模块为历史遗留代码，当前系统中无任何调用方。
实际分类逻辑在 src/calculator/tax_engine.py 中直接实现。

如需使用，请参考 tax_engine.py 中的分类逻辑：
- RSU_VEST → rsu_income（工资薪金所得，3%~45% 累进）
- SELL / RSU_SELL / OPTION_SELL → capital_gain（财产转让所得，20%）
- DIVIDEND → dividend（股息红利所得，20%）
- INTEREST → interest_income（利息所得，20%）
- YIELD_INCOME → yield_income（投资收益，20%）
- OPTION_EXPIRE → capital_gain_expire_loss（期权过期损失，不征税）

保留此文件仅供历史参考。后续版本可能删除。
"""

from __future__ import annotations
from src.models import Transaction, Action


def classify_income(txn: Transaction) -> str | None:
    """根据交易类型分类应税收入类型（已弃用）

    Returns:
        "rsu_vest" | "capital_gain" | "dividend" | None

    .. deprecated::
        此函数不再被系统调用。请使用 tax_engine.py 中的分类逻辑。
    """
    if txn.action == Action.RSU_VEST:
        return "rsu_vest"  # RSU 归属 → 工资薪金所得（单独计税）

    if txn.action == Action.SELL or txn.action == Action.RSU_SELL:
        return "capital_gain"  # 卖出 → 财产转让所得

    if txn.action == Action.DIVIDEND:
        return "dividend"  # 分红 → 股息红利所得

    return None  # buy / fee 不产生应税收入
