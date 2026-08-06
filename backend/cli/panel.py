"""rich 终端面板(方案 §4.12): 前端不可用时的兜底.

用法:
    cd backend && python -m cli.panel
    python -m cli.panel --once      # 只打一次快照后退出
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

console = Console()

# 涨红跌绿(中文约定)
UP_COLOR = "red"
DOWN_COLOR = "green"


def _color(pct: float) -> str:
    if pct > 0:
        return UP_COLOR
    if pct < 0:
        return DOWN_COLOR
    return "white"


def status_table() -> Table:
    from app.core.datasource import data_source_manager

    table = Table(title="数据源状态", box=None)
    table.add_column("源", style="cyan")
    table.add_column("延迟(ms)", justify="right")
    table.add_column("请求数", justify="right")
    table.add_column("成功数", justify="right")
    table.add_column("熔断", justify="center")
    table.add_column("偏好", justify="center")
    for st in data_source_manager.status():
        table.add_row(
            st["label"],
            f"{st['avg_latency_ms']:.1f}",
            str(st["request_count"]),
            str(st["success_count"]),
            "⚠ 熔断" if st["circuit_open"] else "正常",
            "★" if st["preferred"] else "",
        )
    return table


def watchlist_table(symbols: list[str]) -> Table:
    from app.core.datasource import data_source_manager

    table = Table(title="自选股实时行情", box=None)
    table.add_column("代码")
    table.add_column("名称")
    table.add_column("现价", justify="right")
    table.add_column("涨跌", justify="right")
    table.add_column("涨跌%", justify="right")
    table.add_column("最高", justify="right")
    table.add_column("最低", justify="right")
    quotes = asyncio.run(data_source_manager.get_realtime_quote(symbols))
    for q in quotes:
        pct = q.change_pct
        table.add_row(
            q.symbol, q.name or "-",
            f"{q.price:.2f}",
            f"[{_color(pct)}]{q.change:+.2f}[/]",
            f"[{_color(pct)}]{pct:+.2f}%[/]",
            f"{q.high:.2f}", f"{q.low:.2f}",
        )
    return table


def risk_panel() -> Panel:
    from app.core.config import config_manager

    cfg = config_manager.get()
    risk = cfg["风控"]
    return Panel(
        "\n".join([
            f"日亏损熔断线: {risk['daily_loss_limit_pct']}%",
            f"单票仓位上限: {risk['single_position_pct']}%",
            f"总仓位上限: {risk['total_position_pct']}%",
            f"个股止损: {risk['stop_loss_pct']}%",
            f"移动止损: {risk['trailing_stop_pct']}%",
        ]),
        title="[bold]风控参数[/]",
        border_style="yellow",
    )


async def run_once(symbols: list[str]) -> None:
    console.print(risk_panel())
    console.print(status_table())
    console.print()
    console.print(watchlist_table(symbols))


async def run_live(symbols: list[str]) -> None:

    console.print("[dim]Ctrl+C 退出[/]")
    with Live(console=console, refresh_per_second=1) as live:
        while True:
            live.update(
                Table.grid(
                    Panel(status_table(), title="数据源"),
                    watchlist_table(symbols),
                    title="Momentum Trader",
                )
            )
            await asyncio.sleep(5)


def main() -> None:

    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Momentum Trader 终端面板")
    parser.add_argument("--symbols", default="300750,600519,000001,601318", help="自选股, 逗号分隔")
    parser.add_argument("--once", action="store_true", help="只输出一次快照")
    args = parser.parse_args()

    import asyncio

    from app.main import init_app

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    asyncio.run(init_app())
    if args.once:
        asyncio.run(run_once(symbols))
    else:
        try:
            asyncio.run(run_live(symbols))
        except KeyboardInterrupt:
            console.print("\n[dim]已退出[/]")


if __name__ == "__main__":
    main()
