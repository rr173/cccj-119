# 离线票价清分演练记录（手工账 + 独立小数据核对）

由以下命令生成，输出是真实落库与真实清分结果，不是成功提示：

```
python3 -I tools/build_fixtures.py
python3 -I tools/build_fare_fixtures.py
python3 -I tools/fare_walkthrough.py
```

## 手工账怎么核对

- `tools/fare_walkthrough.py` 内的 `EXPECTED` 是逐段手工账：每段命中规则、
  基础产品、实际付费产品、沿用购票段号、收费、到期秒、用后段数、购票原因。
- 第 4 步把引擎输出逐字段与手工账比较（不只比合计）。
- 第 5 步用本文件内另写的一个极简状态机 `independent_minor_ledger`
  （不 import 清分引擎，只认识手工列出的产品参数）复算逐段收费并再比一遍。
- 第 6 步把手工账逐段金额重新求和，与引擎链总额比较。
- 第 7 步用 fare-v2 重算同一批次证明版本隔离；第 8 步重开库后 JSON 逐字节一致。

## 演示覆盖

| 要求 | 位置 |
| --- | --- |
| 宽规则被高优先级线路规则覆盖 | J9（r_r4 胜过 r_any 风格回退）、J3（r_r2） |
| 同优先级两种写法交叠被预检拦截 | 第 1 步 fare-reject-overlap.zip，含 route/zone 见证 |
| 三段链中间免费不续期、三段恰好到期重买 | J1（leg3 同时列 expired+quota_exhausted） |
| 时间有效但段数耗尽 | J2（仅 quota_exhausted） |
| 跨产品覆盖单向生效 | J3（P_LONG→P_STD 免费）与 J3b（反向重买） |
| 0 元产品与 JPY 零位小数 | J5（0.00 CNY）、J6（250 JPY） |
| 乱序输入、非连续站序、007 | 批次行故意乱序；J1 用 T_WKDY 的 10/20/30/40 与站点 007 |
| 一条链无规则/混币种，同批另一条正常 | J7（no_rule）、J8（mixed_currency），其余 8 条照常结算 |
| 非法精度、悬空覆盖整包拒绝 | 第 1 步 fare-reject-amount.zip / fare-reject-dangling.zip |
| 旧规则版重开库复算相同明细 | 第 8 步 JSON 逐字节一致 |

## 原始输出

```

==============================================================================
0. Static edition under test
==============================================================================
loading dist/demo-v1.zip as demo/v1 (the fare packages bind to it)
  static edition ready                                             OK

==============================================================================
1. Install fare-v1 and the three whole-package rejections
==============================================================================
  fare-v1 installs                                                 OK
  fare-v1 currencies                                               OK
  fare-reject-overlap.zip rejected (rule_overlap)                  OK
  fare-reject-overlap.zip reports rule_overlap                     OK
  overlap witness -> {"priority": 1, "rule_a": "r_any", "rule_b": "r_any_dup", "witness": {"route_id": "R1", "from_zone": "ZB", "to_zone": "ZA"}}
  witness names both rules                                         OK
  witness is a concrete route/zone/zone triple                     OK
  fare-reject-amount.zip rejected (invalid_amount)                 OK
  fare-reject-amount.zip reports invalid_amount                    OK
  fare-reject-dangling.zip rejected (foreign_key)                  OK
  fare-reject-dangling.zip reports foreign_key                     OK

==============================================================================
2. Idempotent reinstall and version-content conflict
==============================================================================
  same version+content is idempotent                               OK
  changed content under same version conflicts                     OK

==============================================================================
3. Install fare-v2 alongside v1; both remain independently queryable
==============================================================================
  fare-v2 installs beside v1                                       OK
  both ready versions present                                      OK
  v1 P_STD still 3.00                                              OK
  v2 P_STD is 3.50                                                 OK

==============================================================================
4. Clear the shuffled batch under fare-v1 (per-leg hand ledger check)
==============================================================================
  batch row count (shuffled input accepted)                        OK
  no batch-level parse problems                                    OK
  J1 status                                                        OK
  J1 currency                                                      OK
  J1 total                                                         OK
  J1 leg count                                                     OK
  J1 leg1 leg_index                                                OK
  J1 leg1 route_id                                                 OK
  J1 leg1 trip_id                                                  OK
  J1 leg1 board_stop_id                                            OK
  J1 leg1 board_sequence                                           OK
  J1 leg1 alight_stop_id                                           OK
  J1 leg1 alight_sequence                                          OK
  J1 leg1 matched_rule_id                                          OK
  J1 leg1 base_product_id                                          OK
  J1 leg1 paid_product_id                                          OK
  J1 leg1 source_leg_index                                         OK
  J1 leg1 charge_text                                              OK
  J1 leg1 ticket_expires_utc                                       OK
  J1 leg1 used_legs_after                                          OK
  J1 leg1 purchase_reasons                                         OK
  J1 leg1 charge currency                                          OK
  J1 leg2 leg_index                                                OK
  J1 leg2 route_id                                                 OK
  J1 leg2 trip_id                                                  OK
  J1 leg2 board_stop_id                                            OK
  J1 leg2 board_sequence                                           OK
  J1 leg2 alight_stop_id                                           OK
  J1 leg2 alight_sequence                                          OK
  J1 leg2 matched_rule_id                                          OK
  J1 leg2 base_product_id                                          OK
  J1 leg2 paid_product_id                                          OK
  J1 leg2 source_leg_index                                         OK
  J1 leg2 charge_text                                              OK
  J1 leg2 ticket_expires_utc                                       OK
  J1 leg2 used_legs_after                                          OK
  J1 leg2 purchase_reasons                                         OK
  J1 leg2 charge currency                                          OK
  J1 leg3 leg_index                                                OK
  J1 leg3 route_id                                                 OK
  J1 leg3 trip_id                                                  OK
  J1 leg3 board_stop_id                                            OK
  J1 leg3 board_sequence                                           OK
  J1 leg3 alight_stop_id                                           OK
  J1 leg3 alight_sequence                                          OK
  J1 leg3 matched_rule_id                                          OK
  J1 leg3 base_product_id                                          OK
  J1 leg3 paid_product_id                                          OK
  J1 leg3 source_leg_index                                         OK
  J1 leg3 charge_text                                              OK
  J1 leg3 ticket_expires_utc                                       OK
  J1 leg3 used_legs_after                                          OK
  J1 leg3 purchase_reasons                                         OK
  J1 leg3 charge currency                                          OK
  J2 status                                                        OK
  J2 currency                                                      OK
  J2 total                                                         OK
  J2 leg count                                                     OK
  J2 leg1 leg_index                                                OK
  J2 leg1 route_id                                                 OK
  J2 leg1 trip_id                                                  OK
  J2 leg1 board_stop_id                                            OK
  J2 leg1 board_sequence                                           OK
  J2 leg1 alight_stop_id                                           OK
  J2 leg1 alight_sequence                                          OK
  J2 leg1 matched_rule_id                                          OK
  J2 leg1 base_product_id                                          OK
  J2 leg1 paid_product_id                                          OK
  J2 leg1 source_leg_index                                         OK
  J2 leg1 charge_text                                              OK
  J2 leg1 ticket_expires_utc                                       OK
  J2 leg1 used_legs_after                                          OK
  J2 leg1 purchase_reasons                                         OK
  J2 leg1 charge currency                                          OK
  J2 leg2 leg_index                                                OK
  J2 leg2 route_id                                                 OK
  J2 leg2 trip_id                                                  OK
  J2 leg2 board_stop_id                                            OK
  J2 leg2 board_sequence                                           OK
  J2 leg2 alight_stop_id                                           OK
  J2 leg2 alight_sequence                                          OK
  J2 leg2 matched_rule_id                                          OK
  J2 leg2 base_product_id                                          OK
  J2 leg2 paid_product_id                                          OK
  J2 leg2 source_leg_index                                         OK
  J2 leg2 charge_text                                              OK
  J2 leg2 ticket_expires_utc                                       OK
  J2 leg2 used_legs_after                                          OK
  J2 leg2 purchase_reasons                                         OK
  J2 leg2 charge currency                                          OK
  J2 leg3 leg_index                                                OK
  J2 leg3 route_id                                                 OK
  J2 leg3 trip_id                                                  OK
  J2 leg3 board_stop_id                                            OK
  J2 leg3 board_sequence                                           OK
  J2 leg3 alight_stop_id                                           OK
  J2 leg3 alight_sequence                                          OK
  J2 leg3 matched_rule_id                                          OK
  J2 leg3 base_product_id                                          OK
  J2 leg3 paid_product_id                                          OK
  J2 leg3 source_leg_index                                         OK
  J2 leg3 charge_text                                              OK
  J2 leg3 ticket_expires_utc                                       OK
  J2 leg3 used_legs_after                                          OK
  J2 leg3 purchase_reasons                                         OK
  J2 leg3 charge currency                                          OK
  J3 status                                                        OK
  J3 currency                                                      OK
  J3 total                                                         OK
  J3 leg count                                                     OK
  J3 leg1 leg_index                                                OK
  J3 leg1 route_id                                                 OK
  J3 leg1 trip_id                                                  OK
  J3 leg1 board_stop_id                                            OK
  J3 leg1 board_sequence                                           OK
  J3 leg1 alight_stop_id                                           OK
  J3 leg1 alight_sequence                                          OK
  J3 leg1 matched_rule_id                                          OK
  J3 leg1 base_product_id                                          OK
  J3 leg1 paid_product_id                                          OK
  J3 leg1 source_leg_index                                         OK
  J3 leg1 charge_text                                              OK
  J3 leg1 ticket_expires_utc                                       OK
  J3 leg1 used_legs_after                                          OK
  J3 leg1 purchase_reasons                                         OK
  J3 leg1 charge currency                                          OK
  J3 leg2 leg_index                                                OK
  J3 leg2 route_id                                                 OK
  J3 leg2 trip_id                                                  OK
  J3 leg2 board_stop_id                                            OK
  J3 leg2 board_sequence                                           OK
  J3 leg2 alight_stop_id                                           OK
  J3 leg2 alight_sequence                                          OK
  J3 leg2 matched_rule_id                                          OK
  J3 leg2 base_product_id                                          OK
  J3 leg2 paid_product_id                                          OK
  J3 leg2 source_leg_index                                         OK
  J3 leg2 charge_text                                              OK
  J3 leg2 ticket_expires_utc                                       OK
  J3 leg2 used_legs_after                                          OK
  J3 leg2 purchase_reasons                                         OK
  J3 leg2 charge currency                                          OK
  J3b status                                                       OK
  J3b currency                                                     OK
  J3b total                                                        OK
  J3b leg count                                                    OK
  J3b leg1 leg_index                                               OK
  J3b leg1 route_id                                                OK
  J3b leg1 trip_id                                                 OK
  J3b leg1 board_stop_id                                           OK
  J3b leg1 board_sequence                                          OK
  J3b leg1 alight_stop_id                                          OK
  J3b leg1 alight_sequence                                         OK
  J3b leg1 matched_rule_id                                         OK
  J3b leg1 base_product_id                                         OK
  J3b leg1 paid_product_id                                         OK
  J3b leg1 source_leg_index                                        OK
  J3b leg1 charge_text                                             OK
  J3b leg1 ticket_expires_utc                                      OK
  J3b leg1 used_legs_after                                         OK
  J3b leg1 purchase_reasons                                        OK
  J3b leg1 charge currency                                         OK
  J3b leg2 leg_index                                               OK
  J3b leg2 route_id                                                OK
  J3b leg2 trip_id                                                 OK
  J3b leg2 board_stop_id                                           OK
  J3b leg2 board_sequence                                          OK
  J3b leg2 alight_stop_id                                          OK
  J3b leg2 alight_sequence                                         OK
  J3b leg2 matched_rule_id                                         OK
  J3b leg2 base_product_id                                         OK
  J3b leg2 paid_product_id                                         OK
  J3b leg2 source_leg_index                                        OK
  J3b leg2 charge_text                                             OK
  J3b leg2 ticket_expires_utc                                      OK
  J3b leg2 used_legs_after                                         OK
  J3b leg2 purchase_reasons                                        OK
  J3b leg2 charge currency                                         OK
  J4 status                                                        OK
  J4 currency                                                      OK
  J4 total                                                         OK
  J4 leg count                                                     OK
  J4 leg1 leg_index                                                OK
  J4 leg1 route_id                                                 OK
  J4 leg1 trip_id                                                  OK
  J4 leg1 board_stop_id                                            OK
  J4 leg1 board_sequence                                           OK
  J4 leg1 alight_stop_id                                           OK
  J4 leg1 alight_sequence                                          OK
  J4 leg1 matched_rule_id                                          OK
  J4 leg1 base_product_id                                          OK
  J4 leg1 paid_product_id                                          OK
  J4 leg1 source_leg_index                                         OK
  J4 leg1 charge_text                                              OK
  J4 leg1 ticket_expires_utc                                       OK
  J4 leg1 used_legs_after                                          OK
  J4 leg1 purchase_reasons                                         OK
  J4 leg1 charge currency                                          OK
  J4 leg2 leg_index                                                OK
  J4 leg2 route_id                                                 OK
  J4 leg2 trip_id                                                  OK
  J4 leg2 board_stop_id                                            OK
  J4 leg2 board_sequence                                           OK
  J4 leg2 alight_stop_id                                           OK
  J4 leg2 alight_sequence                                          OK
  J4 leg2 matched_rule_id                                          OK
  J4 leg2 base_product_id                                          OK
  J4 leg2 paid_product_id                                          OK
  J4 leg2 source_leg_index                                         OK
  J4 leg2 charge_text                                              OK
  J4 leg2 ticket_expires_utc                                       OK
  J4 leg2 used_legs_after                                          OK
  J4 leg2 purchase_reasons                                         OK
  J4 leg2 charge currency                                          OK
  J5 status                                                        OK
  J5 currency                                                      OK
  J5 total                                                         OK
  J5 leg count                                                     OK
  J5 leg1 leg_index                                                OK
  J5 leg1 route_id                                                 OK
  J5 leg1 trip_id                                                  OK
  J5 leg1 board_stop_id                                            OK
  J5 leg1 board_sequence                                           OK
  J5 leg1 alight_stop_id                                           OK
  J5 leg1 alight_sequence                                          OK
  J5 leg1 matched_rule_id                                          OK
  J5 leg1 base_product_id                                          OK
  J5 leg1 paid_product_id                                          OK
  J5 leg1 source_leg_index                                         OK
  J5 leg1 charge_text                                              OK
  J5 leg1 ticket_expires_utc                                       OK
  J5 leg1 used_legs_after                                          OK
  J5 leg1 purchase_reasons                                         OK
  J5 leg1 charge currency                                          OK
  J6 status                                                        OK
  J6 currency                                                      OK
  J6 total                                                         OK
  J6 leg count                                                     OK
  J6 leg1 leg_index                                                OK
  J6 leg1 route_id                                                 OK
  J6 leg1 trip_id                                                  OK
  J6 leg1 board_stop_id                                            OK
  J6 leg1 board_sequence                                           OK
  J6 leg1 alight_stop_id                                           OK
  J6 leg1 alight_sequence                                          OK
  J6 leg1 matched_rule_id                                          OK
  J6 leg1 base_product_id                                          OK
  J6 leg1 paid_product_id                                          OK
  J6 leg1 source_leg_index                                         OK
  J6 leg1 charge_text                                              OK
  J6 leg1 ticket_expires_utc                                       OK
  J6 leg1 used_legs_after                                          OK
  J6 leg1 purchase_reasons                                         OK
  J6 leg1 charge currency                                          OK
  J7 status                                                        OK
  J7 failure code                                                  OK
  J7 returns no partial total                                      OK
  J7 returns no partial legs                                       OK
  J8 status                                                        OK
  J8 failure code                                                  OK
  J8 returns no partial total                                      OK
  J8 returns no partial legs                                       OK
  J9 status                                                        OK
  J9 currency                                                      OK
  J9 total                                                         OK
  J9 leg count                                                     OK
  J9 leg1 leg_index                                                OK
  J9 leg1 route_id                                                 OK
  J9 leg1 trip_id                                                  OK
  J9 leg1 board_stop_id                                            OK
  J9 leg1 board_sequence                                           OK
  J9 leg1 alight_stop_id                                           OK
  J9 leg1 alight_sequence                                          OK
  J9 leg1 matched_rule_id                                          OK
  J9 leg1 base_product_id                                          OK
  J9 leg1 paid_product_id                                          OK
  J9 leg1 source_leg_index                                         OK
  J9 leg1 charge_text                                              OK
  J9 leg1 ticket_expires_utc                                       OK
  J9 leg1 used_legs_after                                          OK
  J9 leg1 purchase_reasons                                         OK
  J9 leg1 charge currency                                          OK

==============================================================================
5. Independent mini ticket machine vs. engine money/state fields
==============================================================================
  mini J1 leg1 paid_product_id                                     OK
  mini J1 leg1 charge_minor                                        OK
  mini J1 leg1 ticket_expires_utc                                  OK
  mini J1 leg1 used_legs_after                                     OK
  mini J1 leg1 purchase_reasons                                    OK
  mini J1 leg1 source_leg_index                                    OK
  mini J1 leg2 paid_product_id                                     OK
  mini J1 leg2 charge_minor                                        OK
  mini J1 leg2 ticket_expires_utc                                  OK
  mini J1 leg2 used_legs_after                                     OK
  mini J1 leg2 purchase_reasons                                    OK
  mini J1 leg2 source_leg_index                                    OK
  mini J1 leg3 paid_product_id                                     OK
  mini J1 leg3 charge_minor                                        OK
  mini J1 leg3 ticket_expires_utc                                  OK
  mini J1 leg3 used_legs_after                                     OK
  mini J1 leg3 purchase_reasons                                    OK
  mini J1 leg3 source_leg_index                                    OK
  mini J2 leg1 paid_product_id                                     OK
  mini J2 leg1 charge_minor                                        OK
  mini J2 leg1 ticket_expires_utc                                  OK
  mini J2 leg1 used_legs_after                                     OK
  mini J2 leg1 purchase_reasons                                    OK
  mini J2 leg1 source_leg_index                                    OK
  mini J2 leg2 paid_product_id                                     OK
  mini J2 leg2 charge_minor                                        OK
  mini J2 leg2 ticket_expires_utc                                  OK
  mini J2 leg2 used_legs_after                                     OK
  mini J2 leg2 purchase_reasons                                    OK
  mini J2 leg2 source_leg_index                                    OK
  mini J2 leg3 paid_product_id                                     OK
  mini J2 leg3 charge_minor                                        OK
  mini J2 leg3 ticket_expires_utc                                  OK
  mini J2 leg3 used_legs_after                                     OK
  mini J2 leg3 purchase_reasons                                    OK
  mini J2 leg3 source_leg_index                                    OK
  mini J3 leg1 paid_product_id                                     OK
  mini J3 leg1 charge_minor                                        OK
  mini J3 leg1 ticket_expires_utc                                  OK
  mini J3 leg1 used_legs_after                                     OK
  mini J3 leg1 purchase_reasons                                    OK
  mini J3 leg1 source_leg_index                                    OK
  mini J3 leg2 paid_product_id                                     OK
  mini J3 leg2 charge_minor                                        OK
  mini J3 leg2 ticket_expires_utc                                  OK
  mini J3 leg2 used_legs_after                                     OK
  mini J3 leg2 purchase_reasons                                    OK
  mini J3 leg2 source_leg_index                                    OK
  mini J3b leg1 paid_product_id                                    OK
  mini J3b leg1 charge_minor                                       OK
  mini J3b leg1 ticket_expires_utc                                 OK
  mini J3b leg1 used_legs_after                                    OK
  mini J3b leg1 purchase_reasons                                   OK
  mini J3b leg1 source_leg_index                                   OK
  mini J3b leg2 paid_product_id                                    OK
  mini J3b leg2 charge_minor                                       OK
  mini J3b leg2 ticket_expires_utc                                 OK
  mini J3b leg2 used_legs_after                                    OK
  mini J3b leg2 purchase_reasons                                   OK
  mini J3b leg2 source_leg_index                                   OK
  mini J4 leg1 paid_product_id                                     OK
  mini J4 leg1 charge_minor                                        OK
  mini J4 leg1 ticket_expires_utc                                  OK
  mini J4 leg1 used_legs_after                                     OK
  mini J4 leg1 purchase_reasons                                    OK
  mini J4 leg1 source_leg_index                                    OK
  mini J4 leg2 paid_product_id                                     OK
  mini J4 leg2 charge_minor                                        OK
  mini J4 leg2 ticket_expires_utc                                  OK
  mini J4 leg2 used_legs_after                                     OK
  mini J4 leg2 purchase_reasons                                    OK
  mini J4 leg2 source_leg_index                                    OK
  mini J5 leg1 paid_product_id                                     OK
  mini J5 leg1 charge_minor                                        OK
  mini J5 leg1 ticket_expires_utc                                  OK
  mini J5 leg1 used_legs_after                                     OK
  mini J5 leg1 purchase_reasons                                    OK
  mini J5 leg1 source_leg_index                                    OK
  mini J6 leg1 paid_product_id                                     OK
  mini J6 leg1 charge_minor                                        OK
  mini J6 leg1 ticket_expires_utc                                  OK
  mini J6 leg1 used_legs_after                                     OK
  mini J6 leg1 purchase_reasons                                    OK
  mini J6 leg1 source_leg_index                                    OK
  mini J9 leg1 paid_product_id                                     OK
  mini J9 leg1 charge_minor                                        OK
  mini J9 leg1 ticket_expires_utc                                  OK
  mini J9 leg1 used_legs_after                                     OK
  mini J9 leg1 purchase_reasons                                    OK
  mini J9 leg1 source_leg_index                                    OK

==============================================================================
6. Hand totals recomputed from the per-leg charges
==============================================================================
  J1 hand-summed total matches                                     OK
  J2 hand-summed total matches                                     OK
  J3 hand-summed total matches                                     OK
  J3b hand-summed total matches                                    OK
  J4 hand-summed total matches                                     OK
  J5 hand-summed total matches                                     OK
  J6 hand-summed total matches                                     OK
  J9 hand-summed total matches                                     OK

==============================================================================
7. fare-v2 recomputes the same batch differently; v1 is untouched
==============================================================================
  v2 J1 total changes (10.00 expected)                             OK
  v1 J1 total stays 6.00                                           OK
  v2 content hash differs from v1                                  OK

==============================================================================
8. Reopen the database: historical fare-v1 reproduces the same detail
==============================================================================
  reopened settle is byte-identical in JSON                        OK
  rejection ledger survives reopen                                 OK

==============================================================================
9. Static and realtime entry points still work after fare install
==============================================================================
  static list still serves demo/v1                                 OK
  fare-packages CLI lists versions                                 OK

all fare drill checks passed; database left at /tmp/fare-drill-2_25hjyi/fare.db
```
