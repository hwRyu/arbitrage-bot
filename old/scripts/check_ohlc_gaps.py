#!/usr/bin/env python3
"""
OHLC 갭 체커
OHLC 데이터 수집에서 누락된 날짜를 식별합니다
"""
import sys
from datetime import datetime, timedelta

from config.settings import OHLCConfig
from utils.ohlc_utils import find_missing_dates, get_ohlc_summary
from models.database import init_db


def main():
    """메인 진입점"""
    print("=" * 60)
    print("OHLC Data Gap Checker")
    print("=" * 60)
    print()

    # Initialize DB
    init_db()

    # Get target coins
    coins = OHLCConfig.get_target_coins()
    timeframe = '1d'

    # Check last 30 days
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    print(f"Checking period: {start_date} to {end_date}")
    print(f"Target coins: {', '.join(coins)}")
    print()

    total_missing = 0

    for coin in coins:
        print(f"\n[{coin}]")
        print("-" * 40)

        # Find missing dates
        missing = find_missing_dates(coin, timeframe, start_date, end_date)

        if missing:
            print(f"Missing dates: {len(missing)}")
            for date in missing[:10]:  # Show first 10
                print(f"  - {date}")
            if len(missing) > 10:
                print(f"  ... and {len(missing) - 10} more")
            total_missing += len(missing)
        else:
            print("No missing dates")

        # Show summary
        summary = get_ohlc_summary(coin, timeframe, days=30)
        if 'error' not in summary:
            print(f"Data quality: {summary['data_quality']:.1f} scans/candle")
            print(f"Avg premium: {summary['avg_premium']:.2f}%")

    print()
    print("=" * 60)
    print(f"Total missing dates across all coins: {total_missing}")
    print("=" * 60)


if __name__ == "__main__":
    main()
