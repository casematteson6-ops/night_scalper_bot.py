                            if ok:
                                send_telegram(f"⏰ Session End: Closed {symbol} @ market.")
                                if symbol in active_trades:
                                    del active_trades[symbol]
                            else:
                                send_telegram(f"❌ Failed to close {symbol} at session end: {err}")
                        continue

                    # ── Manage existing trade ──────────────────────────────────
                    if positions:
                        continue  # SL/TP managed by OANDA — just wait

                    # Clean up if position closed externally
                    if not positions and symbol in active_trades:
                        send_telegram(f"✅ {symbol} night trade closed (SL/TP hit).")
                        del active_trades[symbol]

                    if not night:
                        continue  # Outside session — don't open new trades

                    # ── Signal Detection ───────────────────────────────────────
                    df = client.get_candles(symbol, CANDLE_COUNT, GRANULARITY)
                    if df is None or len(df) < BB_PERIOD + ATR_PERIOD + 2:
                        logger.warning(f"⚠️ Not enough data for {symbol}.")
                        continue

                    df   = compute_indicators(df)
                    last = df.iloc[-1]

                    bb_upper = last["bb_upper"]
                    bb_lower = last["bb_lower"]
                    atr      = last["atr"]
                    close    = last["close"]

                    if any(np.isnan(v) for v in [bb_upper, bb_lower, atr]) or atr <= 0:
                        continue

                    sl_dist = ATR_SL_MULT * atr
                    lots    = client.calculate_lots(balance, RISK_PCT, sl_dist, symbol)
                    if lots <= 0:
                        continue

                    # LONG: price below lower BB (mean reversion up)
                    if close < bb_lower:
                        sl = round(close - sl_dist, 5)
                        tp = round(close + ATR_TP_MULT * atr, 5)
                        logger.info(f"🌙 Night LONG {symbol} | Entry:{close} SL:{sl} TP:{tp} Lots:{lots}")
                        order_id, err = client.open_position(symbol, "BUY", lots, sl, tp)
                        if order_id:
                            active_trades[symbol] = {"position_id": order_id, "side": "BUY",
                                                      "sl": sl, "tp": tp, "lots": lots}
                            send_telegram(
                                f"🌙 Night LONG {symbol} opened\n"
                                f"Entry: {close} | SL: {sl} | TP: {tp}\n"
                                f"Lots: {lots} | Risk: ${round(balance * RISK_PCT, 2)}"
                            )
                        else:
                            logger.warning(f"❌ Night LONG {symbol} failed: {err}")
                            send_telegram(f"❌ Night LONG {symbol} failed: {err}")

                    # SHORT: price above upper BB (mean reversion down)
                    elif close > bb_upper:
                        sl = round(close + sl_dist, 5)
                        tp = round(close - ATR_TP_MULT * atr, 5)
                        logger.info(f"🌙 Night SHORT {symbol} | Entry:{close} SL:{sl} TP:{tp} Lots:{lots}")
                        order_id, err = client.open_position(symbol, "SELL", lots, sl, tp)
                        if order_id:
                            active_trades[symbol] = {"position_id": order_id, "side": "SELL",
                                                      "sl": sl, "tp": tp, "lots": lots}
                            send_telegram(
                                f"🌙 Night SHORT {symbol} opened\n"
                                f"Entry: {close} | SL: {sl} | TP: {tp}\n"
                                f"Lots: {lots} | Risk: ${round(balance * RISK_PCT, 2)}"
                            )
                        else:
                            logger.warning(f"❌ Night SHORT {symbol} failed: {err}")
                            send_telegram(f"❌ Night SHORT {symbol} failed: {err}")

                except Exception as e:
                    logger.error(f"❌ Error on {symbol}: {e}")
                    send_telegram(f"❌ Night Scalper error on {symbol}: {e}")

        except Exception as e:
            logger.error(f"🔥 Critical bot error: {e}")
            send_telegram(f"🔥 Night Scalper critical error: {e}")

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    main()
