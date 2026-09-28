from hermes.market.absorption import derive_absorption_context
from hermes.market.patterns import PatternSnapshot, PatternWindow
from hermes.market.structure import StructureSnapshot, StructureWindow


def sw(
    seconds,
    *,
    rep_bid=0,
    rep_ask=0,
    rep_ev_bid=0,
    rep_ev_ask=0,
    buy=0,
    sell=0,
    edge=0,
):
    return StructureWindow(
        seconds=seconds,
        added_bid=0,
        removed_bid=0,
        added_ask=0,
        removed_ask=0,
        replenished_bid=rep_bid,
        replenished_ask=rep_ask,
        replenish_events_bid=rep_ev_bid,
        replenish_events_ask=rep_ev_ask,
        known_buy_at_ask=buy,
        known_sell_at_bid=sell,
        edge_visibility_events=edge,
    )


def pw(
    seconds,
    *,
    buy_sweeps=0,
    sell_sweeps=0,
    buy_sweep_vol=0,
    sell_sweep_vol=0,
    buy_follow=0,
    sell_follow=0,
    buy_no=0,
    sell_no=0,
):
    return PatternWindow(
        seconds=seconds,
        buy_sweeps=buy_sweeps,
        sell_sweeps=sell_sweeps,
        buy_sweep_volume=buy_sweep_vol,
        sell_sweep_volume=sell_sweep_vol,
        max_sweep_levels=0,
        buy_follow_volume=buy_follow,
        sell_follow_volume=sell_follow,
        buy_no_follow_volume=buy_no,
        sell_no_follow_volume=sell_no,
        follow_events=0,
        no_follow_events=0,
    )


def structure(*wins, available=True, reason="ok", epoch=0):
    return StructureSnapshot(
        continuity_epoch=epoch,
        available=available,
        reason=reason,
        current_levels=(),
        windows=tuple(wins),
    )


def patterns(*wins, available=True, reason="ok", epoch=0):
    return PatternSnapshot(
        continuity_epoch=epoch,
        available=available,
        reason=reason,
        latest_sweeps=(),
        latest_follow=(),
        windows=tuple(wins),
    )


def base():
    return (
        structure(sw(1), sw(5), sw(30)),
        patterns(pw(1), pw(5), pw(30)),
    )


def test_available_with_complete_windows():
    s, p = base()
    a = derive_absorption_context(s, p)
    assert a.available
    assert a.reason == "ok"
    assert [w.seconds for w in a.windows] == [1, 5, 30]


def test_buy_context_maps_to_ask_side():
    s = structure(
        sw(1, rep_ask=7, rep_ev_ask=2, buy=10),
        sw(5),
        sw(30),
    )
    p = patterns(
        pw(1, buy_follow=3, buy_no=6, buy_sweep_vol=8, buy_sweeps=1),
        pw(5),
        pw(30),
    )
    c = derive_absorption_context(s, p).windows[0].buy_vs_ask
    assert c.aggressive_volume == 10
    assert c.replenished_volume == 7
    assert c.replenish_events == 2
    assert c.follow_volume == 3
    assert c.no_follow_volume == 6
    assert c.sweep_volume == 8
    assert c.sweep_events == 1


def test_sell_context_maps_to_bid_side():
    s = structure(
        sw(1, rep_bid=5, rep_ev_bid=1, sell=9),
        sw(5),
        sw(30),
    )
    p = patterns(
        pw(1, sell_follow=2, sell_no=4, sell_sweep_vol=6, sell_sweeps=2),
        pw(5),
        pw(30),
    )
    c = derive_absorption_context(s, p).windows[0].sell_vs_bid
    assert (c.aggressive_volume, c.replenished_volume, c.no_follow_volume) == (9, 5, 4)
    assert c.sweep_events == 2


def test_compatible_cap_is_conservative_minimum_not_sum():
    s = structure(sw(1, rep_ask=7, buy=10), sw(5), sw(30))
    p = patterns(pw(1, buy_no=6), pw(5), pw(30))
    c = derive_absorption_context(s, p).windows[0].buy_vs_ask
    assert c.compatible_cap_volume == 6
    assert c.compatible_cap_fraction == 0.6


def test_sweep_compatible_cap_requires_fourth_bucket():
    s = structure(sw(1, rep_ask=9, buy=12), sw(5), sw(30))
    p = patterns(pw(1, buy_no=8, buy_sweep_vol=5), pw(5), pw(30))
    c = derive_absorption_context(s, p).windows[0].buy_vs_ask
    assert c.compatible_cap_volume == 8
    assert c.sweep_compatible_cap_volume == 5


def test_zero_aggression_has_no_fraction_and_zero_cap():
    s, p = base()
    c = derive_absorption_context(s, p).windows[0].buy_vs_ask
    assert c.replenishment_fraction is None
    assert c.compatible_cap_fraction is None
    assert c.compatible_cap_volume == 0


def test_no_follow_fraction_uses_only_resolved_follow_volume():
    s = structure(sw(1, buy=20), sw(5), sw(30))
    p = patterns(pw(1, buy_follow=6, buy_no=4), pw(5), pw(30))
    c = derive_absorption_context(s, p).windows[0].buy_vs_ask
    assert c.resolved_follow_volume == 10
    assert c.no_follow_fraction == 0.4


def test_replenishment_fraction_is_capped_at_one():
    s = structure(sw(1, rep_ask=20, buy=5), sw(5), sw(30))
    p = patterns(pw(1), pw(5), pw(30))
    c = derive_absorption_context(s, p).windows[0].buy_vs_ask
    assert c.replenishment_fraction == 1.0


def test_edge_visibility_is_exposed_but_not_absorption_volume():
    s = structure(sw(1, rep_ask=3, buy=4, edge=9), sw(5), sw(30))
    p = patterns(pw(1, buy_no=2), pw(5), pw(30))
    w = derive_absorption_context(s, p).windows[0]
    assert w.edge_visibility_events == 9
    assert w.buy_vs_ask.compatible_cap_volume == 2


def test_unavailable_structure_blocks_fused_context():
    s = structure(available=False, reason="book_suspect")
    p = patterns(pw(1), pw(5), pw(30))
    a = derive_absorption_context(s, p)
    assert not a.available
    assert a.reason == "structure:book_suspect"
    assert not a.windows


def test_unavailable_patterns_blocks_fused_context():
    s = structure(sw(1), sw(5), sw(30))
    p = patterns(available=False, reason="book_stale")
    a = derive_absorption_context(s, p)
    assert not a.available
    assert a.reason == "patterns:book_stale"


def test_missing_window_fails_closed():
    s = structure(sw(1), sw(5), sw(30))
    p = patterns(pw(1), pw(5))
    a = derive_absorption_context(s, p)
    assert not a.available
    assert a.reason == "missing_windows:30"


def test_epoch_pair_is_preserved_without_requiring_equality():
    s = structure(sw(1), sw(5), sw(30), epoch=2)
    p = patterns(pw(1), pw(5), pw(30), epoch=5)
    a = derive_absorption_context(s, p)
    assert a.available
    assert a.structure_epoch == 2
    assert a.pattern_epoch == 5
