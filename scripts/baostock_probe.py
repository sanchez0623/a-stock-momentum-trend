"""Baostock 连通性与接口可用性探针。

仅用于本地验证 Baostock 服务是否可达、关键接口字段是否可用。
不依赖项目其它模块，可在干净 venv 中直接跑：
    python baostock_probe.py
"""
import sys
import traceback

try:
    import baostock as bs
except Exception as e:  # noqa: BLE001
    print("IMPORT_FAILED:", repr(e))
    sys.exit(2)


def try_query(name, fn):
    try:
        rows = fn()
        print(f"[OK] {name}: {len(rows)} rows")
        for r in rows[:2]:
            print("     ", r)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: {repr(e)}")
        return False


def main():
    lg = bs.login()
    print("LOGIN error_code=%s msg=%s" % (lg.error_code, lg.error_msg))
    if lg.error_code != "0":
        print("LOGIN_FAILED")
        sys.exit(1)

    # 1) 沪深300 成分股
    def q_hs300():
        rs = bs.query_hs300_stocks()
        out = []
        while (rs.error_code == "0") & rs.next():
            out.append(rs.get_row_data())
        return out

    try_query("query_hs300_stocks", q_hs300)

    # 2) 中证500 成分股
    def q_zz500():
        rs = bs.query_zz500_stocks()
        out = []
        while (rs.error_code == "0") & rs.next():
            out.append(rs.get_row_data())
        return out

    try_query("query_zz500_stocks", q_zz500)

    # 3) 上证50 成分股
    def q_sz50():
        rs = bs.query_sz50_stocks()
        out = []
        while (rs.error_code == "0") & rs.next():
            out.append(rs.get_row_data())
        return out

    try_query("query_sz50_stocks", q_sz50)

    # 4) 日K线（含估值 + ST 标记）
    def q_kline():
        rs = bs.query_history_k_data_plus(
            "sh.600000",
            "date,code,open,high,low,close,volume,amount,adjustflag,turn,pctChg,peTTM,pbMRQ,psTTM,pcfNcfTTM,isST",
            start_date="2024-01-01",
            end_date="2024-01-10",
            frequency="d",
            adjustflag="3",
        )
        out = []
        while (rs.error_code == "0") & rs.next():
            out.append(rs.get_row_data())
        return out

    try_query("query_history_k_data_plus(+valuation/isST)", q_kline)

    # 5) 行业分类（申万）
    def q_industry():
        rs = bs.query_stock_industry()
        out = []
        while (rs.error_code == "0") & rs.next():
            out.append(rs.get_row_data())
        return out

    try_query("query_stock_industry", q_industry)

    # 6) 盈利能力（基本面因子）
    def q_profit():
        rs = bs.query_profit_data("sh.600000", year=2023, quarter=4)
        out = []
        while (rs.error_code == "0") & rs.next():
            out.append(rs.get_row_data())
        return out

    try_query("query_profit_data", q_profit)

    # 7) 业绩预告（事件催化）
    def q_forecast():
        rs = bs.query_forecast_report("sh.600000", start_date="2024-01-01", end_date="2024-06-30")
        out = []
        while (rs.error_code == "0") & rs.next():
            out.append(rs.get_row_data())
        return out

    try_query("query_forecast_report", q_forecast)

    # 8) 指数日线（大盘 regime）
    def q_index():
        rs = bs.query_history_k_data_plus(
            "sz.399300",
            "date,code,open,high,low,close,volume,amount,pctChg",
            start_date="2024-01-01",
            end_date="2024-01-10",
            frequency="d",
            adjustflag="3",
        )
        out = []
        while (rs.error_code == "0") & rs.next():
            out.append(rs.get_row_data())
        return out

    try_query("query_history_k_data_plus(index sz.399300)", q_index)

    bs.logout()
    print("DONE")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(3)
