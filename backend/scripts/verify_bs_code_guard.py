"""验证 baostock 代码转换 + 格式闸门: 非法代码绝不传给 baostock.

不联网、不登录, 纯函数 + 拦截检查. 直接 `python scripts/test_bs_code_guard.py` 运行.
"""
import sys
import os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from app.core.datasource import baostock_src as bs

ok = 0
fail = 0


def check(label, got, exp):
    global ok, fail
    status = "OK " if got == exp else "FAIL"
    if got == exp:
        ok += 1
    else:
        fail += 1
    print(f"  [{status}] {label}: got={got!r} exp={exp!r}")


print("== to_bs_code ==")
check("600000 ->", bs.to_bs_code("600000"), "sh.600000")
check("000001 ->", bs.to_bs_code("000001"), "sz.000001")
check("300750 ->", bs.to_bs_code("300750"), "sz.300750")
check("sh.600000(已是) ->", bs.to_bs_code("sh.600000"), "sh.600000")
check("600000.SH(尾缀) ->", bs.to_bs_code("600000.SH"), "sh.600000")
check("SH600000(前缀) ->", bs.to_bs_code("SH600000"), "sh.600000")
check("sz.000001(已是) ->", bs.to_bs_code("sz.000001"), "sz.000001")
check("830799(北交所) ->", bs.to_bs_code("830799"), "bj.830799")
check("4xxxxx(北交所) ->", bs.to_bs_code("430047"), "bj.430047")
check("空串 ->", bs.to_bs_code(""), "")
check("abc(非数字) ->", bs.to_bs_code("abc"), "")

print("== secid_to_bs_codes ==")
check("1.000001 ->", bs.secid_to_bs_codes("1.000001"), ("sh.000001",))
check("0.000300 ->", bs.secid_to_bs_codes("0.000300"), ("sh.000300", "sz.399300"))
check("0.399006 ->", bs.secid_to_bs_codes("0.399006"), ("sz.399006",))
check("sh.000300(已是baostock代码) ->", bs.secid_to_bs_codes("sh.000300"), ("sh.000300",))
check("sz.399300(已是) ->", bs.secid_to_bs_codes("sz.399300"), ("sz.399300",))
check("1.000688 ->", bs.secid_to_bs_codes("1.000688"), ("sh.000688",))
check("无点 000300 ->", bs.secid_to_bs_codes("000300"), ())
check("空 ->", bs.secid_to_bs_codes(""), ())

print("== _valid_bs_code 闸门 ==")
check("sh.600000 合法", bs._valid_bs_code("sh.600000"), True)
check("sz.000001 合法", bs._valid_bs_code("sz.000001"), True)
check("bj.830799 合法(格式对, baostock无数据)", bs._valid_bs_code("bj.830799"), True)
check("600000 非法(无前缀)", bs._valid_bs_code("600000"), False)
check("sh.60000 非法(5位)", bs._valid_bs_code("sh.60000"), False)
check("11.600000 非法(市场码)", bs._valid_bs_code("11.600000"), False)
check("空 非法", bs._valid_bs_code(""), False)

print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
