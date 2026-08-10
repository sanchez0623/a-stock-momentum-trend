"""联网验证 get_kline 格式闸门:
1. 非法 secid(旧代码 bug 模式: 把 6 位裸代码当 secid 传) -> 拦截, 不请求 baostock, 返回空 df
2. 合法 6 位股票代码 -> 正常放行, 返回真实 K 线
需 baostock 已装且联网。
注意: 以 verify_ 开头而非 test_, 避免被 pytest 当作测试收集(脚本顶层 sys.exit)."""
import sys
import os
import asyncio
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from app.core.datasource.baostock_src import BaostockSource

BAOSTOCK_OK = __import__("app.core.datasource.baostock_src", fromlist=["BAOSTOCK_OK"]).BAOSTOCK_OK
if not BAOSTOCK_OK:
    print("SKIP: baostock 未安装")
    sys.exit(0)

src = BaostockSource()


async def main():
    # 1) 非法 secid: 模拟旧代码把 600000 直接当 secid 传给 baostock
    df_bad = await src.get_kline("", period="daily", count=5, secid="600000")
    print(f"[拦截] secid='600000'(裸6位) -> 行数={len(df_bad)} (应为 0, 且不触发 baostock '应为9位')")

    # 2) 合法 6 位股票代码: 应放行并取到真实 K 线
    df_ok = await src.get_kline("600000", period="daily", count=5)
    print(f"[放行] symbol='600000' -> 行数={len(df_ok)} (应为 >0, 真正请求了 baostock)")

    # 3) 合法指数 secid(东财格式): 应放行
    df_idx = await src.get_kline("", period="daily", count=5, secid="0.000300")
    print(f"[放行] secid='0.000300'(沪深300) -> 行数={len(df_idx)} (应为 >0)")

    passed = (
        len(df_bad) == 0
        and len(df_ok) > 0
        and len(df_idx) > 0
    )
    print("RESULT:", "PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


asyncio.run(main())
