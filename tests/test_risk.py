from datetime import datetime, timedelta, timezone

import pytest

from goldbot.risk import AccountType, BalanceFile, RiskManager, size_position, split_lots

STD = AccountType("standard", 100.0, 0.01, 0.01)
CENT = AccountType("cent", 1.0, 0.01, 0.01)
MICRO = AccountType("micro", 10.0, 0.01, 0.01)
T0 = datetime(2025, 3, 4, 9, 0, tzinfo=timezone.utc)


def test_standard_account_skips_wide_stop():
    r = size_position(50, stop_distance=8.0, acct=STD, risk_pct=1.0)
    assert r.skip and "SKIP - risk too high for balance" in r.note
    assert r.risk_usd == pytest.approx(8.0)          # 1 oz x $8


def test_standard_min_lot_allowed_within_max_risk():
    # $0.80 stop on 1 oz = $0.80 = 1.6%: above the 1% target but within the 2% cap
    r = size_position(50, 0.80, STD, risk_pct=1.0, max_risk_pct=2.0)
    assert not r.skip and r.lots == 0.01 and r.risk_usd == pytest.approx(0.80)
    assert "above" in r.note


def test_standard_min_lot_over_cap_skips():
    r = size_position(50, 1.20, STD, risk_pct=1.0, max_risk_pct=2.0)
    assert r.skip


def test_cent_account_sizes_down():
    r = size_position(50, 10.0, CENT, risk_pct=1.0)
    assert not r.skip
    assert r.lots == 0.05 and r.oz == pytest.approx(0.05)
    assert r.risk_usd == pytest.approx(0.50)
    assert r.risk_pct <= 1.0 + 1e-9


def test_size_rounds_down_never_up():
    r = size_position(50, 7.0, CENT, risk_pct=1.0)       # 0.5/7 = 0.0714 -> 0.07
    assert r.lots == 0.07
    assert r.risk_usd <= 0.50


def test_costs_are_included_in_risk():
    r = size_position(50, 9.45, CENT, risk_pct=1.0, extra_cost_per_oz=0.55)   # 10/oz all-in
    assert r.lots == 0.05 and r.risk_usd == pytest.approx(0.50)


def test_risk_pct_hard_capped_at_2():
    r = size_position(50, 1.0, CENT, risk_pct=5.0, max_risk_pct=5.0)
    assert r.risk_usd <= 1.0 + 1e-9


def test_invalid_inputs_skip():
    assert size_position(0, 5, CENT, 1).skip
    assert size_position(50, 0, CENT, 1).skip


def test_micro_account():
    r = size_position(50, 5.0, MICRO, 1.0)    # 0.01 lot = 0.1 oz -> $0.50 at $5 stop
    assert r.lots == 0.01 and r.risk_usd == pytest.approx(0.50)


def test_split_lots():
    assert split_lots(0.05, CENT, 0.5) == 0.02
    assert split_lots(0.01, CENT, 0.5) == 0.0        # cannot split the minimum lot
    assert split_lots(0.02, CENT, 0.5) == 0.01


def test_one_open_signal_at_a_time():
    rm = RiskManager()
    assert rm.can_open(T0, 50)[0]
    rm.on_open(T0, 50)
    ok, why = rm.can_open(T0 + timedelta(minutes=15), 50)
    assert not ok and "already open" in why
    rm.on_close(T0 + timedelta(hours=1), 1.0, 50)
    assert rm.can_open(T0 + timedelta(hours=1), 51)[0]


def test_max_three_signals_per_day():
    rm = RiskManager()
    for k in range(3):
        t = T0 + timedelta(hours=k)
        assert rm.can_open(t, 50)[0]
        rm.on_open(t, 50)
        rm.on_close(t + timedelta(minutes=30), 0.5, 50)
    ok, why = rm.can_open(T0 + timedelta(hours=5), 51.5)
    assert not ok and "3 signals" in why
    assert rm.can_open(T0 + timedelta(days=1), 51.5)[0]     # resets next day


def test_stop_after_two_losses():
    rm = RiskManager()
    for k in range(2):
        t = T0 + timedelta(hours=k)
        rm.on_open(t, 50)
        rm.on_close(t + timedelta(minutes=30), -0.5, 50)
    ok, why = rm.can_open(T0 + timedelta(hours=3), 49)
    assert not ok and "2 losses" in why


def test_stop_after_daily_loss_pct():
    rm = RiskManager(max_losses_per_day=99)
    rm.on_open(T0, 50)
    rm.on_close(T0 + timedelta(minutes=30), -2.0, 50)    # exactly 4% of $50
    ok, why = rm.can_open(T0 + timedelta(hours=1), 48)
    assert not ok and "4.0%" in why


def test_day_boundary_uses_configured_tz():
    rm = RiskManager(tz="America/New_York", max_losses_per_day=1)
    late = datetime(2025, 3, 5, 3, 0, tzinfo=timezone.utc)   # 22:00 NY on Mar 4
    rm.on_open(late, 50)
    rm.on_close(late, -0.5, 50)
    assert not rm.can_open(datetime(2025, 3, 5, 4, 0, tzinfo=timezone.utc), 49.5)[0]   # still Mar 4 in NY
    assert rm.can_open(datetime(2025, 3, 5, 6, 0, tzinfo=timezone.utc), 49.5)[0]       # Mar 5 in NY


def test_balance_file_roundtrip(tmp_path):
    bf = BalanceFile(tmp_path / "b.json", 50.0)
    assert bf.read() == 50.0
    bf.write(52.346)
    assert bf.read() == 52.35
