import os
import sqlite3
import subprocess
from configparser import ConfigParser
from datetime import datetime

from btb_manager_telegram import BOUGHT, BUYING, SELLING, SOLD, logger, settings
from btb_manager_telegram.binance_api_utils import get_current_price
from btb_manager_telegram.utils import (
    find_and_kill_binance_trade_bot_process,
    format_float,
    get_binance_trade_bot_process,
    is_btb_bot_update_available,
    is_tg_bot_update_available,
    telegram_text_truncator,
)


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def current_value():
    logger.info("Current value button pressed.")
    db_file_path = os.path.join(settings.ROOT_PATH, "data/crypto_trading.db")
    message = []

    if os.path.exists(db_file_path):
        try:
            con = sqlite3.connect(db_file_path)
            con.row_factory = dict_factory
            cur = con.cursor()

            # Get coin symbol, bridge symbol, order state, order size, initial buying price and current USD value for
            # each coin with a position size > $10
            try:
                cur.execute(
                    """
                    SELECT th.id,
                           th.alt_coin_id,
                           th.crypto_coin_id,
                           th.state,
                           th.alt_trade_amount,
                           th.crypto_starting_balance,
                           th.crypto_trade_amount,
                           th.datetime               AS thdatetime,
                           cv.balance,
                           cv.usd_price,
                           cv.btc_price,
                           cv.datetime               AS cvdatetime,
                           cv.balance * cv.usd_price AS current_usd_value
                    FROM   trade_history th
                           JOIN coin_value cv
                             ON cv.coin_id = th.alt_coin_id
                    WHERE  current_usd_value > 10
                           AND cv.datetime = (SELECT Max(datetime)
                                              FROM   coin_value)
                           AND th.id = (SELECT Max(id)
                                        FROM   trade_history
                                        WHERE  alt_coin_id = th.alt_coin_id);
                    """
                )
                rows = cur.fetchall()

                all_coins = []
                overall_usd_value = 0.0
                overall_btc_value = 0.0
                overall_usd_bought_for = 0.0
                overall_usd_value_1_day = 0.0
                overall_usd_value_7_day = 0.0
                overall_bridge = ''

                for row in rows:
                    current_coin = row['alt_coin_id']
                    bridge = row['crypto_coin_id']
                    order_size = float(row['crypto_starting_balance'])
                    alt_amount = float(row['alt_trade_amount'])
                    buy_price = float(row['crypto_trade_amount'])
                    balance = float(row['balance'])
                    usd_price = float(row['usd_price'])
                    btc_price = float(row['btc_price'])
                    last_update = row['cvdatetime']

                    if current_coin is None:
                        raise Exception()
                    if row['state'] == "ORDERED":
                        return [
                            f"A buy order of `{format_float(order_size)}` *{bridge}* is currently placed on coin *{current_coin}*.\n\n"
                            f"_Waiting for buy order to complete_.".replace(".", "\.")
                        ]

                    try:
                        cur.execute(
                            f"""SELECT cv.balance,
                                       cv.usd_price
                                FROM   coin_value AS cv
                                WHERE  cv.coin_id = (SELECT th.alt_coin_id
                                                     FROM   trade_history AS th
                                                     WHERE  th.alt_coin_id = '{current_coin}'
                                                            AND th.datetime > Datetime ('now', '-1 day')
                                                            AND th.selling = 0
                                                     ORDER  BY th.datetime ASC
                                                     LIMIT  1)
                                       AND cv.datetime > (SELECT th.datetime
                                                          FROM   trade_history AS th
                                                          WHERE  th.alt_coin_id = '{current_coin}'
                                                                 AND th.datetime > Datetime ('now', '-1 day')
                                                                 AND th.selling = 0
                                                          ORDER  BY th.datetime ASC
                                                          LIMIT  1)
                                ORDER  BY cv.datetime ASC
                                LIMIT  1; """
                        )
                        query_1_day = cur.fetchone()

                        cur.execute(
                            f"""SELECT cv.balance,
                                       cv.usd_price
                                FROM   coin_value AS cv
                                WHERE  cv.coin_id = (SELECT th.alt_coin_id
                                                     FROM   trade_history AS th
                                                     WHERE  th.alt_coin_id = '{current_coin}'
                                                            AND th.datetime > Datetime ('now', '-7 day')
                                                            AND th.selling = 0
                                                     ORDER  BY th.datetime ASC
                                                     LIMIT  1)
                                       AND cv.datetime > (SELECT th.datetime
                                                          FROM   trade_history AS th
                                                          WHERE  th.alt_coin_id = '{current_coin}'
                                                                 AND th.datetime > Datetime ('now', '-7 day')
                                                                 AND th.selling = 0
                                                          ORDER  BY th.datetime ASC
                                                          LIMIT  1)
                                ORDER  BY cv.datetime ASC
                                LIMIT  1; """
                        )
                        query_7_day = cur.fetchone()

                        if balance is None:
                            balance = 0
                        if usd_price is None:
                            usd_price = 0
                        if btc_price is None:
                            btc_price = 0
                        last_update = datetime.strptime(last_update, "%Y-%m-%d %H:%M:%S.%f")

                        if (query_1_day is not None
                                and all(elem is not None for elem in query_1_day)
                                and usd_price != 0):
                            balance_1_day = query_1_day['balance']
                            usd_price_1_day = query_1_day['usd_price']
                            overall_usd_value_1_day = balance_1_day * usd_price_1_day
                        if (query_7_day is not None
                                and all(elem is not None for elem in query_7_day)
                                and usd_price != 0):
                            balance_7_day = query_7_day['balance']
                            usd_price_7_day = query_7_day['usd_price']
                            overall_usd_value_7_day = balance_7_day * usd_price_7_day
                    except Exception as e:
                        logger.error(
                            f"❌ Unable to fetch current coin information from database: {e}",
                            exc_info=True,
                        )
                        con.close()
                        return [
                            "❌ Unable to fetch current coin information from database\.",
                            "⚠ If you tried using the `Current value` button during a trade please try again after the trade has been completed\.",
                        ]

                    # Generate message
                    try:
                        change_in_value = round((balance * usd_price - buy_price) / buy_price * 100, 2)
                        usd_value = round(balance * usd_price, 2)
                        btc_value = balance * btc_price
                        usd_bought_for = round(buy_price, 2)
                        m_list = [
                            f"\nLast update: `{last_update.strftime('%H:%M:%S %d/%m/%Y')}`\n\n"
                            f"*Current coin {current_coin}*\n"
                            f"\t• Balance: `{format_float(balance)}` *{current_coin}*\n"
                            f"\t• Exchange rate purchased: \n\t\t\t`{format_float(buy_price / alt_amount)}` *{bridge}*/*{current_coin}* \n"
                            f"\t• Exchange rate now: \n\t\t\t`{format_float(usd_price)}` *{bridge}*/*{current_coin}*\n"
                            f"\t• Bought for: `{usd_bought_for}` *{bridge}*\n"
                            f"\t• Current value: `${usd_value}`\n"
                            f"\t• Current value: `₿{format_float(btc_value)}`\n"
                            f"\t• Change in value: `{change_in_value}`*%*\n"
                        ]
                        message += telegram_text_truncator(m_list)

                        all_coins.append(current_coin)
                        overall_usd_value += usd_value
                        overall_btc_value += btc_value
                        overall_usd_bought_for += usd_bought_for
                        overall_bridge = bridge
                    except Exception as e:
                        logger.error(
                            f"❌ Something went wrong, unable to generate value at this time: {e}",
                            exc_info=True,
                        )
                        con.close()
                        return [
                            "❌ Something went wrong, unable to generate value at this time\."
                        ]
                percent_change = 0.0
                return_rate_1_day = 0.0
                return_rate_7_day = 0.0
                if overall_usd_bought_for != 0 and overall_usd_value_1_day != 0 and overall_usd_value_7_day != 0:
                    percent_change = (overall_usd_value - overall_usd_bought_for) / overall_usd_bought_for * 100
                    return_rate_1_day = round(
                        (overall_usd_value - overall_usd_value_1_day) / overall_usd_value_1_day * 100, 2
                    )
                    return_rate_7_day = round(
                        (overall_usd_value - overall_usd_value_7_day) / overall_usd_value_7_day * 100, 2
                    )
                overall_value = [
                    f"*All coins: {' & '.join(all_coins)}*\n"
                    f" • Total value: `${round(overall_usd_value, 2)}`\n"
                    f" • Total value: `₿{format_float(overall_btc_value)}`\n"
                    f" • Total bought: `{round(overall_usd_bought_for, 2)}` *{overall_bridge}*\n"
                    f" • Total value change: `{round(percent_change, 2)}`*%*\n\n"
                    f"_*1 day* value change USD_: `{return_rate_1_day}`*%*\n"
                    f"_*7 day* value change USD_: `{return_rate_7_day}`*%*\n"
                ]
                message += telegram_text_truncator(overall_value)
            except Exception as e:
                logger.error(
                    f"❌ Unable to fetch current coin from database: {e}", exc_info=True
                )
                con.close()
                return ["❌ Unable to fetch current coin from database\."]

            con.close()
        except Exception as e:
            logger.error(
                f"❌ Unable to perform actions on the database: {e}", exc_info=True
            )
    else:
        message.append(f"⚠ Unable to find database file at `{db_file_path}`\.")
    print(message)
    return message


def check_progress():
    logger.info("Progress button pressed.")

    db_file_path = os.path.join(settings.ROOT_PATH, "data/crypto_trading.db")
    message = [f"⚠ Unable to find database file at `{db_file_path}`\."]
    if os.path.exists(db_file_path):
        try:
            con = sqlite3.connect(db_file_path)
            cur = con.cursor()

            # Get progress information
            try:
                cur.execute(
                    """
                    SELECT 
                      th1.alt_coin_id AS coin, 
                      th1.alt_trade_amount AS amount, 
                      th1.crypto_trade_amount AS priceInUSD, 
                      (
                        th1.alt_trade_amount - (
                          SELECT 
                            th2.alt_trade_amount 
                          FROM 
                            trade_history th2 
                          WHERE 
                            th2.state = 'COMPLETE' 
                            AND th2.alt_coin_id = th1.alt_coin_id 
                            AND th1.datetime > th2.datetime 
                            AND th2.selling = 0 
                          ORDER BY 
                            th2.datetime DESC 
                          LIMIT 
                            1
                        )
                      ) AS CHANGE, 
                      (
                        SELECT 
                          th2.datetime 
                        FROM 
                          trade_history th2 
                        WHERE 
                          th2.state = 'COMPLETE' 
                          AND th2.alt_coin_id = th1.alt_coin_id 
                          AND th1.datetime > th2.datetime 
                          AND th2.selling = 0 
                        ORDER BY 
                          th2.datetime DESC 
                        LIMIT 
                          1
                      ) AS pre_last_trade_date, 
                      datetime, 
                      (
                        SELECT 
                          SUM(d.usd_amount) 
                        FROM 
                          deposits d 
                        WHERE 
                          d.datetime > (
                            SELECT 
                              th2.datetime 
                            FROM 
                              trade_history th2 
                            WHERE 
                              th2.state = 'COMPLETE' 
                              AND th2.alt_coin_id = th1.alt_coin_id 
                              AND th1.datetime > th2.datetime 
                              AND th2.selling = 0 
                            ORDER BY 
                              th2.datetime DESC 
                            LIMIT 
                              1
                          ) AND d.datetime < th1.datetime
                      ) AS deposit_amt 
                    FROM 
                      trade_history th1 
                    WHERE 
                      th1.state = 'COMPLETE' 
                      AND th1.selling = 0 
                    ORDER BY 
                      th1.datetime DESC 
                    LIMIT 
                      15;

                    """
                )
                query = cur.fetchall()

                # Generate message
                m_list = ["Current coin amount progress:\n\n"]
                for coin in query:
                    last_trade_date = datetime.strptime(coin[5], "%Y-%m-%d %H:%M:%S.%f")
                    if coin[4] is None:
                        pre_last_trade_date = datetime.strptime(
                            coin[5], "%Y-%m-%d %H:%M:%S.%f"
                        )
                    else:
                        pre_last_trade_date = datetime.strptime(
                            coin[4], "%Y-%m-%d %H:%M:%S.%f"
                        )
                    coin_change = coin[3]
                    if coin[6] is not None:
                        coin_price = coin[2] / coin[1]
                        deposited_coin_amt = coin[6] / coin_price
                        coin_change -= deposited_coin_amt

                    time_passed = last_trade_date - pre_last_trade_date
                    last_trade_date = last_trade_date.strftime("%H:%M:%S %d/%m/%Y")
                    nl = "\n"
                    tab = "\t"
                    m_list.append(
                        f"*{coin[0]}*\n"
                        f"\t• Amount: `{format_float(coin[1])}` *{coin[0]}*\n"
                        f"\t• Price: `${round(coin[2], 2)}`\n"
                        f"\t• Change: {f'`{format_float(coin_change)}` *{coin[0]}*{nl}{tab}{tab}{tab}`{round(coin_change / (coin[1] - coin_change) * 100, 2)}`*%* in {time_passed.days} days, {time_passed.seconds // 3600} hours' if coin[3] is not None else f'`{coin[3]}`'}\n"
                        f"\t• Trade datetime:`\n {last_trade_date}`\n\n".replace(".", "\.")
                    )

                message = telegram_text_truncator(m_list)
                con.close()
            except Exception as e:
                logger.error(
                    f"❌ Unable to fetch progress information from database: {e}",
                    exc_info=True,
                )
                con.close()
                return ["❌ Unable to fetch progress information from database\."]
        except Exception as e:
            logger.error(
                f"❌ Unable to perform actions on the database: {e}", exc_info=True
            )
            message = ["❌ Unable to perform actions on the database\."]
    return message


def current_ratios():
    logger.info("Current ratios button pressed.")

    db_file_path = os.path.join(settings.ROOT_PATH, "data/crypto_trading.db")
    user_cfg_file_path = os.path.join(settings.ROOT_PATH, "user.cfg")
    message = [f"⚠ Unable to find database file at `{db_file_path}`\."]
    if os.path.exists(db_file_path):
        try:
            # Get bridge currency symbol
            with open(user_cfg_file_path) as cfg:
                config = ConfigParser()
                config.read_file(cfg)
                bridge = config.get("binance_user_config", "bridge")
                scout_multiplier = config.get("binance_user_config", "scout_multiplier")

            con = sqlite3.connect(db_file_path)
            cur = con.cursor()

            # Get current coin symbol
            try:
                cur.execute(
                    """SELECT alt_coin_id FROM trade_history ORDER BY datetime DESC LIMIT 1;"""
                )
                current_coin = cur.fetchone()[0]
                if current_coin is None:
                    raise Exception()
            except Exception as e:
                logger.error(
                    f"❌ Unable to fetch current coin from database: {e}", exc_info=True
                )
                con.close()
                return ["❌ Unable to fetch current coin from database\."]

            # Get prices and ratios of all alt coins
            try:
                cur.execute(
                    f"""SELECT sh.datetime, p.to_coin_id, sh.other_coin_price, ( ( ( current_coin_price / other_coin_price ) - 0.001 * '{scout_multiplier}' * ( current_coin_price / other_coin_price ) ) - sh.target_ratio ) AS 'ratio_dict' FROM scout_history sh JOIN pairs p ON p.id = sh.pair_id WHERE p.from_coin_id='{current_coin}' AND p.from_coin_id = ( SELECT alt_coin_id FROM trade_history ORDER BY datetime DESC LIMIT 1) ORDER BY sh.datetime DESC LIMIT ( SELECT count(DISTINCT pairs.to_coin_id) FROM pairs JOIN coins ON coins.symbol = pairs.to_coin_id WHERE coins.enabled = 1 AND pairs.from_coin_id='{current_coin}');"""
                )
                query = cur.fetchall()

                # Generate message
                last_update = datetime.strptime(query[0][0], "%Y-%m-%d %H:%M:%S.%f")
                query = sorted(query, key=lambda k: k[-1], reverse=True)

                m_list = [
                    f"\nLast update: `{last_update.strftime('%H:%M:%S %d/%m/%Y')}`\n\n"
                    f"*Coin ratios compared to {current_coin} in decreasing order:*\n".replace(
                        ".", "\."
                    )
                ]
                for coin in query:
                    m_list.append(
                        f"*{coin[1]}*:\n"
                        f"\t• Price: `{coin[2]}` {bridge}\n"
                        f"\t• Ratio: `{format_float(coin[3])}`\n\n".replace(".", "\.")
                    )

                message = telegram_text_truncator(m_list)
                con.close()
            except Exception as e:
                logger.error(
                    f"❌ Something went wrong, unable to generate ratios at this time: {e}",
                    exc_info=True,
                )
                con.close()
                return [
                    "❌ Something went wrong, unable to generate ratios at this time\.",
                    "⚠ Please make sure logging for _Binance Trade Bot_ is enabled\.",
                ]
        except Exception as e:
            logger.error(
                f"❌ Unable to perform actions on the database: {e}", exc_info=True
            )
            message = ["❌ Unable to perform actions on the database\."]
    return message


def next_coin():
    logger.info("Next coin button pressed.")

    db_file_path = os.path.join(settings.ROOT_PATH, "data/crypto_trading.db")
    user_cfg_file_path = os.path.join(settings.ROOT_PATH, "user.cfg")
    message = [f"⚠ Unable to find database file at `{db_file_path}`\."]
    if os.path.exists(db_file_path):
        try:
            # Get bridge currency symbol
            with open(user_cfg_file_path) as cfg:
                config = ConfigParser()
                config.read_file(cfg)
                bridge = config.get("binance_user_config", "bridge")
                scout_multiplier = config.get("binance_user_config", "scout_multiplier")

            con = sqlite3.connect(db_file_path)
            con.row_factory = dict_factory
            cur = con.cursor()

            # Get prices and percentages for a jump to the next coin
            try:
                message = []
                cur.execute(
                    """
                    SELECT th.alt_coin_id
                    FROM   trade_history th
                           JOIN coin_value cv
                             ON cv.coin_id = th.alt_coin_id
                    WHERE  cv.balance * cv.usd_price > 10
                           AND cv.datetime = (SELECT Max(datetime)
                                              FROM   coin_value)
                           AND th.id = (SELECT Max(id)
                                        FROM   trade_history
                                        WHERE  alt_coin_id = th.alt_coin_id);
                    """
                )
                active_coins = cur.fetchall()

                for active_coin in active_coins:
                    active_coin_id = active_coin['alt_coin_id']
                    cur.execute(
                        f"""
                        SELECT   p.to_coin_id AS other_coin,
                                 sh.other_coin_price,
                                 (current_coin_price - 0.00075 * '{scout_multiplier}' * current_coin_price) / sh.target_ratio                         AS 'price_needs_to_drop_to',
                                 ((current_coin_price - 0.00075 * '{scout_multiplier}' * current_coin_price) / sh.target_ratio) / sh.other_coin_price AS 'percentage'
                        FROM     scout_history sh
                        JOIN     pairs p
                        ON       p.id = sh.pair_id
                        WHERE    p.from_coin_id = '{active_coin_id}'
                        ORDER BY sh.datetime DESC,
                                 percentage DESC limit
                                 (
                                          SELECT   count(DISTINCT p.to_coin_id)
                                          FROM     scout_history sh
                                          JOIN     pairs AS p
                                          ON       p.id = sh.pair_id
                                          JOIN     coins AS c
                                          ON       c.symbol = p.to_coin_id
                                          WHERE    p.from_coin_id = '{active_coin_id}'
                                          AND      c.enabled = 1
                                          ORDER BY sh.datetime DESC);
                        """
                    )
                    query = cur.fetchall()

                    m_list = [f"Next coin from *{active_coin_id}*\n\n"]
                    for coin in query:
                        percentage = round(coin['percentage'] * 100, 2)
                        m_list.append(
                            f"*{coin['other_coin']} \(`{format_float(percentage)}`%\)*\n"
                            f"\t• Current Price: `{format_float(round(coin['other_coin_price'], 8))}` {bridge}\n"
                            f"\t• Target Price: `{format_float(round(coin['price_needs_to_drop_to'], 8))}` {bridge}\n\n".replace(
                                ".", "\."
                            )
                        )

                    message += telegram_text_truncator(m_list)
                con.close()
            except Exception as e:
                logger.error(
                    f"❌ Something went wrong, unable to generate next coin at this time: {e}",
                    exc_info=True,
                )
                con.close()
                return [
                    "❌ Something went wrong, unable to generate next coin at this time\.",
                    "⚠ Please make sure logging for _Binance Trade Bot_ is enabled\.",
                ]
        except Exception as e:
            logger.error(
                f"❌ Unable to perform actions on the database: {e}", exc_info=True
            )
            message = ["❌ Unable to perform actions on the database\."]
    return message


def check_status():
    logger.info("Check status button pressed.")

    message = "⚠ Binance Trade Bot is not running."
    if get_binance_trade_bot_process():
        message = "✔ Binance Trade Bot is running."
    return message


def trade_history():
    logger.info("Trade history button pressed.")

    db_file_path = os.path.join(settings.ROOT_PATH, "data/crypto_trading.db")
    message = [f"⚠ Unable to find database file at `{db_file_path}`\."]
    if os.path.exists(db_file_path):
        try:
            con = sqlite3.connect(db_file_path)
            cur = con.cursor()

            # Get last 10 trades
            try:
                cur.execute(
                    """SELECT alt_coin_id, crypto_coin_id, selling, state, alt_trade_amount, crypto_trade_amount, datetime FROM trade_history ORDER BY datetime DESC LIMIT 10;"""
                )
                query = cur.fetchall()

                m_list = [
                    f"Last **{10 if len(query) > 10 else len(query)}** trades:\n\n"
                ]
                for trade in query:
                    if trade[4] is None:
                        continue
                    date = datetime.strptime(trade[6], "%Y-%m-%d %H:%M:%S.%f")
                    m_list.append(
                        f"`{date.strftime('%H:%M:%S %d/%m/%Y')}`\n"
                        f"*{'Sold' if trade[2] else 'Bought'}* `{format_float(trade[4])}` *{trade[0]}*{f' for `{format_float(trade[5])}` *{trade[1]}*' if trade[5] is not None else ''}\n"
                        f"Status: _*{trade[3]}*_\n\n".replace(".", "\.")
                    )

                message = telegram_text_truncator(m_list)
                con.close()
            except Exception as e:
                logger.error(
                    f"❌ Something went wrong, unable to generate trade history at this time: {e}",
                    exc_info=True,
                )
                con.close()
                return [
                    "❌ Something went wrong, unable to generate trade history at this time\."
                ]
        except Exception as e:
            logger.error(
                f"❌ Unable to perform actions on the database: {e}", exc_info=True
            )
            message = ["❌ Unable to perform actions on the database\."]
    return message


def start_bot():
    logger.info("Start bot button pressed.")

    message = "⚠ Binance Trade Bot is already running\."
    if not get_binance_trade_bot_process():
        if os.path.isfile(settings.PYTHON_PATH):
            if os.path.exists(os.path.join(settings.ROOT_PATH, "binance_trade_bot/")):
                subprocess.call(
                    f"cd {settings.ROOT_PATH} && {settings.PYTHON_PATH} -m binance_trade_bot &",
                    shell=True,
                )
                if get_binance_trade_bot_process():
                    message = "✔ Binance Trade Bot successfully started\."
                else:
                    message = "❌ Unable to start Binance Trade Bot\."
            else:
                message = (
                    f"❌ Unable to find _Binance Trade Bot_ installation at `{settings.ROOT_PATH}`\.\n"
                    f"Make sure the `binance-trade-bot` and `BTB-manager-telegram` are in the same parent directory\."
                )
        else:
            message = f"❌ Unable to find python binary at `{settings.PYTHON_PATH}`\.\n"
    return message


def stop_bot():
    logger.info("Stop bot button pressed.")

    message = "⚠ Binance Trade Bot is not running."
    if get_binance_trade_bot_process():
        find_and_kill_binance_trade_bot_process()
        if not get_binance_trade_bot_process():
            message = "✔ Successfully stopped the bot."
        else:
            message = (
                "❌ Unable to stop Binance Trade Bot.\n\n"
                "If you are running the telegram bot on Windows make sure to run with administrator privileges."
            )
    return message


def read_log():
    logger.info("Read log button pressed.")

    log_file_path = os.path.join(settings.ROOT_PATH, "logs/crypto_trading.log")
    message = f"❌ Unable to find log file at `{log_file_path}`.".replace(".", "\.")
    if os.path.exists(log_file_path):
        with open(log_file_path) as f:
            file_content = f.read().replace(".", "\.")[-4000:]
            message = (
                f"Last *4000* characters in log file:\n\n"
                f"```\n"
                f"{file_content}\n"
                f"```"
            )
    return message


def delete_db():
    logger.info("Delete database button pressed.")

    message = "⚠ Please stop Binance Trade Bot before deleting the database file\."
    delete = False
    db_file_path = os.path.join(settings.ROOT_PATH, "data/crypto_trading.db")
    if not get_binance_trade_bot_process():
        if os.path.exists(db_file_path):
            message = (
                "Are you sure you want to delete the database file and clear the logs?"
            )
            delete = True
        else:
            message = f"⚠ Unable to find database file at `{db_file_path}`.".replace(
                ".", "\."
            )
    return [message, delete]


def edit_user_cfg():
    logger.info("Edit user configuration button pressed.")

    message = "⚠ Please stop Binance Trade Bot before editing user configuration file\."
    edit = False
    user_cfg_file_path = os.path.join(settings.ROOT_PATH, "user.cfg")
    if not get_binance_trade_bot_process():
        if os.path.exists(user_cfg_file_path):
            with open(user_cfg_file_path) as f:
                message = (
                    f"Current configuration file is:\n\n"
                    f"```\n"
                    f"{f.read()}\n"
                    f"```\n\n"
                    f"_*Please reply with a message containing the updated configuration*_.\n\n"
                    f"Write /stop to stop editing and exit without changes.".replace(
                        ".", "\."
                    )
                )
                edit = True
        else:
            message = f"❌ Unable to find user configuration file at `{user_cfg_file_path}`.".replace(
                ".", "\."
            )
    return [message, edit]


def edit_coin():
    logger.info("Edit coin list button pressed.")

    message = "⚠ Please stop Binance Trade Bot before editing the coin list\."
    edit = False
    coin_file_path = os.path.join(settings.ROOT_PATH, "supported_coin_list")
    if not get_binance_trade_bot_process():
        if os.path.exists(coin_file_path):
            with open(coin_file_path) as f:
                message = (
                    f"Current coin list is:\n\n"
                    f"```\n{f.read()}\n```\n\n"
                    f"_*Please reply with a message containing the updated coin list*_.\n\n"
                    f"Write /stop to stop editing and exit without changes.".replace(
                        ".", "\."
                    )
                )
                edit = True
        else:
            message = f"❌ Unable to find coin list file at `{coin_file_path}`.".replace(
                ".", "\."
            )
    return [message, edit]


def export_db():
    logger.info("Export database button pressed.")

    message = "⚠ Please stop Binance Trade Bot before exporting the database file\."
    db_file_path = os.path.join(settings.ROOT_PATH, "data/crypto_trading.db")
    fil = None
    if not get_binance_trade_bot_process():
        if os.path.exists(db_file_path):
            with open(db_file_path, "rb") as db:
                fil = db.read()
            message = "Here is your database file:"
        else:
            message = "❌ Unable to Export the database file\."
    return [message, fil]


def update_tg_bot():
    logger.info("⬆ Update Telegram Bot button pressed.")

    message = "Your BTB Manager Telegram installation is already up to date\."
    upd = False
    to_update = is_tg_bot_update_available()
    if to_update is not None:
        if to_update:
            message = (
                "An update for BTB Manager Telegram is available\.\n"
                "Would you like to update now?"
            )
            upd = True
    else:
        message = (
            "Error while trying to fetch BTB Manager Telegram version information\."
        )
    return [message, upd]


def update_btb():
    logger.info("⬆ Update Binance Trade Bot button pressed.")

    message = "Your Binance Trade Bot installation is already up to date\."
    upd = False
    to_update = is_btb_bot_update_available()
    if to_update is not None:
        if to_update:
            upd = True
            message = (
                "An update for Binance Trade Bot is available\.\n"
                "Would you like to update now?"
            )
    else:
        message = "Error while trying to fetch Binance Trade Bot version information\."
    return [message, upd]


def panic_btn():
    logger.info("🚨 Panic Button button pressed.")

    # Check if open orders / not in usd
    db_file_path = os.path.join(settings.ROOT_PATH, "data/crypto_trading.db")
    if not os.path.exists(db_file_path):
        return ["ERROR: Database file not found\.", -1]

    user_cfg_file_path = os.path.join(settings.ROOT_PATH, "user.cfg")
    if not os.path.exists(user_cfg_file_path):
        return ["ERROR: `user.cfg` file not found\.", -1]

    try:
        con = sqlite3.connect(db_file_path)
        cur = con.cursor()

        # Get last trade
        try:
            cur.execute(
                """SELECT alt_coin_id, crypto_coin_id, selling, state, alt_trade_amount, crypto_trade_amount FROM trade_history ORDER BY datetime DESC LIMIT 1;"""
            )
            (
                alt_coin_id,
                crypto_coin_id,
                selling,
                state,
                alt_trade_amount,
                crypto_trade_amount,
            ) = cur.fetchone()

            if not selling:
                price_old = crypto_trade_amount / alt_trade_amount
                price_now = get_current_price(alt_coin_id, crypto_coin_id)
                if state == "COMPLETE":
                    return [
                        f"You are currently holding `{round(alt_trade_amount, 6)}` *{alt_coin_id}* bought for `{round(crypto_trade_amount, 2)}` *{crypto_coin_id}*.\n\n"
                        f"Exchange rate when bought:\n"
                        f"`{round(price_old, 4)}` *{crypto_coin_id}*/*{alt_coin_id}*\n\n"
                        f"Current exchange rate:\n"
                        f"`{round(price_now, 4)}` *{crypto_coin_id}*/*{alt_coin_id}*\n\n"
                        f"Current value:\n"
                        f"`{round(price_now * alt_trade_amount, 4)}` *{crypto_coin_id}*\n\n"
                        f"Change:\n"
                        f"`{round((price_now - price_old) / price_old * 100, 2)}` *%*\n\n"
                        f"Would you like to stop _Binance Trade Bot_ and sell at market price?".replace(
                            ".", "\."
                        ),
                        BOUGHT,
                    ]
                else:
                    return [
                        f"You have an open buy order of `{alt_trade_amount}` *{alt_coin_id}* for `{crypto_trade_amount}` *{crypto_coin_id}*.\n\n"
                        f"Limit buy at price:\n"
                        f"`{round(price_old, 4)}` *{crypto_coin_id}*/*{alt_coin_id}*\n\n"
                        f"Current exchange rate:\n"
                        f"`{round(price_now, 4)}` *{crypto_coin_id}*/*{alt_coin_id}*\n\n"
                        f"Change:\n"
                        f"`{round((price_now - price_old) / price_old * 100, 2)}` *%*\n\n"
                        f"Would you like to stop _Binance Trade Bot_ and cancel the open order?".replace(
                            ".", "\."
                        ),
                        BUYING,
                    ]
            else:
                if state == "COMPLETE":
                    return [
                        f"Your balance is already in *{crypto_coin_id}*.\n\n"
                        f"Would you like to stop _Binance Trade Bot_?".replace(
                            ".", "\."
                        ),
                        SOLD,
                    ]
                else:
                    price_old = crypto_trade_amount / alt_trade_amount
                    price_now = get_current_price(alt_coin_id, crypto_coin_id)
                    return [
                        f"You have an open sell order of `{alt_trade_amount}` *{alt_coin_id}* for `{crypto_trade_amount}` *{crypto_coin_id}*.\n\n"
                        f"Limit sell at price:\n"
                        f"`{round(price_old, 4)}` *{crypto_coin_id}*/*{alt_coin_id}*\n\n"
                        f"Current exchange rate:\n"
                        f"`{round(price_now, 4)}` *{crypto_coin_id}*/*{alt_coin_id}*\n\n"
                        f"Change:\n"
                        f"`{round((price_now - price_old) / price_old * 100, 2)}` *%*\n\n"
                        f"Would you like to stop _Binance Trade Bot_ and cancel the open order?".replace(
                            ".", "\."
                        ),
                        SELLING,
                    ]

            con.close()
        except Exception as e:
            con.close()
            logger.error(
                f"❌ Something went wrong, the panic button is not working at this time: {e}",
                exc_info=True,
            )
            return [
                "❌ Something went wrong, the panic button is not working at this time\.",
                -1,
            ]
    except Exception as e:
        logger.error(f"❌ Unable to perform actions on the database: {e}", exc_info=True)
        return ["❌ Unable to perform actions on the database\.", -1]
